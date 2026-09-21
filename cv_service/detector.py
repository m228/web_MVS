"""Абстракция детектора кристаллов + заглушка для Фазы 1.

Детектор получает картинку (BGR numpy) и возвращает список объектов. Каждый объект —
это словарь с боксом, уверенностью и (для seg-моделей) полигоном маски в координатах
ИСХОДНОГО кадра. SAHI-тайлер работает поверх любого детектора (см. sahi_tiler.py).

Бэкенды (появятся в Фазе 8, когда будет модель):
* UltralyticsDetector — .pt (torch-cuda), ОСНОВНОЙ для GTX 1050 Ti.
* OnnxDetector — .onnx (onnxruntime-gpu), резерв.

Сейчас включён StubDetector — генерит псевдослучайные «кристаллы», чтобы прогнать весь
поток (сайдкар → основной app → cv_analyzer → UI) ещё до готовой модели.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np


@dataclass
class Detection:
    """Один найденный объект в координатах ИСХОДНОГО кадра (пиксели).

    bbox — (x1, y1, x2, y2). polygon — список точек [[x, y], ...] контура маски
    (для seg-модели); у detect-модели/заглушки может быть None — тогда размер считаем
    по боксу. conf — уверенность 0..1.
    """
    bbox: tuple[float, float, float, float]
    conf: float
    polygon: Optional[list[list[float]]] = None

    def as_dict(self) -> dict:
        # приводим к обычному float — numpy-типы (np.float64) ломают JSON-сериализацию
        x1, y1, x2, y2 = self.bbox
        return {
            "bbox": [round(float(x1), 1), round(float(y1), 1),
                     round(float(x2), 1), round(float(y2), 1)],
            "conf": round(float(self.conf), 3),
            "polygon": ([[round(float(px), 1), round(float(py), 1)] for px, py in self.polygon]
                        if self.polygon else None),
        }


class Detector:
    """Базовый интерфейс. Реализация возвращает объекты в координатах переданного кадра."""

    name = "base"
    is_seg = False

    def infer(self, image: np.ndarray, conf: float = 0.25) -> list[Detection]:
        raise NotImplementedError

    def info(self) -> dict:
        return {"name": self.name, "seg": self.is_seg, "device": "cpu"}


class StubDetector(Detector):
    """Заглушка: рисует N кругов-«кристаллов» разного размера + пара «браков».

    Детерминирована по хешу кадра, чтобы результат был стабильным на одном изображении
    (удобно для проверки сквозного потока и UI). Никакого GPU/модели не требует.
    """

    name = "stub"
    is_seg = True

    def __init__(self, avg_count: int = 40):
        self.avg_count = avg_count

    def infer(self, image: np.ndarray, conf: float = 0.25) -> list[Detection]:
        h, w = image.shape[:2]
        # seed от размеров + суммы яркости — стабильно для одного кадра, разно для разных
        seed = int((int(image.sum()) ^ (w << 16) ^ h) & 0x7FFFFFFF)
        rng = random.Random(seed)
        n = max(5, int(rng.gauss(self.avg_count, self.avg_count * 0.2)))
        dets: list[Detection] = []
        for _ in range(n):
            r = rng.uniform(6, min(w, h) * 0.06)      # радиус «кристалла» в пикселях
            cx = rng.uniform(r, w - r)
            cy = rng.uniform(r, h - r)
            # многоугольник-аппроксимация круга (для проверки seg-ветки анализа)
            poly = [[cx + r * np.cos(a), cy + r * np.sin(a)]
                    for a in np.linspace(0, 2 * np.pi, 12, endpoint=False)]
            dets.append(Detection(
                bbox=(cx - r, cy - r, cx + r, cy + r),
                conf=rng.uniform(max(conf, 0.3), 0.95),
                polygon=poly,
            ))
        return dets


class ClassicDetector(Detector):
    """Классический OpenCV-детектор кристаллов (без нейросети) — временный, для теста на
    РЕАЛЬНЫХ кадрах до готовой YOLO-модели. Даёт настоящий рассев, чтобы обкатать
    измерения/группы/overlay. НЕ финальное качество: слипшиеся кристаллы не разделяет,
    может ловить пыль. Заменяется на YOLO-seg в Фазе 8.

    Конвейер под подсветку на просвет (кристалл = тёмный контур + светлая середина, виньетка):
      gray → нормализация фона (деление на сильно размытый фон, убирает виньетку)
           → CLAHE → adaptive threshold по тёмным краям → morph close + fill
           → контуры → фильтр по площади/заполненности.
    """

    name = "classic"
    is_seg = True

    def __init__(self, min_area_px: int = 90, max_area_frac: float = 0.05):
        self.min_area_px = min_area_px          # мельче — считаем пылью
        self.max_area_frac = max_area_frac      # крупнее доли кадра — мусор/пятно света

    def infer(self, image: np.ndarray, conf: float = 0.25) -> list[Detection]:
        h, w = image.shape[:2]
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        # 1) убрать виньетку: поделить на сильно размытый фон
        k = max(31, (min(h, w) // 8) | 1)
        bg = cv2.GaussianBlur(gray, (k, k), 0)
        norm = cv2.divide(gray, bg, scale=128).astype(np.uint8)
        # 2) поднять локальный контраст
        norm = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(norm)
        # 3) тёмные края кристаллов -> бинарь (инверсно: тёмное = передний план)
        thr = cv2.adaptiveThreshold(norm, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                    cv2.THRESH_BINARY_INV, 21, 7)
        # 4) сомкнуть контур кристалла и залить нутро
        ksz = max(3, (min(h, w) // 300) | 1)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ksz, ksz))
        closed = cv2.morphologyEx(thr, cv2.MORPH_CLOSE, kernel, iterations=2)
        closed = cv2.morphologyEx(closed, cv2.MORPH_OPEN, kernel, iterations=1)
        cnts, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        max_area = self.max_area_frac * h * w
        dets: list[Detection] = []
        for c in cnts:
            area = cv2.contourArea(c)
            if area < self.min_area_px or area > max_area:
                continue
            x, y, ww, hh = cv2.boundingRect(c)
            approx = c.reshape(-1, 2)
            # прореживаем контур, чтобы полигон был компактным
            eps = 0.01 * cv2.arcLength(c, True)
            poly = cv2.approxPolyDP(c, eps, True).reshape(-1, 2).tolist()
            dets.append(Detection(
                bbox=(x, y, x + ww, y + hh),
                conf=0.5,
                polygon=[[float(px), float(py)] for px, py in poly],
            ))
        return dets

    def info(self) -> dict:
        return {"name": self.name, "seg": self.is_seg, "device": "cpu",
                "note": "классика (временно), заменится YOLO-seg"}


def build_detector(model_path: Optional[str] = None, avg_count: int = 40) -> Detector:
    """Фабрика бэкендов.

    * model_path пусто → StubDetector (фейковые круги, для сквозной проверки без картинок).
    * model_path == 'classic' → ClassicDetector (реальная классика на настоящих кадрах).
    * .pt/.onnx → реальная YOLO (Фаза 8, пока не подключено).
    """
    if not model_path:
        return StubDetector(avg_count=avg_count)
    if model_path.lower() == "classic":
        return ClassicDetector()
    raise NotImplementedError(
        "Бэкенды .pt/.onnx подключаются в Фазе 8. Сейчас доступны: '' (заглушка), 'classic'."
    )
