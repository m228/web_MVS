"""host_load — сколько этот ПК принимает по сети, чем занят процессор и видеоядро и сколько ещё камер поместится.

Панель «Нагрузка» на /multi (GET /api/host/load). Без сторонних зависимостей: счётчики Windows через ctypes.
  • сеть — GetIfTable2 (байты адаптера и скорость линка), какой адаптер ведёт к камере — GetBestInterface;
  • процессор — GetSystemTimes всего ПК и GetProcessTimes наших процессов (приложение + службы, найденные по порту);
  • видеоядро — счётчик «GPU Engine / VideoDecode» (то же, что график Video Decode в диспетчере задач).
Цифры считаются как среднее между двумя соседними запросами страницы; фоновых потоков нет. Всё, что узнать не удалось, — None.

Запас — оценка, а не обещание: «каждая камера стоит столько же, сколько сейчас в среднем», до порога LIMITS.
"""
from __future__ import annotations

import ctypes
import os
import re
import socket
import struct
import sys
import threading
import time
from typing import Callable, Optional
from urllib.parse import urlparse

MIN_INTERVAL_S = 2.0          # чаще не пересчитываем — отдаём прошлый ответ
MAX_INTERVAL_S = 30.0         # перерыв дольше (страницу закрывали) — начинаем замер заново
LIMITS = {"net": 70.0, "cpu": 85.0, "gpu": 85.0}      # до какой занятости считаем запас, %
MIN_OURS_PCT = 0.5            # наша доля меньше — по ресурсу цену камеры не посчитать
DEFAULT_FPS = 25.0            # частота камеры, пока приложение её само не измерило
CAMERA_SERVICES = ("motion", "level")                 # службы, чья нагрузка растёт с числом камер
SERVICE_PORTS = {"cv": 8765, "video": 8767, "motion": 8768, "level": 8770}
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def camera_key(url: str) -> Optional[tuple]:
    """(адрес, канал) камеры из RTSP-ссылки — основной и дополнительный поток считаются одной камерой. Пароль никуда не уходит."""
    m = re.match(r"^rtsps?://([^/]+)(.*)$", str(url or ""), re.IGNORECASE)
    if not m:
        return None
    host = m.group(1).rsplit("@", 1)[-1].split(":")[0]
    ch = re.search(r"channel=(\d+)", m.group(2))
    return (host, int(ch.group(1)) if ch else 1) if host else None


def estimate(resources: list, fps: float) -> Optional[dict]:
    """resources — [(ключ, занято всего %, из них камерами %, сколько камер дают эту долю)].
    -> {"cameras": сколько ещё таких же камер, "fps": это в кадрах/с, "limit_by": что кончится первым, "by": {ключ: камер}} | None."""
    by = {}
    for key, total, ours, cameras in resources:
        if total is None or ours is None or cameras <= 0 or ours < (0.05 if key == "net" else MIN_OURS_PCT):
            continue
        room = max(0.0, LIMITS[key.split(":")[0]] - total) / (ours / cameras)
        if key.split(":")[0] not in by or room < by[key.split(":")[0]]:
            by[key.split(":")[0]] = room
    if not by:
        return None
    limit_by = min(by, key=by.get)
    return {"cameras": round(by[limit_by], 1), "fps": round(by[limit_by] * fps), "limit_by": limit_by,
            "by": {k: round(v, 1) for k, v in by.items()}}


# ---------- счётчики Windows ----------

class _GUID(ctypes.Structure):
    _fields_ = [("b", ctypes.c_ubyte * 16)]


class _IfRow(ctypes.Structure):          # MIB_IF_ROW2 (netioapi.h)
    _fields_ = [("luid", ctypes.c_uint64), ("index", ctypes.c_uint32), ("guid", _GUID),
                ("alias", ctypes.c_wchar * 257), ("descr", ctypes.c_wchar * 257),
                ("mac_len", ctypes.c_uint32), ("mac", ctypes.c_ubyte * 32), ("perm_mac", ctypes.c_ubyte * 32),
                ("mtu", ctypes.c_uint32), ("type", ctypes.c_uint32), ("tunnel", ctypes.c_int), ("media", ctypes.c_int),
                ("phys_medium", ctypes.c_int), ("access", ctypes.c_int), ("direction", ctypes.c_int),
                ("flags", ctypes.c_ubyte), ("oper", ctypes.c_int), ("admin", ctypes.c_int), ("connect", ctypes.c_int),
                ("network", _GUID), ("conn_type", ctypes.c_int),
                ("tx_speed", ctypes.c_uint64), ("rx_speed", ctypes.c_uint64),
                ("in_octets", ctypes.c_uint64), ("in_counters", ctypes.c_uint64 * 8),
                ("out_octets", ctypes.c_uint64), ("out_counters", ctypes.c_uint64 * 8)]


def _adapters() -> list:
    ip = ctypes.WinDLL("iphlpapi")
    ptr = ctypes.c_void_p()
    if ip.GetIfTable2(ctypes.byref(ptr)) != 0:
        return []
    try:
        count = ctypes.c_uint32.from_address(ptr.value).value
        rows = (_IfRow * count).from_address(ptr.value + 8)
        out = []
        for r in rows:
            if r.flags & 0x02 or r.oper != 1 or r.type == 24:      # фильтр-прослойки, выключенные и loopback не нужны
                continue
            out.append({"index": r.index, "name": r.alias, "hardware": bool(r.flags & 0x01),
                        "link_bps": r.rx_speed, "rx": r.in_octets, "tx": r.out_octets})
        return out
    finally:
        ip.FreeMibTable(ptr)


def _best_interface(host: str) -> Optional[int]:
    """Номер адаптера, через который Windows пойдёт к этому адресу."""
    addr = struct.unpack("<I", socket.inet_aton(socket.gethostbyname(host)))[0]
    index = ctypes.c_uint32()
    rc = ctypes.WinDLL("iphlpapi").GetBestInterface(ctypes.c_uint32(addr), ctypes.byref(index))
    return index.value if rc == 0 else None


def _filetime(ft) -> int:
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def _system_times() -> tuple:
    """(занято, всего) — суммарно по всем ядрам, в единицах по 100 нс."""
    from ctypes import wintypes
    idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
    if not ctypes.WinDLL("kernel32").GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        raise OSError("GetSystemTimes")
    total = _filetime(kernel) + _filetime(user)            # время ядра включает простой
    return total - _filetime(idle), total


def _process_time(pid: int) -> Optional[int]:
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32")
    k.OpenProcess.restype = wintypes.HANDLE
    h = k.OpenProcess(0x1000, False, pid)                  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        t = [wintypes.FILETIME() for _ in range(4)]
        if not k.GetProcessTimes(wintypes.HANDLE(h), *(ctypes.byref(x) for x in t)):
            return None
        return _filetime(t[2]) + _filetime(t[3])           # ядро + пользователь
    finally:
        k.CloseHandle(wintypes.HANDLE(h))


def _listening_pids() -> dict:
    """{порт: pid} слушающих TCP-сокетов IPv4."""
    ip = ctypes.WinDLL("iphlpapi")
    size = ctypes.c_uint32(0)
    ip.GetExtendedTcpTable(None, ctypes.byref(size), False, 2, 3, 0)      # AF_INET, TCP_TABLE_OWNER_PID_LISTENER
    for _ in range(3):
        buf = ctypes.create_string_buffer(size.value + 1024)
        size = ctypes.c_uint32(len(buf))
        if ip.GetExtendedTcpTable(buf, ctypes.byref(size), False, 2, 3, 0) == 0:
            break
    else:
        return {}
    count = struct.unpack_from("<I", buf, 0)[0]
    out = {}
    for i in range(count):
        _state, _addr, port, _raddr, _rport, pid = struct.unpack_from("<6I", buf, 4 + i * 24)
        out[socket.ntohs(port & 0xFFFF)] = pid
    return out


class _PdhValue(ctypes.Structure):
    _fields_ = [("status", ctypes.c_uint32), ("value", ctypes.c_double)]


class _PdhItem(ctypes.Structure):
    _fields_ = [("name", ctypes.c_wchar_p), ("fmt", _PdhValue)]


class _GpuDecode:
    """Загрузка блока декодирования видео. Запрос PDH держим открытым: значение — среднее между соседними сборами."""

    def __init__(self):
        from ctypes import wintypes
        self.pdh = ctypes.WinDLL("pdh")
        self.query, self.counter = wintypes.HANDLE(), wintypes.HANDLE()
        if self.pdh.PdhOpenQueryW(None, None, ctypes.byref(self.query)) != 0:
            raise OSError("PdhOpenQuery")
        path = r"\GPU Engine(*engtype_VideoDecode)\Utilization Percentage"
        if self.pdh.PdhAddEnglishCounterW(self.query, path, None, ctypes.byref(self.counter)) != 0:
            raise OSError("PdhAddEnglishCounter")

    def read(self) -> Optional[list]:
        """[(pid, движок, %)] за время с прошлого вызова; None — данных пока нет."""
        if self.pdh.PdhCollectQueryData(self.query) != 0:
            return None
        size, count = ctypes.c_uint32(0), ctypes.c_uint32(0)
        rc = self.pdh.PdhGetFormattedCounterArrayW(self.counter, 0x200, ctypes.byref(size), ctypes.byref(count), None)
        if rc & 0xFFFFFFFF != 0x800007D2:                  # PDH_MORE_DATA
            return None
        buf = ctypes.create_string_buffer(size.value)
        if self.pdh.PdhGetFormattedCounterArrayW(self.counter, 0x200, ctypes.byref(size), ctypes.byref(count), buf) != 0:
            return None
        out = []
        for item in (_PdhItem * count.value).from_buffer(buf):
            m = re.match(r"pid_(\d+)_(.*)", item.name or "")
            if m and item.fmt.status in (0, 1):            # PDH_CSTATUS_VALID_DATA / NEW_DATA
                out.append((int(m.group(1)), m.group(2), item.fmt.value))
        return out


class WinProbe:
    """Сырые счётчики этого ПК. Тесты подменяют объект целиком."""

    def __init__(self):
        self._gpu = None
        self._gpu_failed = sys.platform != "win32"

    def adapters(self) -> list:
        return _adapters()

    def best_interface(self, host: str) -> Optional[int]:
        return _best_interface(host)

    def system_times(self) -> tuple:
        return _system_times()

    def process_time(self, pid: int) -> Optional[int]:
        return _process_time(pid)

    def listening_pids(self) -> dict:
        return _listening_pids()

    def gpu(self) -> Optional[list]:
        if self._gpu_failed:
            return None
        try:
            if self._gpu is None:
                self._gpu = _GpuDecode()
            return self._gpu.read()
        except Exception:
            self._gpu_failed = True                        # счётчика нет (старая Windows, нет драйвера) — больше не пробуем
            return None


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


class LoadMeter:
    def __init__(self, workers_fn: Callable[[], dict], cameras_fn: Callable[[], list], motion_cfg_fn: Callable[[], dict],
                 motion_state_fn: Callable[[], dict], motion_url_fn: Callable[[], str], probe=None,
                 clock: Callable[[], float] = time.monotonic, pid: Optional[int] = None):
        self.workers_fn, self.cameras_fn = workers_fn, cameras_fn
        self.motion_cfg_fn, self.motion_state_fn, self.motion_url_fn = motion_cfg_fn, motion_state_fn, motion_url_fn
        self.probe = probe or WinProbe()
        self.clock = clock
        self.pid = pid or os.getpid()
        self._lock = threading.Lock()
        self._prev: Optional[dict] = None
        self._last: Optional[dict] = None
        self._fps: dict = {}                     # камера -> последняя измеренная приложением частота (в «Только анализ» она не меряется)

    def read(self) -> dict:
        with self._lock:
            now = self.clock()
            if self._last is not None and self._prev is not None and now - self._prev["t"] < MIN_INTERVAL_S:
                return self._last
            cur = self._counters(now)
            prev = self._prev
            if prev is not None and now - prev["t"] > MAX_INTERVAL_S:
                prev = None
            self._prev = cur
            self._last = self._summary(prev, cur)
            return self._last

    def _counters(self, now: float) -> dict:
        ports = _safe(self.probe.listening_pids, {}) or {}
        wanted = dict(SERVICE_PORTS, motion=_safe(lambda: urlparse(self.motion_url_fn()).port) or SERVICE_PORTS["motion"])
        services = {name: ports[port] for name, port in wanted.items() if port in ports}
        pids = {"app": self.pid, **services}
        return {"t": now,
                "adapters": {a["index"]: a for a in (_safe(self.probe.adapters, []) or [])},
                "system": _safe(self.probe.system_times),
                "procs": {name: (pid, _safe(lambda p=pid: self.probe.process_time(p))) for name, pid in pids.items()},
                "gpu": _safe(self.probe.gpu)}

    def _streams(self) -> dict:
        """Какие камеры сейчас принимает этот ПК: приложением (показ/запись) и сервисом КЗ."""
        app_cams, out_mbps = set(), 0.0
        for w in list((_safe(self.workers_fn, {}) or {}).values()):
            key = camera_key(getattr(w, "rtsp_url", ""))
            if key is None or not getattr(w, "running", False):
                continue
            app_cams.add(key)
            m = w.metrics
            if m.get("fps"):
                self._fps[key] = float(m["fps"])
            out_mbps += float(m.get("bandwidth_mbps") or 0.0) * w.viewer_count()
        motion_cams, remote = set(), (urlparse(_safe(self.motion_url_fn, "") or "").hostname or "127.0.0.1") not in LOCAL_HOSTS
        if not remote:
            st = _safe(self.motion_state_fn, {}) or {}
            live = {c.get("id") for c in st.get("cameras") or [] if c.get("enabled") and c.get("connected")}
            if live:
                cfg = _safe(self.motion_cfg_fn, {}) or {}
                for c in cfg.get("cameras") or []:
                    key = camera_key(c.get("url"))
                    if c.get("id") in live and key is not None:
                        motion_cams.add(key)
        cams = app_cams | motion_cams
        known = [self._fps[k] for k in cams if k in self._fps]
        fps = sum(known) / len(known) if known else DEFAULT_FPS
        return {"app": len(app_cams), "motion": len(motion_cams), "cameras": len(cams), "keys": cams,
                "fps": round(fps, 1), "fps_assumed": not known, "motion_remote": remote,
                "decode_fps": round(sum(self._fps.get(k, fps) for k in app_cams) + sum(self._fps.get(k, fps) for k in motion_cams)),
                "to_browsers_mbps": round(out_mbps, 1)}

    def _routes(self, streams: dict) -> dict:
        """{номер адаптера: сколько камер идёт через него}: и принимаемые сейчас, и сохранённые (чтобы адаптер камер был виден всегда)."""
        saved = {camera_key(e.get("url")) for e in (_safe(self.cameras_fn, []) or [])} - {None}
        out: dict = {}
        for key in saved | streams["keys"]:
            index = _safe(lambda h=key[0]: self.probe.best_interface(h))
            if index is not None:
                out.setdefault(index, 0)
                if key in streams["keys"]:
                    out[index] += 1
        return out

    def _summary(self, prev: Optional[dict], cur: dict) -> dict:
        streams = self._streams()
        routes = self._routes(streams)
        dt = cur["t"] - prev["t"] if prev else 0.0

        adapters = []
        for index, a in cur["adapters"].items():
            if index not in routes and not a["hardware"]:
                continue
            old = prev["adapters"].get(index) if prev else None
            rate = lambda key: (round(max(0, a[key] - old[key]) * 8 / dt / 1e6, 1) if old and dt > 0 else None)
            link = a["link_bps"] / 1e6 if 0 < a["link_bps"] < 10 ** 12 else None
            adapters.append({"name": a["name"], "link_mbps": round(link) if link else None, "rx_mbps": rate("rx"), "tx_mbps": rate("tx"),
                             "cameras": routes.get(index, 0), "camera_link": index in routes})
        adapters.sort(key=lambda a: (not a["camera_link"], -a["cameras"], a["name"]))

        cpu = None
        if prev and prev["system"] and cur["system"] and cur["system"][1] > prev["system"][1]:
            total = cur["system"][1] - prev["system"][1]
            share = {}
            for name, (pid, spent) in cur["procs"].items():
                old_pid, old_spent = prev["procs"].get(name, (None, None))
                if pid == old_pid and spent is not None and old_spent is not None:
                    share[name] = 100.0 * max(0, spent - old_spent) / total
            cpu = {"total": round(min(100.0, 100.0 * max(0, cur["system"][0] - prev["system"][0]) / total), 1),
                   "app": round(share.get("app", 0.0), 1), "motion": round(share.get("motion", 0.0), 1),
                   "other": round(sum(v for k, v in share.items() if k not in ("app", "motion")), 1),
                   "cameras": round(share.get("app", 0.0) + sum(share.get(k, 0.0) for k in CAMERA_SERVICES), 1)}

        gpu = None
        if prev and cur["gpu"] is not None:
            ours = {pid for pid, _ in cur["procs"].values()}
            every, mine = {}, {}
            for pid, engine, value in cur["gpu"]:
                every[engine] = every.get(engine, 0.0) + value
                if pid in ours:
                    mine[engine] = mine.get(engine, 0.0) + value
            gpu = {"total": round(min(100.0, max(every.values(), default=0.0)), 1),
                   "ours": round(min(100.0, max(mine.values(), default=0.0)), 1)}

        resources = []
        for a in adapters:
            if a["cameras"] and a["link_mbps"] and a["rx_mbps"] is not None:
                used = 100.0 * a["rx_mbps"] / a["link_mbps"]
                resources.append(("net", used, used, a["cameras"]))      # приём на адаптере камер — это в основном камеры
        if cpu:
            resources.append(("cpu", cpu["total"], cpu["cameras"], streams["cameras"]))
        if gpu:
            resources.append(("gpu", gpu["total"], gpu["ours"], streams["cameras"]))
        streams = {k: v for k, v in streams.items() if k != "keys"}
        return {"ready": prev is not None, "adapters": adapters, "cpu": cpu, "gpu": gpu, "streams": streams,
                "estimate": estimate(resources, streams["fps"])}
