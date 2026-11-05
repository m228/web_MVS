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
import hmac
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
OFFLINE_AFTER_S = 1800           # «нет пульса» не раньше чем через полчаса молчания: пропавший пульс — ещё не беда (закрытое приложение сообщает само)
EVENT_MAX = 200                  # событий принимаем из одного пульса
TEXT_MAX = 300
RETENTION_DAYS = 90
SKEW_MAX_S = 7 * 86400           # время события от завода дальше недели от сейчас — часы врут, берём время приёма
NAME_MAX = 40
ERROR_KINDS = ("alert", "plate_reboot", "offline", "app_stop")                   # какие события журнала считаются ошибками (фильтр «Тревоги»)
VIEW_BLOCKS = ["pc", "disk", "services", "camera", "motion", "brew", "autocal", "sensors", "plate"]      # общие для всех заводов, затем микроскопные
DEFAULT_HIDDEN = ["plate"]                                          # что скрыто, пока Макс не настроил «Вид» (подробности про плату — по желанию)       # блоки карточки, которые можно скрыть в «Виде» (тревоги скрыть нельзя)
ALERT_DEDUP_S = 120              # событие тревоги от завода и достроенное из снимка — одно и то же, если время рядом
NOTIFY_LEVELS = ("critical", "high")                              # в Telegram идут только они (жёлтое «medium» — только в панели)
NOTIFY_KINDS = ("start", "alert", "plate_reboot", "offline", "app_stop")      # какие события бывают «открытием» тревоги
OFFLINE_KINDS = ("offline", "offline_repeat")
OUTAGE_MIN_S = 180               # приёмник молчал дольше — это перерыв (перезапуск занимает секунды)
ENROLL_PENDING_MAX = 30          # заявок «ждут решения» не больше: ключ регистрации не должен позволять забить панель
ENROLL_KEEP_DAYS = 14            # заявку, от которой 2 недели нет вестей, забываем


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def offline_limit(interval_s: Optional[float]) -> float:
    """Сколько можно молчать до сообщения «нет пульса»: два пропущенных пульса с запасом, но не меньше получаса."""
    return max(2 * (interval_s or DEFAULT_INTERVAL_S) + OFFLINE_GRACE_S, OFFLINE_AFTER_S)


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
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outages (start_t REAL NOT NULL, end_t REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS enroll (
                id INTEGER PRIMARY KEY AUTOINCREMENT, machine TEXT NOT NULL UNIQUE, name TEXT NOT NULL, version TEXT NOT NULL DEFAULT '',
                ip TEXT NOT NULL DEFAULT '', secret_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                first_t REAL NOT NULL, last_t REAL NOT NULL, plant_id INTEGER, token TEXT);
        """)
        if not any(r["name"] == "machine" for r in self.db.execute("PRAGMA table_info(plants)")):
            self.db.execute("ALTER TABLE plants ADD COLUMN machine TEXT")      # какой ПК зарегистрировался под этим заводом (авто-регистрация)
        if not any(r["name"] == "acked_t" for r in self.db.execute("PRAGMA table_info(alerts)")):
            self.db.execute("ALTER TABLE alerts ADD COLUMN acked_t REAL")      # когда Макс сбросил тревогу в панели (NULL — не сбрасывал)
            self.db.execute("ALTER TABLE alerts ADD COLUMN ack_level TEXT")    # какой она была тяжести в момент сброса: стала хуже — откроется снова
        self.db.commit()
        self._note_outage()

    def close(self) -> None:
        with self._lock:
            self.db.close()

    # ------------------------------------------------------------------------------------------- когда не работал сам приёмник

    def meta_get(self, key: str) -> Optional[str]:
        with self._lock:
            r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def meta_set(self, key: str, value: str) -> None:
        with self._lock, self.db:
            self.db.execute("INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def _note_outage(self) -> None:
        """При запуске сверяемся с последней отметкой «жив»: если приёмник молчал дольше OUTAGE_MIN_S, записываем перерыв.
        За это время обрывы связи заводов он не видел — утренняя сводка должна об этом сказать, а не писать «обрывов не было»."""
        now = self.clock()
        try:
            last = float(self.meta_get("alive_t"))
        except (TypeError, ValueError):
            last = None
        if last is not None and last < now and now - last > OUTAGE_MIN_S:
            with self._lock, self.db:
                self.db.execute("INSERT INTO outages (start_t, end_t) VALUES (?,?)", (last, now))
        self.meta_set("alive_t", repr(now))

    def touch_alive(self) -> None:
        self.meta_set("alive_t", repr(self.clock()))

    def outages(self, t0: float, t1: float) -> list[tuple]:
        """Перерывы работы приёмника, пересекающие окно [t0, t1], обрезанные по окну."""
        with self._lock:
            rows = self.db.execute("SELECT start_t, end_t FROM outages WHERE end_t > ? AND start_t < ? ORDER BY start_t", (t0, t1)).fetchall()
        return [(max(r["start_t"], t0), min(r["end_t"], t1)) for r in rows]

    def digest_facts(self, plant_id: int, t0: float, t1: float) -> dict:
        """Что было с заводом в окне [t0, t1] — только то, что записано в журнале: обрывы связи, запуски, перезагрузки платы, тревоги."""
        with self._lock:
            def q(sql, *args):
                return self.db.execute(sql, (plant_id, t0, t1) + args).fetchall()
            offline = [r["t"] for r in q("SELECT t FROM events WHERE plant_id=? AND t>=? AND t<? AND kind='offline' ORDER BY t")]
            starts = [(r["t"], r["text"]) for r in q("SELECT t, text FROM events WHERE plant_id=? AND t>=? AND t<? AND kind='start' ORDER BY t")]
            plate = [r["t"] for r in q("SELECT t FROM events WHERE plant_id=? AND t>=? AND t<? AND kind='plate_reboot' ORDER BY t")]
            alerts: dict = {}
            for r in q("SELECT t, level, text, code FROM events WHERE plant_id=? AND t>=? AND t<? AND kind='alert' AND state='open' ORDER BY t"):
                a = alerts.setdefault(r["code"], {"code": r["code"], "n": 0, "text": "", "level": "info", "first_t": r["t"]})
                a["n"] += 1
                a["text"] = r["text"]
                if LEVELS.index(r["level"]) < LEVELS.index(a["level"]):
                    a["level"] = r["level"]
        return {"offline": offline, "starts": starts, "plate_reboots": plate, "alerts": list(alerts.values())}

    # ------------------------------------------------------------------------------------------------------------- заводы

    def create_plant(self, name: str, machine: Optional[str] = None) -> tuple[int, str]:
        """Завести завод и выдать токен (показать один раз). ValueError — название пустое, длинное или занято.
        machine — id ПК, если завод заведён по заявке (авто-регистрация): по нему повторная заявка того же ПК получит новый токен."""
        name = " ".join((name or "").split())
        if not name or len(name) > NAME_MAX:
            raise ValueError("Название завода — от 1 до %d символов" % NAME_MAX)
        token = secrets.token_urlsafe(24)
        with self._lock:
            try:
                cur = self.db.execute("INSERT INTO plants (name, token_hash, created_t, machine) VALUES (?,?,?,?)",
                                      (name, hash_token(token), self.clock(), machine))
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

    def rename_plant(self, plant_id: int, name: str) -> str:
        """Переименовать завод (кнопка в панели): токен, журнал и датчики остаются. ValueError — название пустое, длинное или занято;
        LookupError — завода нет. Возвращает новое название."""
        name = " ".join((name or "").split())
        if not name or len(name) > NAME_MAX:
            raise ValueError("Название завода — от 1 до %d символов" % NAME_MAX)
        with self._lock:
            try:
                n = self.db.execute("UPDATE plants SET name=? WHERE id=?", (name, plant_id)).rowcount
                if n:
                    self.db.execute("UPDATE notify SET plant_name=? WHERE plant_id=?", (name, plant_id))     # ещё не отправленные сообщения — с новым названием
                self.db.commit()
            except sqlite3.IntegrityError:
                self.db.rollback()
                raise ValueError("Завод с таким названием уже есть")
        if not n:
            raise LookupError("Завода нет")
        return name

    def hidden_blocks(self, plant_id: int) -> list[str]:
        """Какие блоки карточки Макс скрыл через «Вид» (хранится в приёмнике, поэтому одинаково на всех устройствах)."""
        raw = self.meta_get("view:%d" % plant_id)
        if raw is None:
            return list(DEFAULT_HIDDEN)
        try:
            v = json.loads(raw)
        except ValueError:
            return list(DEFAULT_HIDDEN)
        return [b for b in v if b in VIEW_BLOCKS] if isinstance(v, list) else list(DEFAULT_HIDDEN)

    def set_hidden_blocks(self, plant_id: int, hidden) -> list[str]:
        """Запомнить скрытые блоки завода. ValueError — список кривой или блок неизвестен; LookupError — завода нет."""
        if not isinstance(hidden, list) or any(b not in VIEW_BLOCKS for b in hidden):
            raise ValueError("Неизвестный блок карточки")
        with self._lock:
            if not self.db.execute("SELECT 1 FROM plants WHERE id=?", (plant_id,)).fetchone():
                raise LookupError("Завода нет")
            clean = sorted(set(hidden), key=VIEW_BLOCKS.index)
            self.meta_set("view:%d" % plant_id, json.dumps(clean))
        return clean

    def set_hidden_blocks_all(self, hidden) -> list[str]:
        """Один набор скрытых блоков всем заводам сразу («для всех» в «Виде»): единый стандарт. ValueError — блок неизвестен."""
        if not isinstance(hidden, list) or any(b not in VIEW_BLOCKS for b in hidden):
            raise ValueError("Неизвестный блок карточки")
        clean = sorted(set(hidden), key=VIEW_BLOCKS.index)
        with self._lock, self.db:
            for r in self.db.execute("SELECT id FROM plants").fetchall():
                self.meta_set("view:%d" % r["id"], json.dumps(clean))
        return clean

    def delete_plant(self, plant_id: int) -> bool:
        with self._lock, self.db:
            self.db.execute("DELETE FROM meta WHERE key=?", ("view:%d" % plant_id,))
            self.db.execute("DELETE FROM events WHERE plant_id=?", (plant_id,))
            self.db.execute("DELETE FROM alerts WHERE plant_id=?", (plant_id,))
            self.db.execute("DELETE FROM positions WHERE plant_id=?", (plant_id,))
            self.db.execute("DELETE FROM notify WHERE plant_id=?", (plant_id,))
            self.db.execute("DELETE FROM enroll WHERE plant_id=?", (plant_id,))        # удалённый завод может зарегистрироваться заново
            return self.db.execute("DELETE FROM plants WHERE id=?", (plant_id,)).rowcount > 0

    def plant_by_token(self, token: str) -> Optional[sqlite3.Row]:
        if not token:
            return None
        with self._lock:
            return self.db.execute("SELECT * FROM plants WHERE token_hash=?", (hash_token(token),)).fetchone()

    # ------------------------------------------------------------------------------------------------------------- авто-регистрация

    def enroll_request(self, machine: str, name: str, version: str, ip: str, secret: str) -> dict:
        """Заявка клиента (повторяется каждую минуту, пока не примут). Возвращает {"status": pending|approved|rejected, "new": bool, "id", "name"}.
        approved приходит с токеном ОДИН раз (token), потом токен у приёмника стирается. Тот же ПК с другим секретом (переустановка) — заявка
        начинается заново. ValueError — поля не годятся; OverflowError — очередь заявок переполнена."""
        machine, secret = str(machine or "").strip(), str(secret or "").strip()
        if not (8 <= len(machine) <= 128) or not (16 <= len(secret) <= 128):
            raise ValueError("заявка заполнена неверно")
        name = " ".join(str(name or "").split())[:NAME_MAX] or "Новый клиент"
        version, ip = str(version or "")[:20], str(ip or "")[:64]
        shash, now = hash_token(secret), self.clock()
        with self._lock, self.db:
            row = self.db.execute("SELECT * FROM enroll WHERE machine=?", (machine,)).fetchone()
            if row is None:
                if self.db.execute("SELECT COUNT(*) FROM enroll WHERE status='pending'").fetchone()[0] >= ENROLL_PENDING_MAX:
                    raise OverflowError("слишком много заявок")
                cur = self.db.execute("INSERT INTO enroll (machine, name, version, ip, secret_hash, first_t, last_t) VALUES (?,?,?,?,?,?,?)",
                                      (machine, name, version, ip, shash, now, now))
                return {"status": "pending", "new": True, "id": cur.lastrowid, "name": name}
            if not hmac.compare_digest(row["secret_hash"], shash):              # приложение поставили заново: прежний секрет потерян
                self.db.execute("UPDATE enroll SET secret_hash=?, status='pending', token=NULL, name=?, version=?, ip=?, first_t=?, last_t=? WHERE id=?",
                                (shash, name, version, ip, now, now, row["id"]))
                return {"status": "pending", "new": True, "id": row["id"], "name": name}
            # пока заявку не решили, клиент может поправить название (поле «Завод» на странице «Мониторинг»): в панели и Telegram — новое
            shown = name if row["status"] == "pending" else row["name"]
            self.db.execute("UPDATE enroll SET version=?, ip=?, last_t=?, name=? WHERE id=?", (version, ip, now, shown, row["id"]))
            if row["status"] == "approved" and row["token"]:
                self.db.execute("UPDATE enroll SET token=NULL, status='done' WHERE id=?", (row["id"],))
                plant = self.db.execute("SELECT name FROM plants WHERE id=?", (row["plant_id"],)).fetchone()
                return {"status": "approved", "new": False, "id": row["id"], "name": plant["name"] if plant else row["name"], "token": row["token"]}
            status = "approved" if row["status"] == "done" else row["status"]
            return {"status": status, "new": False, "id": row["id"], "name": shown}

    def enroll_pending(self) -> list[dict]:
        """Заявки, которые ждут решения Макса: для панели."""
        with self._lock:
            rows = self.db.execute("SELECT * FROM enroll WHERE status='pending' ORDER BY first_t").fetchall()
        return [{"id": r["id"], "name": r["name"], "version": r["version"], "ip": r["ip"], "first_t": r["first_t"], "last_t": r["last_t"],
                 "known": self._plant_of(r["machine"]) is not None} for r in rows]

    def _plant_of(self, machine: str) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT * FROM plants WHERE machine=?", (machine,)).fetchone()

    def enroll_approve(self, req_id: int, name: Optional[str] = None) -> dict:
        """Принять заявку: завести завод (или, если этот же ПК уже заведён, выдать ему новый токен) и положить токен до следующей заявки клиента.
        LookupError — заявки нет или она уже решена; ValueError — имя пустое или занято другим заводом."""
        with self._lock:
            row = self.db.execute("SELECT * FROM enroll WHERE id=? AND status='pending'", (int(req_id),)).fetchone()
            if row is None:
                raise LookupError("Заявки нет или она уже решена")
            plant = self._plant_of(row["machine"])
            if plant is not None:                                              # тот же ПК зарегистрировался снова: название остаётся прежним
                pid, pname, token = plant["id"], plant["name"], self.rotate_token(plant["id"])
            else:
                pname = " ".join((name or row["name"]).split())
                pid, token = self.create_plant(pname, row["machine"])
            with self.db:
                self.db.execute("UPDATE enroll SET status='approved', plant_id=?, token=?, name=? WHERE id=?", (pid, token, pname, row["id"]))
        return {"id": pid, "name": pname}

    def enroll_reject(self, req_id: int) -> bool:
        with self._lock, self.db:
            return self.db.execute("UPDATE enroll SET status='rejected' WHERE id=? AND status='pending'", (int(req_id),)).rowcount > 0

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
        elif kind == "update":                  # скачана / обновлён / не удалось: шлём всегда (звук или «в историю» решает Notifier по state)
            pass
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
                                     (p["id"], p["name"], "srv-offline-rep-%d" % int(now), now, now, "high", "offline_repeat", "", "open", text)).rowcount
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
                r = open_rows[code]
                self.db.execute("UPDATE alerts SET level=?, text=? WHERE plant_id=? AND code=?", (level, text, plant_id, code))
                if r["acked_t"] is not None and LEVELS.index(level) < LEVELS.index(r["ack_level"] or "info"):      # стало хуже, чем при сбросе
                    self.db.execute("UPDATE alerts SET acked_t=NULL, ack_level=NULL WHERE plant_id=? AND code=?", (plant_id, code))
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

    def ack_alerts(self, plant_id: int, code: Optional[str] = None) -> int:
        """Сбросить открытую тревогу в панели (code=None — все открытые у завода): она уходит с карточки и из сводки, но остаётся в журнале.
        Вернётся сама, если завод перестанет её присылать и пришлёт снова, или если она станет тяжелее. Возвращает, сколько сброшено."""
        now = self.clock()
        n = 0
        with self._lock, self.db:
            sql, args = "SELECT * FROM alerts WHERE plant_id=? AND acked_t IS NULL", [plant_id]
            if code is not None:
                sql, args = sql + " AND code=?", args + [code]
            for r in self.db.execute(sql, args).fetchall():
                self.db.execute("UPDATE alerts SET acked_t=?, ack_level=? WHERE plant_id=? AND code=?", (now, r["level"], plant_id, r["code"]))
                self._insert_event(plant_id, "srv-ack-%s-%d" % (r["code"], int(now * 1000)), now, now, "alert", "info", "Сброшено: " + r["text"], r["code"], "ack")
                n += 1
        return n

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

    def _last_stop(self, plant_id: int):
        """Последнее событие «приложение закрыто», если после него приложение ещё не запускалось (иначе None)."""
        r = self.db.execute("SELECT kind, level, t FROM events WHERE plant_id=? AND kind IN ('app_stop','start') ORDER BY t DESC, id DESC LIMIT 1", (plant_id,)).fetchone()
        return r if r is not None and r["kind"] == "app_stop" else None

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
                level = "high"                                   # пропавший пульс сам по себе не критичен: сообщение, но не «красное»
                stop = self._last_stop(p["id"])
                if stop is not None:
                    if stop["level"] == "critical":              # приложение закрыли — об этом уже писали сразу, тут только запись в журнал
                        level, text = "info", "Приложение закрыто, пульса нет уже %d мин" % round((now - p["last_seen_t"]) / 60)
                    else:                                        # закрылось ради обновления и не вернулось — вот это серьёзно
                        level, text = "critical", "После обновления приложение не вернулось: пульса нет уже %d мин" % round((now - p["last_seen_t"]) / 60)
                if self._insert_event(p["id"], "srv-offline-%d" % int(p["last_seen_t"]), now, now, "offline", level, text):
                    out.append({"plant": p["name"], "plant_id": p["id"], "level": level, "kind": "offline", "text": text, "t": now})
        return out

    def purge(self) -> int:
        with self._lock, self.db:
            cutoff = self.clock() - self.retention_days * 86400
            self.db.execute("DELETE FROM positions WHERE t < ?", (cutoff,))
            self.db.execute("DELETE FROM notify WHERE t < ? AND status!='pending'", (cutoff,))
            self.db.execute("DELETE FROM enroll WHERE last_t < ?", (self.clock() - ENROLL_KEEP_DAYS * 86400,))      # давно молчащие заявки (в т.ч. непринятые)
            return self.db.execute("DELETE FROM events WHERE t < ?", (cutoff,)).rowcount

    # ------------------------------------------------------------------------------------------------------------- чтение

    @staticmethod
    def _event_row(r: sqlite3.Row) -> dict:
        return {"id": r["id"], "t": r["t"], "recv_t": r["recv_t"], "kind": r["kind"], "level": r["level"], "text": r["text"],
                "code": r["code"], "state": r["state"]}

    def events(self, plant_id: int, limit: int = 100, before_id: Optional[int] = None, errors_only: bool = False) -> list[dict]:
        """Журнал завода, новые первыми. errors_only — только ошибки: тревоги (открыты, сняты, сброшены), перезагрузки платы, «нет пульса»."""
        limit = max(1, min(int(limit), 500))
        sql, args = "SELECT * FROM events WHERE plant_id=?", [plant_id]
        if before_id:
            sql, args = sql + " AND id<?", args + [before_id]
        if errors_only:
            sql += " AND kind IN (%s)" % ",".join("?" * len(ERROR_KINDS))
            args += list(ERROR_KINDS)
        with self._lock:
            rows = self.db.execute(sql + " ORDER BY t DESC, id DESC LIMIT ?", args + [limit]).fetchall()
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
                alerts = [{"code": a["code"], "level": a["level"], "text": a["text"], "since": a["since_t"], "acked": a["acked_t"] is not None}
                          for a in self.db.execute("SELECT * FROM alerts WHERE plant_id=? ORDER BY since_t", (p["id"],))]
                try:
                    snap = json.loads(p["snapshot"]) if p["snapshot"] else None
                except ValueError:
                    snap = None
                out.append({
                    "id": p["id"], "name": p["name"], "status": self._status(p, now), "created_t": p["created_t"],
                    "last_seen_t": p["last_seen_t"], "interval_s": p["interval_s"], "offline_since": p["offline_since"],
                    "snapshot": snap, "alerts": alerts, "events": self.events(p["id"], 5), "hidden_blocks": self.hidden_blocks(p["id"]),
                    "closed_t": (lambda st: st["t"] if st is not None and st["level"] == "critical" else None)(self._last_stop(p["id"]))})
        return out
