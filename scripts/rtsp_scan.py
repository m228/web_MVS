"""Сканер подсети: ищет RTSP-камеры и сохраняет по кадру с каждой.

Пример:
    .venv\\Scripts\\python.exe scripts\\rtsp_scan.py 192.168.1.0 --password ПАРОЛЬ1 --password ПАРОЛЬ2

Бежит по x.x.x.1 .. x.x.x.254, находит хосты, где на одном из портов (554, 5054, 8554, 80) отвечает RTSP, и снимает
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


def rtsp_port(ip, port, timeout):
    """Камера = на порту отвечает RTSP (а не просто принимает TCP: прокси/VPN
    могут принимать соединение на любом адресе)."""
    try:
        with socket.create_connection((ip, port), timeout=timeout) as sk:
            sk.settimeout(timeout * 2)
            sk.sendall(f"OPTIONS rtsp://{ip}:{port}/ RTSP/1.0\r\nCSeq: 1\r\n\r\n".encode())
            return sk.recv(64).startswith(b"RTSP/")
    except OSError:
        return False


def find_port(ip, ports, timeout):
    for port in ports:
        if rtsp_port(ip, port, timeout):
            return port
    return None


# пути потока разных производителей: Dahua, Hikvision, типовые
PATHS = [
    "/cam/realmonitor?channel=1&subtype=0",
    "/Streaming/Channels/101",
    "/h264/ch1/main/av_stream",
    "/live/ch0",
    "/stream1",
    "/",
]


def describe_status(ip, port, path, timeout=3):
    """Код ответа на DESCRIBE без пароля: 401 = путь есть, нужен пароль; 404 = пути нет."""
    try:
        with socket.create_connection((ip, port), timeout=timeout) as sk:
            sk.settimeout(timeout)
            sk.sendall(f"DESCRIBE rtsp://{ip}:{port}{path} RTSP/1.0\r\nCSeq: 2\r\nAccept: application/sdp\r\n\r\n".encode())
            line = sk.recv(128).split(b"\r\n")[0].decode("latin1")
            return line
    except OSError as e:
        return f"нет ответа ({e})"


def grab_any(ip, user, passwords, port):
    """-> (кадр, путь) по первой удачной паре путь/пароль."""
    for path in PATHS:
        for pw in passwords:
            frame = grab(ip, user, pw, port, path)
            if frame is not None:
                return frame, path
    return None, None


def grab(ip, user, password, port, path):
    cred = f"{quote(user, safe='')}:{quote(password, safe='')}@" if user else ""
    cap = cv2.VideoCapture(f"rtsp://{cred}{ip}:{port}{path}", cv2.CAP_FFMPEG)
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
    ap.add_argument("subnet", nargs="?", help="подсеть, например 192.168.1.0 или 192.168.1")
    ap.add_argument("--ips", help="вместо подсети: список адресов через запятую или @файл.txt (по адресу в строке)")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", action="append",
                    help="можно несколько раз: --password a --password b (пробуются по очереди)")
    ap.add_argument("--ports", default="554,5054,8554,80", help="порты через запятую, проверяются по очереди")
    ap.add_argument("--out", default="rtsp_scan", help="папка для картинок")
    ap.add_argument("--timeout", type=float, default=0.7, help="таймаут проверки порта, с")
    args = ap.parse_args()

    passwords = args.password or [os.environ.get("RTSP_PASSWORD", "")]
    if args.ips:
        raw = open(args.ips[1:], encoding="utf-8").read() if args.ips.startswith("@") else args.ips
        ips = [x for x in raw.replace(",", " ").split() if x]
        base = "список"
    elif args.subnet:
        base = ".".join(args.subnet.split(".")[:3])
        ips = [f"{base}.{i}" for i in range(1, 255)]
    else:
        ap.error("укажи подсеть или --ips")
    os.makedirs(args.out, exist_ok=True)

    ports = [int(x) for x in args.ports.split(",") if x.strip()]
    t0 = time.time()
    print(f"Проверяю порты {ports} на {base if args.ips else base + ".1-254"} ({len(ips)} адр.) ...")
    with ThreadPoolExecutor(64) as ex:
        res = list(ex.map(lambda ip: find_port(ip, ports, args.timeout), ips))
    found = {ip: pt for ip, pt in zip(ips, res) if pt}
    print(f"RTSP отвечает у {len(found)}: " + (", ".join(f"{ip}:{pt}" for ip, pt in found.items()) or "-"))

    saved, failed = [], []
    # открытие ffmpeg у OpenCV может висеть до 30 с, поэтому параллельно
    ex = ThreadPoolExecutor(max(1, min(16, len(found))))
    futs = {ex.submit(grab_any, ip, args.user, passwords, pt): ip for ip, pt in found.items()}
    for fut in as_completed(futs):
        ip = futs[fut]
        try:
            frame, used = fut.result()
        except Exception as e:  # noqa: BLE001
            print(f"  {ip}: ошибка {e}")
            frame, used = None, None
        if frame is None:
            failed.append(ip)
            st = describe_status(ip, found[ip], PATHS[0])
            print(f"  {ip}:{found[ip]} кадра нет, ответ камеры на {PATHS[0]}: {st}")
            continue
        path = os.path.join(args.out, f"{ip}.jpg")
        cv2.imwrite(path, frame)
        saved.append(ip)
        print(f"  {ip}:{found[ip]} {frame.shape[1]}x{frame.shape[0]} -> {path}  путь потока: {used}")
    ex.shutdown(wait=False)

    print(f"\nГотово за {time.time() - t0:.0f} с. Кадров: {len(saved)}, без кадра: {len(failed)}")
    if failed:
        print("Без кадра:", ", ".join(sorted(failed)))
    sys.stdout.flush()
    os._exit(0)  # не ждать зависшие потоки ffmpeg


if __name__ == "__main__":
    main()
