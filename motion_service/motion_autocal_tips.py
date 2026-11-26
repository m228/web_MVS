"""Автоподгонка светоотражателей на концах палки: цвет кончика, ось по прямым «кончик—кончик», длина и кольцо кончика, «тонкое» полоски.

Зачем: ось, сбитая на 25 px (разметка кликом), ломает вал целиком — стрелки переворачиваются, зеркало уходит на десятки градусов (ролики 24.11).
А кончики отражателей — лучшая опора для оси: прямая через два противоположных кончика всегда проходит через ось палки, и по сотне кадров
пересечение прямых даёт центр с разбросом 2–4 px. Здесь только чистые функции (без записи кадров, без CycleRT): их зовёт RodTuner.stage_tips.

Как:
  1. маска по «ящикам» HSV (посев — ящики tip_hsv зоны) + закрытие 5×5 → пятна; неподвижные (одно место >12 % кадров) вон;
  2. на кадре, где палка найдена, берём по самому большому пятну в клине ±15° вокруг найденного угла и напротив → пара; нет найденных
     кадров (<40 %) — любая пара пятен ≥150° в кольце 0,35–1,4 R;
  3. МНК-точка, ближайшая ко всем прямым через пары (5 проходов, далёкие прямые отбрасываем) → ось;
  4. по пятнам пар относительно новой оси — радиальное начало/конец, длина, ширина → tip_len, tip_r0/r1, tip_thin;
  5. цвет: пиксели вокруг пятен (яркие: V ≥ 200) → «белый» кластер (S мала) и «цветной» (мода оттенка ±15) → свои ящики. Берём подобранные,
     если на них пар не меньше 90 % от посева (иначе остаётся посев — «не хуже»).
Положение и размеры — в пикселях кадра записи; в доли радиуса их переводит RodTuner (радиус ещё может измениться).
"""
from __future__ import annotations

import math
from typing import Optional, Sequence

import cv2
import numpy as np

CLOSE = 5                      # закрытие маски: жёлтая и белая части одной полоски склеиваются в одно пятно
MIN_AREA, MAX_AREA, MIN_LEN = 15, 6000, 12
STATIC_CELL, STATIC_FRAC = 12, 0.12      # сетка (px) и доля кадров, после которой пятно на месте считается неподвижным бликом
WEDGE_DEG = 15.0               # клин вокруг найденного угла палки, где ищем кончик
RING = (0.35, 1.4)             # доли радиуса, где ищем кончики для прямых
PAIR_MIN_DEG = 150.0           # без угла палки: пятна пары не ближе этого друг к другу по углу
MIN_PAIRS = 20                 # меньше прямых — ось не считаем
MIN_SECTORS = 4                # направлений прямых (по 30° из шести) не меньше: иначе ось вдоль одной линии не определена
DIST_FRAC = 0.02               # допустимая медиана расстояния прямых до оси, доля радиуса (не меньше 4 px); на роликах 24.11 хорошие 2–4 px, шумные 13–21
WHITE_BOX = [0, 179, 0, 60, 235, 255]    # «пересвет»: белая часть полоски на val_Down (кнопка «+ пересвет» кладёт тот же)
SHIFT_MAX = 0.35               # ось не уезжает дальше этой доли радиуса от прежней: больше — подозрительно, не применяем
GATED_MIN_FOUND = 0.4          # доля кадров с найденной палкой, с которой клин вокруг угла работает
FIT_MIN_V = 200                # пиксели полоски для подбора цвета — только яркие
SIZE_LEN_K = 0.9               # tip_len = столько × медиана длины полоски: оба конца дают полную силу
RING_MARGIN = 0.08             # запас кольца кончиков, доли радиуса
MAX_BOXES = 3


def boxes_mask(hsv: np.ndarray, boxes: Sequence[Sequence[int]]) -> np.ndarray:
    """Маска по «ящикам» [H1, H2, S1, S2, V1, V2] (H1 > H2 — оттенок через ноль круга)."""
    m = np.zeros(hsv.shape[:2], np.uint8)
    for h1, h2, s1, s2, v1, v2 in boxes:
        if h1 <= h2:
            m |= cv2.inRange(hsv, (h1, s1, v1), (h2, s2, v2))
        else:
            m |= cv2.inRange(hsv, (h1, s1, v1), (179, s2, v2)) | cv2.inRange(hsv, (0, s1, v1), (h2, s2, v2))
    return m


def blobs_of(mask: np.ndarray) -> list:
    """Пятна маски (после закрытия): [{"c": (x, y) центр, "area", "pts": N×2 float32}]; слишком мелкие и крупные (лампа, стена) пропускаем."""
    m = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((CLOSE, CLOSE), np.uint8))
    n, lab, st, cen = cv2.connectedComponentsWithStats((m > 0).astype(np.uint8), connectivity=8)
    out = []
    for i in range(1, n):
        a = int(st[i, cv2.CC_STAT_AREA])
        if a < MIN_AREA or a > MAX_AREA:
            continue
        x0, y0, w, h = int(st[i, 0]), int(st[i, 1]), int(st[i, 2]), int(st[i, 3])
        ys, xs = np.nonzero(lab[y0:y0 + h, x0:x0 + w] == i)
        pts = np.stack([xs + x0, ys + y0], 1).astype(np.float32)
        (_, _), (rw, rh), _ = cv2.minAreaRect(pts)
        if max(rw, rh) < MIN_LEN:
            continue
        out.append({"c": (float(cen[i][0]), float(cen[i][1])), "area": a, "pts": pts})
    return out


def _cell(c) -> tuple:
    return int(round(c[0] / STATIC_CELL)), int(round(c[1] / STATIC_CELL))


def drop_static(frames_blobs: list) -> list:
    """Убрать неподвижные блики: пятно на одном месте (клетка 12 px и соседние) более чем в 12 % кадров — люк, рамка, лампа, а не кончик,
    пробегающий точку за доли оборота."""
    cnt: dict = {}
    for bl in frames_blobs:
        seen = {_cell(b["c"]) for b in bl}
        for c in seen:
            cnt[c] = cnt.get(c, 0) + 1
    lim = STATIC_FRAC * max(1, len(frames_blobs))
    stat = {c for c, v in cnt.items() if v > lim}
    if not stat:
        return frames_blobs
    out = []
    for bl in frames_blobs:
        keep = []
        for b in bl:
            cx, cy = _cell(b["c"])
            if any((cx + dx, cy + dy) in stat for dx in (-1, 0, 1) for dy in (-1, 0, 1)):
                continue
            keep.append(b)
        out.append(keep)
    return out


def _polar(c, center) -> tuple:
    dx, dy = c[0] - center[0], c[1] - center[1]
    return math.degrees(math.atan2(dx, -dy)) % 360.0, math.hypot(dx, dy)


def _adiff(a: float, b: float) -> float:
    return (a - b + 180.0) % 360.0 - 180.0


def pick_pair(blobs: list, center: Sequence[float], R: float, angle: Optional[float] = None):
    """Пара пятен-кончиков на кадре: (blob, blob) или None. angle — найденный угол палки (клин ±15° вокруг него и напротив), None — любая
    пара, разнесённая на ≥150° по углу, в кольце 0,35–1,4 R."""
    cand = []
    for b in blobs:
        a, r = _polar(b["c"], center)
        if RING[0] * R <= r <= RING[1] * R:
            cand.append((a, b))
    if angle is not None:
        s0 = [b for a, b in cand if abs(_adiff(a, angle)) <= WEDGE_DEG]
        s1 = [b for a, b in cand if abs(_adiff(a, angle + 180.0)) <= WEDGE_DEG]
        if not s0 or not s1:
            return None
        return max(s0, key=lambda b: b["area"]), max(s1, key=lambda b: b["area"])
    best = None
    for i in range(len(cand)):
        for j in range(i + 1, len(cand)):
            d = abs(_adiff(cand[i][0], cand[j][0]))
            if d >= PAIR_MIN_DEG and (best is None or d > best[0]):
                best = (d, cand[i][1], cand[j][1])
    return None if best is None else (best[1], best[2])


def solve_axis(lines: Sequence[tuple]):
    """Точка, ближайшая ко всем прямым (p, q): (центр[2], расстояния до прямых, кто в ответе, углы прямых 0..180). Пять проходов — далёкие
    прямые (выше 75-го перцентиля расстояний, но не ближе 3 px) в следующий проход не идут."""
    P = np.array([l[0] for l in lines], float)
    D = np.array([l[1] for l in lines], float) - P
    nrm = np.hypot(D[:, 0], D[:, 1])
    ok = nrm > 1e-6
    P, D, nrm = P[ok], D[ok], nrm[ok]
    D = D / nrm[:, None]
    use = np.ones(len(P), bool)
    c = P.mean(axis=0)
    dist = np.zeros(len(P))
    for _ in range(5):
        dx, dy = D[use, 0], D[use, 1]
        a11, a22, a12 = (1 - dx * dx).sum(), (1 - dy * dy).sum(), -(dx * dy).sum()
        px, py = P[use, 0], P[use, 1]
        b1 = ((1 - dx * dx) * px - dx * dy * py).sum()
        b2 = (-dx * dy * px + (1 - dy * dy) * py).sum()
        det = a11 * a22 - a12 * a12
        if abs(det) < 1e-9:
            break
        c = np.array([(a22 * b1 - a12 * b2) / det, (a11 * b2 - a12 * b1) / det])
        v = c[None, :] - P
        dist = np.abs(v[:, 0] * D[:, 1] - v[:, 1] * D[:, 0])
        use = dist < max(3.0, float(np.percentile(dist, 75)))
    angs = np.degrees(np.arctan2(D[:, 0], -D[:, 1])) % 180.0
    return c, dist, use, angs


def axis_from_frames(frames: list, center0: Sequence[float], R: float, gated: bool, passes: int = 2) -> dict:
    """Ось по кончикам. frames — [{"blobs": …, "angle": угол палки или None}] (пятна уже без неподвижных). Два прохода: пары выбираются
    относительно текущего центра, второй — относительно найденного. Ответ: центр, число пар/в ответе, разброс, охват направлений, shift, reliable
    (хватает пар и направлений, разброс ≤ 1,5 % радиуса, сдвиг не больше 0,35 R) и пары кадров pairs — для размеров."""
    center = (float(center0[0]), float(center0[1]))
    res = {"reliable": False, "pairs": 0, "kept": 0, "center": [round(center[0], 1), round(center[1], 1)], "dist_med": None, "dist_p90": None,
           "sectors": 0, "shift_px": 0.0, "used": [], "gated": bool(gated)}
    for _ in range(passes):
        pairs = []
        for k, fr in enumerate(frames):
            pr = pick_pair(fr["blobs"], center, R, fr["angle"] if gated else None)
            if pr is not None:
                pairs.append((k, pr))
        if len(pairs) < 5:
            res["pairs"] = len(pairs)
            return res
        lines = [(pr[0]["c"], pr[1]["c"]) for _, pr in pairs]
        c, dist, use, angs = solve_axis(lines)
        center = (float(c[0]), float(c[1]))
        res.update(pairs=len(pairs), kept=int(use.sum()), dist_med=round(float(np.median(dist)), 2), dist_p90=round(float(np.percentile(dist, 90)), 2),
                   sectors=len({int(a // 30) for a in angs}), center=[round(center[0], 1), round(center[1], 1)],
                   shift_px=round(float(math.hypot(center[0] - center0[0], center[1] - center0[1])), 1))
        res["_pairs"] = [(k, pr) for (k, pr), u in zip(pairs, use) if u]
    res["reliable"] = bool(res["kept"] >= MIN_PAIRS and res["sectors"] >= MIN_SECTORS and res["dist_med"] <= max(4.0, DIST_FRAC * R)
                           and res["shift_px"] <= SHIFT_MAX * R)
    return res


def quality(ax: dict) -> float:
    """Чем больше прямых в ответе и чем меньше их разброс, тем выше: kept / (1 + разброс/2). Сравнивает наборы ящиков между собой."""
    return ax["kept"] / (1.0 + float(ax["dist_med"] if ax["dist_med"] is not None else 99.0) / 2.0) if ax["kept"] else 0.0


def has_white(boxes: Sequence[Sequence[int]]) -> bool:
    return any(b[0] == 0 and b[1] == 179 and b[3] <= 110 and b[4] >= 200 for b in boxes)


def axis_checked(frames: list, center0: Sequence[float], R: float) -> dict:
    """Ось без клина (угла палки нет или трек «до» врёт): пары — любые пятна, разнесённые на ≥150° ОТНОСИТЕЛЬНО центра. Такой выбор сам тянет прямые
    к начальной точке (они проходят рядом с ней по построению), поэтому решение проверяем: те же кадры с четырёх разных начальных точек (±0,2 R)
    должны прийти в одну и ту же точку. Настоящие кончики дают её с разбросом в пару px, ложные пятна — следуют за стартом."""
    c0 = (float(center0[0]), float(center0[1]))
    starts = [c0, (c0[0] + 0.2 * R, c0[1]), (c0[0] - 0.1 * R, c0[1] + 0.17 * R), (c0[0] - 0.1 * R, c0[1] - 0.17 * R)]
    rs = [axis_from_frames(frames, s, R, False, passes=3) for s in starts]
    main = rs[0]
    if any(r["pairs"] < 5 or r["dist_med"] is None for r in rs):
        main["reliable"] = False
        return main
    cs = np.array([r["center"] for r in rs], float)
    spread = max(float(np.hypot(*(cs[i] - cs[j]))) for i in range(len(cs)) for j in range(i + 1, len(cs)))
    main["spread_px"] = round(spread, 1)
    if spread > max(6.0, 0.015 * R):
        main["reliable"] = False                                       # зависит от начальной точки — не ось
        return main
    m = cs.mean(axis=0)
    main["center"] = [round(float(m[0]), 1), round(float(m[1]), 1)]
    main["shift_px"] = round(float(math.hypot(m[0] - c0[0], m[1] - c0[1])), 1)
    main["reliable"] = bool(main["reliable"] and main["shift_px"] <= SHIFT_MAX * R)
    return main


def axis_best(frames: list, center0: Sequence[float], R: float, gated: bool) -> dict:
    """Ось по кончикам. С клином вокруг найденного угла палки (gated) — прямо; без клина — с проверкой на независимость от начальной точки.
    Не надёжный клин в «без клина» НЕ переводим: самоподтверждающийся ответ хуже честного «не нашли»."""
    return axis_from_frames(frames, center0, R, True) if gated else axis_checked(frames, center0, R)


def tip_size(axis: dict, center: Sequence[float]) -> Optional[dict]:
    """Размеры кончика по пятнам пар в ответе, относительно оси center: длина вдоль палки, ширина поперёк, радиальные начало и конец (px)."""
    ln, wd, r0, r1 = [], [], [], []
    for _, pr in axis.get("_pairs") or []:
        for b in pr:
            v = b["pts"] - np.array(center, np.float32)
            u = np.array(b["c"], float) - np.array(center, float)
            nu = math.hypot(u[0], u[1])
            if nu < 1e-6:
                continue
            u = u / nu
            along = v @ u.astype(np.float32)
            across = v @ np.array([-u[1], u[0]], np.float32)
            ln.append(float(along.max() - along.min()) + 1.0)
            wd.append(float(across.max() - across.min()) + 1.0)
            r0.append(float(along.min()))
            r1.append(float(along.max()))
    if len(ln) < 10:
        return None
    return {"len": float(np.median(ln)), "width_p90": float(np.percentile(wd, 90)), "start_p10": float(np.percentile(r0, 10)),
            "end_p95": float(np.percentile(r1, 95))}


def strip_pixels(hsv: np.ndarray, pair, grow: int = 3) -> np.ndarray:
    """HSV-пиксели вокруг пятен пары (пятно, расширенное на grow px): сюда попадает и белая часть полоски рядом с жёлтой. N×3 uint8."""
    out = []
    for b in pair:
        pts = b["pts"].astype(int)
        x0, y0 = max(0, pts[:, 0].min() - grow - 1), max(0, pts[:, 1].min() - grow - 1)
        x1, y1 = min(hsv.shape[1], pts[:, 0].max() + grow + 2), min(hsv.shape[0], pts[:, 1].max() + grow + 2)
        loc = np.zeros((y1 - y0, x1 - x0), np.uint8)
        loc[pts[:, 1] - y0, pts[:, 0] - x0] = 1
        loc = cv2.dilate(loc, np.ones((2 * grow + 1, 2 * grow + 1), np.uint8))
        out.append(hsv[y0:y1, x0:x1][loc > 0])
    return np.concatenate(out) if out else np.zeros((0, 3), np.uint8)


def fit_boxes(px: np.ndarray) -> list:
    """Ящики по пикселям полоски: «белый» (S мала, V высока — пересвет) и «цветной» (мода оттенка ±15). Пусто — подобрать нечего."""
    p = px[px[:, 2] >= FIT_MIN_V] if len(px) else px
    if len(p) < 40:
        return []
    boxes = []
    white = (p[:, 1] <= 70)
    if white.sum() >= max(20, 0.12 * len(p)):
        w = p[white]
        boxes.append([0, 179, 0, int(min(110, np.percentile(w[:, 1], 98) + 12)), int(max(205, np.percentile(w[:, 2], 2) - 8)), 255])
    col = p[p[:, 1] > 70]
    if len(col) >= max(20, 0.12 * len(p)):
        hist = np.bincount(col[:, 0].astype(int), minlength=180).astype(float)
        sm = np.convolve(np.r_[hist[-3:], hist, hist[:3]], np.ones(5) / 5.0, mode="same")[3:-3]
        mode = int(np.argmax(sm))
        d = ((col[:, 0].astype(int) - mode + 90) % 180) - 90
        c = col[np.abs(d) <= 15]
        if len(c) >= 20:
            dd = ((c[:, 0].astype(int) - mode + 90) % 180) - 90
            h1, h2 = (mode + int(np.percentile(dd, 2)) - 2) % 180, (mode + int(np.percentile(dd, 98)) + 2) % 180
            boxes.append([h1, h2, int(max(30, np.percentile(c[:, 1], 2) - 12)), 255, int(max(120, np.percentile(c[:, 2], 2) - 12)), 255])
    return boxes[:MAX_BOXES]


def radius_floor(size: dict) -> float:
    """Наименьший радиус зоны, при котором кольцо кончиков ещё достаёт до их внешнего края (tip_r1 ≤ 1,5): иначе подгонка радиуса «съежит» зону."""
    return float(size["end_p95"]) / (1.5 - RING_MARGIN)


def to_params(size: dict, R: float, thin: int, k: float) -> dict:
    """Размер кончика в пикселях → ключи зоны. R — радиус зоны (px кадра записи), thin — «тонкое» палки (рабочий масштаб), k — сжатие рабочей области."""
    tip_len = min(0.5, max(0.01, SIZE_LEN_K * size["len"] / R))
    r0 = min(1.0, max(0.4, size["start_p10"] / R - RING_MARGIN))           # ниже 0,4 R — ступица и блики у оси, не кончик
    r1 = min(1.5, max(1.0, size["end_p95"] / R + RING_MARGIN))
    need = int(math.ceil(size["width_p90"] * k)) + 3                        # полоска в рабочем масштабе + запас; уже thin — оставляем «как у палки» (0)
    return {"tip_len": round(tip_len, 3), "tip_r0": round(r0, 3), "tip_r1": round(r1, 3), "tip_thin": int(min(51, need)) if need > thin else 0}
