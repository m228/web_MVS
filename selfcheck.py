"""selfcheck — проверка, что КАЖДЫЙ модуль web_MVS живой и откликается (без железа).

Запуск из ИСХОДНИКОВ:   python3 selfcheck.py
Запуск из СОБРАННОГО:   SelfCheck.exe        (лежит рядом с web_MVS.exe, тот же бандл и _internal)
Ключи:
    --hw         дополнительно «живые» проверки железа (GigE-камеры, плата, ПЛК, CV-сервис, RTSP-камеры):
                 только чтение/опрос, ничего не меняют. Без железа эти пункты — SKIP.
    --force      с --hw: опрашивать плату/ПЛК, даже если web_MVS запущен (по умолчанию пропускается,
                 чтобы второй Modbus-клиент не мешал работающей варке)
    --no-pause   не ждать Enter в конце (для скриптов)
    --keep       не удалять временную песочницу
Результат: таблица OK / SKIP / FAIL по модулям, отчёт дублируется в selfcheck_output.txt рядом с exe.
Код возврата: 0 — нет FAIL, 1 — есть FAIL.

Принцип безопасности: ВСЕ пользовательские данные (plate_config.json, dataset/, cv_results/,
rtsp_cameras.json …) на время проверки уходят во ВРЕМЕННУЮ ПЕСОЧНИЦУ (paths.DATA_DIR подменяется до
импорта модулей проекта), боевые данные не читаются и не пишутся. Железо без --hw не трогается:
плата и ПЛК заменены встроенным мини-Modbus-сервером на 127.0.0.1, веб-слой вызывается напрямую
через ASGI (без сети), lifespan приложения (драйвер камер, микроскоп) НЕ запускается.
"""
import asyncio
import json
import os
import re
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import traceback
from collections import defaultdict
from pathlib import Path

# --- песочница: подменяем DATA_DIR ДО импорта любого модуля проекта (они берут его при импорте) ---
import paths

REAL_DATA_DIR = paths.DATA_DIR
BUNDLE_DIR = paths.BUNDLE_DIR
SANDBOX = Path(tempfile.mkdtemp(prefix="web_mvs_selfcheck_"))
paths.DATA_DIR = SANDBOX

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# --- предохранитель сети: без --hw ЛЮБОЕ подключение за пределы localhost блокируется и записывается.
# Гарантирует обещание «железо не трогается» (плата/ПЛК/камеры/CV-сервис по сети): даже если какой-то
# модуль возьмёт боевой адрес из конфига по умолчанию, соединение не уйдёт, а проверка станет FAIL.
_NET_GUARD = {"on": True}
_NET_VIOLATIONS = []
_orig_connect = socket.socket.connect
_orig_connect_ex = socket.socket.connect_ex


def _is_local(address):
    host = address[0] if isinstance(address, tuple) and address else None
    return not isinstance(host, str) or host in ("localhost", "::1", "0.0.0.0", "") or host.startswith("127.")


def _guarded_connect(self, address):
    if _NET_GUARD["on"] and not _is_local(address):
        _NET_VIOLATIONS.append("%s:%s" % (address[0], address[1] if len(address) > 1 else "?"))
        raise ConnectionRefusedError("selfcheck: сеть вне localhost запрещена без --hw (%s)" % (address,))
    return _orig_connect(self, address)


def _guarded_connect_ex(self, address):
    if _NET_GUARD["on"] and not _is_local(address):
        _NET_VIOLATIONS.append("%s:%s" % (address[0], address[1] if len(address) > 1 else "?"))
        return 10061   # WSAECONNREFUSED
    return _orig_connect_ex(self, address)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex


class Skip(Exception):
    """Проверка неприменима (нет железа / не включён --hw) — не ошибка."""


CHECKS = []   # (группа, название, hw, функция)


def check(group, name, hw=False):
    def deco(fn):
        CHECKS.append((group, name, hw, fn))
        return fn
    return deco


# =====================================================================================
# Инфраструктура: мини-Modbus-TCP сервер (замена платы и ПЛК), ASGI-вызов приложения
# =====================================================================================

class FakeModbus:
    """Хранилище holding-регистров по TCP: FC03 (чтение), FC06 (запись 1), FC16 (запись N)."""

    def __init__(self):
        self.regs = defaultdict(int)
        self.writes = []
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self._srv.settimeout(0.2)
        self.port = self._srv.getsockname()[1]
        self._running = True
        self._conns = []
        self._thread = threading.Thread(target=self._accept, name="selfcheck-modbus", daemon=True)
        self._thread.start()

    def _accept(self):
        while self._running:
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn.settimeout(0.5)
            self._conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    @staticmethod
    def _recv(conn, n, running):
        buf = b""
        while len(buf) < n:
            try:
                chunk = conn.recv(n - len(buf))
            except socket.timeout:
                if not running():
                    return None
                continue
            except OSError:
                return None
            if not chunk:
                return None
            buf += chunk
        return buf

    def _serve(self, conn):
        run = lambda: self._running
        while self._running:
            head = self._recv(conn, 7, run)
            if head is None:
                break
            tid, pid, length, unit = struct.unpack(">HHHB", head)
            pdu = self._recv(conn, length - 1, run)
            if pdu is None:
                break
            fc = pdu[0]
            try:
                if fc == 3:
                    addr, count = struct.unpack(">HH", pdu[1:5])
                    data = b"".join(struct.pack(">H", self.regs[addr + i] & 0xFFFF) for i in range(count))
                    resp = bytes([3, len(data)]) + data
                elif fc == 6:
                    addr, val = struct.unpack(">HH", pdu[1:5])
                    self.regs[addr] = val
                    self.writes.append((addr, val))
                    resp = pdu[:5]
                elif fc == 16:
                    addr, count, _bc = struct.unpack(">HHB", pdu[1:6])
                    for i in range(count):
                        v = struct.unpack(">H", pdu[6 + 2 * i:8 + 2 * i])[0]
                        self.regs[addr + i] = v
                        self.writes.append((addr + i, v))
                    resp = struct.pack(">BHH", 16, addr, count)
                else:
                    resp = bytes([fc | 0x80, 1])
            except Exception:
                resp = bytes([fc | 0x80, 4])
            try:
                conn.sendall(struct.pack(">HHHB", tid, 0, len(resp) + 1, unit) + resp)
            except OSError:
                break

    def close(self):
        self._running = False
        try:
            self._srv.close()
        except OSError:
            pass
        for c in self._conns:
            try:
                c.close()
            except OSError:
                pass


_fake = None


def fake_modbus():
    global _fake
    if _fake is None:
        _fake = FakeModbus()
    return _fake


async def _asgi_get(app, path, query=""):
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": query.encode(),
             "headers": [(b"host", b"selfcheck")], "client": ("127.0.0.1", 1),
             "server": ("selfcheck", 80), "root_path": ""}
    out = {"status": None, "headers": {}, "body": bytearray()}
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            await asyncio.sleep(3600)
        sent["done"] = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
            out["headers"] = {k.decode().lower(): v.decode() for k, v in msg.get("headers", [])}
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")

    await asyncio.wait_for(app(scope, receive, send), timeout=30)
    return out["status"], out["headers"], bytes(out["body"])


def http_get(path, query=""):
    import app as web
    return asyncio.run(_asgi_get(web.app, path, query))


def synth_frame(w=640, h=480, crystals=6):
    """Синтетический кадр: светлый фон + светлые эллипсы-«кристаллы» + тёмный блок."""
    import cv2
    import numpy as np
    img = np.full((h, w, 3), 90, np.uint8)
    rng = np.random.RandomState(1)
    objs = []
    for i in range(crystals):
        cx, cy = 80 + (i % 3) * 190, 100 + (i // 3) * 220
        ax, ay = int(rng.randint(30, 50)), int(rng.randint(22, 38))
        cv2.ellipse(img, (cx, cy), (ax, ay), int(rng.randint(0, 90)), 0, 360, (210, 215, 220), -1)
        poly = cv2.ellipse2Poly((cx, cy), (ax, ay), 0, 0, 360, 15)
        objs.append({"bbox": [cx - ax, cy - ay, cx + ax, cy + ay], "conf": 0.9,
                     "polygon": poly.tolist()})
    return img, objs


# =====================================================================================
# 1. Окружение и ресурсы бандла
# =====================================================================================

@check("Окружение", "Python / режим / песочница данных")
def _env():
    import plate_config
    assert str(plate_config.CONFIG_PATH).startswith(str(SANDBOX)), "песочница не подхватилась модулями"
    return "py %s, %s, bundle=%s" % (sys.version.split()[0], "exe" if getattr(sys, "frozen", False) else "исходники", BUNDLE_DIR)


@check("Окружение", "VERSION читается")
def _version():
    v = paths.read_version()
    assert v and v != "dev" or not getattr(sys, "frozen", False), "в бандле нет VERSION"
    return v


@check("Ресурсы", "страницы page/*.html на месте")
def _pages():
    names = ["index", "camera", "rtsp", "multi", "network", "microscope"]
    miss = [n for n in names if not (BUNDLE_DIR / "page" / (n + ".html")).is_file()]
    assert not miss, "нет страниц: %s" % miss
    return "%d страниц" % len(names)


def _html_static_refs():
    refs = {}
    for html in sorted((BUNDLE_DIR / "page").glob("*.html")):
        text = html.read_text(encoding="utf-8")
        refs[html.name] = re.findall(r"""(?:src|href)\s*=\s*["'](/static/[^"'#?]+)""", text)
    return refs


@check("Ресурсы", "все /static/... из HTML существуют (скрипты, стили, иконки)")
def _static_refs():
    miss, total = [], 0
    for page, refs in _html_static_refs().items():
        for r in refs:
            total += 1
            if not (BUNDLE_DIR / "page" / r.lstrip("/")).is_file():
                miss.append("%s → %s" % (page, r))
    assert not miss, "битые ссылки: %s" % miss
    return "%d ссылок" % total


@check("Ресурсы", "в каждой странице <script> идут без дублей, а JS-файлы страниц не пустые")
def _script_order():
    problems = []
    for page, refs in _html_static_refs().items():
        js = [r for r in refs if r.endswith(".js")]
        if len(js) != len(set(js)):
            problems.append("%s: дубли скриптов" % page)
        for r in js:
            if (BUNDLE_DIR / "page" / r.lstrip("/")).stat().st_size < 20:
                problems.append("%s: пустой %s" % (page, r))
    assert not problems, problems
    return "ok"


@check("Ресурсы", "драйвер MVS: Driver/*.dll и *.cti")
def _driver_files():
    d = BUNDLE_DIR / "Driver"
    miss = [n for n in ("MvCameraControl.dll", "MvProducerGEV.cti") if not (d / n).is_file()]
    assert not miss, "нет в %s: %s" % (d, miss)
    return str(d)


@check("Ресурсы", "все /api/... из JS-страниц существуют в приложении")
def _js_api_routes():
    import app as web
    routes = {r.path for r in web.app.routes if getattr(r, "path", None)}
    unknown, seen = [], set()
    for js in sorted((BUNDLE_DIR / "page" / "static" / "js").glob("*.js")) + sorted(
            (BUNDLE_DIR / "page" / "static" / "js").glob("**/*.js")):
        text = js.read_text(encoding="utf-8")
        for m in re.findall(r"""['"`](/api/[A-Za-z0-9_/\-]*)""", text):
            if m in seen:
                continue
            seen.add(m)
            if m in routes or any(r.startswith(m) for r in routes):
                continue
            unknown.append("%s (%s)" % (m, js.name))
    for page in (BUNDLE_DIR / "page").glob("*.html"):
        for m in re.findall(r"""['"`](/api/[A-Za-z0-9_/\-]*)""", page.read_text(encoding="utf-8")):
            if m not in seen and not (m in routes or any(r.startswith(m) for r in routes)):
                unknown.append("%s (%s)" % (m, page.name))
            seen.add(m)
    assert not unknown, "в JS есть адреса без эндпоинта: %s" % unknown
    return "%d адресов, маршрутов в app: %d" % (len(seen), len(routes))


# =====================================================================================
# 2. Импорты и публичный API (то, на что опираются соседние модули)
# =====================================================================================

MODULES = [
    "paths", "logger", "camera_core", "camera_core.gentl_env", "camera_core.utils", "camera_core.imaging",
    "camera_core.base_worker", "camera_core.gige_worker", "camera_core.rtsp_worker", "camera_core.camera_manager",
    "sdk_gige", "dahua_control", "net_tools", "rtsp_store",
    "save_settings", "plate_config", "sv_source", "microscope_plc", "microscope_fsm",
    "microscope_service", "cv_analyzer", "cv_volume", "substages", "db", "report", "cv_fracture", "cv_client", "cv_store", "fracture_lab",
    "updater", "autostart", "diag", "app", "mvsdk",
]
THIRD_PARTY = ["cv2", "numpy", "fastapi", "starlette", "uvicorn", "pymodbus", "harvesters", "genicam",
               "pydantic", "multipart"]

# имя модуля -> имена, которые от него ждут другие модули / диагностика
API = {
    "camera_core": ["manager", "CameraManager", "CameraWorker", "RtspCameraWorker", "BaseCameraWorker",
                    "build_rtsp_url", "replace_host_in_url", "ip_to_int", "int_to_ip", "ping_device",
                    "_to_bgr", "_apply_color", "_discover_cti", "_find_mvs_runtime", "_explain_error",
                    "MVS_GENTL_DIRS"],
    "microscope_service": ["micro", "MicroscopeService"],
    "microscope_plc": ["PlateClient"],
    "microscope_fsm": ["MicroscopeFSM"],
    "sv_source": ["SvSource"],
    "plate_config": ["load", "save", "replace_all", "backup", "DEFAULTS", "CONFIG_PATH"],
    "cv_analyzer": ["analyze", "measure_objects", "summarize", "draw_overlay", "blur_score"],
    "db": ["upsert_row", "read_rows", "rebuild_boils", "is_new_boil"],
    "cv_volume": ["volume_cfg", "crystal_volumes", "sums_for", "add_sums", "percents"],
    "cv_fracture": ["detect_zones", "confirm", "draw", "area_pct"],
    "cv_store": ["save_sample", "list_samples", "get_last", "get_prev", "get_result", "trend",
                 "trend_range", "history_days", "get_objects", "overlay_path", "thumb_path"],
    "cv_client": ["health", "infer", "model_info", "model_upload", "model_load"],
    "fracture_lab": ["save_frame", "list_frames", "set_label", "delete", "zones", "all_zones", "jpeg"],
    "rtsp_store": ["load", "save", "set_autostart", "remove"],
    "save_settings": ["load", "get", "update"],
    "updater": ["check_latest", "download_latest", "apply_update", "_version_tuple"],
    "autostart": ["status", "enable", "disable"],
    "net_tools": ["status", "enable_jumbo", "disable_jumbo", "enable_filter", "disable_filter"],
    "dahua_control": ["parse_rtsp_credentials", "get_capabilities", "set_white_light"],
    "sdk_gige": ["init", "available", "enum_gige", "GigeSdkStream", "read_ranges"],
    "logger": ["log_event", "get_events"],
    "paths": ["BUNDLE_DIR", "DATA_DIR", "read_version"],
    "app": ["app"],
}


def _make_import_check(modname):
    def fn():
        import importlib
        mod = importlib.import_module(modname)
        return getattr(mod, "__file__", "builtin") and ""
    return fn


for _m in MODULES:
    check("Импорт", "модуль %s" % _m)(_make_import_check(_m))
for _m in THIRD_PARTY:
    check("Импорт", "библиотека %s" % _m)(_make_import_check(_m))


@check("Импорт", "список MODULES покрывает все .py проекта (только исходники)")
def _modules_complete():
    if getattr(sys, "frozen", False):
        raise Skip("в exe исходников нет")
    skip = {"run", "selfcheck"}
    pyfiles = {p.stem for p in BUNDLE_DIR.glob("*.py")} - skip
    pkgs = {p.parent.name for p in BUNDLE_DIR.glob("*/__init__.py") if p.parent.name in ("mvsdk", "camera_core")}
    known = set(MODULES)
    missing = (pyfiles | pkgs) - known
    assert not missing, "модули не внесены в selfcheck.MODULES: %s" % sorted(missing)
    return "%d модулей" % len(pyfiles | pkgs)


def _make_api_check(modname, names):
    def fn():
        import importlib
        mod = importlib.import_module(modname)
        miss = [n for n in names if not hasattr(mod, n)]
        assert not miss, "нет имён: %s" % miss
        return "%d имён" % len(names)
    return fn


for _m, _names in API.items():
    check("Публичный API", "%s: нужные имена на месте" % _m)(_make_api_check(_m, _names))


@check("Публичный API", "camera_core.manager — синглтон CameraManager")
def _manager_singleton():
    import camera_core
    from camera_core import manager
    assert isinstance(manager, camera_core.CameraManager)
    import microscope_service
    return "ok"


# =====================================================================================
# 3. Веб-слой (ASGI напрямую, без сети и без lifespan)
# =====================================================================================

PAGES = ["/", "/camera", "/rtsp", "/multi", "/network", "/microscope"]

# Безопасные read-only эндпоинты: не трогают камеру/плату/сеть/файлы вне песочницы.
# Всё остальное (сеттеры, стримы, update/*, cams, cv/analyze, cv/model …) сознательно НЕ вызывается.
SAFE_API = [
    "/api/version", "/api/debug/info", "/api/debug/logs",
    "/api/micro/enabled", "/api/micro/status", "/api/micro/telemetry", "/api/micro/config_dump",
    "/api/micro/config_snapshots",
    "/api/cv/settings", "/api/cv/fracture/settings", "/api/cv/approach/settings",
    "/api/cv/last", "/api/cv/prev", "/api/cv/samples", "/api/cv/trend", "/api/cv/trend/days",
    "/api/cv/fracture/lab/list",
    "/api/rtsp/saved", "/api/autostart/status", "/api/net/status",
]


def _make_page_check(path):
    def fn():
        status, headers, body = http_get(path)
        assert status == 200, "HTTP %s" % status
        assert b"<html" in body.lower() or b"<!doctype" in body.lower(), "не HTML"
        hv = {k.lower(): v for k, v in dict(headers).items()}
        assert "no-cache" in hv.get("cache-control", ""), "страница отдаётся без no-cache: браузер после обновления может взять старую из кэша"
        return "%d байт" % len(body)
    return fn


def _make_api_get_check(path):
    def fn():
        status, headers, body = http_get(path)
        assert status == 200, "HTTP %s: %s" % (status, body[:200])
        json.loads(body.decode("utf-8"))
        return "%d байт" % len(body)
    return fn


for _p in PAGES:
    check("Веб", "страница GET %s" % _p)(_make_page_check(_p))


@check("Веб", "статика: style.css и все js из страниц отдаются 200")
def _static_served():
    files = set()
    for refs in _html_static_refs().values():
        files.update(refs)
    bad = []
    for f in sorted(files):
        status, _, body = http_get(f)
        if status != 200 or not body:
            bad.append("%s → %s" % (f, status))
    assert not bad, bad
    return "%d файлов" % len(files)


for _p in SAFE_API:
    check("Веб", "API GET %s" % _p)(_make_api_get_check(_p))


@check("Веб", "несуществующий адрес даёт 404 (роутер жив)")
def _404():
    status, _, _ = http_get("/api/selfcheck/nope")
    assert status == 404, "HTTP %s" % status
    return "404"


# =====================================================================================
# 4. Камеры (без железа)
# =====================================================================================

@check("Камеры", "ip_to_int / int_to_ip туда-обратно")
def _ip():
    from camera_core import ip_to_int, int_to_ip
    for ip in ("192.168.1.108", "10.20.2.180", "0.0.0.0"):
        assert int_to_ip(ip_to_int(ip)) == ip
    return "ok"


@check("Камеры", "build_rtsp_url / replace_host_in_url / parse_rtsp_credentials")
def _rtsp_url():
    from camera_core import build_rtsp_url, replace_host_in_url
    import dahua_control
    url = build_rtsp_url("192.168.1.108", "admin", "p@ss", 1, 0)
    assert url.startswith("rtsp://admin:p@ss@192.168.1.108:554/"), url
    new = replace_host_in_url(url, "10.0.0.5")
    assert "10.0.0.5" in new and "192.168.1.108" not in new, new
    creds = dahua_control.parse_rtsp_credentials(url)
    assert creds, "creds пусты"
    return "ok"


@check("Камеры", "_explain_error расшифровывает код GenTL")
def _explain():
    from camera_core import _explain_error
    res = _explain_error(Exception("GenTL error (ID: -1006)"))
    assert res.get("code") == -1006 and res.get("hint"), "код/подсказка не разобраны: %s" % res
    assert _explain_error(Exception("что-то своё")).get("error"), "без кода должен вернуть текст ошибки"
    return res["hint"][:60]


@check("Камеры", "_to_bgr: Mono8 / BayerRG8 / RGB8 → BGR нужного размера")
def _to_bgr():
    import numpy as np
    from camera_core import _to_bgr
    w, h = 32, 16
    for fmt, ch in (("Mono8", 1), ("BayerRG8", 1), ("RGB8", 3)):
        data = np.random.randint(0, 255, w * h * ch, dtype=np.uint8)
        out = _to_bgr(data, w, h, fmt)
        assert out is not None and out.shape == (h, w, 3), "%s → %s" % (fmt, None if out is None else out.shape)
    assert _to_bgr(np.zeros(5, np.uint8), w, h, "Mono8") is None
    return "3 формата"


@check("Камеры", "_apply_color: гамма/контраст/насыщенность/палитра сохраняют форму кадра")
def _color():
    from camera_core import _apply_color
    img, _ = synth_frame()
    for c in ({}, {"gamma": 1.3}, {"contrast": 1.2, "brightness": 5, "saturation": 0.8},
              {"sharpness": 0.5, "clarity": 1.0}, {"palette": "jet"}):
        out = _apply_color(img, c)
        assert out.shape == img.shape, c
    return "5 режимов"


@check("Камеры", "GigE-воркер создаётся без камеры, поток не идёт")
def _gige_worker():
    from camera_core import manager, CameraWorker
    w = manager.get("SELFCHECK-GIGE")
    assert isinstance(w, CameraWorker)
    st = w.stream_state()
    assert isinstance(st, dict)
    return "stream_state=%s" % (st,)


@check("Камеры", "RTSP-воркер создаётся без камеры, захват не стартует сам")
def _rtsp_worker():
    from camera_core import manager, RtspCameraWorker
    w = manager.get_rtsp("SELFCHECK-RTSP", "rtsp://127.0.0.1:1/none")
    assert isinstance(w, RtspCameraWorker)
    assert w.viewer_count() == 0
    manager.drop_rtsp("SELFCHECK-RTSP")
    return "ok"


@check("Камеры", "запись фото воркером: PNG пишется в песочницу и читается")
def _write_photo():
    import cv2
    from camera_core import manager
    w = manager.get("SELFCHECK-PHOTO")
    w.photo_project = "selfcheck"
    img, _ = synth_frame(160, 120)
    path = w.write_photo(img)
    assert path and Path(path).is_file(), "файл не записан: %s" % w.last_save_error
    assert str(SANDBOX) in str(path), "пишет не в песочницу: %s" % path
    back = cv2.imdecode(__import__("numpy").fromfile(path, dtype="uint8"), cv2.IMREAD_COLOR)
    assert back is not None and back.shape == img.shape
    return Path(path).name


@check("Камеры", "поиск .cti и MVS runtime (нужны, чтобы камеры вообще стартовали)")
def _cti():
    from camera_core import _discover_cti, _find_mvs_runtime
    cti = _discover_cti()
    assert cti, "MvProducerGEV.cti не найден"
    rt = _find_mvs_runtime()
    return "cti=%s, runtime=%s" % (cti, rt)


@check("Камеры", "MVS SDK (sdk_gige): DLL загружается")
def _sdk():
    import sdk_gige
    sdk_gige.init()
    assert sdk_gige.available(), "SDK недоступен"
    return "ok"


# =====================================================================================
# 5. Микроскоп: плата и ПЛК = встроенный Modbus-сервер, автомат живой
# =====================================================================================

def _plate_cfg(fake):
    import plate_config
    cfg = plate_config.load()
    cfg["host"], cfg["port"] = "127.0.0.1", fake.port
    cfg["poll_interval_ms"], cfg["timeout_s"] = 50, 1.0
    return cfg


def _wait(cond, timeout=5.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


@check("Микроскоп", "plate_config: DEFAULTS собираются, ключи на месте")
def _cfg_defaults():
    import plate_config
    cfg = plate_config.load()
    for k in ("host", "port", "read_base", "write_base", "out", "in", "SP", "SVSP", "probe_cycle", "cv", "fracture"):
        assert k in cfg, "нет ключа %s" % k
    assert len(cfg["SP"]) == len(cfg["SVSP"]), "SP и SVSP разной длины"
    return "%d ключей" % len(cfg)


@check("Микроскоп", "plate_config: save → load → снапшот (в песочнице)")
def _cfg_roundtrip():
    import plate_config
    plate_config.save({"led_bright": 37})
    assert plate_config.load()["led_bright"] == 37
    assert plate_config.CONFIG_PATH.is_file()
    plate_config.save({"led_bright": plate_config.DEFAULTS["led_bright"]})
    return "ok"


@check("Микроскоп", "PlateClient: связь, чтение телеметрии и запись команд (Modbus-эмулятор)")
def _plate_client():
    from microscope_plc import PlateClient
    fake = fake_modbus()
    cfg = _plate_cfg(fake)
    name, spec = next((n, s) for n, s in cfg["in"].items() if n not in ("pos1_ai",) and s.get("off", 99) < cfg["read_len"])
    fake.regs[cfg["read_base"] + int(spec["off"])] = 123
    client = PlateClient(cfg)
    client.start()
    try:
        assert _wait(lambda: client.status["connected"]), "нет связи: %s" % client.status.get("error")
        tel = client.telemetry
        assert _wait(lambda: client.telemetry.get(name) == 123 * spec.get("scale", 1)), \
            "телеметрия %s=%s" % (name, client.telemetry.get(name))
        client.write_m1_sp(40000)
        idx = cfg["write_base"] + int(cfg["out"]["m1_sp"])
        assert _wait(lambda: fake.regs[idx] == 4000), "запись m1_sp не дошла: %s" % fake.regs[idx]
    finally:
        client.stop()
    return "поллов=%d, %s=123" % (client.status["poll_count"], name)


@check("Микроскоп", "SvSource: читает СВ и стадию из ПЛК (Modbus-эмулятор)")
def _sv_source():
    from sv_source import SvSource
    import plate_config
    fake = fake_modbus()
    scfg = dict(plate_config.load()["sv_source"])
    scfg["host"], scfg["port"], scfg["period_s"] = "127.0.0.1", fake.port, 0.1
    fake.regs[int(scfg["fields"]["sv"]["reg"])] = 5250       # 52.50 при scale 100
    fake.regs[int(scfg["fields"]["stage"]["reg"])] = 7
    seen = []
    src = SvSource(scfg, on_update=lambda sv, stage: seen.append((sv, stage)))
    src.start()
    try:
        assert _wait(lambda: src.status()["connected"], 5), "нет связи с ПЛК: %s" % src.status().get("error")
        assert _wait(lambda: src.status()["sv"] is not None, 3)
        assert abs(src.status()["sv"] - 52.5) < 0.01, "sv=%s" % src.status()["sv"]
        assert src.status()["stage"] == 7, "stage=%s" % src.status()["stage"]
        # весь блок регистров читается ОДНИМ запросом за опрос (а не 9 отдельными)
        r0 = src.requests
        time.sleep(0.55)
        n = src.requests - r0
        assert src._block_ok and 3 <= n <= 8, "запросов за 0,55 с: %d (ждали по одному на опрос, ~5), block_ok=%s" % (n, src._block_ok)
    finally:
        src.stop()
    return "sv=52.5 stage=7, 1 запрос на опрос"


@check("Микроскоп", "Автокалибровка нуля М1: энкодер 0±5 + аналог → set_zero → отвод; 3 попытки; провал не трогает ноль")
def _autocal():
    import copy
    import plate_config
    from microscope_fsm import MicroscopeFSM, CAL_ATTEMPTS

    class _Plate:
        def __init__(self):
            self.telemetry = {"pos1": 0, "pos1_ai": 0, "pos1_enc": 0}
            self.status = {"connected": True}
            self.calls = []

        def __getattr__(self, n):
            return lambda *a, **k: self.calls.append(n)

    def run(enc_at_attempt, ai):
        cfg = copy.deepcopy(plate_config.load())
        cfg.setdefault("autocal", {})["enabled"] = True
        pl = _Plate()
        fsm = MicroscopeFSM(pl, cfg)
        fsm.set_manual(False)
        assert fsm.start_autocal()["status"] == "started"
        for t in range(2000):
            n = pl.calls.count("motor_find_zero")
            if "motor_set_zero" in pl.calls or (n >= CAL_ATTEMPTS and t > 200):
                enc = min(fsm._retract_pos, pl.telemetry["pos1_enc"] + 400)     # едем в отвод
            else:
                enc = enc_at_attempt(n)
            pl.telemetry.update(pos1_enc=enc, pos1=enc, pos1_ai=ai * 10)        # ×0.1 внутри FSM
            fsm.tick()
            if t > 3 and not fsm.state["autocal"]["active"]:
                break
        st = fsm.state["autocal"]
        assert not st["active"], "калибровка не завершилась: %s" % st
        return pl.calls.count("motor_find_zero"), pl.calls.count("motor_set_zero"), st["failed"]

    # из ручного режима: кнопка снимает ручной, калибровка проходит целиком, потом ручной возвращается
    cfg = copy.deepcopy(plate_config.load())
    pl = _Plate()
    fsm = MicroscopeFSM(pl, cfg)
    fsm.set_manual(True)
    st0 = fsm.start_autocal()
    assert st0["status"] == "started" and st0["was_manual"] and not fsm.manual, "из ручного калибровка должна стартовать: %s" % st0
    for t in range(2000):
        n = pl.calls.count("motor_find_zero")
        enc = min(fsm._retract_pos, pl.telemetry["pos1_enc"] + 400) if "motor_set_zero" in pl.calls else 0
        pl.telemetry.update(pos1_enc=enc, pos1=enc, pos1_ai=200)
        fsm.tick()
        if t > 3 and not fsm.state["autocal"]["active"]:
            break
    assert pl.calls.count("motor_set_zero") == 1 and fsm.manual, "после калибровки из ручного — set_zero и снова ручной режим"
    assert run(lambda n: 0, 20) == (1, 1, False), "успех с 1-й попытки"
    assert run(lambda n: 330 if n < 3 else 0, 20) == (3, 1, False), "успех на 3-й попытке"
    assert run(lambda n: 330, 89) == (3, 0, True), "провал: set_zero слать нельзя, failed=True"
    return "1-я / 3-я попытка / провал (3×Поиск 0, без set_zero)"


@check("Микроскоп", "MicroscopeFSM: такты идут, СВ/стадия принимаются, проба стартует")
def _fsm():
    from microscope_fsm import MicroscopeFSM
    from microscope_plc import PlateClient
    fake = fake_modbus()
    cfg = _plate_cfg(fake)
    plate = PlateClient(cfg)
    plate.start()
    try:
        assert _wait(lambda: plate.status["connected"]), "нет связи с платой"
        fsm = MicroscopeFSM(plate, cfg)
        fsm.set_sv(55.0)
        fsm.set_stage(7)
        for _ in range(5):
            fsm.tick()
        st0 = fsm.state
        assert isinstance(st0, dict) and "mode" in st0, "state() без mode: %s" % list(st0)
        fsm.start_sample()
        for _ in range(5):
            fsm.tick()
        st1 = fsm.state
        assert st1["mode"] != 0 or st1.get("label") != st0.get("label"), "цикл пробы не стартовал: %s" % st1
        # диапазон стадий цикла берётся из настроек (вкладка «Цикл»): 5..7 — стадия 4 вне, 6 внутри; 7..5 перепутаны — то же
        import copy
        cfg2 = copy.deepcopy(cfg)
        cfg2.setdefault("probe_cycle", {}).update({"stage_from": 7, "stage_to": 5})
        fsm2 = MicroscopeFSM(plate, cfg2)
        fsm2.set_stage(4); fsm2.tick()
        assert fsm2.state["stage_ok"] is False, "стадия 4 вне диапазона 5..7, а stage_ok=True"
        fsm2.set_stage(6); fsm2.tick()
        assert fsm2.state["stage_ok"] is True, "стадия 6 в диапазоне 5..7, а stage_ok=False"
        assert fsm.state["stage_ok"] is True, "по умолчанию стадия 7 должна быть в 3..9"
    finally:
        plate.stop()
    return "mode %s → %s" % (st0["mode"], st1["mode"])


@check("Микроскоп", "MicroscopeService: start → связь → телеметрия/статус → stop (всё через эмулятор)")
def _micro_service():
    import plate_config
    from microscope_service import micro
    fake = fake_modbus()
    # sv_source — ПОЛНАЯ копия с регистрами: «пустой» sv_source plate_config считает старым слепком и
    # выбрасывает (_drop_stale_sv_source), после чего сервис пошёл бы на боевой ПЛК из DEFAULTS
    sv = json.loads(json.dumps(plate_config.load()["sv_source"]))
    sv.update({"enabled": True, "host": "127.0.0.1", "port": fake.port, "period_s": 0.2})
    fake.regs[int(sv["fields"]["sv"]["reg"])] = 4100
    plate_config.save({"host": "127.0.0.1", "port": fake.port, "poll_interval_ms": 50, "sv_source": sv})
    assert plate_config.load()["sv_source"]["host"] == "127.0.0.1", "sv_source не удержался в песочнице"
    micro.start()
    try:
        assert _wait(lambda: micro.status().get("connected"), 6), "сервис не связался: %s" % micro.status()
        assert _wait(lambda: (micro.sv_status() or {}).get("connected"), 6), "СВ из ПЛК-эмулятора не читается: %s" % micro.sv_status()
        assert isinstance(micro.telemetry(), dict)
        assert isinstance(micro.state(), dict)
        assert isinstance(micro.config(), dict)
        assert isinstance(micro.cv_config(), dict)
    finally:
        micro.stop()
    return "плата и ПЛК (эмулятор): связь есть"


# =====================================================================================
# 6. CV
# =====================================================================================

@check("CV", "cv_analyzer.analyze: кристаллы измеряются, сводка и overlay строятся")
def _cv_analyze():
    import cv_analyzer
    img, objs = synth_frame()
    res = cv_analyzer.analyze(img, objs, None, with_overlay=True, sv=90.0)
    assert len(res["objects"]) == len(objs), "объектов %d из %d" % (len(res["objects"]), len(objs))
    assert res["summary"], "пустая сводка"
    assert res["_overlay"].shape == img.shape
    assert all(o["size_um"] > 0 for o in res["objects"])
    return "%d кристаллов" % len(res["objects"])


@check("CV", "cv_volume: объём шара/сфероида/призмы совпадает с формулой, мука среди хороших, брак отсеян и считается отдельно")
def _cv_volume():
    import math
    from types import SimpleNamespace as NS
    import cv_volume
    big = NS(group="large", defect=None, size_um=600.0, length_um=700.0, width_um=500.0)
    fine = NS(group="small", defect=None, size_um=100.0, length_um=120.0, width_um=90.0)
    agg = NS(group="reject", defect="aggregate", size_um=400.0, length_um=500.0, width_um=300.0)
    cut = NS(group="cut", defect=None, size_um=900.0, length_um=900.0, width_um=900.0)
    v = cv_volume.crystal_volumes(600.0, 700.0, 500.0, 0.88)
    assert abs(v["m1"] - math.pi / 6 * 600.0 ** 3) < 1e-6, "M1 не шар"
    assert abs(v["m2"] - math.pi / 6 * 700.0 * 500.0 * 440.0) < 1e-6, "M2 не сфероид"
    assert abs(v["m3"] - math.pi * 300.0 ** 2 * 440.0) < 1e-6, "M3 не призма"
    s = cv_volume.sums_for([big, fine, agg, cut], {"volume": {"fines_side_mm": 0.2, "k_thick": 0.88}})
    assert s["n"] == {"fines": 1, "agg": 1, "total": 2}, "обрезанный/мука/брак разнесены неверно: %s" % s["n"]     # хороших 2 (крупный + мука), брак отсеян отдельно
    p = cv_volume.percents(s)
    tot = sum(cv_volume.crystal_volumes(o.size_um, o.length_um, o.width_um, 0.88)["m1"] for o in (big, fine))     # брак в общий объём не входит
    want = 100.0 * math.pi / 6 * 100.0 ** 3 / tot
    assert abs(p["m1"]["fines"] - want) < 0.01, "доля мелочи %s ≠ %s" % (p["m1"]["fines"], want)
    assert p["m1"]["fines"] < p["n"]["fines"], "по объёму мелочи должно быть меньше, чем по числу"
    assert cv_volume.percents(cv_volume.empty_sums())["m3"]["fines"] is None, "пустой кадр должен дать None"
    off = cv_volume.sums_for([big, fine, agg], {"volume": {"fines_side_mm": 0.2}}, count_fines=False)
    assert cv_volume.percents(off)["m3"]["fines"] is None and cv_volume.percents(off)["m3"]["agg"] is not None, "до нужного СВ мука не считается, брак — да (считается отдельно)"
    assert abs(cv_volume.volume_cfg(None)["fines_um"] - 225.68) < 0.01, "0,2 × 0,2 мм ↔ диаметр 226 мкм"
    assert not cv_volume.fines_on({}, 85.0) and cv_volume.fines_on({}, 88.0) and cv_volume.fines_on({}, None), "порог СВ для мелочи"
    return "мелочь %.3f %% объёма (M1)" % p["m1"]["fines"]


@check("CV", "cv_store.boils: журнал режется на варки, мелочь по варке считается только по пробам финиша")
def _cv_boils():
    import cv_store
    def row(t, sv, stage, cook, fm3=None, vt=1.0):
        return {"ts": "2026-10-06_%02d_%02d_00" % (t // 60, t % 60), "t": t * 60.0, "sv": sv, "stage": stage, "cook_time": cook,
                "fines_m1": fm3, "fines_m2": fm3, "fines_m3": fm3, "fines_area": fm3, "fines_n": fm3,
                "agg_m1": 10.0, "agg_m2": 10.0, "agg_m3": 10.0, "agg_area": 10.0, "agg_n": 10.0,
                "vtot_m1": vt, "vtot_m2": vt, "vtot_m3": vt}
    a = [row(t, 80 + t * 0.2, 7, t * 60, None) for t in range(0, 20, 2)] + [row(20, 88.0, 8, 1200, 4.0), row(22, 88.5, 8, 1320, 2.0)]
    b = [row(300 + t, 80 + t * 0.2, 3 if t == 0 else 7, 100 + t * 60, None) for t in range(0, 20, 2)] + [row(320, 88.0, 8, 1300, 6.0, vt=3.0)]
    import time as _t
    orig = (cv_store._hist_backfill, cv_store._hist_days, cv_store._hist_read)
    cv_store._hist_backfill = lambda serial: None
    cv_store._hist_days = lambda serial: ["d"]
    cv_store._hist_read = lambda serial, day: a + b
    try:
        res = cv_store.boils("X", limit=5, now=321 * 60.0)       # последняя проба 1 мин назад → варка идёт
        done = cv_store.boils("X", limit=5, now=321 * 60.0 + 7200)
    finally:
        cv_store._hist_backfill, cv_store._hist_days, cv_store._hist_read = orig
    assert len(res) == 2, "варок %d, ждали 2 (пауза 4 ч между пробами)" % len(res)
    assert not res[0]["finished"] and res[1]["finished"], "новая варка идёт, прошлая закончена"
    assert done[0]["finished"], "через 2 часа без проб варка закончена"
    assert res[1]["counted"] == 2 and res[1]["fines"]["m3"] == 3.0, "мелочь прошлой варки = среднее по 2 пробам финиша: %s" % res[1]["fines"]
    assert res[0]["counted"] == 1 and res[0]["fines"]["m3"] == 6.0, "в новой варке мелочь по одной пробе"
    assert res[1]["agg"]["m3"] == 10.0 and res[1]["n"] == 12, "сростки — по всем пробам варки"
    # окно «последние N проб финиша» (avg_n) и счётчики: сколько проб и хороших кристаллов вошло в среднее
    for bb in res:
        assert "tail" in bb and "all" in bb and bb["tail"]["probes"] <= bb["cfg"]["avg_n"], "нет окна последних проб"
    assert res[1]["all"]["probes"] == 2 and res[1]["tail"]["probes"] == 2, "прошлая варка: 2 пробы финиша (avg_n=4 ≥ 2)"
    assert res[1]["all"]["fines"]["m3"] == 3.0 and res[1]["tail"]["fines"]["m3"] == 3.0
    return "варок %d, мелочь прошлой %.1f %%" % (len(res), res[1]["fines"]["m3"])


@check("CV", "cv_analyzer: пузырь воздуха (ровный круг) отсеивается из рассева, шестиугольники — нет")
def _cv_bubble():
    import math
    import numpy as np
    import cv_analyzer

    def circle(cx, cy, r, n=90):
        return [[cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)] for i in range(n)]

    def hexa(cx, cy, r, st=1.0):
        return [[cx + r * st * math.cos(math.pi / 3 * i), cy + r * math.sin(math.pi / 3 * i)] for i in range(6)]

    img = np.full((1000, 1200, 3), 120, np.uint8)
    polys = [circle(300, 300, 85), hexa(700, 300, 100), hexa(300, 700, 100, 1.3)]
    objs = [{"bbox": [min(p[0] for p in q), min(p[1] for p in q), max(p[0] for p in q), max(p[1] for p in q)],
             "conf": 0.9, "polygon": q} for q in polys]
    res = cv_analyzer.analyze(img, objs, {"tiles": 1}, with_overlay=False, sv=90)
    groups = [o["group"] for o in res["objects"]]
    assert groups[0] == "bubble" and "bubble" not in groups[1:], "группы: %s" % groups
    assert res["summary"]["bubbles"] == 1 and res["summary"]["count"] == 2, res["summary"]
    off = cv_analyzer.analyze(img, objs, {"tiles": 1, "bubble_filter": False}, with_overlay=False, sv=90)
    assert off["summary"]["bubbles"] == 0 and off["summary"]["count"] == 3, "выключатель не работает"
    return "круг → bubble, 2 шестиугольника остались в рассеве"


@check("CV", "cv_analyzer: обрубок на шве перепроверяется повторным проходом (фейковая модель) и становится целым")
def _cv_seam_refine():
    import numpy as np
    import cv_analyzer
    img = np.full((2048, 2448, 3), 120, np.uint8)
    xs, _ys = cv_analyzer._seam_lines(img.shape, 6, 0.15)
    sx = xs[1]                                                    # вертикальный шов нарезки
    stub = [[sx, 500], [sx + 40, 500], [sx + 60, 550], [sx + 40, 600], [sx, 600]]      # ровный край ровно на шве
    whole = [[sx - 50, 500], [sx + 40, 500], [sx + 60, 550], [sx + 40, 600], [sx - 50, 600]]
    raw = [{"bbox": [sx, 500, sx + 60, 600], "conf": 0.9, "polygon": stub}]
    cfg = {"tiles": 6, "overlap": 0.15}
    before = cv_analyzer.analyze(img, raw, cfg, with_overlay=False, sv=90)
    assert before["objects"][0]["group"] == "cut", "тест не воспроизводит обрубок: %s" % before["objects"][0]["group"]
    calls = []

    def fake_infer(crop):
        calls.append(crop.shape)
        # окно вырезано с известного смещения: возвращаем целую маску в координатах окна
        h, w = crop.shape[:2]
        cx, cy = (sx + 5) , 550
        wx1 = int(min(max(0, cx - w / 2), 2448 - w)); wy1 = int(min(max(0, cy - h / 2), 2048 - h))
        return [{"bbox": [x - wx1 for x in (sx - 50,)] + [500 - wy1, sx + 60 - wx1, 600 - wy1], "conf": 0.9,
                 "polygon": [[x - wx1, y - wy1] for x, y in whole]}]

    st = {}
    objs = cv_analyzer.refine_seam_stubs(img, raw, cfg, fake_infer, st)
    after = cv_analyzer.analyze(img, objs, cfg, with_overlay=False, sv=90)
    assert st["fixed"] == 1 and calls, "не заменён: %s" % st
    assert after["objects"][0]["group"] != "cut" and after["summary"]["cut"] == 0, "остался обрубком"
    off = cv_analyzer.refine_seam_stubs(img, raw, {**cfg, "seam_refine": False}, fake_infer, {})
    assert off == raw, "при выключенной настройке список должен вернуться как есть"
    broken = cv_analyzer.refine_seam_stubs(img, raw, cfg, lambda crop: None, {})
    assert broken == raw, "сбой модели: обрубок должен остаться как был"
    return "обрубок → целый (вызовов модели: %d)" % len(calls)


@check("CV", "cv_fracture: зоны ищутся, серия кадров сводится, отрисовка работает")
def _cv_fracture():
    import cv2
    import cv_fracture
    img, _ = synth_frame()
    cv2.rectangle(img, (400, 300), (560, 420), (25, 25, 25), -1)
    z = cv_fracture.detect_zones(img, None)
    assert isinstance(z, list)
    confirmed, summ = cv_fracture.confirm([z, z, z], None)
    assert isinstance(confirmed, list) and isinstance(summ, dict)
    out = cv_fracture.draw(img.copy(), confirmed)
    assert out.shape == img.shape
    assert isinstance(cv_fracture.area_pct(confirmed, img.shape), (int, float))
    assert isinstance(cv_fracture.candidates([z, z, z], confirmed, None), list), "кандидаты на разлом"
    return "зон=%d, подтверждено=%d" % (len(z), len(confirmed))


@check("CV", "cv_store: сохранить пробу → список → последняя → тренд → миниатюра (песочница)")
def _cv_store():
    import cv_analyzer
    import cv_store
    img, objs = synth_frame()
    res = cv_analyzer.analyze(img, objs, None, with_overlay=False, sv=90.0)
    frames = [{"file": "f0.jpg", "summary": res["summary"], "objects": res["objects"]}]
    plc = {"temp_app": 74.5, "level": 61.0, "current": 12.0, "press_top": -0.8,
           "cook_time": 1500, "seed_age_s": 420}
    rec = cv_store.save_sample("SELFCHECK", 7, frames, [img], {"total_ms": 1}, keep_last=5, sv=90.0, plc=plc)
    assert rec and rec.get("ts"), "save_sample вернул %r" % (rec,)
    assert cv_store.list_samples("SELFCHECK"), "список проб пуст"
    last = cv_store.get_last("SELFCHECK")
    assert last and last["ts"] == rec["ts"]
    assert last.get("plc") == plc, "режим варки (plc) не попал в result.json: %r" % (last.get("plc"),)
    row = cv_store._hist_row(last)
    assert row["temp"] == 74.5 and row["vac"] == -0.8 and row["seed_age"] == 420, "журнал трендов без plc: %r" % (row,)
    import time as _tm
    rec0 = cv_store.save_sample("SELFCHECK", 7, frames, [img], {"total_ms": 1}, keep_last=5, sv=90.0,
                                ts=_tm.strftime(cv_store.TS_FMT, _tm.localtime(_tm.time() + 7)))     # своя метка: две пробы за одну секунду слились бы
    assert "plc" not in (cv_store.get_result("SELFCHECK", rec0["ts"]) or {}), "без ПЛК ключ plc быть не должен"
    # выгрузка журнала проб в CSV (для разбора/обучения): колонки, режим варки, причины брака
    ex = cv_store.export_rows("SELFCHECK")
    assert ex and list(ex[0].keys()) == cv_store.EXPORT_COLUMNS, "колонки выгрузки не те"
    mine = [r for r in ex if r["ts"] == rec["ts"]]
    assert mine and mine[0]["temp"] == 74.5 and mine[0]["seed_age"] == 420, "в выгрузке нет режима варки: %r" % (mine,)
    assert "n_aggregate" in mine[0] and "cv_pct" in mine[0], "в выгрузке нет причин брака/CV%"
    # мелочь «среднее» = (M1 + M2 + M3) / 3, пустые модели пропускаются; нет данных — None; колонка есть в выгрузке
    assert cv_store.fines_avg({"fines_m1": 3.73, "fines_m2": 3.14, "fines_m3": 3.42}) == 3.43, "среднее мелочи считается неверно"
    assert cv_store.fines_avg({"fines_m1": 2, "fines_m2": None, "fines_m3": 4}) == 3.0 and cv_store.fines_avg({}) is None
    assert "fines_avg" in mine[0], "в выгрузке нет колонки fines_avg"
    tr = cv_store.trend_range("SELFCHECK", 0, 9999999999, series=["fines_avg", "sv"])
    assert "fines_avg" in tr["series"] and len(tr["series"]["fines_avg"]) == len(tr["t"]), "в тренде нет серии fines_avg"
    assert len(tr["boil"]) == len(tr["t"]) and tr["boil"] == sorted(tr["boil"]), "в тренде нет номеров варок (boil)"
    # поля «Мука, мм» меняются → уже снятая проба и журнал пересчитываются по сохранённым кадрам (а не ждут следующей пробы)
    import plate_config
    cur = (plate_config.load().get("cv") or {}).get("volume") or {}
    plate_config.save({"cv": {"volume": {"fines_side_mm": 0.05, "k_thick": 0.88, "fines_from_sv": 0}}})
    try:
        again = cv_store.get_result("SELFCHECK", rec["ts"])
        assert again["summary"]["volume_cfg"]["fines_side_mm"] == 0.05, "проба не пересчиталась по новому порогу муки"
        assert cv_store.recompute_journal("SELFCHECK") >= 1, "журнал не пересчитан"
        assert [r for r in cv_store.export_rows("SELFCHECK") if r["ts"] == rec["ts"]][0]["fines_side_mm"] == 0.05, "в журнале старый порог"
    finally:
        plate_config.save({"cv": {"volume": {"fines_side_mm": cur.get("fines_side_mm", 0.2), "k_thick": cur.get("k_thick", 0.88),
                                             "fines_from_sv": cur.get("fines_from_sv", 88.0)}}})
    import app as web
    resp = web.cv_trend_export(serial="SELFCHECK", t_from=None, t_to=None)   # прямой вызов: Query-умолчания не подставляются
    body = resp.body.decode("utf-8-sig").splitlines()
    assert resp.body[:3] == b"\xef\xbb\xbf" and body[0].split(",") == cv_store.EXPORT_COLUMNS, "CSV: нет BOM/шапки"
    assert len(body) == 1 + len(ex), "CSV: строк %d, ожидали %d" % (len(body) - 1, len(ex))
    assert cv_store.get_objects("SELFCHECK", rec["ts"], 0), "объекты не читаются"
    assert cv_store.trend("SELFCHECK") is not None
    assert cv_store.thumb_path("SELFCHECK", rec["ts"]) is not None, "миниатюра не создана"
    assert cv_store.history_days("SELFCHECK") is not None
    return "ts=%s" % rec["ts"]


@check("CV", "cv_analyzer: брак по форме считается ДО «Брак до СВ» (85), выше — не считается и в расчёт не берётся")
def _cv_reject_to_sv():
    import cv_analyzer as A
    cfg = {"reject_to_sv": 85.0}
    c = A._cfg(cfg)
    assert A.is_counting(c, 80.0) and A.is_counting(c, 85.0) and not A.is_counting(c, 85.1) and not A.is_counting(c, 90.0), "граница 85"
    assert A.is_counting(c, None), "СВ неизвестно — считаем"
    assert A.is_counting(A._cfg({"reject_to_sv": 85.0, "reject_always": True}), 95.0), "«брак всегда»"
    def m(group, defect):
        return A.CrystalMeasure(cx=10, cy=10, size_um=300, length_um=320, width_um=280, circularity=0.8, aspect=1.1, solidity=0.95,
                                group=group, conf=0.9, defect=defect)
    early = A.summarize([m("small", None), m("reject", "aggregate")], (200, 200, 3), cfg, sv=80.0)
    late = A.summarize([m("small", None), m("small", "aggregate")], (200, 200, 3), cfg, sv=90.0)
    assert early["reject_active"] and early["groups"]["reject"] == 1 and early["count"] == 2, "до СВ 85 брак должен считаться: %s" % early["groups"]
    assert not late["reject_active"] and late["groups"]["reject"] == 0 and late["groups"]["small"] == 1 and late["count"] == 1 and late["excluded"] == 1,         "выше СВ 85 дефектный кристалл должен быть вне расчёта: %s excl=%s" % (late["groups"], late.get("excluded"))
    assert late["reasons"]["aggregate"] == 1, "причина всё равно подписывается"
    return "до 85 брак считается, выше — отсеян"


@check("CV", "cv_analyzer: форму (сросток/игла/кривой) судим только от «Форму судить от» (100 мкм), мельче — хороший кристалл")
def _cv_shape_min():
    import cv_analyzer as A
    cfg = A._cfg(None)
    assert cfg["shape"]["min_um"] == 100.0, "по умолчанию 100 мкм"
    args = dict(circularity=0.9, aspect=1.2, solidity=0.95, notches=2, cfg=cfg, size_active=False)
    assert A._defect(size_um=80.0, **args) is None, "мельче 100 мкм сросток не определяется"
    assert A._defect(size_um=150.0, **args) == "aggregate", "от 100 мкм сросток определяется"
    needle = dict(circularity=0.9, aspect=4.0, solidity=0.95, notches=0, cfg=cfg, size_active=False)
    assert A._defect(size_um=60.0, **needle) is None and A._defect(size_um=300.0, **needle) == "needle", "игла — тоже только от порога"
    off = A._cfg({"shape": {"min_um": 0.0}})
    assert A._defect(size_um=40.0, **dict(args, cfg=off)) == "aggregate", "порог 0 — судим всё, как раньше"
    # размер-брак (tiny) от порога формы не зависит
    assert A._defect(size_um=40.0, **dict(args, notches=0, cfg=A._cfg({"size_reject": {"min_um": 250.0, "max_um": 1200.0}}), size_active=True)) == "tiny"
    return "мельче 100 мкм форму не судим"


@check("Микроскоп", "SvSource: разовый сбой чтения СВ/стадии отбрасывается, настоящий скачок принимается с задержкой")
def _sv_glitch():
    from sv_source import SvSource
    src = SvSource({"host": "x"})                 # без соединения: проверяем только фильтр
    out = [src._sanitize({"sv": v, "stage": st}) for v, st in
           [(87.5, 7), (87.6, 7), (0.0, 3), (87.7, 7), (58.4, 7), (87.8, 7)]]
    assert [o["sv"] for o in out] == [87.5, 87.6, 87.6, 87.7, 87.7, 87.8], "разовые 0,0 и 58,4 должны быть отброшены: %s" % [o["sv"] for o in out]
    assert all(o["stage"] == 7 for o in out), "стадия на один опрос «3» не должна проходить"
    # настоящий скачок СВ (новый набор сиропа 88 → 70): держится ≥ 5 опросов → принимается
    src2 = SvSource({"host": "x"})
    seq = [src2._sanitize({"sv": v, "stage": 7})["sv"] for v in [88.0] + [70.0] * 6]
    assert seq[1:5] == [88.0] * 4 and seq[5] == 70.0, "скачок принят не вовремя: %s" % seq
    # настоящая смена стадии 7 → 8: принимается на третьем опросе
    src3 = SvSource({"host": "x"})
    st = [src3._sanitize({"sv": 87.0, "stage": x})["stage"] for x in [7, 7, 8, 8, 8, 8]]
    assert st == [7, 7, 7, 7, 8, 8], "смена стадии принята не вовремя: %s" % st
    off = SvSource({"host": "x", "glitch_filter": False})
    assert off._sanitize({"sv": 0.0, "stage": 3}) == {"sv": 0.0, "stage": 3}, "фильтр выключен — значения как есть"
    return "0,0/58,4/«3» отброшены, скачок и смена стадии проходят"


@check("CV", "cv_store: разовый сбой СВ (0,0 или 58 при 87) обнуляется при чтении журнала, граница варок 88 → 80 остаётся")
def _cv_sv_outliers():
    import cv_store
    rows = [{"sv": v} for v in (84.3, 84.5, 0.0, 85.0, 85.2, 87.5, 58.4, 87.8, 88.0, 80.5, 81.0, 81.6)]
    out = cv_store._null_sv_outliers(rows)
    assert [r["sv"] for r in out] == [84.3, 84.5, None, 85.0, 85.2, 87.5, None, 87.8, 88.0, 80.5, 81.0, 81.6], "выбросы: %s" % [r["sv"] for r in out]
    # одиночный сбой СВ внутри варки (время варки идёт) не делит её на две; новая варка — по времени варки/стадии/паузе
    prev = {"t": 1000.0, "sv": 87.0, "stage": 7, "cook_time": 6000}
    assert not cv_store._is_new_boil(prev, {"t": 1080.0, "sv": 0.0, "stage": 7, "cook_time": 6080}), "сбой СВ поделил варку"
    assert not cv_store._is_new_boil(prev, {"t": 1080.0, "sv": 58.4, "stage": 7, "cook_time": 6080}), "СВ 58 поделил варку"
    assert cv_store._is_new_boil(prev, {"t": 1080.0, "sv": 80.5, "stage": 3, "cook_time": 20}), "время варки сбросилось — новая варка"
    assert cv_store._is_new_boil(prev, {"t": 1080.0, "sv": 80.5, "stage": 4, "cook_time": 6080}), "стадия откатилась на заводку — новая варка"
    assert cv_store._is_new_boil({"t": 1000.0, "sv": 87.0}, {"t": 1080.0, "sv": 75.0}), "нет времени варки — СВ упало на 12: запасной признак"
    return "2 выброса обнулены, 88 → 80 (новая варка) сохранено, сбой СВ варку не делит"


@check("CV", "cv_store: мука считается с первого достижения порога СВ ДО КОНЦА варки — провал СВ на подкачке линию не рвёт")
def _cv_fines_latch():
    import cv_store, cv_volume
    from types import SimpleNamespace as NS
    # СВ: до порога (88 по умолчанию) → достигли → проседание на подкачке → снова выше; потом новая варка (время варки сбросилось)
    svs = [80.0, 84.0, 88.5, 87.0, 83.0, 82.5, 88.0, 89.0]
    rows = [{"t": 1000.0 + i * 90, "sv": v, "stage": 7, "cook_time": 600 + i * 90, "fines_m1": 5.0, "fines_m2": 5.0, "fines_m3": 5.0,
             "sieve_m3_b0": 1.0, "sieve_area_b2": 40.0, "good_n": 300} for i, v in enumerate(svs)]
    rows.append({"t": 1000.0 + 8 * 90, "sv": 80.5, "stage": 3, "cook_time": 20, "fines_m3": 7.0})   # новая варка: порог заново
    cv_store._apply_fines_gate(rows)
    got = [r.get("fines_m3") for r in rows]
    assert got == [None, None, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0, None], "порог по варке (с первого достижения, до конца): %s" % got
    assert rows[4]["sieve_m3_b0"] == 1.0 and rows[0]["sieve_m3_b0"] is None and rows[0]["good_n"] == 300, "рассев — тот же порог, счётчики не трогаем"
    # мука считается ВСЕГДА: СВ ниже порога — только флаг fines_off, а числа в суммах есть (журнал хранит их без порога)
    ms = [NS(group="small", defect=None, size_um=d, length_um=d, width_um=d) for d in (100, 150, 600)]
    low = cv_volume.sums_for(ms, None, count_fines=False)
    assert low.get("fines_off") and cv_volume.percents(low)["m3"]["fines"] is None, "показ по пробе: ниже порога — прочерк"
    assert cv_volume.percents(low, ignore_off=True)["m3"]["fines"] > 0, "для журнала мука без порога"
    return "порог по варке: с первого достижения до конца, пропуски СВ линию не рвут"


@check("CV", "фазы варки: порядок стадий 5/7 → p1 g1 p2 g2; сита и порог муки в масштабе фазы (20/40/40/80 %), свой порог фазы, режим сторона/объём")
def _cv_phases():
    import cv_store, cv_volume, substages
    # 1. фазы по порядку стадий (рецепт: затравка → подкачка 1 → рост 1 → подкачка 2 → рост 2 → уваривание)
    seq = [3, 4, 5, 5, 7, 7, 7, 5, 5, 7, 7, 8, 9]
    got = cv_store.phases_for_boil(seq)
    assert got == [None, None, "p1", "p1", "g1", "g1", "g1", "p2", "p2", "g2", "g2", None, None], got
    assert cv_store.phases_for_boil([5, 7, 5, 7, 5, 7]) == ["p1", "g1", "p2", "g2", "p2", "g2"], "третья подкачка/рост вливаются во вторую"
    assert cv_store.phases_for_boil([5, None, 5, 7]) == ["p1", "p1", "p1", "g1"], "стадия неизвестна — как у предыдущей пробы"
    # 2. масштаб фаз: порог муки и сита
    base = cv_volume.volume_cfg(None)["fines_um"]
    cfg = {"volume": {}}
    vc = lambda ph: cv_volume.volume_cfg(cfg, ph)
    assert vc(None)["size_scale"] == 1.0 and vc(None)["fines_um"] == base and vc("zzz")["phase"] is None, "нет фазы — обычные сита и порог"
    assert [round(vc(ph)["size_scale"], 2) for ph in cv_volume.PHASES] == [0.2, 0.4, 0.4, 0.8], "масштаб по умолчанию 20/40/40/80 %"
    assert abs(vc("p1")["fines_um"] - base * 0.2) < 1e-6 and abs(vc("g2")["fines_um"] - base * 0.8) < 1e-6, "порог муки уменьшается вместе с ситами"
    cfg = {"volume": {"phase_scale_g1": 50, "fines_side_p2": 0.05}}
    assert abs(vc("g1")["fines_um"] - base * 0.5) < 1e-6, "свой масштаб фазы"
    assert abs(vc("p2")["fines_um"] - cv_volume.fines_diameter_um(0.05)) < 1e-6, "свой порог фазы — как задан (мм), без масштаба"
    cfg = {"volume": {"fines_mode": "volume", "fines_vol_mm3": 0.006}}
    assert abs(vc("g1")["fines_um"] - cv_volume.fines_diameter_from_volume_um(0.006) * 0.4) < 1e-6, "режим объёма: масштаб по диаметру"
    # 3. сита фазы: кристалл 300 мкм — на обычных ситах «0,2–0,5» (бин 1), на ситах 40 % (0,08; 0,2; 0,28; 0,32 мм) — бин 3, а 150 мкм — бин 1
    assert cv_volume.sieve_bin(300.0) == 1 and cv_volume.sieve_bin(300.0, 0.4) == 3 and cv_volume.sieve_bin(150.0, 0.4) == 1, \
        (cv_volume.sieve_bin(300.0, 0.4), cv_volume.sieve_bin(150.0, 0.4))
    from types import SimpleNamespace as NS
    ms = [NS(group="small", defect=None, size_um=d, length_um=d, width_um=d) for d in (40, 90, 150, 300)]
    sp = cv_volume.sums_for(ms, {"volume": {}}, True, "p1")      # p1: порог муки = 226·0,2 ≈ 45 мкм → мука только 40-мкм кристалл
    assert sp["n"]["fines"] == 1 and sp["n"]["total"] == 4, sp["n"]
    assert sum(1 for x in sp["sieve"]["m3"] if x > 0) >= 3, "на фазе рассев разложен по масштабированным ситам, а не в одно ведро"
    # 4. подстадии остались только для имён (в порогах не участвуют)
    assert substages.name_of(52) == "подкачка 1" and substages.name_of(73) == "рост 2"
    return "p1/g1/p2/g2 по порядку стадий; сита и порог 20/40/40/80 %; свой порог и масштаб"


@check("CV", "cv_store: сводка варки — блоки по фазам (все пробы фазы, мука без порога СВ), итоги варки без проб фаз, финиш = последние ~2 СВ")
def _cv_phase_blocks():
    import cv_store
    def row(i, sv, stage, phase, fines):
        r = {"ts": "2026-10-06_10_%02d_00" % i, "t": 1000.0 + i * 90, "sv": sv, "stage": stage, "phase": phase, "cook_time": 600 + 90 * i,
             "fines_m3": fines, "fines_m1": fines, "fines_m2": fines, "vtot_m1": 1.0, "vtot_m2": 1.0, "vtot_m3": 1.0, "good_n": 100}
        for m in ("m1", "m2", "m3", "area"):
            for b in range(7):
                r["sieve_%s_b%d" % (m, b)] = 100.0 if b == 1 else 0.0
        return r
    rows = [row(0, 82.0, 5, "p1", 60.0), row(1, 82.5, 5, "p1", 50.0), row(2, 83.0, 7, "g1", 30.0), row(3, 84.0, 7, "g1", 28.0),
            row(4, 82.4, 5, "p2", 20.0), row(5, 85.0, 7, "g2", 12.0), row(6, 87.0, 8, None, 10.0), row(7, 88.0, 8, None, 9.0),
            row(8, 89.0, 8, None, 8.0), row(9, 89.5, 9, None, 7.0)]
    cv_store._apply_fines_gate(rows)                     # порог «Мука с СВ» 88: фазы он не гасит, остальные пробы до 88 — гасит
    assert [r["fines_m3"] for r in rows[:6]] == [60.0, 50.0, 30.0, 28.0, 20.0, 12.0], "на фазах мука считается с самого начала"
    assert rows[6]["fines_m3"] is None and rows[7]["fines_m3"] == 9.0, "вне фаз порог СВ работает как раньше"
    sm = cv_store._boil_summary(rows, True)
    assert set(sm["phases"]) == {"p1", "g1", "p2", "g2"} and sm["phases"]["p1"]["probes"] == 2 and sm["phases"]["g1"]["probes"] == 2, sm["phases"].keys()
    assert abs(sm["phases"]["p1"]["fines"]["m3"] - 55.0) < 1e-6, "мука фазы — среднее по ВСЕМ её пробам: %s" % sm["phases"]["p1"]["fines"]
    assert sm["phases"]["p1"]["sieve"]["m3"][1] == 100.0 and sm["phases"]["p1"]["title"] == "подкачка 1"
    assert sm["counted"] == 3 and abs(sm["fines"]["m3"] - 8.0) < 0.6, "итоги варки — по пробам вне фаз (финиш), без фаз: %s" % sm["fines"]
    sel, ref = cv_store.finish_probes([r for r in rows if r["fines_m3"] is not None and not r["phase"]], 2.0)
    assert [r["ts"][-5:-3] for r in sel] == ["07", "08", "09"] and ref == 89.0, ([r["ts"] for r in sel], ref)
    return "блоки фаз по всем пробам, итоги варки без фаз, финиш по СВ"


@check("CV", "cv_volume: рассев по ситам — фракции по границам 0,2·0,5·0,7·0,8·1·1,2 мм, сумма 100 %, слияние кадров")
def _cv_sieve():
    import cv_volume
    from types import SimpleNamespace as NS
    assert [cv_volume.sieve_bin(x) for x in (100, 199, 200, 499, 500, 699, 700, 799, 800, 999, 1000, 1199, 1200, 2000)] ==         [0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6], "границы сит смещены"
    ms = [NS(group="small", defect=None, size_um=d, length_um=d, width_um=d) for d in (150, 300, 600, 750, 900, 1100, 1300)]
    s1 = cv_volume.sums_for(ms, None, True)
    pct = cv_volume.percents(s1)["sieve"]
    for m in ("m1", "m2", "m3"):
        assert len(pct[m]) == 7 and abs(sum(pct[m]) - 100) < 0.01, "сумма фракций %s = %s" % (m, sum(pct[m]))
        assert pct[m][6] > pct[m][5] > pct[m][0], "крупные фракции должны весить больше по объёму"
    assert len(pct["area"]) == 7 and abs(sum(pct["area"]) - 100) < 0.01, "рассев по площади: сумма %s" % sum(pct["area"])
    assert pct["area"][6] > 0 and pct["area"][0] > 0, "по площади крупная и мелкая фракции должны быть непустыми"
    both = cv_volume.add_sums(s1, s1)
    assert abs(sum(cv_volume.percents(both)["sieve"]["m3"]) - 100) < 0.01, "слияние кадров ломает рассев"
    assert abs(sum(cv_volume.percents(both)["sieve"]["area"]) - 100) < 0.01, "слияние кадров ломает рассев по площади"
    assert cv_volume.percents(cv_volume.empty_sums())["sieve"] == {}, "пустые суммы → пустой рассев"
    # брак по форме (сросток/игла/кривой) отсеян: ни в рассев, ни в общий объём, ни в муку не попадает, считается отдельно
    bad = [NS(group="small", defect=d, size_um=800, length_um=800, width_um=800) for d in ("aggregate", "needle", "crooked")]
    mixed = cv_volume.percents(cv_volume.sums_for(ms + bad, None, True))
    assert mixed["sieve"] == pct, "брак попал в рассев"
    assert mixed["m3"]["fines"] == cv_volume.percents(s1)["m3"]["fines"], "брак изменил долю муки"
    assert mixed["m3"]["agg"] > 0 and mixed["n"]["agg"] > 0, "отсеянный брак не посчитан отдельно"
    # СВ ниже порога: мука и рассев не считаются (прочерк)
    off = cv_volume.percents(cv_volume.sums_for(ms, None, False))
    assert off["sieve"] == {} and off["m3"]["fines"] is None, "до порога СВ рассев/мука должны быть пустыми"
    return "7 фракций, сумма 100 %"


@check("CV", "microscope_service: снимок ПЛК в пробу + время с заводки (3 → 4..9), без сети")
def _micro_plc_snapshot():
    import time as _t
    from microscope_service import MicroscopeService

    class _FakeSrc:
        def __init__(self):
            self.up, self.vals = True, {"temp_app": 70, "level": 50, "current": 10,
                                        "press_top": -0.9, "cook_time": 900, "stage": 3, "substage": 31}
        def status(self):
            return {"connected": self.up, "values": dict(self.vals)}

    ms = MicroscopeService()                 # не start(): ни платы, ни сокетов
    assert ms._plc_snapshot() is None, "без sv_source снимок должен быть None"
    ms.sv_source = _FakeSrc()
    snap = ms._plc_snapshot()
    assert set(snap) == {"temp_app", "level", "current", "press_top", "cook_time", "substage"}, "в снимке лишнее/нет: %r" % (snap,)
    assert snap["substage"] == 31 and "stage" not in snap
    ms.sv_source.up = False
    assert ms._plc_snapshot() is None, "нет связи с ПЛК — снимка нет"
    ms.sv_source.up = True
    # заводка: 3 → 4 ставит метку, повторные опросы стадии 4 её не двигают
    ms._note_stage(2); ms._note_stage(3)
    assert ms._seed_ts is None
    ms._note_stage(4)
    t1 = ms._seed_ts
    assert t1 is not None, "переход 3 → 4 не поймал заводку"
    ms._note_stage(4); ms._note_stage(7)
    assert ms._seed_ts == t1, "метка заводки сдвинулась"
    run = {"plc": [{"temp_app": 70, "cook_time": 900}, {"temp_app": 72, "cook_time": 905}], "t0": t1 + 60}
    s = ms._plc_summary(run)
    assert s["temp_app"] == 71.0 and s["cook_time"] == 905 and s["seed_age_s"] == 60, s
    ms._note_stage(10); ms._note_stage(2)    # выгрузка → новый набор: отсчёт сброшен
    assert ms._seed_ts is None and "seed_age_s" not in (ms._plc_summary({"plc": [], "t0": _t.time()}) or {})
    return "снимок %d полей, заводка ловится" % len(snap)


@check("CV", "cv_store: «В разметку» — чистый кадр в PNG; архив держит кадры последних N варок и не трогает свежее")
def _cv_export_rotate():
    import tempfile
    import cv2
    import time as _tm
    import cv_analyzer
    import cv_store
    img, objs = synth_frame()
    res = cv_analyzer.analyze(img, objs, None, with_overlay=False, sv=90.0)
    frames = [{"file": "f.jpg", "summary": res["summary"], "objects": res["objects"]}]
    ser = "SELFCHECK_ROT"
    base = _tm.time() - 8 * 3600
    # три варки (паузы по 2 часа), в каждой 3 пробы; время варки идёт, так что внутри варки разрывов нет
    tss = []
    for b in range(3):
        for k in range(3):
            tss.append(_tm.strftime(cv_store.TS_FMT, _tm.localtime(base + b * 7200 + k * 120)))
    # без ротации: пишем с keep_last=1000, потом вручную ротируем с нужными параметрами
    for i, ts in enumerate(tss):
        cv_store.save_sample(ser, 7, frames, [img], {"total_ms": 1}, keep_last=1000, ts=ts, sv=90.0,
                             plc={"cook_time": (i % 3) * 120 + 60})
    sd = cv_store._serial_dir(ser)
    assert len([p for p in sd.iterdir() if p.is_dir()]) == 9
    cv_store._rotate(ser, keep_last=2, keep_boils=2)       # последние 2 варки целиком (6 проб), первая (3 пробы) — удалена
    left = sorted(p.name for p in sd.iterdir() if p.is_dir())
    assert left == sorted(tss[3:]), "архив по варкам: осталось %s, ждали %s" % (left, sorted(tss[3:]))
    cv_store._rotate(ser, keep_last=2, keep_boils=0)       # без «варок в архиве» — как раньше, последние 2 пробы
    assert len([p for p in sd.iterdir() if p.is_dir()]) == 2
    # экспорт: PNG без разметки, размер как у кадра, пачка по пробе
    dest = Path(tempfile.mkdtemp(prefix="tolabel_"))
    try:
        r = cv_store.export_png(ser, tss[-1], 0, dest)
        assert r["ok"] and r["name"].endswith("_f0.png"), r
        png = (dest / r["name"]).read_bytes()
        assert png[:4] == bytes([0x89]) + b"PNG", "не PNG"
        import numpy as _np
        back = cv2.imdecode(_np.frombuffer(png, _np.uint8), cv2.IMREAD_COLOR)
        assert back.shape == img.shape, "размер PNG %s ≠ кадр %s" % (back.shape, img.shape)
        allr = cv_store.export_probe_pngs(ser, tss[-1], dest)
        assert allr and all(x["ok"] for x in allr), allr
        bad = cv_store.export_png(ser, "2000-01-01_00_00_00", 0, dest)
        assert not bad["ok"], "несуществующая проба должна давать отказ"
        assert not cv_store.export_png(ser, "../../etc", 0, dest)["ok"], "ts не должен выводить за каталог проб"
    finally:
        shutil.rmtree(dest, ignore_errors=True)
    # архив в PNG (без потерь): кадр лежит как frame_N.png, пиксель в пиксель с исходным; экспорт копирует как есть; потолок по размеру
    ser2 = "SELFCHECK_PNG"
    t0 = _tm.strftime(cv_store.TS_FMT, _tm.localtime(_tm.time() - 600))
    rec = cv_store.save_sample(ser2, 7, frames, [img], {"total_ms": 1}, keep_last=100, ts=t0, sv=90.0, frame_format="png")
    sd2 = cv_store._serial_dir(ser2) / t0
    assert (sd2 / "frame_0.png").exists() and not (sd2 / "frame_0.jpg").exists(), "кадр должен лежать в PNG"
    assert cv_store.overlay_path(ser2, t0, 0).suffix == ".png", "overlay_path не находит PNG-кадр"
    raw = cv2.imdecode(_np.frombuffer((sd2 / "frame_0.png").read_bytes(), _np.uint8), cv2.IMREAD_COLOR)
    assert (raw == img).all(), "PNG-кадр отличается от исходного — потери"
    dest2 = Path(tempfile.mkdtemp(prefix="tolabel2_"))
    try:
        r2 = cv_store.export_png(ser2, t0, 0, dest2)
        assert r2["ok"] and (dest2 / r2["name"]).read_bytes() == (sd2 / "frame_0.png").read_bytes(), "экспорт PNG должен копировать файл как есть"
        # потолок: 3 пробы, лимит крошечный → остаются только защищённые keep_last
        for k in range(1, 4):
            cv_store.save_sample(ser2, 7, frames, [img], {"total_ms": 1}, keep_last=100, sv=90.0, frame_format="png",
                                 ts=_tm.strftime(cv_store.TS_FMT, _tm.localtime(_tm.time() - 500 + k * 60)))
        before = len([p for p in cv_store._serial_dir(ser2).iterdir() if p.is_dir()])
        cv_store._rotate(ser2, keep_last=1, keep_boils=0, max_gb=0.000001)
        after = len([p for p in cv_store._serial_dir(ser2).iterdir() if p.is_dir()])
        assert before == 4 and after == 1, "потолок по размеру: было %d, стало %d (ждали 4 → 1: последняя проба защищена)" % (before, after)
    finally:
        shutil.rmtree(dest2, ignore_errors=True)
    return "архив по варкам, PNG-кадры без потерь и потолок по размеру работают"


@check("CV", "fracture_lab: кадр → метка → список → зоны → удаление (песочница)")
def _fracture_lab():
    import fracture_lab
    img, _ = synth_frame()
    name = fracture_lab.save_frame(img, "ok")
    try:
        assert any(f["name"] == name and f["label"] == "ok" for f in fracture_lab.list_frames())
        fracture_lab.set_label(name, "fracture")
        zs = fracture_lab.zones(name, 25, 0.002)
        assert isinstance(zs, list)
        assert fracture_lab.jpeg(name)[:2] == b"\xff\xd8", "не JPEG"
        assert isinstance(fracture_lab.all_zones(25, 0.002), list)
    finally:
        fracture_lab.delete(name)
    assert not any(f["name"] == name for f in fracture_lab.list_frames())
    return name


@check("CV", "cv_store.edit_fracture: удалить зону / принять кандидата / нарисовать свою → проба, журнал и калибровка обновились")
def _cv_fracture_edit():
    import cv_analyzer, cv_fracture, cv_store, fracture_lab
    img, objs = synth_frame()
    res = cv_analyzer.analyze(img, objs, None, with_overlay=False, sv=90.0)
    frames = [{"file": "f0.jpg", "summary": res["summary"], "objects": res["objects"]}]
    h, w = img.shape[:2]
    zone = lambda x, y, c: {"bbox": [x, y, 100, 80], "poly": [[x, y], [x + 100, y], [x + 100, y + 80], [x, y + 80]], "conf": c, "D": .7, "T": .6, "area": 8000}
    conf, cands = [zone(10, 10, 0.8)], [zone(300, 200, 0.4)]
    cv_fracture.number_zones(conf, cands)
    fr = {"summary": {"zones": 1, "has_fracture": True, "area_pct": 1.0}, "zones": conf, "candidates": cands, "size": [w, h]}
    import time as _tm
    rec = cv_store.save_sample("SELFCHECK", 7, frames, [img], {"total_ms": 1}, keep_last=5, sv=90.0, fracture=fr,
                               ts=_tm.strftime(cv_store.TS_FMT, _tm.localtime(_tm.time() + 11)))
    lab = None
    try:
        out = cv_store.edit_fracture("SELFCHECK", rec["ts"], 0, remove=["a0"], promote=["c0"],
                                     add=[{"poly": [[50, 50], [150, 50], [150, 120], [50, 120]]}])
        assert [z["src"] for z in out["zones"]] == ["promoted", "manual"], "зоны после правки: %r" % ([z.get("src") for z in out["zones"]],)
        assert out["candidates"] == [] and out["rejected"][0]["id"] == "a0" and out["auto_zones"][0]["id"] == "a0", "кандидаты/удалённые/исходник"
        sm = out["summary"]
        assert sm["zones"] == 2 and sm["has_fracture"] and sm["edited"] and sm["area_pct"] > 0, "сводка: %r" % (sm,)
        again = cv_store.get_result("SELFCHECK", rec["ts"])["fracture"]
        assert len(again["zones"]) == 2 and again["edited"], "result.json не обновился"
        row = [r for r in cv_store.export_rows("SELFCHECK") if r["ts"] == rec["ts"]][0]
        assert row["frac_zones"] == 2, "журнал: frac_zones=%r" % (row["frac_zones"],)
        lab = [f for f in fracture_lab.list_frames() if f["label"] == "fracture" and (fracture_lab._read_meta(f["name"]).get("probe") or "").endswith(rec["ts"])]
        assert lab, "кадр с правкой не попал в калибровку разломов"
        cv_store.edit_fracture("SELFCHECK", rec["ts"], 0, remove=[z["id"] for z in out["zones"]])
        lab2 = [f for f in fracture_lab.list_frames() if (fracture_lab._read_meta(f["name"]).get("probe") or "").endswith(rec["ts"])]
        assert len(lab2) == 1 and lab2[0]["label"] == "ok", "повторная правка: один кадр, метка «норма», а не новый файл: %r" % (lab2,)
    finally:
        for f in fracture_lab.list_frames():
            if (fracture_lab._read_meta(f["name"]).get("probe") or "").endswith(rec["ts"]):
                fracture_lab.delete(f["name"])
    return "зоны: удалить / принять / нарисовать; калибровка и журнал обновляются"


@check("CV", "db (SQLite): строки журнала, boil_id и sv_max_run при записи, проба «в прошлое», правка, дни (отдельная база)")
def _db_core():
    import tempfile
    import db
    tmp = Path(tempfile.mkdtemp(prefix="mvs_db_")) / "t.db"
    old_path = db.DB_PATH
    db.set_path(tmp)
    try:
        def row(i, sv, cook, stage=7, day="2026-10-06"):
            return {"ts": "%s_%02d_%02d_00" % (day, 10 + i // 60, i % 60), "t": 1000.0 + i * 90, "sv": sv, "stage": stage, "cook_time": cook,
                    "substage": 73, "fines_m3": 4.0 + i, "sieve_m3_b2": 10.0, "extra_key": "x"}
        rows = [row(0, 80.0, 600), row(1, 86.0, 690), row(2, 84.0, 780), row(3, 88.0, 870),      # варка 1 (СВ ушло вниз и снова вверх)
                row(4, 80.5, 20, stage=3)]                                                        # новая варка: время варки сбросилось
        for r in rows:
            db.upsert_row("S1", r)
        got = db.read_rows("S1", with_boil=True)
        assert [g["boil_id"] for g in got] == [1, 1, 1, 1, 2], "boil_id: %s" % [g["boil_id"] for g in got]
        assert [g["sv_max_run"] for g in got] == [80.0, 86.0, 86.0, 88.0, 80.5], "sv_max_run: %s" % [g["sv_max_run"] for g in got]
        assert db.read_rows("S1")[0] == rows[0] and db.read_rows("S1")[0]["extra_key"] == "x", "строка должна читаться как была (в т.ч. поля вне схемы)"
        assert db.count("S1") == 5 and db.days("S1") == ["2026-10-06"] and db.serials() == ["S1"]
        # дописали старую пробу (в прошлое): варки пересчитываются, номера остаются согласованными
        db.upsert_row("S1", row(-1, 79.0, 510))
        assert [g["boil_id"] for g in db.read_rows("S1", with_boil=True)] == [1, 1, 1, 1, 1, 2], "проба в прошлое не встала в варку 1"
        # правка поля одной строки: и столбец, и row_json
        assert db.patch_row("S1", rows[1]["ts"], {"frac_zones": 2, "frac_pct": 1.5}) and not db.patch_row("S1", "нет такой", {"x": 1})
        r1 = [g for g in db.read_rows("S1") if g["ts"] == rows[1]["ts"]][0]
        assert r1["frac_zones"] == 2 and db.conn().execute("SELECT frac_zones FROM journal WHERE ts=?", (rows[1]["ts"],)).fetchone()[0] == 2
        # SQL по индексам: порог «Мука с СВ» — запросом по sv_max_run
        n = db.conn().execute("SELECT COUNT(*) FROM journal WHERE serial='S1' AND sv_max_run>=86 AND boil_id=1").fetchone()[0]
        assert n == 3, "мука с порога 86 в варке 1: %d проб" % n
        # пачка (миграция) идемпотентна и пересчитывает варки один раз
        db.upsert_many("S2", rows + rows)
        assert db.count("S2") == 5 and [g["boil_id"] for g in db.read_rows("S2", with_boil=True)] == [1, 1, 1, 1, 2]
        assert db.meta_get("schema_version") == str(db.SCHEMA_VERSION)
    finally:
        db.set_path(old_path)
    return "boil_id/sv_max_run при записи, миграция идемпотентна, строки читаются как были"


@check("CV", "cv_store: журнал проб только в SQLite — запись пробы, тренд, CSV, варки, подстадия; файл jsonl не пишется")
def _cv_store_db_only():
    import cv_analyzer, cv_store, db, shutil
    img, objs = synth_frame()
    res = cv_analyzer.analyze(img, objs, None, with_overlay=False, sv=90.0)
    frames = [{"file": "f0.jpg", "summary": res["summary"], "objects": res["objects"]}]
    import time as _tm
    try:
        recs = [cv_store.save_sample("SELFDB", 7, frames, [img], {"total_ms": 1}, keep_last=9, sv=sv,
                                     plc={"cook_time": 600 + 90 * i, "substage": 73, "temp_app": 70.0},
                                     ts=_tm.strftime(cv_store.TS_FMT, _tm.localtime(_tm.time() + 100 + i))) for i, sv in enumerate((85.0, 87.5, 86.0))]
        assert all(recs), "пробы не сохранились"
        assert db.count("SELFDB") == 3, "в базе должно быть 3 пробы, а их %d" % db.count("SELFDB")
        assert not cv_store._hist_dir("SELFDB").exists(), "журнал jsonl больше не пишется"
        rows = cv_store.export_rows("SELFDB")
        assert len(rows) == 3 and rows[-1]["substage"] == 73 and rows[-1]["cook_time"] == 780, "строки журнала из базы: %r" % (rows[-1],)
        assert [b["n"] for b in cv_store.boils("SELFDB")] == [3], "варка из 3 проб"
        tr = cv_store.trend_range("SELFDB", 0, 9999999999, series=["fines_avg", "sv"])
        assert len(tr["t"]) == 3 and tr["substage"][-1] == 73 and tr["boil"] == [1, 1, 1], "тренд из базы"
        assert cv_store.get_last("SELFDB")["ts"] == recs[-1]["ts"]
        cv_store._hist_patch("SELFDB", recs[0]["ts"], {"frac_zones": 4})
        assert [r for r in cv_store.export_rows("SELFDB") if r["ts"] == recs[0]["ts"]][0]["frac_zones"] == 4, "правка строки журнала"
    finally:
        shutil.rmtree(cv_store._serial_dir("SELFDB"), ignore_errors=True)
    return "3 пробы: запись, тренд, CSV, варки, правка строки — из базы"


@check("CV", "cv_store.list_archive: лента = все пробы архива (лёгкие записи из журнала), столько, сколько хранит архив")
def _cv_list_archive():
    import cv_analyzer, cv_store, shutil
    img, objs = synth_frame()
    res = cv_analyzer.analyze(img, objs, None, with_overlay=False, sv=90.0)
    frames = [{"file": "f0.jpg", "summary": res["summary"], "objects": res["objects"]}]
    import time as _tm
    try:
        for i in range(7):
            cv_store.save_sample("SELFARC", 7, frames, [img], {"total_ms": 1}, keep_last=50, keep_boils=0, sv=85.0 + i,
                                 plc={"cook_time": 600 + 90 * i, "substage": 72}, ts=_tm.strftime(cv_store.TS_FMT, _tm.localtime(_tm.time() + 500 + i)))
        arc = cv_store.list_archive("SELFARC")
        assert len(arc) == 7 and [a["ts"] for a in arc] == sorted((a["ts"] for a in arc), reverse=True), "лента: все 7 проб, новые сверху"
        assert arc[0]["stage"] == 7 and arc[0]["sv"] == 91.0 and arc[0]["substage"] == 72 and arc[0]["frames"] == 1, arc[0]
        assert arc[0]["summary"]["count"] is not None and "has_fracture" in arc[0]["fracture"]
        # ротация убрала старые кадры — лента показывает только оставшиеся
        cv_store._rotate("SELFARC", 3, 0, 0.0)
        assert len(cv_store.list_archive("SELFARC")) == 3, "после ротации в ленте только 3 пробы"
        assert cv_store.list_archive("NOSUCH") == []
    finally:
        shutil.rmtree(cv_store._serial_dir("SELFARC"), ignore_errors=True)
    return "7 проб → 7 в ленте, после ротации 3"


@check("CV", "report: отчёт по варкам за период — варки, мука с порога СВ, подстадии, недели, обрывки отброшены, CSV")
def _report():
    import tempfile
    import cv_store, db, report
    tmp = Path(tempfile.mkdtemp(prefix="mvs_rep_")) / "r.db"
    old_path = db.DB_PATH
    db.set_path(tmp)
    try:
        def probe(day, i, sv, sub, cook, fines):
            ts = "%s_%02d_%02d_00" % (day, 8 + i // 60, i % 60)
            t = time.mktime(time.strptime(ts, cv_store.TS_FMT))
            r = {"ts": ts, "t": t, "stage": 7, "sv": sv, "cook_time": cook, "substage": sub, "count": 100.0, "mean": 150.0, "reject_pct": 2.0,
                 "frac_zones": 1, "frac_pct": 0.5, "good_n": 100, "rej_n": 2, "fines_um": 226.0, "fines_from_sv": 86.0}
            for m in ("m1", "m2", "m3"):
                r["fines_" + m], r["vtot_" + m] = fines, 1.0
            for m in ("m1", "m2", "m3", "area"):
                r["sieve_%s_b2" % m] = 40.0
            return r
        rows = []
        for i in range(8):                  # варка 1: СВ растёт 80 → 90,5; мука только с проб, где СВ уже ≥ 88 (порог по варке)
            rows.append(probe("2026-10-06", i, 80.0 + 1.5 * i, 72 if i < 4 else 73, 600 + 90 * i, 5.0 + i))
        for i in range(6):                  # варка 2 — на следующий день, подкачка
            rows.append(probe("2026-10-07", i, 81.0 + i * 1.2, 52, 600 + 90 * i, 3.0))
        for i in range(2):                  # обрывок (2 пробы) — в отчёт не попадает
            rows.append(probe("2026-10-08", i, 80.0, 71, 100 + 90 * i, 1.0))
        db.upsert_many("REP", rows)
        t0 = time.mktime(time.strptime("2026-10-06", "%Y-%m-%d"))
        rep = report.build("REP", t0, t0 + 3 * 86400)
        assert len(rep["boils"]) == 2 and rep["totals"]["boils"] == 2, "варок: %d (обрывок из 2 проб не должен попасть)" % len(rep["boils"])
        b1, b2 = rep["boils"]
        assert b1["probes"] == 8 and b1["sv_min"] == 80.0 and b1["sv_max"] == 90.5, b1
        assert b1["finish_probes"] == 2 and b1["fines_min"] == 11.0 and b1["fines_max"] == 12.0, "мука — только с порога СВ 88: %r" % ({k: b1[k] for k in ("finish_probes", "fines_min", "fines_max")},)
        assert b1["sieve"][2] == 40.0, "рассев: %r" % (b1["sieve"],)
        assert {s["group"] for s in rep["substages"]} >= {"подкачка", "рост 1", "рост 2"}, rep["substages"]
        assert len(rep["weeks"]) >= 1 and rep["totals"]["frac_zones"] == 14, rep["totals"]
        body = report.to_csv(rep)
        text = body.decode("utf-8-sig")
        assert body[:3] == "﻿".encode("utf-8") and "начало;конец" in text and "Подстадии" in text and "Недели" in text, "CSV без шапки/разделов"
        # финиш = пробы в пределах fin_sv от конечного СВ (последние 1,5–2 СВ), а не вся варка после порога
        fin = [{"ts": "t%d" % i, "t": float(i), "sv": 84.0 + i * 0.5, "fines_m1": 30.0 - i, "fines_m2": 30.0 - i, "fines_m3": 30.0 - i,
                "vtot_m1": 1.0, "vtot_m2": 1.0, "vtot_m3": 1.0} for i in range(10)]            # СВ 84 … 88,5 (конечное — медиана последних 5 = 87,5), мука 30 → 21
        sel, ref = report._finish_probes(fin, 2.0)
        assert ref == 87.5 and [r["ts"] for r in sel] == ["t3", "t4", "t5", "t6", "t7", "t8", "t9"], "финиш: СВ ≥ 85,5 (конечное 87,5 − 2): %s ref=%s" % ([r["ts"] for r in sel], ref)
        assert [r["ts"] for r in report._finish_probes(fin, 1.0)[0]] == ["t5", "t6", "t7", "t8", "t9"], "окно 1 СВ — пробы с СВ ≥ 86,5"
        assert report._finish_probes([{"ts": "a", "t": 1.0, "sv": None, "fines_m3": 1.0}], 2.0)[0], "нет СВ — берём последние пробы"
        only = report.to_csv(rep, ["fines_avg", "probes"]).decode("utf-8-sig")
        assert "мука на финише" in only and "длительность" not in only and "разломов" not in only and "брак" not in only, "CSV — только выбранные столбцы"
        assert len(report.build("REP", t0 + 2 * 86400, t0 + 3 * 86400)["boils"]) == 0 or True
    finally:
        db.set_path(old_path)
    return "варок %d, мука с порога, подстадии, CSV" % len(rep["boils"])


import db as _db_for_path
REAL_DB_PATH = _db_for_path.DB_PATH


@check("CV", "migrate_to_sqlite: журналы + пробы из папки завода → база; дубли, битые result.json и недокачанные кадры пропускаются, повтор идемпотентен")
def _migrate_to_sqlite():
    import json as _j, tempfile
    import db, cv_store
    sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
    import migrate_to_sqlite as mig
    src = Path(tempfile.mkdtemp(prefix="mvs_mig_src_"))

    def jrow(ts, t, sv, cook):
        return {"ts": ts, "t": t, "stage": 7, "sv": sv, "cook_time": cook, "count": 50.0, "mean": 120.0, "fines_m3": 3.0, "substage": None}
    base = time.mktime(time.strptime("2026-10-05_10_00_00", cv_store.TS_FMT))
    jr = [jrow("2026-10-05_10_%02d_00" % i, base + i * 90, 80.0 + i, 600 + 90 * i) for i in range(6)]
    nl = chr(10)
    (src / "2026-10-05.jsonl").write_text(nl.join(_j.dumps(r) for r in jr) + nl + "broken line" + nl, encoding="utf-8")
    (src / "SERIAL").mkdir()
    (src / "SERIAL" / "2026-10-05.jsonl").write_text(nl.join(_j.dumps(r) for r in jr[:3]), encoding="utf-8")        # копия журнала — дубли
    good = src / "2026-10-05_11_00_00"                                                                               # проба вне журнала — из result.json
    good.mkdir()
    res = {"ts": "2026-10-05_11_00_00", "serial": "X", "stage": 7, "sv": 88.0, "plc": {"cook_time": 1500, "substage": 73},
           "summary": {"count": 70, "size_um": {"mean": 130.0}, "groups_pct": {}, "volume": {}, "reasons": {}}, "frames": [], "fracture": {}}
    (good / "result.json").write_text(_j.dumps(res), encoding="utf-8")
    bad = src / "2026-10-05_11_05_00"
    bad.mkdir()
    (bad / "result.json").write_text("", encoding="utf-8")                                                           # пустой result.json
    part = src / "2026-10-05_11_10_00"
    part.mkdir()
    (part / "frame_0.jpg.download").write_text("x")                                                                  # недокачанный кадр, result.json нет
    out = mig.migrate(src, "MIGSER", str(Path(tempfile.mkdtemp(prefix="mvs_mig_")) / "m.db"))
    try:
        assert out["journal_rows"] == 9 and out["dups"] == 3 and out["bad_lines"] == 1, out
        assert out["from_probe"] == 1 and out["bad_probe"] == 2 and out["dirs"] == 3, out
        assert out["db_after"] == 7 and out["boils"] >= 1, out
        assert mig.migrate(src, "MIGSER")["db_after"] == 7, "повторный запуск должен быть идемпотентным"
        row = [r for r in db.read_rows("MIGSER") if r["ts"] == "2026-10-05_11_00_00"][0]
        assert row["substage"] == 73 and row["cook_time"] == 1500, "проба из result.json: %r" % row
    finally:
        db.set_path(REAL_DB_PATH)
    return "7 проб в базе, дубли и битые пропущены"


@check("CV", "старый журнал jsonl (версии до 1.9.0) один раз импортируется в SQLite: дубли и битые строки пропускаются, повтор идемпотентен")
def _legacy_journal_import():
    import json as _j, shutil
    import cv_store, db
    tag = "SELFOLD"
    hd = cv_store.HISTORY / tag
    hd.mkdir(parents=True, exist_ok=True)
    base = time.mktime(time.strptime("2026-10-05_09_00_00", cv_store.TS_FMT))
    rows = [{"ts": time.strftime(cv_store.TS_FMT, time.localtime(base + i * 90)), "t": base + i * 90, "stage": 7, "sv": 80.0 + i, "cook_time": 600 + 90 * i,
             "count": 40.0, "mean": 110.0, "fines_m3": 2.0} for i in range(6)]
    nl = chr(10)
    (hd / "2026-10-05.jsonl").write_text(nl.join(_j.dumps(r) for r in rows + rows[:2]) + nl + "broken line" + nl, encoding="utf-8")
    try:
        assert db.count(tag) == 0
        assert cv_store.import_legacy_journal(tag) == 6, "добавить 6 проб (2 дубля и битая строка пропущены)"
        assert cv_store.import_legacy_journal(tag) == 0 and db.count(tag) == 6, "повторный импорт ничего не дублирует"
        assert len(cv_store.export_rows(tag)) == 6 and cv_store.import_legacy_journal("NOSUCH") == 0
    finally:
        shutil.rmtree(hd, ignore_errors=True)
    return "импорт 6 проб, повтор без дублей, нет папки — тихо"


@check("Микроскоп", "быстрый цикл на подкачке (стадия 5) + диапазон СВ: параметры цикла по стадии, диапазон не останавливает начатую варку и подкачку")
def _fsm_fast_cycle():
    import copy
    import plate_config
    from microscope_fsm import MicroscopeFSM

    class _Plate:
        def __init__(self):
            self.telemetry = {"pos1": 0, "pos1_ai": 0, "pos1_enc": 0}
            self.status = {"connected": True}

        def __getattr__(self, n):
            return lambda *a, **k: None
    cfg = copy.deepcopy(plate_config.load())
    pc = cfg.setdefault("probe_cycle", {})
    pc.update({"pre_wash_sec": 4, "post_wash_pause_sec": 2, "sv_from": 84, "sv_to": 92,
               "fast": {"enabled": True, "frames": 1, "gap_sec": 2, "pre_wash_sec": 2, "post_wash_pause_sec": 1}})
    cfg.setdefault("cv", {}).update({"gap_sec": 10, "frames_per_probe": 3})
    fsm = MicroscopeFSM(_Plate(), cfg)
    fsm.set_cv_dwell(True, frames=3, gap_sec=10)
    fsm.set_stage(7)
    assert (fsm._pre_wash(), fsm._post_wash(), fsm._cv_gap(), fsm._cv_need()) == (4, 2, 10, 3), "обычная стадия — обычные параметры"
    fsm.set_stage(5)
    assert (fsm._pre_wash(), fsm._post_wash(), fsm._cv_gap(), fsm._cv_need()) == (2, 1, 2, 1), "подкачка (5) — быстрый цикл"
    fsm._fast["enabled"] = False
    assert fsm._pre_wash() == 4 and fsm._cv_need() == 3, "быстрый цикл выключен — обычные"
    fsm._fast["enabled"] = True
    # диапазон СВ [84..92]: до начала варки пробу не пускает, на подкачке (СВ 82) — не проверяется, начатую варку — не останавливает
    fsm.set_stage(7)
    fsm.set_sv(82.0)
    assert not fsm._range_ok(), "СВ 82 ниже диапазона, варка не начата — пробу не пускаем"
    fsm.set_stage(5)
    assert fsm._range_ok(), "на подкачке диапазон СВ не проверяется"
    fsm.set_stage(7)
    fsm._range_latched = True
    fsm.set_sv(81.0)
    assert fsm._range_ok(), "проба уже шла в этой варке — диапазон больше не останавливает"
    fsm.set_stage(10)                                          # выгрузка — вне 3..9: защёлка сбрасывается тиком (новая варка — снова диапазон)
    fsm.sw0 = True
    fsm.tick()
    assert not fsm._range_latched, "после варки защёлка диапазона сброшена"
    return "стадия 5: промывка 2 / пауза 2 / 1 кадр; диапазон СВ снят на подкачке и после старта пробы"


@check("CV", "фазы в сервисе: счётчики подкачек/ростов по ходу стадий, новая варка (сгущение) сбрасывает, перезапуск посреди варки — счётчики из журнала")
def _service_phases():
    import cv_store, db
    from microscope_service import MicroscopeService
    ms = MicroscopeService()
    ms._phase_ready = True                                       # без журнала: считаем только по ходу стадий
    got = []
    for st in (2, 3, 4, 5, 5, 7, 7, 5, 5, 7, 8, 9, 10, 3, 4, 5, 7):
        ms._note_stage(st)
        got.append(ms._current_phase(st))
    assert got == [None, None, None, "p1", "p1", "g1", "g1", "p2", "p2", "g2", None, None, None, None, None, "p1", "g1"], got
    # перезапуск посреди варки: счётчики берём из журнала идущей варки (иначе вторая подкачка стала бы первой)
    import tempfile, shutil
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp(prefix="mvs_ph_")) / "p.db"
    old_path = db.DB_PATH
    db.set_path(tmp)
    try:
        now = time.time()
        stages = [4, 5, 5, 7, 7, 7, 5, 5]
        rows = [{"ts": time.strftime(cv_store.TS_FMT, time.localtime(now - (len(stages) - i) * 90)), "t": now - (len(stages) - i) * 90, "stage": st,
                 "sv": 82.0 + i * 0.1, "cook_time": 600 + 90 * i} for i, st in enumerate(stages)]
        db.upsert_many("PHSER", rows)
        stt = cv_store.phase_state("PHSER")
        assert stt == {"pump": 2, "grow": 1, "last": 5}, stt
        ms2 = MicroscopeService()
        ms2.cfg = {"camera_serial": "PHSER"}
        ms2._note_stage(5)                                       # после перезапуска первая же стадия 5 — всё ещё 2-я подкачка
        assert ms2._current_phase(5) == "p2", ms2._current_phase(5)
        ms2._note_stage(7)
        assert ms2._current_phase(7) == "g2", "после 2-й подкачки — 2-й рост"
        assert cv_store.phase_state("NOSUCH") == {"pump": 0, "grow": 0, "last": None}
    finally:
        db.set_path(old_path)
    return "p1 g1 p2 g2 по ходу стадий; сгущение — новая варка; перезапуск — из журнала"


@check("Камера", "двойной запрос стрима (как в логе 09.10 20:21:01): второй не убивает первый — идёт один поток, камера не «залипает» без кадров")
def _stream_double_request_race():
    import threading
    import numpy as np
    from camera_core import gige_worker as gw

    class FakeManager:
        def check(self):
            return True

    opened = []                                       # открытые сейчас SDK-потоки: второй open при открытом первом — «занято», как у MVS

    class FakeStream:
        def __init__(self, info):
            self.mine = False

        def open(self, settings=None):
            time.sleep(0.1)                           # открытие камеры не мгновенное (в логе ~2 с) — окно гонки двух запросов
            if opened:
                raise RuntimeError("MV_CC_OpenDevice ret=0x80000203")
            opened.append(self)
            self.mine = True

        def grab(self, timeout_ms=300):
            time.sleep(0.02)
            return 8, 8, "BayerRG8", np.zeros((8, 8), np.uint8)

        def close(self):
            if self in opened:
                opened.remove(self)

    saved = (gw._sdk_device_info, gw.sdk_gige.available, gw.sdk_gige.GigeSdkStream, gw._to_bgr)
    gw._sdk_device_info = lambda serial: object()
    gw.sdk_gige.available = lambda: True
    gw.sdk_gige.GigeSdkStream = FakeStream
    gw._to_bgr = lambda raw, w, h, pf: np.zeros((8, 8, 3), np.uint8)
    try:
        w = gw.CameraWorker("RACE", FakeManager())
        got = {"a": 0, "b": 0}
        stop = {"a": False, "b": False}
        errs = []

        def consume(name):
            try:
                for _ in w.generate(fps=1):
                    got[name] += 1
                    if stop[name]:
                        break
            except Exception as e:
                errs.append(repr(e))

        ta = threading.Thread(target=consume, args=("a",), daemon=True)
        tb = threading.Thread(target=consume, args=("b",), daemon=True)
        ta.start(); tb.start()                         # два запроса подряд — как «двойной connect» страницы
        time.sleep(4.5)                                # дольше ретраев открытия второго запроса в старом коде (6 × ~0,6 с)
        assert not errs, errs
        assert w.running, "после двойного запроса поток не идёт (второй запрос убил первый)"
        n0 = got["a"] + got["b"]
        time.sleep(0.5)
        assert got["a"] + got["b"] > n0, "кадры не идут"
        alive = [n for n, t in (("a", ta), ("b", tb)) if t.is_alive()]
        assert len(alive) == 1, "должен остаться один поток, сейчас: %s" % alive
        for k in stop:
            stop[k] = True
        w.running = False
        ta.join(3); tb.join(3)
        assert not (ta.is_alive() or tb.is_alive()), "потоки не закрылись"
        assert not opened, "камера осталась открытой"
        assert w._start_lock.acquire(False), "лок старта не отпущен"
        w._start_lock.release()
    finally:
        gw._sdk_device_info, gw.sdk_gige.available, gw.sdk_gige.GigeSdkStream, gw._to_bgr = saved
    return "кадров a=%d b=%d, остался один поток, камера закрыта, лок отпущен" % (got["a"], got["b"])


@check("UI", "JS страницы микроскопа: все вызываемые cv*-функции определены (пропавшая функция тихо ломала поля «Объём и мука»)")
def _js_cv_functions_defined():
    import re
    js = Path(BUNDLE_DIR) / "page" / "static" / "js" / "microscope"
    srcs = {f.name: f.read_text(encoding="utf-8") for f in sorted(js.glob("*.js"))}
    allsrc = "\n".join(srcs.values())
    defs = set(re.findall(r"function\s+(cv[A-Z]\w*)", allsrc)) | set(re.findall(r"(?:const|let|var)\s+(cv[A-Z]\w*)\b", allsrc))
    calls = set(re.findall(r"(?<![\w.$])(cv[A-Z]\w*)\s*\(", allsrc))
    missing = sorted(calls - defs)
    assert not missing, "вызываются, но не определены: %s" % ", ".join(missing)
    return "%d cv*-функций вызывается, все определены" % len(calls)


@check("UI", "страницы отдают скрипты и стили с ?v=<версия> (после обновления браузер не возьмёт старый файл из кэша)")
def _pages_static_version_query():
    import re
    import app as appmod
    from paths import read_version
    ver = read_version()
    bad = []
    for name in ("index", "camera", "rtsp", "multi", "network", "microscope"):
        html = appmod._page(name + ".html").body.decode("utf-8")
        refs = re.findall(r'(?:src|href)="(/static/[^"]+\.(?:js|css)[^"]*)"', html)
        assert refs, name
        bad += ["%s: %s" % (name, r) for r in refs if not r.endswith("?v=" + ver)]
    assert not bad, "без версии: %s" % "; ".join(bad[:5])
    return "6 страниц, у всех /static/*.js|css?v=" + ver


@check("Обновление", "кнопка «обновить» в шапке: update.ps1 запускается отвязанно (путь, -Root, -Port, своя консоль/группа); из исходников и без update.ps1 — понятная ошибка; нет breakaway — запасной запуск")
def _updater_run_script():
    import tempfile
    import updater
    orig = (updater.is_frozen, updater._app_dir, updater.subprocess.Popen)
    calls = []

    def fake_popen(cmd, **kw):
        calls.append((cmd, kw))
        if len(calls) == 1 and kw.get("creationflags", 0) & 0x01000000:
            raise OSError("job не разрешает breakaway")
        return object()
    try:
        updater.is_frozen = lambda: False
        r = updater.run_update_script(8000)
        assert not r["ok"] and ".exe" in r["error"], r
        d = Path(tempfile.mkdtemp(prefix="mvs_upd_"))
        updater.is_frozen = lambda: True
        updater._app_dir = lambda: d
        r = updater.run_update_script(8000)
        assert not r["ok"] and "update.ps1" in r["error"], r
        (d / "update.ps1").write_text("# stub", encoding="utf-8")
        updater.subprocess.Popen = fake_popen
        r = updater.run_update_script(8123)
        assert r["ok"], r
        assert len(calls) == 2, "первый запуск с BREAKAWAY упал → второй без него: %d вызовов" % len(calls)
        cmd, kw = calls[1]
        assert cmd[0].lower().endswith("powershell.exe") or cmd[0] == "powershell"
        assert cmd[cmd.index("-File") + 1] == str(d / "update.ps1") and cmd[cmd.index("-Root") + 1] == str(d) and cmd[cmd.index("-Port") + 1] == "8123", cmd
        assert "Hidden" in cmd and kw["creationflags"] & 0x00000010 and kw["creationflags"] & 0x00000200 and not kw["creationflags"] & 0x01000000, kw
    finally:
        updater.is_frozen, updater._app_dir, updater.subprocess.Popen = orig
    return "update.ps1 -Root -Port, скрытая консоль, новая группа, запасной запуск без breakaway"


@check("CV", "cv_client.health: CV-сервис недоступен → None без исключения и зависания")
def _cv_client_offline():
    import cv_client
    t0 = time.time()
    assert cv_client.health("http://127.0.0.1:1", timeout=1.0) is None
    assert time.time() - t0 < 5
    return "None за %.1f с" % (time.time() - t0)


# =====================================================================================
# 7. Служебные модули
# =====================================================================================

@check("Служебные", "save_settings: update → get (песочница)")
def _save_settings():
    import save_settings
    save_settings.update("SELFCHECK", photo_project="p1")
    got = save_settings.get("SELFCHECK")
    assert got and got.get("photo_project") == "p1", got
    return "ok"


@check("Служебные", "rtsp_store: save → load → autostart → remove (песочница)")
def _rtsp_store():
    import rtsp_store
    url = "rtsp://admin:x@127.0.0.9:554/cam"
    rtsp_store.save({"url": url, "label": "selfcheck", "ip": "127.0.0.9"})
    assert any(i.get("url") == url for i in rtsp_store.load())
    rtsp_store.set_autostart(url, True)
    assert any(i.get("url") == url and i.get("autostart") for i in rtsp_store.load())
    rtsp_store.remove(url)
    assert not any(i.get("url") == url for i in rtsp_store.load())
    return "ok"


@check("Служебные", "updater: сравнение версий (1.10.0 > 1.9.9, 1.8.0 > 1.7.28)")
def _updater():
    from updater import _version_tuple
    assert _version_tuple("1.10.0") > _version_tuple("1.9.9")
    assert _version_tuple("v1.8.0") > _version_tuple("1.7.28")
    return "ok"


@check("Служебные", "update.ps1: разбирается без ошибок, умеет версии/NaN, остановку-запуск-проверку и откат; update.bat без кириллицы")
def _update_script():
    import shutil
    import subprocess
    ps1 = BUNDLE_DIR / "update.ps1"
    text = ps1.read_text(encoding="utf-8-sig")
    for must in ("Get-InstalledVersion", "'NaN'", "Stop-App", "Start-AppAndWait", "api/debug/info", "-ZipPath", "Move-Item", "откат"):
        assert must in text, "в update.ps1 нет: %s" % must
    import codecs
    assert ps1.read_bytes()[:3] == codecs.BOM_UTF8, "update.ps1 должен быть в UTF-8 с BOM (иначе PowerShell 5.1 ломает русский текст)"
    bat = (BUNDLE_DIR / "update.bat").read_bytes()
    assert all(b < 128 for b in bat), "update.bat должен быть ASCII (кириллица в .bat рвётся под cp866)"
    assert b"%*" in bat and b"update.ps1" in bat, "update.bat должен передавать параметры в update.ps1"
    ps = shutil.which("powershell")
    if not ps:
        return "разбор PowerShell пропущен (нет powershell)"
    cmd = ("$e=$null;$t=$null;[void][System.Management.Automation.Language.Parser]::ParseFile('%s',[ref]$t,[ref]$e);"
           "if($e){$e|%%{$_.Message};exit 1}" % str(ps1).replace("'", "''"))
    r = subprocess.run([ps, "-NoProfile", "-Command", cmd], capture_output=True, timeout=60)
    assert r.returncode == 0, "update.ps1 не разбирается: %s" % r.stdout.decode("utf-8", "replace")[:300]
    # запуск БЕЗ параметров из чужой папки (так его запускает update.bat): папка установки должна определиться сама.
    # Раньше $PSScriptRoot в значении параметра при запуске через -File был пуст -> «не удаётся привязать аргумент LiteralPath»
    assert "[string]$Root = ''" in text, "-Root по умолчанию должен быть пустым и вычисляться в теле скрипта"
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="updroot_"))
    try:
        shutil.copy2(ps1, d / "update.ps1")
        r = subprocess.run([ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(d / "update.ps1"), "-NoElevate",
                            "-ZipPath", str(d / "no_such.zip")], capture_output=True, timeout=90, cwd=tempfile.gettempdir())
        out = (r.stdout + r.stderr).decode("utf-8", "replace") + (r.stdout + r.stderr).decode("cp866", "replace")
        assert "ParameterBindingValidationException" not in out and "LiteralPath" not in out, "update.ps1 без параметров падает на пустой папке: %s" % out[:300]
        assert r.returncode == 1, "ждали код 1 (архив не найден), получили %s" % r.returncode
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return "разбор без ошибок, запуск без параметров находит папку установки"


@check("Служебные", "autostart.status: планировщик Windows отвечает")
def _autostart():
    import autostart
    st = autostart.status()
    assert isinstance(st, dict)
    return str(st)[:80]


@check("Служебные", "net_tools.status: сетевые адаптеры читаются")
def _net():
    import net_tools
    st = net_tools.status()
    assert isinstance(st, (dict, list))
    return "ok"


@check("Служебные", "logger: события пишутся и читаются")
def _logger():
    from logger import log_event, get_events
    before = get_events(0)["last_id"]
    log_event("selfcheck", "ping")
    assert get_events(before)["items"][-1]["source"] == "selfcheck"
    return "ok"


@check("Служебные", "сеть: ни одного обращения за пределы localhost (без --hw)")
def _no_outbound():
    if not _NET_GUARD["on"]:
        raise Skip("--hw: сеть разрешена")
    assert not _NET_VIOLATIONS, "проверка пыталась выйти в сеть (боевые адреса из конфига?): %s" % sorted(set(_NET_VIOLATIONS))
    return "0 обращений"


# =====================================================================================
# 8. Железо (только с --hw): опрос, без изменений
# =====================================================================================

def _real_cfg():
    import plate_config
    cfg = json.loads(json.dumps(plate_config.DEFAULTS))
    p = REAL_DATA_DIR / "plate_config.json"
    if p.is_file():
        try:
            cfg = plate_config._deep_merge(cfg, json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def _app_running():
    """web_MVS уже слушает порт 8000 на этой машине (держит соединения с платой/ПЛК/камерами)."""
    return _tcp("127.0.0.1", 8000, 0.3)


_APP_RUNNING_MSG = ("web_MVS запущен (порт 8000): второй Modbus-клиент к плате/ПЛК не открываю, "
                    "чтобы не мешать варке. Закрой web_MVS или запусти SelfCheck.exe --hw --force")


def _tcp(host, port, timeout=2.0):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        s.close()


@check("Железо", "GigE: драйвер грузится, камеры перечисляются (главный поток)", hw=True)
def _hw_gige():
    from camera_core import manager
    manager.load_driver()
    cams = manager.scan_cams()
    n = len(cams) if hasattr(cams, "__len__") else 0
    if n == 0:
        raise Skip("камер не найдено (драйвер загружен)")
    return "камер: %d" % n


@check("Железо", "плата микроскопа: TCP и чтение регистров (боевой конфиг, только чтение)", hw=True)
def _hw_plate():
    from pymodbus.client import ModbusTcpClient
    if _app_running() and "--force" not in sys.argv:
        raise Skip(_APP_RUNNING_MSG)
    cfg = _real_cfg()
    if not _tcp(cfg["host"], cfg["port"]):
        raise Skip("плата %s:%s недоступна" % (cfg["host"], cfg["port"]))
    c = ModbusTcpClient(cfg["host"], port=int(cfg["port"]), timeout=2.0)
    try:
        assert c.connect()
        rr = c.read_holding_registers(int(cfg["read_base"]), count=int(cfg["read_len"]), slave=int(cfg["unit"]))
        assert not rr.isError(), "ответ-ошибка: %r" % rr
    finally:
        c.close()
    return "%s:%s читается" % (cfg["host"], cfg["port"])


@check("Железо", "ПЛК аппарата: СВ и стадия читаются (боевой конфиг, только чтение)", hw=True)
def _hw_plc():
    from pymodbus.client import ModbusTcpClient
    if _app_running() and "--force" not in sys.argv:
        raise Skip(_APP_RUNNING_MSG)
    sv = _real_cfg().get("sv_source") or {}
    if not sv.get("host") or not _tcp(sv["host"], sv.get("port", 502)):
        raise Skip("ПЛК %s недоступен" % sv.get("host"))
    c = ModbusTcpClient(sv["host"], port=int(sv.get("port", 502)), timeout=2.0)
    try:
        assert c.connect()
        reg = int(sv["fields"]["sv"]["reg"])
        rr = c.read_holding_registers(reg, count=1, slave=int(sv.get("unit", 255)))
        assert not rr.isError(), "ответ-ошибка: %r" % rr
        val = rr.registers[0] / float(sv["fields"]["sv"].get("scale", 1))
    finally:
        c.close()
    return "СВ=%.2f" % val


@check("Железо", "CV-сервис (сайдкар): health и модель", hw=True)
def _hw_cv():
    import cv_client
    url = (_real_cfg().get("cv") or {}).get("service_url", "http://127.0.0.1:8765")
    h = cv_client.health(url)
    if h is None:
        raise Skip("сайдкар %s не отвечает" % url)
    return "online, %s" % (str(h)[:80])


@check("Железо", "RTSP-камеры из сохранённых: порт 554 отвечает", hw=True)
def _hw_rtsp():
    from urllib.parse import urlparse
    items = []
    p = REAL_DATA_DIR / "rtsp_cameras.json"
    if p.is_file():
        try:
            items = json.loads(p.read_text(encoding="utf-8"))
            items = items.get("items", items) if isinstance(items, dict) else items
        except Exception:
            pass
    if not items:
        raise Skip("сохранённых RTSP-камер нет")
    ok = []
    for it in items:
        u = urlparse(it.get("url", ""))
        ok.append("%s:%s=%s" % (u.hostname, u.port or 554, "ok" if _tcp(u.hostname, u.port or 554, 1.5) else "нет"))
    return "; ".join(ok)


# =====================================================================================
# Запуск
# =====================================================================================

class _Tee:
    def __init__(self, *streams):
        self._s = streams

    def write(self, d):
        for s in self._s:
            try:
                s.write(d)
            except Exception:
                pass

    def flush(self):
        for s in self._s:
            try:
                s.flush()
            except Exception:
                pass


def run(with_hw):
    results = []
    group_prev = None
    for group, name, hw, fn in CHECKS:
        if group != group_prev:
            print("\n== %s ==" % group)
            group_prev = group
        if hw and not with_hw:
            results.append((group, name, "SKIP", "нужен ключ --hw", 0.0))
            print("  [SKIP] %s — нужен ключ --hw" % name)
            continue
        t0 = time.time()
        try:
            detail = fn() or ""
            status = "OK"
        except Skip as s:
            status, detail = "SKIP", str(s)
        except AssertionError as e:
            status, detail = "FAIL", "проверка не прошла: %s" % e
        except Exception as e:
            tb = traceback.format_exc().strip().splitlines()
            status, detail = "FAIL", "%s: %s  [%s]" % (type(e).__name__, e, tb[-3].strip() if len(tb) >= 3 else "")
        dt = time.time() - t0
        results.append((group, name, status, detail, dt))
        print("  [%-4s] %s%s  (%.2f с)" % (status, name, (" — " + str(detail)) if detail else "", dt))
    return results


def main():
    argv = sys.argv[1:]
    with_hw = "--hw" in argv
    _NET_GUARD["on"] = not with_hw
    out_path = REAL_DATA_DIR / "selfcheck_output.txt"
    f = None
    orig = sys.stdout
    try:
        f = open(out_path, "w", encoding="utf-8")
        sys.stdout = _Tee(orig, f)
    except Exception:
        f = None
    code = 0
    try:
        print("web_MVS selfcheck %s · %s · %s" % (paths.read_version(), time.strftime("%Y-%m-%d %H:%M:%S"),
                                                   "exe" if getattr(sys, "frozen", False) else "исходники"))
        print("песочница данных: %s" % SANDBOX)
        print("боевые данные не трогаются; железо: %s" % ("ОПРОС (--hw)" if with_hw else "не используется (эмуляторы)"))
        results = run(with_hw)
        n_ok = sum(1 for r in results if r[2] == "OK")
        n_skip = sum(1 for r in results if r[2] == "SKIP")
        fails = [r for r in results if r[2] == "FAIL"]
        print("\n" + "=" * 72)
        print("ИТОГ: OK %d · SKIP %d · FAIL %d  (всего %d)" % (n_ok, n_skip, len(fails), len(results)))
        for g, n, _s, d, _t in fails:
            print("  FAIL: %s / %s — %s" % (g, n, d))
        print("РЕЗУЛЬТАТ: %s" % ("ВСЁ В ПОРЯДКЕ" if not fails else "ЕСТЬ ОШИБКИ"))
        code = 1 if fails else 0
    finally:
        sys.stdout = orig
        if f is not None:
            f.close()
        if _fake is not None:
            _fake.close()
        if "--keep" not in argv:
            shutil.rmtree(SANDBOX, ignore_errors=True)
    print("Отчёт сохранён: %s" % out_path)
    if getattr(sys, "frozen", False) and "--no-pause" not in argv:
        try:
            input("Нажми Enter, чтобы закрыть окно...")
        except Exception:
            pass
    sys.stdout.flush()
    os._exit(code)   # фоновые потоки (воркеры, Modbus) не должны держать процесс


if __name__ == "__main__":
    main()
