"""Сканер подсети для страницы «Scan Camera».

Проходит по x.x.x.<от>..<до>, находит хосты с RTSP на портах 554/5054/8554/80, снимает с каждого один кадр
и держит превью в памяти (альбом). Логика та же, что в scripts/rtsp_scan.py, но с прогрессом и остановкой.
Рабочие порт/путь/пароль запоминаются на сервере — по двойному клику камера добавляется без повторного ввода пароля.
Ничего не пишет в базу, пока пользователь сам не добавит камеру.
"""
import os
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

PORTS = (554, 5054, 8554, 80)
# пути потока разных производителей: Dahua, Hikvision, типовые
PATHS = (
    "/cam/realmonitor?channel=1&subtype=0",
    "/Streaming/Channels/101",
    "/h264/ch1/main/av_stream",
    "/live/ch0",
    "/stream1",
    "/",
)
THUMB_W = 320
_PREFIX_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")


def parse_prefix(text):
    """'10.32.2' или '10.32.2.0' -> '10.32.2'; мусор -> None."""
    parts = (text or "").strip().split(".")
    if len(parts) == 4 and parts[3].isdigit():
        parts = parts[:3]
    m = _PREFIX_RE.match(".".join(parts))
    if not m or any(int(g) > 255 for g in m.groups()):
        return None
    return ".".join(str(int(g)) for g in m.groups())


def rtsp_port(ip, port, timeout):
    """Камера = на порту отвечает RTSP (а не просто принимает TCP: прокси/VPN принимают соединение на любом адресе)."""
    try:
        with socket.create_connection((ip, port), timeout=timeout) as sk:
            sk.settimeout(timeout * 2)
            sk.sendall(f"OPTIONS rtsp://{ip}:{port}/ RTSP/1.0\r\nCSeq: 1\r\n\r\n".encode())
            return sk.recv(64).startswith(b"RTSP/")
    except OSError:
        return False


def find_port(ip, timeout):
    for port in PORTS:
        if rtsp_port(ip, port, timeout):
            return port
    return None


def describe_code(ip, port, path, timeout=3):
    """Код ответа на DESCRIBE без пароля: 401 = путь есть, нужен пароль; 404 = пути нет. 0 = нет ответа."""
    try:
        with socket.create_connection((ip, port), timeout=timeout) as sk:
            sk.settimeout(timeout)
            sk.sendall(f"DESCRIBE rtsp://{ip}:{port}{path} RTSP/1.0\r\nCSeq: 2\r\nAccept: application/sdp\r\n\r\n".encode())
            parts = sk.recv(128).split(b"\r\n")[0].decode("latin1").split()
            return int(parts[1]) if len(parts) > 1 else 0
    except (OSError, ValueError):
        return 0


def build_url(ip, port, path, user, password):
    cred = f"{quote(user, safe='')}:{quote(password, safe='')}@" if user else ""
    return f"rtsp://{cred}{ip}:{port}{path}"


def grab(url):
    import cv2  # лениво: чтобы импорт модуля (и тесты) не тянул OpenCV
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    try:
        if not cap.isOpened():
            return None
        frame = None
        for _ in range(5):  # первые кадры часто серые/битые: берём не первый
            ok, f = cap.read()
            if ok and f is not None:
                frame = f
        return frame
    finally:
        cap.release()


def thumb_jpeg(frame):
    import cv2
    h, w = frame.shape[:2]
    if w > THUMB_W:
        frame = cv2.resize(frame, (THUMB_W, max(1, round(h * THUMB_W / w))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return buf.tobytes() if ok else None


def probe(ip, port, user, passwords):
    """-> (jpeg, size, path, password) по первой удачной паре путь/пароль; (None, ...) если кадра нет."""
    # путь, на который камера вообще отвечает (401/200), — иначе перебор 6 путей × N паролей тянется минутами
    paths = [p for p in PATHS if describe_code(ip, port, p) in (200, 401)] or [PATHS[0]]
    for path in paths:
        for pw in passwords:
            frame = grab(build_url(ip, port, path, user, pw))
            if frame is not None:
                return thumb_jpeg(frame), (frame.shape[1], frame.shape[0]), path, pw
    return None, None, None, None


class ScanJob:
    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.reset()

    def reset(self):
        self.running = False
        self.phase = ""          # "ports" | "frames" | ""
        self.total = 0           # адресов к проверке
        self.done = 0
        self.prefix = ""
        self.user = "admin"
        self.started = 0.0
        self.finished = 0.0
        self.items = {}          # ip -> {ip, port, status: pending|frame|noframe, w, h}
        self._found = {}         # ip -> {port, path, password, jpeg}

    def start(self, prefix, first, last, user, passwords, port_timeout=0.7):
        with self._lock:
            if self.running:
                return False
            self.reset()
            self._stop.clear()
            self.running = True
            self.phase = "ports"
            self.prefix, self.user = prefix, user
            self.total = last - first + 1
            self.started = time.time()
            self._thread = threading.Thread(
                target=self._run, args=(prefix, first, last, user, passwords or [""], port_timeout),
                name="scan-camera", daemon=True)
            self._thread.start()
        return True

    def stop(self):
        self._stop.set()

    def _run(self, prefix, first, last, user, passwords, port_timeout):
        try:
            ips = [f"{prefix}.{i}" for i in range(first, last + 1)]
            found = {}

            def check(ip):
                if self._stop.is_set():
                    return
                port = find_port(ip, port_timeout)
                with self._lock:
                    self.done += 1
                    if port:
                        found[ip] = port
                        self.items[ip] = {"ip": ip, "port": port, "status": "pending"}

            with ThreadPoolExecutor(64) as ex:
                list(ex.map(check, ips))
            with self._lock:
                self.phase = "frames"
                self.total, self.done = len(found), 0

            # открытие ffmpeg у OpenCV может висеть до 30 с — поэтому параллельно
            def shoot(ip):
                if self._stop.is_set():
                    return
                port = found[ip]
                try:
                    jpeg, size, path, pw = probe(ip, port, user, passwords)
                except Exception:  # noqa: BLE001
                    jpeg = size = path = pw = None
                with self._lock:
                    self.done += 1
                    if jpeg:
                        self._found[ip] = {"port": port, "path": path, "password": pw, "jpeg": jpeg}
                        self.items[ip].update(status="frame", w=size[0], h=size[1])
                    else:
                        self.items[ip]["status"] = "noframe"

            with ThreadPoolExecutor(min(16, max(1, len(found)))) as ex:
                list(ex.map(shoot, sorted(found, key=lambda s: int(s.rsplit(".", 1)[1]))))
        finally:
            with self._lock:
                self.running = False
                self.phase = ""
                self.finished = time.time()

    def state(self):
        with self._lock:
            items = sorted(self.items.values(), key=lambda i: int(i["ip"].rsplit(".", 1)[1]))
            return {
                "running": self.running, "phase": self.phase, "total": self.total, "done": self.done,
                "prefix": self.prefix, "user": self.user, "stopped": self._stop.is_set(),
                "items": [dict(i) for i in items],
                "seconds": round((self.finished or time.time()) - self.started) if self.started else 0,
            }

    def frame(self, ip):
        with self._lock:
            f = self._found.get(ip)
            return f["jpeg"] if f else None

    def connection(self, ip):
        """Порт/путь/пароль, с которыми кадр снят (для добавления камеры без повторного ввода)."""
        with self._lock:
            f = self._found.get(ip)
            if f:
                return {"port": f["port"], "path": f["path"], "password": f["password"]}
            it = self.items.get(ip)
            return {"port": it["port"], "path": PATHS[0], "password": None} if it else None


job = ScanJob()
