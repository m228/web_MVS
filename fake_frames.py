"""fake_frames — синтетические кадры для фейк-режима (WEB_MVS_FAKE=1, см. fake_mode.py).

Две «сцены»: "micro" — поле зрения микроскопа (светлые кристаллы на тёмном фоне, 2592x1944, как у MV-CS050), "rtsp" — общий
вид 1920x1080 (сетка, подпись, движущаяся метка). Основа рисуется один раз на камеру и кэшируется; на каждый кадр поверх
копии кладётся подпись «FAKE <серийник> #<номер> <время>» и бегущая метка — по ним видно, что поток живой и кадры меняются.
"""
import threading
import time

import cv2
import numpy as np

SENSOR_W, SENSOR_H = 2592, 1944          # матрица «микроскопной» GigE-камеры
RTSP_W, RTSP_H = 1920, 1080

_cache: dict = {}
_lock = threading.Lock()


def _seed(serial: str) -> int:
    return sum((i + 1) * ord(c) for i, c in enumerate(str(serial))) % (2 ** 31)


def _micro_base(serial: str, w: int, h: int) -> np.ndarray:
    rng = np.random.default_rng(_seed(serial))
    img = np.full((h, w, 3), (38, 34, 30), np.uint8)
    # мягкий градиент подсветки
    yy, xx = np.mgrid[0:h, 0:w]
    glow = (1.0 - np.hypot((xx - w / 2) / w, (yy - h / 2) / h) * 0.9).clip(0.3, 1.0)
    img = (img * glow[..., None]).astype(np.uint8)
    n = 260
    for _ in range(n):
        cx, cy = int(rng.uniform(0, w)), int(rng.uniform(0, h))
        r = float(rng.lognormal(mean=np.log(w / 45), sigma=0.35))
        k = int(rng.integers(5, 8))
        ang0 = rng.uniform(0, 6.28)
        pts = []
        for i in range(k):
            a = ang0 + 6.2832 * i / k
            rr = r * rng.uniform(0.75, 1.15)
            pts.append((cx + rr * np.cos(a), cy + rr * np.sin(a) * rng.uniform(0.7, 1.0)))
        pts = np.array(pts, np.int32)
        tone = int(rng.uniform(170, 235))
        cv2.fillPoly(img, [pts], (tone - 20, tone - 5, tone))
        cv2.polylines(img, [pts], True, (tone + 15, tone + 18, 255 if tone > 235 else tone + 20), 2, cv2.LINE_AA)
    noise = rng.normal(0, 4, img.shape).astype(np.int16)
    return np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)


def _rtsp_base(serial: str, w: int, h: int) -> np.ndarray:
    rng = np.random.default_rng(_seed(serial))
    top, bot = np.array([70, 62, 55], float), np.array([30, 28, 26], float)
    t = np.linspace(0, 1, h)[:, None, None]
    img = (top * (1 - t) + bot * t).repeat(w, axis=1).astype(np.uint8)
    for x in range(0, w, 120):
        cv2.line(img, (x, 0), (x, h), (90, 84, 76), 1)
    for y in range(0, h, 120):
        cv2.line(img, (0, y), (w, y), (90, 84, 76), 1)
    for _ in range(6):                                   # «оборудование»
        x0, y0 = int(rng.uniform(0, w - 300)), int(rng.uniform(h * 0.3, h - 260))
        cv2.rectangle(img, (x0, y0), (x0 + int(rng.uniform(120, 300)), y0 + int(rng.uniform(100, 240))),
                      tuple(int(v) for v in rng.integers(80, 170, 3)), -1)
    return img


def base_image(serial: str, kind: str = "micro", w: int | None = None, h: int | None = None) -> np.ndarray:
    key = (serial, kind, w, h)
    with _lock:
        img = _cache.get(key)
    if img is None:
        if kind == "micro":
            img = _micro_base(serial, w or SENSOR_W, h or SENSOR_H)
        else:
            img = _rtsp_base(serial, w or RTSP_W, h or RTSP_H)
        with _lock:
            _cache[key] = img
    return img


def render(serial: str, n: int, kind: str = "micro", width: int | None = None, height: int | None = None,
           offset_x: int = 0, offset_y: int = 0, exposure_time: float | None = None, gain: float | None = None,
           mono: bool = False) -> np.ndarray:
    """Кадр № n. width/height/offset — ROI (для «micro» вырезается из полной матрицы); exposure_time (мкс, 10000 = номинал)
    и gain (дБ) меняют яркость, чтобы кнопки «Применить» на вкладке «Камера» были видны."""
    base = base_image(serial, kind)
    bh, bw = base.shape[:2]
    w = max(16, min(int(width or bw), bw))
    h = max(16, min(int(height or bh), bh))
    ox = max(0, min(int(offset_x or 0), bw - w))
    oy = max(0, min(int(offset_y or 0), bh - h))
    img = base[oy:oy + h, ox:ox + w].copy()

    k = 1.0
    if exposure_time:
        k *= float(np.clip(float(exposure_time) / 10000.0, 0.15, 3.0))
    if gain:
        k *= float(10 ** (float(gain) / 40.0))
    if abs(k - 1.0) > 0.02:
        img = cv2.convertScaleAbs(img, alpha=k)
    if mono:
        img = cv2.cvtColor(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)

    scale = max(0.6, min(w, h) / 900.0)
    th = max(1, int(round(2 * scale)))
    label = "FAKE %s  #%d  %s" % (serial, n, time.strftime("%H:%M:%S"))
    cv2.putText(img, label, (int(16 * scale), int(44 * scale)), cv2.FONT_HERSHEY_SIMPLEX, 1.0 * scale, (0, 0, 0), th + 3, cv2.LINE_AA)
    cv2.putText(img, label, (int(16 * scale), int(44 * scale)), cv2.FONT_HERSHEY_SIMPLEX, 1.0 * scale, (60, 220, 255), th, cv2.LINE_AA)
    # бегущая метка снизу: видно, что кадры меняются
    x = int((n * 14) % max(1, w - 40)) + 20
    cv2.circle(img, (x, h - int(30 * scale)), max(4, int(10 * scale)), (60, 220, 255), -1, cv2.LINE_AA)
    return img


class FakeCapture:
    """Заменитель cv2.VideoCapture для RTSP-воркера: isOpened/read/grab/get/set/release."""

    def __init__(self, serial: str, fps: float = 8.0, kind: str = "rtsp"):
        self.serial, self.fps, self.kind = serial, float(fps), kind
        self._n = 0
        self._t = time.time()
        self._open = True

    def isOpened(self):
        return self._open

    def _tick(self):
        wait = self._t + 1.0 / self.fps - time.time()
        if wait > 0:
            time.sleep(wait)
        self._t = time.time()
        self._n += 1

    def read(self):
        if not self.grab():
            return False, None
        return self.retrieve()

    def grab(self):
        """Дождаться следующего кадра (по fps); картинку рисуем лениво — в retrieve, пропущенные кадры ничего не стоят."""
        if not self._open:
            return False
        self._tick()
        return True

    def retrieve(self):
        if not self._open:
            return False, None
        return True, render(self.serial, self._n, self.kind)

    def get(self, prop):
        if prop == cv2.CAP_PROP_FPS:
            return self.fps
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return RTSP_W
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return RTSP_H
        return 0.0

    def set(self, prop, value):
        return True

    def release(self):
        self._open = False
