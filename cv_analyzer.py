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

# дефолты-заглушки (перекрываются блоком cv из plate_config); размеры — по эквив. диаметру, мкм
DEFAULTS = {
    "um_per_px": 1.629,
    "groups": {"small_max_um": 300.0, "medium_max_um": 600.0},
    "shape": {"min_circularity": 0.55, "max_aspect": 2.8, "min_solidity": 0.82},
    "min_size_um": 20.0,   # мельче — считаем пылью/шумом, не кристаллом
    "blur_min": 8.0,       # variance of Laplacian ниже — кадр смазан (тюним под камеру на Server)
}

# цвета групп (BGR) для overlay — согласованы с палитрой UI (teal/blue/amber/red)
GROUP_COLORS = {
    "small":  (165, 202, 93),    # teal
    "medium": (221, 138, 55),    # blue
    "large":  (23, 117, 186),    # amber
    "reject": (74, 75, 226),     # red
}
GROUP_ORDER = ["small", "medium", "large", "reject"]


def _cfg(cv_cfg: Optional[dict]) -> dict:
    c = {**DEFAULTS}
    if cv_cfg:
        c["um_per_px"] = float(cv_cfg.get("um_per_px", c["um_per_px"]))
        c["min_size_um"] = float(cv_cfg.get("min_size_um", c["min_size_um"]))
        c["blur_min"] = float(cv_cfg.get("blur_min", c["blur_min"]))
        if cv_cfg.get("groups"):
            c["groups"] = {**c["groups"], **cv_cfg["groups"]}
        if cv_cfg.get("shape"):
            c["shape"] = {**c["shape"], **cv_cfg["shape"]}
    return c


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


def _classify(size_um: float, circularity: float, aspect: float, solidity: float,
              cfg: dict) -> str:
    """Группа по размеру; брак — по форме (нестандартная = не похоже на правильный кристалл)."""
    sh = cfg["shape"]
    if (circularity < sh["min_circularity"] or aspect > sh["max_aspect"]
            or solidity < sh["min_solidity"]):
        return "reject"
    g = cfg["groups"]
    if size_um < g["small_max_um"]:
        return "small"
    if size_um < g["medium_max_um"]:
        return "medium"
    return "large"


def measure_objects(objects: list[dict], cv_cfg: Optional[dict] = None) -> list[CrystalMeasure]:
    """Посчитать геометрию/форму/группу для каждого объекта. Мелочь < min_size_um отсекаем."""
    cfg = _cfg(cv_cfg)
    upp = cfg["um_per_px"]
    min_size = cfg["min_size_um"]
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
        circ = (4.0 * math.pi * area_px / (perim * perim)) if perim > 0 else 0.0
        circ = min(circ, 1.0)
        aspect = (max(rw, rh) / min(rw, rh)) if min(rw, rh) > 0 else 99.0
        hull = cv2.convexHull(cnt)
        hull_area = cv2.contourArea(hull)
        solidity = (area_px / hull_area) if hull_area > 0 else 0.0
        m = cv2.moments(cnt)
        cx = m["m10"] / m["m00"] if m["m00"] else float(cnt[:, 0, 0].mean())
        cy = m["m01"] / m["m00"] if m["m00"] else float(cnt[:, 0, 1].mean())
        group = _classify(size_um, circ, aspect, solidity, cfg)
        out.append(CrystalMeasure(
            cx=cx, cy=cy, size_um=size_um, length_um=length_um, width_um=width_um,
            circularity=circ, aspect=aspect, solidity=solidity, group=group,
            conf=float(obj.get("conf", 0.0)), contour=cnt.astype(np.int32),
            bbox=tuple(obj["bbox"]) if obj.get("bbox") else None,
        ))
    return out


def blur_score(image: np.ndarray) -> float:
    """Variance of Laplacian — чем ниже, тем более смазан кадр."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def summarize(measures: list[CrystalMeasure], image_shape: tuple,
              cv_cfg: Optional[dict] = None, blur: Optional[float] = None) -> dict:
    """Сводка кадра: счётчики групп, %, средний/медианный размер, плотность, доля брака."""
    cfg = _cfg(cv_cfg)
    h, w = image_shape[:2]
    n = len(measures)
    counts = {g: 0 for g in GROUP_ORDER}
    sizes = []
    for m in measures:
        counts[m.group] += 1
        if m.group != "reject":
            sizes.append(m.size_um)
    area_mm2 = (w * cfg["um_per_px"] / 1000.0) * (h * cfg["um_per_px"] / 1000.0)
    sizes_np = np.array(sizes) if sizes else np.array([0.0])
    quality = "ok"
    if blur is not None and blur < cfg["blur_min"]:
        quality = "low"
    return {
        "count": n,
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
        color = GROUP_COLORS.get(m.group, (200, 200, 200))
        if m.contour is not None:
            cv2.drawContours(out, [m.contour], -1, color, 2)
        if draw_size and m.group != "reject":
            cv2.putText(out, f"{int(round(m.size_um))}",
                        (int(m.cx) - 12, int(m.cy) - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
    return out


def analyze(image: np.ndarray, objects: list[dict], cv_cfg: Optional[dict] = None,
            with_overlay: bool = True) -> dict:
    """Полный разбор одного кадра: измерения → сводка → (опц.) overlay-картинка (BGR)."""
    measures = measure_objects(objects, cv_cfg)
    blur = blur_score(image)
    summary = summarize(measures, image.shape, cv_cfg, blur=blur)
    def _obj(m):
        # bbox для наведения (hit-test в UI); площадь — по эквив.диаметру (= площадь маски), мкм²
        if m.contour is not None:
            x, y, w, h = cv2.boundingRect(m.contour)
        else:
            x = y = w = h = 0
        area_um2 = round(math.pi * (m.size_um / 2.0) ** 2, 0)
        return {
            "cx": round(m.cx, 1), "cy": round(m.cy, 1),
            "bbox": [int(x), int(y), int(w), int(h)],
            "size_um": round(m.size_um, 1), "area_um2": area_um2,
            "length_um": round(m.length_um, 1), "width_um": round(m.width_um, 1),
            "circularity": round(m.circularity, 3), "aspect": round(m.aspect, 2),
            "solidity": round(m.solidity, 3), "group": m.group, "conf": round(m.conf, 3),
        }
    result = {"summary": summary, "objects": [_obj(m) for m in measures]}
    if with_overlay:
        result["_overlay"] = draw_overlay(image, measures)   # numpy BGR, кодирует вызывающий
    return result
