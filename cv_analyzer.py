"""cv_analyzer — измерения и рассев кристаллов по детекциям (чистый OpenCV/numpy, без GPU).

Вход: кадр (BGR) + объекты от сайдкара (список dict: bbox, conf, polygon?) + конфиг блока
`cv`. Выход: по-объектная геометрия (размер в мкм, метрики формы, группа) + сводка кадра
(счётчики групп, проценты, средний/медианный размер, плотность, доля брака) + overlay-кадр
с подписями. Здесь НЕТ модели и GPU — только геометрия поверх готовых масок/боксов.

Масштаб: 613.8 px/mm → 1 px = 1.629 мкм (правится в конфиге `um_per_px`).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np

# дефолты-заглушки (перекрываются блоком cv из plate_config); размеры — по эквив. диаметру, мкм.
# Стартовые пороги — из «Памятки оператора» (Сапронов): норма 0,5–0,9 мм, игла > 3,0, сросток/кривой
# при выпуклости < 0,90; вытянутые 1,6–3,0 — не брак, а предупреждение.
DEFAULTS = {
    "um_per_px": 1.629,
    "groups": {"small_max_um": 500.0, "medium_max_um": 900.0},
    "shape": {"min_circularity": 0.55, "max_aspect": 3.0, "min_solidity": 0.90,
              "suspect_aspect": 1.6,     # вытянутые: от этого L/W до max_aspect (не брак)
              "notch_frac": 0.06},       # «выемка» контура глубже этой доли диаметра → признак сростка
    "size_reject": {"min_um": 250.0, "max_um": 1200.0},   # брак по размеру (только у готового, см. reject_from_sv)
    "reject_from_sv": 88.0,    # брак идёт в рассев/тренд, только когда СВ ≥ этого (кристаллы подросли)
    "reject_always": False,    # True — считать брак всегда, без порога по СВ
    "cluster_gap_px": 4.0,     # маски с зазором ≤ этого (px) склеиваются в сросток; 0 — выкл
    "nest_frac": 0.8,          # маска, лежащая внутри другой на ≥ этой доли, — лишняя, убирается; 0 — выкл
    "edge_margin_px": 12.0,    # объект ближе этого (px) к краю кадра считается обрезанным; 0 — выкл
    "seam_merge": True,        # склеивать кристалл, разрезанный швом нарезки (ровный край на линии стыка частей)
    "tiles": 6, "overlap": 0.15,   # как в настройках CV (их же получает сайдкар) — нужны, чтобы знать, где швы
    "min_size_um": 20.0,   # мельче — считаем пылью/шумом, не кристаллом
    "blur_min": 8.0,       # variance of Laplacian ниже — кадр смазан (тюним под камеру на Server)
}

# цвета групп (BGR) для overlay — согласованы с палитрой UI (teal/blue/amber/red)
GROUP_COLORS = {
    "small":  (165, 202, 93),    # teal
    "medium": (221, 138, 55),    # blue
    "large":  (23, 117, 186),    # amber
    "reject": (74, 75, 226),     # red
    "suspect": (245, 209, 106),  # голубой — вытянутые 1,6–3,0 (не брак)
    "cut":    (150, 150, 150),   # grey — обрезан краем кадра/швом, в статистику не идёт
}
GROUP_ORDER = ["small", "medium", "large", "reject"]
# причины брака (по «Памятке»): игла — раффиноза; сросток — высокое пересыщение; кривой — несахара;
# tiny/huge — по размеру (только у готового сахара)
REASONS = ["needle", "aggregate", "crooked", "tiny", "huge"]
# «cut» — служебная группа вне рассева: кристалл обрезан краем кадра (edge) или швом нарезки
# (cut, см. cv_service/sahi_tiler.py). Размер/форма такого обрубка недостоверны.
CUT_GROUP = "cut"
NOTCH_MAX = 5          # больше выемок — рваный край (кривой), а не сросток
POLY_EPS_PX = 1.5      # упрощение контура для сохранения/отрисовки в браузере, px


def _cfg(cv_cfg: Optional[dict]) -> dict:
    c = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    if cv_cfg:
        c["um_per_px"] = float(cv_cfg.get("um_per_px", c["um_per_px"]))
        c["min_size_um"] = float(cv_cfg.get("min_size_um", c["min_size_um"]))
        c["blur_min"] = float(cv_cfg.get("blur_min", c["blur_min"]))
        c["reject_from_sv"] = float(cv_cfg.get("reject_from_sv", c["reject_from_sv"]))
        c["reject_always"] = bool(cv_cfg.get("reject_always", c["reject_always"]))
        for k in ("cluster_gap_px", "nest_frac", "edge_margin_px", "tiles", "overlap"):
            c[k] = float(cv_cfg.get(k, c[k]))
        c["seam_merge"] = bool(cv_cfg.get("seam_merge", c["seam_merge"]))
        for key in ("groups", "shape", "size_reject"):
            if cv_cfg.get(key):
                c[key] = {**c[key], **cv_cfg[key]}
    return c


def is_counting(cfg: dict, sv: Optional[float]) -> bool:
    """Идёт ли брак в рассев/тренд: СВ дошло до порога, либо порог отключён галочкой, либо СВ
    неизвестно (без датчика СВ фильтровать нечем — показываем как есть)."""
    return bool(cfg["reject_always"] or sv is None or sv >= cfg["reject_from_sv"])


@dataclass
class CrystalMeasure:
    cx: float
    cy: float
    size_um: float            # эквивалентный диаметр (по площади), мкм
    length_um: float          # длинная сторона minAreaRect, мкм
    width_um: float           # короткая сторона minAreaRect, мкм
    circularity: float
    aspect: float
    solidity: float
    group: str
    conf: float
    contour: Optional[np.ndarray] = None
    bbox: Optional[tuple] = None
    outline: Optional[np.ndarray] = None   # контур для отрисовки (чистый и скруглённый); метрики — не по нему
    defect: Optional[str] = None    # причина брака (REASONS) — определяется всегда, даже если брак пока не считается
    suspect: bool = False           # вытянутый 1,6–3,0: не брак, предупреждение
    notches: int = 0                # глубоких выемок контура (признак сростка)
    members: int = 1                # из скольких масок склеен объект (>1 — сросток по близости масок)
    seam_merged: int = 0            # >0 — один кристалл, склеенный из стольких частей по шву нарезки


def _contour_from_obj(obj: dict) -> Optional[np.ndarray]:
    poly = obj.get("polygon")
    if poly and len(poly) >= 3:
        return np.array(poly, dtype=np.float32).reshape(-1, 1, 2)
    bbox = obj.get("bbox")
    if bbox:
        x1, y1, x2, y2 = bbox
        return np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]],
                        dtype=np.float32).reshape(-1, 1, 2)
    return None


def _size_group(size_um: float, cfg: dict) -> str:
    g = cfg["groups"]
    if size_um < g["small_max_um"]:
        return "small"
    if size_um < g["medium_max_um"]:
        return "medium"
    return "large"


def _clean_contour(cnt: np.ndarray, eq_d_px: float) -> np.ndarray:
    """Контур маски без тонких хвостов и шипов: маска морфологически открывается (ядро ≈ 5 % диаметра).
    Модель часто тянет за кристаллом волосок — он занижает выпуклость и даёт ложные «перетяжки»;
    форму кристалла (округлость, выпуклость, выемки) оцениваем по очищенному контуру.
    Не получилось (маска слишком мала/развалилась) — возвращаем исходный."""
    try:
        pts = cnt.reshape(-1, 2).astype(np.float32)
        x0, y0 = np.floor(pts.min(axis=0)).astype(int) - 3
        w, h = (np.ceil(pts.max(axis=0)).astype(int) + 3 - (x0, y0))
        if w < 8 or h < 8 or w * h > 4_000_000:
            return cnt
        m = np.zeros((h, w), np.uint8)
        cv2.fillPoly(m, [np.round(pts - (x0, y0)).astype(np.int32).reshape(-1, 1, 2)], 1)
        k = max(3, int(round(0.05 * eq_d_px)) | 1)
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
        cs = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)[0]
        if not cs:
            return cnt
        c = max(cs, key=cv2.contourArea)
        if cv2.contourArea(c) < 0.5 * cv2.contourArea(cnt):
            return cnt
        return (c.reshape(-1, 2).astype(np.float32) + np.array([x0, y0], np.float32)).reshape(-1, 1, 2)
    except cv2.error:
        return cnt


def _outline(cnt: np.ndarray, eq_d_px: float) -> np.ndarray:
    """Контур ДЛЯ ОТРИСОВКИ: чистый (без самопересечений, волосков и шипов) и слегка скруглённый.
    Маска → открытие (убрать хвосты) → небольшое размытие и порог (убрать «лесенку» пикселей) →
    внешний контур → упрощение. На метрики формы не влияет (они считаются по _clean_contour)."""
    try:
        pts = cnt.reshape(-1, 2).astype(np.float32)
        x0, y0 = np.floor(pts.min(axis=0)).astype(int) - 4
        w, h = (np.ceil(pts.max(axis=0)).astype(int) + 4 - (x0, y0))
        if w < 8 or h < 8 or w * h > 4_000_000:
            return cnt
        m = np.zeros((h, w), np.uint8)
        cv2.fillPoly(m, [np.round(pts - (x0, y0)).astype(np.int32).reshape(-1, 1, 2)], 255)
        k = max(3, int(round(0.04 * eq_d_px)) | 1)
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
        kb = max(3, int(round(0.03 * eq_d_px)) | 1)
        m = cv2.threshold(cv2.GaussianBlur(m, (kb, kb), 0), 127, 255, cv2.THRESH_BINARY)[1]
        cs = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)[0]
        if not cs:
            return cnt
        c = max(cs, key=cv2.contourArea)
        if cv2.contourArea(c) < 0.5 * cv2.contourArea(cnt):
            return cnt
        c = cv2.approxPolyDP(c, max(1.0, 0.004 * cv2.arcLength(c, True)), True)
        return (c.reshape(-1, 2).astype(np.float32) + np.array([x0, y0], np.float32)).reshape(-1, 1, 2)
    except cv2.error:
        return cnt


def _notches(clean: np.ndarray, eq_d_px: float, frac: float) -> int:
    """Число глубоких выемок ОЧИЩЕННОГО контура (глубже frac·диаметра) — признак срастания тел."""
    try:
        ap = np.round(cv2.approxPolyDP(clean.astype(np.float32), max(1.0, 0.01 * cv2.arcLength(clean, True)), True)).astype(np.int32)
        if len(ap) < 4:
            return 0
        hull = cv2.convexHull(ap, returnPoints=False)
        if hull is None or len(hull) < 4:
            return 0
        d = cv2.convexityDefects(ap, hull)
        if d is None:
            return 0
        depth = d.reshape(-1, 4)[:, 3] / 256.0      # форма массива зависит от версии OpenCV
        return int((depth > frac * eq_d_px).sum())
    except cv2.error:
        return 0       # самопересекающийся контур — считаем «без выемок»


def _defect(size_um: float, circularity: float, aspect: float, solidity: float,
            notches: int, cfg: dict, size_active: bool) -> Optional[str]:
    """Причина брака по форме/размеру или None. Порядок = приоритет причин."""
    sh = cfg["shape"]
    if aspect > sh["max_aspect"]:
        return "needle"                                   # игла (раффиноза)
    # сросток: 2–5 глубоких перетяжек контура (либо 1 перетяжка при невыпуклом силуэте);
    # выемок МНОГО (>5) — это не срастание тел, а рваный/неровный край → «кривой»
    if 1 <= notches <= NOTCH_MAX and (notches >= 2 or solidity < sh["min_solidity"]):
        return "aggregate"
    if notches > NOTCH_MAX or solidity < sh["min_solidity"] or circularity < sh["min_circularity"]:
        return "crooked"                                  # кривой / неровный контур
    if size_active:
        sr = cfg["size_reject"]
        if size_um < sr["min_um"]:
            return "tiny"
        if size_um > sr["max_um"]:
            return "huge"
    return None


def _grid(cols_rows: int) -> tuple[int, int]:
    """Число частей нарезки -> (столбцы, строки): как в cv_service/sahi_tiler.py (должно совпадать)."""
    presets = {1: (1, 1), 2: (2, 1), 4: (2, 2), 6: (3, 2), 9: (3, 3)}
    if cols_rows in presets:
        return presets[cols_rows]
    cols = int(math.ceil(math.sqrt(cols_rows)))
    return cols, int(math.ceil(cols_rows / cols))


def _seam_lines(shape: tuple, tiles: int, overlap: float) -> tuple[list, list]:
    """Внутренние границы частей нарезки (x-ы вертикальных и y-и горизонтальных стыков) — по тем же
    формулам, что в сайдкаре (tiles и overlap берутся из настроек CV, их же получает сайдкар)."""
    h, w = shape[:2]
    if tiles <= 1:
        return [], []
    cols, rows = _grid(tiles)
    tw, th = w / cols, h / rows
    ox, oy = tw * overlap, th * overlap
    xs, ys = set(), set()
    for c in range(cols):
        x1, x2 = max(0, int(c * tw - ox)), min(w, int((c + 1) * tw + ox))
        if x1 > 0:
            xs.add(x1)
        if x2 < w:
            xs.add(x2)
    for r in range(rows):
        y1, y2 = max(0, int(r * th - oy)), min(h, int((r + 1) * th + oy))
        if y1 > 0:
            ys.add(y1)
        if y2 < h:
            ys.add(y2)
    return sorted(xs), sorted(ys)


def _on_seam(poly: np.ndarray, xs: list, ys: list, tol: float = 2.5, min_len: float = 16.0) -> bool:
    """У маски есть ровный край ровно на линии стыка частей кадра (≥2 вершины в пределах tol px от линии,
    растянутые вдоль неё на ≥ min_len px) — значит кристалл разрезан швом нарезки."""
    for axis, lines in ((0, xs), (1, ys)):
        for L in lines:
            on = poly[np.abs(poly[:, axis] - L) <= tol]
            if len(on) >= 2 and float(on[:, 1 - axis].max() - on[:, 1 - axis].min()) >= min_len:
                return True
    return False


def _merge_seams(objects: list[dict], shape: tuple, tiles: int, overlap: float, gap: float) -> list[dict]:
    """Склеиваем кристаллы, разрезанные швом нарезки (старый сайдкар их не метит): маска с ровным краем на
    линии стыка + соприкасающаяся с ней маска = ОДИН кристалл (не сросток). Обрубок без пары помечаем cut."""
    xs, ys = _seam_lines(shape, tiles, overlap)
    if not xs and not ys:
        return objects
    idx = [i for i, o in enumerate(objects)
           if o.get("polygon") and len(o["polygon"]) >= 3 and not (o.get("cut") or o.get("edge"))]
    P = {i: np.asarray(objects[i]["polygon"], np.float32) for i in idx}
    piece = {i for i in idx if _on_seam(P[i], xs, ys)}
    if not piece:
        return objects
    parent = {i: i for i in idx}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    bb = {i: (P[i][:, 0].min(), P[i][:, 1].min(), P[i][:, 0].max(), P[i][:, 1].max()) for i in idx}
    pad = max(gap, 0) + 1
    paired = set()
    for i in piece:
        for j in idx:
            if j == i or (j in piece and j < i):
                continue
            if bb[i][2] + pad < bb[j][0] or bb[j][2] + pad < bb[i][0] or bb[i][3] + pad < bb[j][1] or bb[j][3] + pad < bb[i][1]:
                continue
            if _pair_relation(P[i], P[j], gap)[2]:
                parent[find(i)] = find(j)
                paired.update((i, j))
    groups: dict[int, list[int]] = {}
    for i in idx:
        groups.setdefault(find(i), []).append(i)
    out, done = [], set()
    for i, o in enumerate(objects):
        if i not in P:
            out.append(o)
            continue
        g = groups[find(i)]
        if len(g) == 1:
            out.append({**o, "cut": True} if i in piece else o)       # обрубок шва без пары — обрезанный
            continue
        if not any(k in piece for k in g):
            out.append(o)
            continue
        if find(i) in done:
            continue
        done.add(find(i))
        poly = _union_polygon([P[k] for k in g], gap)
        if poly is None:
            out.extend(objects[k] for k in g)
            continue
        xs_, ys_ = [p[0] for p in poly], [p[1] for p in poly]
        out.append({"bbox": [min(xs_), min(ys_), max(xs_), max(ys_)],
                    "conf": float(max(objects[k].get("conf", 0.0) for k in g)),
                    "polygon": poly, "seam_merged": len(g)})
    return out


def _flag_edges(objects: list[dict], shape: tuple, margin: float) -> list[dict]:
    """Помечаем edge у объектов, чей бокс ближе margin px к краю кадра: кристалл обрезан кадром (или маска
    модели не доходит до края на пару пикселей), его размер и форма недостоверны."""
    h, w = shape[:2]
    out = []
    for o in objects:
        b = o.get("bbox")
        if b and not (o.get("cut") or o.get("edge")) and (
                b[0] <= margin or b[1] <= margin or b[2] >= w - margin or b[3] >= h - margin):
            o = {**o, "edge": True}
        out.append(o)
    return out


def _pair_relation(pa: np.ndarray, pb: np.ndarray, gap: float) -> tuple[float, float, bool]:
    """Отношение двух масок: (доля A внутри B, доля B внутри A, касаются ли с зазором ≤ gap px)."""
    x0 = min(pa[:, 0].min(), pb[:, 0].min()) - gap - 2
    y0 = min(pa[:, 1].min(), pb[:, 1].min()) - gap - 2
    x1 = max(pa[:, 0].max(), pb[:, 0].max()) + gap + 2
    y1 = max(pa[:, 1].max(), pb[:, 1].max()) + gap + 2
    s = min(1.0, 200.0 / max(x1 - x0, y1 - y0, 1.0))
    shape = (int((y1 - y0) * s) + 2, int((x1 - x0) * s) + 2)
    off = np.array([x0, y0], np.float32)

    def ras(p):
        m = np.zeros(shape, np.uint8)
        cv2.fillPoly(m, [np.round((p - off) * s).astype(np.int32).reshape(-1, 1, 2)], 1)
        return m

    ma, mb = ras(pa), ras(pb)
    aa, ab = float(ma.sum()), float(mb.sum())
    if aa <= 0 or ab <= 0:
        return 0.0, 0.0, False
    inter = float((ma & mb).sum())
    touch = inter > 0
    if not touch and gap > 0:
        k = int(round(gap * s)) * 2 + 1
        if k > 1:
            touch = bool((cv2.dilate(ma, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))) & mb).any())
    return inter / aa, inter / ab, touch


def _union_polygon(polys: list[np.ndarray], gap: float) -> Optional[list]:
    """Общий контур группы масок (с закрытием зазоров ≤ gap px)."""
    allp = np.vstack(polys)
    x0, y0 = np.floor(allp.min(axis=0)).astype(int) - int(gap) - 3
    x1, y1 = np.ceil(allp.max(axis=0)).astype(int) + int(gap) + 3
    w, h = x1 - x0, y1 - y0
    if w < 8 or h < 8 or w * h > 6_000_000:
        return None
    m = np.zeros((h, w), np.uint8)
    for p in polys:
        cv2.fillPoly(m, [np.round(p - np.array([x0, y0], np.float32)).astype(np.int32).reshape(-1, 1, 2)], 255)
    k = int(gap) * 2 + 1
    if k > 1:
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    cs = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)[0]
    if not cs:
        return None
    c = cv2.approxPolyDP(max(cs, key=cv2.contourArea), 1.5, True)
    return (c.reshape(-1, 2).astype(np.float32) + np.array([x0, y0], np.float32)).tolist()


def _merge_touching(objects: list[dict], gap: float, nest: float) -> list[dict]:
    """Сборка масок по нашему расчёту (а не по решению модели):
      • маска, которая почти целиком (≥ nest) лежит внутри другой, — лишнее второе обнаружение на том же
        кристалле: убираем;
      • маски, которые соприкасаются или налезают друг на друга (зазор ≤ gap px), — это сросток нескольких
        кристаллов: склеиваем в ОДИН объект (общий контур, members = сколько кристаллов).
    Обрезанные (cut/edge) и без контура не трогаем."""
    idx = [i for i, o in enumerate(objects)
           if o.get("polygon") and len(o["polygon"]) >= 3 and not (o.get("cut") or o.get("edge"))]
    if len(idx) < 2 or (gap <= 0 and nest <= 0):
        return objects
    P = {i: np.asarray(objects[i]["polygon"], np.float32) for i in idx}
    bb = {i: (P[i][:, 0].min(), P[i][:, 1].min(), P[i][:, 0].max(), P[i][:, 1].max()) for i in idx}
    pad = max(gap, 0) + 1
    rel = {}
    for a_pos, i in enumerate(idx):
        for j in idx[a_pos + 1:]:
            if bb[i][2] + pad < bb[j][0] or bb[j][2] + pad < bb[i][0] or bb[i][3] + pad < bb[j][1] or bb[j][3] + pad < bb[i][1]:
                continue
            rel[(i, j)] = _pair_relation(P[i], P[j], gap)
    drop = set()
    if nest > 0:
        for (i, j), (fa, fb, _t) in rel.items():
            if fa >= nest and fa >= fb:
                drop.add(i)                               # i почти целиком внутри j
            elif fb >= nest:
                drop.add(j)
    parent = {i: i for i in idx}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    if gap > 0:
        for (i, j), (fa, fb, touch) in rel.items():
            if touch and i not in drop and j not in drop:
                parent[find(i)] = find(j)
    groups: dict[int, list[int]] = {}
    for i in idx:
        if i not in drop:
            groups.setdefault(find(i), []).append(i)
    out, done = [], set()
    for i, o in enumerate(objects):
        if i in drop:
            continue
        if i not in P:
            out.append(o)
            continue
        g = groups[find(i)]
        if len(g) == 1:
            out.append(o)
            continue
        if find(i) in done:
            continue
        done.add(find(i))
        poly = _union_polygon([P[k] for k in g], gap)
        if poly is None:
            out.extend(objects[k] for k in g)
            continue
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        out.append({"bbox": [min(xs), min(ys), max(xs), max(ys)],
                    "conf": float(np.mean([objects[k].get("conf", 0.0) for k in g])),
                    "polygon": poly, "members": len(g)})
    return out


def measure_objects(objects: list[dict], cv_cfg: Optional[dict] = None,
                    sv: Optional[float] = None, img_shape: Optional[tuple] = None) -> list[CrystalMeasure]:
    """Посчитать геометрию/форму/группу для каждого объекта. Мелочь < min_size_um отсекаем.

    sv — текущее СВ пробы: пока оно ниже reject_from_sv (и не стоит reject_always), кристаллы с
    дефектом формы НЕ идут в группу «брак» (считаются по размеру), но причина дефекта всё равно
    определяется и сохраняется — в окне CV всегда видно, что это.
    """
    cfg = _cfg(cv_cfg)
    upp = cfg["um_per_px"]
    min_size = cfg["min_size_um"]
    if img_shape is not None and cfg["edge_margin_px"] > 0:
        objects = _flag_edges(objects, img_shape, cfg["edge_margin_px"])
    if img_shape is not None and cfg["seam_merge"]:
        objects = _merge_seams(objects, img_shape, int(cfg["tiles"]), cfg["overlap"], max(cfg["cluster_gap_px"], 4.0))
    objects = _merge_touching(objects, cfg["cluster_gap_px"], cfg["nest_frac"])
    counting = is_counting(cfg, sv)
    size_active = sv is not None and sv >= cfg["reject_from_sv"]   # размер-брак — только у готового
    out: list[CrystalMeasure] = []
    for obj in objects:
        cnt = _contour_from_obj(obj)
        if cnt is None:
            continue
        area_px = cv2.contourArea(cnt)
        if area_px <= 1:
            continue
        perim = cv2.arcLength(cnt, True)
        eq_d_px = math.sqrt(4.0 * area_px / math.pi)
        size_um = eq_d_px * upp
        if size_um < min_size:
            continue
        (_, _), (rw, rh), _ = cv2.minAreaRect(cnt)
        length_um = max(rw, rh) * upp
        width_um = min(rw, rh) * upp
        aspect = (max(rw, rh) / min(rw, rh)) if min(rw, rh) > 0 else 99.0
        # форма (округлость, выпуклость, выемки) — по очищенному от шипов контуру; размер — по маске как есть
        clean = _clean_contour(cnt, eq_d_px) if obj.get("polygon") else cnt
        c_area, c_perim = cv2.contourArea(clean), cv2.arcLength(clean, True)
        circ = min((4.0 * math.pi * c_area / (c_perim * c_perim)) if c_perim > 0 else 0.0, 1.0)
        hull_area = cv2.contourArea(cv2.convexHull(clean))
        solidity = min((c_area / hull_area) if hull_area > 0 else 0.0, 1.0)
        m = cv2.moments(cnt)
        cx = m["m10"] / m["m00"] if m["m00"] else float(cnt[:, 0, 0].mean())
        cy = m["m01"] / m["m00"] if m["m00"] else float(cnt[:, 0, 1].mean())
        cut = bool(obj.get("cut") or obj.get("edge"))
        notches = 0 if (cut or not obj.get("polygon")) else _notches(clean, eq_d_px, cfg["shape"]["notch_frac"])
        members = int(obj.get("members", 1))
        defect = None if cut else _defect(size_um, circ, aspect, solidity, notches, cfg, size_active)
        if members > 1 and not cut:
            defect = "aggregate"          # склеено из нескольких масок — сросток по определению
        suspect = (not cut) and defect is None and aspect > cfg["shape"]["suspect_aspect"]
        if cut:
            group = CUT_GROUP
        elif defect and counting:
            group = "reject"
        else:
            group = _size_group(size_um, cfg)
        out.append(CrystalMeasure(
            cx=cx, cy=cy, size_um=size_um, length_um=length_um, width_um=width_um,
            circularity=circ, aspect=aspect, solidity=solidity, group=group,
            conf=float(obj.get("conf", 0.0)), contour=cnt.astype(np.int32),
            bbox=tuple(obj["bbox"]) if obj.get("bbox") else None,
            defect=defect, suspect=suspect, notches=notches, members=members,
            seam_merged=int(obj.get("seam_merged", 0)),
            outline=(_outline(cnt, eq_d_px) if obj.get("polygon") else cnt),
        ))
    return out


def blur_score(image: np.ndarray) -> float:
    """Variance of Laplacian — чем ниже, тем более смазан кадр."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def summarize(measures: list[CrystalMeasure], image_shape: tuple,
              cv_cfg: Optional[dict] = None, blur: Optional[float] = None,
              sv: Optional[float] = None) -> dict:
    """Сводка кадра: счётчики групп, %, средний/медианный размер, плотность, доля брака."""
    cfg = _cfg(cv_cfg)
    h, w = image_shape[:2]
    counts = {g: 0 for g in GROUP_ORDER}
    sizes = []
    n_cut = 0
    reasons = {r: 0 for r in REASONS}
    n_suspect = 0
    for m in measures:
        if m.group == CUT_GROUP:      # обрезан кадром/швом — вне рассева и размеров
            n_cut += 1
            continue
        counts[m.group] += 1
        if m.defect:
            reasons[m.defect] += 1    # причины считаем всегда (в рассев брак идёт по порогу СВ)
        if m.suspect:
            n_suspect += 1
        if m.group != "reject":
            sizes.append(m.size_um)
    n = len(measures) - n_cut
    area_mm2 = (w * cfg["um_per_px"] / 1000.0) * (h * cfg["um_per_px"] / 1000.0)
    sizes_np = np.array(sizes) if sizes else np.array([0.0])
    quality = "ok"
    if blur is not None and blur < cfg["blur_min"]:
        quality = "low"
    return {
        "count": n,
        "cut": n_cut,
        "reasons": reasons,
        "suspect": n_suspect,
        "reject_active": is_counting(cfg, sv),
        "sv": sv,
        "groups": counts,
        "groups_pct": {g: (round(100.0 * counts[g] / n, 1) if n else 0.0) for g in GROUP_ORDER},
        "size_um": {
            "mean": round(float(sizes_np.mean()), 1),
            "median": round(float(np.median(sizes_np)), 1),
            "p10": round(float(np.percentile(sizes_np, 10)), 1),
            "p90": round(float(np.percentile(sizes_np, 90)), 1),
            "cv_pct": round(float(100.0 * sizes_np.std() / sizes_np.mean()), 1) if sizes_np.mean() else 0.0,
        },
        "density_per_mm2": round(n / area_mm2, 2) if area_mm2 > 0 else 0.0,
        "reject_pct": round(100.0 * counts["reject"] / n, 1) if n else 0.0,
        "quality": quality,
        "blur": round(blur, 1) if blur is not None else None,
    }


def draw_overlay(image: np.ndarray, measures: list[CrystalMeasure],
                 draw_size: bool = True) -> np.ndarray:
    """Нарисовать контуры кристаллов цветом группы + подпись размера. Возвращает копию кадра."""
    out = image.copy()
    if out.ndim == 2:
        out = cv2.cvtColor(out, cv2.COLOR_GRAY2BGR)
    for m in measures:
        color = GROUP_COLORS.get("reject" if m.defect else ("suspect" if m.suspect else m.group), (200, 200, 200))
        if m.contour is not None:
            cv2.drawContours(out, [m.contour], -1, color, 2)
        if draw_size and m.group not in ("reject", CUT_GROUP):
            cv2.putText(out, f"{int(round(m.size_um))}",
                        (int(m.cx) - 12, int(m.cy) - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
    return out


def draw_objects(image: np.ndarray, objects: list[dict]) -> np.ndarray:
    """Контуры кристаллов по готовым объектам разбора (поля poly/group) — для миниатюры пробы.
    Сам кадр пробы хранится чистым, в окне CV контуры рисует браузер. Возвращает копию кадра."""
    out = image.copy()
    if out.ndim == 2:
        out = cv2.cvtColor(out, cv2.COLOR_GRAY2BGR)
    thick = max(2, int(round(max(out.shape[:2]) / 600)))     # чтобы контур не пропал при уменьшении
    for o in objects:
        poly = o.get("poly")
        if poly and len(poly) >= 3:
            pts = np.asarray(poly, dtype=np.int32).reshape(-1, 1, 2)
            key = "reject" if o.get("defect") else ("suspect" if o.get("suspect") else o.get("group"))
            cv2.polylines(out, [pts], True, GROUP_COLORS.get(key, (200, 200, 200)), thick)
    return out


def analyze(image: np.ndarray, objects: list[dict], cv_cfg: Optional[dict] = None,
            with_overlay: bool = True, sv: Optional[float] = None) -> dict:
    """Полный разбор одного кадра: измерения → сводка → (опц.) overlay-картинка (BGR).
    sv — СВ на момент кадра (порог, с которого брак идёт в рассев)."""
    measures = measure_objects(objects, cv_cfg, sv=sv, img_shape=image.shape)
    blur = blur_score(image)
    summary = summarize(measures, image.shape, cv_cfg, blur=blur, sv=sv)
    def _obj(m):
        # bbox для наведения (hit-test в UI); площадь — по эквив.диаметру (= площадь маски), мкм²
        if m.contour is not None:
            x, y, w, h = cv2.boundingRect(m.contour)
        else:
            x = y = w = h = 0
        area_um2 = round(math.pi * (m.size_um / 2.0) ** 2, 0)
        # контур для отрисовки в браузере (слои, подсветка формы под мышкой) — упрощённый
        poly = []
        src = m.outline if m.outline is not None else m.contour
        if src is not None:
            poly = np.round(cv2.approxPolyDP(src.astype(np.float32), POLY_EPS_PX, True)).astype(int).reshape(-1, 2).tolist()
        return {
            "poly": poly,
            "cx": round(m.cx, 1), "cy": round(m.cy, 1),
            "bbox": [int(x), int(y), int(w), int(h)],
            "size_um": round(m.size_um, 1), "area_um2": area_um2,
            "length_um": round(m.length_um, 1), "width_um": round(m.width_um, 1),
            "circularity": round(m.circularity, 3), "aspect": round(m.aspect, 2),
            "solidity": round(m.solidity, 3), "group": m.group, "conf": round(m.conf, 3),
            "defect": m.defect, "suspect": m.suspect, "notches": m.notches, "members": m.members, "seam_merged": m.seam_merged,
        }
    result = {"summary": summary, "objects": [_obj(m) for m in measures]}
    if with_overlay:
        result["_overlay"] = draw_overlay(image, measures)   # numpy BGR, кодирует вызывающий
    return result
