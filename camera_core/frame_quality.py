"""Качество кадра RTSP: «серая каша» — так декодер H.264/H.265 стирает картинку после потери пакетов.

Та же проверка, что в сервисе КЗ (motion_service/motion_camera.py: garbage_ratio) — сервис ставится отдельно,
со своим venv, поэтому функция продублирована; пороги держать одинаковыми.
"""
import cv2
import numpy as np

GARBAGE_THRESHOLD = 0.3     # доля блоков, залитых серым, с которой кадр считаем кашей (как corrupt_ratio в КЗ)
SAMPLE_PERIOD_S = 1.0       # проверяем не каждый кадр, а раз в секунду: вся нагрузка — сотня микросекунд на кадр
SAMPLE_WIDTH = 640          # кадр перед проверкой ужимаем (как в КЗ анализ идёт на уменьшенной копии)


def garbage_ratio(gray: np.ndarray, bs: int = 16) -> float:
    """Доля блоков bs×bs, залитых ровным серым ~128. Живой кадр (даже тёмный/засвеченный) такого не даёт."""
    h, w = gray.shape[:2]
    h -= h % bs
    w -= w % bs
    if h < bs or w < bs:
        return 0.0
    b = gray[:h, :w].reshape(h // bs, bs, w // bs, bs).astype(np.float32).transpose(0, 2, 1, 3).reshape(-1, bs * bs)
    flat = (b.std(axis=1) < 1.5) & (np.abs(b.mean(axis=1) - 128.0) <= 10.0)
    return float(flat.mean())


def frame_garbage(frame: np.ndarray) -> float:
    """Доля «серой каши» в полноценном (BGR или серый) кадре."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    h, w = gray.shape[:2]
    if w > SAMPLE_WIDTH:
        gray = cv2.resize(gray, (SAMPLE_WIDTH, max(16, int(h * SAMPLE_WIDTH / w))), interpolation=cv2.INTER_AREA)
    return garbage_ratio(gray)
