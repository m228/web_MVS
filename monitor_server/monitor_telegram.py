"""monitor_telegram — уведомления приёмника в Telegram: тревоги заводов приходят Максу в личный чат с ботом.

Что шлём (правила — monitor_store.Store._maybe_notify): свежие критичные и красные события — перезапуск ПК/приложения, «нет пульса»,
красные тревоги, перезагрузка платы — и «снято» / «связь восстановилась», если об открытии мы сообщали. Жёлтые предупреждения — только в панели.
Сообщения об обновлении завода (вид update: скачана, обновлён и всё поднялось, откат) приходят «в историю» — без звука; со звуком
только «обновлён, но не всё поднялось» (is_quiet). Тревоги и «в историю» уходят отдельными сообщениями, тихие потолок не едят.
Старые события (пачка после обрыва связи) в чат не идут. Несколько событий одного завода в одном проходе — одно сообщение. Не больше
max_per_hour сообщений в час: остальное одной строкой «ещё N событий, смотри панель».

Кроме уведомлений бот отвечает на вопросы: кнопки с названиями заводов (Кир, Буи, «Все заводы») — нажал, пришла текстовая сводка
по заводу: статус, тревоги, датчики последней пробы, последние события. Отвечает только чатам из chat_ids; вопросы, заданные пока приёмник
не работал, не отвечаются (старое не новость).

Очередь живёт в monitor.db (таблица notify): перезапуск приёмника и обрыв Telegram ничего не теряют и не дублируют.
Настройки — data\\server.json, блок "telegram": {"token": "...", "chat_ids": [123456789]} (вне git). Токен бота в логах и ответах панели маскируется.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("monitor.telegram")

API_BASE = "https://api.telegram.org"
HTTP_TIMEOUT_S = 10
RETRY_MAX_S = 1800
WATCH_EVERY_S = 30
DEFAULTS = {"fresh_s": 900, "max_per_hour": 20, "offline_repeat_s": 7200, "max_age_s": 7200}
TEST_TEXT = "✅ web_MVS панель: Telegram подключён. Сюда придут тревоги заводов: перезапуск ПК или приложения, нет связи, красные тревоги."


def load_config(data: Path) -> Optional[dict]:
    """Настройки из data\\server.json (блок "telegram"). None — не настроено (нет токена или чата): тогда приёмник работает как раньше."""
    try:
        t = json.loads((Path(data) / "server.json").read_text(encoding="utf-8")).get("telegram")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(t, dict):
        return None
    token = str(t.get("token") or "").strip()
    ids = t.get("chat_ids")
    ids = [ids] if isinstance(ids, (int, str)) else ids
    chats = [c for c in (ids or []) if isinstance(c, (int, str)) and str(c).strip() and not isinstance(c, bool)]
    if not token or not chats:
        return None
    cfg = dict(DEFAULTS, token=token, chat_ids=chats, api_base=os.environ.get("MONITOR_TELEGRAM_API") or API_BASE)
    for key, src, mult in (("fresh_s", "fresh_min", 60), ("offline_repeat_s", "offline_repeat_min", 60)):
        if isinstance(t.get(src), (int, float)) and t[src] > 0:
            cfg[key] = int(t[src] * mult)
    if isinstance(t.get("max_per_hour"), int) and t["max_per_hour"] > 0:
        cfg["max_per_hour"] = t["max_per_hour"]
    return cfg


def mask(text: str, token: str) -> str:
    return str(text).replace(token, "***") if token else str(text)


class TelegramClient:
    """Минимальный клиент Bot API: sendMessage, getMe, getUpdates. Токен нигде не светится: ошибки возвращаются уже замаскированными."""

    def __init__(self, token: str, api_base: str = API_BASE, timeout: float = HTTP_TIMEOUT_S):
        self.token, self.api_base, self.timeout = token, api_base.rstrip("/"), timeout

    def call(self, method: str, payload: Optional[dict] = None) -> tuple:
        """(успех, ответ-словарь или None, секунд ждать до повтора, текст ошибки по-русски)."""
        url = "%s/bot%s/%s" % (self.api_base, self.token, method)
        data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json; charset=utf-8"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = json.loads(r.read().decode("utf-8"))
            return bool(body.get("ok")), body, 0.0, "" if body.get("ok") else mask(body.get("description") or "Telegram ответил отказом", self.token)
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8"))
            except Exception:
                body = {}
            wait = float((body.get("parameters") or {}).get("retry_after") or 0)
            if e.code in (401, 404):
                err = "Telegram не принял токен бота (HTTP %d)" % e.code
            elif e.code == 429:
                err = "Telegram просит подождать %d с (HTTP 429)" % wait
            elif e.code == 409:
                err = "Этого бота уже слушает другая программа (HTTP 409): закрой второй приёмник или мастер"
            elif e.code in (400, 403):
                desc = str(body.get("description") or "")
                if "chat not found" in desc.lower():
                    reason = "чат не найден — открой бота и нажми Start"
                elif "blocked" in desc.lower():
                    reason = "бот заблокирован в этом чате"
                else:
                    reason = mask(desc or "чат недоступен", self.token)
                err = "Telegram отказал: %s (HTTP %d)" % (reason, e.code)
            else:
                err = "Telegram ответил ошибкой HTTP %d" % e.code
            return False, body or None, wait, err
        except urllib.error.URLError as e:
            return False, None, 0.0, mask("Нет связи с Telegram: %s" % (e.reason,), self.token)
        except Exception as e:
            return False, None, 0.0, mask("Ошибка отправки в Telegram: %s" % e, self.token)

    def send(self, chat_id, text: str, markup: Optional[dict] = None, silent: bool = False) -> tuple:
        """silent — сообщение «в историю»: приходит в чат без звука и вибрации (disable_notification)."""
        payload = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
        if markup:
            payload["reply_markup"] = markup
        if silent:
            payload["disable_notification"] = True
        ok, _, wait, err = self.call("sendMessage", payload)
        return ok, wait, err


# события обновления (kind "update", state от завода): что в Telegram приходит без звука «в историю», а что со звуком.
# Тихо: скачана, обновлён и всё поднялось, откат (о нём и так кричит красная тревога «приложение перезапущено»).
# Со звуком — только «обновлён, но что-то не поднялось»: другой тревоги об этом нет, а камера/плата могут не работать.
UPDATE_ICON = {"staged": "📦", "ok": "✅", "warn": "⚠️", "failed": "↩️"}
QUIET_UPDATE_STATES = ("staged", "ok", "failed")


def is_quiet(r: dict) -> bool:
    return r.get("kind") == "update" and r.get("state") in QUIET_UPDATE_STATES


def format_line(r: dict) -> str:
    """Одна строка сообщения: «🔴 Кир — текст · 19:40». Снятие и восстановление — зелёные, обновление — свой значок."""
    hm = time.strftime("%H:%M", time.localtime(r["t"]))
    text = r["text"].strip()
    if r["kind"] == "update":
        return "%s %s — %s · %s" % (UPDATE_ICON.get(r.get("state"), "🔄"), r["plant_name"], text, hm)
    if r["kind"] == "online":
        return "🟢 %s — %s · %s" % (r["plant_name"], text[:1].lower() + text[1:], hm)
    if r["state"] == "closed":
        text = text[len("Снято: "):] if text.startswith("Снято: ") else text
        return "🟢 %s — снято: %s · %s" % (r["plant_name"], text, hm)
    return "🔴 %s — %s · %s" % (r["plant_name"], text, hm)


class Notifier:
    """Забирает очередь notify из хранилища и отправляет в Telegram. Свой поток; kick() — «проверь очередь сейчас» (после пульса)."""

    def __init__(self, store, client: TelegramClient, cfg: dict, clock: Callable[[], float] = time.time):
        self.store, self.client, self.cfg, self.clock = store, client, cfg, clock
        self.state = {"last_ok_t": None, "last_error": "", "last_try_t": None, "sent_messages": 0}
        self._sent_times: deque = deque()       # когда ушли последние сообщения (потолок max_per_hour)
        self._summary_t = 0.0
        self._kick = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def kick(self) -> None:
        self._kick.set()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="monitor-telegram", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._kick.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.flush()
            except Exception:
                log.exception("отправка в Telegram")
            self._kick.wait(WATCH_EVERY_S)
            self._kick.clear()

    def _send_all(self, text: str, silent: bool = False) -> tuple:
        wait, err = 0.0, ""
        for chat in self.cfg["chat_ids"]:
            ok, w, e = self.client.send(chat, text, silent=silent)
            if not ok:
                return False, max(wait, w), e
        return True, 0.0, ""

    def _recent(self, now: float) -> int:
        while self._sent_times and now - self._sent_times[0] > 3600:
            self._sent_times.popleft()
        return len(self._sent_times)

    def flush(self) -> int:
        """Один проход: напоминания «нет связи», затем отправка очереди. Возвращает, сколько сообщений ушло."""
        with self._lock:
            now = self.clock()
            self.store.notify_offline_reminders(now, self.cfg["offline_repeat_s"])
            rows = self.store.notify_pending(now, 100)
            if not rows:
                return 0
            old = [r["id"] for r in rows if now - r["t"] > self.cfg["max_age_s"]]
            if old:                                              # Telegram долго не отвечал — давнее уже не новость
                self.store.notify_mark(old, "muted", now, "устарело")
                rows = [r for r in rows if r["id"] not in old]
            by_plant: dict = {}
            for r in rows:
                by_plant.setdefault(r["plant_id"], []).append(r)
            sent, muted = 0, 0
            groups = []                                          # (строки, тихо?): у завода тревоги и «история» уходят отдельными сообщениями
            for plant_rows in by_plant.values():
                loud = [r for r in plant_rows if not is_quiet(r)]
                quiet = [r for r in plant_rows if is_quiet(r)]
                groups += [(g, silent) for g, silent in ((loud, False), (quiet, True)) if g]
            for group_rows, silent in groups:
                ids = [r["id"] for r in group_rows]
                if not silent and self._recent(now) >= self.cfg["max_per_hour"]:      # тихие потолок не едят: бюджет — для тревог
                    self.store.notify_mark(ids, "muted", now, "потолок сообщений в час")
                    muted += len(ids)
                    continue
                ok, wait, err = self._send_all("\n".join(format_line(r) for r in group_rows), silent)
                self.state["last_try_t"] = now
                if ok:
                    self.store.notify_mark(ids, "sent", now)
                    if not silent:
                        self._sent_times.append(now)
                    self.state.update(last_ok_t=now, last_error="", sent_messages=self.state["sent_messages"] + 1)
                    sent += 1
                else:
                    tries = max(r["tries"] for r in group_rows) + 1
                    delay = max(wait, min(RETRY_MAX_S, 30 * 2 ** (tries - 1)))
                    self.store.notify_retry(ids, now + delay, err)
                    self.state["last_error"] = err
                    log.warning("Telegram: %s (повтор через %d с)", err, delay)
                    break                                        # связи нет — остальные заводы тоже не уйдут, не долбим
            if muted and now - self._summary_t > 3600:
                self._summary_t = now
                ok, _, err = self._send_all("⚠ Слишком много событий за час: ещё %d не отправлено. Смотри панель." % muted)
                if ok:
                    sent += 1
            return sent

    def send_test(self) -> tuple:
        ok, _, err = self._send_all(TEST_TEXT)
        if ok:
            self.state.update(last_ok_t=self.clock(), last_error="")
        else:
            self.state["last_error"] = err
        return ok, err

    def public_state(self) -> dict:
        """Для панели: без токена и без chat id."""
        return {"configured": True, "ok": not self.state["last_error"], "last_ok_t": self.state["last_ok_t"],
                "last_error": self.state["last_error"], "pending": self.store.notify_count()}


# ------------------------------------------------------------------------------------------------------- вопросы к боту: сводка по заводу

STATUS_RU = {"online": "в сети", "offline": "нет связи", "waiting": "ещё не выходил на связь", "checking": "проверяю связь"}
STATUS_ICON = {"online": "🟢", "offline": "🔴", "waiting": "⚪", "checking": "🟡"}
ALL_BUTTON = "Все заводы"
MENU_WORDS = ("start", "help", "menu", "заводы", "меню", "помощь")
ALL_WORDS = ("all", "status", "все", "всё", "все заводы")
MESSAGE_MAX = 3800


def _hm(t: float) -> str:
    return time.strftime("%H:%M", time.localtime(t))


def _dhm(t: float) -> str:
    return time.strftime("%d.%m %H:%M", time.localtime(t))


def _ago(sec: float) -> str:
    sec = max(0, sec)
    if sec < 90:
        return "только что"
    if sec < 5400:
        return "%d мин назад" % round(sec / 60)
    if sec < 172800:
        return "%d ч назад" % round(sec / 3600)
    return "%d д назад" % round(sec / 86400)


def _head(p: dict, now: float) -> str:
    st = p.get("status") or "waiting"
    head = "%s %s — %s" % (STATUS_ICON.get(st, "⚪"), p["name"], STATUS_RU.get(st, st))
    if p.get("last_seen_t"):
        head += ", пульс " + _ago(now - p["last_seen_t"])
    return head


def format_all(plants: list, now: float) -> str:
    """Коротко по каждому заводу: статус, когда был пульс, сколько тревог."""
    if not plants:
        return "Заводов пока нет."
    lines = ["Заводы на %s:" % _hm(now)]
    for p in plants:
        n = len(p.get("alerts") or [])
        lines.append(_head(p, now) + (", тревог: %d" % n if n else ""))
    return "\n".join(lines)[:MESSAGE_MAX]


def format_plant(p: dict, now: float) -> str:
    """Вся текстовая сводка по заводу: статус, строка «как дела» из снимка, тревоги с временем, датчики последней пробы, последние события."""
    lines = [_head(p, now)]
    snap = p.get("snapshot") or {}
    if p.get("status") == "waiting" or not snap:
        return "\n".join(lines)
    body = [ln for ln in str(snap.get("text") or "").split("\n") if ln and not ln.startswith("Тревоги:")]
    if body and snap.get("plant") and body[0].startswith(str(snap["plant"]) + " · "):
        body[0] = body[0][len(str(snap["plant"])) + 3:]           # имя завода уже в заголовке
    lines += body
    alerts = p.get("alerts") or []
    if alerts:
        lines.append("Тревоги:")
        lines += ["%s %s (с %s)" % ("🔴" if a.get("level") in ("critical", "high") else "🟡", a["text"], _hm(a["since"])) for a in alerts]
    else:
        lines.append("Тревог нет")
    pos = ((snap.get("micro") or {}).get("positions")) or {}
    shown = [(name, pos[key]) for key, name in (("retract", "Отвод"), ("approach", "Подвод")) if pos.get(key)]
    if shown:
        lines.append("Датчики последней пробы, мкм:")
        for name, r in shown:
            vals = " · ".join("%s %s" % (lbl, round(r[k])) for lbl, k in (("расчётная", "calc"), ("энкодер", "enc"), ("датчик", "ai")) if r.get(k) is not None)
            lines.append("%s%s (%s): %s%s" % ("" if r.get("ok") else "⚠ ", name, _hm(r["t"]) if r.get("t") else "—", vals,
                                              ", разница %s" % round(r["gap"]) if r.get("gap") is not None else ""))
    events = p.get("events") or []
    if events:
        lines.append("Последние события:")
        lines += ["• %s %s" % (_dhm(e["t"]), e["text"]) for e in events]
    return "\n".join(lines)[:MESSAGE_MAX]


class Bot:
    """Отвечает на сообщения в Telegram: кнопки заводов → сводка по заводу. Свой поток, длинный опрос getUpdates (наружу порты не открываем).
    Отвечает только чатам из chat_ids: чужим — молчание (в журнал только номер чата)."""

    POLL_S = 25

    def __init__(self, store, client: TelegramClient, poll_client: TelegramClient, chat_ids: list, clock: Callable[[], float] = time.time):
        self.store, self.client, self.poll_client, self.clock = store, client, poll_client, clock
        self.allowed = {str(c) for c in chat_ids}
        self.offset: Optional[int] = None
        self.state = {"last_error": "", "answered": 0}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def keyboard(self) -> dict:
        names = self.store.plant_names()
        rows = [[{"text": n} for n in names[i:i + 2]] for i in range(0, len(names), 2)]
        rows.append([{"text": ALL_BUTTON}])
        return {"keyboard": rows, "resize_keyboard": True, "is_persistent": True}

    def handle(self, update: dict) -> Optional[str]:
        """Один входящий апдейт. Возвращает текст ответа (или None, если отвечать не надо)."""
        m = update.get("message") or {}
        chat = m.get("chat") or {}
        if chat.get("id") is None:
            return None
        if str(chat["id"]) not in self.allowed or chat.get("type") != "private":
            log.info("Telegram: сообщение из чужого чата %s проигнорировано", chat["id"])
            return None
        text = " ".join(str(m.get("text") or "").split())
        if not text:
            return None
        key = text.lstrip("/").split("@")[0].strip().lower()
        plants = self.store.plants_view()
        by_name = {p["name"].lower(): p for p in plants}
        now = self.clock()
        if key in MENU_WORDS:
            reply = "Нажми кнопку с названием завода внизу — пришлю всё по нему текстом. «%s» — коротко по каждому." % ALL_BUTTON
        elif key in ALL_WORDS:
            reply = format_all(plants, now)
        elif key in by_name:
            reply = format_plant(by_name[key], now)
        else:
            reply = "Не понял. Нажми кнопку с названием завода внизу."
        ok, _, err = self.client.send(chat["id"], reply, self.keyboard())
        if ok:
            self.state["answered"] += 1
        else:
            self.state["last_error"] = err
            log.warning("Telegram: ответ не ушёл — %s", err)
        return reply

    def skip_backlog(self) -> None:
        """Вопросы, заданные пока приёмник не работал, не отвечаем: после простоя они уже не новость и ответ придёт не вовремя."""
        ok, body, _, _ = self.poll_client.call("getUpdates", {"offset": -1, "timeout": 0, "allowed_updates": ["message"]})
        res = (body or {}).get("result") if ok else None
        if res:
            self.offset = res[-1]["update_id"] + 1

    def poll_once(self, timeout: int = POLL_S) -> tuple:
        """Один длинный опрос. (успех, сколько секунд подождать до следующего)."""
        payload = {"timeout": timeout, "allowed_updates": ["message"]}
        if self.offset is not None:
            payload["offset"] = self.offset
        ok, body, wait, err = self.poll_client.call("getUpdates", payload)
        if not ok:
            self.state["last_error"] = err
            return False, wait
        self.state["last_error"] = ""
        for u in (body or {}).get("result", []):
            self.offset = u["update_id"] + 1
            try:
                self.handle(u)
            except Exception:
                log.exception("Telegram: ошибка при ответе боту")
        return True, 0.0

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="monitor-telegram-bot", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        try:
            self.skip_backlog()
        except Exception:
            log.exception("Telegram: не удалось пропустить старые вопросы")
        fails = 0
        while not self._stop.is_set():
            try:
                ok, wait = self.poll_once()
            except Exception:
                log.exception("Telegram: опрос бота")
                ok, wait = False, 0.0
            if ok:
                fails = 0
                continue
            fails += 1
            if fails == 1 or fails % 20 == 0:
                log.warning("Telegram: бот не опрашивается — %s", self.state["last_error"])
            self._stop.wait(max(wait, min(60, 2 ** min(fails, 6))))
