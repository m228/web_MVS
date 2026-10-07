"""camera_core.camera_manager — CameraManager: драйвер, сканирование сети, реестр воркеров; singleton manager.

Часть бывшего camera_core.py (механический перенос, тела определений не менялись).
"""
from datetime import datetime
import re
import threading
import time
from harvesters.core import Harvester
from logger import log_event
import sdk_gige
import save_settings
import rtsp_store

from .gentl_env import (
    _OPEN_FLAKY_RETRIES,
    _discover_cti,
    _explain_error,
    _find_mvs_runtime,
    _gentl_code,
    _preload_runtime_dlls,
    _sdk_gige_warmup
)
from .utils import int_to_ip, ip_to_int
from .gige_worker import CameraWorker
from .rtsp_worker import RtspCameraWorker


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
        # сериализует общий Harvester (load_driver/update): два GigE-стрима стартуют из
        # разных потоков — одновременный update() гонка. RLock: scan_cams зовёт load_driver.
        self._driver_lock = threading.RLock()

    # создать/получить GigE-камеру
    def get(self, serial_number) -> CameraWorker:
        with self._registry_lock:
            if serial_number not in self.workers:
                self.workers[serial_number] = CameraWorker(serial_number, self)
            return self.workers[serial_number]

    # создать acquirer по серийнику. Подбирает рабочий экземпляр устройства из всех дублей.
    # device_handle — уникальный ключ конкретной записи (приоритет № 1).
    # interface_id  — id GenTL-интерфейса (приоритет № 2, если он различает дубли).
    # Без параметров — просто пробует все доступные подряд.
    def create_acquirer(self, serial_number, interface_id=None, device_handle=None):
        # ВАЖНО (регрессия рефактора, коммит 02f71aa): лишний harvester.update()
        # прямо перед create() на долгоживущем Harvester дестабилизирует
        # Hikrobot-продюсер — create() падает с -1006/-1003 и "мусором из памяти"
        # (не-UTF8, decode_error). Рабочая версия делала create() БЕЗ update() здесь.
        # Поэтому переобновляем список ТОЛЬКО если камеры в нём ещё нет (горячее
        # подключение); если камера уже известна после scan — open сразу.
        known = any(getattr(d, "serial_number", None) == serial_number
                    for d in self.harvester.device_info_list)
        if not known:
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
            # ФЛАКИ genicam 1.5.1: продюсер иногда отдаёт не-UTF-8 url_info -> UnicodeDecodeError
            # и коды -1020/-1006/-1003. Ошибка НЕ детерминирована (мусорные байты меняются от раза
            # к разу), поэтому ПОВТОРЯЕМ create() несколько раз — обычно попадаем в «удачный» декод.
            for retry in range(_OPEN_FLAKY_RETRIES + 1):
                try:
                    # защита от гонки: список мог сократиться между update() и create()
                    if index >= len(self.harvester.device_info_list):
                        break
                    acquirer = self.harvester.create(index)
                    if retry > 0:
                        log_event("camera_core.create_acquirer",
                                  "Камера открыта после повтора (флаки genicam-декод)", "info",
                                  {"serial_number": serial_number, "retry": retry})
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
                    code = _gentl_code(repr(e))
                    flaky = isinstance(e, UnicodeDecodeError) or code in (-1020, -1006, -1003)
                    if flaky and retry < _OPEN_FLAKY_RETRIES:
                        time.sleep(0.1)
                        continue               # тот же record — повтор (новый порт, новый декод)
                    tried.append({"handle": m["device_handle"], "error": code or "n/a"})
                    break

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
        grouped = {}
        for entry in self.list_devices():
            grouped.setdefault(entry["serial_number"], []).append(entry)
        return grouped

    # ---------- автоподключение RTSP при старте приложения ----------

    # серийник (ключ воркера и настроек сохранения) для записи из базы RTSP.
    # Приоритет — сохранённый в базе; иначе строим как фронтенд: RTSP-<ip>.
    @staticmethod
    def _saved_rtsp_serial(entry):
        serial = (entry.get("serial") or "").strip()
        if serial:
            return serial
        ip = (entry.get("ip") or "").strip()
        return f"RTSP-{ip}" if ip else "RTSP-custom"

    # поднять камеры с галочкой «автоподключение» и возобновить их автосохранение.
    # Зовётся из lifespan: после перезагрузки ПК съёмка продолжается сама, без браузера.
    def resume_rtsp_autostart(self):
        started = []
        for entry in rtsp_store.load():
            if not entry.get("autostart"):
                continue
            url = entry.get("url")
            if not url:
                continue

            serial = self._saved_rtsp_serial(entry)
            worker = self.get_rtsp(serial, url)
            if worker is None:
                continue
            worker.autostart = True

            # возобновляем автосохранение фото, если его не выключали осознанно
            settings = save_settings.get(serial)
            interval = settings.get("photo_interval")
            if settings.get("photo_autostart") and interval:
                try:
                    worker.on_photo(int(interval), settings.get("photo_project"))
                except Exception as e:
                    log_event("camera_core.rtsp_autostart", "Не удалось возобновить автосохранение фото",
                              "warn", {"serial_number": serial, "error": str(e)})

            worker.ensure_capture(scale=entry.get("scale") or 100, target_fps=entry.get("fps") or None)
            started.append({"serial_number": serial, "photo": worker.save_photo,
                            "project": worker.photo_project})

        if started:
            log_event("camera_core.rtsp_autostart", "Автоподключение сохранённых RTSP-камер", "success",
                      {"count": len(started), "cameras": started})
        return {"started": started}

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

    # убрать RTSP-воркер из реестра (напр. после смены IP: старый serial завязан на
    # старый адрес). Останавливает поток и удаляет объект, чтобы не копить «висящие».
    def drop_rtsp(self, serial_number):
        with self._registry_lock:
            worker = self.rtsp_workers.pop(serial_number, None)
        if worker is not None:
            try:
                worker.force_close()
            except Exception as e:
                log_event("camera_core.rtsp", "Ошибка остановки RTSP при drop", "warn",
                          {"serial_number": serial_number, "error": str(e)})
        return worker is not None

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
      with self._driver_lock:
        try:
            # драйвер уже загружен — повторно .cti не добавляем (иначе производитель
            # регистрируется дублями и устройства задваиваются → "multiple devices found"),
            # только обновляем список устройств
            if self.driver_loaded:
                # ВАЖНО: повторный update() на живом Harvester дестабилизирует
                # Hikrobot-продюсер (та же болезнь, что в create_acquirer: -1006/пустой
                # список). Поэтому update ТОЛЬКО когда камер НЕ видно (надо найти или
                # переподхватить после обрыва) — тогда прогреваем SDK и обновляем. Если
                # камеры уже перечислены — НЕ трогаем, иначе список «мигает» в 0.
                if not self.harvester.device_info_list:
                    self.harvester.update()                 # GenTL enum сперва (как diag)
                    if not self.harvester.device_info_list:  # не нашёл — разбудить NIC через SDK
                        _sdk_gige_warmup()
                        self.harvester.update()
                return

            cti_path, cti_source = _discover_cti()
            if cti_path is None:
                log_event("camera_core.load_driver", "Драйвер (.cti) не найден ни в MVS, ни в папке программы", "error")
                self.driver_loaded = False
                return

            cti_path = str(cti_path)
            self.harvester.add_file(cti_path)
            # явная загрузка runtime-DLL продюсера (как diag) — иначе enum GigE даёт 0
            _preload_runtime_dlls()
            # GenTL enum ПЕРВЫМ — ровно как diag STAGE 1 (свежий Harvester → update находит
            # камеру). ВАЖНО: _sdk_gige_warmup (MVS SDK EnumDevices) до GenTL-update
            # инициализирует GigE-стек так, что harvester перечисляет 0 (diag без него находит).
            # Поэтому SDK-прогрев теперь ПОСЛЕ и только если GenTL никого не нашёл.
            self.harvester.update()
            if not self.harvester.device_info_list:
                _sdk_gige_warmup()
                self.harvester.update()
            else:
                # harvester нашёл камеру — но SDK всё равно инициализируем (ПОСЛЕ update,
                # чтобы не сбить harvester-enum): стрим пойдёт нативным SDK-путём (как MVS),
                # в обход genicam с его −1020/−1006. Без этого SDK молчал, если harvester
                # видел камеру, и всё падало в harvesters+genicam.
                _sdk_gige_warmup()
            self.driver_loaded = True
            log_event("camera_core.load_driver", "MVS SDK для стрима: %s" %
                      ("доступен" if sdk_gige.available() else "НЕ доступен (стрим пойдёт через harvesters)"),
                      "info" if sdk_gige.available() else "warn",
                      {"sdk_available": sdk_gige.available()})
            # в лог пишем, откуда взят продюсер и найден ли его runtime — удобно
            # для диагностики самодостаточной поставки (всё из папки Driver/)
            runtime = _find_mvs_runtime()
            # число перечисленных устройств кладём в лог: главный маркер той самой
            # проблемы (продюсер грузится, но 0 камер → выбран неполный бандл вместо MVS)
            try:
                device_count = len(self.harvester.device_info_list)
            except Exception:
                device_count = None
            log_event("camera_core.load_driver", "Драйвер загружен", "success",
                      {"cti_path": cti_path, "source": cti_source,
                       "mvs_runtime": runtime or "НЕ НАЙДЕН",
                       "device_count": device_count,
                       "thread": threading.current_thread().name})
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
        self.cam_online = {}
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


# единый объект-менеджер на всё приложение
manager = CameraManager()
