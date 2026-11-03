"""camera_core.hw_decode — аппаратное декодирование RTSP на видеоядре (Intel/AMD/NVIDIA через Direct3D 11).

Те же функции, что в motion_service/motion_camera.py: тот сервис — отдельный процесс со своим .venv и
из web_MVS ничего не импортирует, поэтому общий код не вынести.
"""
import threading
import time

import cv2

HW_MODES = ("off", "auto", "d3d11")

OPEN_GAP_S = 2.0
_open_lock = threading.Lock()
_open_next = 0.0


def wait_open_turn(gap=None, clock=time.monotonic, sleep=time.sleep) -> float:
    """Очередь на открытие потока: каждое следующее открытие стартует не раньше чем через gap секунд после предыдущего.
    После перезапуска все камеры открывались одновременно (декодер, сеть и процессор разом — страница не отвечала);
    теперь они подхватываются по одной. Держим лишь расписание, а не само открытие: мёртвая камера (открытие до 30 с)
    остальные не задерживает. -> сколько секунд ждали."""
    global _open_next
    gap = OPEN_GAP_S if gap is None else gap
    with _open_lock:
        now = clock()
        start = max(now, _open_next)
        _open_next = start + gap
    wait = start - now
    if wait > 0:
        sleep(wait)
    return max(wait, 0.0)


def normalize_mode(value) -> str:
    """Неизвестное значение в конфиге -> auto (безопаснее выключенного: при сбое всё равно вернёмся на процессор)."""
    mode = str(value or "auto").strip().lower()
    return mode if mode in HW_MODES else "auto"


def hw_params(mode: str) -> list:
    """Параметры VideoCapture для аппаратного декодирования; [] — программное (или OpenCV без поддержки).
    CAP_PROP_HW_DEVICE не передаём: OpenCV отказывается открывать поток, если устройство указано без явного режима."""
    prop = getattr(cv2, "CAP_PROP_HW_ACCELERATION", None)
    acc = {"auto": getattr(cv2, "VIDEO_ACCELERATION_ANY", None),
           "d3d11": getattr(cv2, "VIDEO_ACCELERATION_D3D11", None)}.get(mode)
    return [prop, acc] if prop is not None and acc is not None else []


def decoder_kind(cap) -> str:
    """'gpu' — поток реально декодируется на видеоядре, 'cpu' — программно, '' — неизвестно (подмена в тестах)."""
    try:
        v = cap.get(cv2.CAP_PROP_HW_ACCELERATION)
    except Exception:
        return ""
    if v == 0:
        return "cpu"
    return "gpu" if 0 < v <= 5 else ""         # VIDEO_ACCELERATION_*: 1..5; иное (подмена) — неизвестно
