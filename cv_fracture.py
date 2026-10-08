"""cv_fracture — детект РАЗЛОМОВ/осколков кристаллов на OpenCV (без YOLO).

Отдельная подсистема компьютерного зрения (не путать с cv_analyzer, который считает кристаллы).
Разлом = сплошная ТЁМНАЯ масса с высокой внутренней текстурой (много тонких тёмных прожилок).
Целый/склеенный кристалл светлее и с гладкими гранями → низкая уверенность. Потрескавшийся
(светлый с тонкими трещинами) не даёт сплошного тёмного пятна → тоже отсекается.

Метрики зоны:
  D (darkness) — тёмность 0..1 (лопнутый непрозрачный → высокая).
  T (texture)  — плотность краёв внутри 0..1 (хаос осколков → высокая).
  C (conf)     — итог = w_dark*D + w_tex*T; порог conf_thr отсекает разлом от склеенного.

Подтверждение по серии кадров: разлом при передавливании прижат и НЕ двигается, значит зона
держится в одном месте на нескольких кадрах пробы. Зона засчитывается только если найдена
на >= confirm_frames кадрах в одном месте (по IoU) — это убирает фейки (тень, пузырёк, дрейф).
"""
from __future__ import annotations

import cv2
import numpy as np

DEFAULTS = {
    "enabled": True,       # Часть B: детект разломов работает всегда, пока включён CV
    "conf_thr": 0.6,       # порог уверенности «это разлом» (правится на вкладке «Разломы»)
    "w_dark": 0.6,         # вес тёмности в итоговой уверенности
    "w_tex": 0.4,          # вес текстуры
    "dark_thr": 30,        # пиксель темнее (медиана-порог) считаем тёмным
    "min_area_frac": 0.0018,   # зона мельче этой доли кадра — не разлом (грань/пыль)
    "confirm_frames": 3,   # на скольких кадрах серии зона должна держаться, чтобы подтвердиться
    "iou": 0.3,            # порог совпадения зон между кадрами
}

# цвета overlay (BGR)
COL_CONFIRMED = (0, 0, 255)     # красный — подтверждённый разлом
COL_MAYBE = (0, 165, 255)       # оранжевый — кандидат (не подтверждён серией)


def _cfg(fr):
    c = dict(DEFAULTS)
    if fr:
        c.update({k: fr[k] for k in fr if k in DEFAULTS})
    return c


def detect_zones(image: np.ndarray, fr_cfg: dict | None = None) -> list[dict]:
    """Найти зоны-кандидаты разломов на ОДНОМ кадре. Возвращает список зон с bbox/poly/conf/D/T."""
    c = _cfg(fr_cfg)
    h, w = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    # нормализация неравномерного света/виньетки
    k = max(31, (min(h, w) // 6) | 1)
    bg = cv2.GaussianBlur(gray, (k, k), 0)
    norm = cv2.divide(gray, bg, scale=128).astype(np.uint8)

    med = int(np.median(norm))
    dark = (norm < med - int(c["dark_thr"])).astype(np.float32)
    win = max(21, (min(h, w) // 22) | 1)
    dark_frac = cv2.boxFilter(dark, ddepth=-1, ksize=(win, win))
    edges = (cv2.Canny(norm, 40, 120) > 0).astype(np.float32)
    tex = np.clip(cv2.boxFilter(edges, ddepth=-1, ksize=(win, win)) * 6.0, 0, 1)

    # зона разлома = тёмное И текстурное одновременно; гасим углы-виньетку (эллипс обзора)
    score = np.clip(dark_frac, 0, 1) * tex
    score = cv2.normalize(score, None, 0, 1, cv2.NORM_MINMAX)
    roi = np.zeros((h, w), np.float32)
    cv2.ellipse(roi, (w // 2, h // 2), (int(w * 0.48), int(h * 0.48)), 0, 0, 360, 1, -1)
    score = score * roi

    mask = (score > 0.30).astype(np.uint8) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (win // 2 | 1, win // 2 | 1))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    min_area = float(c["min_area_frac"]) * h * w
    zones = []
    for cnt in cnts:
        a = cv2.contourArea(cnt)
        if a < min_area:
            continue
        m = np.zeros((h, w), np.uint8)
        cv2.drawContours(m, [cnt], -1, 255, -1)
        mean_bright = float(norm[m > 0].mean())
        D = float(np.clip((128 - mean_bright) / 55.0, 0, 1))
        T = float(np.clip(float(edges[m > 0].mean()) * 5.0, 0, 1))
        conf = float(c["w_dark"]) * D + float(c["w_tex"]) * T
        x, y, ww, hh = cv2.boundingRect(cnt)
        poly = cv2.approxPolyDP(cnt, 0.008 * cv2.arcLength(cnt, True), True).reshape(-1, 2)
        zones.append({
            "bbox": [int(x), int(y), int(ww), int(hh)],
            "poly": [[int(px), int(py)] for px, py in poly],
            "conf": round(conf, 3), "D": round(D, 3), "T": round(T, 3),
            "area": int(a),
        })
    return zones


def _iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix1, iy1 = max(ax, bx), max(ay, by)
    ix2, iy2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    return inter / (aw * ah + bw * bh - inter)


def confirm(frame_zone_lists: list[list[dict]], fr_cfg: dict | None = None) -> tuple[list[dict], dict]:
    """Свести зоны по кадрам: зона ПОДТВЕРЖДЕНА, если держится в одном месте на >=confirm_frames
    кадрах (по IoU). Возвращает (подтверждённые зоны, сводка).

    Каждая подтверждённая зона представлена самым уверенным своим экземпляром + число кадров.
    """
    c = _cfg(fr_cfg)
    thr_conf = float(c["conf_thr"])
    need = int(c["confirm_frames"])
    iou_thr = float(c["iou"])

    # кластеризуем зоны с conf>=порога по перекрытию между кадрами
    clusters = []   # каждый: {"members":[zone...], "frames":set(idx), "bbox":ref}
    for fi, zones in enumerate(frame_zone_lists):
        for z in zones:
            if z["conf"] < thr_conf:
                continue
            placed = False
            for cl in clusters:
                if _iou(cl["bbox"], z["bbox"]) >= iou_thr:
                    cl["members"].append(z)
                    cl["frames"].add(fi)
                    if z["conf"] > cl["best"]["conf"]:
                        cl["best"] = z
                        cl["bbox"] = z["bbox"]
                    placed = True
                    break
            if not placed:
                clusters.append({"members": [z], "frames": {fi}, "best": z, "bbox": z["bbox"]})

    confirmed = []
    for cl in clusters:
        if len(cl["frames"]) >= need:
            z = dict(cl["best"])
            z["frames_seen"] = len(cl["frames"])
            confirmed.append(z)
    confirmed.sort(key=lambda z: z["conf"], reverse=True)

    n_frames = max(1, len(frame_zone_lists))
    total_area = sum(z["area"] for z in confirmed)
    frame_area = None
    # доля площади считаем относительно первого кадра, если он был
    summary = {
        "zones": len(confirmed),
        "candidates": sum(len([z for z in zl if z["conf"] >= thr_conf]) for zl in frame_zone_lists),
        "mean_conf": round(float(np.mean([z["conf"] for z in confirmed])), 3) if confirmed else 0.0,
        "confirm_frames": need,
        "has_fracture": len(confirmed) > 0,
    }
    return confirmed, summary


def candidates(frame_zone_lists: list[list[dict]], confirmed: list[dict], fr_cfg: dict | None = None,
               min_conf: float = 0.25, limit: int = 12) -> list[dict]:
    """Кандидаты на разлом, которые НЕ стали подтверждёнными (уверенность ниже порога или держатся на слишком малом числе
    кадров): сохраняются в пробу, чтобы оператор мог сам решить — «да, это разлом». Каждый — лучший экземпляр кластера
    по кадрам, с числом кадров. Зоны, совпадающие с подтверждёнными, пропускаются."""
    c = _cfg(fr_cfg)
    iou_thr = float(c["iou"])
    clusters = []
    for fi, zones in enumerate(frame_zone_lists):
        for z in zones:
            if z["conf"] < min_conf:
                continue
            for cl in clusters:
                if _iou(cl["bbox"], z["bbox"]) >= iou_thr:
                    cl["frames"].add(fi)
                    if z["conf"] > cl["best"]["conf"]:
                        cl["best"], cl["bbox"] = z, z["bbox"]
                    break
            else:
                clusters.append({"best": z, "bbox": z["bbox"], "frames": {fi}})
    out = []
    for cl in sorted(clusters, key=lambda k: k["best"]["conf"], reverse=True):
        if any(_iou(cl["bbox"], z["bbox"]) >= iou_thr for z in confirmed):
            continue
        z = dict(cl["best"])
        z["frames_seen"] = len(cl["frames"])
        out.append(z)
        if len(out) >= limit:
            break
    return out


def number_zones(confirmed: list[dict], cands: list[dict]) -> None:
    """Проставить зонам стабильные id (по ним правка из интерфейса): a0.. — подтверждённые, c0.. — кандидаты."""
    for i, z in enumerate(confirmed):
        z["id"], z["src"] = "a%d" % i, "auto"
    for i, z in enumerate(cands):
        z["id"], z["src"] = "c%d" % i, "candidate"


def draw(overlay: np.ndarray, zones: list[dict], confirmed: bool = True) -> np.ndarray:
    """Нарисовать зоны разломов контуром поверх кадра (поверх кристаллов). Возвращает тот же кадр."""
    col = COL_CONFIRMED if confirmed else COL_MAYBE
    for z in zones:
        pts = np.array(z["poly"], dtype=np.int32).reshape(-1, 1, 2)
        cv2.polylines(overlay, [pts], True, col, 3)
        x, y, _, _ = z["bbox"]
        lbl = "razlom %.2f" % z["conf"]
        cv2.putText(overlay, lbl, (x, max(14, y - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, col, 2, cv2.LINE_AA)
    return overlay


def area_pct(zones: list[dict], image_shape) -> float:
    h, w = image_shape[:2]
    return round(100.0 * sum(z["area"] for z in zones) / (h * w), 2) if h and w else 0.0
