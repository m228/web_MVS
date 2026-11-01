"""Рисует иконку панели «web_MVS панель» (синий скруглённый квадрат, белая линия пульса, зелёная точка «в сети»).

Запуск (из корня репозитория, нужны только numpy и opencv из venv web_MVS):
    .venv\\Scripts\\python.exe monitor_server\\tools\\make_icons.py
Результат кладётся в monitor_server\\static\\: icon-192.png, icon-512.png (для браузера и «установить как приложение») и panel.ico
(для ярлыка на рабочем столе; внутри PNG 16–256 px). Готовые файлы лежат в репозитории — запускать скрипт нужно только чтобы поменять рисунок.
"""
import struct
from pathlib import Path

import cv2
import numpy as np

OUT = Path(__file__).resolve().parent.parent / "static"
BASE = 1024                                   # рисуем крупно, потом уменьшаем — края гладкие
ICO_SIZES = (256, 128, 64, 48, 32, 16)

PULSE = [(0.13, 0.56), (0.34, 0.56), (0.44, 0.31), (0.56, 0.78), (0.66, 0.45), (0.72, 0.56), (0.87, 0.56)]


def draw() -> np.ndarray:
    """BGRA 1024x1024: прозрачный фон, скруглённый квадрат с градиентом, линия пульса, точка статуса."""
    s = BASE
    yy, xx = np.mgrid[0:s, 0:s].astype(np.float32)
    t = (xx + yy) / (2.0 * s)                                   # 0 слева-сверху, 1 справа-снизу
    top, bot = np.array([246, 130, 59], np.float32), np.array([216, 78, 29], np.float32)   # BGR: #3b82f6 -> #1d4ed8
    grad = (top[None, None, :] * (1 - t[..., None]) + bot[None, None, :] * t[..., None])

    mask = np.zeros((s, s), np.uint8)                           # скруглённый квадрат с отступом 4 %
    m, r = int(s * 0.04), int(s * 0.22)
    cv2.rectangle(mask, (m + r, m), (s - m - r, s - m), 255, -1)
    cv2.rectangle(mask, (m, m + r), (s - m, s - m - r), 255, -1)
    for cx, cy in ((m + r, m + r), (s - m - r, m + r), (m + r, s - m - r), (s - m - r, s - m - r)):
        cv2.circle(mask, (cx, cy), r, 255, -1, cv2.LINE_AA)
    mask = cv2.GaussianBlur(mask, (0, 0), 1.2)

    img = np.zeros((s, s, 4), np.uint8)
    img[..., :3] = np.clip(grad, 0, 255).astype(np.uint8)
    img[..., 3] = mask

    pts = np.array([(int(x * s), int(y * s)) for x, y in PULSE], np.int32)
    thick = int(s * 0.062)
    cv2.polylines(img, [pts], False, (255, 255, 255, 255), thick, cv2.LINE_AA)
    for p in pts:                                               # круглые стыки и концы линии
        cv2.circle(img, tuple(int(v) for v in p), thick // 2, (255, 255, 255, 255), -1, cv2.LINE_AA)

    dot = (int(s * 0.79), int(s * 0.23))
    cv2.circle(img, dot, int(s * 0.095), (255, 255, 255, 255), -1, cv2.LINE_AA)       # белое кольцо
    cv2.circle(img, dot, int(s * 0.07), (128, 222, 74, 255), -1, cv2.LINE_AA)         # зелёная точка #4ade80
    return img


def png_bytes(img: np.ndarray, size: int) -> bytes:
    small = cv2.resize(img, (size, size), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".png", small)
    assert ok
    return buf.tobytes()


def ico_bytes(img: np.ndarray, sizes=ICO_SIZES) -> bytes:
    """ICO с PNG внутри (формат понимает Windows Vista и новее): заголовок, таблица записей, данные."""
    pngs = [(sz, png_bytes(img, sz)) for sz in sizes]
    head = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    table, data = b"", b""
    for sz, png in pngs:
        table += struct.pack("<BBBBHHII", sz % 256, sz % 256, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    return head + table + data


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    img = draw()
    (OUT / "icon-192.png").write_bytes(png_bytes(img, 192))
    (OUT / "icon-512.png").write_bytes(png_bytes(img, 512))
    (OUT / "panel.ico").write_bytes(ico_bytes(img))
    for n in ("icon-192.png", "icon-512.png", "panel.ico"):
        print(n, (OUT / n).stat().st_size, "байт")


if __name__ == "__main__":
    main()
