"""monitor_store — хранилище приёмника мониторинга: заводы, токены (только хеши), события, открытые тревоги, правило «нет пульса».

Завод шлёт пульс (sysmon.py в web_MVS): снимок «как дела» + события с прошлой отправки. Здесь:
  * токен завода хранится только хешем (SHA-256); сам токен показывается один раз при выдаче;
  * события из пульса пишутся по паре (завод, eid): повторная доставка после обрыва связи дублей не даёт;
  * если завод прислал снимок, а события про старт/тревогу потерялись (отправка ещё не была включена), они достраиваются
    из снимка: «start» из snapshot.start, открытие/снятие тревоги по сравнению snapshot.alerts с уже открытыми;
  * положения датчиков на отводе/подводе (события kind="positions") идут не в журнал событий, а в свою таблицу positions —
    по ним панель показывает таблицу проб и график; тревоги по плохим пробам завод присылает сам, обычными тревогами;
  * очередь уведомлений в Telegram (таблица notify): в неё попадают только свежие критичные и красные события и «восстановилось»
    (жёлтые — нет); отправляет monitor_telegram.Notifier, очередь переживает перезапуск и обрыв связи;
  * «нет пульса»: прошло больше 2 интервалов + OFFLINE_GRACE_S — критичное событие; вернулся пульс — событие «связь восстановилась».
    После запуска самого приёмника ждём такой же срок, прежде чем объявлять заводы недоступными (сервер сам мог не работать).
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Optional

SCHEMA = 1                       # версия формата пульса (snapshot.schema из sysmon.py)
LEVELS = ("critical", "high", "medium", "info")
DEFAULT_INTERVAL_S = 300
OFFLINE_GRACE_S = 120            # запас к двум пропущенным пульсам
EVENT_MAX = 200                  # событий принимаем из одного пульса
TEXT_MAX = 300
RETENTION_DAYS = 90
SKEW_MAX_S = 7 * 86400           # время события от завода дальше недели от сейчас — часы врут, берём время приёма
NAME_MAX = 40
ALERT_DEDUP_S = 120              # событие тревоги от завода и достроенное из снимка — одно и то же, если время рядом
NOTIFY_LEVELS = ("critical", "high")                              # в Telegram идут только они (жёлтое «medium» — только в панели)
NOTIFY_KINDS = ("start", "alert", "plate_reboot", "offline")      # какие события бывают «открытием» тревоги
OFFLINE_KINDS = ("offline", "offline_repeat")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def offline_limit(interval_s: Optional[float]) -> float:
    """Сколько можно молчать до тревоги: два пропущенных пульса и запас."""
    return 2 * (interval_s or DEFAULT_INTERVAL_S) + OFFLINE_GRACE_S


def _num(v) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


class Store:
    def __init__(self, path, clock: Callable[[], float] = time.time, retention_days: int = RETENTION_DAYS, notify: Optional[dict] = None):
        """notify — настройки уведомлений ({"fresh_s": 900}); None — очередь в Telegram не ведётся (ничего не копится)."""
        self.notify_cfg = notify
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.retention_days = retention_days
        self.started_at = clock()
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS plants (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, token_hash TEXT NOT NULL UNIQUE,
                created_t REAL NOT NULL, last_seen_t REAL, interval_s INTEGER, snapshot TEXT, offline_since REAL);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, plant_id INTEGER NOT NULL, eid TEXT NOT NULL, t REAL NOT NULL, recv_t REAL NOT NULL,
                kind TEXT NOT NULL, level TEXT NOT NULL, text TEXT NOT NULL, code TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT '',
                data TEXT NOT NULL DEFAULT '{}', UNIQUE (plant_id, eid));
            CREATE INDEX IF NOT EXISTS ix_events_plant ON events (plant_id, t);
            CREATE TABLE IF NOT EXISTS alerts (
                plant_id INTEGER NOT NULL, code TEXT NOT NULL, level TEXT NOT NULL, text TEXT NOT NULL, since_t REAL NOT NULL,
                PRIMARY KEY (plant_id, code));
            CREATE TABLE IF NOT EXISTS notify (
                id INTEGER PRIMARY KEY AUTOINCREMENT, plant_id INTEGER NOT NULL, plant_name TEXT NOT NULL, eid TEXT NOT NULL,
                t REAL NOT NULL, created_t REAL NOT NULL, level TEXT NOT NULL, kind TEXT NOT NULL, code TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT '', text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', reason TEXT NOT NULL DEFAULT '',
                tries INTEGER NOT NULL DEFAULT 0, next_try_t REAL NOT NULL DEFAULT 0, sent_t REAL, UNIQUE (plant_id, eid));
            CREATE INDEX IF NOT EXISTS ix_notify_status ON notify (status, id);
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, plant_id INTEGER NOT NULL, eid TEXT NOT NULL, t REAL NOT NULL, step TEXT NOT NULL,
                target REAL, calc REAL, enc REAL, ai REAL, gap REAL, ok INTEGER NOT NULL DEFAULT 1, timeout INTEGER NOT NULL DEFAULT 0,
                why TEXT NOT NULL DEFAULT '', sv REAL, stage INTEGER, UNIQUE (plant_id, eid));
            CREATE INDEX IF NOT EXISTS ix_positions_plant ON positions (plant_id, t);
        """)
        self.db.commit()

    def close(self) -> None:
        with self._lock:
            self.db.close()

    # ------------------------------------------------------------------------------------------------------------- заводы

    def create_plant(self, name: str) -> tuple[int, str]:
        """Завести завод и выдать токен (показать один раз). ValueError — название пустое, длинное или занято."""
        name = " ".join((name or "").split())
        if not name or len(name) > NAME_MAX:
            raise ValueError("Название завода — от 1 до %d символов" % NAME_MAX)
        token = secrets.token_urlsafe(24)
        with self._lock:
            try:
                cur = self.db.execute("INSERT INTO plants (name, token_hash, created_t) VALUES (?,?,?)", (name, hash_token(token), self.clock()))
                self.db.commit()
            except sqlite3.IntegrityError:
                raise ValueError("Завод с таким названием уже есть")
        return cur.lastrowid, token

    def rotate_token(self, plant_id: int) -> str:
        token = secrets.token_urlsafe(24)
        with self._lock:
            n = self.db.execute("UPDATE plants SET token_hash=? WHERE id=?", (hash_token(token), plant_id)).rowcount
            self.db.commit()
        if not n:
            raise LookupError("Завода нет")
        return token

    def delete_plant(self, plant_id: int) -> bool:
        with self._lock, self.db:
            self.db.execute("DELETE FROM events WHERE plant_id=?", (plant_id,))
            self.db.execute("DELETE FROM alerts WHERE plant_id=?", (plant_id,))
            self.db.execute("DELETE FROM positions WHERE plant_id=?", (plant_id,))
            self.db.execute("DELETE FROM notify WHERE plant_id=?", (plant_id,))
            return self.db.execute("DELETE FROM plants WHERE id=?", (plant_id,)).rowcount > 0

    def plant_by_token(self, token: str) -> Optional[sqlite3.Row]:
        if not token:
            return None
        with self._lock:
            return self.db.execute("SELECT * FROM plants WHERE token_hash=?", (hash_token(token),)).fetchone()

    # ------------------------------------------------------------------------------------------------------------- приём

    def _clamp_t(self, t, now: float) -> float:
        t = _num(t)
        return now if t is None or abs(t - now) > SKEW_MAX_S else t

    def _insert_event(self, plant_id: int, eid: str, t: float, now: float, kind: str, level: str, text: str,
                      code: str = "", state: str = "", data: Optional[dict] = None) -> int:
        data_json = json.dumps(data or {}, ensure_ascii=False)
        if len(data_json) > 2000:
            data_json = "{}"
        level = level if level in LEVELS else "info"
        n = self.db.execute(
            "INSERT OR IGNORE INTO events (plant_id, eid, t, recv_t, kind, level, text, code, state, data) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (plant_id, eid[:64], t, now, kind[:24], level, text[:TEXT_MAX], code[:40], state[:12], data_json)).rowcount
        if n and self.notify_cfg is not None:
            self._maybe_notify(plant_id, eid[:64], t, now, kind[:24], level, state[:12], code[:40], text[:TEXT_MAX])
        return n

    # ------------------------------------------------------------------------------------------------ очередь уведомлений в Telegram

    def _last_notified(self, plant_id: int, kinds: tuple, code: Optional[str] = None) -> Optional[sqlite3.Row]:
        marks = ",".join("?" * len(kinds))
        sql = "SELECT * FROM notify WHERE plant_id=? AND kind IN (%s) AND status IN ('pending','sent')" % marks
        args = [plant_id, *kinds]
        if code is not None:
            sql += " AND code=?"
            args.append(code)
        return self.db.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()

    def _maybe_notify(self, plant_id: int, eid: str, t: float, now: float, kind: str, level: str, state: str, code: str, text: str) -> None:
        """Нужно ли сообщить об этом событии в Telegram. Шлём: свежие критичные и красные события (старт ПК/приложения, «нет пульса»,
        красные тревоги, перезагрузка платы), «снято» и «связь восстановилась» — только если об открытии мы сообщали. Жёлтое — никогда.
        Старое событие (после обрыва связи пришла пачка) — только в журнал."""
        if now - t > self.notify_cfg.get("fresh_s", 900):
            return
        if level in NOTIFY_LEVELS and kind in NOTIFY_KINDS and state != "closed":
            pass
        elif kind == "alert" and state == "closed":
            last = self._last_notified(plant_id, ("alert",), code)
            if last is None or last["state"] != "open":
                return
        elif kind == "online":
            last = self._last_notified(plant_id, OFFLINE_KINDS + ("online",))
            if last is None or last["kind"] == "online":
                return
        else:
            return
        row = self.db.execute("SELECT name FROM plants WHERE id=?", (plant_id,)).fetchone()
        self.db.execute("INSERT OR IGNORE INTO notify (plant_id, plant_name, eid, t, created_t, level, kind, code, state, text) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (plant_id, row["name"] if row else "?", eid, t, now, level, kind, code, state, text))

    def notify_pending(self, now: float, limit: int = 100) -> list:
        with self._lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM notify WHERE status='pending' AND next_try_t<=? ORDER BY id LIMIT ?", (now, limit))]

    def notify_count(self) -> int:
        with self._lock:
            return self.db.execute("SELECT COUNT(*) n FROM notify WHERE status='pending'").fetchone()["n"]

    def notify_mark(self, ids: list, status: str, now: float, reason: str = "") -> None:
        with self._lock, self.db:
            self.db.executemany("UPDATE notify SET status=?, reason=?, sent_t=? WHERE id=?",
                                [(status, reason, now if status == "sent" else None, i) for i in ids])

    def notify_retry(self, ids: list, next_try_t: float, reason: str) -> None:
        with self._lock, self.db:
            self.db.executemany("UPDATE notify SET tries=tries+1, next_try_t=?, reason=? WHERE id=?", [(next_try_t, reason[:200], i) for i in ids])

    def notify_offline_reminders(self, now: float, every_s: float) -> int:
        """Завод всё ещё без связи, а об этом уже писали: раз в every_s напоминаем (иначе после одного сообщения ночью про него забудут)."""
        n = 0
        with self._lock, self.db:
            for p in self.db.execute("SELECT * FROM plants WHERE offline_since IS NOT NULL").fetchall():
                last = self._last_notified(p["id"], OFFLINE_KINDS)
                if last is None or last["status"] != "sent" or now - (last["sent_t"] or now) < every_s:
                    continue
                if self.db.execute("SELECT 1 FROM notify WHERE plant_id=? AND kind IN ('offline','offline_repeat') AND status='pending'", (p["id"],)).fetchone():
                    continue
                hours = (now - p["offline_since"]) / 3600
                text = "Всё ещё нет пульса (уже %s)" % ("%d ч" % round(hours) if hours >= 1.5 else "%d мин" % round(hours * 60))
                n += self.db.execute("INSERT OR IGNORE INTO notify (plant_id, plant_name, eid, t, created_t, level, kind, code, state, text) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                     (p["id"], p["name"], "srv-offline-rep-%d" % int(now), now, now, "critical", "offline_repeat", "", "open", text)).rowcount
        return n

    def _insert_position(self, plant_id: int, eid: str, t: float, d: dict) -> None:
        step = d.get("step")
        if step not in ("отвод", "подвод"):
            return
        stage = d.get("stage")
        self.db.execute(
            "INSERT OR IGNORE INTO positions (plant_id, eid, t, step, target, calc, enc, ai, gap, ok, timeout, why, sv, stage) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (plant_id, eid[:64], t, step, _num(d.get("target")), _num(d.get("calc")), _num(d.get("enc")), _num(d.get("ai")), _num(d.get("gap")),
             0 if d.get("ok") is False else 1, 1 if d.get("timeout") else 0, str(d.get("why") or "")[:TEXT_MAX], _num(d.get("sv")),
             int(stage) if isinstance(stage, int) and not isinstance(stage, bool) else None))

    def _client_event(self, plant_id: int, e, now: float) -> int:
        if not isinstance(e, dict) or not e.get("eid"):
            return 0
        data = e.get("data") if isinstance(e.get("data"), dict) else {}
        if e.get("kind") == "positions":                # не событие журнала: запись для таблицы и графика проб
            self._insert_position(plant_id, str(e["eid"]), self._clamp_t(e.get("t"), now), data)
            return 0
        return self._insert_event(plant_id, str(e["eid"]), self._clamp_t(e.get("t"), now), now, str(e.get("kind") or "event"),
                                  str(e.get("level") or "info"), str(e.get("text") or ""), str(data.get("code") or ""),
                                  str(data.get("state") or ""), data)

    def _sync_start(self, plant_id: int, snap: dict, now: float) -> int:
        """Старт приложения из снимка, если события «start» от завода нет (его могли не включить на момент старта)."""
        s = snap.get("start")
        t = _num(s.get("t")) if isinstance(s, dict) else None
        if t is None:
            return 0
        if self.db.execute("SELECT 1 FROM events WHERE plant_id=? AND kind='start' AND ABS(t-?)<5 LIMIT 1", (plant_id, t)).fetchone():
            return 0
        return self._insert_event(plant_id, "srv-start-%d" % int(t), self._clamp_t(t, now), now, "start", str(s.get("level") or "info"),
                                  str(s.get("text") or "Запуск приложения"), data={"reason": s.get("reason"), "from": "snapshot"})

    def _sync_alerts(self, plant_id: int, snap: dict, now: float) -> int:
        """Открытые тревоги держим по снимку: появилась — открыли (и записали в журнал, если завод не прислал событие), пропала — сняли."""
        new = 0
        cur = {}
        for a in snap.get("alerts") or []:
            if isinstance(a, dict) and a.get("code"):
                cur[str(a["code"])[:40]] = a
        open_rows = {r["code"]: r for r in self.db.execute("SELECT * FROM alerts WHERE plant_id=?", (plant_id,))}
        for code, a in cur.items():
            level = str(a.get("level") or "info")
            level = level if level in LEVELS else "info"
            text = str(a.get("text") or code)[:TEXT_MAX]
            if code in open_rows:
                self.db.execute("UPDATE alerts SET level=?, text=? WHERE plant_id=? AND code=?", (level, text, plant_id, code))
                continue
            since = self._clamp_t(a.get("since"), now)
            self.db.execute("INSERT INTO alerts (plant_id, code, level, text, since_t) VALUES (?,?,?,?,?)", (plant_id, code, level, text, since))
            if not self.db.execute("SELECT 1 FROM events WHERE plant_id=? AND kind='alert' AND code=? AND state='open' AND t>=? LIMIT 1",
                                   (plant_id, code, since - ALERT_DEDUP_S)).fetchone():
                new += self._insert_event(plant_id, "srv-alert-%s-%d-open" % (code, int(since)), since, now, "alert", level, text, code, "open")
        for code, r in open_rows.items():
            if code in cur:
                continue
            self.db.execute("DELETE FROM alerts WHERE plant_id=? AND code=?", (plant_id, code))
            if not self.db.execute("SELECT 1 FROM events WHERE plant_id=? AND kind='alert' AND code=? AND state='closed' AND t>=? LIMIT 1",
                                   (plant_id, code, r["since_t"])).fetchone():
                new += self._insert_event(plant_id, "srv-alert-%s-%d-closed" % (code, int(r["since_t"])), now, now, "alert", "info",
                                          "Снято: " + r["text"], code, "closed")
        return new

    def ingest(self, plant_id: int, payload) -> dict:
        """Принять пульс. ValueError — формат не тот (завод получит 400), LookupError — завод удалили."""
        if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
            raise ValueError("неизвестный формат пульса (schema)")
        snap = payload.get("snapshot")
        if not isinstance(snap, dict):
            raise ValueError("в пульсе нет снимка (snapshot)")
        events = payload.get("events") or []
        if not isinstance(events, list):
            raise ValueError("events должен быть списком")
        now = self.clock()
        with self._lock, self.db:
            p = self.db.execute("SELECT * FROM plants WHERE id=?", (plant_id,)).fetchone()
            if p is None:
                raise LookupError("завода нет")
            new = sum(self._client_event(plant_id, e, now) for e in events[:EVENT_MAX])
            new += self._sync_start(plant_id, snap, now)
            new += self._sync_alerts(plant_id, snap, now)
            if p["offline_since"] is not None:
                gap = now - (p["last_seen_t"] or p["offline_since"])
                new += self._insert_event(plant_id, "srv-online-%d" % int(now), now, now, "online", "info",
                                          "Связь восстановилась (не было %d мин)" % round(gap / 60))
            iv = _num(snap.get("interval_s"))
            iv = int(iv) if iv and 60 <= iv <= 3600 else (p["interval_s"] or DEFAULT_INTERVAL_S)
            self.db.execute("UPDATE plants SET last_seen_t=?, interval_s=?, snapshot=?, offline_since=NULL WHERE id=?",
                            (now, iv, json.dumps(snap, ensure_ascii=False), plant_id))
        return {"new_events": new}

    # ----------------------------------------------------------------------------------------------------------- «нет пульса»

    def _status(self, p: sqlite3.Row, now: float) -> str:
        if p["last_seen_t"] is None:
            return "waiting"
        if p["offline_since"] is not None:
            return "offline"
        limit = offline_limit(p["interval_s"])
        if now - p["last_seen_t"] > limit:
            return "offline" if now - self.started_at > limit else "checking"
        return "online"

    def check_offline(self) -> list[dict]:
        """Объявить недоступными заводы, которые молчат дольше допустимого. Возвращает новые события (для оповещений)."""
        now = self.clock()
        out = []
        with self._lock, self.db:
            for p in self.db.execute("SELECT * FROM plants WHERE last_seen_t IS NOT NULL AND offline_since IS NULL").fetchall():
                if self._status(p, now) != "offline":
                    continue
                self.db.execute("UPDATE plants SET offline_since=? WHERE id=?", (now, p["id"]))
                text = "Нет пульса уже %d мин" % round((now - p["last_seen_t"]) / 60)
                if self._insert_event(p["id"], "srv-offline-%d" % int(p["last_seen_t"]), now, now, "offline", "critical", text):
                    out.append({"plant": p["name"], "plant_id": p["id"], "level": "critical", "kind": "offline", "text": text, "t": now})
        return out

    def purge(self) -> int:
        with self._lock, self.db:
            cutoff = self.clock() - self.retention_days * 86400
            self.db.execute("DELETE FROM positions WHERE t < ?", (cutoff,))
            self.db.execute("DELETE FROM notify WHERE t < ? AND status!='pending'", (cutoff,))
            return self.db.execute("DELETE FROM events WHERE t < ?", (cutoff,)).rowcount

    # ------------------------------------------------------------------------------------------------------------- чтение

    @staticmethod
    def _event_row(r: sqlite3.Row) -> dict:
        return {"id": r["id"], "t": r["t"], "recv_t": r["recv_t"], "kind": r["kind"], "level": r["level"], "text": r["text"],
                "code": r["code"], "state": r["state"]}

    def events(self, plant_id: int, limit: int = 100, before_id: Optional[int] = None) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            if before_id:
                rows = self.db.execute("SELECT * FROM events WHERE plant_id=? AND id<? ORDER BY t DESC, id DESC LIMIT ?",
                                       (plant_id, before_id, limit)).fetchall()
            else:
                rows = self.db.execute("SELECT * FROM events WHERE plant_id=? ORDER BY t DESC, id DESC LIMIT ?", (plant_id, limit)).fetchall()
        return [self._event_row(r) for r in rows]

    def positions(self, plant_id: int, limit: int = 100) -> list[dict]:
        """Последние положения датчиков на отводе/подводе: новые первыми."""
        limit = max(1, min(int(limit), 500))
        with self._lock:
            rows = self.db.execute("SELECT * FROM positions WHERE plant_id=? ORDER BY t DESC, id DESC LIMIT ?", (plant_id, limit)).fetchall()
        return [{"t": r["t"], "step": r["step"], "target": r["target"], "calc": r["calc"], "enc": r["enc"], "ai": r["ai"], "gap": r["gap"],
                 "ok": bool(r["ok"]), "timeout": bool(r["timeout"]), "why": r["why"], "sv": r["sv"], "stage": r["stage"]} for r in rows]

    def plant_names(self) -> list:
        with self._lock:
            return [r["name"] for r in self.db.execute("SELECT name FROM plants ORDER BY name")]

    def plants_view(self) -> list[dict]:
        """Карточки для панели: статус, последний снимок, открытые тревоги, несколько последних событий."""
        now = self.clock()
        out = []
        with self._lock:
            for p in self.db.execute("SELECT * FROM plants ORDER BY name").fetchall():
                alerts = [{"code": a["code"], "level": a["level"], "text": a["text"], "since": a["since_t"]}
                          for a in self.db.execute("SELECT * FROM alerts WHERE plant_id=? ORDER BY since_t", (p["id"],))]
                try:
                    snap = json.loads(p["snapshot"]) if p["snapshot"] else None
                except ValueError:
                    snap = None
                out.append({
                    "id": p["id"], "name": p["name"], "status": self._status(p, now), "created_t": p["created_t"],
                    "last_seen_t": p["last_seen_t"], "interval_s": p["interval_s"], "offline_since": p["offline_since"],
                    "snapshot": snap, "alerts": alerts, "events": self.events(p["id"], 5)})
        return out
