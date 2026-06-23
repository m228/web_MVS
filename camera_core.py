from datetime import datetime
import os
import re
import threading
import time
from pathlib import Path

from harvesters.core import Harvester
import numpy as np
import socket
import struct
import cv2

from logger import log_event

import subprocess


# --- Совместимость с Hikrobot MVS: неподдерживаемая регистрация событий GenTL ---
# harvesters в ImageAcquirer.__init__ вызывает module.register_event(...) для
# модулей System/Interface/Device. Hikrobot-продюсер их не реализует (GenTL -1003)
# и harvesters штатно ловит это как NotImplementedException и пропускает. НО
# genicam 1.5.1, собирая текст ошибки из не-UTF-8 байтов продюсера, бросает
# UnicodeDecodeError ВМЕСТО NotImplementedException — а его harvesters не ловит,
# и create() падает (нестабильно, в зависимости от "мусорных" байтов).
# Чиним точечно: оборачиваем register_event этих модулей так, чтобы такой
# UnicodeDecodeError превращался обратно в NotImplementedException. События нужны
# только для асинхронных уведомлений; кадры мы берём через fetch(), DataStream
# не трогаем — стрим не страдает.
def _patch_genicam_register_event():
    try:
        from genicam import gentl
    except Exception as exc:
        log_event("camera_core.compat", "genicam.gentl недоступен, патч событий пропущен", "warn", {"error": str(exc)})
        return

    not_implemented = getattr(gentl, "NotImplementedException", None)
    if not_implemented is None:
        return

    patched = []
    for cls_name in ("System", "Interface", "Device"):
        cls = getattr(gentl, cls_name, None)
        original = getattr(cls, "register_event", None) if cls is not None else None
        if original is None or getattr(original, "_mvs_guarded", False):
            continue

        def make_guard(orig):
            def guard(self, *args, **kwargs):
                try:
                    return orig(self, *args, **kwargs)
                except UnicodeDecodeError as exc:
                    raise not_implemented(
                        "register_event не поддерживается продюсером (не-UTF-8 ответ GenTL)"
                    ) from exc
            guard._mvs_guarded = True
            return guard

        try:
            cls.register_event = make_guard(original)
            patched.append(cls_name)
        except Exception as exc:
            log_event("camera_core.compat", f"не удалось пропатчить {cls_name}.register_event", "warn", {"error": str(exc)})

    if patched:
        log_event("camera_core.compat", "Включена совместимость событий GenTL (Hikrobot)", "info", {"patched": patched})


_patch_genicam_register_event()


# понятные подсказки для типичных GenTL-кодов
GENTL_HINTS = {
    -1003: "операция не поддерживается камерой или GenTL-интерфейсом",
    -1005: "доступ запрещён — камера занята другим клиентом (закройте MVS / другое приложение)",
    -1006: "продюсер не может работать с камерой через этот интерфейс (попробуйте автовыбор или другую запись из списка)",
    -1011: "таймаут получения кадра — потери UDP-пакетов (другая подсеть / маршрутизатор / MTU)",
    -1020: "исчерпаны ресурсы драйвера, требуется сброс",
}

# таймаут на один кадр (сек) и сколько таймаутов подряд можно стерпеть до выхода
FRAME_FETCH_TIMEOUT = 5.0
MAX_FRAME_TIMEOUTS = 20
# пока камера прогревается (до первого кадра) таймауты не считаем ошибками и
# терпим дольше — промышленная камера может «раскачиваться» несколько секунд
WARMUP_MAX_TIMEOUTS = 60


def _gentl_code(error_text):
    match = re.search(r"ID:\s*(-?\d+)", error_text or "")
    return int(match.group(1)) if match else None


# genicam новых версий декодирует сообщения GenTL-продюсера строго как UTF-8.
# Hikrobot MvProducerGEV.cti для части операций отдаёт сообщение с байтами не из
# UTF-8 (например, b'...\xc0\x1d\x1e...'), и genicam падает с UnicodeDecodeError
# ещё до того, как поднять нормальную GenTL-ошибку. Достаём сырые байты и
# декодируем терпимо (latin-1), чтобы вытащить читаемый текст и код (ID: -1003).
def _decode_gentl_message(error):
    if isinstance(error, UnicodeDecodeError):
        try:
            return error.object.decode("latin-1", "replace")
        except Exception:
            return None
    return None


def _explain_error(error):
    decoded = _decode_gentl_message(error)
    text = decoded if decoded is not None else repr(error)
    code = _gentl_code(text)

    if code:
        result = {"error": text, "code": code, "hint": GENTL_HINTS.get(code)}
    else:
        result = {"error": text}

    # это не баг приложения: продюсер вернул не-UTF-8 сообщение, а свежая genicam
    # не смогла его декодировать — почти всегда это рассинхрон версий
    # genicam/harvesters и драйвера (.cti) после обновления библиотек
    if isinstance(error, UnicodeDecodeError):
        result["decode_error"] = True
        result.setdefault("hint", GENTL_HINTS.get(-1003))
        result["lib_hint"] = (
            "genicam не смог декодировать сообщение GenTL-продюсера (не UTF-8). "
            "Скорее всего обновились genicam/harvesters — откатите их к рабочей "
            "версии или замените Driver/MvProducerGEV.cti на совместимый."
        )
    return result


# RTSP поверх TCP — стабильнее, меньше «рассыпающихся» кадров
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

# Путь до директории программы и имена файлов драйвера. Поставка самодостаточна:
# и продюсер (.cti), и его runtime (MvCameraControl.dll) лежат в папке Driver/.
PROGRAM_DIR = Path(__file__).resolve().parent
CTI_FILENAME = "MvProducerGEV.cti"
MVS_RUNTIME_DLL = "MvCameraControl.dll"


# Поиск GenTL-продюсера: явный путь (MVS_CTI_PATH) -> копия в папке программы
# (Driver/), но только если рядом лежит её runtime-DLL -> каталоги из
# GENICAM_GENTL64_PATH (системный MVS) -> бандл без runtime как последний шанс.
def _discover_cti():
    explicit = os.environ.get("MVS_CTI_PATH")
    if explicit and Path(explicit).is_file():
        return Path(explicit), "env:MVS_CTI_PATH"

    # бандл берём, только если рядом есть и его runtime (MvCameraControl.dll) —
    # иначе продюсер не загрузится, и лучше упасть на системный MVS
    bundled = next(PROGRAM_DIR.rglob(CTI_FILENAME), None)
    if bundled is not None and (bundled.parent / MVS_RUNTIME_DLL).is_file():
        return bundled, "bundled"

    for raw in (os.environ.get("GENICAM_GENTL64_PATH") or "").split(os.pathsep):
        directory = Path(raw) if raw else None
        if directory and directory.is_dir():
            hit = next(directory.glob(CTI_FILENAME), None)
            if hit:
                return hit, "env:GENICAM_GENTL64_PATH"

    if bundled is not None:
        return bundled, "bundled"
    return None, None


# Регистрируем папку с .cti в путях поиска DLL, чтобы бандл MvCameraControl.dll
# подхватывался без установленного в системе MVS (самодостаточная поставка).
def _register_driver_dll_dir():
    cti, _ = _discover_cti()
    if cti is None or not hasattr(os, "add_dll_directory"):
        return
    try:
        os.add_dll_directory(str(cti.parent))
    except Exception as exc:
        log_event("camera_core.driver", "Не удалось добавить папку DLL в поиск", "warn", {"error": str(exc)})


_register_driver_dll_dir()


# Путь к runtime-DLL продюсера (рядом с .cti или в PATH), либо None.
def _find_mvs_runtime():
    cti, _ = _discover_cti()
    search = [cti.parent] if cti is not None else []
    search += [Path(d) for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    for directory in search:
        try:
            candidate = directory / MVS_RUNTIME_DLL
            if candidate.is_file():
                return str(candidate)
        except Exception:
            pass
    return None

# fps по умолчанию для записи видео, когда камера не сообщила частоту кадров
DEFAULT_VIDEO_FPS = 20.0


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


# разбор сырого буфера кадра в BGR-картинку независимо от пиксельного формата.
# Число каналов определяем по размеру буфера: 3 (RGB/BGR), 1 (Mono/Bayer -> серое),
# 4 (RGBA). Раньше код жёстко решейпил в 3 канала и падал на моно-камере
# (ValueError: cannot reshape array of size ... into (h, w, 3)).
def _to_bgr(data, width, height):
    arr = np.asarray(data, dtype=np.uint8).reshape(-1)
    pixels = width * height
    if pixels <= 0 or arr.size == 0 or arr.size % pixels != 0:
        return None

    channels = arr.size // pixels
    if channels == 3:
        # как и раньше: продюсер отдаёт 3 канала — кодируем как есть
        return arr.reshape(height, width, 3)
    if channels == 1:
        return cv2.cvtColor(arr.reshape(height, width), cv2.COLOR_GRAY2BGR)
    if channels == 4:
        return cv2.cvtColor(arr.reshape(height, width, 4), cv2.COLOR_RGBA2BGR)
    return None


class BaseCameraWorker:
    """Общее состояние и механизмы сохранения (фото/видео) для всех типов камер."""

    def __init__(self, serial_number, manager):
        self.serial_number = serial_number
        self.manager = manager

        self.running = False

        self.save_photo = False
        self.photo_interval = None
        self.last_photo = None

        # 0 нет автосохранения видео / 1 идёт / 2 завершение
        self.save_video = 0
        self.video_duration = None
        self.video_start = None
        self.video_writer = None

        self.metrics = {
            "fps": 0.0,
            "image_number": 0,
            "bandwidth_mbps": 0.0,
            "width": 0,
            "height": 0,
            "errors": 0,
        }

    # ---------- состояние потока ----------

    def stream_state(self):
        return {
            "serial_number": self.serial_number,
            "running": self.running,
            "closed": not self.running,
        }

    def close(self):
        self.running = False
        log_event("camera_core.close_stream", "Запрошена мягкая остановка потока")
        return {"status": "stopping"}

    # ---------- фото ----------

    def on_photo(self, interval):
        self.save_photo = True
        self.photo_interval = interval

        log_event("camera_core.on_save", "Вкл. автосохранение фото c интервалом", "info", {"interval": interval})
        return {"status": "ok", "photo_enabled": True, "interval": self.photo_interval}

    def off_photo(self):
        self.save_photo = False
        self.photo_interval = None
        self.last_photo = None

        log_event("camera_core.off_save", "Выкл. автосохранение фото")
        return {"status": "ok", "photo_enabled": False}

    def _should_save_photo(self, interval):
        current_time = time.time()

        if interval is None:
            return False

        if self.last_photo is None:
            self.last_photo = current_time
            return True

        if current_time - self.last_photo >= interval:
            self.last_photo = current_time
            return True

        return False

    @staticmethod
    def write_photo(img):
        folder = Path("dataset")
        folder.mkdir(parents=True, exist_ok=True)
        filename = f"frame_{datetime.now().strftime('%d_%m_%H_%M_%S')}.jpg"
        path = os.path.join(folder, filename)
        cv2.imwrite(path, img)

    # ---------- видео ----------

    def on_video(self, duration):
        if duration is None:
            self.video_duration = None
        elif self.save_video == 0:
            self.video_duration = duration

        if self.save_video == 0:
            self.save_video = 1
            self.video_start = time.time()
        log_event("camera_core.on_video", "Вкл. автосохранение видео с длительностью: ", "info", {"video_duration": self.video_duration})
        return {"status": "ok", "video_enabled": "1"}

    def off_video(self):
        if self.save_video == 1:
            self.save_video = 2
        log_event("camera_core.off_video", "Выкл. автосохранение видео")
        return {"status": "ok", "video_enabled": "2"}

    def _check_video_finished(self):
        if self.save_video == 1 and self.video_duration is not None:
            if time.time() - self.video_start >= self.video_duration:
                self.save_video = 2

    def _write_video(self, img, fps):
        if self.video_writer is None:
            folder = Path("Videos")
            folder.mkdir(parents=True, exist_ok=True)
            filename = f"Video{datetime.now().strftime('%d_%m_%H_%M_%S')}.avi"
            path = os.path.join(folder, filename)
            fourcc = cv2.VideoWriter_fourcc(*"MJPG")
            writer_fps = fps if fps and fps > 0 else DEFAULT_VIDEO_FPS
            self.video_writer = cv2.VideoWriter(path, fourcc, writer_fps, (img.shape[1], img.shape[0]))

        self.video_writer.write(img)

    # ---------- сохранение в цикле стрима ----------

    # вызывается на каждый кадр: автофото + запись видео
    def _maybe_save(self, img, fps):
        if self.save_photo and self._should_save_photo(self.photo_interval):
            self.write_photo(img)

        self._check_video_finished()
        if self.save_video == 1:
            self._write_video(img, fps)

        if self.save_video == 2 and self.video_writer is not None:
            self.video_writer.release()
            self.video_writer = None
            self.save_video = 0
            self.video_duration = None
            self.video_start = None

    # сброс состояния сохранения при закрытии потока
    def _reset_save_state(self):
        self.save_photo = False
        self.photo_interval = None
        self.last_photo = None

        if self.video_writer is not None:
            self.video_writer.release()
            self.video_writer = None
            self.save_video = 0
            self.video_duration = None
            self.video_start = None


class CameraWorker(BaseCameraWorker):
    """Промышленная GigE Vision камера (Harvester/GenICam)."""

    def __init__(self, serial_number, manager):
        super().__init__(serial_number, manager)

        self.ia = None
        # лимиты/текущие настройки камеры (заполняется при подключении)
        self.data_limit = None
        self.advanced_settings = False

        # GenTL ID сетевого интерфейса (вспомогательный фильтр)
        self.interface_id = None
        # уникальный ключ конкретной записи в device_info_list — основной критерий выбора
        self.device_handle = None

        # сериализация одновременных control-операций к одной камере
        # (без него Promise.all из фронта открывает 5 acquirer'ов на один control-канал
        # и они дерутся за -1005 AccessDenied)
        self._control_lock = threading.Lock()

        # кэш последних "сетевых" данных, чтобы не дёргать control повторно
        self._cached_ip = None              # {"ip": "..."} | None
        self._cached_network = None         # (ip, mask, gateway, dhcp) | None
        self._cache_ts = 0.0
        self._cache_ttl = 10.0              # сек — кэш живёт между refresh-цикл UI

    # запомнить выбранную пользователем запись (handle) и/или интерфейс
    def select_interface(self, interface_id=None, device_handle=None):
        # переключение допустимо только при закрытом потоке —
        # иначе self.ia, открытый через старый интерфейс, повиснет
        if self.running:
            return {"status": "stream_running",
                    "hint": "сначала остановите поток, потом меняйте интерфейс"}

        self.interface_id = interface_id or None
        self.device_handle = device_handle or None
        # при смене записи кэш мог относиться к другой — инвалидируем
        self._cached_ip = None
        self._cached_network = None
        log_event("camera_core.select_interface", "Выбрана запись камеры", "info",
                  {"serial_number": self.serial_number,
                   "interface_id": self.interface_id, "device_handle": self.device_handle})
        return {"status": "ok",
                "interface_id": self.interface_id, "device_handle": self.device_handle}

    # ---------- доступ к камере / nodemap ----------

    # подключение к камере и получение nodemap
    def open_node_map(self):
        # access_status проверяем агрегированный (по серийнику) — статус для конкретной
        # записи может быть != 1, но другая запись того же серийника при этом откроется
        if not self.manager.cam_online.get(self.serial_number):
            log_event("camera_core.get_node_map_cam", "Камера недоступна для подключения", "error",
                      {"serial_number": self.serial_number,
                       "hint": "пересканируйте список камер или проверьте, не занята ли камера другим приложением"})
            return None, None

        ia = None
        try:
            ia = self.manager.create_acquirer(
                self.serial_number,
                interface_id=self.interface_id,
                device_handle=self.device_handle,
            )
            node_map = ia.remote_device.node_map
            self.data_limit = self.read_settings(node_map)
            return node_map, ia
        except Exception as e:
            if ia is not None:
                try:
                    ia.destroy()
                except Exception:
                    pass
            payload = {"serial_number": self.serial_number,
                       "interface_id": self.interface_id,
                       "device_handle": self.device_handle,
                       **_explain_error(e)}
            log_event("camera_core.get_node_map_cam", "Ошибка подключения к камере", "error", payload)
            return None, None

    # получение данных с камеры, текущие + лимиты
    def read_settings(self, node_map):
        if not self.manager.check():
            return None
        data = {
            "width": {
                "value": node_map.Width.value,
                "min": node_map.Width.min,
                "max": node_map.Width.max,
                "step": node_map.Width.inc,
            },
            "height": {
                "value": node_map.Height.value,
                "min": node_map.Height.min,
                "max": node_map.Height.max,
                "step": node_map.Height.inc,
            },
            "offset_x": {
                "value": node_map.OffsetX.value,
                "min": node_map.OffsetX.min,
                "max": node_map.OffsetX.max,
            },
            "offset_y": {
                "value": node_map.OffsetY.value,
                "min": node_map.OffsetY.min,
                "max": node_map.OffsetY.max,
            },
            "exposure_time": {
                "value": node_map.ExposureTime.value,
                "min": node_map.ExposureTime.min,
                "max": node_map.ExposureTime.max,
            },
            "exposure_auto": {
                "value": node_map.ExposureAuto.value,
                "options": node_map.ExposureAuto.symbolics,
            },
        }
        # пиксельный формат (RGB8/Mono8/BayerRG8/...) — нужен, чтобы выбрать цвет;
        # набор у каждой камеры свой, поэтому через try
        try:
            data["pixel_format"] = {
                "value": node_map.PixelFormat.value,
                "options": list(node_map.PixelFormat.symbolics),
            }
        except Exception:
            pass
        return data

    # ---------- информация о камере ----------

    # получение айпи камеры по серийнику
    def get_ip(self):
        status = self.manager.access_status(self.serial_number)
        if status != 1:
            log_event("camera_core.get_ip", "Ошибка получения ip камеры", "error", {"status_camera": str(status)})
            return None

        # пробные данные(потом убрать)
        if self.serial_number == "DA123123":
            return {"ip": "192.168.2.10"}

        # кэш — если параллельно/недавно уже спрашивали, возвращаем без открытия control
        if self._cached_ip is not None and (time.time() - self._cache_ts) < self._cache_ttl:
            return self._cached_ip

        if not self.manager.check():
            return None

        # control-операция — сериализуем (см. self._control_lock)
        with self._control_lock:
            # пока ждали лок, кто-то другой мог уже получить ответ — используем его
            if self._cached_ip is not None and (time.time() - self._cache_ts) < self._cache_ttl:
                return self._cached_ip

            ia = None
            try:
                node_map, ia = self.open_node_map()
                if node_map is None:
                    return None
                ip = int_to_ip(node_map.GevCurrentIPAddress.value)
                self._cached_ip = {"ip": ip}
                self._cache_ts = time.time()
                return self._cached_ip
            finally:
                if ia is not None:
                    try:
                        ia.destroy()
                    except Exception:
                        pass

    # полная read-only информация о камере (для модалки «инфо»).
    # Как и get_ip: открываем control, читаем доступные узлы, отдаём список.
    def get_info(self):
        status = self.manager.access_status(self.serial_number)
        if status != 1:
            log_event("camera_core.get_info", "Камера недоступна для запроса информации", "warn",
                      {"serial_number": self.serial_number, "status_camera": str(status)})
            return None

        # пробные данные(потом убрать)
        if self.serial_number == "DA123123":
            return {"items": [
                {"label": "Модель", "value": "Mock-камера"},
                {"label": "Серийный номер", "value": "DA123123"},
                {"label": "IP-адрес", "value": "192.168.2.10"},
            ]}

        if not self.manager.check():
            return None

        # control-операция — сериализуем (один control-канал на камеру)
        with self._control_lock:
            ia = None
            try:
                node_map, ia = self.open_node_map()
                if node_map is None:
                    return None
                return {"items": self._collect_info(node_map)}
            finally:
                if ia is not None:
                    try:
                        ia.destroy()
                    except Exception:
                        pass

    # сбор доступных read-only полей; отсутствующие узлы тихо пропускаем
    @staticmethod
    def _collect_info(node_map):
        def val(node_name, fmt=None):
            try:
                value = getattr(node_map, node_name).value
            except Exception:
                return None
            if fmt is not None:
                try:
                    return fmt(value)
                except Exception:
                    return value
            return value

        items = []

        def add(label, value):
            if value is not None and value != "":
                items.append({"label": label, "value": str(value)})

        add("Модель", val("DeviceModelName"))
        add("Производитель", val("DeviceVendorName"))
        add("Серийный номер", val("DeviceSerialNumber"))
        add("Версия прошивки", val("DeviceVersion") or val("DeviceFirmwareVersion"))
        add("Имя устройства", val("DeviceUserID"))
        add("IP-адрес", val("GevCurrentIPAddress", lambda v: int_to_ip(int(v))))
        add("Маска подсети", val("GevCurrentSubnetMask", lambda v: int_to_ip(int(v))))
        add("Шлюз", val("GevCurrentDefaultGateway", lambda v: int_to_ip(int(v))))
        add("MAC-адрес", val("GevMACAddress", _format_mac))

        width_max, height_max = val("WidthMax"), val("HeightMax")
        if width_max and height_max:
            add("Макс. разрешение", f"{width_max} × {height_max}")

        width, height = val("Width"), val("Height")
        if width and height:
            add("Текущее разрешение", f"{width} × {height}")

        add("Формат пикселей", val("PixelFormat"))

        frame_rate = val("AcquisitionFrameRate")
        if frame_rate:
            try:
                add("Частота кадров", f"{float(frame_rate):.2f} fps")
            except Exception:
                add("Частота кадров", frame_rate)

        temperature = val("DeviceTemperature")
        if temperature is not None:
            try:
                add("Температура", f"{float(temperature):.1f} °C")
            except Exception:
                add("Температура", temperature)

        return items

    # ---------- применение настроек ----------

    # проверка диапазона значения настройки
    @staticmethod
    def check_value(value, min_value, max_value) -> bool:
        if value is None:
            return False
        return min_value <= value <= max_value

    def apply_settings(self, node_map, width=None, height=None, offset_x=None, offset_y=None,
                       fps=None, exposure_auto=None, exposure_time=None, pixel_format=None):
        limits = self.data_limit
        try:
            # пиксельный формат задаём первым: он меняет размер кадра/каналы,
            # и от него зависит корректный разбор буфера в get_frame
            if pixel_format:
                try:
                    if pixel_format in node_map.PixelFormat.symbolics:
                        node_map.PixelFormat.value = pixel_format
                    else:
                        log_event("camera_core.apply_settings_camera", "Пиксельный формат не поддерживается камерой",
                                  "warn", {"pixel_format": pixel_format})
                except Exception as e:
                    log_event("camera_core.apply_settings_camera", "Не удалось задать пиксельный формат",
                              "warn", {"error": str(e), "pixel_format": pixel_format})

            if self.check_value(width, limits["width"]["min"], limits["width"]["max"]):
                node_map.Width.value = int(width)

            if self.check_value(height, limits["height"]["min"], limits["height"]["max"]):
                node_map.Height.value = int(height)

            # смещения проверяем по актуальным границам узла (зависят от width/height)
            if self.check_value(offset_x, node_map.OffsetX.min, node_map.OffsetX.max):
                node_map.OffsetX.value = int(offset_x)

            if self.check_value(offset_y, node_map.OffsetY.min, node_map.OffsetY.max):
                node_map.OffsetY.value = int(offset_y)

            if self.check_value(fps, 0.1, 30):
                # камера следует заданному FPS только при включённом
                # AcquisitionFrameRateEnable (в MVS — галочка "Acquisition Frame
                # Rate Control Enable"); иначе AcquisitionFrameRate игнорируется.
                # Имя ноды у разных прошивок отличается — пробуем оба варианта.
                enabled = False
                for node_name in ("AcquisitionFrameRateEnable", "AcquisitionFrameRateControlEnable"):
                    try:
                        getattr(node_map, node_name).value = True
                        enabled = True
                        break
                    except Exception:
                        continue
                try:
                    node_map.AcquisitionFrameRate.value = float(fps)
                except Exception as e:
                    log_event("camera_core.apply_settings_camera", "Не удалось задать FPS", "warn", {"error": str(e)})
                log_event("camera_core.apply_settings_camera", "FPS применён", "info",
                          {"requested_fps": float(fps), "rate_control_enabled": enabled})

            # авто-экспозиция (Off / Once / Continuous)
            if exposure_auto is not None and exposure_auto in node_map.ExposureAuto.symbolics:
                node_map.ExposureAuto.value = exposure_auto

            # ручную экспозицию выставляем только при выключенной авто-экспозиции
            if exposure_auto in (None, "Off") and self.check_value(
                exposure_time, limits["exposure_time"]["min"], limits["exposure_time"]["max"]
            ):
                node_map.ExposureTime.value = int(exposure_time)

            return True, None

        except Exception as e:
            log_event("camera_core.apply_settings_camera", "Ошибка применение параметров камеры", "error", {"error": str(e)})
            return False, e

    # ---------- стрим ----------

    def get_frame(self, ia, node_map):
        try:
            with ia.fetch(timeout=FRAME_FETCH_TIMEOUT) as buffer:
                data = buffer.payload.components[0].data
                real_width = node_map.Width.value
                real_height = node_map.Height.value
                img = _to_bgr(data, real_width, real_height)

                if img is None:
                    log_event("camera_core.get_frame", "Не удалось разобрать кадр (формат пикселей)", "warn",
                              {"serial_number": self.serial_number,
                               "size": int(np.asarray(data).size),
                               "width": real_width, "height": real_height,
                               "hint": "выберите подходящий пиксельный формат (RGB/Mono)"})
                    return None, None

                ok, encoded = cv2.imencode(".jpg", img)

                if not ok:
                    log_event("camera_core.get_frame", "Ошибка кодирования кадра", "warn")
                    return None, None

                return img, encoded.tobytes()
        except Exception as e:
            # таймаут получения кадра — не фатально, пропускаем кадр и крутим цикл дальше
            if _gentl_code(repr(e)) == -1011:
                return None, None
            if not self.running:
                return None, None
            raise

    def generate(self, width=None, height=None, offset_x=None, offset_y=None,
                 fps=None, exposure_auto=None, exposure_time=None, pixel_format=None):
        ia = None
        last_frame_time = None

        if not self.manager.check():
            return

        if self.running:
            log_event("camera_core.generate_stream", "Старый поток открыт, принудительно закрытие", "warn")
            self.force_close()
            time.sleep(0.3)

        try:
            log_event(
                "camera_core.generate_stream",
                "Запрошен старт потока",
                "info",
                {
                    "serial_number": self.serial_number,
                    "width": width,
                    "height": height,
                    "offset_x": offset_x,
                    "offset_y": offset_y,
                    "fps": fps,
                    "exposure_auto": exposure_auto,
                    "exposure_time": exposure_time,
                    "pixel_format": pixel_format,
                },
            )

            node_map, ia = self.open_node_map()
            if node_map is None or ia is None:
                # подробная причина уже в логе open_node_map — здесь только статус потока
                return

            ok, err = self.apply_settings(
                node_map,
                width=width,
                height=height,
                offset_x=offset_x,
                offset_y=offset_y,
                fps=fps,
                exposure_auto=exposure_auto,
                exposure_time=exposure_time,
                pixel_format=pixel_format,
            )

            if not ok:
                log_event("camera_core.generate_stream", "Ошибка применения настроек камеры", "warn", {"error": str(err)})
                return

            self.ia = ia
            self.running = True

            ia.start()
            log_event("camera_core.generate_stream", "Поток камеры запущен", "success", {"serial_number": self.serial_number})

            # метрики считаем по факту текущего сеанса (стартовый прогрев не копим)
            self.metrics["errors"] = 0
            self.metrics["image_number"] = 0
            self.metrics["fps"] = 0.0
            self.metrics["bandwidth_mbps"] = 0.0

            timeouts_in_a_row = 0
            first_frame = False

            while self.running:
                try:
                    img, frame = self.get_frame(ia, node_map)

                    if frame is None or img is None:
                        timeouts_in_a_row += 1
                        # до первого кадра камера ещё прогружается: таймауты не
                        # считаем ошибками и терпим дольше (WARMUP_MAX_TIMEOUTS)
                        limit = MAX_FRAME_TIMEOUTS if first_frame else WARMUP_MAX_TIMEOUTS
                        if first_frame:
                            self.metrics["errors"] += 1
                        if timeouts_in_a_row >= limit:
                            log_event("camera_core.generate_stream",
                                      "Поток прерван: подряд слишком много таймаутов получения кадра", "error",
                                      {"serial_number": self.serial_number,
                                       "timeouts": timeouts_in_a_row,
                                       "hint": GENTL_HINTS[-1011]})
                            break
                        continue

                    timeouts_in_a_row = 0
                    first_frame = True

                    now = time.time()

                    self.metrics["image_number"] += 1
                    self.metrics["width"] = img.shape[1]
                    self.metrics["height"] = img.shape[0]

                    if last_frame_time is not None:
                        dt = now - last_frame_time
                        if dt > 0:
                            self.metrics["fps"] = 1.0 / dt

                    if self.metrics["fps"] > 0:
                        self.metrics["bandwidth_mbps"] = (len(frame) * 8 * self.metrics["fps"]) / 1_000_000

                    last_frame_time = now

                except Exception as e:
                    if not self.running:
                        break
                    log_event("camera_core.generate_stream", "Ошибка получения потока", "error",
                              {"serial_number": self.serial_number, **_explain_error(e)})
                    break

                # автофото + запись видео
                self._maybe_save(img, fps)

                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                )

        except Exception as e:
            if not self.running:
                log_event("camera_core.generate_stream", "Поток остановлен", "error", {"error": repr(e)})
            else:
                log_event("camera_core.generate_stream", "Ошибка потока", "error", {"error": repr(e)})
                self.metrics["errors"] += 1

        finally:
            log_event("camera_core.generate_stream", "Поток камеры закрыт", "info", {"serial_number": self.serial_number})

            self.running = False

            if self.ia is not None:
                try:
                    self.ia.stop()
                except Exception:
                    pass

                try:
                    self.ia.destroy()
                except Exception:
                    pass

                if self.ia is ia:
                    self.ia = None

                self._reset_save_state()

    def force_close(self):
        self.running = False

        if self.ia is not None:
            try:
                self.ia.stop()
            except Exception as e:
                log_event("camera_core.close_stream_force", "Ошибка force stop", "warn", {"error": str(e)})

            try:
                self.ia.destroy()
            except Exception as e:
                log_event("camera_core.close_stream_force", "Ошибка force destroy", "warn", {"error": str(e)})

            self.ia = None

        log_event("camera_core.close_stream_force", "Запрошена принудительная остановка потока", "warn")
        return {"status": "force_stopped"}

    # ---------- сетевые настройки ----------

    def set_advanced(self):
        self.advanced_settings = True
        return {"advanced_network_settings": self.advanced_settings}

    def get_network_settings(self):
        status = self.manager.access_status(self.serial_number)
        if status != 1:
            return None, None, None, None

        if self._cached_network is not None and (time.time() - self._cache_ts) < self._cache_ttl:
            return self._cached_network

        if not self.manager.check():
            return None, None, None, None

        with self._control_lock:
            if self._cached_network is not None and (time.time() - self._cache_ts) < self._cache_ttl:
                return self._cached_network

            ia = None
            try:
                node_map, ia = self.open_node_map()
                if node_map is None:
                    # подробная причина уже в логе open_node_map
                    return None, None, None, None

                ip = int_to_ip(node_map.GevCurrentIPAddress.value)
                mask = int_to_ip(node_map.GevCurrentSubnetMask.value)
                gateway = int_to_ip(node_map.GevCurrentDefaultGateway.value)
                dhcp_enabled = node_map.GevCurrentIPConfigurationDHCP.value

                self._cached_network = (ip, mask, gateway, dhcp_enabled)
                # IP оттуда же — обновим и его кэш
                self._cached_ip = {"ip": ip}
                self._cache_ts = time.time()
                return self._cached_network
            except Exception as e:
                log_event("camera_core.get_network_settings", "Ошибка чтения сетевых настроек", "error",
                          {"serial_number": self.serial_number, **_explain_error(e)})
                return None, None, None, None
            finally:
                if ia is not None:
                    try:
                        ia.destroy()
                    except Exception:
                        pass

    def change_ip(self, ip, mask="", gateway=""):
        node_map, ia = None, None

        log_event("camera_core.change_ip", "Запрошено изменение ip-mask-gateway", "info",
                  {"serial_number": self.serial_number, "ip": ip, "mask": mask, "gateway": gateway, "advanced": self.advanced_settings})

        # инвалидируем кэш до старта — после ребута камеры он точно устарел
        self._cached_ip = None
        self._cached_network = None

        # сериализуем с остальными control-операциями
        with self._control_lock:
            return self._change_ip_locked(ip, mask, gateway)

    def _change_ip_locked(self, ip, mask, gateway):
        node_map, ia = None, None
        try:
            if not self.manager.check():
                log_event("camera_core.change_ip", "не загружен драйвер", "warn")
                return {"ip": "not_driver"}

            if self.running:
                log_event("camera_core.change_ip", "Поток видео не закрыт", "warn")
                return {"ip": "stream_not_closed"}

            old_ip, old_mask, old_gateway, dhcp_enabled = self.get_network_settings()

            if old_ip is None or old_mask is None or old_gateway is None:
                log_event("camera_core.change_ip", "Ip не получен", "warn")
                return {"ip": "ip_not_received"}

            log_event("camera_core.change_ip", "Запрошены старые сетевые настройки", "info",
                      {"serial_number": self.serial_number, "ip": ip, "mask": mask, "gateway": gateway, "advanced": dhcp_enabled})

            if mask == "" and gateway == "":
                self.advanced_settings = False

            if not self.advanced_settings:
                if not mask:
                    mask = old_mask
                if not gateway:
                    gateway = old_gateway

            ip_changed = old_ip != ip
            mask_changed = old_mask != mask
            gateway_changed = old_gateway != gateway
            advanced_changed = mask_changed or gateway_changed

            if not ip_changed and not advanced_changed:
                log_event("camera_core.change_ip", "нет изменений ip-mask-gateway", "warn")
                return {"ip": "no_changes"}

            if ip == gateway:
                log_event("camera_core.change_ip", "ip совпадает с gateway", "warn")
                return {"ip": "gateway==ip"}

            if ip_changed:
                device_busy = ping_device(ip)
                if not device_busy:
                    log_event("camera_core.change_ip", "данный IP занят", "warn")
                    return {"ip": "ip_busy"}

            try:
                node_map, ia = self.open_node_map()
                if node_map is None or ia is None:
                    log_event("camera_core.change_ip", "не доступен node_map", "warn")
                    return {"ip": "node_map_not_available"}

                if dhcp_enabled:
                    node_map.GevCurrentIPConfigurationPersistentIP.value = True
                    node_map.GevCurrentIPConfigurationDHCP.value = False

                if ip_changed:
                    node_map.GevPersistentIPAddress.value = ip_to_int(ip)

                if self.advanced_settings:
                    if mask_changed:
                        node_map.GevPersistentSubnetMask.value = ip_to_int(mask)
                    if gateway_changed:
                        node_map.GevPersistentDefaultGateway.value = ip_to_int(gateway)
                else:
                    log_event("camera_core.change_ip", "mask-gateway нет изменени, т.к. не прожата кнопка", "warn")
                    return {"ip": "mask_gateway_not_changed_advanced_off"}

                time.sleep(1)
                node_map.DeviceReset.execute()

                # после смены IP старая GenTL-запись устарела:
                # сбрасываем "запомненный" handle/интерфейс — следующий scan/connect возьмёт новый
                self.interface_id = None
                self.device_handle = None

                if ip_changed and self.advanced_settings and advanced_changed:
                    log_event("camera_core.change_ip", "Ip mask gateway успешно поменяны", "info")
                    return {"ip": "ip_mask_gateway_changed"}
                elif ip_changed:
                    log_event("camera_core.change_ip", "Ip успешно поменян", "info")
                    return {"ip": "ip_changed"}
                elif self.advanced_settings and advanced_changed:
                    log_event("camera_core.change_ip", "Mask gateway успешно поменяны", "info")
                    return {"ip": "mask_gateway_changed"}

                log_event("camera_core.change_ip", "неизвестная ошибка ip", "warn")
                return {"ip": "unknown"}

            except Exception:
                log_event("camera_core.change_ip", "Ошибка изменения ip-mask-gateway", "warn")
                return {"error": "Ошибка изменения ip-mask-gateway"}
            finally:
                if ia is not None:
                    try:
                        ia.destroy()
                    except Exception as e:
                        log_event("camera_core.change_ip", "Ошибка освобождения nodemap", "warn", {"error": str(e)})
        finally:
            self.advanced_settings = False


class RtspCameraWorker(BaseCameraWorker):
    """IP-камера по RTSP (например, Dahua). Просмотр, запись видео и снимки."""

    def __init__(self, serial_number, manager, rtsp_url):
        super().__init__(serial_number, manager)

        self.rtsp_url = rtsp_url
        self.capture = None
        # последний полученный кадр — для снимка без повторного подключения
        self.last_frame = None

    def _open_capture(self):
        capture = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
        try:
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        return capture

    def _capture_fps(self, capture):
        try:
            fps = capture.get(cv2.CAP_PROP_FPS)
        except Exception:
            fps = 0
        return fps if fps and fps > 0 else DEFAULT_VIDEO_FPS

    # ---------- стрим ----------

    def generate(self, scale=100, target_fps=None):
        capture = None
        last_frame_time = None
        last_emit = 0.0

        # масштаб кадра: 10..100 % от оригинала
        try:
            scale_factor = max(10, min(100, int(scale))) / 100.0
        except (TypeError, ValueError):
            scale_factor = 1.0

        # ограничение частоты кадров (троттлинг по времени)
        min_interval = (1.0 / target_fps) if (target_fps and target_fps > 0) else 0.0

        if self.running:
            log_event("camera_core.rtsp_stream", "Старый RTSP-поток открыт, принудительно закрытие", "warn")
            self.force_close()
            time.sleep(0.3)

        try:
            log_event("camera_core.rtsp_stream", "Запрошен старт RTSP-потока", "info",
                      {"serial_number": self.serial_number, "rtsp_url": self.rtsp_url,
                       "scale": scale, "target_fps": target_fps})

            capture = self._open_capture()
            if not capture.isOpened():
                log_event("camera_core.rtsp_stream", "Не удалось подключиться к RTSP", "error", {"rtsp_url": self.rtsp_url})
                return

            self.capture = capture
            self.running = True
            source_fps = self._capture_fps(capture)
            # fps для записи видео: целевой, если задан, иначе из камеры
            video_fps = target_fps if (target_fps and target_fps > 0) else source_fps

            log_event("camera_core.rtsp_stream", "RTSP-поток запущен", "success",
                      {"serial_number": self.serial_number, "fps": source_fps})

            while self.running:
                ok, raw = capture.read()

                if not ok or raw is None:
                    self.metrics["errors"] += 1
                    if not self.running:
                        break
                    log_event("camera_core.rtsp_stream", "Кадр RTSP не получен", "warn")
                    time.sleep(0.05)
                    continue

                # полноразмерный кадр держим для снимка
                self.last_frame = raw

                now = time.time()
                # троттлинг: пропускаем кадр, если интервал ещё не прошёл
                if min_interval and (now - last_emit) < min_interval:
                    continue
                last_emit = now

                # масштабирование для снижения нагрузки на сеть
                if scale_factor < 1.0:
                    new_w = max(2, int(raw.shape[1] * scale_factor))
                    new_h = max(2, int(raw.shape[0] * scale_factor))
                    img = cv2.resize(raw, (new_w, new_h), interpolation=cv2.INTER_AREA)
                else:
                    img = raw

                ok_jpeg, encoded = cv2.imencode(".jpg", img)
                if not ok_jpeg:
                    self.metrics["errors"] += 1
                    continue
                frame = encoded.tobytes()

                self.metrics["image_number"] += 1
                self.metrics["width"] = img.shape[1]
                self.metrics["height"] = img.shape[0]

                if last_frame_time is not None:
                    dt = now - last_frame_time
                    if dt > 0:
                        self.metrics["fps"] = 1.0 / dt

                if self.metrics["fps"] > 0:
                    self.metrics["bandwidth_mbps"] = (len(frame) * 8 * self.metrics["fps"]) / 1_000_000

                last_frame_time = now

                # автофото + запись видео (тот же механизм, что и у GigE)
                self._maybe_save(img, video_fps)

                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                )

        except Exception as e:
            if not self.running:
                log_event("camera_core.rtsp_stream", "RTSP-поток остановлен", "error", {"error": repr(e)})
            else:
                log_event("camera_core.rtsp_stream", "Ошибка RTSP-потока", "error", {"error": repr(e)})
                self.metrics["errors"] += 1

        finally:
            log_event("camera_core.rtsp_stream", "RTSP-поток закрыт", "info", {"serial_number": self.serial_number})

            self.running = False

            if self.capture is not None:
                try:
                    self.capture.release()
                except Exception:
                    pass

                if self.capture is capture:
                    self.capture = None

            self._reset_save_state()

    # ---------- снимок ----------

    # сохранить отдельный снимок и вернуть его как jpeg
    def snapshot(self):
        img = self.last_frame
        opened = None
        try:
            if img is None:
                opened = self._open_capture()
                if not opened.isOpened():
                    log_event("camera_core.rtsp_snapshot", "Не удалось подключиться к RTSP для снимка", "error", {"rtsp_url": self.rtsp_url})
                    return None
                ok, img = opened.read()
                if not ok or img is None:
                    log_event("camera_core.rtsp_snapshot", "Не удалось получить кадр для снимка", "error")
                    return None

            self.write_photo(img)

            ok_jpeg, encoded = cv2.imencode(".jpg", img)
            if not ok_jpeg:
                log_event("camera_core.rtsp_snapshot", "Ошибка кодирования снимка", "error")
                return None

            log_event("camera_core.rtsp_snapshot", "Снимок сохранён", "success", {"serial_number": self.serial_number})
            return encoded.tobytes()
        finally:
            if opened is not None:
                opened.release()

    def force_close(self):
        self.running = False

        if self.capture is not None:
            try:
                self.capture.release()
            except Exception as e:
                log_event("camera_core.rtsp_force", "Ошибка release RTSP", "warn", {"error": str(e)})

            self.capture = None

        log_event("camera_core.rtsp_force", "Запрошена принудительная остановка RTSP-потока", "warn")
        return {"status": "force_stopped"}


class CameraManager:
    """Управляет драйвером, сканированием сети и реестром камер."""

    def __init__(self):
        self.harvester = Harvester()
        self.driver_loaded = False
        # серийник -> агрегированный лучший статус (для обратной совместимости)
        self.cam_online = {}
        # (serial_number, interface_id) -> {access_status, interface_name, interface_ip, available, ...}
        self.devices = {}
        # серийник -> CameraWorker (GigE Vision)
        self.workers = {}
        # серийник -> RtspCameraWorker (RTSP)
        self.rtsp_workers = {}
        # реестр воркеров общий на все потоки. Под многопоточный режим (несколько
        # камер стримятся одновременно) создание воркера сериализуем, чтобы два
        # запроса по одному серийнику не создали два конкурирующих объекта.
        self._registry_lock = threading.Lock()

    # создать/получить GigE-камеру
    def get(self, serial_number) -> CameraWorker:
        with self._registry_lock:
            if serial_number not in self.workers:
                self.workers[serial_number] = CameraWorker(serial_number, self)
            return self.workers[serial_number]

    # полный сброс Harvester (используется при GenTL -1020 resource exhausted)
    def reset_harvester(self):
        try:
            self.harvester.reset()
        except Exception as e:
            log_event("camera_core.reset_harvester", "Ошибка сброса Harvester", "warn", {"error": str(e)})

        self.harvester = Harvester()
        self.driver_loaded = False
        self.load_driver()
        log_event("camera_core.reset_harvester", "Harvester пересоздан", "info")

    # создать acquirer по серийнику. Подбирает рабочий экземпляр устройства из всех дублей.
    # device_handle — уникальный ключ конкретной записи (приоритет № 1).
    # interface_id  — id GenTL-интерфейса (приоритет № 2, если он различает дубли).
    # Без параметров — просто пробует все доступные подряд.
    def create_acquirer(self, serial_number, interface_id=None, device_handle=None):
        # перечитываем список — даём шанс продюсеру актуализировать дубли
        try:
            self.harvester.update()
        except Exception:
            pass

        devices = self.harvester.device_info_list

        matches = []
        for index, device in enumerate(devices):
            if device.serial_number != serial_number:
                continue
            matches.append({
                "index": index,
                "interface_id": self._interface_id(device),
                "device_handle": self._device_handle(device, index),
                "access_status": self._safe_status(device),
            })

        if not matches:
            raise ValueError(f"устройство не найдено: {serial_number}")

        # приоритеты выбора:
        # 1) запись с конкретным device_handle (если он у нас сохранён);
        # 2) записи на конкретном interface_id (если он реально различает дубли);
        # 3) остальные available;
        # 4) недоступные — как последний шанс.
        preferred = [m for m in matches if device_handle and m["device_handle"] == device_handle]

        if not preferred and interface_id:
            unique_ifaces = {m["interface_id"] for m in matches}
            if len(unique_ifaces) > 1:
                preferred = [m for m in matches if m["interface_id"] == interface_id]

        available = [m for m in matches if m["access_status"] == 1 and m not in preferred]
        fallback = [m for m in matches if m not in preferred and m not in available]
        ordered = preferred + available + fallback

        last_error = None
        tried = []
        for attempt_index, m in enumerate(ordered):
            index = m["index"]
            # перед второй и последующими попытками — короткая пауза,
            # чтобы продюсер успел освободить control-канал после неудачи
            if attempt_index > 0:
                time.sleep(0.15)
            try:
                # защита от гонки: список мог сократиться между update() и create()
                if index >= len(self.harvester.device_info_list):
                    continue
                acquirer = self.harvester.create(index)
                if device_handle and m["device_handle"] != device_handle:
                    log_event("camera_core.create_acquirer",
                              "Выбранная запись не открылась, подключено через резервную",
                              "warn", {"serial_number": serial_number,
                                       "preferred_handle": device_handle,
                                       "used_handle": m["device_handle"]})
                else:
                    log_event("camera_core.create_acquirer", "Подключение к камере открыто", "info",
                              {"serial_number": serial_number,
                               "device_handle": m["device_handle"],
                               "interface_id": m["interface_id"]})
                return acquirer
            except Exception as e:
                last_error = e
                tried.append({"handle": m["device_handle"], "error": _gentl_code(repr(e)) or "n/a"})

        raise last_error if last_error is not None else ValueError(
            f"не удалось открыть устройство: {serial_number} (пробовали: {tried})")

    # ---------- ForceIP (смена IP для камеры в другой подсети) ----------

    # принудительно задать IP через node_map GenTL-интерфейса.
    # Работает, даже когда control-канал не открыть (камера в чужой подсети),
    # т.к. ForceIP идёт широковещательно на уровне интерфейса. IP временный —
    # держится до перезагрузки камеры, но этого достаточно, чтобы она появилась
    # в нашей подсети и дальше уже можно прописать постоянный IP обычным путём.
    def force_ip(self, serial_number, ip, mask=None, gateway=None):
        self.load_driver()
        if not self.driver_loaded:
            return {"ip": "not_driver"}
        try:
            self.harvester.update()
        except Exception:
            pass

        device = next((d for d in self.harvester.device_info_list
                       if getattr(d, "serial_number", None) == serial_number), None)
        if device is None:
            log_event("camera_core.force_ip", "Камера не найдена для ForceIP", "warn",
                      {"serial_number": serial_number})
            return {"ip": "not_found"}

        parent = getattr(device, "parent", None)
        iface = getattr(parent, "node_map", None) if parent is not None else None
        if iface is None:
            log_event("camera_core.force_ip", "Нет доступа к node_map интерфейса", "warn",
                      {"serial_number": serial_number})
            return {"ip": "no_interface"}

        if not mask:
            mask = "255.255.255.0"
        if not gateway:
            gateway = "0.0.0.0"

        try:
            # обновим список устройств на интерфейсе (имя команды у продюсеров разное)
            for cmd in ("DeviceUpdateList", "GevDeviceUpdateList"):
                try:
                    getattr(iface, cmd).execute()
                    break
                except Exception:
                    continue

            if not self._select_iface_device(iface, serial_number):
                log_event("camera_core.force_ip", "Не удалось выбрать камеру на интерфейсе", "warn",
                          {"serial_number": serial_number})
                return {"ip": "device_select_failed"}

            iface.GevDeviceForceIPAddress.value = ip_to_int(ip)
            iface.GevDeviceForceSubnetMask.value = ip_to_int(mask)
            try:
                iface.GevDeviceForceGateway.value = ip_to_int(gateway)
            except Exception:
                pass
            iface.GevDeviceForceIP.execute()

            log_event("camera_core.force_ip", "ForceIP выполнен", "success",
                      {"serial_number": serial_number, "ip": ip, "mask": mask, "gateway": gateway})

            # запись устарела — следующий scan/connect возьмёт новую
            worker = self.workers.get(serial_number)
            if worker is not None:
                worker.interface_id = None
                worker.device_handle = None
            return {"ip": "force_ip_ok", "new_ip": ip}
        except Exception as e:
            log_event("camera_core.force_ip", "Ошибка ForceIP", "error",
                      {"serial_number": serial_number, **_explain_error(e)})
            return {"ip": "force_ip_failed", "error": str(e)}

    # выбрать нужное устройство на интерфейсе по серийнику (через DeviceSelector)
    @staticmethod
    def _select_iface_device(iface, serial_number):
        try:
            selector = iface.DeviceSelector
        except Exception:
            # нет селектора — возможно, ForceIP-ноды относятся к единственному устройству
            return True

        try:
            max_index = int(selector.max)
        except Exception:
            max_index = 0

        for index in range(max_index + 1):
            try:
                selector.value = index
            except Exception:
                continue
            for node_name in ("DeviceSerialNumber", "GevDeviceSerialNumber"):
                try:
                    if str(getattr(iface, node_name).value) == str(serial_number):
                        return True
                except Exception:
                    continue

        # серийник не прочитать, но устройство на интерфейсе одно — выбираем его
        if max_index == 0:
            try:
                selector.value = 0
                return True
            except Exception:
                return False
        return False

    # ---------- перечисление устройств с разбивкой по интерфейсам ----------

    @staticmethod
    def _interface_id(device_info):
        # стабильный GenTL-идентификатор сетевого интерфейса (parent)
        parent = getattr(device_info, "parent", None)
        if parent is None:
            return None
        return getattr(parent, "id_", None) or getattr(parent, "id", None)

    @staticmethod
    def _device_handle(device_info, index):
        # уникальный ключ записи в device_info_list: id_ устройства, либо его суффикс с индексом,
        # если у продюсера id_ не уникален (как у Hikrobot, где у всех 5 копий один id_).
        raw = getattr(device_info, "id_", None) or getattr(device_info, "id", None) or ""
        return f"{raw}#{index}" if raw else f"dev#{index}"

    @staticmethod
    def _interface_name(device_info):
        parent = getattr(device_info, "parent", None)
        if parent is None:
            return None
        return getattr(parent, "display_name", None) or getattr(parent, "model", None)

    @staticmethod
    def _model(device_info):
        # модель камеры (например, MV-CS050-10GC) — показываем её в списке
        return getattr(device_info, "model", None) or None

    @staticmethod
    def _interface_ip(device_info):
        parent = getattr(device_info, "parent", None)
        if parent is None:
            return None
        # 1) GEV-нода интерфейса (если продюсер её предоставляет)
        try:
            value = parent.node_map.GevInterfaceSubnetIPAddress.value
            if value:
                return int_to_ip(int(value))
        except Exception:
            pass
        # 2) fallback — вытаскиваем IPv4 из display_name (часто там "Ethernet [192.168.1.222]")
        try:
            match = re.search(r"(\d+\.\d+\.\d+\.\d+)", parent.display_name or "")
            if match:
                return match.group(1)
        except Exception:
            pass
        return None

    @staticmethod
    def _safe_status(device_info):
        try:
            return int(device_info.access_status)
        except Exception:
            return 0

    # полный список записей устройств (одна запись на пару серийник+интерфейс)
    def list_devices(self):
        self.load_driver()
        result = []
        if not self.driver_loaded:
            return result

        for index, device in enumerate(self.harvester.device_info_list):
            try:
                serial = device.serial_number
            except Exception:
                continue

            status = self._safe_status(device)
            result.append({
                "device_index": index,
                "device_handle": self._device_handle(device, index),
                "serial_number": serial,
                "access_status": status,
                "available": status == 1,
                "model": self._model(device),
                "interface_id": self._interface_id(device),
                "interface_name": self._interface_name(device),
                "interface_ip": self._interface_ip(device),
            })
        return result

    # сгруппированный список: {серийник: [записи по интерфейсам]}
    def list_devices_grouped(self):
        grouped = {"DA123123": [{
            "device_index": -1,
            "device_handle": "mock#DA123123",
            "serial_number": "DA123123",
            "access_status": 1,
            "available": True,
            "model": "Mock-камера",
            "interface_id": "mock",
            "interface_name": "Mock-интерфейс",
            "interface_ip": "192.168.2.10",
        }]}
        for entry in self.list_devices():
            grouped.setdefault(entry["serial_number"], []).append(entry)
        return grouped

    # создать/получить RTSP-камеру (rtsp_url нужен при первом обращении)
    def get_rtsp(self, serial_number, rtsp_url=None):
        with self._registry_lock:
            worker = self.rtsp_workers.get(serial_number)
            if worker is None:
                if not rtsp_url:
                    return None
                worker = RtspCameraWorker(serial_number, self, rtsp_url)
                self.rtsp_workers[serial_number] = worker
            elif rtsp_url:
                worker.rtsp_url = rtsp_url
            return worker

    # статус доступа: для пары (serial, interface_id), либо лучший статус по серийнику
    def access_status(self, serial_number, interface_id=None):
        if interface_id is None:
            return self.cam_online.get(serial_number)

        entry = self.devices.get((serial_number, interface_id))
        if entry is not None:
            return entry["access_status"]

        # запись могла появиться после scan'а: ищем напрямую в device_info_list
        if not self.driver_loaded:
            return None
        for device in self.harvester.device_info_list:
            if device.serial_number == serial_number and self._interface_id(device) == interface_id:
                return self._safe_status(device)
        return None

    # диагностика окружения: версии Python и библиотек + параметры файла .cti.
    # Нужна, чтобы видеть, не сменилась ли версия genicam/harvesters между
    # запусками (типовая причина "раньше работало, теперь нет"). Сам .cti при
    # этом обычно не меняется — сверяем его дату/размер.
    def log_environment(self):
        import platform

        info = {"python": platform.python_version()}
        for name in ("harvesters", "genicam", "numpy", "cv2"):
            try:
                module = __import__(name)
                info[name] = getattr(module, "__version__", "?")
            except Exception:
                info[name] = "n/a"

        cti_path, cti_source = _discover_cti()
        if cti_path is not None:
            try:
                stat = cti_path.stat()
                info["cti"] = str(cti_path)
                info["cti_source"] = cti_source
                info["cti_size"] = stat.st_size
                info["cti_modified"] = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
            except Exception:
                pass

        # runtime продюсера: без него create() падает с -1003, даже если .cti найден
        runtime = _find_mvs_runtime()
        info["mvs_runtime"] = runtime or "НЕ НАЙДЕН (нужен MVS SDK для create() камеры)"

        log_event("camera_core.environment", "Версии окружения (Python/драйвер/библиотеки)", "info", info)
        return info

    # загрузка драйвера для работы
    def load_driver(self):
        try:
            # драйвер уже загружен — повторно .cti не добавляем (иначе производитель
            # регистрируется дублями и устройства задваиваются → "multiple devices found"),
            # только обновляем список устройств
            if self.driver_loaded:
                self.harvester.update()
                return

            cti_path, cti_source = _discover_cti()
            if cti_path is None:
                log_event("camera_core.load_driver", "Драйвер (.cti) не найден ни в MVS, ни в папке программы", "error")
                self.driver_loaded = False
                return

            cti_path = str(cti_path)
            self.harvester.add_file(cti_path)
            self.harvester.update()
            self.driver_loaded = True
            # в лог пишем, откуда взят продюсер и найден ли его runtime — удобно
            # для диагностики самодостаточной поставки (всё из папки Driver/)
            runtime = _find_mvs_runtime()
            log_event("camera_core.load_driver", "Драйвер загружен", "success",
                      {"cti_path": cti_path, "source": cti_source,
                       "mvs_runtime": runtime or "НЕ НАЙДЕН"})
        except Exception as e:
            self.driver_loaded = False
            log_event("camera_core.load_driver", "Ошибка загрузки драйвера", "error", {"error": str(e)})

    # проверка состояния загрузки драйвера
    def check(self):
        if not self.driver_loaded:
            self.load_driver()
            if not self.driver_loaded:
                log_event("camera_core.load_driver", "Ошибка загрузки драйвера", "error")
        return self.driver_loaded

    # сканирование всех сетевых камер.
    # обновляет devices (по парам serial+interface) и cam_online (агрегированный статус)
    def scan_cams(self):
        self.cam_online = {"DA123123": 1}  # пробные данные(потом убрать)
        self.devices = {}

        # load_driver сам обновит список устройств (и при первом вызове добавит .cti)
        self.load_driver()
        if not self.check():
            return self.cam_online

        for entry in self.list_devices():
            serial = entry["serial_number"]
            iface = entry["interface_id"]
            status = entry["access_status"]

            self.devices[(serial, iface)] = entry

            # в cam_online держим лучший статус по серийнику (Ok приоритетнее остальных)
            prev = self.cam_online.get(serial)
            if prev is None or status == 1:
                self.cam_online[serial] = status

            # гарантируем наличие воркера
            self.get(serial)

        return self.cam_online

    def count_cams(self):
        return {"count": len(self.cam_online)}


def ping_device(ip: str) -> bool:
    result = subprocess.run(
        ["ping", "-n", "1", ip],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    log_event("camera_core.change_ip", "пинг до устройства", "info", {"result": result.returncode})
    return result.returncode == 0


# единый объект-менеджер на всё приложение
manager = CameraManager()
