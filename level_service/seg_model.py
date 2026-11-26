"""seg_model — обёртка над YOLO (ultralytics): кадр → объекты по ВСЕМ классам модели с контурами в координатах кадра.

Сервис не знает, что такое «жидкость»: он отдаёт найденное как есть (класс, уверенность, рамка, контуры), а что из этого брать,
решает тот, кто спрашивает (motion_service берёт класс `liquid`). Поэтому сюда же можно загрузить любую другую модель
(другие классы, detect вместо seg, .onnx) — сервис менять не придётся.

Геометрия масок (`geometry`):
  lvldec — как в LVLDec.exe: маска сети (она в «письмовом» размере letterbox, с полями сверху/снизу) растягивается на весь кадр как есть.
           Края кадра при этом «съезжают» к центру (до ~30 px на 1080p). Калибровки завода (glass2_full и т.п.) подобраны именно под это,
           поэтому по умолчанию цифры совпадают с заводскими.
  exact  — поля letterbox вырезаются, маска ложится на кадр без растяжения (честная геометрия).

ultralytics импортируется лениво: без него сервис всё равно стартует и честно говорит «не установлен» (и тесты не требуют torch).
"""
from __future__ import annotations

import os
import time
from typing import Optional

import cv2
import numpy as np

GEOMETRIES = ("lvldec", "exact")
MODEL_EXTS = (".pt", ".onnx", ".engine")
POLY_EPS = 1.0            # допуск упрощения контура (px кадра): точек меньше, площадь не меняется заметно


def crop_letterbox(m: np.ndarray, W: int, H: int) -> np.ndarray:
    """Убрать поля letterbox из маски сети (mh × mw), оставив только область, соответствующую кадру W × H."""
    mh, mw = m.shape[:2]
    gain = min(mh / H, mw / W)
    px, py = (mw - W * gain) / 2.0, (mh - H * gain) / 2.0
    left, top = int(round(px - 0.1)), int(round(py - 0.1))
    right, bottom = mw - int(round(px + 0.1)), mh - int(round(py + 0.1))
    if right - left < 2 or bottom - top < 2:
        return m
    return m[top:bottom, left:right]


def mask_to_polygons(m: np.ndarray, W: int, H: int, geometry: str = "lvldec", eps: float = POLY_EPS) -> list:
    """Бинарная маска сети → список внешних контуров [[x, y], …] в пикселях кадра W × H. Мелочь без площади отбрасываем."""
    mm = crop_letterbox(m, W, H) if geometry == "exact" else m
    full = cv2.resize(mm.astype(np.uint8), (W, H), interpolation=cv2.INTER_NEAREST)
    cnts, _ = cv2.findContours(full, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in cnts:
        c = cv2.approxPolyDP(c, eps, True) if eps > 0 else c
        if len(c) >= 3:
            out.append([[int(x), int(y)] for x, y in c.reshape(-1, 2)])
    return out


def box_polygon(b) -> list:
    """Рамка → четырёхугольник (для detect-моделей без масок: зону заливает рамка)."""
    x1, y1, x2, y2 = (int(round(float(v))) for v in b)
    return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]


def pick_device(want: Optional[str] = None) -> str:
    """cpu / cuda:0. Явное значение главнее; иначе CUDA, если есть, иначе процессор."""
    if want:
        return want
    try:
        import torch
        return "cuda:0" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


class SegModel:
    """Загруженная модель. infer() не потокобезопасен — сервер вызывает его под замком."""

    def __init__(self, path: str, device: Optional[str] = None, imgsz: Optional[int] = None):
        from ultralytics import YOLO          # лениво: нет ultralytics — исключение понятно вызывающему
        self.path = path
        self.name = os.path.basename(path)
        self.device = pick_device(device or os.environ.get("LEVEL_DEVICE", "").strip() or None)
        self.model = YOLO(path, task="segment") if path.lower().endswith((".onnx", ".engine")) else YOLO(path)
        self.task = str(getattr(self.model, "task", "") or "")
        self.names = {int(k): str(v) for k, v in dict(self.model.names).items()}
        self.imgsz = int(imgsz or self._train_imgsz())
        self.size = os.path.getsize(path) if os.path.exists(path) else 0
        self.loaded_at = time.time()
        self._warm()

    def _train_imgsz(self) -> int:
        """Размер входа, на котором обучали (из чекпойнта); иначе 640."""
        v = (getattr(self.model, "overrides", None) or {}).get("imgsz")
        if isinstance(v, (list, tuple)) and v:
            v = max(v)
        try:
            return max(32, int(v))
        except (TypeError, ValueError):
            return 640

    def _warm(self):
        """Один пробный прогон: первый настоящий кадр иначе тянется секунды (инициализация весов и памяти)."""
        try:
            self.model.predict(np.zeros((self.imgsz, self.imgsz, 3), np.uint8), imgsz=self.imgsz, device=self.device, verbose=False)
        except Exception:
            pass

    def info(self) -> dict:
        return {"name": self.name, "path": self.path, "task": self.task, "seg": self.task == "segment",
                "classes": [self.names[k] for k in sorted(self.names)], "imgsz": self.imgsz, "device": self.device,
                "size": self.size, "loaded_at": self.loaded_at}

    def infer(self, img: np.ndarray, conf: float = 0.35, iou: float = 0.7, imgsz: Optional[int] = None,
              max_det: int = 300, geometry: str = "lvldec") -> list:
        """Кадр BGR → [{cls, name, conf, bbox, polygons}] по всем найденным объектам (координаты кадра)."""
        H, W = img.shape[:2]
        r = self.model.predict(img, conf=conf, iou=iou, imgsz=int(imgsz or self.imgsz), max_det=max_det,
                               device=self.device, verbose=False)[0]
        masks = getattr(r, "masks", None)
        out = []
        for i, b in enumerate(r.boxes):
            cls = int(b.cls[0])
            box = [float(v) for v in b.xyxy[0]]
            if masks is not None:
                polys = mask_to_polygons(masks.data[i].cpu().numpy() > 0.5, W, H, geometry)
            else:
                polys = [box_polygon(box)]
            out.append({"cls": cls, "name": self.names.get(cls, str(cls)), "conf": round(float(b.conf[0]), 3),
                        "bbox": [round(v, 1) for v in box], "polygons": polys})
        return out
