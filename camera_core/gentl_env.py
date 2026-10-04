"""camera_core.gentl_env — драйвер GenTL/MVS: совместимость genicam, расшифровка ошибок, поиск .cti и runtime, кэш SDK-устройств.

Часть бывшего camera_core.py (механический перенос, тела определений не менялись).
"""
# как в оригинале: harvesters.core импортировался ДО патча genicam (_patch_genicam_register_event)
from harvesters.core import Harvester  # noqa: F401
import ctypes
import os
import re
import threading
from pathlib import Path
from logger import log_event
from paths import BUNDLE_DIR, DATA_DIR
import sdk_gige


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

# Сколько раз ПОВТОРИТЬ create() при флаки-ошибке открытия (genicam 1.5.1 иногда не может
# декодировать не-UTF-8 url_info продюсера -> UnicodeDecodeError/-1006/-1020). Декод
# недетерминирован, повтор обычно попадает в удачный. Настоящее лечение — выровнять версии.
# По логам с живой камеры удачный декод редок (~единицы %), поэтому повторов много:
# каждый create() — новый порт (новые «мусорные» байты), рано или поздно попадаем в валидный.
_OPEN_FLAKY_RETRIES = 20

# таймаут на один кадр (сек) и сколько таймаутов подряд можно стерпеть до выхода.
# Значения с запасом: камера долго «раскачивается» на старте (особенно 5 МП и при
# 2 камерах), а -1011 между кадрами на низком FPS — норма. Меньшие значения рвут
# поток до того, как камера прогрелась → она «ложится».
FRAME_FETCH_TIMEOUT = 10.0
MAX_FRAME_TIMEOUTS = 60

# SDK-путь GigE: короткий таймаут одной попытки захвата — цикл часто проверяет running и
# быстро отпускает камеру при стопе. Простой считаем ПО ВРЕМЕНИ (на низком FPS пустых
# опросов много — это норма), а не по числу попыток.
SDK_GRAB_TIMEOUT_MS = 300
SDK_STREAM_STALL_SECONDS = 20.0

# ПРИМЕЧАНИЕ про буферы приёма (num_buffers): НЕ переопределяем — оставляем дефолт
# harvesters. Раздувание пула до 24 буферов (≈360 МБ для 5 МП RGB8) дестабилизировало
# продюсер Hikrobot на одиночной камере (~5 кадров, затем сплошные -1011). Несколько
# GigE одновременно запрещены в UI, поэтому большой пул не нужен. См. bug.txt, кейс №2.


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


# RTSP поверх TCP — стабильнее, меньше «рассыпающихся» кадров.
#
# ВАЖНО про таймауты: у OpenCV свой interrupt-колбэк на FFmpeg с ЖЁСТКИМИ 30 с на
# открытие и на чтение. Проверено на «молчащем» порту (TCP принимает, RTSP не отвечает):
# ни timeout/stimeout/rw_timeout в этих опциях, ни OPENCV_FFMPEG_OPEN_TIMEOUT/
# _READ_TIMEOUT ничего не меняют — open/read всё равно возвращаются ровно через 30 с.
# Поэтому сократить саму блокировку нельзя, и обрыв ЗАМЕЧАЕТ отдельный сторож
# (_watchdog_loop): он не сидит в read() и сообщает о тишине через RTSP_STALL_SECONDS.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

# Путь до директории программы и имена файлов драйвера. Поставка самодостаточна:
# и продюсер (.cti), и его runtime (MvCameraControl.dll) лежат в папке Driver/.
# каталог с кодом/ассетами и драйвером (рядом с exe в собранном бандле); см. paths.py
PROGRAM_DIR = BUNDLE_DIR
CTI_FILENAME = "MvProducerGEV.cti"
MVS_RUNTIME_DLL = "MvCameraControl.dll"

# Папки установки MVS SDK с GenTL-продюсером и его runtime. env GENICAM_GENTL64_PATH есть
# не всегда (exe от Explorer/UAC её не наследует), поэтому ищем системный MVS и по путям.
# Common Files — первым: актуальные версии кладут ВЕСЬ runtime (.cti + genicam + DLL) туда,
# туда же смотрит env; зависимости продюсера резолвятся из одного места.
MVS_GENTL_DIRS = [
    r"C:\Program Files (x86)\Common Files\MVS\Runtime\Win64_x64",
    r"C:\Program Files\Common Files\MVS\Runtime\Win64_x64",
    r"C:\Program Files (x86)\MVS\Runtime\Win64_x64",
    r"C:\Program Files\MVS\Runtime\Win64_x64",
    r"C:\Program Files (x86)\MVS\Runtime\Win64",
    r"C:\Program Files\MVS\Runtime\Win64",
    r"C:\Program Files (x86)\MVS\Development\GenTL\Win64_x64",
    r"C:\Program Files\MVS\Development\GenTL\Win64_x64",
    r"C:\Program Files (x86)\MVS\Development\GenTL\Win64",
    r"C:\Program Files\MVS\Development\GenTL\Win64",
]


# самодостаточен ли bundled-продюсер: рядом с .cti должен лежать ВЕСЬ его runtime,
# а не только MvCameraControl.dll. Ключевая зависимость — genicam-рантайм
# GenApi_*_MV.dll (на её отсутствие прямо ругается сборка PyInstaller: "could not
# resolve GenApi_MD_VC120_v3_0_MV.dll"). Имя версионное (VC120/VC140, v3_0/...),
# поэтому проверяем по маске. Без полного runtime продюсер ГРУЗИТСЯ, но GigE-камеры
# не перечисляет — список устройств пуст (это и ловили: bundled → 0 камер).
def _bundle_has_full_runtime(cti_dir):
    if not (cti_dir / MVS_RUNTIME_DLL).is_file():
        return False
    return next(cti_dir.glob("GenApi_*_MV.dll"), None) is not None


# Поиск GenTL-продюсера: явный путь (MVS_CTI_PATH) -> САМОДОСТАТОЧНЫЙ бандл (Driver/
# со всем runtime) -> системный MVS (env GENICAM_GENTL64_PATH ИЛИ стандартные папки
# установки MVS_GENTL_DIRS) -> неполный бандл как последний шанс. Порядок важен:
# неполный бандл перечисляет 0 камер, поэтому при нехватке его runtime сперва пробуем
# полноценный системный MVS. Системный MVS ищем и по env, и по фиксированным путям —
# env есть не всегда (exe от Explorer + UAC её не наследует, а из PyCharm видна).
def _discover_cti():
    explicit = os.environ.get("MVS_CTI_PATH")
    if explicit and Path(explicit).is_file():
        return Path(explicit), "env:MVS_CTI_PATH"

    # бандл берём первым, ТОЛЬКО если он самодостаточен (весь runtime рядом с .cti)
    bundled = next(PROGRAM_DIR.rglob(CTI_FILENAME), None)
    if bundled is not None and _bundle_has_full_runtime(bundled.parent):
        return bundled, "bundled"

    for raw in (os.environ.get("GENICAM_GENTL64_PATH") or "").split(os.pathsep):
        directory = Path(raw) if raw else None
        if directory and directory.is_dir():
            hit = next(directory.glob(CTI_FILENAME), None)
            if hit:
                return hit, "env:GENICAM_GENTL64_PATH"

    # env не задана (типично для exe) — ищем системный MVS по стандартным папкам установки
    for raw in MVS_GENTL_DIRS:
        directory = Path(raw)
        if directory.is_dir():
            hit = next(directory.glob(CTI_FILENAME), None)
            if hit:
                return hit, "mvs_install_dir"

    # системного MVS нет — как последний шанс берём неполный бандл (вдруг зависимости
    # найдутся в PATH); всё равно лучше, чем совсем без продюсера
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


# Явная загрузка runtime-DLL продюсера через ctypes ДО первого enum. КЛЮЧЕВОЕ отличие
# приложения от diag: diag грузит эти DLL (в т.ч. MVGigEVisionSDK.dll — GigE-транспорт)
# через ctypes.WinDLL и НАХОДИТ камеру, а приложение полагалось только на harvester и
# видело 0 устройств. Явная предзагрузка в главном потоке инициализирует сетевой стек
# SDK — после неё enum перечисляет GigE-камеру (воспроизводим рабочий путь diag).
_runtime_preloaded = False


def _preload_runtime_dlls():
    global _runtime_preloaded
    if _runtime_preloaded:
        return
    cti, _ = _discover_cti()
    if cti is None:
        return
    cti_dir = cti.parent
    try:
        if hasattr(os, "add_dll_directory") and cti_dir.is_dir():
            os.add_dll_directory(str(cti_dir))
    except Exception:
        pass
    patterns = ["MvCameraControl.dll", "GenApi_*.dll", "GCBase_*.dll", "Log_*.dll",
                "MvRender*.dll", "MVGigEVisionSDK*.dll", "MvProducerGEV.cti"]
    loaded, seen = [], set()
    for pat in patterns:
        for dll in sorted(cti_dir.glob(pat)):
            if dll.name in seen:
                continue
            seen.add(dll.name)
            try:
                ctypes.WinDLL(str(dll))
                loaded.append(dll.name)
            except Exception:
                pass
    _runtime_preloaded = True
    log_event("camera_core.driver", "Runtime-DLL продюсера предзагружены (как в diag)",
              "info", {"loaded": loaded})


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


# кэш GigE-устройств от MVS SDK: серийник -> MV_CC_DEVICE_INFO (для открытия стрима)
_sdk_devices = {}
_sdk_ips = {}                 # серийник -> IP (из SDK-enum, без открытия камеры)
_sdk_devices_lock = threading.Lock()


def _sdk_ip(serial_number):
    """IP камеры из кэша SDK-enum (обход genicam-open). None, если SDK не нашёл."""
    with _sdk_devices_lock:
        return _sdk_ips.get(serial_number)


# Прогрев сетевого слоя MVS SDK (MV_CC_EnumDevices) ДО discovery harvesters + кэш устройств.
# GenTL-продюсер ищет через limited-broadcast, который ОС шлёт в интерфейс с высшим
# приоритетом; при множестве адаптеров/VPN он уходит не на тот NIC → 0 устройств. SDK (как
# MVS) сам обходит все NIC — после его enum тот же MvCameraControl.dll отдаёт устройства и
# продюсеру (эффект на процесс). Заодно кэшируем device_info по серийнику для SDK-стрима.
def _sdk_gige_warmup():
    cti, _ = _discover_cti()
    if cti is None:
        return
    if not sdk_gige.init(cti.parent):
        return
    try:
        devices = sdk_gige.enum_gige()
        # кэш НЕ очищаем: при активном стриме камера занята и enum вернёт 0 — иначе выбили
        # бы device_info и следующий стрим ушёл бы в harvesters-путь. Держим последнее известное.
        with _sdk_devices_lock:
            for d in devices:
                _sdk_devices[d["serial"]] = d["_info"]
                if d.get("ip"):
                    _sdk_ips[d["serial"]] = d["ip"]
        log_event("camera_core.sdk_warmup", "Прогрев MVS SDK (обход сетевых адаптеров)",
                  "info", {"device_count": len(devices)})
    except Exception as e:
        log_event("camera_core.sdk_warmup", "Прогрев MVS SDK не выполнен", "warn",
                  {"error": str(e)})


def _sdk_device_info(serial_number):
    with _sdk_devices_lock:
        info = _sdk_devices.get(serial_number)
    if info is not None:
        return info
    # нет в кэше — свежий enum (камера могла только что подключиться/освободиться)
    try:
        for d in sdk_gige.enum_gige():
            with _sdk_devices_lock:
                _sdk_devices[d["serial"]] = d["_info"]
                if d.get("ip"):
                    _sdk_ips[d["serial"]] = d["ip"]
    except Exception:
        pass
    with _sdk_devices_lock:
        return _sdk_devices.get(serial_number)


# Убрать серийник из SDK-кэша device_info. Нужно после смены IP: в кэше остаётся
# device_info со СТАРЫМ адресом, и MV_CC_OpenDevice идёт на него → 0x80000206
# (MV_E_GC_ACCESS, камера уже на новом IP). После сброса следующий _sdk_device_info()
# промахнётся по кэшу и сделает свежий enum, подхватив новый адрес. См. bug.txt.
def _sdk_invalidate_device(serial_number):
    with _sdk_devices_lock:
        _sdk_devices.pop(serial_number, None)
