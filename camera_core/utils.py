"""camera_core.utils — константы записи/RTSP, преобразования IP, сборка RTSP-ссылок.

Часть бывшего camera_core.py (механический перенос, тела определений не менялись).
"""
import re
import socket
import struct
from logger import log_event
import subprocess


# fps по умолчанию для записи видео, когда камера не сообщила частоту кадров
DEFAULT_VIDEO_FPS = 20.0

# --- надёжность RTSP ---
# сколько секунд тишины (нет удачного кадра) считаем обрывом связи и идём на реконнект.
# Кадры RTSP идут плотно (>=5 fps), поэтому 5 c простоя — это уже точно не «между кадрами».
RTSP_STALL_SECONDS = 5.0
# пауза перед попытками переподключения (сек); дальше повторяется последнее значение
RTSP_RECONNECT_BACKOFF = (1.0, 2.0, 5.0, 10.0, 30.0)
# логируем не каждую попытку реконнекта, а первую и далее каждую N-ю (не спамим журнал)
RTSP_RECONNECT_LOG_EVERY = 5
# порог свободного места на диске (МБ), ниже которого предупреждаем о риске остановки записи
DISK_FREE_WARN_MB = 500
# как часто перепроверять свободное место в фоне (сек)
DISK_CHECK_PERIOD = 30.0

# форматы автосохранения фото: PNG (по умолчанию, без потерь — для анализа кристаллов)
# и JPG (компактно, с потерями). Формат выбирается в UI; дефолт — png.
PHOTO_FORMATS = ("png", "jpg")
# степень сжатия PNG: 0 — БЕЗ сжатия (макс. размер, но lossless и без нагрузки на CPU).
PNG_COMPRESSION = 0


# из ip в int для записи в камеру
def ip_to_int(ip):
    return struct.unpack("!I", socket.inet_aton(ip))[0]


# обратно для показа на списке
def int_to_ip(n):
    return socket.inet_ntoa(struct.pack("!I", n))


# целое -> MAC вида AA:BB:CC:DD:EE:FF (для показа в инфо о камере)
def _format_mac(value):
    value = int(value)
    return ":".join(f"{(value >> shift) & 0xFF:02X}" for shift in (40, 32, 24, 16, 8, 0))


# сборка RTSP-ссылки (формат Dahua/Hikvision-совместимый)
def build_rtsp_url(ip, username="admin", password="", channel=1, subtype=0, port=554):
    credentials = f"{username}:{password}@" if username else ""
    return f"rtsp://{credentials}{ip}:{port}/cam/realmonitor?channel={channel}&subtype={subtype}"


# подменить хост (IP) в RTSP-ссылке, сохранив логин/пароль, порт, путь и query.
# Нужно после смены IP камеры: старый url ведёт на старый адрес. Разбираем строкой,
# а не urllib, чтобы не декодировать уже закодированные логин/пароль.
def replace_host_in_url(url, new_host):
    m = re.match(r"^(rtsps?://)(.*)$", url or "", re.IGNORECASE)
    if not m:
        return url
    scheme, rest = m.group(1), m.group(2)
    slash = rest.find("/")
    authority = rest if slash < 0 else rest[:slash]
    tail = "" if slash < 0 else rest[slash:]
    if "@" in authority:
        userinfo, hostport = authority.rsplit("@", 1)
        userinfo += "@"
    else:
        userinfo, hostport = "", authority
    port = ""
    if ":" in hostport:
        _, p = hostport.rsplit(":", 1)
        port = ":" + p
    return f"{scheme}{userinfo}{new_host}{port}{tail}"


def ping_device(ip: str) -> bool:
    # отбрасываем мусорный ввод до запуска ping: аргументы и так идут списком
    # (shell-инъекция невозможна), но невалидный IP гонять смысла нет
    try:
        socket.inet_aton((ip or "").strip())
    except (OSError, AttributeError):
        log_event("camera_core.change_ip", "пинг: невалидный IP", "warn", {"ip": ip})
        return False

    # -w 500: ждём ответа максимум 0.5 c. Пинг зовётся под _control_lock, а дефолтный
    # таймаут неответа (~4 c на Windows) заблокировал бы все control-операции на это время.
    result = subprocess.run(
        ["ping", "-n", "1", "-w", "500", ip.strip()],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    log_event("camera_core.change_ip", "пинг до устройства", "info", {"result": result.returncode})
    return result.returncode == 0
