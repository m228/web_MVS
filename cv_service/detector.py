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


def build_detector(model_path: Optional[str] = None, avg_count: int = 40) -> Detector:
    """Фабрика. Пока модели нет (Фаза 1) — всегда StubDetector.

    В Фазе 8: по расширению model_path выбирать UltralyticsDetector(.pt) / OnnxDetector(.onnx).
    """
    if not model_path:
        return StubDetector(avg_count=avg_count)
    # заготовка на будущее — реальные бэкенды подключим с готовой моделью
    raise NotImplementedError(
        "Реальные бэкенды (.pt/.onnx) подключаются в Фазе 8. Сейчас работает только заглушка."
    )
