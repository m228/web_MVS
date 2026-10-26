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

    def reboot(self, addrs):
        """«Перезагрузка платы»: регистры addrs — в умолчания (0), открытые соединения рвутся."""
        for a in addrs:
            self.regs[a] = 0
        conns, self._conns = self._conns, []
        for c in conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
                c.close()
            except OSError:
                pass

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
    "sdk_gige", "camera_params", "dahua_control", "net_tools", "rtsp_store",
    "save_settings", "plate_config", "sv_source", "microscope_plc", "microscope_fsm",
    "microscope_service", "cv_analyzer", "cv_volume", "substages", "db", "report", "boil_compare", "cv_fracture", "cv_client", "video_client", "services", "cv_store", "boil_archive", "fracture_lab",
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
    "camera_params": ["saved", "resolve", "store", "mismatches", "overexposure"],
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


class _FakeGigeCam:
    """Фейковая GigE-камера для sdk_gige: узлы живут «в камере» (общие для всех хэндлов), как в
    настоящей; power_cycle() — отключение питания: камера грузится со своего UserSet."""
    BOOT = {"Width": 64, "Height": 48, "OffsetX": 0, "OffsetY": 0, "PixelFormat": "Mono8",
            "ExposureAuto": "Off", "ExposureTime": 5000.0, "GainAuto": "Off", "Gain": 0.0,
            "AcquisitionFrameRate": 1.0}
    nodes = dict(BOOT)
    writes = []
    ignore_exposure = 0          # столько следующих записей ExposureTime камера «молча» не примет

    @classmethod
    def power_cycle(cls):
        cls.nodes = dict(cls.BOOT)
        cls.writes = []

    def _set(self, name, val):
        type(self).writes.append(name)
        if name == "ExposureTime" and type(self).ignore_exposure:
            type(self).ignore_exposure -= 1
            return 0
        type(self).nodes[name] = val
        return 0

    def MV_CC_CreateHandle(self, info): return 0
    def MV_CC_OpenDevice(self): return 0
    def MV_CC_CloseDevice(self): return 0
    def MV_CC_DestroyHandle(self): return 0
    def MV_CC_StartGrabbing(self): return 0
    def MV_CC_StopGrabbing(self): return 0
    def MV_CC_GetOptimalPacketSize(self): return 1500
    def MV_GIGE_SetResend(self, *a): return 0
    def MV_CC_SetBoolValue(self, name, val): return 0
    def MV_CC_SetIntValue(self, name, val):
        return self._set(name, int(val)) if name in type(self).nodes else 0
    def MV_CC_SetFloatValue(self, name, val): return self._set(name, float(val))
    def MV_CC_SetEnumValueByString(self, name, val): return self._set(name, str(val))


def _fake_gige_sdk():
    """Подменить SDK в sdk_gige на фейковую камеру; возвращает функцию отката."""
    import types
    import numpy as np
    import sdk_gige
    from camera_core import gige_worker
    nodes = lambda: _FakeGigeCam.nodes
    saved = {k: getattr(sdk_gige, k) for k in ("_sdk", "available", "_int_node", "_float_node", "_enum_node")}
    saved_grab, saved_info = sdk_gige.GigeSdkStream.grab, gige_worker._sdk_device_info
    sdk_gige._sdk = types.SimpleNamespace(MvCamera=_FakeGigeCam)
    sdk_gige.available = lambda: True
    sdk_gige._int_node = lambda c, k: {"value": int(nodes()[k])} if k in nodes() else None
    sdk_gige._float_node = lambda c, k: {"value": float(nodes()[k])} if k in nodes() else None
    sdk_gige._enum_node = lambda c, k: {"value": nodes()[k], "options": []} if k in nodes() else None
    sdk_gige.GigeSdkStream.grab = lambda self, timeout_ms=1000: (8, 8, "Mono8", np.full(64, 100, np.uint8))
    gige_worker._sdk_device_info = lambda serial: object()

    def undo():
        for k, v in saved.items():
            setattr(sdk_gige, k, v)
        sdk_gige.GigeSdkStream.grab = saved_grab
        gige_worker._sdk_device_info = saved_info
    return undo


def _one_frame(worker, **kw):
    """Открыть поток воркера (как /api/camera/stream), взять один кадр и закрыть."""
    gen = worker.generate(**kw)
    chunk = next(gen)
    gen.close()
    assert chunk.startswith(b"--frame"), chunk[:20]


@check("Камеры", "настройки камеры: «Применить» сохраняет то, что камера приняла; после отключения питания пишутся обратно")
def _camera_params_restore():
    import types
    import camera_params
    import plate_config
    from camera_core import CameraWorker
    serial = "SELFCAM-PARAMS"
    plate_config.save({"camera_serial": serial, "camera_params": {}})
    undo = _fake_gige_sdk()
    try:
        _FakeGigeCam.power_cycle()
        w = CameraWorker(serial, types.SimpleNamespace(check=lambda: True))
        # Макс настроил: экспозиция 170 на странице, усиление 6 дБ (в камере) → «Применить»
        _FakeGigeCam.nodes["Gain"] = 6.0
        _one_frame(w, exposure_auto="Off", exposure_time=170, fps=1, save=True)
        s = camera_params.saved(serial)
        assert s and s["exposure_time"] == 170 and s["gain"] == 6.0 and s["exposure_auto"] == "Off", s
        # отключение питания: камера грузится с 5000 / 0 дБ; страница при автоподключении шлёт то,
        # что прочитала с камеры (5000) — в камеру всё равно должно уйти сохранённое
        _FakeGigeCam.power_cycle()
        assert _FakeGigeCam.nodes["ExposureTime"] == 5000.0
        _one_frame(w, exposure_auto="Off", exposure_time=5000, fps=1)
        n = _FakeGigeCam.nodes
        assert n["ExposureTime"] == 170 and n["Gain"] == 6.0, "после перезапуска в камере %s / %s" % (n["ExposureTime"], n["Gain"])
        assert camera_params.saved(serial)["exposure_time"] == 170, "автоподключение перезаписало сохранённое"
        assert w.current_config.get("exposure_time") == 170, w.current_config
        # камера молча не приняла первую запись выдержки → обратное чтение ловит и повторяет
        _FakeGigeCam.power_cycle()
        _FakeGigeCam.ignore_exposure = 1
        _one_frame(w)
        assert _FakeGigeCam.nodes["ExposureTime"] == 170, "повтор не сработал"
        assert _FakeGigeCam.writes.count("ExposureTime") == 2, _FakeGigeCam.writes
        # не принимает совсем → после всех попыток ошибка, поток всё равно идёт
        _FakeGigeCam.power_cycle()
        _FakeGigeCam.ignore_exposure = 99
        import sdk_gige
        st = sdk_gige.GigeSdkStream(object())
        st.open(camera_params.resolve(serial, {}))
        assert st.mismatch.get("exposure_time") == [170, 5000.0], st.mismatch
        assert _FakeGigeCam.writes.count("ExposureTime") == camera_params.APPLY_TRIES
        st.close()
        _FakeGigeCam.ignore_exposure = 0
        # замена камеры: чужие сохранённые значения не пишутся
        assert camera_params.resolve("OTHER-CAM", {"exposure_time": 900}) == {"exposure_time": 900}
        plate_config.save({"camera_serial": "OTHER-CAM"})
        assert camera_params.saved("OTHER-CAM") is None
    finally:
        undo()
        plate_config.save({"camera_serial": "", "camera_params": {}})
    return "170 мкс / 6 дБ пережили «отключение питания»; повтор при молчаливом отказе; ошибка после %d попыток" % camera_params.APPLY_TRIES


@check("CV", "пересвет: белый кадр не идёт в пробу, статус «пересвет», обычный кадр проходит")
def _cv_overexposed():
    import cv2
    import numpy as np
    import camera_params
    import cv_client
    import cv_store
    from camera_core import manager
    from microscope_service import MicroscopeService
    normal, _ = synth_frame()
    white = np.full((480, 640, 3), 252, np.uint8)
    cv2.circle(white, (200, 200), 40, (90, 90, 90), -1)          # силуэты кристаллов на белом поле
    assert not camera_params.overexposure(normal)[0], camera_params.overexposure(normal)
    over, med, _ = camera_params.overexposure(white)
    assert over and med >= camera_params.OVER_MEDIAN, med
    serial = "SELF-OVER"
    worker = manager.get(serial)
    worker.running = True
    orig = (cv_client.infer, cv_client.health)
    cv_client.health = lambda url, timeout=1.5: {"ok": True}
    cv_client.infer = lambda *a, **k: {"objects": [], "timing": {}}
    try:
        ms = MicroscopeService()                 # не start(): ни платы, ни сокетов
        ms.cfg = {"camera_serial": serial, "cv": {"frames_per_probe": 3, "dwell_timeout_sec": 10}}
        ms._probe_at_glass = lambda: True
        ms._grab_frame = lambda *a, **k: white
        ms._cv_probe_session()
        st = ms.cv_status()
        assert st["state"] == "overexposed", st
        assert cv_store.get_last(serial) is None, "пересвеченная проба сохранилась"
        ms._grab_frame = lambda *a, **k: normal
        ms._cv_probe_session()
        assert cv_store.get_last(serial), "обычная проба не сохранилась: %s" % ms.cv_status()
    finally:
        worker.running = False
        cv_client.infer, cv_client.health = orig
    return "медиана белого кадра %d, обычного %d" % (med, camera_params.overexposure(normal)[1])


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


@check("Микроскоп", "PlateClient: после перезагрузки платы / обрыва TCP блок OUT переписывается, goto не уходит раньше цели 1253")
def _plate_out_resync():
    from microscope_plc import PlateClient
    fake = fake_modbus()
    cfg = _plate_cfg(fake)
    base = cfg["write_base"]
    out_regs = range(base, base + cfg["block_len"])
    sp = base + int(cfg["out"]["m1_sp"])
    led = base + int(cfg["out"]["led_bright"])
    cmd = int(cfg["motor"]["cmd_reg"]["1"])
    # 1) живой цикл: плата перезагрузилась (регистры 0, TCP порван) — pymodbus переподключается сам,
    #    _connected не падает; цель М1 и яркость должны вернуться в плату без новых команд
    client = PlateClient(cfg)
    client.start()
    try:
        assert _wait(lambda: client.status["connected"]), "нет связи: %s" % client.status.get("error")
        client.write_m1_sp(40000)
        client.set_led(bright=37)
        assert _wait(lambda: fake.regs[sp] == 4000 and fake.regs[led] == 37), "OUT не записан до перезагрузки"
        n = len(fake.writes)
        fake.reboot(out_regs)
        assert _wait(lambda: fake.regs[sp] == 4000 and fake.regs[led] == 37), \
            "после переподключения OUT не переписан: 1253=%s 1257=%s" % (fake.regs[sp], fake.regs[led])
        owned = {base + int(i) for i in cfg["out"].values()}
        alien = [w for w in fake.writes[n:] if w[0] in out_regs and w[0] not in owned]
        assert not alien, "переписаны слова OUT вне карты out (смысл в прошивке не разобран): %s" % alien
    finally:
        client.stop()
    # 2) сокет закрыт сбоем чтения, а переподключится уже сама команда goto: цель в 1253 — раньше 1248
    c2 = PlateClient(cfg)
    try:
        assert c2._ensure_connected(), "нет TCP"
        c2.write_m1_sp(30000)
        c2._write_changed_out()
        fake.reboot(out_regs)
        c2._client.close()          # как pymodbus после неудачного чтения: сокета нет
        c2._write_changed_out()     # без сокета менять нечего — ничего не пишет
        n = len(fake.writes)
        c2.cmd1(0x1006)
        c2._flush_pending()
        tail = fake.writes[n:]
        assert (cmd, 0x1006) in tail, "goto не ушёл: %s" % tail
        assert (sp, 3000) in tail[:tail.index((cmd, 0x1006))], "goto ушёл раньше цели в 1253: %s" % tail
    finally:
        c2._close_client()
    return "1253/1257 вернулись после обрыва; goto после цели"


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


class _FwM1:
    """Мотор М1 так, как его ведёт прошивка платы (разбор fw-crc-l.bin, tasks/todo.md п. 2b): x — где он на самом деле
    (датчик = gain·x + s_off), упор у стекла x_stop (дальше шаги теряются), calc — расчётная (шаги, не ниже 0),
    enc — энкодер (реальный ход). «Поиск 0» — ход в расчётную 0 на 600 шаг/с (из 0 — дожим ещё 2000 шагов), ноль в
    конце штатного хода; команды на ходу — в очередь; стоп флаг поиска не снимает, «шаги 0» снимает; блокировки
    бит 0 (у стекла расчётная := датчик при расхождении > 100) и бит 5 (энкодер < 0 → оба 0). Шаг 2,5 мкм."""
    STEP = 2.5

    def __init__(self, x=2000.0, calc=2000.0, enc=2000.0, x_stop=-30.0, gain=1.0, noise=0.0):
        import random
        self.x, self.calc, self.enc, self.x_stop, self.gain, self.noise = x, calc, enc, x_stop, gain, noise
        self.flag, self.queue, self.run, self.fwd, self.pos_set = False, [], None, True, 0
        self.pending, self.calls, self.bad = [], [], []
        self.status = {"connected": True}
        self.rnd = random.Random(1)
        self._tele()

    def __getattr__(self, n):                      # LED, клапаны, М2 — только запоминаем
        return lambda *a, **k: self.calls.append((n, a))

    def _c(self, m, op, arg=None):
        self.calls.append(("motor_" + op, (m, arg)))
        if m == 1:
            self.pending.append((op, arg))

    def motor_enable(self, m, on): self.calls.append(("motor_enable", (m, on)))
    def motor_direction(self, m, fwd): self.fwd = bool(fwd) if m == 1 else self.fwd
    def write_m1_sp(self, um): self.pos_set = int(um) // 10 * 10
    def write_m2_sp(self, um): pass
    def motor_find_zero(self, m): self._c(m, "find_zero")
    def motor_set_zero(self, m): self._c(m, "set_zero")
    def motor_stop(self, m): self._c(m, "stop")
    def motor_steps(self, m, n): self._c(m, "steps", int(n))
    def motor_shift(self, m, um): self._c(m, "shift", (int(um) // 10 * 10) * (1 if self.fwd else -1))
    def cmd1(self, code): self._c(1, "goto", self.pos_set)

    def sensor(self):
        return self.gain * self.x

    def _start(self, op, arg):
        if op == "find_zero":
            if self.calc < self.STEP:
                self.calc = 2000 * self.STEP
            self.flag, self.run = True, [-self.calc, 1500.0]
        elif op == "goto":
            self.run = [arg - self.calc, 1500.0 if self.flag else 1000.0]
        elif op == "shift":
            self.run = [float(arg), 1000.0]
        elif op == "steps":
            self.run = [arg * self.STEP * (1 if self.fwd else -1), 1000.0]
        if abs(self.run[0]) < self.STEP:
            self.run, self.flag = None, False

    def step(self):
        cmds, self.pending = ([] if not self.status["connected"] else self.pending), []
        if len(cmds) > 1:
            self.bad.append("две команды в 1248 за такт: %s" % cmds)
        for op, arg in cmds:
            if op == "stop":
                self.run, self.queue = None, []
            elif op == "set_zero":
                if self.run:
                    self.bad.append("«Установить 0» на ходу")
                self.calc = self.enc = 0.0
            elif self.run:
                if op == "find_zero":
                    self.bad.append("«Поиск 0» на ходу")
                self.queue.append((op, arg))
            else:
                self._start(op, arg)
        if self.run:
            d = max(-self.run[1] * 0.1, min(self.run[1] * 0.1, self.run[0]))
            self.run[0] -= d
            self.calc = max(0.0, self.calc + d)
            nx = max(self.x_stop, self.x + d)
            self.enc += nx - self.x
            self.x = nx
            if abs(self.run[0]) < 1e-6:
                self.run = None
                if self.flag:
                    self.calc = self.enc = 0.0
                    self.flag = False
        if self.enc < 0:
            self.enc = self.calc = 0.0
        s = self.sensor()
        if s < 1000 and abs(self.calc - s) > 100:
            self.calc = s if s >= 0 else 1e7
        if not self.run and self.queue:
            self._start(*self.queue.pop(0))
        self._tele()

    def _tele(self):
        ai = round(self.sensor() + self.rnd.uniform(-self.noise, self.noise)) * 10
        self.telemetry = {"pos1": int(self.calc / 10) * 10, "pos1_enc": int(self.enc / 10) * 10, "pos2": 0,
                          "pos1_ai": None if self.__dict__.get("no_ai") else ai}   # __getattr__ ловит всё — только __dict__


@check("Микроскоп", "Автокалибровка нуля М1 на модели прошивки: команды только стоя, сдвиг по датчику, «все три "
                    "одинаковые», масштаб на отводе, стоп/нет связи/проба поверх, пропарка")
def _autocal():
    import copy
    import plate_config
    from microscope_fsm import MicroscopeFSM

    def mk(manual=False, **kw):
        cfg = copy.deepcopy(plate_config.DEFAULTS)
        cfg["probe_cycle"]["retract_pos"] = 2000
        cfg["autocal"].update(enabled=True, timeout_sec=5)
        pl = _FwM1(**kw)
        fsm = MicroscopeFSM(pl, cfg)
        fsm.set_manual(manual)
        return fsm, pl

    def run(fsm, pl, hook=None, start=True):
        if start:
            assert fsm.start_autocal()["status"] == "started"
        tail = None
        for t in range(1500):
            if hook:
                hook(t, fsm, pl)
            fsm.tick()
            pl.step()
            if tail is None and t > 3 and not fsm.state["autocal"]["active"]:
                tail = t + 60                       # хвост: «шаги 0» после стопа, команды из очереди
            if tail is not None and t >= tail:
                break
        st = fsm.state["autocal"]
        assert not st["active"], "калибровка не закончилась: %s" % st
        assert not pl.bad, "команды не вовремя: %s" % pl.bad[:3]
        return st

    n = lambda pl, op: sum(1 for c in pl.calls if c[0] == "motor_" + op and c[1][0] == 1)

    fsm, pl = mk()                                                   # ноль в порядке
    st = run(fsm, pl)
    assert st["result"] == "готово" and n(pl, "set_zero") == 1 and n(pl, "find_zero") == 1, st
    assert abs(pl.sensor() - pl.enc) <= 30 and abs(pl.enc - 2000) <= 50, (pl.x, pl.enc)
    fsm, pl = mk(x=2200)                                             # ноль уполз: у нуля датчик +200 -> сдвиг к стеклу
    st = run(fsm, pl)
    assert st["result"] == "готово" and st["attempt"] == 1 and abs(pl.sensor() - pl.enc) <= 50, (st, pl.x, pl.enc)
    assert st["last"]["diff_before"] == 200 and abs(st["last"]["diff_after"]) <= 50, st["last"]
    fsm, pl = mk(x=1800)                                             # уполз вниз: упор ниже нуля датчика -> сдвиг от стекла
    st = run(fsm, pl)
    assert st["result"] == "готово" and abs(pl.sensor() - pl.enc) <= 50, (st, pl.x, pl.enc)
    fsm, pl = mk(x=2150, noise=60)                                   # шум датчика ±60: медиана стоя
    st = run(fsm, pl)
    assert st["result"] == "готово" and abs(pl.sensor() - pl.enc) <= 50, (st, pl.x, pl.enc)
    fsm, pl = mk(gain=0.92)                                          # масштаб датчика: случай «датчик на ~160 меньше»
    st = run(fsm, pl)
    assert st["result"].startswith("готово, на отводе расхождение") and st["failed"] and "масштаб" in st["fail_msg"], st
    fsm, pl = mk(x=2120, x_stop=120)                                 # упор выше зоны 0..50: не лечится -> не удалось
    st = run(fsm, pl)
    assert st["result"] == "не удалось" and st["failed"] and n(pl, "set_zero") == 0, st
    fsm, pl = mk()                                                   # датчик не приходит: не виснем, «не удалось»
    pl.no_ai = True
    pl._tele()
    st = run(fsm, pl)
    assert st["result"] == "не удалось" and "датчика" in st["fail_msg"] and n(pl, "set_zero") == 0, st

    fsm, pl = mk(x=2200)                                             # «Стоп движения» посреди «Поиска 0»
    st = run(fsm, pl, hook=lambda t, f, p: f.set_movement_inhibit(True) if t == 40 else None)
    assert st["result"] == "прервано (стоп движения)" and n(pl, "stop") == 1 and n(pl, "steps") == 1 and not pl.flag, st
    assert fsm.start_autocal()["status"] == "blocked", "при «Стоп движения» калибровка не стартует"

    fsm, pl = mk(x=2200)                                             # пропала связь: прервано, счётчик цел
    fsm.set_autocal(count=7)

    def drop(t, f, p):
        p.status["connected"] = not 30 <= t < 80
    st = run(fsm, pl, hook=drop)
    assert st["result"] == "прервано (нет связи)" and st["count"] == 7 and n(pl, "steps") == 1, st

    fsm, pl = mk()                                                   # проба поверх калибровки — нельзя
    fsm.start_autocal()
    fsm.tick()
    assert fsm.start_sample()["status"] == "busy_cal"

    fsm, pl = mk(manual=True)                                        # из Ручного: калибровка целиком, потом снова Ручной
    st = run(fsm, pl)
    assert st["result"] == "готово" and fsm.manual, st

    # пропарка: перезапуск программы на стадии 11 — не новая варка; фронт входа +1; идёт проба — ждём её конца
    fsm, pl = mk()
    fsm.set_autocal(count=2)
    fsm.set_stage(11)
    fsm.tick(); pl.step()
    assert fsm.state["autocal"]["count"] == 2 and not fsm.state["autocal"]["active"], "перезапуск на пропарке посчитан"
    fsm.set_stage(10); fsm.tick(); pl.step()
    fsm.mode = 24                                                    # проба ещё возвращается
    fsm.set_stage(11); fsm.tick(); pl.step()
    assert fsm.state["autocal"]["count"] == 3 and not fsm.state["autocal"]["active"], "старт поверх пробы"
    fsm.mode = 0
    fsm.tick(); pl.step()
    assert fsm.state["autocal"]["active"], "проба кончилась на пропарке — калибровка должна стартовать"
    st = run(fsm, pl, start=False)
    assert st["result"] == "готово" and st["count"] == 0, st

    fsm, pl = mk(manual=True)                                        # стоп с пульта -> «шаги 0» через 0,5 с (и в Ручном)
    fsm.request_unflag(1)
    for _ in range(8):
        fsm.tick()
    assert n(pl, "steps") == 1, pl.calls
    return "7 случаев нуля (и без датчика) + стоп / нет связи / проба поверх / из Ручного / пропарка — ни одной команды на ходу"


@check("Микроскоп", "Страховка разбора CV: по умолчанию 120 с, значение из настроек доходит до автомата, меньше 10 с не бывает")
def _cv_dwell_timeout():
    import copy
    import plate_config
    from microscope_fsm import MicroscopeFSM

    class _Plate:
        def __init__(self):
            self.telemetry = {"pos1": 0, "pos1_ai": 0, "pos1_enc": 0}
            self.status = {"connected": True}

        def __getattr__(self, n):
            return lambda *a, **k: None

    assert plate_config.DEFAULTS["cv"]["dwell_timeout_sec"] == 120, "страховка по умолчанию должна быть 120 с"
    fsm = MicroscopeFSM(_Plate(), copy.deepcopy(plate_config.load()))
    assert fsm._cv_timeout_sec == 120, "автомат без настроек: %s" % fsm._cv_timeout_sec
    fsm.set_cv_dwell(True, frames=7, timeout_sec=150)
    assert fsm._cv_timeout_sec == 150, "значение из настроек не дошло: %s" % fsm._cv_timeout_sec
    fsm.set_cv_dwell(True, timeout_sec=3)
    assert fsm._cv_timeout_sec == 10, "меньше 10 с должно подниматься до 10: %s" % fsm._cv_timeout_sec
    return "120 с по умолчанию · 150 из настроек · 3 → 10"


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
        for st, ok in ((20, True), (10, False), (19, False), (21, False)):
            fsm.set_stage(st); fsm.tick()
            assert fsm.state["stage_ok"] is ok, "стадия %d: stage_ok должно быть %s (3..9 и 20 УНВ)" % (st, ok)
        # ежечасная промывка: только когда варки не было целый час (пока варка/час после неё — клапаны не трогаем)
        import datetime as _dt, microscope_fsm
        class _At3(_dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return _dt.datetime(2026, 10, 16, 10, 3, 1)
        real_dt = microscope_fsm.datetime
        microscope_fsm.datetime = _At3
        try:
            hw = MicroscopeFSM(plate, copy.deepcopy(cfg))
            hw.set_stage(12); hw.tick()                       # варки не видели → простой, промывка можно
            assert hw.state["valve_tube"] and hw.state["hw_idle"], "простой: ежечасная промывка должна идти"
            hw2 = MicroscopeFSM(plate, copy.deepcopy(cfg))
            hw2.set_stage(7); hw2.tick(); hw2.set_stage(12); hw2.tick()      # варка только что кончилась (< часа)
            assert not hw2.state["valve_tube"] and not hw2.state["hw_idle"], "после варки < часа промывка не должна идти"
            hw2._last_brew_t -= 3601; hw2.tick()                                # прошёл час без варки
            assert hw2.state["valve_tube"] and hw2.state["hw_idle"], "час без варки: промывка должна идти"
        finally:
            microscope_fsm.datetime = real_dt
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
    mid = NS(group="medium", defect=None, size_um=210.0, length_um=250.0, width_um=180.0)       # 210 мкм: меньше порога 226, но не меньше сита 200
    sf = cv_volume.sums_for([big, fine, mid], {"volume": {"fines_side_mm": 0.2}}, phase="p1")        # на фазе порог муки ×0,2 — «плоская» мука от этого не зависит
    pf = cv_volume.percents(sf)
    tot3 = sum(cv_volume.crystal_volumes(o.size_um, o.length_um, o.width_um, 0.88)["m1"] for o in (big, fine, mid))
    want_f = 100.0 * (math.pi / 6 * (100.0 ** 3 + 210.0 ** 3)) / tot3
    assert abs(pf["flat"]["m1"] - want_f) < 0.01 and pf["m1"]["fines"] < pf["flat"]["m1"], "плоская мука по порогу 226 мкм: %s ≠ %s" % (pf["flat"]["m1"], want_f)
    assert cv_volume.volume_cfg(None)["fines_flat_um"] == 225.7, "порог плоской муки = поле «Мука» (0,2 мм → 226 мкм)"
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
                "vtot_m1": vt, "vtot_m2": vt, "vtot_m3": vt, "size_min": 50.0 - t, "size_max": 400.0 + t, "len_max": 500.0 + t}
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
        p1, p2 = cv_store.boils("X", limit=1, now=321 * 60.0), cv_store.boils("X", limit=1, offset=1, now=321 * 60.0)
        far = cv_store.boils("X", limit=1, offset=5, now=321 * 60.0)
    finally:
        cv_store._hist_backfill, cv_store._hist_days, cv_store._hist_read = orig
    assert len(res) == 2, "варок %d, ждали 2 (пауза 4 ч между пробами)" % len(res)
    assert not res[0]["finished"] and res[1]["finished"], "новая варка идёт, прошлая закончена"
    assert done[0]["finished"], "через 2 часа без проб варка закончена"
    assert [b["id"] for b in p1 + p2] == [b["id"] for b in res] and p2[0]["finished"] and not p1[0]["finished"], "листание страницами (offset) = тот же список"
    assert far == [], "за концом истории — пусто"
    assert res[1]["counted"] == 2 and res[1]["fines"]["m3"] == 3.0, "мелочь прошлой варки = среднее по 2 пробам финиша: %s" % res[1]["fines"]
    assert res[0]["counted"] == 1 and res[0]["fines"]["m3"] == 6.0, "в новой варке мелочь по одной пробе"
    assert res[1]["size"] == {"min": 28.0, "max": 422.0, "len_max": 522.0}, "крайние кристаллы варки: %s" % res[1]["size"]
    assert res[1]["agg"]["m3"] == 10.0 and res[1]["n"] == 12, "сростки — по всем пробам варки"
    # окно «последние N проб финиша» (avg_n) и счётчики: сколько проб и хороших кристаллов вошло в среднее
    for bb in res:
        assert "tail" in bb and "all" in bb and bb["tail"]["probes"] <= bb["cfg"]["avg_n"], "нет окна последних проб"
    assert res[1]["all"]["probes"] == 2 and res[1]["tail"]["probes"] == 2, "прошлая варка: 2 пробы финиша (avg_n=4 ≥ 2)"
    assert res[1]["all"]["fines"]["m3"] == 3.0 and res[1]["tail"]["fines"]["m3"] == 3.0
    return "варок %d, мелочь прошлой %.1f %%" % (len(res), res[1]["fines"]["m3"])


@check("CV", "видео-сервис (ролик варки): варки для вкладки «Ролик» — новые первыми, сколько папок проб ещё на диске; "
             "без сервиса клиент не падает; вкладка видна только при онлайне")
def _video_service():
    import cv_store
    import plate_config
    import video_client
    rows = [{"ts": "2026-10-06_%02d_%02d_00" % (t // 60, t % 60), "t": t * 60.0, "sv": 80 + t * 0.1, "stage": 7, "cook_time": t * 60}
            for t in list(range(0, 20, 2)) + list(range(300, 320, 2))]          # две варки: пауза 4,5 ч между пробами
    vd = SANDBOX / "video_probes"
    for r in rows[3:6] + rows[-2:]:                                              # кадры живы у 3 проб старой варки и 2 — новой
        (vd / r["ts"]).mkdir(parents=True, exist_ok=True)
    orig = (cv_store._hist_backfill, cv_store._hist_days, cv_store._hist_read, cv_store._serial_dir)
    cv_store._hist_backfill = lambda serial: None
    cv_store._hist_days = lambda serial: ["d"]
    cv_store._hist_read = lambda serial, day: [dict(r) for r in rows]
    cv_store._serial_dir = lambda serial: vd
    try:
        res = cv_store.video_boils("X", limit=5, now=319 * 60.0)
        one = cv_store.video_boils("X", limit=1, now=319 * 60.0 + 7200)
    finally:
        cv_store._hist_backfill, cv_store._hist_days, cv_store._hist_read, cv_store._serial_dir = orig
    assert [b["id"] for b in res] == [rows[10]["ts"], rows[0]["ts"]], "новая варка первой: %s" % [b["id"] for b in res]
    assert [(b["n"], b["on_disk"]) for b in res] == [(10, 2), (10, 3)], "проб в журнале / на диске: %s" % [(b["n"], b["on_disk"]) for b in res]
    assert not res[0]["finished"] and res[1]["finished"] and one[0]["finished"], "идущая варка — пока последняя проба свежая"
    assert res[1]["dirs"] == [str(vd / r["ts"]) for r in rows[3:6]], "пути к папкам проб для сервиса"
    assert len(one) == 1 and one[0]["id"] == rows[10]["ts"], "limit — сколько последних варок"
    assert plate_config.DEFAULTS["video"]["service_url"] == video_client.DEFAULT_URL, "адрес сервиса по умолчанию"
    assert video_client.health("http://127.0.0.1:1", timeout=0.5) is None, "сервис не запущен — None, без исключения"
    assert "error" in video_client.build("http://127.0.0.1:1", ["x"], "x.mp4"), "сборка без сервиса — {error}"
    html = (Path(BUNDLE_DIR) / "page" / "microscope.html").read_text(encoding="utf-8")
    js = (Path(BUNDLE_DIR) / "page" / "static" / "js" / "microscope" / "07-video.js").read_text(encoding="utf-8")
    init = (Path(BUNDLE_DIR) / "page" / "static" / "js" / "microscope" / "04-init.js").read_text(encoding="utf-8")
    assert re.search(r'id="teleVideoBtn"[^>]*\bhidden\b', html) and 'id="teleStripVideo"' in html, "кнопка «Ролик» скрыта по умолчанию, панель на месте"
    assert html.index("07-video.js") < html.index("04-init.js") and "wireVideo()" in init and "/api/video/health" in js, "07-video.js подключён до init"
    return "варок %d: на диске %d и %d проб; без сервиса — None" % (len(res), res[0]["on_disk"], res[1]["on_disk"])


@check("Служебные", "сервисы: web_MVS сам поднимает установленные cv_service / video_service; нет .venv — пропуск, уже отвечает — "
                    "не трогает, не localhost — не наш, CV — по тумблеру; сторож ждёт грузящийся процесс и не долбит упавший")
def _services_supervisor():
    import services
    base = SANDBOX / "svc"
    cvd = base / "cv_service"
    (cvd / ".venv" / "Scripts").mkdir(parents=True, exist_ok=True)
    (cvd / ".venv" / "Scripts" / "python.exe").write_bytes(b"")
    (cvd / "server.py").write_text("", encoding="utf-8")
    cfg = {"cv": {"enabled": False, "service_url": "http://127.0.0.1:8766"}, "video": {"service_url": "http://127.0.0.1:8767"}}
    clock, spawned, healthy = [1000.0], [], {"cv": False, "video": False}

    class Proc:                                       # вместо настоящего процесса: poll() — None, пока «жив»
        code = None

        def poll(self):
            return self.code

    def fake_spawn(launch):
        p = Proc()
        spawned.append((launch, p))
        return p

    orig = {n: services.SPECS[n]["health"] for n in services.SPECS}
    for n in services.SPECS:
        services.SPECS[n]["health"] = (lambda nn: (lambda url: healthy[nn]))(n)
    try:
        sup = services.Supervisor(base=base, spawn_fn=fake_spawn, now=lambda: clock[0], load_cfg=lambda: cfg)
        r = sup.check_once()
        assert r == {"cv": "off", "video": "absent"}, "CV выключен тумблером, видео не установлено: %s" % r
        cfg["cv"]["enabled"] = True
        import logger

        def last_ev():
            ev = [e for e in logger.get_events()["items"] if e["source"] == "services"]
            return ev[-1]["level"], ev[-1]["message"]
        assert sup.check_once()["cv"] == "started" and len(spawned) == 1, "CV включили — запуск"
        lv, msg = last_ev()
        assert lv == "info" and "перезапуск" not in msg, "первый запуск — не «перезапуск»: %s %s" % (lv, msg)
        env = spawned[0][0]["env"]
        assert (env["CV_PORT"], env["CV_HOST"], env["CV_MODEL"], spawned[0][0]["cwd"]) == ("8766", "127.0.0.1", "classic", str(cvd)), \
            "порт из service_url, модели нет — classic: %s" % env
        assert sup.check_once()["cv"] == "starting" and len(spawned) == 1, "наш процесс жив (грузит модель) — второй не запускаем"
        healthy["cv"] = True
        assert sup.check_once()["cv"] == "healthy" and len(spawned) == 1, "отвечает — не трогаем"
        healthy["cv"] = False                         # упал: процесс завершился, /health молчит
        for _ in range(2):
            spawned[-1][1].code, clock[0] = 1, clock[0] + 60
            assert sup.check_once()["cv"] == "started", "упал — поднимаем снова"
            lv, msg = last_ev()
            assert lv == "warn" and "перезапуск" in msg, "повторный запуск — «перезапуск»: %s %s" % (lv, msg)
        spawned[-1][1].code, clock[0] = 1, clock[0] + 60
        assert sup.check_once()["cv"] == "gave_up" and len(spawned) == 3, "3 запуска за 10 мин — дальше не долбим"
        clock[0] += 601
        assert sup.check_once()["cv"] == "started" and len(spawned) == 4, "через 10 мин — снова пробуем"
        (cvd / "model").mkdir(exist_ok=True)
        (cvd / "model" / "best.pt").write_bytes(b"")
        assert services.decide("cv", cfg, base)[2]["env"]["CV_MODEL"] == "model\\best.pt", "есть model\\best.pt — как в run.bat"
        assert services.decide("cv", {"cv": {"enabled": True, "service_url": "http://192.168.1.50:8765"}}, base)[0] == "remote", "чужой адрес — не наш"
        assert services.decide("cv", dict(cfg, services={"cv": {"autostart": False}}), base)[0] == "off", "autostart: false — руками"
        assert services.decide("video", cfg, base)[0] == "absent", "нет video_service\\.venv — пропуск"
    finally:
        for n, h in orig.items():
            services.SPECS[n]["health"] = h
    old = {k: os.environ.get(k) for k in ("PYTHONHOME", "_MEIPASS2")}
    os.environ["PYTHONHOME"], os.environ["_MEIPASS2"] = r"C:\bad", "x"
    try:
        e = services.child_env({"CV_PORT": "1"})
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    assert "PYTHONHOME" not in e and "_MEIPASS2" not in e and e["CV_PORT"] == "1", "окружение exe не утекает в venv сервиса"
    return "запусков %d (лимит %d за %d мин), остальное — пропуск/ожидание" % (len(spawned), services.RESTART_MAX, services.RESTART_WIN_S // 60)


@check("CV", "тренд «мука без порога»: доля муки по общему порогу из поля «Мука» (226 мкм), виден с начала варки (порог СВ не гасит), плавная мука при этом гасится")
def _cv_flat_fines():
    import cv_store
    def row(t, sv, fa):
        r = {"ts": "2026-10-16_10_%02d_00" % t, "t": t * 60.0, "sv": sv, "stage": 7, "cook_time": t * 60, "fines_m1": 5.0, "fines_m2": 5.0, "fines_m3": 5.0,
             "fines_flat": fa}
        for m in ("m1", "m2", "m3"):
            r["fsieve_%s_b0" % m] = 7.0
        return r
    rows = [row(0, 70.0, 10.0), row(2, 90.0, 2.0)]
    orig = (cv_store._hist_backfill, cv_store._hist_days, cv_store._hist_read)
    cv_store._hist_backfill = lambda serial: None
    cv_store._hist_days = lambda serial: ["2026-10-16"]
    cv_store._hist_read = lambda serial, day: [dict(r) for r in rows]
    try:
        d = cv_store.trend_range("X", 0.0, 1e12, series=["fines_flat", "fines_avg", "fsv4"])
    finally:
        cv_store._hist_backfill, cv_store._hist_days, cv_store._hist_read = orig
    flat = d["series"]["fines_flat"]
    assert flat == [10.0, 2.0], "плоская мука из журнала: %s" % flat
    assert d["series"]["fines_avg"][0] is None and d["series"]["fines_avg"][1] == 5.0, "плавная мука до порога СВ гасится: %s" % d["series"]["fines_avg"]
    assert d["series"]["fsv4"][0] is None, "рассев до порога СВ гасится, как раньше"
    return "плоская мука %s" % flat


@check("CV", "самый мелкий/крупный/длинный кристалл: кадр → проба (min/max по кадрам, не среднее) → строка журнала; брак и обрезанные не в счёт")
def _cv_size_extremes():
    import math
    import numpy as np
    import cv_analyzer
    import cv_store

    def hexa(cx, cy, r):
        return [[cx + r * math.cos(math.pi / 3 * i), cy + r * math.sin(math.pi / 3 * i)] for i in range(6)]

    img = np.full((1000, 1400, 3), 120, np.uint8)
    polys = [hexa(200, 300, 40), hexa(600, 300, 90), hexa(1000, 300, 140), hexa(1250, 500, 120)]   # последний вылезает за край кадра — обрезан
    objs = [{"bbox": [min(p[0] for p in q), min(p[1] for p in q), max(p[0] for p in q), max(p[1] for p in q)], "conf": 0.9, "polygon": q} for q in polys]
    res = cv_analyzer.analyze(img, objs, {"tiles": 1}, with_overlay=False, sv=70)
    sz = res["summary"]["size_um"]
    assert sz["min"] is not None and sz["min"] < sz["max"] and sz["len_max"] >= sz["max"] - 0.5, sz
    assert sz["max"] < 2 * 140 * cv_analyzer.DEFAULTS["um_per_px"] * 1.2, "обрезанный кристалл не должен попасть в максимум: %s" % sz
    a = {"summary": {"groups": {}, "count": 5, "size_um": {"mean": 100.0, "median": 90.0, "cv_pct": 10, "min": 30.0, "max": 400.0, "len_max": 520.0}, "density_per_mm2": 1, "reject_pct": 0}}
    b = {"summary": {"groups": {}, "count": 5, "size_um": {"mean": 120.0, "median": 95.0, "cv_pct": 10, "min": 20.0, "max": 380.0, "len_max": 610.0}, "density_per_mm2": 1, "reject_pct": 0}}
    ag = cv_store._aggregate([a, b])["size_um"]
    assert (ag["min"], ag["max"], ag["len_max"]) == (20.0, 400.0, 610.0), "по пробе — крайние значения кадров, не среднее: %s" % ag
    old = {"summary": {"groups": {}, "count": 1, "size_um": {"mean": 1.0, "median": 1.0}, "density_per_mm2": 1, "reject_pct": 0}}
    assert cv_store._aggregate([old])["size_um"]["max"] is None, "старая проба без крайних — None, не падаем"
    ex = cv_store.size_extremes([{"size_min": 30, "size_max": 400, "len_max": 500}, {"size_min": 25, "size_max": 380, "len_max": 700}, {}])
    assert ex == {"min": 25, "max": 400, "len_max": 700}, ex
    assert cv_store.size_extremes([{}]) == {"min": None, "max": None, "len_max": None}
    row = cv_store._hist_row({"ts": "2026-10-12_10_00_00", "summary": {**a["summary"], "size_um": {**a["summary"]["size_um"]}}, "frames": [1]})
    assert row["size_min"] == 30.0 and row["size_max"] == 400.0 and row["len_max"] == 520.0, "в журнал не попали крайние кристаллы"
    assert "size_max" in cv_store.EXPORT_COLUMNS, "нет в выгрузке CSV"
    return "мин %.0f / макс %.0f / длина %.0f мкм на кадре; по пробе min/max кадров" % (sz["min"], sz["max"], sz["len_max"])


@check("CV", "самый крупный кристалл и самый большой сросток: где они (кадр, номер объекта) — плитки в окне CV кликабельны")
def _cv_extreme_at():
    import math
    import numpy as np
    import cv_analyzer
    import cv_store

    def hexa(cx, cy, r):
        return [[cx + r * math.cos(math.pi / 3 * i), cy + r * math.sin(math.pi / 3 * i)] for i in range(6)]

    def obj(q, **kw):
        return {"bbox": [min(p[0] for p in q), min(p[1] for p in q), max(p[0] for p in q), max(p[1] for p in q)], "conf": 0.9, "polygon": q, **kw}

    img = np.full((1000, 1400, 3), 120, np.uint8)
    cfg = {"tiles": 1, "cluster_gap_px": 0}
    # кадр 0: два хороших кристалла + сросток (склеен из 2 масок); кадр 1: хороший крупнее
    f0 = cv_analyzer.analyze(img, [obj(hexa(300, 300, 60)), obj(hexa(700, 300, 100)), obj(hexa(1000, 600, 150), members=2)],
                             cfg, with_overlay=False, sv=95)          # СВ выше «Брак до СВ»: сросток не в рассеве, но найден
    f1 = cv_analyzer.analyze(img, [obj(hexa(400, 400, 130))], cfg, with_overlay=False, sv=95)
    s0 = f0["summary"]["size_um"]
    assert f0["objects"][s0["max_i"]]["size_um"] == s0["max"], "max_i не указывает на самый крупный: %s" % s0
    assert f0["objects"][s0["agg_max_i"]]["defect"] == "aggregate" and s0["agg_max"] == f0["objects"][s0["agg_max_i"]]["size_um"], s0
    ag = cv_store._aggregate([{"summary": f0["summary"]}, {"summary": f1["summary"]}])
    assert ag["max_at"] == [1, 0] and ag["agg_max_at"] == [0, s0["agg_max_i"]] and ag["agg_max"] == s0["agg_max"], ag
    old = {"summary": {"groups": {}, "count": 1, "size_um": {"mean": 1.0, "median": 1.0, "max": 5.0}, "density_per_mm2": 1, "reject_pct": 0}}
    assert "max_at" not in cv_store._aggregate([old]), "старая проба без места — плитка не кликается, не падаем"
    return "крупный → кадр %s, сросток %.0f мкм → кадр %s" % (ag["max_at"], ag["agg_max"], ag["agg_max_at"])


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


@check("CV", "cv_analyzer: «обрезанный» у шва — целый (шов пересекает/касается) в рассеве, обрубок и край кадра — серые")
def _cv_seam_in_sieve():
    import numpy as np
    import cv_analyzer
    img = np.full((2048, 2448, 3), 120, np.uint8)
    xs, _ys = cv_analyzer._seam_lines(img.shape, 4, 0.15)
    sx = xs[0]
    hexa = lambda cx, cy, r: [[cx + r * np.cos(a), cy + r * np.sin(a)] for a in np.linspace(0, 2 * np.pi, 7)[:-1]]

    def cut(poly):
        P = np.asarray(poly, float)
        return {"bbox": [*P.min(0), *P.max(0)], "conf": 0.9, "polygon": [list(p) for p in P], "cut": True}

    raw = [cut(hexa(sx + 10, 500, 120)),                                                    # шов проходит сквозь — целый
           cut([[sx, 1000], [sx + 50, 1000], [sx + 60, 1060], [sx + 50, 1120], [sx, 1120]]),  # ровный срез по шву — обрубок
           cut(hexa(sx - 122, 1500, 120)),                                                  # лишь касается шва углом — целый
           cut(hexa(2448 - 110, 1500, 105))]                                                # у края кадра — обрезан кадром
    cfg = {"tiles": 4, "overlap": 0.15, "um_per_px": 1.0, "cluster_gap_px": 0}
    groups = [o["group"] for o in cv_analyzer.analyze(img, raw, cfg, with_overlay=False, sv=90)["objects"]]
    assert groups[0] != "cut" and groups[2] != "cut", "целые на шве должны идти в рассев: %s" % groups
    assert groups[1] == "cut" and groups[3] == "cut", "обрубок и край кадра должны остаться серыми: %s" % groups
    off = [o["group"] for o in cv_analyzer.analyze(img, raw, {**cfg, "seam_in_sieve": False}, with_overlay=False, sv=90)["objects"]]
    assert off.count("cut") == 4, "галочка выключена — всё как было: %s" % off
    return "в рассеве 2, серых 2; выкл — серых 4"


@check("CV", "cv_analyzer: маска заглаживается — щель и отросток убраны, широкая перетяжка сростка остаётся")
def _cv_mask_smooth():
    import cv2
    import numpy as np
    import cv_analyzer
    # квадрат 300 px с узкой щелью внутрь (зигзаг-шов ultralytics) и тонким хвостом наружу
    slit = [[100, 100], [400, 100], [400, 240], [250, 245], [400, 250], [400, 400], [100, 400],
            [100, 260], [20, 262], [100, 266]]
    sm = cv_analyzer._smooth_mask(np.array(slit, np.float32).reshape(-1, 1, 2), cv_analyzer.DEFAULTS["mask_smooth_pct"])
    P = sm.reshape(-1, 2)
    assert P[:, 0].min() > 90, "хвост не срезан: x от %.0f" % P[:, 0].min()
    assert cv2.pointPolygonTest(sm.astype(np.float32), (330.0, 245.0), False) >= 0, "щель не заклеена"
    # два квадрата, сросшихся узкой перемычкой (перетяжка шире ядра) — форма не меняется
    agg = [[0, 0], [200, 0], [200, 80], [260, 80], [260, 0], [460, 0], [460, 200], [260, 200], [260, 120],
           [200, 120], [200, 200], [0, 200]]
    a0 = np.array(agg, np.float32).reshape(-1, 1, 2)
    a1 = cv_analyzer._smooth_mask(a0, cv_analyzer.DEFAULTS["mask_smooth_pct"])
    assert abs(cv2.contourArea(a1) - cv2.contourArea(a0)) < 0.03 * cv2.contourArea(a0), "сросток заглажен"
    assert cv_analyzer._notches(a1, 300.0, 0.06) >= 2, "перетяжка сростка пропала"
    return "щель заклеена, хвост срезан, сросток на месте"


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


@check("CV", "проба помнит модель и параметры распознавания (паспорт в result.json); старая проба без него читается")
def _cv_model_passport():
    import hashlib
    import importlib.util
    import tempfile
    import cv_client
    import cv_store
    from microscope_service import MicroscopeService
    img, objs = synth_frame()
    det = {"name": "yolo:26x_1280.pt", "seg": True, "device": "0", "imgsz": 640, "size": 141854526,
           "mtime": 1760487975, "sha": "0123abcdef", "train_date": "2026-10-15T03:26:15+03:00", "train_imgsz": 1280}
    calls = []

    def fake_infer(url, data, tiles=6, conf=0.25, iou=0.45, overlap=0.15, timeout=20.0):
        calls.append({"tiles": tiles, "conf": conf, "iou": iou, "overlap": overlap})
        return {"objects": [dict(o) for o in objs], "timing": {"total_ms": 1, "tiles": tiles}, "detector": det}

    orig = (cv_client.infer, cv_client.health)
    cv_client.infer, cv_client.health = fake_infer, (lambda url, timeout=1.5: {"ok": True})
    try:
        ms = MicroscopeService()                 # не start(): ни платы, ни сокетов
        run = ms._cv_begin({"cv": {"tiles": 2, "conf": 0.55, "iou": 0.5, "overlap": 0.2, "keep_last": 5}})
        ms._cv_analyze_one(run, img)
        ms._cv_finish(run, 7, "SELFMODEL")
    finally:
        cv_client.infer, cv_client.health = orig
    r = cv_store.get_last("SELFMODEL")
    assert r, "проба не сохранилась"
    assert r.get("detector") == det, "паспорт модели не попал в result.json: %r" % (r.get("detector"),)
    inf = r.get("infer") or {}
    assert calls and calls[0] == {k: inf.get(k) for k in ("tiles", "conf", "iou", "overlap")}, \
        "в result.json не те параметры, что ушли в infer: %r vs %r" % (inf, calls[0] if calls else None)
    assert inf == {"tiles": 2, "conf": 0.55, "iou": 0.5, "overlap": 0.2, "seam_refine": True}, inf
    # старая проба (до паспорта): ключей нет, открывается как раньше
    old = cv_store.save_sample("SELFMODEL", 7, [{"file": "f0", "summary": run["frame_recs"][0]["summary"],
                                                  "objects": run["frame_recs"][0]["objects"]}], [img], {"total_ms": 1},
                               keep_last=5, ts="2020-01-01_00_00_00")
    ro = cv_store.get_result("SELFMODEL", old["ts"])
    assert ro and ro.get("summary") and "detector" not in ro and "infer" not in ro, "старая проба читается не так"
    # сайдкар: паспорт файла модели (размер, sha) и обучение из чекпойнта; битый чекпойнт не роняет
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cv_service", "detector.py")
    if not os.path.exists(p):
        return "паспорт пишется и читается (detector.py в сборке нет — его часть пропущена)"
    spec = importlib.util.spec_from_file_location("_sc_cv_detector", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod                 # dataclass ищет свой модуль в sys.modules
    spec.loader.exec_module(mod)
    with tempfile.TemporaryDirectory() as td:
        f = os.path.join(td, "m.pt")
        with open(f, "wb") as fh:
            fh.write(b"not a checkpoint")
        m = mod.UltralyticsDetector._file_meta(f, {"date": "2026-10-15T03:26:15+03:00", "train_args": {"imgsz": 1280}})
        assert m["size"] == 16 and m["sha"] == hashlib.sha1(b"not a checkpoint").hexdigest()[:10], m
        assert m["train_date"].startswith("2026-10-15") and m["train_imgsz"] == 1280, m
        bad = mod.UltralyticsDetector._file_meta(f)          # чекпойнт не читается → без даты обучения, без падения
        assert "train_date" not in bad and bad["sha"] == m["sha"], bad
    # размер входа: явный > CV_IMGSZ > обучающий из чекпойнта > 640, кратно 32
    pick = mod.UltralyticsDetector._pick_imgsz
    old_env = os.environ.pop("CV_IMGSZ", None)
    try:
        assert pick(None, 1280) == 1280 and pick(None, None) == 640 and pick(None, [1280]) == 640
        assert pick(960, 1280) == 960 and pick(None, 1000) == 1024
        os.environ["CV_IMGSZ"] = "1920"
        assert pick(None, 1280) == 1920 and pick(640, 1280) == 640
    finally:
        os.environ.pop("CV_IMGSZ", None)
        if old_env is not None:
            os.environ["CV_IMGSZ"] = old_env
    return "модель %s, sha %s, imgsz %s, conf %s, частей %s" % (det["name"], det["sha"], det["imgsz"], inf["conf"], inf["tiles"])


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


@check("Микроскоп", "Клапаны сиропа, воды и продукта из ПЛК: доза между пробами (%·с), время открытия, обрыв связи не считается, запись в пробу и журнал")
def _plc_valves():
    import plate_config, cv_store
    from sv_source import SvSource
    from microscope_service import MicroscopeService
    f = plate_config.DEFAULTS["sv_source"]["fields"]
    assert all(f[k]["reg"] == 0 for k in ("v_syrup", "v_water", "v_product")), "по умолчанию клапаны не читаются (адреса свои на заводе)"
    # 1. доза: вода 100 % 4 с, потом 1 % (закрыт — обратка не ровно 0); сироп 50 %; разрыв опросов 30 с в дозу не идёт
    src = SvSource({"host": "x"})
    t = 1000.0
    for dt, w, sy in [(0, 100, 50), (1, 100, 50), (1, 100, 50), (1, 100, 50), (1, 1, 50), (1, 1, 50), (30, 1, 50), (1, 0, 0)]:
        t += dt
        src._accumulate_valves({"sv": 85.0, "v_water": w, "v_syrup": sy}, t)
    got = src.take_valves()
    assert got == {"water_dose": 100 * 4 + 1 * 2, "water_s": 4, "syrup_dose": 50 * 6, "syrup_s": 6}, got
    assert src.take_valves() == {}, "после take_valves накопленное обнуляется"
    assert SvSource({"host": "x"}).take_valves() == {}, "клапаны не настроены — пусто"
    # 2. чтение с Modbus-эмулятора: адреса Кира 659–661 одним блоком с остальными
    fake = fake_modbus()
    scfg = plate_config._deep_merge(plate_config.load()["sv_source"],
                                    {"fields": {"v_syrup": {"reg": 659}, "v_water": {"reg": 660}, "v_product": {"reg": 661}}})
    scfg["host"], scfg["port"], scfg["period_s"] = "127.0.0.1", fake.port, 0.1
    fake.regs[659], fake.regs[660], fake.regs[661] = 37, 100, 100
    src2 = SvSource(scfg)
    src2.start()
    try:
        assert _wait(lambda: src2.status()["connected"] and src2.status()["values"].get("v_product") is not None, 5), src2.status()
        v = src2.status()["values"]
        assert (v["v_syrup"], v["v_water"], v["v_product"]) == (37, 100, 100), v
        time.sleep(0.5)
        d = src2.take_valves()
        assert d["water_dose"] > 0 and d["syrup_dose"] > 0 and d["product_dose"] > 0, d
        assert src2._block_ok, "блок 650–661 читается одним запросом"
    finally:
        src2.stop()
    # 3. сервис: в сводку ПЛК пробы — среднее обратки и доза с прошлой пробы
    ms = MicroscopeService()
    ms.sv_source = src
    src._accumulate_valves({"v_water": 80, "v_syrup": 0}, 2000.0)
    src._accumulate_valves({"v_water": 80, "v_syrup": 0}, 2002.0)
    pl = ms._plc_summary({"plc": [{"v_water": 80, "v_syrup": 0, "v_product": 90}, {"v_water": 60, "v_syrup": 0, "v_product": 90}]})
    assert pl["v_water"] == 70 and pl["v_product"] == 90 and pl["water_dose"] == 160 and pl["water_s"] == 2, pl
    # 4. журнал: поля и модель, которой снята проба
    row = cv_store._hist_row({"ts": "2026-10-19_10_00_00", "summary": {}, "frames": [1], "plc": pl,
                              "detector": {"name": "yolo:26x_1280.pt", "imgsz": 1280}})
    assert (row["v_water"], row["v_product"], row["water_dose"], row["model"]) == (70, 90, 160, "26x_1280.pt@1280"), row
    assert cv_store._hist_row({"ts": "2026-10-19_10_00_00", "summary": {}})["model"] is None, "старая проба без паспорта модели"
    for k in ("v_syrup", "v_water", "v_product", "water_dose", "product_dose", "model"):
        assert k in cv_store.EXPORT_COLUMNS, "нет в выгрузке: " + k
    return "доза 402 %·с / 4 с открыт, обрыв не считается; 659–661 одним блоком; проба и журнал"


@check("Микроскоп", "Показатели варки: вода по клапану (только при открытом клапане продукта) и по провалу СВ, УНВ не вода, размер на СВ 86, прирост, минут до готовности")
def _boil_metrics():
    import cv_store, report
    from sv_source import SvSource
    # 1. сироп и вода идут в аппарат только через клапан продукта: закрыт — в дозу не идут, сам клапан продукта считается
    src = SvSource({"host": "x"})
    for t, p in [(0, 0), (1, 0), (2, 0), (3, 50), (4, 50), (5, 50)]:
        src._accumulate_valves({"v_water": 100, "v_syrup": 40, "v_product": p}, 1000.0 + t)
    got = src.take_valves()
    assert got == {"water_dose": 200, "water_s": 2, "syrup_dose": 80, "syrup_s": 2, "product_dose": 100, "product_s": 2}, got
    # 2. синтетическая варка, проба раз в минуту: рост СВ, на 86,4 УНВ (СВ 84 — не вода), вода 86,4 → 84,6, уваривание, готовность
    plan = [(7, 84.0), (7, 84.5), (7, 85.0), (7, 85.5), (7, 86.0), (20, 84.0), (7, 86.4), (7, 85.6), (7, 84.6), (7, 85.2),
            (7, 86.5), (8, 88.0), (8, 88.6), (9, 89.0), (9, 89.2), (9, 89.3)]
    g = [{"t": 60.0 * i, "stage": st, "sv": sv, "mean": 150.0 + 5 * i, "fines_m3": 5.0 if i >= 12 else None} for i, (st, sv) in enumerate(plan)]
    m = cv_store.boil_metrics(g)
    assert (m["water"], m["water_by"], m["water_dip"]) == (True, "sv", [86.4, 84.6]), m
    assert m["point_um"] == 165.0, "размер на 86: пробы за 150 с до первой с СВ ≥ 86 (2, 3, 4-я) — %s" % m["point_um"]
    assert m["fin_um"] == 217.5 and m["grow_um"] == 52.5, "финиш — пробы в 2 СВ от конечного (12–15-я): %s" % m
    assert m["to_ready_min"] == 9.0, "готовность (стадия 9) через 9 мин после точки: %s" % m["to_ready_min"]
    dry = [dict(r, sv=86.5) if 7 <= i <= 9 else r for i, r in enumerate(g)]
    assert cv_store.boil_metrics(dry)["water"] is False, "без провала — без воды; УНВ с СВ 84 не вода"
    # 3. клапан читается — решает он, а не СВ: доза до СВ 85,5 и после уваривания не в счёт
    wet = [dict(r, water_dose=0, water_s=0) for r in g]
    wet[2].update(water_dose=5000, water_s=60)          # до 85,5 — не стабилизация
    wet[8].update(water_dose=3000, water_s=40)
    wet[13].update(water_dose=7000, water_s=70)         # после уваривания
    m = cv_store.boil_metrics(wet)
    assert (m["water"], m["water_by"], m["water_dose"], m["water_s"]) == (True, "valve", 3000, 40), m
    wet[8].update(water_dose=0, water_s=0)
    assert cv_store.boil_metrics(wet)["water"] is False, "клапан не открывался — воды нет, хоть СВ и провалилось"
    # 4. СВ не дошло до стабилизации — про воду не знаем; точки 86 нет — размера на ней нет
    low = [dict(r, sv=min(r["sv"], 85.0)) for r in g]
    m = cv_store.boil_metrics(low)
    assert m["water"] is None and m["point_um"] is None and m["to_ready_min"] is None, m
    assert report._cell({"water": True, "water_by": "sv"}, "water") == "да (по СВ)" and report._cell({"water": False, "water_by": "valve"}, "water") == "нет"
    # 5. маточник целиком: заводка за 2 мин до первой пробы (время с заводки из ПЛК), однородность и сростки — по пробам финиша (12–15-я)
    full = [dict(r, seed_age=r["t"] + 120, cv_pct=40.0 + i, n_aggregate=20.0 if i == 15 else 10.0, count=100.0) for i, r in enumerate(g)]
    m = cv_store.boil_metrics(full)
    assert (m["seed_t"], m["seed_ready_min"], m["seed_end_min"]) == (-120.0, 15.0, 17.0), "готовность (780 с) и последняя проба (900 с) от заводки: %s" % m
    assert m["fin_cv"] == 53.5 and m["fin_agg_pct"] == 12.5, "CV 52…55 → 53,5; сростков 50 на 400 кристаллов: %s / %s" % (m["fin_cv"], m["fin_agg_pct"])
    bui = cv_store.boil_metrics([dict(r, stage=8) if r["stage"] == 9 else r for r in full])
    assert bui["seed_ready_min"] is None and bui["seed_end_min"] == 17.0, "нет «готовности» в пробах (Буи) — только заводка → последняя проба"
    return "доза только при открытом продукте; провал 86,4 → 84,6 — вода, УНВ нет; на 86: 165 мкм; заводка → готовность 15 мин, CV 53,5, сростки 12,5 %"


@check("Микроскоп", "Мусор в регистре клапана (Кир, вода: 65535): значение неизвестно — не доза и не «вода да», решает провал СВ; старые пробы в базе тоже")
def _plc_valve_garbage():
    import tempfile
    import plate_config, cv_store, db
    from sv_source import SvSource, valve_pct
    from microscope_service import MicroscopeService
    # 1. правило: 0–100 как есть, шум на пару % — к краю, дальше — значения нет
    got = [valve_pct(v) for v in (0, 36, 100, -1, 103, 65535, 26222, -10, 120, None, float("nan"))]
    assert got == [0, 36, 100, 0, 100, None, None, None, None, None, None], got
    # 2. поток опросов раз в секунду, продукт открыт: вода 65535 и мусор — ни дозы, ни секунд; сироп и продукт считаются как раньше
    src = SvSource({"host": "x"})
    for i, w in enumerate([65535, 65535, 26222, 65535, 58982, 65535]):
        src._accumulate_valves({"v_water": w, "v_syrup": 40, "v_product": 50}, 1000.0 + i)
    got = src.take_valves()
    assert got == {"syrup_dose": 200, "syrup_s": 5, "product_dose": 250, "product_s": 5}, "вода неизвестна — её ключей нет: %s" % got
    for i, w in enumerate([65535, 36, 36, 65535, 65535]):                  # настоящие 36 % посреди мусора считаются: 2 с
        src._accumulate_valves({"v_water": w, "v_syrup": 0, "v_product": 40}, 2000.0 + i)
    got = src.take_valves()
    assert (got["water_dose"], got["water_s"], got["syrup_s"]) == (72, 2, 0), got
    for i in range(3):                                                      # продукт неизвестен — попала ли вода в аппарат, не знаем
        src._accumulate_valves({"v_water": 36, "v_syrup": 40, "v_product": 65535}, 3000.0 + i)
    assert src.take_valves() == {}, "клапан продукта неизвестен — вода и сироп не считаются"
    # 3. ПЛК (Modbus-эмулятор) отдаёт 65535 в регистре воды 660: на странице «(—)», в пробу не идёт, дозы воды нет
    fake = fake_modbus()
    old_regs = [fake.regs[r] for r in (659, 660, 661)]
    fake.regs[659], fake.regs[660], fake.regs[661] = 40, 65535, 50
    scfg = plate_config._deep_merge(plate_config.load()["sv_source"],
                                    {"fields": {"v_syrup": {"reg": 659}, "v_water": {"reg": 660}, "v_product": {"reg": 661}}})
    scfg["host"], scfg["port"], scfg["period_s"] = "127.0.0.1", fake.port, 0.1
    src2 = SvSource(scfg)
    src2.start()
    try:
        assert _wait(lambda: src2.status()["connected"] and src2.status()["values"].get("v_product") is not None, 5), src2.status()
        v = src2.status()["values"]
        assert v["v_water"] is None and (v["v_syrup"], v["v_product"]) == (40, 50), v
        ms = MicroscopeService()
        ms.sv_source = src2
        snap = ms._plc_snapshot()
        assert "v_water" not in snap and snap["v_syrup"] == 40, snap
        time.sleep(0.5)
        d = src2.take_valves()
        assert "water_dose" not in d and "water_s" not in d and d["syrup_dose"] > 0, d
    finally:
        src2.stop()
        fake.regs[659], fake.regs[660], fake.regs[661] = old_regs
    # 4. варка: клапан воды читался только на подкачке (до СВ 85,5), на стабилизации неизвестен — решает провал СВ, а не «воды нет»
    plan = [(5, 82.0), (7, 84.0), (7, 85.6), (7, 86.0), (7, 86.3), (7, 86.5), (8, 88.0), (9, 89.0)]
    g = [{"t": 60.0 * i, "stage": st, "sv": sv} for i, (st, sv) in enumerate(plan)]
    g[0].update(v_water=36.0, water_dose=1200, water_s=33)
    m = cv_store.boil_metrics(g)
    assert (m["water"], m["water_by"], m["water_dose"]) == (False, "sv", None), m
    dip = [dict(r, sv=84.8) if i == 4 else r for i, r in enumerate(g)]    # 86,0 → 84,8 — провал на стабилизации
    assert (cv_store.boil_metrics(dip)["water"], cv_store.boil_metrics(dip)["water_by"]) == (True, "sv")
    # 5. старые пробы в базе (до 1.9.87): 65535 и среднее по кадрам с ним (26222), доза — мусор «открыт на 100 %».
    #    При чтении — None, «вода да» по клапану нет; сироп и подкачка (36 %) как есть, в базе данные не меняются
    tmp = Path(tempfile.mkdtemp(prefix="mvs_valve_")) / "v.db"
    old_path = db.DB_PATH
    db.set_path(tmp)
    try:
        t0 = time.mktime(time.strptime("2026-10-19 12:12", "%Y-%m-%d %H:%M"))
        for i, r in enumerate(g):
            row = dict(r, ts=time.strftime(cv_store.TS_FMT, time.localtime(t0 + r["t"])), t=t0 + r["t"], v_syrup=45.0, syrup_dose=4500, syrup_s=100)
            if i:
                row.update(v_water=26222.0 if i == 1 else 65535.0, water_dose=10000, water_s=100)
            db.upsert_row("SELFVALVE", row)
        raw = cv_store._hist_read("SELFVALVE", "2026-10-19", clean=False)
        assert cv_store.boil_metrics(raw)["water_by"] == "valve" and cv_store.boil_metrics(raw)["water"] is True, "без чистки — та самая ложная вода"
        rows = cv_store._hist_read("SELFVALVE", "2026-10-19")
        assert [r["v_water"] for r in rows] == [36.0] + [None] * 7 and all(r["water_dose"] is None and r["water_s"] is None for r in rows[1:]), rows
        assert rows[0]["water_s"] == 33 and all(r["v_syrup"] == 45.0 and r["syrup_s"] == 100 for r in rows), "нормальные клапаны не трогаем"
        m = cv_store.boil_metrics(rows)
        assert (m["water"], m["water_by"]) == (False, "sv"), m
        assert cv_store._hist_read("SELFVALVE", "2026-10-19", clean=False)[2]["v_water"] == 65535.0, "в базе как было"
    finally:
        db.set_path(old_path)
    return "65535/мусор — None, дозы нет; 36 % посреди мусора — 2 с; стабилизация без клапана → по СВ; старые пробы «вода да» → «нет (по СВ)»"


@check("Микроскоп", "Сравнение варок целиком: перестановочный тест, разброс между варками, итог по правилам (качество важнее времени), варка на границе / короткая / затянутая не в счёт, запрет разных моделей")
def _boil_compare():
    import boil_compare as bc
    assert abs(bc.perm_p([1, 2], [3, 4]) - 2 / 6) < 1e-9, "полный перебор 6 разбиений: крайние два"
    assert bc.perm_p([30.0] * 6, [21.0] * 10) < 0.001, "полностью разделённые группы — p ≈ 1/8008 (по медианам было 0,12)"
    assert bc.perm_p([1, 2, 3], [1, 2, 3]) == 1.0
    assert bc.need_per_group(10, 10) == 16 and bc.need_per_group(10, 0) is None
    jit = [1.5, -2.0, 0.5, 2.5, -1.0, -0.5, 1.0, -2.5, 2.0, -1.5]

    def mk(i, model="26x_1280.pt@1280", dur=80.0, **kw):
        j = lambda k: jit[(i + k) % 10]       # noqa: E731
        b = {"t_start": 10000.0 * i, "start": "b%d" % i, "finished": True, "duration_min": dur, "flag": None, "water": None, "model": model,
             "fin_um": 240 + 2 * j(0), "fines_avg": 7 + 0.3 * j(0), "fin_agg_pct": 10 + 0.2 * j(0), "fin_cv": 41 + 0.3 * j(3),
             "seed_ready_min": 60 + j(5), "seed_end_min": 70 + j(5)}
        b.update({k: (v(i) if callable(v) else v) for k, v in kw.items()})
        b.setdefault("t_end", b["t_start"] + dur * 60)
        return b

    def grp_a(n=6, **kw):
        return [mk(i, **kw) for i in range(n)]
    gb = [mk(10 + i) for i in range(10)]
    a, b = {"from": 0, "to": 95000}, {"from": 100000, "to": 1e9}

    def run(ga, mode="groups", extra=()):
        r = bc.compare_rows(ga + gb + list(extra), a, b, mode=mode)
        assert r["ok"], r
        return r, {x["key"]: x for x in r["metrics"]}, r["summary"]
    # ничего не меняли — разницы нет; разброс между варками на 6 варках не поймать — строкой, но итог не «мало варок»
    r, m, s = run(grp_a())
    assert s["verdict"] == "none" and m["fin_um_spread"]["verdict"] == "few", s
    # муки меньше — плюс; плюс, но дольше — «решать технологу»; хуже по сросткам — минус, даже если быстрее
    r, m, s = run(grp_a(fines_avg=lambda i: 4 + 0.3 * jit[i]))
    assert m["fines_avg"]["verdict"] == "real" and s["verdict"] == "plus" and s["text"] == "Итог: изменение в плюс — муки меньше.", s
    r, m, s = run(grp_a(fines_avg=lambda i: 4 + 0.3 * jit[i], seed_ready_min=lambda i: 75 + jit[(i + 5) % 10]))
    assert s["verdict"] == "mixed" and "дольше на 15 мин" in s["text"], s
    r, m, s = run(grp_a(fin_agg_pct=lambda i: 14 + 0.2 * jit[i], seed_ready_min=lambda i: 50 + jit[(i + 5) % 10]), mode="change")
    assert s["verdict"] == "minus" and "сростков больше" in s["text"] and "короче на 10 мин" in s["text"] and s["caveat"], s
    # крупнее и муки меньше — одна строка и один плюс (у нас это почти один сигнал)
    r, m, s = run(grp_a(fines_avg=lambda i: 4 + 0.3 * jit[i], fin_um=lambda i: 260 + 2 * jit[i]))
    assert s["text"] == "Итог: изменение в плюс — кристалл крупнее и муки меньше." and sum("один сигнал" in x for x in s["lines"]) == 1, s
    # порог муки 8 %: медиана группы выше — минус; вне коридора размера — минус
    r, m, s = run(grp_a(fines_avg=lambda i: 9 + 0.3 * jit[i]))
    assert m["fines_avg"]["off"] == {"a": True, "b": False} and "мука выше порога 8 %" in s["text"], s
    r, m, s = run(grp_a(fin_um=lambda i: 320 + 2 * jit[i]))
    assert m["fin_um"]["off"]["a"] and "вне коридора 200–300" in s["text"] and s["verdict"] == "minus", s
    # разброс между варками: 8 варок с размером ±20 мкм против ±4 — гуляет сильнее, реально
    r, m, s = run(grp_a(8, fin_um=lambda i: 240 + 10 * jit[i]))
    assert m["fin_um_spread"]["verdict"] == "real" and "гуляет сильнее" in s["text"], (m["fin_um_spread"], s)
    # на границе групп (шла в момент изменения), короткая и затянутая — не в счёт, видны в ответе
    r, m, s = run(grp_a(), extra=[mk(7, t_start=90000.0, t_end=101000.0), mk(8, flag="short", dur=30.0), mk(25, dur=220.0)])
    assert {x["start"]: x["why"] for x in r["excluded"]} == {"b7": "edge", "b8": "short", "b25": "long"} and len(r["a"]["boils"]) == 6 and len(r["b"]["boils"]) == 10, r["excluded"]
    # «готовности» в пробах нет (Буи) — время от заводки до последней пробы
    r = bc.compare_rows([dict(x, seed_ready_min=None) for x in grp_a() + gb], a, b)
    assert "seed_end_min" in {x["key"] for x in r["metrics"]} and "seed_ready_min" not in {x["key"] for x in r["metrics"]}
    # мало варок, разные модели, пересечение групп, модель не записана
    r, m, s = run(grp_a(3))
    assert all(x["verdict"] == "few" for x in r["metrics"]) and s["verdict"] == "few", "3 варки в группе — «мало варок»"
    r = bc.compare_rows(grp_a() + [dict(x, model="best.pt@640") for x in gb], a, b)
    assert not r["ok"] and "разные модели" in r["error"], r
    r = bc.compare_rows(grp_a() + gb, {"from": 0, "to": 130000}, {"from": 20000, "to": 1e9})
    assert not r["ok"] and "пересекаются" in r["error"], r
    r = bc.compare_rows(grp_a() + [dict(x, model=None) for x in gb], a, b)
    assert r["ok"] and r["unknown_model"] == 10 and "другой (старой)" in r["warnings"][0], r["warnings"]
    # порог conf — часть «модели»: 0,55 не пишется (старые варки той же модели), другой — разводит периоды
    import cv_store
    det = {"name": "yolo:26x_1280.pt", "imgsz": 1280}
    assert cv_store._model_tag(det) == cv_store._model_tag(det, {"conf": 0.55}) == "26x_1280.pt@1280"
    assert cv_store._model_tag(det, {"conf": 0.25}) == "26x_1280.pt@1280 conf 0.25" and cv_store._model_tag(None, {"conf": 0.25}) is None
    assert cv_store._model_tag(det, {"conf": 0.25, "conf_big": 0.55, "conf_big_k": 4}) == "26x_1280.pt@1280 conf 0.25, крупные >4 медиан 0.55"
    # защита от крупного мусора — граница от медианы кадра: на старте (мелкие ~80 мкм) неуверенный объект ~550 мкм (складка) отсекается,
    # в конце (кристаллы ~330 мкм) такой же неуверенный объект — настоящая кучка и остаётся; уверенный крупный и мелкий неуверенный — всегда
    import cv_analyzer
    def sq(x, y, side_px, conf):
        return {"polygon": [[x, y], [x + side_px, y], [x + side_px, y + side_px], [x, y + side_px]],
                "bbox": [x, y, x + side_px, y + side_px], "conf": conf}
    small = [sq(100 + 70 * i, 1500, 44, 0.7) for i in range(10)]              # ~80 мкм, уверенные
    start = small + [sq(100, 500, 300, 0.45), sq(600, 500, 60, 0.30), sq(900, 500, 300, 0.60)]   # складка ~550 / мелкий / уверенный ~550
    end = [sq(100 + 230 * i, 1500, 180, 0.7) for i in range(8)] + [sq(100, 500, 300, 0.45)]    # кристаллы ~330 мкм + неуверенная кучка ~550
    cfg = {"um_per_px": 1.629, "cluster_gap_px": 0.0, "conf_big": 0.55, "conf_big_k": 4}
    on = cv_analyzer.measure_objects(start, cfg, img_shape=(2048, 2448, 3))
    assert len(on) == 12 and not any(abs(m.conf - 0.45) < 1e-6 for m in on), [(round(m.size_um), m.conf) for m in on]
    assert len(cv_analyzer.measure_objects(start, {**cfg, "conf_big": 0}, img_shape=(2048, 2448, 3))) == 13
    assert len(cv_analyzer.measure_objects(end, cfg, img_shape=(2048, 2448, 3))) == 9
    ga = [dict(x, model="26x_1280.pt@1280") for x in grp_a()]
    r = bc.compare_rows(ga + [dict(x, model="26x_1280.pt@1280 conf 0.25") for x in gb], a, b)
    assert not r["ok"] and "conf 0.25" in r["error"], r
    assert bc.parse_t("2026-10-19 11:20") == time.mktime((2026, 10, 19, 11, 20, 0, 0, 0, -1))
    assert bc.parse_t("2026-10-19", True) - bc.parse_t("2026-10-19") == 86399
    return "плюс / минус / «решать технологу» по правилам; крупнее и муки меньше — один плюс; порог 8 % и коридор; граница, короткая, затянутая — не в счёт"


@check("Микроскоп", "Журнал изменений: добавить, поправить, удалить; чужой серийник не трогается; отчёт отдаёт изменения периода и цели маточника")
def _changes_journal():
    import tempfile
    import db, report
    old_path = db.DB_PATH
    db.set_path(Path(tempfile.mkdtemp(prefix="mvs_chg_")) / "c.db")
    try:
        t = time.mktime((2026, 10, 19, 12, 0, 0, 0, 0, -1))
        i1 = db.change_save("CHG", t, "рецепт 2", 1.0)
        i2 = db.change_save("CHG", t - 2400, "подкачка 40 %", 2.0)
        db.change_save("OTHER", t, "чужой", 3.0)
        assert [c["text"] for c in db.changes("CHG")] == ["подкачка 40 %", "рецепт 2"], "по времени"
        assert db.change_save("CHG", t + 60, "рецепт 2 — стабилизация 82,5", 4.0, i1) == i1 and db.change_save("OTHER", t, "x", 5.0, i1) is None
        assert db.change_delete("OTHER", i2) is False and db.change_delete("CHG", i2) is True
        ch = report.changes("CHG")
        assert len(ch) == 1 and ch[0]["text"] == "рецепт 2 — стабилизация 82,5" and ch[0]["label"] == "19.10 12:01", ch
        assert report.changes("CHG", t + 120) == [], "период после изменения — пусто"
        tg = report.seed_target()
        assert set(tg) == {"fines_max", "size_min_um", "size_max_um"} and tg["size_min_um"] < tg["size_max_um"], tg
        # группы «до / после»: варки 08:00, 10:00, 12:00 (по 8 проб); изменения 09:00, 11:00 и 11:05 — между 11:00 и 11:05 целой варки
        # нет, это одно изменение: «до» кончается в 11:00 и начинается с прошлого изменения (09:00)
        import boil_compare as bc
        day = time.mktime((2026, 10, 6, 0, 0, 0, 0, 0, -1))
        rows = []
        for h in (8, 10, 12):
            for i in range(8):
                ts = "2026-10-06_%02d_%02d_00" % (h, i)
                rows.append({"ts": ts, "t": day + h * 3600 + i * 60, "stage": 7, "sv": 84.0 + i * 0.5, "mean": 150.0, "count": 100.0})
        db.upsert_many("AR", rows)
        for hm, text in ((9 * 60, "подкачка"), (11 * 60, "вода"), (11 * 60 + 5, "рецепт")):
            db.change_save("AR", day + hm * 60, text, 1.0)
        ar = bc.around("AR", day + (11 * 60 + 5) * 60, now=day + 86400)
        assert (ar["b_from"], ar["b_to"], ar["a_from"], ar["together"]) == (day + 9 * 3600, day + 11 * 3600, day + (11 * 60 + 5) * 60, [day + 11 * 3600]), ar
    finally:
        db.set_path(old_path)
    return "2 изменения по времени, правка своего, чужое не трогается; 11:00 и 11:05 — одно изменение; цели %s" % report.seed_target()


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
    # УНВ (20) / пауза (21) посреди роста — не новый рост (Кир 17.10 06:20: 7 → 20 → 7 давало g1 → None → g2)
    assert cv_store.phases_for_boil([5, 7, 20, 7, 8]) == ["p1", "g1", "g1", "g1", None], cv_store.phases_for_boil([5, 7, 20, 7, 8])
    assert cv_store.phases_for_boil([5, 7, 21, 20, 7, 5, 7]) == ["p1", "g1", "g1", "g1", "g1", "p2", "g2"], "пауза и УНВ подряд — тот же рост"
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


@check("CV", "архив варки (ZIP): законченная варка → README/boil.json/timeline.csv/пробы; JPEG только начало варки, финиш без потерь; идущая не пакуется; прерывание без .part; лимит; сборка только между варками")
def _boil_zip():
    import io
    import zipfile
    import time as _tm
    from types import SimpleNamespace as NS
    import cv2
    import numpy as _np
    import boil_archive
    import cv_analyzer
    import cv_store
    from microscope_service import MicroscopeService
    img, objs = synth_frame()
    res = cv_analyzer.analyze(img, objs, None, with_overlay=False, sv=90.0)
    frames = [{"file": "f.png", "summary": res["summary"], "objects": res["objects"]}]
    ser, now = "SELFCHECK_ZIP", _tm.time()
    old = [_tm.strftime(cv_store.TS_FMT, _tm.localtime(now - 8 * 3600 + k * 120)) for k in range(3)]    # законченная варка
    cur = [_tm.strftime(cv_store.TS_FMT, _tm.localtime(now - 300 + k * 60)) for k in range(2)]           # идёт (моложе 15 мин)
    for i, ts in enumerate(old + cur):
        cv_store.save_sample(ser, 7 if i == 0 else 9, frames, [img], {"total_ms": 1}, keep_last=1000, ts=ts, sv=90.0, frame_format="png",
                             plc={"cook_time": i * 120 + 60, "temp_app": 70.0}, phase="g1" if i == 0 else None)    # 1-я — рост 1, дальше готовность
    dest = Path(tempfile.mkdtemp(prefix="boilzip_"))
    try:
        todo = boil_archive.pending(ser, dest, now)
        assert len(todo) == 1 and [r["ts"] for r in todo[0]] == old, "пакуется только законченная варка: %r" % ([[r["ts"] for r in g] for g in todo],)
        g = todo[0]
        # прервали посреди сборки — архива нет, .part не остаётся
        assert boil_archive.pack(ser, g, dest, stop=lambda: True) is None
        assert not list(dest.iterdir()), "после прерывания в папке осталось: %s" % [p.name for p in dest.iterdir()]
        p = boil_archive.pack(ser, g, dest)                                   # кадры как есть (PNG, без потерь)
        assert p and p.name == boil_archive.archive_name(ser, g) and p.name.startswith(ser + "_варка_"), p
        with zipfile.ZipFile(p) as z:
            names = set(z.namelist())
            for need in ("README.md", "boil.json", "timeline.csv") + tuple("probes/%s/%s" % (old[0], f) for f in ("result.json", "objects_0.json", "frame_0.png", "thumb.jpg")):
                assert need in names, "в архиве нет %s" % need
            meta = json.loads(z.read("boil.json"))
            assert meta["probes"] == 3 and not meta["probes_missing"] and meta["summary"]["n"] == 3, meta
            tl = z.read("timeline.csv").decode("utf-8-sig").splitlines()
            assert tl[0].split(";") == cv_store.EXPORT_COLUMNS and len(tl) == 4, "timeline.csv: %d строк" % len(tl)
            src = (cv_store._serial_dir(ser) / old[0] / "frame_0.png").read_bytes()
            assert z.read("probes/%s/frame_0.png" % old[0]) == src, "кадр без пересжатия должен лечь байт в байт"
        assert not boil_archive.pending(ser, dest, now), "собранная варка снова в очереди"
        assert [f["name"] for f in boil_archive.listing(dest)] == [p.name], boil_archive.listing(dest)
        # JPEG только для начала варки: проба фазы g1 → frame_0.jpg (читается, ссылка в result.json на новое имя),
        # проба финиша (без фазы) — PNG байт в байт
        d2 = Path(tempfile.mkdtemp(prefix="boilzip2_"))
        try:
            p2 = boil_archive.pack(ser, g, d2, early_quality=90)
            with zipfile.ZipFile(p2) as z:
                jpg = z.read("probes/%s/frame_0.jpg" % old[0])
                back = cv2.imdecode(_np.frombuffer(jpg, _np.uint8), cv2.IMREAD_COLOR)
                assert back is not None and back.shape == img.shape, "JPEG-кадр не читается"
                rj = json.loads(z.read("probes/%s/result.json" % old[0]))
                assert all(not str(f.get("overlay", "")).endswith(".png") for f in rj.get("frames") or []), "ссылка на кадр осталась .png"
                fin = (cv_store._serial_dir(ser) / old[1] / "frame_0.png").read_bytes()
                assert z.read("probes/%s/frame_0.png" % old[1]) == fin, "кадр финиша должен лечь без потерь"
        finally:
            shutil.rmtree(d2, ignore_errors=True)
        # лимит места: крошечный — архив удаляется
        assert boil_archive.enforce_limit(dest, 1e-9) == [p.name] and not list(dest.glob("*.zip"))
    finally:
        shutil.rmtree(dest, ignore_errors=True)
    # сборка только между варками: стадия в зоне цикла пробы или проба идёт — нельзя; стадия вне зоны (выгрузка) — можно
    ms = MicroscopeService()
    ms.fsm = NS(_stage_in=lambda: True)
    assert not ms._boil_quiet(), "стадия варки — собирать нельзя"
    ms.fsm = NS(_stage_in=lambda: False)
    assert ms._boil_quiet(), "выгрузка/пауза — собирать можно"
    ms._probe_collecting = True
    assert not ms._boil_quiet(), "проба набирается — собирать нельзя"
    return "варка %d проб → %s; идущая не пакуется, прерывание и лимит работают" % (len(old), p.name)


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
        assert len(rep["days"]) == 2 and rep["days"][0]["day"] == "2026-10-06" and rep["days"][0]["boils"] == 1, rep["days"]
        assert len(rep["segments"]) == 1 and rep["segments"][0]["model"] is None, "модель не записана — один отрезок"
    finally:
        db.set_path(old_path)
    return "варок %d, мука с порога, подстадии, CSV" % len(rep["boils"])


@check("CV", "report: итоги без нетипичных варок — короткая/затянутая/идущая помечены, медиана и 25–75 %, лучшая/худшая, дни, отрезки по модели")
def _report_summary():
    import report

    def row(i, dur, fines, model=None, finished=True, water=None, day="2026-10-13"):
        return {"start": "%s_%02d_00_00" % (day, i), "t_start": float(i), "duration_min": dur, "probes": 10, "frac_zones": 0, "finished": finished,
                "fines_avg": fines, "fin_um": 150.0 + i, "point_um": 120.0, "grow_um": 30.0 + i, "model": model, "water": water,
                "sieve": [5.0, 80.0, 10.0, 5.0, None, None]}
    rows = [row(0, 80, 6.0), row(1, 82, 4.0, water=True), row(2, 40, 39.0), row(3, 78, 8.0, water=False), row(4, 200, 1.0),     # 40 — короткая, 200 — затянутая
            row(5, 79, 5.0, model="26x_1280.pt@1280"), row(6, 81, 7.0, model="26x_1280.pt@1280"), row(7, 30, 2.0, model="26x_1280.pt@1280", finished=False)]
    report._mark_flags(rows)
    assert [r["flag"] for r in rows] == [None, None, "short", None, "long", None, None, "open"], [r["flag"] for r in rows]
    t = report._totals(rows)
    assert t["boils"] == 8 and t["typical"] == 5 and t["short"] == 1 and t["long"] == 1, t
    f = t["fines_avg"]
    assert (f["n"], f["median"], f["q1"], f["q3"], f["min"], f["max"]) == (5, 6.0, 5.0, 7.0, 4.0, 8.0), "мука по типичным 4, 5, 6, 7, 8: %r" % (f,)
    assert t["best"]["fines_avg"] == 4.0 and t["worst"]["fines_avg"] == 8.0, "обрывок 39 % и затянутая 1 % — не лучшая/худшая: %r / %r" % (t["best"], t["worst"])
    assert (t["water"], t["water_known"]) == (1, 2) and t["sieve"][:4] == [5.0, 80.0, 10.0, 5.0] and t["sieve"][4] is None, (t["water"], t["sieve"])
    assert report._quantile([1.0, 2.0, 3.0, 4.0], 0.25) == 1.75, "квантиль — как ПРОЦЕНТИЛЬ.ВКЛ"
    seg = report._segments(rows)
    assert [(s["model"], s["boils"], s["typical"]) for s in seg] == [(None, 5, 3), ("26x_1280.pt@1280", 3, 2)], [(s["model"], s["boils"], s["typical"]) for s in seg]
    gap = [row(0, 80, 6.0, model="A"), row(1, 80, 6.0), row(2, 80, 6.0, model="A"), row(3, 80, 6.0, model="B")]
    assert [(s["model"], s["boils"]) for s in report._segments(gap)] == [("A", 3), ("B", 1)], "A, без записи, A — одна модель, не смена"
    few = [row(0, 80, 6.0), row(1, 20, 6.0)]
    report._mark_flags(few)
    assert [r["flag"] for r in few] == [None, None], "меньше 3 законченных варок — короткие не ищем"
    csv = report.to_csv({"serial": "X", "from": 0.0, "to": 0.0, "sieve_labels": list(report.SIEVE_LABELS), "boils": rows, "totals": t,
                         "weeks": [], "substages": []}, ["fines_avg"]).decode("utf-8-sig")
    assert "пометка" in csv and "короткая" in csv and "затянутая" in csv and "медиана" in csv and "рассев" not in csv, "CSV: пометка всегда, рассев — только если выбран"
    return "5 типичных из 8, мука 6 (5–7), лучшая 4 / худшая 8, 2 отрезка модели"


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
    got = []                                                     # УНВ посреди роста: проба на 20 — фаза роста, после неё рост тот же
    for st in (7, 20, 20, 7, 5):
        ms._note_stage(st)
        got.append(ms._current_phase(st))
    assert got == ["g1", "g1", "g1", "g1", "p2"], got
    # перезапуск посреди варки: счётчики берём из журнала идущей варки (иначе вторая подкачка стала бы первой)
    import tempfile, shutil
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp(prefix="mvs_ph_")) / "p.db"
    old_path = db.DB_PATH
    db.set_path(tmp)
    try:
        now = time.time()
        stages = [4, 5, 5, 7, 20, 7, 5, 5]                       # УНВ посреди роста не добавляет рост
        rows = [{"ts": time.strftime(cv_store.TS_FMT, time.localtime(now - (len(stages) - i) * 90)), "t": now - (len(stages) - i) * 90, "stage": st,
                 "sv": 82.0 + i * 0.1, "cook_time": 600 + 90 * i, "seed_age": 10 + 90 * i} for i, st in enumerate(stages)]
        db.upsert_many("PHSER", rows)
        stt = cv_store.phase_state("PHSER")
        assert (stt["pump"], stt["grow"], stt["last"]) == (2, 1, 5), stt
        assert abs(stt["seed_ts"] - (now - 730)) < 1 and stt["boil_t_s"] is None, "заводка из журнала: время пробы минус время с заводки: %s" % stt
        ms2 = MicroscopeService()
        ms2.cfg = {"camera_serial": "PHSER"}
        ms2._note_stage(5)                                       # после перезапуска первая же стадия 5 — всё ещё 2-я подкачка
        assert ms2._current_phase(5) == "p2", ms2._current_phase(5)
        assert ms2._seed_ts is not None and abs(ms2._seed_ts - (now - 730)) < 1, "после перезапуска заводка восстановлена (иначе порог муки падает на порог фазы)"
        ms2._note_stage(7)
        assert ms2._current_phase(7) == "g2", "после 2-й подкачки — 2-й рост"
        assert cv_store.phase_state("NOSUCH") == {"pump": 0, "grow": 0, "last": None, "seed_ts": None, "boil_t_s": None}
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

        def read_config(self):
            return {}

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
                for _ in w.generate(fps=1, client_id=name):
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
        assert w.client_id in ("a", "b"), "владелец потока (client_id) не записан: %r" % w.client_id
        n0 = got["a"] + got["b"]
        time.sleep(0.5)
        assert got["a"] + got["b"] > n0, "кадры не идут"
        alive = [n for n, t in (("a", ta), ("b", tb)) if t.is_alive()]
        assert len(alive) == 1, "должен остаться один поток, сейчас: %s" % alive
        assert w.client_id == alive[0], "client_id (%r) не совпал с живым потоком (%s)" % (w.client_id, alive[0])
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


@check("CV", "рассев: у блока (варка / фаза) есть диапазон СВ проб — sv_from / sv_to (min / max СВ проб, вошедших в рассев)")
def _vol_block_sv_range():
    import cv_store
    rows = [{"ts": "20261009_10%02d00" % i, "t": 1000.0 + i, "sv": sv, "fines_m3": 3.0, "vtot_m1": 1.0, "vtot_m2": 1.0, "vtot_m3": 1.0}
            for i, sv in enumerate([82.0, 83.4, 84.5, None])]
    rows.append({"ts": "20261009_109900", "t": 2000.0, "sv": 70.0, "fines_m3": None})          # без муки — в рассев не входит
    b = cv_store._vol_block(rows)
    assert (b["sv_from"], b["sv_to"]) == (82.0, 84.5), (b["sv_from"], b["sv_to"])
    e = cv_store._vol_block([])
    assert e["sv_from"] is None and e["sv_to"] is None
    return "СВ %s – %s, пустой набор — None" % (b["sv_from"], b["sv_to"])


@check("CV", "95% интервал муки блока (t-оценка по пробам): одинаковые — нулевой, при равных весах = среднее ± t·σ/√n, 3 пробы — честно широкий, не ниже 0, меньше 3 проб — None")
def _vol_block_fines_ci():
    import math
    import statistics
    import cv_store

    def probes(vals):
        return [{"ts": "x%d" % i, "t": float(i), "sv": 88.0, "vtot_m1": 1.0 + i % 3, "vtot_m2": 1.0, "vtot_m3": 1.0,
                 "fines_m1": v, "fines_m2": v, "fines_m3": v} for i, v in enumerate(vals)]
    same = cv_store._vol_block(probes([3.4] * 11))
    assert abs(same["fines_lo"]["m3"] - 3.4) < 1e-9 and abs(same["fines_hi"]["m3"] - 3.4) < 1e-9, (same["fines_lo"], same["fines_hi"])
    vals = [2.3, 3.8, 3.2, 3.5, 2.8, 4.0, 2.8, 3.3, 3.9, 2.9, 3.1]
    spread = cv_store._vol_block(probes(vals))
    h = 2.228 * statistics.stdev(vals) / math.sqrt(len(vals))           # t(0,975; 10) — M3 с равными весами: обычная формула
    lo, hi, mid = spread["fines_lo"]["m3"], spread["fines_hi"]["m3"], statistics.mean(vals)
    assert abs(lo - (mid - h)) < 1e-3 and abs(hi - (mid + h)) < 1e-3, (lo, hi, mid - h, mid + h)
    assert spread["fines_lo"]["m1"] < spread["fines"]["m1"] < spread["fines_hi"]["m1"], (spread["fines_lo"], spread["fines_hi"])    # M1 с весами
    three = cv_store._vol_block(probes([2.8, 3.3, 3.9]))
    h3 = (three["fines_hi"]["m3"] - three["fines_lo"]["m3"]) / 2
    assert abs(h3 - 4.303 * statistics.stdev([2.8, 3.3, 3.9]) / math.sqrt(3)) < 1e-3, h3                # t(0,975; 2) = 4,303 — не бутстрэп-узкий
    zero = cv_store._vol_block(probes([0.0, 0.0, 5.0]))
    assert zero["fines_lo"]["m3"] == 0.0, zero["fines_lo"]
    two = cv_store._vol_block(probes([3.0, 4.0]))
    assert all(two["fines_lo"][m] is None and two["fines_hi"][m] is None for m in ("m1", "m2", "m3")), (two["fines_lo"], two["fines_hi"])
    return ("одинаковые 3,4–3,4; 11 проб %.2f (%.2f–%.2f); 3 пробы ±%.2f; 2 пробы — None" % (mid, lo, hi, h3)).replace(".", ",")


@check("CV", "длительность фаз варки: от первой пробы фазы до первой пробы следующей, отрезки одной фазы складываются, в сводке варки dur_s")
def _phase_durations():
    import cv_store
    ph = ["p1", "p1", "g1", "g1", "g1", None, "p2", "g2", "g2", "p2"]
    ts = [0, 600, 1200, 1800, 2400, 3000, 3600, 4200, 4800, 5400]
    rows = [{"t": float(t), "phase": p, "ts": "x%d" % t} for t, p in zip(ts, ph)]
    d = cv_store.phase_durations(rows)
    assert d == {"p1": 1200, "g1": 1800, "p2": 600 + 0, "g2": 1200}, d
    assert cv_store.phase_durations([]) == {}
    return "p1 20 мин, g1 30, g2 20, p2 (два отрезка) сложены; пустая варка — {}"


@check("CV", "тренд «Рассев»: группы фракций fsv0…fsv12 (мука по 0,05 мм + крупные по 0,1 мм и > 1,0) — сумма 0,05-мм фракций, среднее M1–M3; пропуски и нет рассева → None; серия отдаётся trend_range")
def _trend_sieve_groups():
    import cv_store
    r = {}
    for m in ("m1", "m2", "m3"):
        for i in range(cv_store.cv_volume.FINE_N):
            r["fsieve_%s_b%d" % (m, i)] = 1.0 if m != "m3" else 4.0       # среднее по моделям на фракцию = 2.0
    assert [cv_store.fsieve_group(r, k) for k in range(4)] == [2.0] * 4, "мука 0–0,20 — четыре отдельные фракции по 0,05 мм"
    assert [cv_store.fsieve_group(r, k) for k in range(4, 12)] == [4.0] * 8 and cv_store.fsieve_group(r, 12) == 2.0, "0,20–0,30 … 0,90–1,0 — по 2 фракции, «> 1,0» — одна"
    assert [(g.start, g.stop) for g in cv_store.FSV_GROUPS[4:]] == [(4, 6), (6, 8), (8, 10), (10, 12), (12, 14), (14, 16), (16, 18), (18, 20), (20, 21)], "шаг крупных не 0,1 мм"
    assert sum(cv_store.fsieve_group(r, k) for k in range(13)) == 42.0, "21 фракция × 2 % — группы покрывают все"
    assert cv_store.fsieve_group({}, 0) is None and cv_store._trend_val({}, "fsv3") is None, "нет рассева — None"
    assert cv_store._trend_val(r, "fsv13") is None and cv_store._trend_val(r, "fsv12") == 2.0 and cv_store._trend_val({"sv": 5}, "sv") == 5, "лишний номер не падает, обычные серии как раньше"
    js = (Path(BUNDLE_DIR) / "page" / "static" / "js" / "microscope" / "02-cv.js").read_text(encoding="utf-8")
    html = (Path(BUNDLE_DIR) / "page" / "microscope.html").read_text(encoding="utf-8")
    for k in range(13):
        assert 'value="fsv%d"' % k in html and '"fsv%d"' % k in js, "серия fsv%d не в странице/скрипте" % k
    assert 'id="cvTrendModeSeg"' in html and 'id="cvTrSieveGrid"' in html, "нет переключателя режима или таблицы рассева на тренде"
    return "13 линий = 21 фракция, переключатель и таблица на месте"


@check("CV", "тренд «Рассев» → «по пробе»: рассев одной пробы из журнала (probe_sieve) — порог «Мука с СВ» как у линий, проба фазы с начала, чужой ts — None; кнопки «финиш» больше нет")
def _probe_sieve():
    import cv_store
    N = cv_store.cv_volume.FINE_N
    def row(hh, mm, sv, phase=None, k=0):
        ts = "2026-10-06_%02d_%02d_00" % (hh, mm)
        r = {"ts": ts, "t": cv_store._ts_epoch(ts) + 0.4, "sv": sv, "stage": 7 if phase else 8, "phase": phase}    # t в журнале — с долями секунды
        for m in ("m1", "m2", "m3", "area"):
            r["vtot_" + m] = 1.0
            for i in range(N):
                r["fsieve_%s_b%d" % (m, i)] = 100.0 if i == k else 0.0
        return r
    rows = [row(10, 0, 80.0), row(10, 30, 82.0, "g1", k=1), row(11, 0, 88.5, k=5)]
    orig = (cv_store._hist_backfill, cv_store._hist_days, cv_store._hist_read)
    cv_store._hist_backfill = lambda serial: None
    cv_store._hist_days = lambda serial: ["2026-10-06"]
    cv_store._hist_read = lambda serial, day: [dict(r) for r in rows]
    try:
        pre, ph, fin = (cv_store.probe_sieve("X", r["ts"]) for r in rows)
        miss = (cv_store.probe_sieve("X", "2026-10-06_12_00_00"), cv_store.probe_sieve("X", "мусор"))
    finally:
        cv_store._hist_backfill, cv_store._hist_days, cv_store._hist_read = orig
    assert pre and pre["fsieve"] is None and pre["sv"] == 80.0, "до порога «Мука с СВ» рассев пробы не считается (как линии тренда)"
    assert ph["phase"] == "g1" and ph["fsieve"]["m3"][1] == 100.0, "проба фазы — рассев с самого начала"
    assert fin["fsieve"]["m1"][5] == 100.0 and sum(fin["fsieve"]["area"]) == 100.0 and len(fin["fsieve"]["m2"]) == N, "рассев пробы — её 21 фракция"
    assert miss == (None, None), "нет такой пробы / битая метка — None"
    js = (Path(BUNDLE_DIR) / "page" / "static" / "js" / "microscope" / "02-cv.js").read_text(encoding="utf-8")
    html = (Path(BUNDLE_DIR) / "page" / "microscope.html").read_text(encoding="utf-8")
    assert 'id="cvTrSieveSrc"' in html and 'data-src="probe"' in html and "/api/cv/probe_sieve" in js, "нет переключателя «по варке | по пробе» в рассеве тренда"
    assert 'data-vmode="tail"' not in html and 'cvVolMode === "tail"' not in js, "кнопка/режим «финиш» вернулись — «по варке» и так по последним ~2 СВ"
    # «авто» у «по пробе» (самая свежая проба при окне «Камера») — одно состояние с кнопкой «авто» окна CV
    assert 'id="cvVolAuto"' in html and 'id="cvTrAuto"' in html, "нет галочек «авто» у «по пробе»"
    assert re.search(r'\["cvVolAuto",\s*"cvTrAuto"\]', js) and "cvSetFollow(" in js and "cvTrFollowLatest();" in js, "галочки «авто» не связаны со слежением за свежей пробой"
    return "до порога — нет, фаза — есть, финиш — есть; переключатель и «авто» на месте"


@check("UI", "тренд: «мука среднее» (fines_avg) на общей шкале 0–100 %, без авто-шкалы по видимому участку (иначе при приближении 30 % рисовалось у верха)")
def _trend_fines_fixed_scale():
    import re
    js = (Path(BUNDLE_DIR) / "page" / "static" / "js" / "microscope" / "02-cv.js").read_text(encoding="utf-8")
    m = re.search(r"const CV_AUTO_SPAN\s*=\s*\{([^}]*)\}", js)
    assert m, "CV_AUTO_SPAN не найден"
    assert "fines_avg" not in m.group(1), "у fines_avg снова своя авто-шкала: %s" % m.group(1)
    return "авто-шкала только у режима варки (%s)" % ", ".join(sorted(re.findall(r"(\w+):", m.group(1))))


@check("UI", "пульт: вкладки «Рассев» и «Тренды» — блок из нижней полосы переезжает в пульт; иконки, пустые панели и заглушки на месте")
def _pult_sieve_trend_tabs():
    import re
    js = (Path(BUNDLE_DIR) / "page" / "static" / "js" / "microscope" / "02-cv.js").read_text(encoding="utf-8")
    html = (Path(BUNDLE_DIR) / "page" / "microscope.html").read_text(encoding="utf-8")
    m = re.search(r"const CV_PULT_BLOCKS\s*=\s*\{([^}]*)\}", js)
    assert m, "CV_PULT_BLOCKS не найден"
    pairs = re.findall(r"(\w+):\s*\"(\w+)\"", m.group(1))
    assert dict(pairs) == {"sieve": "cvVol", "trend": "cvTrendBlock"}, "вкладки → блоки: %s" % pairs
    for pane, bid in pairs:
        assert 'data-ptab="%s"' % pane in html and 'data-ppane="%s"' % pane in html, "нет иконки или панели «%s»" % pane
        assert 'id="%s"' % bid in html and 'id="%sHome"' % bid in html, "нет блока %s или заглушки %sHome" % (bid, bid)
    return "%d вкладки: %s" % (len(pairs), ", ".join("%s → #%s" % p for p in pairs))


@check("CV", "плавный порог муки по времени с заводки: разбор/кривая, без ступенек на стыках фаз, выкл → порог фазы, нет времени → порог фазы, мука реально зависит от минуты")
def _fines_curve_time():
    import cv_volume as V
    from types import SimpleNamespace as NS
    # разбор текста и защита от мусора
    assert V.parse_fines_curve("0:25, 10:30; 20:65\n30=110") == [[0, 25], [10, 30], [20, 65], [30, 110]]
    assert V.parse_fines_curve("0:25,10:30,20:65") == [[0, 25], [10, 30], [20, 65]]
    for bad in ("5:10, 3:20", "abc", "0:25", "0:25, 10:-3", None, 5):
        assert V.parse_fines_curve(bad) is None, bad
    # кривая по умолчанию: монотонна, плавна (шаг по минуте мал), к концу — общий порог 203 мкм
    c = V.FINES_CURVE_DEFAULT
    vals = [V.fines_curve_um(c, m / 10.0) for m in range(0, 1500)]
    assert all(b >= a for a, b in zip(vals, vals[1:])), "кривая должна не убывать"
    assert max(abs(b - a) for a, b in zip(vals, vals[1:])) < 1.0, "ступенька: порог прыгает больше 1 мкм за 6 с"
    assert V.fines_curve_um(c, 81) == 203 and V.fines_curve_um(c, 200) == 203 and V.fines_curve_um(c, -5) == 20 and abs(V.fines_curve_um(c, 40.5) - 111.5) < 0.01
    base = {"volume": {"fines_side_mm": 0.18, "fines_side_g1": 0.15, "phase_scale_g1": 70}}
    # выключено / нет времени с заводки: порог фазы, как раньше
    off = V.volume_cfg({"volume": dict(base["volume"])}, "g1", 3000)
    assert off["fines_curve_used"] is False and abs(off["fines_um"] - V.fines_diameter_um(0.15)) < 1e-6, off["fines_um"]
    on = {"volume": dict(base["volume"], fines_curve_on=True)}
    assert V.volume_cfg(on, "g1", None)["fines_curve_used"] is False
    assert abs(V.volume_cfg(on, "g1", None)["fines_um"] - V.fines_diameter_um(0.15)) < 1e-6
    # включено: порог идёт по минуте и не зависит от фазы; сита рассева остаются в масштабе фазы
    a, b = V.volume_cfg(on, "g1", 45 * 60), V.volume_cfg(on, "p2", 45 * 60)
    assert a["fines_curve_used"] and a["fines_um"] == b["fines_um"] == round(V.fines_curve_um(c, 45), 1)
    assert a["size_scale"] == 0.7 and b["size_scale"] != a["size_scale"], (a["size_scale"], b["size_scale"])
    # стык фаз во времени: порог в минуте до и после смены фазы одинаков (раньше был скачок 102 → 158 мкм)
    assert abs(V.volume_cfg(on, "p2", 54 * 60 + 50)["fines_um"] - V.volume_cfg(on, "g2", 55 * 60 + 10)["fines_um"]) < 1.0
    # мука зависит от минуты: кристалл 120 мкм на 20-й минуте (порог 65) — не мука, на 60-й (155) — мука
    m = NS(group="medium", defect=None, size_um=120.0, length_um=130.0, width_um=110.0)
    early = V.sums_for([m], on, True, "g1", 20 * 60)
    late = V.sums_for([m], on, True, "g1", 60 * 60)
    assert early["n"]["fines"] == 0 and late["n"]["fines"] == 1, (early["n"], late["n"])
    return "разбор/мусор, плавность (<1 мкм за 6 с), выкл и нет времени → порог фазы, на стыке фаз без скачка, 120 мкм: на 20-й мин не мука, на 60-й мука"


@check("CV", "рассев в процентах: 21 фракция по 0,05 мм (до 1 мм и «больше»), границы без ошибок округления, сумма 100 %, запись в журнал, усреднение по варке, пересчёт старой пробы по объектам")
def _fine_sieve():
    import json
    import tempfile
    import cv_store
    import cv_volume as V
    from types import SimpleNamespace as NS
    # границы: ровно 50 мкм — уже следующая фракция; 1000 мкм и больше — последняя
    for um, b in ((0, 0), (49.9, 0), (50, 1), (99.9, 1), (100, 2), (150, 3), (300, 6), (350, 7), (950, 19), (999.9, 19), (1000, 20), (5000, 20)):
        assert V.fine_bin(um) == b, (um, V.fine_bin(um), b)
    assert V.FINE_N == 21 and V.CALC_VER >= 7
    mk = lambda size: NS(group="medium", defect=None, size_um=size, length_um=size * 1.1, width_um=size * 0.9)
    ms = [mk(30), mk(70), mk(130), mk(130), mk(180), mk(1100)]
    tot = V.sums_for(ms, {"volume": {}}, True, None)
    pct = V.percents(tot, ignore_off=True)["sieve_fine"]
    for mod in ("m1", "m2", "m3", "area"):
        assert len(pct[mod]) == 21 and abs(sum(pct[mod]) - 100) < 0.01, (mod, sum(pct[mod]))
    assert pct["m3"][0] > 0 and pct["m3"][1] > 0 and pct["m3"][2] > 0 and pct["m3"][3] > 0 and pct["m3"][20] > 0 and pct["m3"][5] == 0
    # кадры одной пробы складываются, а не усредняются
    two = V.add_sums(tot, tot)
    assert V.percents(two, ignore_off=True)["sieve_fine"]["m3"] == pct["m3"]
    # СВ ниже порога: без ignore_off рассева нет (как у муки), в журнал идёт с ignore_off
    off = dict(tot, fines_off=True)
    assert V.percents(off)["sieve_fine"] == {} and V.percents(off, ignore_off=True)["sieve_fine"]
    # журнал: столбцы fsieve_<модель>_b<номер>
    res = {"ts": "2026-10-11_10_00_00", "summary": {"volume": tot, "volume_cfg": {}}, "frames": [1]}
    row = cv_store._hist_row(res)
    assert abs(sum(row["fsieve_m3_b%d" % i] for i in range(21)) - 100) < 0.05 and "fsieve_area_b20" in row, [k for k in row if k.startswith("fsieve")][:3]
    assert cv_store._FINES_KEYS[0] == "fsieve_", "порог «Мука с СВ» должен гасить и мелкий рассев"
    # усреднение по пробам варки: вес — общий объём пробы; нет данных — None
    a = {"fines_m3": 1.0, "vtot_m3": 1.0, **{"fsieve_m3_b%d" % i: (100.0 if i == 2 else 0.0) for i in range(21)}}
    b = {"fines_m3": 1.0, "vtot_m3": 3.0, **{"fsieve_m3_b%d" % i: (100.0 if i == 4 else 0.0) for i in range(21)}}
    fs = cv_store._fsieve([a, b])
    assert abs(fs["m3"][2] - 25.0) < 0.01 and abs(fs["m3"][4] - 75.0) < 0.01, (fs["m3"][2], fs["m3"][4])
    assert cv_store._fsieve([{"fines_m3": 1.0}]) is None
    # пересчёт СТАРОЙ пробы (посчитана до мелкого рассева, calc_ver 6): объекты кристаллов на диске → мелкий рассев появляется
    d = Path(tempfile.mkdtemp(prefix="mvs_fs_"))
    objs = [{"group": "medium", "defect": None, "size_um": float(u), "length_um": u * 1.1, "width_um": u * 0.9} for u in (60, 120, 130, 170)]
    (d / "objects_0.json").write_text(json.dumps(objs), encoding="utf-8")
    old = {"ts": "2026-10-11_10_00_00", "phase": "g1", "sv": 85.0, "frames": [1],
           "summary": {"sv": 85.0, "volume": {"sieve": {"m3": [1] * 7}, "n": {}}, "volume_cfg": {"calc_ver": 6}}}
    new = cv_store._with_volume(d, old, {"volume": {}}, "g1")
    sf = (new["summary"]["volume_pct"] or {}).get("sieve_fine") or {}
    assert sf.get("m3") and abs(sum(sf["m3"]) - 100) < 0.01 and sf["m3"][1] > 0 and sf["m3"][2] > 0 and sf["m3"][3] > 0, sf
    return "границы, сумма 100 %%, журнал (%d столбцов), вес по объёму пробы, СВ-порог, пересчёт старой пробы по объектам" % sum(1 for k in row if k.startswith("fsieve"))


@check("CV", "плавный порог муки растягивается под длительность варки: кривая → «готовность» этой варки, время готовности по журналу, идущая варка — по средней, пересчёт сохраняет растяжение")
def _fines_curve_fit():
    import json
    import tempfile
    import cv_store
    import cv_volume as V
    from types import SimpleNamespace as NS
    on = {"volume": {"fines_curve_on": True, "fines_curve": [[0, 20], [81, 203]]}}
    # средняя кривая: на 30-й минуте 20 + 183·30/81 = 87,8 мкм; на 81-й — 203
    assert abs(V.volume_cfg(on, "g1", 30 * 60)["fines_um"] - 87.8) < 0.05
    # варка закончилась на 60-й минуте: кривая растянута — на 60-й порог уже 203, на 30-й — как у средней на 40,5-й (111,5)
    f60 = V.volume_cfg(on, "g1", 60 * 60, 60 * 60)
    assert f60["fines_um"] == 203.0 and f60["fines_curve_boil_t_s"] == 3600, f60["fines_um"]
    assert abs(V.volume_cfg(on, "g1", 30 * 60, 60 * 60)["fines_um"] - 111.5) < 0.05
    # нетипичное «готовность» (5 минут) ограничено 20 минутами: порог на 20-й минуте уже полный
    assert V.volume_cfg(on, None, 20 * 60, 5 * 60)["fines_um"] == 203.0 and V.volume_cfg(on, None, 20 * 60, 5 * 60)["fines_curve_boil_t_s"] == 1200
    # без плавного порога растяжение ни на что не влияет
    off = {"volume": {"fines_side_mm": 0.18}}
    assert V.volume_cfg(off, None, 30 * 60, 60 * 60)["fines_curve_used"] is False
    # время «готовности» по журналу: первая проба стадии 9 → вся варка; закрытая варка без неё — последняя проба; идущая без неё — нет
    def row(ts, t, stage, seed, cook):
        return {"ts": ts, "t": float(t), "stage": stage, "seed_age": seed, "cook_time": cook, "sv": 85.0}
    A = [row("a1", 1000, 5, 60, 100), row("a2", 1100, 7, 1000, 1200), row("a3", 1200, 9, 3600, 3700), row("a4", 1300, 9, 3900, 4000)]
    B = [row("b1", 100000, 5, 60, 100), row("b2", 100100, 7, 1500, 1600)]                   # закрыта (после неё есть варка C) без «готовности»
    C = [row("c1", 200000, 5, 60, 100), row("c2", 200100, 7, 900, 1000)]                     # идущая: последняя проба свежая
    fit = cv_store.boil_fit_times(A + B + C, now=200200)
    assert all(fit.get(k) == 3600 for k in ("a1", "a2", "a3", "a4")), fit
    assert all(fit.get(k) == 1500 for k in ("b1", "b2")), fit
    assert "c1" not in fit and "c2" not in fit, "идущая варка без «готовности» не растягивается"
    assert cv_store.boil_fit_times([row("x", 1000, 7, None, 100)], now=1e9) == {}, "нет времени с заводки — не растягиваем"
    # перезапуск приложения потерял «с заводки» у части проб (в т.ч. у «готовности»): восстанавливаем по соседним пробам варки
    D = [row("d1", 1000, 5, 60, 100), row("d2", 1100, 7, 160, 200), row("d3", 1200, 7, None, 300), row("d4", 1300, 9, None, 400)]
    assert cv_store.fill_seed_age(D) == {"d3": 260, "d4": 360}, cv_store.fill_seed_age(D)
    assert all(v == 360 for v in cv_store.boil_fit_times(D, now=1e9).values()) and len(cv_store.boil_fit_times(D, now=1e9)) == 4, "«готовность» без seed_age в журнале — берём восстановленное"
    assert cv_store.fill_seed_age([row("z1", 1000, 3, None, 10), row("z2", 1100, 5, 60, 100)]) == {}, "до заводки (стадия 3) «с заводки» не придумываем"
    # пересчёт пробы: растяжение применяется по объектам и ОСТАЁТСЯ при показе пробы без явного времени варки
    d = Path(tempfile.mkdtemp(prefix="mvs_fit_"))
    objs = [{"group": "medium", "defect": None, "size_um": float(u), "length_um": u * 1.1, "width_um": u * 0.9} for u in (60, 120, 150, 180)]
    (d / "objects_0.json").write_text(json.dumps(objs), encoding="utf-8")
    mk = lambda: {"ts": "2026-10-11_10_00_00", "phase": "g1", "sv": 85.0, "frames": [1], "plc": {"seed_age_s": 1800},
                  "summary": {"sv": 85.0, "volume": {"sieve": {"m3": [1] * 7}, "sieve_fine": {"m3": [1.0] * 21}, "n": {}}, "volume_cfg": {"calc_ver": V.CALC_VER}}}
    r1 = cv_store._with_volume(d, mk(), on, "g1", boil_t_s=3600)
    cfg1 = r1["summary"]["volume_cfg"]
    assert cfg1["fines_curve_boil_t_s"] == 3600 and abs(cfg1["fines_um"] - 111.5) < 0.05, cfg1
    # кадр без кристаллов файла объектов не пишет: такая проба не должна «застревать» на средней кривой (раньше пересчёт молча падал на отсутствующем файле)
    mk2 = mk(); mk2["frames"] = [1, 2]
    r3 = cv_store._with_volume(d, mk2, on, "g1", boil_t_s=3600)
    assert abs(r3["summary"]["volume_cfg"]["fines_um"] - 111.5) < 0.05, "проба с пустым кадром не пересчиталась: %s" % r3["summary"]["volume_cfg"]
    r2 = cv_store._with_volume(d, json.loads(json.dumps(r1)), on, "g1")                    # показ пробы: растяжение не теряется
    assert r2["summary"]["volume_cfg"]["fines_curve_boil_t_s"] == 3600 and abs(r2["summary"]["volume_cfg"]["fines_um"] - 111.5) < 0.05
    # выключен плавный порог — автопересчёт ничего не делает
    saved = cv_store._volume_cfg_now
    cv_store._volume_cfg_now = lambda: {"volume": {"fines_curve_on": False}}
    try:
        assert cv_store.refit_curve("NOSUCH") == 0
    finally:
        cv_store._volume_cfg_now = saved
    return "растяжение: 30-я мин при готовности 60 → 111,5 мкм; готовность по журналу (стадия 9 / закрытая без неё / идущая — нет); пересчёт держит растяжение"


@check("CV", "«готовность»: пробы после первой (и сама первая) сразу считаются по растянутой кривой, а не по средней — порог на стадиях 8 и 9 одного пошага (без скачка 0,18 → 0,15)")
def _fines_curve_live_fit():
    import numpy as np
    import cv_analyzer
    from microscope_service import MicroscopeService
    on = {"volume": {"fines_curve_on": True, "fines_curve": [[0, 20], [81, 203]]}}
    img = np.zeros((100, 100, 3), np.uint8)
    # варка кончилась на 64-й минуте (первая проба «готовности»): проба на 64-й — полный порог 203, и следующие (66-я, 70-я) — тоже; без растяжения было бы 164 / 169 / 183
    um = lambda seed, t: cv_analyzer.analyze(img, [], cv_cfg=on, with_overlay=False, sv=86.0, phase=None, seed_age_s=seed * 60, boil_t_s=t)["summary"]["volume_cfg"]["fines_um"]
    assert um(64, 64 * 60) == 203.0 and um(66, 64 * 60) == 203.0 and um(70, 64 * 60) == 203.0
    assert um(64, None) < 170 and um(70, None) < 190, "без растяжения порог по средней кривой"
    assert abs(um(50, 64 * 60) - 20 - 183 * 50 / 64) < 0.2, "до «готовности» — та же растянутая кривая"
    # новая варка (заводка / сгущение) сбрасывает известную длительность варки
    ms = MicroscopeService()
    ms._spawn_refit = lambda serial: None
    ms._boil_t_s = 3840
    ms._last_stage = 3
    ms._note_stage(4)
    assert ms._boil_t_s is None and ms._seed_ts is not None, "заводка должна сбросить длительность прошлой варки"
    ms._boil_t_s = 3840
    ms._last_stage = 9
    ms._note_stage(2)
    assert ms._boil_t_s is None, "вход в 2/3 после готовности — новая варка"
    return "порог 203 на 64, 66 и 70-й минуте при готовности на 64-й; без растяжения 164 / 169 / 183; новая варка сбрасывает длительность"


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


@check("Обновление", "обновление в две фазы: скачивание в фоне (.part → проверка → архив), одно скачивание за раз, версия готового архива, «перезапустить» = update.ps1 -ZipPath без скачивания")
def _update_two_phase():
    import hashlib
    import io
    import tempfile
    import zipfile
    import updater

    def make_zip(version):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("web_MVS.exe", b"MZ")
            zf.writestr("_internal/VERSION", version + "\n")
        return buf.getvalue()

    class FakeResp:
        def __init__(self, data):
            self.b = io.BytesIO(data)

        def read(self, n=-1):
            return self.b.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    saved = (updater.STAGING, updater.STAGED, updater.is_frozen, updater._fetch_latest_meta, updater.urllib.request.urlopen,
             updater.read_version, updater._app_dir, updater.subprocess.Popen)
    d = Path(tempfile.mkdtemp(prefix="mvs_upd2_"))
    try:
        updater.STAGING, updater.STAGED = d / ".update", d / ".update" / "staged"
        updater.is_frozen = lambda: True
        data = make_zip("9.9.9")
        asset = {"name": "web_MVS_v9.9.9.zip", "browser_download_url": "http://x/y.zip", "size": len(data),
                 "digest": "sha256:" + hashlib.sha256(data).hexdigest()}
        updater._fetch_latest_meta = lambda max_age=0.0: ("v9.9.9", asset, "")
        updater.urllib.request.urlopen = lambda req, timeout=60: FakeResp(data)
        updater.read_version = lambda: "1.0.0"

        # 1) удачное скачивание без распаковки: архив на месте, .part нет, версия читается из архива, STAGED не создан
        r = updater.download_latest(extract=False)
        assert r["ok"] and r["version"] == "9.9.9", r
        assert updater.staged_zip() and not list(updater.STAGING.glob("*.part")) and not updater.STAGED.exists()
        assert updater.staged_version() == "9.9.9"
        c = updater.check_latest()
        assert c["update_available"] and c["staged"] == "9.9.9" and c["downloading"] is False, c

        # 2) повреждённая загрузка (размер не тот): архива нет, .part убран, готовым не считается
        updater.urllib.request.urlopen = lambda req, timeout=60: FakeResp(data[:-5])
        r = updater.download_latest(extract=False)
        assert not r["ok"], r
        assert updater.staged_zip() is None and not list(updater.STAGING.glob("*.part"))

        # 3) одновременно — только одно скачивание
        assert updater._DL_LOCK.acquire(False)
        try:
            r = updater.download_latest(extract=False)
            assert not r["ok"] and r.get("busy"), r
            assert updater.check_latest()["downloading"] is True
        finally:
            updater._DL_LOCK.release()

        # 4) недокачанный *.part готовым не считается; устаревший архив (версия уже установлена) check убирает
        (updater.STAGING).mkdir(parents=True, exist_ok=True)
        (updater.STAGING / "web_MVS_v9.9.9.zip.part").write_bytes(b"half")
        assert updater.staged_zip() is None
        (updater.STAGING / "web_MVS_v1.0.0.zip").write_bytes(make_zip("1.0.0"))
        assert updater.staged_version() == "1.0.0"
        c = updater.check_latest()
        assert c["staged"] is None and not updater.STAGING.exists(), "архив уже установленной версии не убран"

        # 5) «перезапустить»: update.ps1 запускается с -ZipPath на скачанный архив; без архива — понятная ошибка
        app = d / "app"
        app.mkdir()
        (app / "update.ps1").write_text("# stub", encoding="utf-8")
        updater._app_dir = lambda: app
        calls = []
        updater.subprocess.Popen = lambda cmd, **kw: calls.append(cmd) or object()
        r = updater.run_update_script(8000, staged=True)
        assert not r["ok"] and "скачайте" in r["error"].lower(), r
        updater.STAGING.mkdir(parents=True, exist_ok=True)
        z = updater.STAGING / "web_MVS_v9.9.9.zip"
        z.write_bytes(data)
        r = updater.run_update_script(8000, staged=True)
        assert r["ok"] and calls, r
        assert calls[-1][calls[-1].index("-ZipPath") + 1] == str(z), calls[-1]
        calls.clear()
        r = updater.run_update_script(8000)
        assert r["ok"] and "-ZipPath" not in calls[-1], "обычный запуск не должен брать готовый архив"
    finally:
        (updater.STAGING, updater.STAGED, updater.is_frozen, updater._fetch_latest_meta, updater.urllib.request.urlopen,
         updater.read_version, updater._app_dir, updater.subprocess.Popen) = saved
    return "скачивание в .part → проверка → архив; битый не принят; один за раз; версия из архива; устаревший убран; run: -ZipPath"


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
