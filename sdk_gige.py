"""Захват кадров GigE через официальный MVS SDK (MvCameraControl.dll) — с resend.

Зачем: harvesters/GenTL-продюсер не умеет переспрашивать потерянные UDP-пакеты
(resend). Кадр 5МП RGB = ~15 МБ = ~10000 пакетов; на нагруженной сети теряется хоть
один — и кадр не собирается (сплошные таймауты -1011). Настольный MVS работает, потому
что включает MV_GIGE_SetResend. Здесь делаем так же: открываем камеру напрямую через
SDK, включаем resend, оптимальный размер пакета, и берём кадры MV_CC_GetImageBuffer.

Обёртка SDK вложена в проект (mvsdk/, чистый ctypes). Рантайм MVS (MvCameraControl.dll)
обязателен — он и так нужен GenTL-продюсеру для .cti. Модуль тихо деградирует
(available()==False), если рантайма/обёртки нет: тогда остаётся старый harvesters-путь.
"""
import os
import threading

import numpy as np

from logger import log_event

_sdk = None          # модуль mvsdk после успешной загрузки
_init_done = False
_pixel_names = {}    # enPixelType(int) -> строка формата для _to_bgr


def init(runtime_dir=None):
    """Один раз: добавить каталог рантайма в поиск DLL и импортировать mvsdk.

    runtime_dir — папка с MvCameraControl.dll (обычно рядом с .cti). Возвращает True,
    если SDK загрузился."""
    global _sdk, _init_done
    if _init_done:
        return _sdk is not None
    _init_done = True
    try:
        if runtime_dir:
            runtime_dir = str(runtime_dir)
            if hasattr(os, "add_dll_directory") and os.path.isdir(runtime_dir):
                try:
                    os.add_dll_directory(runtime_dir)
                except Exception:
                    pass
            os.environ["PATH"] = runtime_dir + os.pathsep + os.environ.get("PATH", "")
        import mvsdk  # вложенная обёртка (mvsdk/__init__.py)
        _sdk = mvsdk
        _build_pixel_map()
        log_event("sdk_gige", "MVS SDK загружен для захвата GigE", "info")
        return True
    except Exception as e:
        log_event("sdk_gige", "MVS SDK недоступен — остаётся harvesters-путь", "warn",
                  {"error": str(e)})
        return False


def available():
    return _sdk is not None


def _build_pixel_map():
    # SDK enPixelType -> строка формата, понятная camera_core._to_bgr
    names = {
        "PixelType_Gvsp_RGB8_Packed": "RGB8",
        "PixelType_Gvsp_BGR8_Packed": "BGR8",
        "PixelType_Gvsp_Mono8": "Mono8",
        "PixelType_Gvsp_BayerRG8": "BayerRG",
        "PixelType_Gvsp_BayerGB8": "BayerGB",
        "PixelType_Gvsp_BayerGR8": "BayerGR",
        "PixelType_Gvsp_BayerBG8": "BayerBG",
    }
    for const_name, fmt in names.items():
        val = getattr(_sdk, const_name, None)
        if val is not None:
            _pixel_names[int(val)] = fmt


def enum_gige():
    """GigE-камеры через SDK: список {serial, model, ip, _info}. _info — для open()."""
    if _sdk is None:
        return []
    dev_list = _sdk.MV_CC_DEVICE_INFO_LIST()
    ret = _sdk.MvCamera.MV_CC_EnumDevices(_sdk.MV_GIGE_DEVICE, dev_list)
    result = []
    if ret != 0:
        return result
    for i in range(dev_list.nDeviceNum):
        info = _sdk.cast(dev_list.pDeviceInfo[i],
                         _sdk.POINTER(_sdk.MV_CC_DEVICE_INFO)).contents
        gige = info.SpecialInfo.stGigEInfo
        ip = gige.nCurrentIp
        # копируем структуру: указатели из dev_list живут только до следующего Enum
        info_copy = _sdk.MV_CC_DEVICE_INFO()
        _sdk.memmove(_sdk.byref(info_copy), _sdk.byref(info),
                     _sdk.sizeof(_sdk.MV_CC_DEVICE_INFO))
        result.append({
            "serial": bytes(gige.chSerialNumber).split(b"\x00")[0].decode("ascii", "replace"),
            "model": bytes(gige.chModelName).split(b"\x00")[0].decode("ascii", "replace"),
            "ip": "%d.%d.%d.%d" % ((ip >> 24) & 255, (ip >> 16) & 255, (ip >> 8) & 255, ip & 255),
            "_info": info_copy,
        })
    return result


class GigeSdkStream:
    """Один сеанс захвата с GigE-камеры через SDK. По handle на камеру — как в MVS,
    поэтому несколько камер параллельно не мешают друг другу."""

    def __init__(self, device_info):
        self._info = device_info
        self._cam = _sdk.MvCamera()
        self._grabbing = False
        self._lock = threading.Lock()

    def open(self, settings=None):
        c = self._cam
        r = c.MV_CC_CreateHandle(self._info)
        if r != 0:
            raise RuntimeError("MV_CC_CreateHandle ret=0x%x" % (r & 0xffffffff))
        r = c.MV_CC_OpenDevice()
        if r != 0:
            c.MV_CC_DestroyHandle()
            # 0x80000203 = device busy/уже открыта другим клиентом (частая причина
            # «первый раз ок, потом не подключается» — предыдущий handle не отпущен)
            raise RuntimeError("MV_CC_OpenDevice ret=0x%x" % (r & 0xffffffff))
        # настройки применяем ДО StartGrabbing (размер/формат нельзя менять на ходу)
        if settings:
            self._apply_settings(settings)
        # оптимальный размер пакета под текущий линк (как MVS). На путях без сквозного
        # jumbo (VPN/свитч) вернёт 1500 — надёжно; jumbo дал бы больше fps, но его роняет сеть.
        try:
            opt = c.MV_CC_GetOptimalPacketSize()
            if opt > 0:
                c.MV_CC_SetIntValue("GevSCPSPacketSize", opt)
        except Exception:
            pass
        # задержку между пакетами в 0 — максимальная скорость; потери добирает resend.
        # (заодно сбрасываем возможное «наследие» большого GevSCPD, роняющего fps)
        try:
            c.MV_CC_SetIntValue("GevSCPD", 0)
        except Exception:
            pass
        # КЛЮЧЕВОЕ: переспрос потерянных пакетов — иначе кадр 15 МБ не собирается
        try:
            c.MV_GIGE_SetResend(1, 10, 50)
        except Exception as e:
            log_event("sdk_gige", "MV_GIGE_SetResend не применён", "warn", {"error": str(e)})
        if c.MV_CC_StartGrabbing() != 0:
            self.close()
            raise RuntimeError("MV_CC_StartGrabbing failed")
        self._grabbing = True

    def _apply_settings(self, s):
        """Применить настройки камеры по SDK (до StartGrabbing). Порядок важен:
        формат -> смещения в 0 -> размеры -> смещения. Ошибку каждого поля терпим
        (несовместимые значения бывают) и логируем, чтобы поток всё равно поднялся."""
        c = self._cam
        res = {}

        def si(name, val):
            if val is None:
                return
            res[name] = c.MV_CC_SetIntValue(name, int(val)) & 0xffffffff

        def sf(name, val):
            if val is None:
                return
            res[name] = c.MV_CC_SetFloatValue(name, float(val)) & 0xffffffff

        def se(name, val):
            if not val:
                return
            res[name] = c.MV_CC_SetEnumValueByString(name, str(val)) & 0xffffffff

        se("PixelFormat", s.get("pixel_format"))
        # сбрасываем смещения, чтобы уменьшение размера не упёрлось в старый offset
        c.MV_CC_SetIntValue("OffsetX", 0)
        c.MV_CC_SetIntValue("OffsetY", 0)
        si("Width", s.get("width"))
        si("Height", s.get("height"))
        si("OffsetX", s.get("offset_x"))
        si("OffsetY", s.get("offset_y"))
        se("ExposureAuto", s.get("exposure_auto"))
        # выдержку задаём только при ручном режиме (в авто камера её игнорирует/отклонит)
        if str(s.get("exposure_auto") or "").lower() in ("off", ""):
            sf("ExposureTime", s.get("exposure_time"))
        # частота кадров: включаем лимит и ставим значение
        if s.get("fps"):
            try:
                c.MV_CC_SetBoolValue("AcquisitionFrameRateEnable", True)
            except Exception:
                pass
            sf("AcquisitionFrameRate", s.get("fps"))

        applied = [k for k, v in res.items() if v == 0]
        failed = {k: "0x%x" % v for k, v in res.items() if v != 0}
        log_event("sdk_gige", "Настройки камеры применены (SDK)", "info",
                  {"applied": applied, "failed": failed})

    def grab(self, timeout_ms=1000):
        """Один кадр: (width, height, pixel_format_str, raw_uint8) или None по таймауту."""
        frame = _sdk.MV_FRAME_OUT()
        _sdk.memset(_sdk.byref(frame), 0, _sdk.sizeof(frame))
        ret = self._cam.MV_CC_GetImageBuffer(frame, timeout_ms)
        if ret != 0:
            return None
        try:
            fi = frame.stFrameInfo
            size = fi.nFrameLen
            buf = (_sdk.c_ubyte * size)()
            _sdk.memmove(buf, frame.pBufAddr, size)
            arr = np.frombuffer(bytes(buf), dtype=np.uint8)
            fmt = _pixel_names.get(int(fi.enPixelType))
            return fi.nWidth, fi.nHeight, fmt, arr
        finally:
            self._cam.MV_CC_FreeImageBuffer(frame)

    def close(self):
        with self._lock:
            try:
                if self._grabbing:
                    self._cam.MV_CC_StopGrabbing()
            except Exception:
                pass
            self._grabbing = False
            try:
                self._cam.MV_CC_CloseDevice()
            except Exception:
                pass
            try:
                self._cam.MV_CC_DestroyHandle()
            except Exception:
                pass
