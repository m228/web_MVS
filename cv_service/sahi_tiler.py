"""SAHI-тайлинг: режем большой кадр на тайлы, прогоняем детектор по каждому, склеиваем.

Зачем: кадр микроскопа большой, мелкие кристаллы на полном кадре занимают несколько
пикселей и теряются. Нарезав кадр на 4-6 перекрывающихся тайлов, каждый тайл прогоняем
в исходном разрешении — мелочь становится крупной относительно тайла и детектится.
Потом координаты объектов переводим обратно в полный кадр и убираем дубли на швах (NMS).

Модуль не зависит от бэкенда детектора — принимает любой Detector (в т.ч. заглушку).
"""
from __future__ import annotations

import math
import time
from typing import Optional

import numpy as np

from detector import Detection, Detector


def _grid(cols_rows: int) -> tuple[int, int]:
    """Число тайлов -> (столбцы, строки). 4->2x2, 6->3x2, иначе близко к квадрату."""
    presets = {1: (1, 1), 2: (2, 1), 4: (2, 2), 6: (3, 2), 9: (3, 3)}
    if cols_rows in presets:
        return presets[cols_rows]
    cols = int(math.ceil(math.sqrt(cols_rows)))
    rows = int(math.ceil(cols_rows / cols))
    return cols, rows


def _iou(a: tuple, b: tuple) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def nms(dets: list[Detection], iou_thr: float = 0.45) -> list[Detection]:
    """Жадный NMS по боксам — убирает дубли одного кристалла с соседних тайлов."""
    kept: list[Detection] = []
    for d in sorted(dets, key=lambda x: x.conf, reverse=True):
        if all(_iou(d.bbox, k.bbox) < iou_thr for k in kept):
            kept.append(d)
    return kept


def run_tiled(detector: Detector, image: np.ndarray, tiles: int = 6,
              overlap: float = 0.15, conf: float = 0.25,
              iou_thr: float = 0.45) -> tuple[list[Detection], dict]:
    """Прогнать детектор по тайлам и вернуть (объекты в координатах полного кадра, тайминги).

    overlap — доля перекрытия соседних тайлов (чтобы кристалл на шве не терялся).
    tiles<=1 — прогон по всему кадру без нарезки.
    """
    h, w = image.shape[:2]
    t0 = time.perf_counter()

    if tiles <= 1:
        cols, rows = 1, 1
    else:
        cols, rows = _grid(tiles)

    tile_w = w / cols
    tile_h = h / rows
    ov_x = tile_w * overlap
    ov_y = tile_h * overlap

    all_dets: list[Detection] = []
    t_infer = 0.0
    for r in range(rows):
        for c in range(cols):
            x1 = max(0, int(c * tile_w - ov_x))
            y1 = max(0, int(r * tile_h - ov_y))
            x2 = min(w, int((c + 1) * tile_w + ov_x))
            y2 = min(h, int((r + 1) * tile_h + ov_y))
            crop = image[y1:y2, x1:x2]
            if crop.size == 0:
                continue
            ti = time.perf_counter()
            local = detector.infer(crop, conf=conf)
            t_infer += time.perf_counter() - ti
            # координаты тайла -> полный кадр
            for d in local:
                bx1, by1, bx2, by2 = d.bbox
                poly = ([[px + x1, py + y1] for px, py in d.polygon]
                        if d.polygon else None)
                all_dets.append(Detection(
                    bbox=(bx1 + x1, by1 + y1, bx2 + x1, by2 + y1),
                    conf=d.conf, polygon=poly,
                ))

    t_nms = time.perf_counter()
    merged = nms(all_dets, iou_thr=iou_thr)
    timing = {
        "tiles": cols * rows,
        "grid": f"{cols}x{rows}",
        "infer_ms": round(t_infer * 1000, 1),
        "nms_ms": round((time.perf_counter() - t_nms) * 1000, 1),
        "total_ms": round((time.perf_counter() - t0) * 1000, 1),
        "raw": len(all_dets),
        "kept": len(merged),
    }
    return merged, timing
