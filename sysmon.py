"""sysmon — «пульс» заводского ПК для удалённого мониторинга (панель на сервере Макса, monitor_server).

Зачем: журнал событий в памяти (logger.py) при перезапуске обнуляется, поэтому раньше ни перезагрузка ПК, ни сброс экспозиции камеры
никому не были видны — картинка целый день шла пересвеченной.

Что делает (всё фоном, на работу приложения не влияет):
  * при каждом запуске пишет в sysmon.db строку «старт» и определяет причину: перезагрузка ПК / перезапуск приложения / обновление
    версии / первый запуск; сколько ПК или приложение не работали (по «пульсу» на диске) и была ли остановка штатной;
  * раз в 15 с проверяет правила тревог (камера, пересвет, сервисы, плата, авария автомата, «варка идёт, а проб нет»);
    тревога открывается и снимается событием;
  * раз в interval_s (по умолчанию 5 мин) шлёт снимок на сервер: HTTPS POST <url>/ingest, токен завода в заголовке Authorization;
    срочное (старт, тревога «высокая/критичная») уходит сразу;
  * события копятся в sysmon.db (outbox) и доезжают после обрыва связи; у каждого свой eid, сервер по нему отсекает дубли.

Настройки — monitor.json в каталоге данных (адрес, токен, имя завода, интервал); правятся на странице «Мониторинг».
Отдельным файлом, а не в plate_config, чтобы токен не попадал в дампы и снимки конфига, а сброс конфига его не стирал.
В снимок и события идут только числа, статусы и короткий текст: ни кадров, ни паролей, ни путей.
"""
from __future__ import annotations

import ctypes
import ipaddress
import json
import os
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from logger import log_event, mask_secrets
from paths import DATA_DIR, read_version

DB_PATH = DATA_DIR / "sysmon.db"
CONFIG_PATH = DATA_DIR / "monitor.json"

SCHEMA = 1
DEFAULTS = {"enabled": False, "url": "", "token": "", "plant": "", "interval_s": 300, "system_proxy": True, "no_probe_min": 30}

BOOT_TOL_S = 120            # время загрузки Windows у двух запусков расходится больше — это была перезагрузка ПК
BEAT_EVERY_S = 30           # как часто пишем на диск «я жив» (по нему считаем, сколько не работали)
WATCH_EVERY_S = 15          # как часто проверяем правила тревог
TICK_S = 5                  # шаг фонового потока
STARTUP_DELAY_S = 20        # первый пульс не раньше, чем через столько после старта (пока поднимаются плата, камера, сервисы)
RETRY_S = (15, 30, 60, 120)  # пауза перед повтором после неудачной отправки (дальше — обычный интервал)
HTTP_TIMEOUT_S = 10
BATCH_MAX = 100             # событий в одном запросе
OUTBOX_MAX = 500            # дольше сервер не отвечает — самые старые события отбрасываем
RECENT_MAX = 50
URGENT = ("critical", "high")
# сколько проверок подряд правило должно срабатывать, чтобы тревога открылась (плата, например, молча переподключается)
CONFIRM = {"service_cv": 2, "service_video": 2, "service_motion": 2, "plate_down": 4, "camera_exposure": 2}

STAGE_NAMES = {1: "Остановлен", 2: "Набор", 3: "Сгущение", 4: "Затравка", 5: "Подкачка", 6: "Стабилизация", 7: "Рост", 8: "Уваривание",
               9: "Готовность", 10: "Выгрузка", 11: "Пропарка", 12: "Надвыгрузка", 13: "Собрать аппарат", 14: "Термоудар",
               20: "УНВ", 21: "Пауза"}
START_LEVEL = {"pc_reboot": "critical", "app_restart": "critical", "app_update": "info", "first": "info"}
PARAM_LABELS = {"exposure_time": "экспозиция", "gain": "усиление", "exposure_auto": "авто-экспозиция", "gain_auto": "авто-усиление",
                "pixel_format": "формат кадра", "width": "ширина кадра", "height": "высота кадра", "offset_x": "смещение X",
                "offset_y": "смещение Y"}
SERVICE_LABELS = {"cv": "CV-сервис", "video": "Видео-сервис", "motion": "Сервис движения"}


# ---------------------------------------------------------------------------------------------------------------- вспомогательное

def fmt_dur(sec) -> str:
    """Длительность для человека: «3 д 4 ч», «2 ч 15 мин», «7 мин», «45 с»."""
    s = int(max(0, sec or 0))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return "%d д %d ч" % (d, h)
    if h:
        return "%d ч %d мин" % (h, m)
    if m:
        return "%d мин" % m
    return "%d с" % s


def stage_name(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "—"
    if n == 0:
        return "нет данных ПЛК"
    return "%d %s" % (n, STAGE_NAMES[n]) if n in STAGE_NAMES else str(n)


def boot_time(now: Optional[float] = None) -> Optional[float]:
    """Когда загружена Windows (unix-время). GetTickCount64 — без psutil. None, если узнать не удалось."""
    now = time.time() if now is None else now
    try:
        if os.name == "nt":
            k32 = ctypes.windll.kernel32
            k32.GetTickCount64.restype = ctypes.c_ulonglong
            return now - k32.GetTickCount64() / 1000.0
        with open("/proc/uptime", encoding="ascii") as f:
            return now - float(f.read().split()[0])
    except Exception:
        return None


def _num(v, nd=None):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return round(v, nd) if nd is not None else v


def _safe(fn, default=None, errors: Optional[list] = None, name: str = ""):
    """Вызвать источник данных; любая ошибка — default (пульс не должен падать из-за одного датчика)."""
    try:
        return fn()
    except Exception as e:
        if errors is not None:
            errors.append("%s: %s" % (name, e))
        return default


# --------------------------------------------------------------------------------------------------------------------- настройки

def validate_url(url: str) -> str:
    """Адрес сервера: https://…; http:// только для этого ПК и локальных сетей (проверка у себя). Возвращает адрес без хвостового «/»."""
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("Адрес сервера должен начинаться с https://")
    if u.scheme == "http":
        host = u.hostname
        local = host == "localhost" or host.endswith(".localhost")
        if not local:
            try:
                ip = ipaddress.ip_address(host)
                local = ip.is_loopback or ip.is_private
            except ValueError:
                local = False
        if not local:
            raise ValueError("Токен нельзя слать по http:// через интернет — нужен https://")
    return url


def _clean_cfg(cfg: dict) -> dict:
    out = dict(DEFAULTS)
    out["enabled"] = bool(cfg.get("enabled", False))
    out["url"] = str(cfg.get("url") or "").strip().rstrip("/")
    out["token"] = str(cfg.get("token") or "").strip()
    out["plant"] = str(cfg.get("plant") or "").strip()[:40]
    out["system_proxy"] = bool(cfg.get("system_proxy", True))
    for k, lo, hi in (("interval_s", 60, 3600), ("no_probe_min", 5, 240)):
        try:
            out[k] = max(lo, min(hi, int(cfg.get(k, DEFAULTS[k]))))
        except (TypeError, ValueError):
            out[k] = DEFAULTS[k]
    return out


def load_config() -> dict:
    cfg = {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            cfg = data
    except FileNotFoundError:
        pass
    except Exception as e:
        log_event("sysmon", "Не удалось прочитать monitor.json — беру настройки по умолчанию", "warn", {"error": str(e)})
    return _clean_cfg(cfg)


def save_config(patch: dict) -> dict:
    """Слить правки в monitor.json. Пустой токен в правке = «не менять» (форма не показывает старый). Ошибка адреса — ValueError."""
    cur = load_config()
    new = dict(cur)
    for k in DEFAULTS:
        if k not in patch or patch[k] is None:
            continue
        if k == "token" and not str(patch[k]).strip():
            continue
        new[k] = patch[k]
    if "url" in patch and patch["url"] is not None:
        new["url"] = validate_url(str(patch["url"]))
    new = _clean_cfg(new)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    tmp.write_text(json.dumps(new, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)
    log_event("sysmon", "Настройки мониторинга сохранены (%s)" % ("включён" if new["enabled"] else "выключен"), "info",
              {"url": new["url"], "plant": new["plant"], "interval_s": new["interval_s"]})
    return new


def public_config(cfg: Optional[dict] = None) -> dict:
    """Настройки для страницы: сам токен не отдаём, только «задан ли» и хвост для узнавания."""
    cfg = dict(cfg or load_config())
    tok = cfg.pop("token", "")
    cfg["token_set"] = bool(tok)
    cfg["token_tail"] = tok[-4:] if len(tok) >= 12 else ""
    return cfg


def configured(cfg: dict) -> bool:
    return bool(cfg.get("enabled") and cfg.get("url") and cfg.get("token"))


# ------------------------------------------------------------------------------------------------------- хранилище (sysmon.db)

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None
_conn_path = None


def _db() -> sqlite3.Connection:
    global _conn, _conn_path
    if _conn is None or _conn_path != DB_PATH:
        close()
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("CREATE TABLE IF NOT EXISTS starts (id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL NOT NULL, boot REAL, "
                  "version TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL, clean INTEGER, gap_s REAL, text TEXT NOT NULL DEFAULT '')")
        c.execute("CREATE TABLE IF NOT EXISTS outbox (eid TEXT PRIMARY KEY, t REAL NOT NULL, kind TEXT NOT NULL, level TEXT NOT NULL, "
                  "text TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}')")
        c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        c.commit()
        _conn, _conn_path = c, DB_PATH
    return _conn


def close() -> None:
    global _conn, _conn_path
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        _conn, _conn_path = None, None


def _meta_get(key: str) -> Optional[str]:
    r = _db().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else None


def _meta_set(key: str, value) -> None:
    _db().execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))


def start_text(reason: str, gap_s: Optional[float], clean: Optional[bool], version: str) -> str:
    gap = " (не работало %s)" % fmt_dur(gap_s) if gap_s is not None and gap_s >= 60 else ""
    if reason == "pc_reboot":
        return "ПК перезагрузился" + gap
    if reason == "app_restart":
        return "Приложение перезапущено" + gap + ("" if clean else ", предыдущая остановка нештатная")
    if reason == "app_update":
        return "Приложение обновлено до v%s%s" % (version, gap)
    return "Мониторинг запущен впервые"


def register_start(now: float, boot: Optional[float], version: str) -> dict:
    """Записать запуск приложения и определить причину. pc_reboot — время загрузки Windows сменилось (> BOOT_TOL_S);
    app_update — Windows та же, версия другая; иначе app_restart. clean — прошлая остановка была штатной (lifespan дошёл до конца)."""
    with _lock:
        c = _db()
        prev = c.execute("SELECT * FROM starts ORDER BY id DESC LIMIT 1").fetchone()
        beat = _meta_get("beat")
        try:
            gap = now - float(beat) if beat else None
        except ValueError:
            gap = None
        if gap is not None and gap < 0:           # часы ушли назад — длительность не знаем
            gap = None
        clean = _meta_get("clean_stop") == "1"
        if prev is None:
            reason = "first"
        elif boot is not None and prev["boot"] is not None and abs(boot - prev["boot"]) > BOOT_TOL_S:
            reason = "pc_reboot"
        elif prev["version"] != version:
            reason = "app_update"
        else:
            reason = "app_restart"
        text = start_text(reason, gap, clean, version)
        c.execute("INSERT INTO starts (t, boot, version, reason, clean, gap_s, text) VALUES (?,?,?,?,?,?,?)",
                  (now, boot, version, reason, int(clean), gap, text))
        _meta_set("clean_stop", "0")
        _meta_set("beat", repr(now))
        c.commit()
    return {"t": now, "reason": reason, "level": START_LEVEL[reason], "text": text, "gap_s": gap, "clean": clean, "version": version}


def starts(limit: int = 30) -> list[dict]:
    with _lock:
        rows = _db().execute("SELECT * FROM starts ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [{"t": r["t"], "reason": r["reason"], "level": START_LEVEL.get(r["reason"], "info"), "text": r["text"],
             "gap_s": r["gap_s"], "clean": bool(r["clean"]), "version": r["version"]} for r in rows]


def mark_clean_stop() -> None:
    with _lock:
        _meta_set("clean_stop", "1")
        _db().commit()


def beat(now: float) -> None:
    with _lock:
        _meta_set("beat", repr(now))
        _db().commit()


def outbox_add(kind: str, level: str, text: str, data: Optional[dict] = None, t: Optional[float] = None) -> dict:
    ev = {"eid": uuid.uuid4().hex, "t": time.time() if t is None else t, "kind": kind, "level": level,
          "text": mask_secrets(text), "data": mask_secrets(data or {})}
    with _lock:
        c = _db()
        c.execute("INSERT INTO outbox (eid, t, kind, level, text, data) VALUES (?,?,?,?,?,?)",
                  (ev["eid"], ev["t"], kind, level, ev["text"], json.dumps(ev["data"], ensure_ascii=False)))
        c.execute("DELETE FROM outbox WHERE rowid <= (SELECT MAX(rowid) FROM outbox) - ?", (OUTBOX_MAX,))
        c.commit()
    return ev


def outbox_batch(limit: int = BATCH_MAX) -> list[dict]:
    with _lock:
        rows = _db().execute("SELECT * FROM outbox ORDER BY rowid LIMIT ?", (int(limit),)).fetchall()
    return [{"eid": r["eid"], "t": r["t"], "kind": r["kind"], "level": r["level"], "text": r["text"], "data": json.loads(r["data"] or "{}")}
            for r in rows]


def outbox_done(eids: list[str]) -> None:
    if not eids:
        return
    with _lock:
        c = _db()
        c.executemany("DELETE FROM outbox WHERE eid=?", [(e,) for e in eids])
        c.commit()


def outbox_count() -> int:
    with _lock:
        return _db().execute("SELECT COUNT(*) n FROM outbox").fetchone()["n"]


# ------------------------------------------------------------------------------------------------------------ источники данных

def default_sources() -> dict:
    """Откуда берём данные. Всё — функции без аргументов, только чтение; тесты подменяют их целиком."""
    import camera_params
    import db
    import plate_config
    import services
    from camera_core import manager
    from microscope_service import micro

    def camera():
        serial = (plate_config.load().get("camera_serial") or "").strip()
        if not serial:
            return {"serial": ""}
        w = manager.workers.get(serial)               # не создаём воркер ради мониторинга
        cur = getattr(w, "current_config", None) if w is not None else None
        return {"serial": serial, "streaming": bool(w is not None and w.running), "current": dict(cur) if cur else None,
                "saved": camera_params.saved(serial)}

    def probes(serial, since):
        c = db.conn()
        n = c.execute("SELECT COUNT(*) n FROM journal WHERE serial=? AND t>=?", (serial, since)).fetchone()["n"]
        last = c.execute("SELECT MAX(t) last FROM journal WHERE serial=?", (serial,)).fetchone()["last"]
        return {"last_t": last, "today": n}

    return {
        "cfg": plate_config.load,
        "micro_on": lambda: services.micro_enabled(plate_config.load()),
        "state": micro.state, "status": micro.status, "ext": micro.ext, "cv": micro.cv_status,
        "services": services.states,
        "camera": camera,
        "probes": probes,
    }


# --------------------------------------------------------------------------------------------------------------------- агент

class Agent:
    """Фоновый агент: старт → проверка правил → отправка. sources / clock подменяются в тестах."""

    def __init__(self, sources: Optional[dict] = None, clock: Callable[[], float] = time.time):
        self.src = sources
        self.clock = clock
        self.started_at = clock()
        self.boot = boot_time(self.started_at)
        self.start_info: Optional[dict] = None
        self.active: dict = {}              # code -> {level, text, since}
        self._seen: dict = {}               # code -> сколько проверок подряд правило срабатывало
        self._ready_since: Optional[float] = None
        self._plate_up: Optional[float] = None
        self.recent: deque = deque(maxlen=RECENT_MAX)
        self.last_data: Optional[dict] = None
        self.status = {"ok": None, "last_ok_t": None, "last_try_t": None, "error": "", "http": None, "fails": 0}
        self._next_send = 0.0
        self._stop = threading.Event()
        self._kick = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._send_lock = threading.Lock()
        self._cfg = load_config()

    # ---- жизненный цикл
    def start(self) -> None:
        if self._thread is not None:
            return
        if self.src is None:
            self.src = default_sources()
        self.start_info = register_start(self.started_at, self.boot, read_version())
        si = self.start_info
        self._emit("start", si["level"], si["text"], {"reason": si["reason"], "gap_s": si["gap_s"], "clean": si["clean"]}, t=self.started_at)
        log_event("sysmon", "Старт: " + si["text"], "warn" if si["level"] in URGENT else "info", {"reason": si["reason"]})
        self._thread = threading.Thread(target=self._loop, name="sysmon", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._kick.set()
        t = self._thread
        if t is not None:
            t.join(timeout=3)
        mark_clean_stop()

    def _loop(self) -> None:
        next_watch = 0.0
        last_beat = 0.0
        while not self._stop.is_set():
            now = self.clock()
            try:
                if now - last_beat >= BEAT_EVERY_S:
                    beat(now)
                    last_beat = now
                if now >= next_watch:
                    self._cfg = load_config()
                    self._sync_schedule()
                    self.tick(now)
                    next_watch = now + WATCH_EVERY_S
                if self._send_due(now):
                    self.send(now=now)
            except Exception as e:                  # агент не должен умирать от случайной ошибки
                log_event("sysmon", "Мониторинг: ошибка цикла: %s" % e, "error")
            self._stop.wait(TICK_S)

    def _sync_schedule(self) -> None:
        """Интервал укоротили в настройках — следующий пульс не ждёт старого срока (после удачной отправки)."""
        last = self.status.get("last_try_t")
        if last and self.status.get("ok") and self._next_send > last + self._cfg["interval_s"]:
            self._next_send = last + self._cfg["interval_s"]

    def _send_due(self, now: float) -> bool:
        if not configured(self._cfg) or now < self.started_at + STARTUP_DELAY_S:
            return False
        return self._kick.is_set() or now >= self._next_send

    # ---- события
    def _emit(self, kind: str, level: str, text: str, data: Optional[dict] = None, t: Optional[float] = None) -> dict:
        t = self.clock() if t is None else t
        ev = {"t": t, "kind": kind, "level": level, "text": text, "data": data or {}}
        self.recent.appendleft(ev)
        if configured(self._cfg):
            outbox_add(kind, level, text, data, t)
            if level in URGENT:
                self._kick.set()
        return ev

    # ---- сбор и правила
    def collect(self) -> dict:
        """Сырые показания. Не бросает: недоступный источник даёт None, причина — в errors."""
        now = self.clock()
        errs: list = []
        g = lambda name, default=None: _safe(self.src[name], default, errs, name) if name in self.src else default
        cfg = g("cfg", {}) or {}
        d = {"t": now, "errors": errs, "micro_on": bool(g("micro_on", False)), "cv_enabled": bool((cfg.get("cv") or {}).get("enabled")),
             "serial": (cfg.get("camera_serial") or "").strip()}
        if d["micro_on"]:
            d["state"] = g("state", {}) or {}
            d["status"] = g("status", {}) or {}
            d["ext"] = g("ext", {}) or {}
            d["cv"] = g("cv", {}) or {}
            if d["serial"] and "probes" in self.src:
                def probes():
                    lt = time.localtime(now)                  # локальная полночь без datetime: на Windows ему не нравятся даты около 1970
                    return self.src["probes"](d["serial"], now - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec))
                d["probes"] = _safe(probes, None, errs, "probes")
        d["camera"] = g("camera", {}) or {}
        d["services"] = g("services", {}) or {}
        return d

    def _rules(self, d: dict, now: float) -> tuple[dict, set]:
        """Какие тревоги сейчас горят: ({код: (уровень, текст)}, коды, про которые сейчас ничего не известно — их не трогаем)."""
        found: dict = {}
        keep: set = set()
        micro = d.get("micro_on")
        state, cv = d.get("state") or {}, d.get("cv") or {}
        stage_ok = bool(state.get("stage_ok"))

        cam = d.get("camera") or {}
        if cam.get("serial"):                        # камера микроскопа: стоит ли в ней то, что сохранили кнопкой «Применить»
            if cam.get("streaming") and cam.get("current") and cam.get("saved"):
                import camera_params
                bad = camera_params.mismatches(cam["saved"], cam["current"])
                if bad:
                    parts = ["%s %s вместо %s" % (PARAM_LABELS.get(k, k), _fmt_val(a), _fmt_val(e)) for k, (e, a) in list(bad.items())[:3]]
                    found["camera_exposure"] = ("high", "Камера: " + ", ".join(parts))
            else:
                keep.add("camera_exposure")           # камера не открыта — не знаем, остаётся как было

        for name, st in (d.get("services") or {}).items():
            if st in ("gave_up", "spawn_error") and name in SERVICE_LABELS:
                found["service_" + name] = ("medium", "%s не поднимается" % SERVICE_LABELS[name])

        if micro:
            if cv.get("state") == "overexposed" and d.get("cv_enabled") and stage_ok:
                found["overexposed"] = ("high", "Кадры пересвечены, пробы пропускаются")
            elif cv.get("state") in ("no_camera", "no_frames", "sidecar_offline") and d.get("cv_enabled") and stage_ok:
                found["cv_fail"] = ("high", "Проба не разобрана: " + str(cv.get("message") or cv.get("state"))[:120])

            conn = (d.get("status") or {}).get("connected")
            if conn is False:
                found["plate_down"] = ("medium", "Плата микроскопа не отвечает")
            elif conn is None:
                keep.add("plate_down")

            if state.get("fault"):
                found["fault"] = ("high", "Авария автомата: " + str(state.get("fault_msg") or "")[:120])

            cp = state.get("cycle_params") or {}
            in_range = (cp.get("trigger_mode") == "time" or state.get("stage") == 5
                        or (cp.get("sv_from") is not None and cp["sv_from"] <= (state.get("sv") or 0) <= cp.get("sv_to", 1e9)))
            ready = bool(stage_ok and d.get("cv_enabled") and d.get("serial") and state.get("cyclic") and not state.get("manual")
                         and not state.get("fault") and in_range)
            if not ready:
                self._ready_since = None
            else:
                self._ready_since = self._ready_since or now
                pr = d.get("probes")
                if pr is None:
                    keep.add("no_probes")
                else:
                    limit = self._cfg["no_probe_min"] * 60
                    ref = max(self._ready_since, pr.get("last_t") or 0)
                    if now - ref >= limit:
                        found["no_probes"] = ("medium", "Варка идёт, а проб нет уже %s" % fmt_dur(now - ref))
        return found, keep

    def evaluate(self, d: dict, now: float) -> list[dict]:
        """Применить правила: открыть/снять тревоги, отметить перезагрузку платы. Возвращает новые события."""
        events: list = []
        found, keep = self._rules(d, now)
        for code in found:                               # подтверждение: открыть тревогу можно, когда правило сработало CONFIRM проверок подряд
            self._seen[code] = self._seen.get(code, 0) + 1
        for code in [c for c in self._seen if c not in found and c not in keep]:
            self._seen.pop(code, None)
        for code, (level, text) in found.items():
            if code not in self.active:
                if self._seen[code] < CONFIRM.get(code, 1):
                    continue
                self.active[code] = {"level": level, "text": text, "since": now}
                events.append(self._emit("alert", level, text, {"code": code, "state": "open"}, t=now))
            else:
                self.active[code]["text"] = text
        for code in [c for c in self.active if c not in found and c not in keep]:
            old = self.active.pop(code)
            events.append(self._emit("alert", "info", "Снято: " + old["text"], {"code": code, "state": "closed"}, t=now))
        up = _num((d.get("ext") or {}).get("uptime_s"))
        if up is not None:
            if self._plate_up is not None and up + 10 < self._plate_up:
                events.append(self._emit("plate_reboot", "high", "Плата микроскопа перезагрузилась (её аптайм сбросился)", {}, t=now))
            self._plate_up = up
        return events

    def tick(self, now: Optional[float] = None) -> list[dict]:
        now = self.clock() if now is None else now
        d = self.collect()
        self.last_data = d
        return self.evaluate(d, now)

    # ---- снимок
    def snapshot(self, d: Optional[dict] = None, cfg: Optional[dict] = None) -> dict:
        d = d or self.collect()
        cfg = cfg or self._cfg
        now = d["t"]
        app_up = now - self.started_at
        snap = {
            "schema": SCHEMA, "plant": cfg.get("plant") or socket.gethostname(), "host": socket.gethostname(),
            "app_version": read_version(), "t": now, "tz_off_s": time.localtime(now).tm_gmtoff or 0,
            "interval_s": cfg.get("interval_s"), "boot_t": self.boot, "pc_up_s": (now - self.boot) if self.boot else None,
            "app_start_t": self.started_at, "app_up_s": app_up,
            "start": self.start_info,
            "alerts": [{"code": c, **a} for c, a in list(self.active.items())],
            "camera": _camera_block(d.get("camera") or {}),
            "services": {k: _svc_state(v) for k, v in (d.get("services") or {}).items()},
            "errors": d.get("errors") or [],
        }
        if d.get("micro_on"):
            snap["micro"] = _micro_block(d, now)
        snap["text"] = summary_text(snap)
        return mask_secrets(snap)

    def state(self) -> dict:
        """Всё для страницы «Мониторинг»."""
        cfg = load_config()
        d = self.collect()
        snap = self.snapshot(d, cfg)
        return {"snapshot": snap, "alerts": snap["alerts"], "send": {**self.status, "pending": outbox_count(),
                "configured": configured(cfg)}, "config": public_config(cfg), "recent": list(self.recent), "starts": starts(15)}

    # ---- отправка
    def send(self, now: Optional[float] = None, force: bool = False) -> dict:
        """Одна отправка: снимок + накопленные события. Успех — HTTP 2xx; неуспех — события остаются, повтор позже.
        force — «Проверить связь»: слать, даже если галочка «Отправлять» ещё не включена (адрес и токен нужны всё равно)."""
        with self._send_lock:
            now = self.clock() if now is None else now
            cfg = load_config()
            self._cfg = cfg
            if not (cfg["url"] and cfg["token"]):
                return {"ok": False, "error": "Не заданы адрес сервера и токен"}
            if not cfg["enabled"] and not force:
                return {"ok": False, "error": "Отправка выключена"}
            events = outbox_batch()
            body = {"schema": SCHEMA, "sent_t": now, "snapshot": self.snapshot(None, cfg), "events": events}
            ok, code, err = _post(cfg["url"] + "/ingest", cfg["token"], body, cfg["system_proxy"])
            st = self.status
            st.update({"last_try_t": now, "http": code})
            if ok:
                outbox_done([e["eid"] for e in events])
                st.update({"ok": True, "last_ok_t": now, "error": "", "fails": 0})
                self._next_send = now + cfg["interval_s"]
                self._kick.clear()
                if len(events) >= BATCH_MAX:             # накопилось больше пачки — остальное сразу следом
                    self._kick.set()
            else:
                st.update({"ok": False, "error": err, "fails": st["fails"] + 1})
                if st["fails"] == 1 or st["fails"] % 20 == 0:
                    log_event("sysmon", "Мониторинг: не удалось отправить пульс — " + err, "warn", {"fails": st["fails"]})
                self._next_send = now + min(cfg["interval_s"], RETRY_S[min(st["fails"] - 1, len(RETRY_S) - 1)])
                self._kick.clear()
            return {"ok": ok, "http": code, "error": err, "sent_events": len(events) if ok else 0}


def _fmt_val(v) -> str:
    return ("%g" % v) if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)


def _svc_state(s) -> str:
    return {"healthy": "ok", "started": "starting", "starting": "starting", "gave_up": "down", "spawn_error": "down"}.get(s, str(s))


def _camera_block(cam: dict) -> dict:
    if not cam.get("serial"):
        return {"set": False}
    cur, saved = cam.get("current") or {}, cam.get("saved") or {}
    out = {"set": True, "streaming": bool(cam.get("streaming"))}
    for k in ("exposure_time", "gain"):
        if cur.get(k) is not None:
            out[k] = _num(cur.get(k), 2)
        if saved.get(k) is not None:
            out[k + "_saved"] = _num(saved.get(k), 2)
    return out


def _micro_block(d: dict, now: float) -> dict:
    state, status, ext, cv = d.get("state") or {}, d.get("status") or {}, d.get("ext") or {}, d.get("cv") or {}
    pr = d.get("probes") or {}
    u12 = _num(ext.get("u12v"))
    return {
        "plate_connected": status.get("connected"),
        "stage": state.get("stage"), "stage_name": stage_name(state.get("stage")), "stage_ok": state.get("stage_ok"),
        "step": state.get("step"), "manual": state.get("manual"), "auto": state.get("cyclic"), "fault": state.get("fault"),
        "cv_state": cv.get("state"), "cv_enabled": d.get("cv_enabled"),
        "last_probe_t": pr.get("last_t"), "probes_today": pr.get("today"),
        "plate_up_s": _num(ext.get("uptime_s")), "plate_temp": _num(ext.get("temp")),
        "plate_12v": round(u12 / 1000.0, 1) if u12 is not None else None,
    }


def summary_text(s: dict) -> str:
    """Короткий текст «как дела» для человека (панель, Telegram)."""
    lines = []
    head = "%s · ПК работает %s" % (s["plant"], fmt_dur(s["pc_up_s"]) if s.get("pc_up_s") is not None else "—")
    head += " · приложение %s (v%s)" % (fmt_dur(s["app_up_s"]), s["app_version"])
    lines.append(head)
    m = s.get("micro")
    if m:
        if m.get("plate_connected") is False:
            lines.append("Микроскоп: плата не отвечает")
        else:
            part = "Варка: стадия %s" % m.get("stage_name", "—")
            if m.get("fault"):
                part += " · АВАРИЯ"
            elif m.get("manual"):
                part += " · ручной режим"
            elif m.get("step"):
                part += " · %s" % m["step"]
            if m.get("last_probe_t"):
                part += " · последняя проба %s назад" % fmt_dur(s["t"] - m["last_probe_t"])
            if m.get("probes_today") is not None:
                part += " · проб сегодня %d" % m["probes_today"]
            lines.append(part)
    cam = s.get("camera") or {}
    if cam.get("set"):
        if not cam.get("streaming"):
            lines.append("Камера: не стримит")
        else:
            txt = "Камера: стримит"
            if cam.get("exposure_time") is not None:
                txt += ", экспозиция %g" % cam["exposure_time"]
                if cam.get("exposure_time_saved") is not None:
                    txt += " (сохранено %g)" % cam["exposure_time_saved"]
            lines.append(txt)
    svc = s.get("services") or {}
    shown = [("%s %s" % (SERVICE_LABELS.get(k, k), {"ok": "ок", "starting": "запускается", "down": "не работает"}.get(v, v)))
             for k, v in svc.items() if v in ("ok", "starting", "down")]
    if shown:
        lines.append("Сервисы: " + ", ".join(shown))
    al = s.get("alerts") or []
    lines.append("Тревоги: " + ("; ".join(a["text"] for a in al) if al else "нет"))
    return "\n".join(lines)


def _post(url: str, token: str, body: dict, system_proxy: bool = True, timeout: float = HTTP_TIMEOUT_S) -> tuple[bool, Optional[int], str]:
    """POST JSON. (успех, HTTP-код, текст ошибки по-русски). Успех — любой 2xx."""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json; charset=utf-8", "Authorization": "Bearer " + token,
        "User-Agent": "web_MVS-sysmon/" + read_version()})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(None if system_proxy else {}))
    try:
        with opener.open(req, timeout=timeout) as r:
            r.read(65536)
            return True, r.status, ""
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, e.code, "Сервер не принял токен (HTTP %d)" % e.code
        return False, e.code, "Сервер ответил ошибкой HTTP %d" % e.code
    except urllib.error.URLError as e:
        return False, None, mask_secrets("Нет связи с сервером: %s" % (e.reason,))
    except Exception as e:
        return False, None, mask_secrets("Ошибка отправки: %s" % e)


# ------------------------------------------------------------------------------------------------------------------- синглтон

_agent: Optional[Agent] = None


def start() -> Agent:
    """Запустить агента (один на процесс; повторный вызов ничего не делает). Вызывается из lifespan приложения."""
    global _agent
    if _agent is None:
        _agent = Agent()
        _agent.start()
    return _agent


def stop() -> None:
    if _agent is not None:
        _agent.stop()


def agent() -> Optional[Agent]:
    return _agent
