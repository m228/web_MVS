"""monitor_store — хранилище приёмника мониторинга: заводы, токены (только хеши), события, открытые тревоги, правило «нет пульса».

Завод шлёт пульс (sysmon.py в web_MVS): снимок «как дела» + события с прошлой отправки. Здесь:
  * токен завода хранится только хешем (SHA-256); сам токен показывается один раз при выдаче;
  * события из пульса пишутся по паре (завод, eid): повторная доставка после обрыва связи дублей не даёт;
  * если завод прислал снимок, а события про старт/тревогу потерялись (отправка ещё не была включена), они достраиваются
    из снимка: «start» из snapshot.start, открытие/снятие тревоги по сравнению snapshot.alerts с уже открытыми;
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


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def offline_limit(interval_s: Optional[float]) -> float:
    """Сколько можно молчать до тревоги: два пропущенных пульса и запас."""
    return 2 * (interval_s or DEFAULT_INTERVAL_S) + OFFLINE_GRACE_S


def _num(v) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


class Store:
    def __init__(self, path, clock: Callable[[], float] = time.time, retention_days: int = RETENTION_DAYS):
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
        return self.db.execute(
            "INSERT OR IGNORE INTO events (plant_id, eid, t, recv_t, kind, level, text, code, state, data) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (plant_id, eid[:64], t, now, kind[:24], level if level in LEVELS else "info", text[:TEXT_MAX], code[:40], state[:12], data_json)).rowcount

    def _client_event(self, plant_id: int, e, now: float) -> int:
        if not isinstance(e, dict) or not e.get("eid"):
            return 0
        data = e.get("data") if isinstance(e.get("data"), dict) else {}
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
            return self.db.execute("DELETE FROM events WHERE t < ?", (self.clock() - self.retention_days * 86400,)).rowcount

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
