"""services — web_MVS сам поднимает сайдкары (cv_service, video_service), которые установлены рядом.

Установлен = есть папка <DATA_DIR>\\<сервис> с server.py и .venv\\Scripts\\python.exe (setup.bat отработал). Нет —
пропускаем (одна запись в журнал). /health уже отвечает (запущен руками run.bat или остался с прошлого запуска web_MVS)
— не трогаем. Адрес в конфиге не localhost — сервис на другом компе, не наш. CV — только при включённом тумблере CV.
Выключить по одному: plate_config → services.<cv|video>.autostart = false.

Запуск — отдельный процесс без окна, своя группа + CREATE_BREAKAWAY_FROM_JOB с откатом (как helper в updater.py):
переживает перезапуск и автообновление web_MVS, модель на GPU не перегружается. Вывод — в <папка сервиса>\\service.log.
Сторож раз в минуту: установлен, включён, а /health молчит — поднять заново. Пока наш процесс жив (грузит модель) — ждём;
не чаще RESTART_MAX раз за RESTART_WIN_S, дальше только запись в журнал, чтобы не долбить упавший сервис.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import cv_client
import motion_client
import plate_config
import video_client
from logger import log_event
from paths import DATA_DIR

CHECK_EVERY_S = 60
RESTART_MAX, RESTART_WIN_S = 3, 600
LOG_MAX_BYTES = 5 * 1024 * 1024
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

SPECS = {
    "cv": {"title": "CV-сервис", "dir": "cv_service", "default_url": "http://127.0.0.1:8765", "port_env": "CV_PORT", "host_env": "CV_HOST",
           "url": lambda cfg: (cfg.get("cv") or {}).get("service_url"),
           "enabled": lambda cfg: bool((cfg.get("cv") or {}).get("enabled")),
           "health": lambda url: cv_client.health(url, timeout=1.5) is not None},
    "video": {"title": "Видео-сервис", "dir": "video_service", "default_url": video_client.DEFAULT_URL, "port_env": "VIDEO_PORT", "host_env": "VIDEO_HOST",
              "url": lambda cfg: (cfg.get("video") or {}).get("service_url"),
              "enabled": lambda cfg: True,
              "health": lambda url: video_client.health(url, timeout=1.0) is not None},
    "motion": {"title": "Сервис движения", "dir": "motion_service", "default_url": motion_client.DEFAULT_URL, "port_env": "MOTION_PORT",
               "host_env": "MOTION_HOST",
               "url": lambda cfg: (cfg.get("motion") or {}).get("service_url"),
               "enabled": lambda cfg: True,
               "health": lambda url: motion_client.health(url, timeout=1.0) is not None},
}


def installed(name: str, base: Path = DATA_DIR) -> bool:
    """Сервис стоит рядом: есть <папка>\\server.py и .venv\\Scripts\\python.exe (setup.bat отработал)."""
    d = Path(base) / SPECS[name]["dir"]
    return (d / ".venv" / "Scripts" / "python.exe").is_file() and (d / "server.py").is_file()


def micro_enabled(cfg: dict, base: Path = DATA_DIR) -> bool:
    """Микроскоп включён? Галочка на главной (micro_enabled в конфиге) главнее; не нажимали (None) — включён только там,
    где стоит CV-сервис: нет его — нет и микроскопа, плату/ПЛК на 192.168.1.128 незачем долбить."""
    v = cfg.get("micro_enabled")
    return installed("cv", base) if v is None else bool(v)


def decide(name: str, cfg: dict, base: Path = DATA_DIR) -> tuple[str, str, Optional[dict]]:
    """Что делать с сервисом: ("off" | "absent" | "remote", почему, None) или ("start", адрес, как запускать).
    "start" значит «должен работать» — отвечает ли он уже, проверяет Supervisor."""
    sp = SPECS[name]
    d = Path(base) / sp["dir"]
    if not ((cfg.get("services") or {}).get(name) or {}).get("autostart", True):
        return "off", "автозапуск выключен (services.%s.autostart)" % name, None
    py = d / ".venv" / "Scripts" / "python.exe"
    if not installed(name, base):
        return "absent", "не установлен (нет %s\\.venv — один раз запусти %s\\setup.bat)" % (sp["dir"], sp["dir"]), None
    if not sp["enabled"](cfg):
        return "off", "выключен тумблером на вкладке «CV»", None
    url = sp["url"](cfg) or sp["default_url"]
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if host not in _LOCAL_HOSTS:
        return "remote", "адрес %s — не этот компьютер, запуск не наш" % url, None
    env = {sp["port_env"]: str(u.port or urlparse(sp["default_url"]).port), sp["host_env"]: "127.0.0.1" if host == "localhost" else host,
           "PYTHONUNBUFFERED": "1"}
    if name == "cv":                                   # как в cv_service\run.bat: есть model\best.pt — он, нет — classic (без GPU)
        env["CV_MODEL"] = "model\\best.pt" if (d / "model" / "best.pt").is_file() else "classic"
    return "start", url, {"python": str(py), "cwd": str(d), "env": env, "log": str(d / "service.log")}


def child_env(extra: dict) -> dict:
    """Окружение сервиса: окружение web_MVS без того, что собранный exe (PyInstaller) или чужой Python оставляют для себя —
    с ними python из venv сервиса может не найти свои библиотеки."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "PYTHONPATH") and not k.startswith(("_MEI", "_PYI"))}
    env.update(extra)
    return env


def spawn(launch: dict):
    """Запустить сервис отдельным процессом без окна, вывод — в service.log (старый > 5 МБ уходит в service.log.1)."""
    log = Path(launch["log"])
    try:
        if log.exists() and log.stat().st_size > LOG_MAX_BYTES:
            log.replace(log.with_name(log.name + ".1"))
    except OSError:
        pass
    out = open(log, "ab")
    out.write(("\n=== %s · запущен web_MVS ===\n" % time.strftime("%Y-%m-%d %H:%M:%S")).encode("utf-8"))
    out.flush()
    kw = dict(cwd=launch["cwd"], env=child_env(launch["env"]), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
              close_fds=True)
    try:
        if os.name != "nt":
            return subprocess.Popen([launch["python"], "server.py"], start_new_session=True, **kw)
        CREATE_NO_WINDOW, CREATE_NEW_PROCESS_GROUP, CREATE_BREAKAWAY_FROM_JOB = 0x08000000, 0x00000200, 0x01000000
        base = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        try:
            return subprocess.Popen([launch["python"], "server.py"], creationflags=base | CREATE_BREAKAWAY_FROM_JOB, **kw)
        except OSError:                                # job не разрешает breakaway (или job нет) — без него
            return subprocess.Popen([launch["python"], "server.py"], creationflags=base, **kw)
    finally:
        out.close()


class Supervisor:
    """Проверка всех сервисов (check_once) и сторож (loop). spawn / now / load_cfg подменяются в selfcheck."""

    def __init__(self, base: Path = DATA_DIR, spawn_fn: Callable = spawn, now: Callable[[], float] = time.time,
                 load_cfg: Callable[[], dict] = plate_config.load):
        self.base, self.spawn_fn, self.now, self.load_cfg = Path(base), spawn_fn, now, load_cfg
        self.procs: dict = {}                          # наши запущенные процессы (Popen)
        self.starts: dict = {n: [] for n in SPECS}     # когда запускали — для лимита перезапусков
        self.state: dict = {}                          # последнее состояние — в журнал только перемены

    def _note(self, name: str, state: str, msg: str, level: str = "info"):
        if self.state.get(name) != state:
            self.state[name] = state
            log_event("services", "%s: %s" % (SPECS[name]["title"], msg), level)

    def check_once(self) -> dict:
        cfg = self.load_cfg() or {}
        out = {}
        for name, sp in SPECS.items():
            act, why, launch = decide(name, cfg, self.base)
            if act != "start":
                self._note(name, act, why)
                out[name] = act
                continue
            if sp["health"](why):
                self._note(name, "healthy", "отвечает (%s)" % why)
                out[name] = "healthy"
                continue
            p = self.procs.get(name)
            if p is not None and p.poll() is None:     # наш процесс жив — ещё грузится (модель, импорт torch), ждём
                out[name] = "starting"
                continue
            now = self.now()
            recent = [t for t in self.starts[name] if now - t < RESTART_WIN_S]
            self.starts[name] = recent
            if len(recent) >= RESTART_MAX:
                self._note(name, "gave_up", "не поднимается — %d запуска за %d мин; смотри %s\\service.log" %
                           (RESTART_MAX, RESTART_WIN_S // 60, sp["dir"]), "error")
                out[name] = "gave_up"
                continue
            again = bool(recent)                       # до append: recent — тот же список, что self.starts[name]
            try:
                self.procs[name] = self.spawn_fn(launch)
            except Exception as e:
                self.starts[name].append(now)
                self._note(name, "spawn_error", "не запустился: %s" % e, "error")
                out[name] = "spawn_error"
                continue
            self.starts[name].append(now)
            self.state[name] = "started"
            log_event("services", "%s: запущен web_MVS (%s)%s" % (sp["title"], why, ", перезапуск" if again else ""),
                      "warn" if again else "info", {"python": launch["python"], "env": launch["env"]})
            out[name] = "started"
        return out

    def loop(self):
        while True:
            try:
                self.check_once()
            except Exception as e:                     # сторож не должен умирать от случайной ошибки
                log_event("services", "Сторож сервисов: ошибка проверки: %s" % e, "error")
            time.sleep(CHECK_EVERY_S)


_sup: Optional[Supervisor] = None


def states() -> dict:
    """Последнее известное состояние сервисов {имя: healthy | started | gave_up | spawn_error | off | absent | remote} — для мониторинга (sysmon)."""
    return dict(_sup.state) if _sup is not None else {}


def start():
    """Поднять установленные сервисы и запустить сторожа (фоном — старт web_MVS не ждёт). Повторный вызов ничего не делает."""
    global _sup
    if _sup is not None:
        return
    _sup = Supervisor()
    threading.Thread(target=_sup.loop, daemon=True, name="services").start()
