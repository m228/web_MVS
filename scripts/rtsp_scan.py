"""Сканер подсети: ищет RTSP-камеры и сохраняет по кадру с каждой.

Пример:
    .venv\\Scripts\\python.exe scripts\\rtsp_scan.py 192.168.1.0 --password ПАРОЛЬ1 --password ПАРОЛЬ2

Бежит по x.x.x.1 .. x.x.x.254, находит хосты с открытым портом 554 и снимает
один кадр. Файл называется по IP (192.168.1.108.jpg), чтобы искать по адресу.
Ничего не пишет в программу и не трогает её базу.
"""
import argparse
import os
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

sys.stdout.reconfigure(line_buffering=True)
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import cv2  # noqa: E402

# Dahua/Hikvision-совместимый путь, как в camera_core.utils.build_rtsp_url
PATH = "/cam/realmonitor?channel=1&subtype=0"


def port_open(ip, port, timeout):
    """Камера = на порту отвечает RTSP (а не просто принимает TCP: прокси/VPN
    могут принимать соединение на любом адресе)."""
    try:
        with socket.create_connection((ip, port), timeout=timeout) as sk:
            sk.settimeout(timeout * 2)
            sk.sendall(f"OPTIONS rtsp://{ip}:{port}/ RTSP/1.0\r\nCSeq: 1\r\n\r\n".encode())
            return sk.recv(64).startswith(b"RTSP/")
    except OSError:
        return False


def grab_any(ip, user, passwords, port):
    for pw in passwords:
        frame = grab(ip, user, pw, port)
        if frame is not None:
            return frame
    return None


def grab(ip, user, password, port):
    cred = f"{quote(user, safe='')}:{quote(password, safe='')}@" if user else ""
    cap = cv2.VideoCapture(f"rtsp://{cred}{ip}:{port}{PATH}", cv2.CAP_FFMPEG)
    try:
        if not cap.isOpened():
            return None
        # первые кадры часто серые/битые: берём не первый
        frame = None
        for _ in range(5):
            ok, f = cap.read()
            if ok and f is not None:
                frame = f
        return frame
    finally:
        cap.release()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("subnet", help="подсеть, например 192.168.1.0 или 192.168.1")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", action="append",
                    help="можно несколько раз: --password a --password b (пробуются по очереди)")
    ap.add_argument("--port", type=int, default=554)
    ap.add_argument("--out", default="rtsp_scan", help="папка для картинок")
    ap.add_argument("--timeout", type=float, default=0.7, help="таймаут проверки порта, с")
    args = ap.parse_args()

    passwords = args.password or [os.environ.get("RTSP_PASSWORD", "")]
    base = ".".join(args.subnet.split(".")[:3])
    ips = [f"{base}.{i}" for i in range(1, 255)]
    os.makedirs(args.out, exist_ok=True)

    t0 = time.time()
    print(f"Проверяю порт {args.port} на {base}.1-254 ...")
    with ThreadPoolExecutor(64) as ex:
        res = list(ex.map(lambda ip: port_open(ip, args.port, args.timeout), ips))
    found = [ip for ip, ok in zip(ips, res) if ok]
    print(f"RTSP отвечает у {len(found)}: {', '.join(found) or '-'}")

    saved, failed = [], []
    # открытие ffmpeg у OpenCV может висеть до 30 с, поэтому параллельно
    ex = ThreadPoolExecutor(max(1, min(16, len(found))))
    futs = {ex.submit(grab_any, ip, args.user, passwords, args.port): ip for ip in found}
    for fut in as_completed(futs):
        ip = futs[fut]
        try:
            frame = fut.result()
        except Exception as e:  # noqa: BLE001
            print(f"  {ip}: ошибка {e}")
            frame = None
        if frame is None:
            failed.append(ip)
            print(f"  {ip}: кадра нет (неверный логин/пароль или другой путь)")
            continue
        path = os.path.join(args.out, f"{ip}.jpg")
        cv2.imwrite(path, frame)
        saved.append(ip)
        print(f"  {ip}: {frame.shape[1]}x{frame.shape[0]} -> {path}")
    ex.shutdown(wait=False)

    print(f"\nГотово за {time.time() - t0:.0f} с. Кадров: {len(saved)}, без кадра: {len(failed)}")
    if failed:
        print("Без кадра:", ", ".join(sorted(failed)))
    sys.stdout.flush()
    os._exit(0)  # не ждать зависшие потоки ffmpeg


if __name__ == "__main__":
    main()
