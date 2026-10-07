"""camera_core — пакет работы с камерами (GigE Vision через MVS SDK/Harvesters и RTSP).

Бывший модуль camera_core.py, разрезанный механическим переносом. Публичный API не изменился:
  from camera_core import manager, build_rtsp_url, replace_host_in_url, ...
Все прежние имена (включая приватные) реэкспортируются здесь. Порядок импорта важен: gentl_env
применяет совместимость genicam и регистрирует каталог DLL драйвера ДО создания manager.
"""
from .gentl_env import (
    _patch_genicam_register_event,
    GENTL_HINTS,
    _OPEN_FLAKY_RETRIES,
    FRAME_FETCH_TIMEOUT,
    MAX_FRAME_TIMEOUTS,
    SDK_GRAB_TIMEOUT_MS,
    SDK_STREAM_STALL_SECONDS,
    _gentl_code,
    _decode_gentl_message,
    _explain_error,
    PROGRAM_DIR,
    CTI_FILENAME,
    MVS_RUNTIME_DLL,
    MVS_GENTL_DIRS,
    _bundle_has_full_runtime,
    _discover_cti,
    _register_driver_dll_dir,
    _runtime_preloaded,
    _preload_runtime_dlls,
    _find_mvs_runtime,
    _sdk_devices,
    _sdk_ips,
    _sdk_devices_lock,
    _sdk_ip,
    _sdk_gige_warmup,
    _sdk_device_info,
    _sdk_invalidate_device,
)
from .utils import (
    DEFAULT_VIDEO_FPS,
    RTSP_STALL_SECONDS,
    RTSP_RECONNECT_BACKOFF,
    RTSP_RECONNECT_LOG_EVERY,
    DISK_FREE_WARN_MB,
    DISK_CHECK_PERIOD,
    PHOTO_FORMATS,
    PNG_COMPRESSION,
    ip_to_int,
    int_to_ip,
    _format_mac,
    build_rtsp_url,
    replace_host_in_url,
    ping_device,
)
from .imaging import (
    _BAYER_TO_BGR,
    _bayer_code,
    _to_bgr,
    _GAMMA_LUT_CACHE,
    _gamma_lut,
    _COLORMAPS,
    _apply_color,
)
from .base_worker import (
    BaseCameraWorker,
)
from .gige_worker import (
    CameraWorker,
)
from .rtsp_worker import (
    RtspCameraWorker,
)
from .camera_manager import (
    CameraManager,
    manager,
)
