"""fake_mode — стенд без железа: web_MVS целиком на ноутбуке, с фейковыми камерами, платой, ПЛК и сайдкарами.

Включается переменной WEB_MVS_FAKE=1 (её ставит scripts/fake_server.py; руками не нужно). Что подменяется:
  * данные — отдельный каталог (WEB_MVS_DATA_DIR, по умолчанию <репо>/.fake_data): боевые plate_config.json / mvs.db не трогаем;
  * камеры — camera_core/fake_worker.py (GigE и RTSP отдают синтетические кадры, см. fake_frames.py);
  * Dahua CGI (свет, зум, фокус, сеть) — in-memory «камера» ниже (fake_cgi), разбор ответов остаётся настоящим;
  * плата микроскопа и ПЛК — scripts/plate_sim.py (Modbus на 127.0.0.1:15020);
  * сайдкары cv/video/motion/level — настоящие, но из общего venv и на синтетических кадрах (scripts/fake_server.py);
  * наружу ничего не уходит: пульс sysmon, автообновление, автозапуск Windows и кнопки обновления в фейке выключены (app.py).
Без WEB_MVS_FAKE этот модуль ничего не меняет.
"""
import json
import os
import re
import threading
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parent
ENABLED = os.environ.get("WEB_MVS_FAKE", "").strip().lower() in ("1", "true", "yes", "on")

PLATE_HOST, PLATE_PORT = "127.0.0.1", 15020          # плата микроскопа и ПЛК — один эмулятор (plate_sim)
APP_PORT = 8011

# «GigE-камеры»: серийник -> (IP, модель). Первая — камера микроскопа (plate_config.camera_serial)
GIGE_DEVICES = {
    "FAKE-GIGE-1": ("192.168.1.222", "MV-CS050-10GC (fake)"),
    "FAKE-GIGE-2": ("192.168.1.223", "MV-CS050-10GC (fake)"),
}
# RTSP-камеры в базе: loopback-адреса (127.0.0.x), чтобы ничего не уходило в сеть
RTSP_SEED = [
    {"url": "rtsp://admin:fake@127.0.0.2:554/cam/realmonitor?channel=1&subtype=0", "label": "fake zoom", "ip": "127.0.0.2",
     "scale": 100, "fps": 0, "serial": "rtsp_fake_1", "autostart": True},
    {"url": "rtsp://admin:fake@127.0.0.3:554/cam/realmonitor?channel=1&subtype=0", "label": "fake КЗ-1", "ip": "127.0.0.3",
     "scale": 100, "fps": 0, "serial": "rtsp_fake_2", "autostart": False},
]
FAKE_RTSP_HOSTS = ("127.0.0.2", "127.0.0.3", "127.0.0.4", "127.0.0.5")


def is_fake_url(url: str) -> bool:
    """RTSP-адрес из фейк-базы (loopback 127.0.0.x)? Такие камеры отдают синтетику (и в motion_service тоже)."""
    return bool(re.match(r"^rtsps?://[^/]*@?(127\.0\.0\.\d+)", url or ""))


def default_data_dir() -> Path:
    return REPO / ".fake_data"


def plate_config_seed() -> dict:
    """Стартовый plate_config.json песочницы: всё на локальные эмуляторы, автозапуски сайдкаров — нашим сервером."""
    return {
        "host": PLATE_HOST, "port": PLATE_PORT,
        "micro_enabled": True,
        "camera_serial": "FAKE-GIGE-1", "camera_ip": GIGE_DEVICES["FAKE-GIGE-1"][0],
        "sv_source": {"enabled": True, "host": PLATE_HOST, "port": PLATE_PORT,
                      "fields": {"v_syrup": {"reg": 659, "scale": 1}, "v_water": {"reg": 660, "scale": 1},
                                 "v_product": {"reg": 661, "scale": 1}}},
        "cv": {"enabled": True, "service_url": "http://127.0.0.1:8765"},
        "video": {"service_url": "http://127.0.0.1:8767"},
        "motion": {"service_url": "http://127.0.0.1:8768"},
        "level": {"service_url": "http://127.0.0.1:8770"},
        "services": {n: {"autostart": False} for n in ("cv", "video", "motion", "level")},
    }


def seed(data_dir: Path) -> None:
    """Положить в песочницу стартовые файлы, если их ещё нет (повторный запуск ничего не затирает)."""
    data_dir.mkdir(parents=True, exist_ok=True)
    f = data_dir / "plate_config.json"
    if not f.exists():
        f.write_text(json.dumps(plate_config_seed(), ensure_ascii=False, indent=2), encoding="utf-8")
    r = data_dir / "rtsp_cameras.json"
    if not r.exists():
        r.write_text(json.dumps(RTSP_SEED, ensure_ascii=False, indent=2), encoding="utf-8")
    # монитор: отправка наружу отключена жёстко (sysmon в фейке не стартует), но и конфиг держим пустым
    m = data_dir / "monitor.json"
    if not m.exists():
        m.write_text(json.dumps({"enabled": False}), encoding="utf-8")


def prepare_env() -> Path:
    """Вызывать ДО импорта модулей проекта (они берут DATA_DIR при импорте): ставит флаги и каталог данных, сидит песочницу."""
    global ENABLED
    os.environ["WEB_MVS_FAKE"] = "1"
    ENABLED = True                         # модуль уже мог быть импортирован до установки переменной (scripts/fake_server.py)
    os.environ.setdefault("WEB_MVS_DATA_DIR", str(default_data_dir()))
    d = Path(os.environ["WEB_MVS_DATA_DIR"]).resolve()
    seed(d)
    return d


# ---------- что в фейке выключено (наружу / необратимое) ----------

BLOCKED_PREFIXES = ("/api/update/download", "/api/update/run", "/api/update/apply",
                    "/api/autostart/enable", "/api/autostart/disable",
                    "/api/net/enable_", "/api/net/disable_")      # jumbo/фильтр-драйвер: меняют настройки сетевой карты ПК


def is_blocked(path: str) -> bool:
    return ENABLED and path.startswith(BLOCKED_PREFIXES)


BADGE_HTML = ('<div style="position:fixed;left:8px;bottom:8px;z-index:99999;padding:2px 8px;border-radius:4px;'
              'background:#c2410c;color:#fff;font:600 11px/1.6 system-ui,sans-serif;letter-spacing:.06em;opacity:.85;'
              'pointer-events:none">FAKE</div>')


def inject_badge(html: str) -> str:
    return html.replace("</body>", BADGE_HTML + "</body>", 1) if ENABLED else html


# ---------- фейковая камера Dahua: подмена dahua_control._cgi ----------

_cams: dict = {}
_cams_lock = threading.Lock()


def _cam(host: str) -> dict:
    with _cams_lock:
        c = _cams.get(host)
        if c is None:
            cfg = {"Network.DefaultInterface": "eth0", "Network.eth0.IPAddress": host, "Network.eth0.SubnetMask": "255.255.255.0",
                   "Network.eth0.DefaultGateway": "192.168.1.1", "Network.eth0.DhcpEnable": "false",
                   "Network.eth0.PhysicalAddress": "00:11:22:33:44:55"}
            for p in (0, 1, 2):
                cfg["Lighting_V2[0][%d][0].LightType" % p] = "WhiteLight"
                cfg["Lighting_V2[0][%d][0].Mode" % p] = "Off"
                cfg["Lighting_V2[0][%d][0].MiddleLight[0].Light" % p] = "50"
                cfg["VideoInExposure[0][%d].Compensation" % p] = "50"
                cfg["VideoInExposure[0][%d].GainMin" % p] = "0"
                cfg["VideoInExposure[0][%d].GainMax" % p] = "60"
                cfg["VideoInWhiteBalance[0][%d].Mode" % p] = "Auto"
                cfg["VideoInDayNight[0][%d].Mode" % p] = "Color"
            c = _cams[host] = {"cfg": cfg, "zoom": 0.3, "focus": 0.5}
        return c


def fake_cgi(host, user, password, query):
    """Заменитель dahua_control._cgi: отвечает текстом в формате Dahua по состоянию «камеры» в памяти."""
    cam = _cam(host)
    path, _, qs = query.partition("?")
    q = {k: v[0] for k, v in urllib.parse.parse_qs(qs, keep_blank_values=True).items()}
    action = q.get("action", "")
    with _cams_lock:
        if path == "magicBox.cgi" and action == "getSystemInfo":
            return "deviceType=DH-IPC-HFW-FAKE\nserialNumber=FAKE0001\n"
        if path == "configManager.cgi" and action == "getConfig":
            name = q.get("name", "")
            return "".join("table.%s=%s\n" % (k, v) for k, v in cam["cfg"].items() if k.startswith(name))
        if path == "configManager.cgi" and action == "setConfig":
            # ключи вида Lighting_V2[0][0][0].Mode=Manual (значения в URL-кодировке); parse_qs их не сломает, но берём из строки
            for part in qs.split("&")[1:]:
                k, _, v = part.partition("=")
                cam["cfg"][urllib.parse.unquote(k)] = urllib.parse.unquote(v)
            return "OK\n"
        if path == "ptz.cgi":
            if action == "getCurrentProtocolCaps":
                return "caps.ZoomTele=true\ncaps.FocusNear=true\ncaps.AutoFocus=true\n"
            code = q.get("code", "")
            if action == "start":
                step = 0.02 * int(q.get("arg2", "1") or 1)
                if code == "ZoomTele":
                    cam["zoom"] = min(1.0, cam["zoom"] + step)
                elif code == "ZoomWide":
                    cam["zoom"] = max(0.0, cam["zoom"] - step)
                elif code == "FocusNear":
                    cam["focus"] = max(0.0, cam["focus"] - step)
                elif code == "FocusFar":
                    cam["focus"] = min(1.0, cam["focus"] + step)
            return "OK\n"
        if path == "devVideoInput.cgi":
            if action == "getFocusStatus":
                return "status.Zoom=%.4f\nstatus.Focus=%.4f\n" % (cam["zoom"], cam["focus"])
            if action == "adjustFocus":
                cam["focus"] = float(q.get("focus", cam["focus"]))
                return "OK\n"
            if action == "autoFocus":
                cam["focus"] = 0.8
                return "OK\n"
    raise RuntimeError("fake cgi: не знаю запрос %s" % query)


def install_dahua() -> None:
    import dahua_control
    dahua_control._cgi = fake_cgi
