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
import re
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import enroll_client
from logger import log_event, mask_secrets
from paths import DATA_DIR, read_version

DB_PATH = DATA_DIR / "sysmon.db"
CONFIG_PATH = DATA_DIR / "monitor.json"

SCHEMA = 1
DEFAULTS = {"enabled": False, "url": "", "token": "", "plant": "", "interval_s": 300, "system_proxy": True, "no_probe_min": 30, "auto_off_min": 10, "allow_http": False,
            "auto_enroll": True,       # нет токена — самому просить его у приёмника (enroll_client); нужен ключ в сборке
            # датчики микроскопа (правятся в monitor.json): допуски расхождения трёх датчиков, мкм — на отводе и на подводе; «ноль уполз» — |датчик − энкодер|
            # до автокалибровки, мкм. Везде 200 по решению Макса 04.11 (до этого отвод 150, подвод 200, ноль 100: сообщения с разными допусками путали);
            # автокалибровки нет дольше overdue_h часов работы; bad_streak плохих проб подряд — жёлтое предупреждение становится красной тревогой
            "gap_far_um": 200, "gap_near_um": 200, "drift_um": 200, "overdue_h": 24, "bad_streak": 2}
TUNING = ("gap_far_um", "gap_near_um", "drift_um", "overdue_h", "bad_streak")      # пороги: в файл пишем только то, что изменили руками
OLD_TUNING_DEFAULTS = {"gap_far_um": 150, "gap_near_um": 200, "drift_um": 100}    # прежние умолчания: такое значение в файле — не выбор человека

BOOT_TOL_S = 120            # время загрузки Windows у двух запусков расходится больше — это была перезагрузка ПК
BEAT_EVERY_S = 30           # как часто пишем на диск «я жив» (по нему считаем, сколько не работали)
STOP_SEND_TIMEOUT_S = 3     # сколько ждём приёмник при закрытии приложения
UPDATE_RESTART_S = 600      # закрытие в пределах этого времени после запуска апдейтера — «для обновления»
MOTION_CAMS_MAX = 24        # сколько камер движения кладём в пульс
WATCH_EVERY_S = 15          # как часто проверяем правила тревог
TICK_S = 5                  # шаг фонового потока
STARTUP_DELAY_S = 20        # первый пульс не раньше, чем через столько после старта (пока поднимаются плата, камера, сервисы)
RETRY_S = (15, 30, 60, 120)  # пауза перед повтором после неудачной отправки (дальше — обычный интервал)
HTTP_TIMEOUT_S = 10
BATCH_MAX = 100             # событий в одном запросе
OUTBOX_MAX = 500            # дольше сервер не отвечает — самые старые события отбрасываем
RECENT_MAX = 50
POS_KEEP = 200              # записей положений датчиков в outbox: поток проб не должен вытеснить важные события при долгом обрыве
POS_MEM = 20                # последних записей положений держим в памяти для правил
POS_STALE_S = 6 * 3600      # плохая проба старше — уже не повод держать тревогу
STEP_KEYS = {"отвод": "retract", "подвод": "approach"}
PUSH_CODES = ("pos_", "autocal_drift")     # жёлтые (не срочные) предупреждения, которые всё равно уходят сразу
URGENT = ("critical", "high")
# сколько проверок подряд правило должно срабатывать, чтобы тревога открылась (плата, например, молча переподключается)
CONFIRM = {"service_cv": 2, "service_video": 2, "service_motion": 2, "plate_down": 4, "camera_exposure": 2, "cv_fail": 2, "black": 2}
NEAR_MEDIAN_N = 3           # подвод: расхождение датчиков судим по медиане стольких последних проб, а не по одной (у Kir бывают одиночные пики)

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


PLATE_UP_WRAP = 65536      # регистр аптайма платы (1523) — uint16 в секундах; обнулений считает регистр 1524 (uptime_laps)


def plate_uptime(ext: dict) -> tuple[Optional[float], bool]:
    """Аптайм платы, с: (значение, точно ли). Точно — когда прочитан счётчик кругов (laps*65536 + секунды);
    иначе голый регистр секунд, он обнуляется каждые 18 ч 12 мин сам, без перезагрузки."""
    secs = _num((ext or {}).get("uptime_s"))
    if secs is None:
        return None, False
    laps = _num((ext or {}).get("uptime_laps"))
    if laps is None:
        return secs, False
    return laps * PLATE_UP_WRAP + secs, True


def _iso_ts(v) -> Optional[float]:
    """Время из ISO-строки без пояса (так пишет автокалибровка: локальное время ПК) -> unix-время; None, если не разобрать."""
    try:
        return datetime.fromisoformat(str(v)).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def position_verdict(rec: dict, cfg: dict) -> dict:
    """Оценка положений на отводе/подводе: доехал ли и сходятся ли датчики.
    gap — худшее расхождение аналогового датчика с энкодером и с расчётной (мкм), как `_far_gap` в автокалибровке;
    нет аналогового — сравниваем расчётную с энкодером."""
    enc, calc, ai = _num(rec.get("enc")), _num(rec.get("calc")), _num(rec.get("ai"))
    pairs = [(ai, enc), (ai, calc)] if ai is not None else [(calc, enc)]
    gaps = [abs(a - b) for a, b in pairs if a is not None and b is not None]
    gap = round(max(gaps)) if gaps else None
    tol = cfg["gap_far_um"] if rec.get("step") == "отвод" else cfg["gap_near_um"]
    why = ""
    if rec.get("timeout"):
        why = "не доехал за отведённое время"
    elif gap is not None and gap > tol:
        why = "датчики разошлись на %d мкм (допуск %d)" % (gap, tol)
    return {"gap": gap, "ok": not why, "why": why}


def position_text(rec: dict) -> str:
    """Запись положений одной строкой для человека."""
    parts = [("цель", rec.get("target")), ("расчётная", rec.get("calc")), ("энкодер", rec.get("enc")), ("датчик", rec.get("ai"))]
    txt = "%s: %s мкм" % (str(rec.get("step") or "?").capitalize(), " · ".join("%s %s" % (k, _fmt_val(v)) for k, v in parts if v is not None))
    return txt + ("" if rec.get("ok") else " — " + str(rec.get("why") or "не в норме"))


def _safe(fn, default=None, errors: Optional[list] = None, name: str = ""):
    """Вызвать источник данных; любая ошибка — default (пульс не должен падать из-за одного датчика)."""
    try:
        return fn()
    except Exception as e:
        if errors is not None:
            errors.append("%s: %s" % (name, e))
        return default


# --------------------------------------------------------------------------------------------------------------------- настройки

def closed_network(url: str, allow_http: bool = False) -> bool:
    """Адрес в закрытой сети (ZeroTier, VPN, локалка): туда идём напрямую, мимо системного прокси. Прокси, который раздаёт соседний сервер,
    до таких адресов не дотянется, а HTTPS-домен в интернете без него как раз не откроется — поэтому решаем по адресу."""
    u = urlparse((url or "").strip())
    host = u.hostname or ""
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback or ip.is_private or ip.is_link_local:
            return True
    except ValueError:
        pass
    return u.scheme == "http" and bool(allow_http)


def validate_url(url: str, allow_http: bool = False) -> str:
    """Адрес сервера: https://…; http:// только для этого ПК и локальных сетей (проверка у себя) или если явно разрешено галочкой
    allow_http — для закрытых сетей вроде ZeroTier, где трафик и так зашифрован (адрес там бывает «белым», но чужие в сеть не попадают).
    Возвращает адрес без хвостового «/»."""
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
        if not local and not allow_http:
            raise ValueError("Токен нельзя слать по http:// через интернет — нужен https:// (для ZeroTier или VPN включи «http в закрытой сети»)")
    return url


TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")


def token_problem(token: str) -> str:
    """Почему строка не похожа на токен завода ('' — похожа). Токен выдаёт панель: 32 символа из букв, цифр, «-» и «_».
    Ловим типичные промахи при копировании: вместо токена вставился адрес, скопировался только кусок (двойной щелчок режет по «-»)."""
    t = token or ""
    if TOKEN_RE.match(t):
        return ""
    if any(c in t for c in ":/."):
        what = "это похоже на адрес, а не на токен"
    elif len(t) < 20:
        what = "слишком короткий (%d симв., настоящий — 32)" % len(t)
    else:
        what = "в нём лишние символы (бывают только буквы, цифры, «-» и «_»)"
    return "Токен неверный: %s. Скопируй его заново из окна «Токен завода» в панели: клик в поле, Ctrl+A, Ctrl+C." % what


def _clean_cfg(cfg: dict) -> dict:
    out = dict(DEFAULTS)
    out["enabled"] = bool(cfg.get("enabled", False))
    out["url"] = str(cfg.get("url") or "").strip().rstrip("/")
    out["token"] = str(cfg.get("token") or "").strip()
    out["plant"] = str(cfg.get("plant") or "").strip()[:40]
    out["system_proxy"] = bool(cfg.get("system_proxy", True))
    out["allow_http"] = bool(cfg.get("allow_http", False))
    out["auto_enroll"] = bool(cfg.get("auto_enroll", True))
    for k, lo, hi in (("interval_s", 60, 3600), ("no_probe_min", 5, 240), ("auto_off_min", 2, 240), ("gap_far_um", 10, 2000), ("gap_near_um", 10, 2000),
                      ("drift_um", 10, 5000), ("overdue_h", 1, 168), ("bad_streak", 1, 10)):
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
            # 2.0.42–2.0.43 писали допуски в файл при каждом «Сохранить»: пара 100/50 — прежние значения по умолчанию, не выбор человека
            if cfg.get("gap_far_um") == 100 and cfg.get("gap_near_um") == 50:
                cfg = {k: v for k, v in cfg.items() if k not in ("gap_far_um", "gap_near_um")}
            # значение, равное прежнему умолчанию, — тоже след старых версий, а не выбор человека: пусть работает новое умолчание
            cfg = {k: v for k, v in cfg.items() if not (k in OLD_TUNING_DEFAULTS and v == OLD_TUNING_DEFAULTS[k] != DEFAULTS[k])}
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
        if k == "token":
            if not str(patch[k]).strip():
                continue
            problem = token_problem(str(patch[k]).strip())
            if problem:
                raise ValueError(problem)
        new[k] = patch[k]
    new = _clean_cfg(new)
    new["url"] = validate_url(new["url"], new["allow_http"])        # и при смене только галочки: без неё прежний http-адрес уже не годится
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    out = {k: v for k, v in new.items() if k not in TUNING or v != DEFAULTS[k]}       # пороги по умолчанию в файл не пишем: смена умолчаний дойдёт до заводов
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)
    log_event("sysmon", "Настройки мониторинга сохранены (%s)" % ("включён" if new["enabled"] else "выключен"), "info",
              {"url": new["url"], "plant": new["plant"], "interval_s": new["interval_s"]})
    return new


def public_config(cfg: Optional[dict] = None) -> dict:
    """Настройки для страницы: сам токен не отдаём, только «задан ли» и хвост для узнавания."""
    cfg = dict(cfg or load_config())
    tok = cfg.pop("token", "")
    cfg["token_set"] = bool(tok)
    cfg["token_len"] = len(tok)
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
        if kind == "positions":
            c.execute("DELETE FROM outbox WHERE kind='positions' AND rowid <= (SELECT MAX(rowid) FROM outbox WHERE kind='positions') - ?", (POS_KEEP,))
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
        bw = getattr(w, "black", None) if w is not None else None
        return {"serial": serial, "streaming": bool(w is not None and w.running), "current": dict(cur) if cur else None,
                "saved": camera_params.saved(serial), "black_s": round(bw.seconds(), 1) if bw is not None else 0.0}

    def probes(serial, since):
        c = db.conn()
        n = c.execute("SELECT COUNT(*) n FROM journal WHERE serial=? AND t>=?", (serial, since)).fetchone()["n"]
        last = c.execute("SELECT MAX(t) last FROM journal WHERE serial=?", (serial,)).fetchone()["last"]
        return {"last_t": last, "today": n}

    def motion():
        """Камеры сервиса движения. Сервис не запущен — None: блока в панели не будет."""
        import motion_client
        cfg = plate_config.load()
        if services.states().get("motion") not in ("healthy", "remote"):
            return None
        st = motion_client.state((cfg.get("motion") or {}).get("service_url") or motion_client.DEFAULT_URL, timeout=2.0)
        cams = st.get("cameras") if isinstance(st, dict) else None
        if not cams:
            return None
        return {"cameras": cams}

    def disk():
        """Свободное место на диске приложения: там лежат база, кадры, видео и записи — у всех заводов одинаково."""
        import shutil

        import paths
        du = shutil.disk_usage(str(paths.DATA_DIR))
        return {"free": du.free, "total": du.total}

    return {
        "cfg": plate_config.load,
        "micro_on": lambda: services.micro_enabled(plate_config.load()),
        "state": micro.state, "status": micro.status, "ext": micro.ext, "cv": micro.cv_status, "guard": micro.guard_status,
        "services": services.states,
        "camera": camera,
        "probes": probes,
        "motion": motion,
        "disk": disk,
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
        self._auto_off_since: Optional[float] = None      # с какого момента варка идёт, а «Автомат» выключен / стоит «Ручной»
        self._work_since: Optional[float] = None      # с какого момента автомат в работе (для «автокалибровки давно не было»)
        self._plate_up: Optional[float] = None
        self._plate_up_exact = False                   # _plate_up посчитан с кругами (иначе — голый 16-битный регистр)
        self.positions: deque = deque(maxlen=POS_MEM)  # положения датчиков на отводе/подводе (с оценкой), старые — слева
        self._ac_seen_ts: Optional[float] = None       # какую по счёту автокалибровку уже учли в истории дрейфа нуля
        self._ac_drift: deque = deque(maxlen=5)        # (ноль уползал больше допуска?, на сколько мкм) по последним калибровкам
        self.recent: deque = deque(maxlen=RECENT_MAX)
        self.last_data: Optional[dict] = None
        self.status = {"ok": None, "last_ok_t": None, "last_try_t": None, "error": "", "http": None, "fails": 0}
        self._next_send = 0.0
        self._stop = threading.Event()
        self._kick = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._send_lock = threading.Lock()
        self._cfg = load_config()
        self.enroller = enroll_client.Enroller()
        self._enroll_lock = threading.Lock()           # заявку шлют цикл агента и кнопка на странице — не одновременно

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
            self._report_stop()
        mark_clean_stop()

    def _report_stop(self) -> None:
        """Приложение закрывают: пишем событие и пробуем сразу отправить (коротко, выход не задерживаем). Закрыли руками — это красное,
        закрыли ради обновления — просто запись в историю. Не ушло — событие лежит в очереди и уйдёт со следующим стартом."""
        try:
            now = self.clock()
            upd = _update_restart_t is not None and now - _update_restart_t < UPDATE_RESTART_S
            self._emit("app_stop", "info" if upd else "critical", "Приложение закрыто" + (" для обновления" if upd else ""), {"update": upd}, t=now)
            if configured(self._cfg):
                self.send(now=now, timeout=STOP_SEND_TIMEOUT_S)
        except Exception as e:                        # закрытие не должно падать из-за мониторинга
            log_event("sysmon", "Мониторинг: не удалось отправить «приложение закрыто»: %s" % e, "warn")

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
                self._enroll(now)
            except Exception as e:                  # агент не должен умирать от случайной ошибки
                log_event("sysmon", "Мониторинг: ошибка цикла: %s" % e, "error")
            self._stop.wait(TICK_S)

    def _enroll(self, now: float) -> None:
        """Токена нет — просим его у приёмника (раз в минуту, см. enroll_client). Получили — сохраняем и сразу шлём первый пульс."""
        cfg = self._cfg
        if cfg.get("token") or not cfg.get("auto_enroll", True):
            return
        with self._enroll_lock:
            patch = self.enroller.step(cfg, now)
            if patch:
                save_config(patch)
                self._cfg = load_config()
                self._kick.set()

    def enroll_now(self) -> dict:
        """Кнопка «Отправить заявку на сервер» на странице «Мониторинг»: заявка сразу, без пауз и даже при выключенной автозаявке.
        Возвращает {ok, state, error}: ok — заявка принята приёмником (ждёт «Принять») или токен уже получен."""
        with self._enroll_lock:
            cfg = self._cfg = load_config()
            if cfg.get("token"):
                return {"ok": False, "state": "has_token", "error": "токен уже есть — регистрация не нужна"}
            self.enroller.hurry()
            patch = self.enroller.step(cfg, self.clock())
            if patch:
                save_config(patch)
                self._cfg = load_config()
                self._kick.set()
            st = self.enroller.status
            return {"ok": st["state"] in ("pending", "done"), "state": st["state"], "error": st.get("error") or ""}

    # ---- ожидание токена

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
    def _emit(self, kind: str, level: str, text: str, data: Optional[dict] = None, t: Optional[float] = None, push: bool = False) -> dict:
        t = self.clock() if t is None else t
        ev = {"t": t, "kind": kind, "level": level, "text": text, "data": data or {}}
        self.recent.appendleft(ev)
        if configured(self._cfg):
            outbox_add(kind, level, text, data, t)
            if level in URGENT or push:          # push — жёлтое предупреждение: не тревога, но знать надо сразу
                self._kick.set()
        return ev

    def add_positions(self, info: dict) -> None:
        """Положения трёх датчиков на доезде отвода/подвода (колбэк автомата): оценить, запомнить для правил и положить в очередь —
        уйдёт с ближайшим пульсом. Срочно не отправляем: срочное — это тревога по плохой пробе (см. _position_rules)."""
        if not isinstance(info, dict) or info.get("step") not in STEP_KEYS:
            return
        cfg = self._cfg
        rec = {k: info.get(k) for k in ("t", "step", "target", "calc", "enc", "ai", "timeout", "sv", "stage")}
        rec["t"] = _num(rec["t"]) or self.clock()
        rec["timeout"] = bool(rec["timeout"])
        rec.update(position_verdict(rec, cfg))
        self.positions.append(rec)
        if configured(cfg):
            outbox_add("positions", "info", position_text(rec), rec, rec["t"])

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
        d["guard"] = g("guard", {}) or {}
        d["services"] = g("services", {}) or {}
        d["motion"] = g("motion", None)
        d["disk"] = g("disk", None)
        d["positions"] = list(self.positions)
        return d

    def _black_text(self, d: dict, head: str) -> str:
        """Текст тревоги «чёрный кадр» с тем, что видно по плате (свет, 12V, статус, сбросы): по Telegram сразу ясно, куда смотреть."""
        state, ext = d.get("state") or {}, d.get("ext") or {}
        cur = (d.get("camera") or {}).get("current") or {}
        bits = ["свет %s" % ("%s %%" % state.get("led_bright") if state.get("led_on") else "выкл")]
        if cur.get("exposure_time") is not None:
            bits.append("экспозиция %s мкс" % _fmt_val(cur["exposure_time"]))
        u12 = _num(ext.get("u12v"))
        if u12 is not None:
            bits.append("12V %.1f В" % (u12 / 1000.0))
        st = _num(ext.get("init_status"))
        if st is not None:
            bits.append("статус платы 0x%04X" % int(st))
        resets = [_num(ext.get(k)) for k in ("wdt_resets", "lan_resets", "lan_stack_resets")]
        if all(r is not None for r in resets):
            bits.append("сбросы платы: сторож %d, LAN %d/%d" % tuple(int(r) for r in resets))
        if (d.get("status") or {}).get("connected") is False:
            bits.append("плата не отвечает")
        guard = d.get("guard") or {}
        if guard.get("t"):
            bits.append("плату уже перезапускали в %s: %s" % (time.strftime("%H:%M", time.localtime(guard["t"])), guard.get("text") or guard.get("result")))
        return "%s (%s)" % (head, ", ".join(bits))

    def _black_rule(self, d: dict, cam: dict) -> str:
        """Живой поток камеры микроскопа непрерывно чёрный BLACK_SECONDS, а свет включён и экспозиция не нулевая -> текст тревоги, иначе ''."""
        import camera_params
        secs = _num(cam.get("black_s")) or 0
        state = d.get("state") or {}
        if not (cam.get("serial") and camera_params.black_alarm(secs, cam.get("streaming"), state.get("led_on"),
                                                                _num(state.get("led_bright")), _num((cam.get("current") or {}).get("exposure_time")))):
            return ""
        return self._black_text(d, "Кадр камеры чёрный уже %s при включённом свете" % fmt_dur(secs))

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
            guard = d.get("guard") or {}
            if guard.get("result") == "no_return":                        # защита перезапустила плату, а она не поднялась — сразу, без подтверждений
                found["board_reboot"] = ("high", "Защита от чёрного кадра перезапустила плату: " + str(guard.get("text")))
            black = self._black_rule(d, cam)
            if black:
                found["black"] = ("high", black)
            elif cam.get("serial") and not cam.get("streaming"):
                keep.add("black")                     # камера не стримит — не знаем, вернулся ли свет, тревога остаётся как была
            if cv.get("state") == "black" and d.get("cv_enabled") and stage_ok:
                found["black"] = ("high", self._black_text(d, "Проба пропущена: кадр абсолютно чёрный"))
            elif cv.get("state") == "overexposed" and d.get("cv_enabled") and stage_ok:
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

            # варка идёт (стадия в зоне цикла), а «Автомат» выключен или стоит «Ручной»: проб нет, и правило «нет проб» ниже молчит —
            # оно работает только при включённом Автомате. 29–31.10 на Кире так пропали 16 варок: после перезапуска «Автомат» включает страница в браузере
            idle = bool(stage_ok and d.get("cv_enabled") and d.get("serial") and not state.get("fault") and (state.get("manual") or not state.get("cyclic")))
            if not idle:
                self._auto_off_since = None
            else:
                self._auto_off_since = self._auto_off_since or now
                if now - self._auto_off_since >= self._cfg["auto_off_min"] * 60:
                    found["auto_off"] = ("medium" if state.get("manual") else "high", "Варка идёт (стадия %s), а %s уже %s — проб нет" % (
                        state.get("stage"), "стоит «Ручной»" if state.get("manual") else "«Автомат» выключен", fmt_dur(now - self._auto_off_since)))

            self._position_rules(found, d, now)
            self._autocal_rules(found, state, now)

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

    def _bad_flags(self, step: str, recs: list) -> list:
        """По каждой пробе: (плохая ли, почему). Отвод — по самой пробе. Подвод — по медиане последних NEAR_MEDIAN_N проб:
        одиночный пик расхождения тревогу не открывает; не доехал (таймаут) — плохая сразу. Меньше N проб подряд с замером — не судим."""
        out = []
        tol = self._cfg["gap_near_um"]
        for i, r in enumerate(recs):
            if step != "подвод":
                out.append((not r.get("ok"), r.get("why") or "не в норме"))
            elif r.get("timeout"):
                out.append((True, r.get("why") or "не доехал за отведённое время"))
            else:
                win = recs[max(0, i - NEAR_MEDIAN_N + 1):i + 1]
                gaps = [x.get("gap") for x in win if not x.get("timeout") and x.get("gap") is not None]
                med = sorted(gaps)[len(gaps) // 2] if len(gaps) == NEAR_MEDIAN_N else None
                bad = med is not None and med > tol
                out.append((bad, "датчики разошлись на %d мкм (допуск %d, медиана %d проб)" % (med, tol, NEAR_MEDIAN_N) if bad else ""))
        return out

    def _position_rules(self, found: dict, d: dict, now: float) -> None:
        """Отвод и подвод: плохая проба (не доехал / датчики разошлись; на подводе — медиана трёх проб) — жёлтое предупреждение сразу;
        bad_streak плохих подряд на этом шаге — красная тревога. Хорошая проба снимает."""
        need = self._cfg["bad_streak"]
        for step, key in STEP_KEYS.items():
            recs = [r for r in (d.get("positions") or []) if r.get("step") == step and now - (r.get("t") or 0) <= POS_STALE_S]
            flags = self._bad_flags(step, recs)
            run = 0
            for bad, _ in reversed(flags):
                if not bad:
                    break
                run += 1
            if run:
                text = "%s: %s" % (step.capitalize(), flags[-1][1])
                if run > 1:
                    text += " · проб подряд: %d" % run
                found["pos_" + key] = ("high" if run >= need else "medium", text)

    def _autocal_rules(self, found: dict, state: dict, now: float) -> None:
        """Автокалибровка нуля: не удалась — красная сразу; «ноль уполз» — жёлтая сразу, красная при повторе; давно не было — жёлтая."""
        ac = state.get("autocal") or {}
        if not ac:
            return
        cfg = self._cfg
        if ac.get("failed"):
            found["autocal_failed"] = ("high", "Автокалибровка нуля: " + str(ac.get("fail_msg") or "не удалась")[:140])
        last = ac.get("last") or {}
        ts = _iso_ts(last.get("ts"))
        if ts is not None and ts != self._ac_seen_ts:                   # новая калибровка — в историю дрейфа нуля
            self._ac_seen_ts = ts
            diff = _num(last.get("diff_before"))
            self._ac_drift.append((diff is not None and abs(diff) > cfg["drift_um"], abs(diff) if diff is not None else 0))
        if ts is not None and now - ts <= cfg["overdue_h"] * 3600 and self._ac_drift and self._ac_drift[-1][0]:
            run = 0
            for bad, _ in reversed(self._ac_drift):
                if not bad:
                    break
                run += 1
            text = "Ноль уползал на %d мкм до автокалибровки (допуск %d)" % (self._ac_drift[-1][1], cfg["drift_um"])
            if run > 1:
                text += " · калибровок подряд: %d" % run
            found["autocal_drift"] = ("high" if run >= cfg["bad_streak"] else "medium", text)
        working = bool(state.get("stage_ok") and state.get("cyclic") and not state.get("manual"))
        if not working:
            self._work_since = None
        else:
            self._work_since = self._work_since or now
            if ac.get("enabled"):
                ok_ts = ts if ts is not None and str(last.get("result") or "").startswith("готово") else 0
                ref = max(self._work_since, ok_ts)
                if now - ref >= cfg["overdue_h"] * 3600:
                    found["autocal_overdue"] = ("medium", "Автокалибровка включена, а успешной не было уже %s" % fmt_dur(now - ref))

    def evaluate(self, d: dict, now: float) -> list[dict]:
        """Применить правила: открыть/снять тревоги, отметить перезагрузку платы. Возвращает новые события."""
        events: list = []
        found, keep = self._rules(d, now)
        for code in found:                               # подтверждение: открыть тревогу можно, когда правило сработало CONFIRM проверок подряд
            self._seen[code] = self._seen.get(code, 0) + 1
        for code in [c for c in self._seen if c not in found and c not in keep]:
            self._seen.pop(code, None)
        for code, (level, text) in found.items():
            push = code.startswith(PUSH_CODES)
            if code not in self.active:
                if self._seen[code] < CONFIRM.get(code, 1):
                    continue
                self.active[code] = {"level": level, "text": text, "since": now}
                events.append(self._emit("alert", level, text, {"code": code, "state": "open"}, t=now, push=push))
            elif self.active[code]["level"] != level:         # жёлтое предупреждение стало красной тревогой (повтор) — сообщаем ещё раз
                self.active[code].update(level=level, text=text)
                events.append(self._emit("alert", level, text, {"code": code, "state": "open"}, t=now, push=push))
            else:
                self.active[code]["text"] = text
        for code in [c for c in self.active if c not in found and c not in keep]:
            old = self.active.pop(code)
            events.append(self._emit("alert", "info", "Снято: " + old["text"], {"code": code, "state": "closed"}, t=now))
        up, exact = plate_uptime(d.get("ext") or {})
        if up is not None:
            prev, prev_exact = self._plate_up, self._plate_up_exact
            if prev is not None and prev_exact == exact and up + 10 < prev:
                # без счётчика кругов падение с «почти 65536» к малому числу — обычное обнуление регистра, а не перезагрузка
                wrap = not exact and prev > PLATE_UP_WRAP - 3600 and up < 3600
                if not wrap:
                    by_guard = now - ((d.get("guard") or {}).get("t") or 0) < 300       # её только что перезапустила защита от чёрного кадра
                    events.append(self._emit("plate_reboot", "high", "Плата микроскопа перезагрузилась (её аптайм сбросился)"
                                             + (" по команде защиты от чёрного кадра" if by_guard else ""), {}, t=now))
            self._plate_up, self._plate_up_exact = up, exact
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
        disk = _disk_block(d.get("disk"))
        if disk:
            snap["disk"] = disk
        motion = _motion_block(d["motion"]) if d.get("motion") else None
        if motion:
            snap["motion"] = motion
        snap["text"] = summary_text(snap)
        return mask_secrets(snap)

    def state(self) -> dict:
        """Всё для страницы «Мониторинг»."""
        cfg = load_config()
        d = self.collect()
        snap = self.snapshot(d, cfg)
        return {"snapshot": snap, "alerts": snap["alerts"], "send": {**self.status, "pending": outbox_count(),
                "configured": configured(cfg)}, "config": public_config(cfg), "recent": list(self.recent), "starts": starts(15),
                "enroll": {**self.enroller.status, "auto": cfg["auto_enroll"] and not cfg["token"]}}

    # ---- отправка
    def send(self, now: Optional[float] = None, force: bool = False, timeout: Optional[float] = None) -> dict:
        """Одна отправка: снимок + накопленные события. Успех — HTTP 2xx; неуспех — события остаются, повтор позже.
        force — «Проверить связь»: слать, даже если галочка «Отправлять» ещё не включена (адрес и токен нужны всё равно)."""
        with self._send_lock:
            now = self.clock() if now is None else now
            cfg = load_config()
            self._cfg = cfg
            if not (cfg["url"] and cfg["token"]):
                return {"ok": False, "error": "Не заданы адрес сервера и токен"}
            try:
                validate_url(cfg["url"], cfg["allow_http"])
            except ValueError as e:                              # файл правили руками: токен по открытому http не уходит
                return {"ok": False, "error": str(e)}
            if not cfg["enabled"] and not force:
                return {"ok": False, "error": "Отправка выключена"}
            bad = token_problem(cfg["token"])              # сохранённый раньше «токен» мог быть адресом или обрубком: понятнее, чем 401
            if bad:
                self.status.update({"last_try_t": now, "http": None, "ok": False, "error": bad})
                self._next_send = now + cfg["interval_s"]
                self._kick.clear()
                return {"ok": False, "error": bad}
            events = outbox_batch()
            body = {"schema": SCHEMA, "sent_t": now, "snapshot": self.snapshot(None, cfg), "events": events}
            via_proxy = cfg["system_proxy"] and not closed_network(cfg["url"], cfg["allow_http"])
            ok, code, err = _post(cfg["url"] + "/ingest", cfg["token"], body, via_proxy, **({"timeout": timeout} if timeout else {}))
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


def _motion_block(m: dict) -> Optional[dict]:
    """Камеры движения для панели: имя, работает ли, кадров в секунду, сколько секунд назад был последний кадр, пишется ли запись."""
    cams = []
    for c in (m.get("cameras") or [])[:MOTION_CAMS_MAX]:
        if not isinstance(c, dict):
            continue
        cams.append({"name": str(c.get("name") or c.get("id") or "?")[:40], "on": bool(c.get("enabled", True)), "ok": bool(c.get("connected")),
                     "fps": _num(c.get("fps"), 1), "age_s": _num(c.get("age_s"), 0), "rec": bool(c.get("recording")),
                     "error": str(c.get("error") or "")[:80] or None})
    if not cams:
        return None
    return {"cameras": cams}


def _disk_block(disk: Optional[dict]) -> Optional[dict]:
    """Свободно / всего на диске приложения, ГБ."""
    if not isinstance(disk, dict) or not disk.get("total"):
        return None
    return {"free_gb": round(disk["free"] / 1e9, 1), "total_gb": round(disk["total"] / 1e9, 1)}


def _micro_block(d: dict, now: float) -> dict:
    state, status, ext, cv = d.get("state") or {}, d.get("status") or {}, d.get("ext") or {}, d.get("cv") or {}
    pr = d.get("probes") or {}
    u12 = _num(ext.get("u12v"))
    ac = state.get("autocal") or {}
    last = ac.get("last") or {}
    pos = {}
    for step, key in STEP_KEYS.items():                  # по последней свежей записи на каждый шаг
        recs = [r for r in (d.get("positions") or []) if r.get("step") == step and now - (r.get("t") or 0) <= POS_STALE_S]
        if recs:
            r = recs[-1]
            pos[key] = {k: r.get(k) for k in ("t", "ok", "gap", "why", "target", "calc", "enc", "ai", "timeout")}
    return {
        "autocal": {"enabled": ac.get("enabled"), "every_n": ac.get("every_n"), "count": ac.get("count"), "failed": ac.get("failed"),
                    "fail_msg": ac.get("fail_msg"), "last_ts": _iso_ts(last.get("ts")), "last_result": last.get("result"),
                    "diff_before": _num(last.get("diff_before")), "diff_after": _num(last.get("diff_after"))} if ac else None,
        "positions": pos,
        "plate_connected": status.get("connected"),
        "stage": state.get("stage"), "stage_name": stage_name(state.get("stage")), "stage_ok": state.get("stage_ok"),
        "step": state.get("step"), "manual": state.get("manual"), "auto": state.get("cyclic"), "fault": state.get("fault"),
        "cv_state": cv.get("state"), "cv_enabled": d.get("cv_enabled"),
        "last_probe_t": pr.get("last_t"), "probes_today": pr.get("today"),
        "plate_up_s": plate_uptime(ext)[0], "plate_temp": _num(ext.get("temp")),
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
            ac = m.get("autocal")
            if ac and ac.get("failed"):
                lines.append("Автокалибровка: не удалась — %s" % (ac.get("fail_msg") or "причина неизвестна"))
            elif ac and ac.get("last_ts"):
                txt = "Автокалибровка: %s, %s назад" % (ac.get("last_result") or "—", fmt_dur(s["t"] - ac["last_ts"]))
                if ac.get("diff_before") is not None:
                    txt += ", ноль уползал на %d мкм" % abs(ac["diff_before"])
                lines.append(txt)
            pos = m.get("positions") or {}
            names = {"retract": "Отвод", "approach": "Подвод"}
            if pos and all(v.get("ok") for v in pos.values()):      # плохая проба видна тревогой (жёлтой или красной), здесь не дублируем
                gaps = [v["gap"] for v in pos.values() if v.get("gap") is not None]
                lines.append("%s в норме%s" % (" и ".join(names[k].lower() for k in pos),
                                               ", расхождение датчиков до %d мкм" % max(gaps) if gaps else ""))
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
    mo = s.get("motion")
    if mo:
        cams = mo["cameras"]
        lines.append("Камеры движения: %d из %d работают" % (sum(1 for c in cams if c["on"] and c["ok"]), sum(1 for c in cams if c["on"])))
    dk = s.get("disk")
    if dk:
        lines.append("Диск: свободно %g ГБ из %g" % (dk["free_gb"], dk["total_gb"]))
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


_update_restart_t: Optional[float] = None


def note_update_restart() -> None:
    """Апдейтер запущен и сейчас закроет приложение: закрытие не тревога (его вызвал сам завод)."""
    global _update_restart_t
    _update_restart_t = time.time()


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


def report(kind: str, level: str, text: str, data: Optional[dict] = None) -> bool:
    """Событие от другого модуля (автообновление): в очередь на приёмник и сразу, не дожидаясь пульса. Агент не запущен или отправка
    не настроена — молча ничего (False): мониторинг не обязателен."""
    ag = _agent
    if ag is None or not configured(ag._cfg):
        return False
    ag._emit(kind, level, text, data, push=True)
    return True


def start_reason() -> Optional[str]:
    """Почему стартовало это приложение (pc_reboot / app_restart / app_update / first) — по записи, которую агент сделал при старте."""
    ag = _agent
    return (ag.start_info or {}).get("reason") if ag is not None and ag.start_info else None


def record_positions(info: dict) -> None:
    """Положения датчиков на отводе/подводе — из колбэка автомата. Агент не запущен — молча ничего (мониторинг не обязателен)."""
    ag = _agent
    if ag is not None:
        ag.add_positions(info)
