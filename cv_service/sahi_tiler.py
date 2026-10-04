"""Нарезка кадра на части (SAHI) + общий проход по всему кадру + одна склейка результатов.

Зачем нарезка: кадр микроскопа большой, мелкие кристаллы на полном кадре занимают несколько
пикселей и теряются. В части кадра, прогнанной в исходном разрешении, мелочь становится
крупной и детектится.

Зачем общий проход: кристалл, попавший на шов между частями, режется пополам — получаются
обрубки с прямым срезом (ложный «брак», заниженный размер, двойные контуры). На проходе по
ВСЕМУ кадру крупные кристаллы находятся целыми.

Склейка (merge_detections) — одна на всё:
  1. «целые» объекты (не упираются во внутренний шов): из частей и с общего прохода; дубли
     одного кристалла убираем по перекрытию МАСОК (IoU), а не прямоугольников. При дубле
     оставляем объект из части кадра — у него контур точнее (выше разрешение);
  2. «обрезки» (объект из части, упёршийся во внутренний шов): если тот же кристалл найден
     целиком — обрезок выбрасываем; если нет — оставляем с пометкой cut (в статистику размера
     он не идёт, см. cv_analyzer);
  3. объекты, упёршиеся в край КАДРА, помечаем edge — они обрезаны самим кадром.

Модуль не зависит от бэкенда детектора — принимает любой Detector (в т.ч. заглушку).
"""
from __future__ import annotations

import math
import time

import cv2
import numpy as np

from detector import Detection, Detector

EDGE_TOL = 3.0       # px: бокс ближе этого к границе — объект ею обрезан
COVER_IOS = 0.6      # обрезок «накрыт» целым кристаллом: доля пересечения от площади обрезка
RASTER_MAX = 96      # сторона растра для сравнения масок (точности хватает, считается быстро)


def _grid(cols_rows: int) -> tuple[int, int]:
    """Число частей -> (столбцы, строки). 4->2x2, 6->3x2, иначе близко к квадрату."""
    presets = {1: (1, 1), 2: (2, 1), 4: (2, 2), 6: (3, 2), 9: (3, 3)}
    if cols_rows in presets:
        return presets[cols_rows]
    cols = int(math.ceil(math.sqrt(cols_rows)))
    rows = int(math.ceil(cols_rows / cols))
    return cols, rows


def _overlap(a: Detection, b: Detection) -> tuple[float, float]:
    """Перекрытие двух объектов по МАСКАМ: (IoU, IoS). IoS — пересечение / площадь меньшего
    (≈1, когда один объект целиком лежит в другом). Нет полигона — считаем по боксу."""
    ax1, ay1, ax2, ay2 = a.bbox
    bx1, by1, bx2, by2 = b.bbox
    iw = min(ax2, bx2) - max(ax1, bx1)
    ih = min(ay2, by2) - max(ay1, by1)
    if iw <= 0 or ih <= 0:
        return 0.0, 0.0
    if not a.polygon or not b.polygon:
        inter = iw * ih
        area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
        area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    else:
        # растеризуем оба полигона в общий прямоугольник, уменьшив до RASTER_MAX по стороне
        x0, y0 = min(ax1, bx1), min(ay1, by1)
        rw, rh = max(ax2, bx2) - x0, max(ay2, by2) - y0
        s = min(1.0, RASTER_MAX / max(rw, rh, 1.0))
        shape = (max(1, int(math.ceil(rh * s)) + 1), max(1, int(math.ceil(rw * s)) + 1))

        def raster(poly):
            m = np.zeros(shape, dtype=np.uint8)
            pts = ((np.asarray(poly, dtype=np.float32) - (x0, y0)) * s).round().astype(np.int32)
            cv2.fillPoly(m, [pts.reshape(-1, 1, 2)], 1)
            return m

        ma, mb = raster(a.polygon), raster(b.polygon)
        inter = float(np.count_nonzero(ma & mb))
        area_a, area_b = float(np.count_nonzero(ma)), float(np.count_nonzero(mb))
    if inter <= 0 or area_a <= 0 or area_b <= 0:
        return 0.0, 0.0
    return inter / (area_a + area_b - inter), inter / min(area_a, area_b)


def _near(d: Detection, others: list[Detection]) -> list[Detection]:
    """Объекты из others, чьи боксы пересекаются с боксом d (быстрый отсев перед масками)."""
    if not others:
        return []
    b = np.asarray([o.bbox for o in others], dtype=np.float32)
    x1, y1, x2, y2 = d.bbox
    hit = (b[:, 0] < x2) & (b[:, 2] > x1) & (b[:, 1] < y2) & (b[:, 3] > y1)
    return [others[i] for i in np.nonzero(hit)[0]]


def _dedupe(dets: list[Detection], kept: list[Detection], iou_thr: float) -> list[Detection]:
    """Добавить в kept объекты из dets (в их порядке), пропуская дубли уже принятых."""
    for d in dets:
        if all(_overlap(d, k)[0] < iou_thr for k in _near(d, kept)):
            kept.append(d)
    return kept


def merge_detections(full: list[Detection], tile_whole: list[Detection],
                     tile_seam: list[Detection], iou_thr: float = 0.45) -> tuple[list[Detection], dict]:
    """Одна склейка: общий проход + части кадра → объекты без дублей и обрубков на швах.

    full — объекты с прохода по всему кадру; tile_whole — из частей, не касаются внутреннего
    шва; tile_seam — из частей, упёрлись во внутренний шов (обрезаны им).
    """
    by_conf = lambda d: d.conf
    # 1) целые: сначала из частей (точнее контур), потом с общего прохода
    kept: list[Detection] = []
    _dedupe(sorted(tile_whole, key=by_conf, reverse=True), kept, iou_thr)
    n_tile = len(kept)
    _dedupe(sorted(full, key=by_conf, reverse=True), kept, iou_thr)
    n_full = len(kept) - n_tile
    # 2) обрезки на швах: накрыт целым кристаллом → выбросить; иначе оставить с пометкой cut
    whole = list(kept)
    orphans = [s for s in sorted(tile_seam, key=by_conf, reverse=True)
               if all(_overlap(s, k)[1] < COVER_IOS for k in _near(s, whole))]
    for s in orphans:
        s.cut = True
    n_before = len(kept)
    _dedupe(orphans, kept, iou_thr)
    stats = {"from_tiles": n_tile, "from_full": n_full,
             "seam_dropped": len(tile_seam) - (len(kept) - n_before),
             "cut_kept": len(kept) - n_before}
    return kept, stats


def run_tiled(detector: Detector, image: np.ndarray, tiles: int = 6,
              overlap: float = 0.15, conf: float = 0.25,
              iou_thr: float = 0.45) -> tuple[list[Detection], dict]:
    """Общий проход по кадру + (при tiles>1) проход по частям → одна склейка.

    Возвращает (объекты в координатах полного кадра, тайминги). overlap — доля перекрытия
    соседних частей. tiles<=1 — только общий проход, без нарезки.
    """
    h, w = image.shape[:2]
    t0 = time.perf_counter()
    cols, rows = (1, 1) if tiles <= 1 else _grid(tiles)

    # --- общий проход: крупные кристаллы целиком ---
    ti = time.perf_counter()
    full = detector.infer(image, conf=conf)
    t_full = time.perf_counter() - ti

    # --- части кадра: мелочь в исходном разрешении ---
    tile_whole: list[Detection] = []
    tile_seam: list[Detection] = []
    t_tiles = 0.0
    if cols * rows > 1:
        tile_w, tile_h = w / cols, h / rows
        ov_x, ov_y = tile_w * overlap, tile_h * overlap
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
                t_tiles += time.perf_counter() - ti
                cw, ch = x2 - x1, y2 - y1
                for d in local:
                    bx1, by1, bx2, by2 = d.bbox
                    # упёрся во ВНУТРЕННИЙ шов (граница части, не совпадающая с краем кадра)?
                    seam = ((x1 > 0 and bx1 <= EDGE_TOL) or (y1 > 0 and by1 <= EDGE_TOL)
                            or (x2 < w and bx2 >= cw - EDGE_TOL) or (y2 < h and by2 >= ch - EDGE_TOL))
                    # координаты части -> полный кадр
                    poly = ([[px + x1, py + y1] for px, py in d.polygon] if d.polygon else None)
                    moved = Detection(bbox=(bx1 + x1, by1 + y1, bx2 + x1, by2 + y1),
                                      conf=d.conf, polygon=poly)
                    (tile_seam if seam else tile_whole).append(moved)

    t_merge = time.perf_counter()
    merged, stats = merge_detections(full, tile_whole, tile_seam, iou_thr=iou_thr)
    # объект упёрся в край КАДРА — обрезан самим кадром
    for d in merged:
        bx1, by1, bx2, by2 = d.bbox
        d.edge = bx1 <= EDGE_TOL or by1 <= EDGE_TOL or bx2 >= w - EDGE_TOL or by2 >= h - EDGE_TOL
    merged.sort(key=lambda d: d.conf, reverse=True)

    timing = {
        "tiles": cols * rows,
        "grid": f"{cols}x{rows}",
        "infer_ms": round((t_full + t_tiles) * 1000, 1),
        "full_ms": round(t_full * 1000, 1),
        "nms_ms": round((time.perf_counter() - t_merge) * 1000, 1),
        "total_ms": round((time.perf_counter() - t0) * 1000, 1),
        "raw": len(full) + len(tile_whole) + len(tile_seam),
        "kept": len(merged),
        **stats,
    }
    return merged, timing
