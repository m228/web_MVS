"""camera_core.gige_worker — CameraWorker: GigE Vision камера (MVS SDK / Harvesters).

Часть бывшего camera_core.py (механический перенос, тела определений не менялись).
"""
import threading
import time
import numpy as np
import cv2
from logger import log_event
import sdk_gige

from .gentl_env import (
    FRAME_FETCH_TIMEOUT,
    GENTL_HINTS,
    MAX_FRAME_TIMEOUTS,
    SDK_GRAB_TIMEOUT_MS,
    SDK_STREAM_STALL_SECONDS,
    _explain_error,
    _gentl_code,
    _sdk_device_info,
    _sdk_invalidate_device,
    _sdk_ip
)
from .utils import _format_mac, int_to_ip, ip_to_int, ping_device
from .imaging import _apply_color, _to_bgr
from .base_worker import BaseCameraWorker


class CameraWorker(BaseCameraWorker):
    """Промышленная GigE Vision камера (Harvester/GenICam)."""

    def __init__(self, serial_number, manager):
        super().__init__(serial_number, manager)

        self.ia = None
        # сеанс SDK-стрима (GigE через MvCameraControl.dll с resend), если активен
        self._sdk_stream = None
        # защищает жизненный цикл self.ia: старт потока в generate() и
        # остановку в force_close() могут дёргать разные потоки uvicorn
        # (/stream и /force_close). Без него force_close мог уничтожить
        # acquirer между `self.ia = ia` и `ia.start()`.
        self._ia_lock = threading.Lock()
        # запросы стрима (/api/camera/stream) идут строго по очереди: страница может прислать два подряд
        # (двойной connect). Раньше второй не открывал камеру (занята первым), но в finally выставлял
        # running=False и убивал уже идущий поток первого — картинка замирала, кадров не было до ручного перезапуска.
        self._start_lock = threading.Lock()
        # номер сеанса стрима: новый запрос вытесняет прежний; вытесненный не трогает общее состояние (running и т.д.)
        self._session = 0
        # метка страницы, чей запрос стрима сейчас владеет камерой (параметр «_» запроса): по ней страница узнаёт в метриках,
        # что поток перехватило другое окно (иначе в проигравшем окне картинка чёрная, а телеметрия «кадры идут»)
        self.client_id = None
        # лимиты/текущие настройки камеры (заполняется при подключении)
        self.data_limit = None
        # фактический конфиг, с которым камера запущена в последний раз
        # (читается с камеры после применения настроек) — для значка «инфо»
        self.current_config = None
        self.advanced_settings = False

        # GenTL ID сетевого интерфейса (вспомогательный фильтр)
        self.interface_id = None
        # уникальный ключ конкретной записи в device_info_list — основной критерий выбора
        self.device_handle = None

        # сериализация одновременных control-операций к одной камере
        # (без него Promise.all из фронта открывает 5 acquirer'ов на один control-канал
        # и они дерутся за -1005 AccessDenied)
        # RLock (не Lock): change_ip держит лок и внутри зовёт get_network_settings,
        # который берёт этот же лок повторно — с нереентрантным Lock это дедлок.
        self._control_lock = threading.RLock()

        # кэш последних "сетевых"/info данных, чтобы не дёргать control повторно.
        # Каждый control-probe = полный GigE open/close (heartbeat+stream-канал);
        # частые опросы (UI ~10 с) на коротком TTL плодили GVCP-таймауты между коннектами.
        self._cached_ip = None              # {"ip": "..."} | None
        self._cached_network = None         # (ip, mask, gateway, dhcp) | None
        self._cached_info = None            # {"items": [...]} | None
        self._cache_ts = 0.0
        self._cache_ttl = 30.0              # сек — кэш живёт между refresh-цикл UI

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
        self._cached_info = None
        log_event("camera_core.select_interface", "Выбрана запись камеры", "info",
                  {"serial_number": self.serial_number,
                   "interface_id": self.interface_id, "device_handle": self.device_handle})
        return {"status": "ok",
                "interface_id": self.interface_id, "device_handle": self.device_handle}

    # ---------- доступ к камере / nodemap ----------

    # подключение к камере и получение nodemap.
    # interface_id/device_handle можно передать разово (read-запросы дают их как
    # параметр запроса) — тогда общее состояние воркера не мутируется и параллельные
    # запросы не перетирают выбор друг друга. Без явных значений берём то, что
    # зафиксировал select_interface (используется потоком generate).
    def open_node_map(self, interface_id=None, device_handle=None):
        iid = interface_id if interface_id is not None else self.interface_id
        handle = device_handle if device_handle is not None else self.device_handle

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
                interface_id=iid,
                device_handle=handle,
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
                       "interface_id": iid,
                       "device_handle": handle,
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
            # step (inc) у смещений нужен для ROI мышкой: камера принимает только
            # значения, кратные шагу, иначе запись узла отбрасывается или округляется
            "offset_x": {
                "value": node_map.OffsetX.value,
                "min": node_map.OffsetX.min,
                "max": node_map.OffsetX.max,
                "step": node_map.OffsetX.inc,
            },
            "offset_y": {
                "value": node_map.OffsetY.value,
                "min": node_map.OffsetY.min,
                "max": node_map.OffsetY.max,
                "step": node_map.OffsetY.inc,
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
        # полный размер сенсора: Width.max/Height.max сужаются текущим смещением,
        # а для ROI мышкой и кнопки «сбросить ROI» нужен именно предел матрицы
        try:
            data["sensor"] = {
                "width_max": int(node_map.WidthMax.value),
                "height_max": int(node_map.HeightMax.value),
            }
        except Exception:
            pass

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

    # получение айпи камеры по серийнику.
    # interface_id/device_handle — разовые (из параметров запроса), состояние не мутируем
    def sdk_read_data_limit(self):
        """Заполнить self.data_limit по SDK (диапазоны/форматы, БЕЗ genicam), если ещё пусто
        и не идёт стрим. Даёт вкладке «Камера» ранги и список форматов в обход −1020/−1006."""
        if self.data_limit or self.running:
            return self.data_limit
        try:
            info = _sdk_device_info(self.serial_number)
            if sdk_gige.available() and info is not None:
                d = sdk_gige.read_ranges(info)
                if d:
                    self.data_limit = d
                    log_event("camera_core.data_limit", "Параметры камеры прочитаны по SDK (без genicam)",
                              "info", {"serial_number": self.serial_number})
        except Exception as e:
            log_event("camera_core.data_limit", "SDK-чтение параметров не удалось", "warn", {"error": str(e)})
        return self.data_limit

    def get_ip(self, interface_id=None, device_handle=None):
        # СНАЧАЛА — IP из SDK-enum (без открытия камеры и БЕЗ genicam): убирает стартовый
        # −1020 при чтении IP, когда SDK-путь доступен.
        sdk_ip = _sdk_ip(self.serial_number)
        if sdk_ip:
            self._cached_ip = {"ip": sdk_ip}
            self._cache_ts = time.time()
            return self._cached_ip

        status = self.manager.access_status(self.serial_number)
        if status != 1:
            log_event("camera_core.get_ip", "Ошибка получения ip камеры", "error", {"status_camera": str(status)})
            return None

        # кэш — если параллельно/недавно уже спрашивали, возвращаем без открытия control
        if self._cached_ip is not None and (time.time() - self._cache_ts) < self._cache_ttl:
            return self._cached_ip

        # идёт видеопоток — НЕ открываем второй control-канал (иначе GVCP-коллизия/таймаут):
        # отдаём последний известный IP, даже если кэш формально протух
        if self.running:
            return self._cached_ip

        if not self.manager.check():
            return None

        # control-операция — сериализуем (см. self._control_lock)
        with self._control_lock:
            # пока ждали лок, кто-то другой мог уже получить ответ — используем его
            if self._cached_ip is not None and (time.time() - self._cache_ts) < self._cache_ttl:
                return self._cached_ip
            if self.running:                     # стрим стартовал, пока ждали лок
                return self._cached_ip

            ia = None
            try:
                node_map, ia = self.open_node_map(interface_id, device_handle)
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
    # Кэшируем (TTL) и НЕ открываем control во время стрима — чтобы повторные
    # запросы страницы не плодили open/close камеры (GVCP-таймауты между коннектами).
    def get_info(self, interface_id=None, device_handle=None):
        # свежий кэш — отдаём без открытия control
        if self._cached_info is not None and (time.time() - self._cache_ts) < self._cache_ttl:
            return self._cached_info
        # идёт видеопоток — второй control-канал не открываем, отдаём что есть
        if self.running:
            return self._cached_info

        status = self.manager.access_status(self.serial_number)
        if status != 1:
            log_event("camera_core.get_info", "Камера недоступна для запроса информации", "warn",
                      {"serial_number": self.serial_number, "status_camera": str(status)})
            return None

        if not self.manager.check():
            return None

        # control-операция — сериализуем (один control-канал на камеру)
        with self._control_lock:
            # пока ждали лок — кэш мог заполниться, или стартовал стрим
            if self._cached_info is not None and (time.time() - self._cache_ts) < self._cache_ttl:
                return self._cached_info
            if self.running:
                return self._cached_info
            ia = None
            try:
                node_map, ia = self.open_node_map(interface_id, device_handle)
                if node_map is None:
                    return None
                self._cached_info = {"items": self._collect_info(node_map)}
                self._cache_ts = time.time()
                return self._cached_info
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
                # как в рабочей до-multicam версии (02f71aa): просто пишем
                # AcquisitionFrameRate, НЕ включаем AcquisitionFrameRateEnable.
                # Принудительное включение rate-control было единственным
                # всегда-исполняемым отличием от рабочего потока — убрано.
                # float, а не int: узел дробный, а нижняя граница check_value = 0.1 —
                # int() рубил бы дробный fps (0.5 -> 0, что вырубает поток).
                node_map.AcquisitionFrameRate.value = float(fps)

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
                try:
                    pixel_format = node_map.PixelFormat.value
                except Exception:
                    pixel_format = None
                img = _to_bgr(data, real_width, real_height, pixel_format)

                if img is None:
                    log_event("camera_core.get_frame", "Не удалось разобрать кадр (формат пикселей)", "warn",
                              {"serial_number": self.serial_number,
                               "size": int(np.asarray(data).size),
                               "width": real_width, "height": real_height,
                               "hint": "выберите подходящий пиксельный формат (RGB/Mono)"})
                    return None, None

                if self.color:
                    img = _apply_color(img, self.color)

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

    # фактические значения, прочитанные с камеры (для значка «инфо»).
    # Отсутствующие узлы тихо пропускаем.
    @staticmethod
    def _read_current_config(node_map):
        def rv(name, cast=None):
            try:
                value = getattr(node_map, name).value
                return cast(value) if cast is not None else value
            except Exception:
                return None

        config = {
            "width": rv("Width", int),
            "height": rv("Height", int),
            "offset_x": rv("OffsetX", int),
            "offset_y": rv("OffsetY", int),
            "exposure_auto": rv("ExposureAuto"),
            "exposure_time": rv("ExposureTime", lambda v: int(float(v))),
            "pixel_format": rv("PixelFormat"),
            "fps": rv("AcquisitionFrameRate", lambda v: round(float(v), 2)),
        }
        return {k: v for k, v in config.items() if v is not None}

    def generate(self, width=None, height=None, offset_x=None, offset_y=None,
                 fps=None, exposure_auto=None, exposure_time=None, pixel_format=None, client_id=None):
        ia = None
        last_frame_time = None

        if not self.manager.check():
            return

        self._start_lock.acquire()           # старт сеанса — по очереди; SDK-путь отпускает лок сам, как только камера открыта
        try:
            self._session += 1
            token = self._session
            self.client_id = client_id
            if self.running:
                log_event("camera_core.generate_stream", "Старый поток открыт, принудительно закрытие", "warn")
                self.force_close()

            # ждём, пока прошлый сеанс полностью отпустит камеру (его генератор закрывает
            # поток на своём потоке за ~SDK_GRAB_TIMEOUT_MS), иначе новый open упрётся в «занято»
            _deadline = time.time() + 3.0
            while time.time() < _deadline and (self._sdk_stream is not None or self.ia is not None):
                time.sleep(0.1)

            # GigE через MVS SDK (resend) — надёжнее harvesters на нагруженной сети И в обход
            # genicam-декод-бага (−1020/−1006). Если SDK доступен и по серийнику есть device_info —
            # идём этим путём (как MVS). Иначе — harvesters (с ретраем на флаки-декод).
            device_info = _sdk_device_info(self.serial_number)
            sdk_ok = sdk_gige.available() and device_info is not None
            log_event("camera_core.generate_stream",
                      "Путь стрима: %s" % ("SDK (нативный, как MVS)" if sdk_ok else "harvesters+genicam (SDK недоступен)"),
                      "info" if sdk_ok else "warn",
                      {"serial_number": self.serial_number, "sdk": sdk_gige.available(),
                       "device_info": device_info is not None})
        except BaseException:
            self._start_lock.release()
            raise
        if sdk_ok:
            settings = {
                "width": width, "height": height,
                "offset_x": offset_x, "offset_y": offset_y,
                "fps": fps, "exposure_auto": exposure_auto,
                "exposure_time": exposure_time, "pixel_format": pixel_format,
            }
            yield from self._generate_sdk(device_info, settings, token)      # лок отпустит сам
            return
        self._start_lock.release()           # harvesters-путь: прежнее поведение (лок только на предстартовые проверки)

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

            # запоминаем фактический конфиг (читаем с камеры ПОСЛЕ применения,
            # пока держим node_map) — для значка «инфо» на странице камеры
            self.current_config = self._read_current_config(node_map)

            # num_buffers НЕ трогаем — оставляем дефолт harvesters, как было до
            # multicam-рефактора (тогда поток одиночной камеры был стабилен).
            # Принудительные 24 буфера (≈360 МБ на 5 МП RGB8) дестабилизировали
            # продюсер Hikrobot: проходило ~5 кадров, дальше сплошные таймауты -1011
            # ("хватает на 5 кадров и потом падает"). Несколько GigE одновременно мы
            # больше не запускаем (ограничение в UI), поэтому раздувать пул не нужно.

            # выставление self.ia и старт — под локом, чтобы параллельный
            # force_close не уничтожил acquirer между присваиванием и start()
            with self._ia_lock:
                self.ia = ia
                self.running = True
                ia.start()
            log_event("camera_core.generate_stream", "Поток камеры запущен", "success",
                      {"serial_number": self.serial_number, "num_buffers": getattr(ia, "num_buffers", None)})

            # метрики считаем по факту текущего сеанса (стартовый прогрев не копим)
            self.metrics["errors"] = 0
            self.metrics["image_number"] = 0
            self.metrics["fps"] = 0.0
            self.metrics["bandwidth_mbps"] = 0.0

            # простая логика таймаутов как до multicam (02f71aa "6.7"): считаем
            # подряд идущие пропуски кадра, выходим после MAX_FRAME_TIMEOUTS.
            timeouts_in_a_row = 0

            while self.running:
                try:
                    img, frame = self.get_frame(ia, node_map)

                    if frame is None or img is None:
                        # -1011 / пустой кадр — это НЕ ошибка приложения, а нормальный
                        # пропуск/недокадр (на низком FPS между кадрами и при потере
                        # пакетов их много). В "errors" их не считаем, иначе счётчик
                        # сыпет по 20/сек при 1 fps. Копим только для логики выхода.
                        timeouts_in_a_row += 1
                        if timeouts_in_a_row >= MAX_FRAME_TIMEOUTS:
                            log_event("camera_core.generate_stream",
                                      "Поток прерван: подряд слишком много таймаутов получения кадра", "error",
                                      {"serial_number": self.serial_number,
                                       "timeouts": timeouts_in_a_row,
                                       "hint": GENTL_HINTS[-1011]})
                            break
                        continue

                    timeouts_in_a_row = 0

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
                    # настоящая ошибка потока (не -1011) — вот её и считаем
                    self.metrics["errors"] += 1
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

            # уничтожаем ЛОКАЛЬНЫЙ acquirer этого сеанса (ia), а не self.ia: при раннем
            # выходе (провал apply_settings/open_node_map) self.ia ещё не выставлен, и
            # опора на него утекала бы acquirer. destroy в try — если параллельный
            # force_close уже уничтожил тот же объект, повторный вызов молча пройдёт.
            if ia is not None:
                try:
                    ia.stop()
                except Exception:
                    pass

                try:
                    ia.destroy()
                except Exception:
                    pass

            # обнуляем ссылку, только если это всё ещё наш acquirer (не мешаем force_close)
            with self._ia_lock:
                if self.ia is ia:
                    self.ia = None

            self._reset_save_state()

    # GigE-поток через MVS SDK (resend): применяет settings, отдаёт MJPEG теми же чанками,
    # что и harvesters-путь.
    def _generate_sdk(self, device_info, settings=None, token=None):
        stream = None
        lock_held = True                   # _start_lock взят в generate(); отпускаем, как только камера открыта (или не открылась)

        def _unlock():
            nonlocal lock_held
            if lock_held:
                lock_held = False
                self._start_lock.release()
        last_frame_time = None
        last_frame_wall = time.time()  # для отсчёта простоя ПО ВРЕМЕНИ
        fps = (settings or {}).get("fps")
        try:
            # ретрай открытия: сразу после остановки прошлого стрима камера ~секунду
            # ещё «занята» (control-канал отпускается по heartbeat) — не сдаёмся с первого раза
            open_err = None
            for attempt in range(6):
                try:
                    stream = sdk_gige.GigeSdkStream(device_info)
                    stream.open(settings)
                    open_err = None
                    break
                except Exception as e:
                    open_err = e
                    stream = None
                    time.sleep(0.5)
            if stream is None:
                raise RuntimeError("open не удался после ретраев: %r" % open_err)

            with self._ia_lock:
                self._sdk_stream = stream
                self.running = True
            _unlock()
            log_event("camera_core.generate_stream", "Поток камеры запущен (SDK)", "success",
                      {"serial_number": self.serial_number})

            self.metrics["errors"] = 0
            self.metrics["image_number"] = 0
            self.metrics["fps"] = 0.0
            self.metrics["bandwidth_mbps"] = 0.0

            while self.running and self._session == token:      # вытеснил новый запрос стрима — выходим, не трогая общее состояние
                res = stream.grab(timeout_ms=SDK_GRAB_TIMEOUT_MS)
                if res is None:
                    if not self.running or self._session != token:
                        break
                    # нет кадра — норм на низком FPS; рвём поток только если тишина
                    # дольше SDK_STREAM_STALL_SECONDS (реальная потеря связи)
                    if time.time() - last_frame_wall >= SDK_STREAM_STALL_SECONDS:
                        log_event("camera_core.generate_stream",
                                  "Поток прерван: нет кадров дольше таймаута (SDK)", "error",
                                  {"serial_number": self.serial_number, "hint": GENTL_HINTS[-1011]})
                        break
                    continue
                last_frame_wall = time.time()

                width, height, pixel_format, raw = res
                img = _to_bgr(raw, width, height, pixel_format)
                if img is None:
                    self.metrics["errors"] += 1
                    continue
                self._last_bgr = img               # сырой кадр (до цветокоррекции) — для автофокуса
                if self.color:
                    img = _apply_color(img, self.color)
                ok, encoded = cv2.imencode(".jpg", img)
                if not ok:
                    self.metrics["errors"] += 1
                    continue
                frame = encoded.tobytes()

                now = time.time()
                self.metrics["image_number"] += 1
                self.metrics["width"] = img.shape[1]
                self.metrics["height"] = img.shape[0]
                if last_frame_time is not None:
                    dt = now - last_frame_time
                    if dt > 0:
                        self.metrics["fps"] = 1.0 / dt
                if self.metrics["fps"] > 0:
                    # СЫРОЙ кадр (raw) = реальные байты на линке (GVSP), а не сжатый JPEG
                    # в браузер. Так «Скорость потока» отражает загрузку канала камера↔ПК
                    # (видно, когда упираемся в 100/1000 Мбит/с), а не крохи JPEG.
                    self.metrics["bandwidth_mbps"] = (raw.size * 8 * self.metrics["fps"]) / 1_000_000
                last_frame_time = now

                self._maybe_save(img, fps)

                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                )
        except Exception as e:
            # логируем ВСЕГДА, даже если running ещё не выставлен (падение на open):
            # именно это давало «поток закрыт без ошибки» на 2-м запуске
            self.metrics["errors"] += 1
            log_event("camera_core.generate_stream", "Ошибка GigE-потока (SDK)", "error",
                      {"serial_number": self.serial_number, "error": repr(e),
                       "running": self.running})
        finally:
            log_event("camera_core.generate_stream", "Поток камеры закрыт (SDK)", "info",
                      {"serial_number": self.serial_number})
            mine = self._session == token       # False — нас вытеснил новый запрос: running и сохранение уже его
            if mine:
                self.running = False
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            with self._ia_lock:
                if self._sdk_stream is stream:
                    self._sdk_stream = None
            if mine:
                self._reset_save_state()
            _unlock()

    def force_close(self):
        # под тем же локом, что и старт в generate: снимаем acquirer атомарно,
        # чтобы не разойтись с `self.ia = ia; ia.start()`
        with self._ia_lock:
            self.running = False
            ia = self.ia
            self.ia = None

        if ia is not None:
            try:
                ia.stop()
            except Exception as e:
                log_event("camera_core.close_stream_force", "Ошибка force stop", "warn", {"error": str(e)})

            try:
                ia.destroy()
            except Exception as e:
                log_event("camera_core.close_stream_force", "Ошибка force destroy", "warn", {"error": str(e)})

        # SDK-поток отсюда НЕ закрываем: его закроет свой генератор (на своём потоке, по
        # running). Рушить handle из чужого потока, пока тот в GetImageBuffer, ломает SDK.

        log_event("camera_core.close_stream_force", "Запрошена принудительная остановка потока", "warn")
        return {"status": "force_stopped"}

    # ---------- сетевые настройки ----------

    def set_advanced(self):
        self.advanced_settings = True
        return {"advanced_network_settings": self.advanced_settings}

    def get_network_settings(self, interface_id=None, device_handle=None):
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
                node_map, ia = self.open_node_map(interface_id, device_handle)
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
        self._cached_info = None

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
                # ping отвечает → по новому IP уже кто-то есть → адрес занят, менять нельзя
                if ping_device(ip):
                    log_event("camera_core.change_ip", "данный IP уже занят другим устройством", "warn")
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

                # маску/шлюз пишем только в расширенном режиме; но выходить здесь нельзя —
                # иначе при смене только IP пропускается DeviceReset ниже, и новый IP
                # не применяется к камере (persistent-IP подхватывается лишь после ресета)
                if self.advanced_settings:
                    if mask_changed:
                        node_map.GevPersistentSubnetMask.value = ip_to_int(mask)
                    if gateway_changed:
                        node_map.GevPersistentDefaultGateway.value = ip_to_int(gateway)

                time.sleep(1)
                node_map.DeviceReset.execute()

                # после смены IP старая GenTL-запись устарела:
                # сбрасываем "запомненный" handle/интерфейс — следующий scan/connect возьмёт новый
                self.interface_id = None
                self.device_handle = None
                # и SDK-кэш device_info: там остался старый IP, иначе следующий SDK-стрим
                # уйдёт открывать камеру по прежнему адресу и упрётся в 0x80000206
                _sdk_invalidate_device(self.serial_number)

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

            except Exception as e:
                # тип и текст ошибки + расшифровку кода GenTL кладём в payload:
                # без них в логе только строка, и причину (напр. -1005 занято) не видно
                log_event("camera_core.change_ip", "Ошибка изменения ip-mask-gateway", "warn",
                          {"serial_number": self.serial_number, **_explain_error(e)})
                return {"error": "Ошибка изменения ip-mask-gateway"}
            finally:
                if ia is not None:
                    try:
                        ia.destroy()
                    except Exception as e:
                        log_event("camera_core.change_ip", "Ошибка освобождения nodemap", "warn", {"error": str(e)})
        finally:
            self.advanced_settings = False
