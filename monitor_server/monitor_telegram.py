"""monitor_telegram — уведомления приёмника в Telegram: тревоги заводов приходят Максу в личный чат с ботом.

Что шлём (правила — monitor_store.Store._maybe_notify): свежие критичные и красные события — перезапуск ПК/приложения, «нет пульса»,
красные тревоги, перезагрузка платы — и «снято» / «связь восстановилась», если об открытии мы сообщали. Жёлтые предупреждения — только в панели.
Сообщения об обновлении завода (вид update: скачана, обновлён и всё поднялось, откат) приходят «в историю» — без звука; со звуком
только «обновлён, но не всё поднялось» (is_quiet). Тревоги и «в историю» уходят отдельными сообщениями, тихие потолок не едят.
Старые события (пачка после обрыва связи) в чат не идут. Несколько событий одного завода в одном проходе — одно сообщение. Не больше
max_per_hour сообщений в час: остальное одной строкой «ещё N событий, смотри панель».

Кроме уведомлений бот отвечает на вопросы: кнопки с названиями заводов (Кир, Буи, «Все заводы») — нажал, пришла текстовая сводка
по заводу: статус, диск, тревоги, датчики последней пробы, последние события — и кнопки управления тем же, что на карточке в панели:
журнал, диск, сбросить тревоги, настройки «нет пульса», переименовать, новый токен (токен стирается из чата через 5 минут), удалить
(с подтверждением); «🆕 Заявки» — кто ждёт токена. Отвечает только чатам из chat_ids; вопросы, заданные пока приёмник
не работал, не отвечаются (старое не новость).

Очередь живёт в monitor.db (таблица notify): перезапуск приёмника и обрыв Telegram ничего не теряют и не дублируют.
Настройки — data\\server.json, блок "telegram": {"token": "...", "chat_ids": [123456789]} (вне git). Токен бота в логах и ответах панели маскируется.
"""
from __future__ import annotations

import html
import json
import logging
import math
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import Callable, Optional

import monitor_release
import monitor_store

log = logging.getLogger("monitor.telegram")

API_BASE = "https://api.telegram.org"
HTTP_TIMEOUT_S = 10
RETRY_MAX_S = 1800
WATCH_EVERY_S = 30
DEFAULTS = {"fresh_s": 900, "max_per_hour": 20, "offline_repeat_s": 21600, "max_age_s": 7200, "morning": (8, 0)}
MORNING_WINDOW_S = 2 * 3600          # утреннюю сводку после этого срока уже не шлём (приёмник включили днём — это не «утро»)
MORNING_SPAN_S = 12 * 3600           # за сколько часов до отправки смотрим: 08:00 -> с 20:00 (граница смен Макса)
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
    if "morning" in t:                                    # "07:30" — другое время; false / "" / "off" — не слать утреннюю сводку
        cfg["morning"] = parse_hhmm(t["morning"])
    return cfg


def parse_hhmm(v) -> Optional[tuple]:
    """«08:00» -> (8, 0). Не время или «выключено» -> None (сводка отключена)."""
    try:
        h, m = str(v).strip().split(":")
        h, m = int(h), int(m)
    except ValueError:
        return None
    return (h, m) if 0 <= h < 24 and 0 <= m < 60 else None


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
    icon = "🟠" if r["kind"] in ("offline", "offline_repeat") and r.get("level") == "high" else "🔴"      # пропал пульс — оранжевый, не красный
    return "%s %s — %s · %s" % (icon, r["plant_name"], text, hm)


class Notifier:
    """Забирает очередь notify из хранилища и отправляет в Telegram. Свой поток; kick() — «проверь очередь сейчас» (после пульса)."""

    def __init__(self, store, client: TelegramClient, cfg: dict, clock: Callable[[], float] = time.time, releases=None):
        self.store, self.client, self.cfg, self.clock, self.releases = store, client, cfg, clock, releases
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
            try:
                self.morning()
            except Exception:
                log.exception("утренняя сводка")
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

    def morning(self) -> bool:
        """Утренняя сводка: раз в сутки после заданного времени (по умолчанию 08:00 по часам ПК приёмника), одним сообщением на все заводы.
        Не ушла (нет связи) — повторяем, пока не вышел срок MORNING_WINDOW_S. Что уже отправлена сегодня, помнит БД: перезапуск не задвоит."""
        hm = self.cfg.get("morning")
        if not hm:
            return False
        with self._lock:
            now = self.clock()
            lt = time.localtime(now)
            due = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hm[0], hm[1], 0, 0, 0, -1))
            day = time.strftime("%Y-%m-%d", lt)
            if now < due or now - due > MORNING_WINDOW_S or self.store.meta_get("morning_day") == day:
                return False
            t0 = due - MORNING_SPAN_S
            plants = self.store.plants_view()
            facts = {p["id"]: self.store.digest_facts(p["id"], t0, due) for p in plants}
            text = format_morning(plants, facts, self.store.outages(t0, due), t0, due, now, self.releases.latest if self.releases else None)
            ok, _, err = self._send_all(text)
            self.state["last_try_t"] = now
            if not ok:
                self.state["last_error"] = err
                log.warning("Telegram: утренняя сводка не ушла — %s", err)
                return False
            self.store.meta_set("morning_day", day)
            self.state.update(last_ok_t=now, last_error="", sent_messages=self.state["sent_messages"] + 1)
            return True

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
REQ_BUTTON = "🆕 Заявки"
REQ_WORDS = ("заявки", "🆕 заявки")
CANCEL_WORDS = ("отмена", "cancel")
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


def disk_of(snap: dict) -> Optional[dict]:
    """Диск из снимка завода. У клиентов 2.1.27 он лежал внутри блока камер движения; нет данных — None."""
    motion = snap.get("motion")
    d = snap.get("disk") or (motion.get("disk") if isinstance(motion, dict) else None)
    if not isinstance(d, dict):
        return None
    total, free = d.get("total_gb"), d.get("free_gb")
    nums = all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in (total, free))     # снимок пишет клиент: ему не доверяем
    return d if nums and total > 0 and 0 <= free <= total else None


def disk_line(d: dict) -> str:
    free = d["free_gb"] / d["total_gb"]
    icon = "🔴 " if free < 0.05 else "⚠ " if free < 0.1 else ""
    return "%sДиск: свободно %d из %d ГБ (%d %%)" % (icon, round(d["free_gb"]), round(d["total_gb"]), round(free * 100))


def _old_version(p: dict, latest: Optional[str]) -> str:
    """«2.0.43 → есть 2.0.50», если завод на более старой версии, иначе пусто. Версию не додумываем: нет данных — нет и пометки."""
    ver = (p.get("snapshot") or {}).get("app_version")
    return "%s, есть %s" % (ver, latest) if monitor_release.is_older(ver, latest) else ""


def format_all(plants: list, now: float, latest: Optional[str] = None) -> str:
    """Коротко по каждому заводу: статус, когда был пульс, сколько тревог, старая ли версия."""
    if not plants:
        return "Заводов пока нет."
    lines = ["Заводы на %s:" % _hm(now)]
    for p in plants:
        n = len(p.get("alerts") or [])
        old = _old_version(p, latest)
        d = disk_of(p.get("snapshot") or {})
        lines.append(_head(p, now) + (", тревог: %d" % n if n else "") + (", " + disk_line(d).replace("Диск: свободно", "диск свободно").replace(" из ", "/") if d else "") +
                     (", старая версия %s" % old if old else ""))
    return "\n".join(lines)[:MESSAGE_MAX]


def format_plant(p: dict, now: float, latest: Optional[str] = None) -> str:
    """Вся текстовая сводка по заводу: статус, строка «как дела» из снимка, тревоги с временем, датчики последней пробы, последние события."""
    lines = [_head(p, now)]
    snap = p.get("snapshot") or {}
    if p.get("status") == "waiting" or not snap:
        return "\n".join(lines)
    old = _old_version(p, latest)
    if old:
        lines.append("⚠ Старая версия приложения: %s" % old)
    body = [ln for ln in str(snap.get("text") or "").split("\n") if ln and not ln.startswith("Тревоги:")]
    if body and snap.get("plant") and body[0].startswith(str(snap["plant"]) + " · "):
        body[0] = body[0][len(str(snap["plant"])) + 3:]           # имя завода уже в заголовке
    lines += body
    d = disk_of(snap)
    if d:
        lines.append(disk_line(d))
    alerts = p.get("alerts") or []
    if alerts:
        lines.append("Тревоги:")
        lines += ["%s %s (с %s)%s" % ("🔴" if a.get("level") in ("critical", "high") else "🟡", a["text"], _hm(a["since"]),
                                     " · сброшена в панели" if a.get("acked") else "") for a in alerts]
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


def format_morning(plants: list, facts: dict, outages: list, t0: float, t1: float, now: float, latest: Optional[str] = None) -> str:
    """Утренняя сводка за окно [t0, t1] (ночь: с 20:00 до 08:00). Только факты из журнала и последнего снимка завода, без оценок.
    Где данных нет — «нет данных»; если приёмник сам не работал, об обрывах и тревогах пишем «не зафиксировано», а не «не было»."""
    day = time.strftime("%d.%m", time.localtime(t1))
    lines = ["☀ Сводка за ночь %s–%s (%s)" % (_hm(t0), _hm(t1), day)]
    for a, b in outages:
        lines.append("⚠ Приёмник не работал %s–%s: в это время обрывы связи и тревоги не фиксировались" % (_hm(a), _hm(b)))
    none = "не зафиксировано" if outages else "не было"
    if not plants:
        return "\n".join(lines + ["Заводов пока нет."])
    for p in plants:
        snap = p.get("snapshot") or {}
        head = _head(p, now)
        ver = snap.get("app_version")
        if ver:
            head += " · версия %s" % ver
            if monitor_release.is_older(ver, latest):
                head += " (есть %s)" % latest
        lines += ["", head]
        if p.get("status") == "waiting" or not snap:
            lines.append("• нет данных: завод ещё не присылал пульс")
            continue
        f = facts.get(p["id"]) or {}
        off = f.get("offline") or []
        lines.append("• Связь: обрывов %s" % ("%d (%s)" % (len(off), ", ".join(_hm(t) for t in off[:6])) if off else none))
        starts = f.get("starts") or []
        if starts:
            lines.append("• Запуски: " + "; ".join("%s %s" % (_hm(t), text) for t, text in starts[:4]))
        else:
            lines.append("• Запуски ПК и приложения: %s" % none)
        plate = f.get("plate_reboots") or []
        if plate:
            lines.append("• Перезагрузок платы: %d (%s)" % (len(plate), ", ".join(_hm(t) for t in plate[:6])))
        alerts = f.get("alerts") or []
        if alerts:
            lines.append("• Тревоги за ночь:")
            for a in alerts[:6]:
                icon = "🔴" if a["level"] in ("critical", "high") else "🟡"
                lines.append("   %s %s — открытий: %d, первое в %s" % (icon, a["text"], a["n"], _hm(a["first_t"])))
        else:
            lines.append("• Тревоги за ночь: %s" % none)
        open_now = p.get("alerts") or []
        if open_now:
            lines.append("• Сейчас открыто: " + "; ".join("%s (с %s)%s" % (a["text"], _dhm(a["since"]), ", сброшена в панели" if a.get("acked") else "") for a in open_now[:4]))
        else:
            lines.append("• Сейчас открытых тревог нет")
        probes = (snap.get("micro") or {}).get("probes_today")
        if probes is not None and p.get("last_seen_t"):
            lines.append("• Проб с 00:00 до пульса %s: %s" % (_hm(p["last_seen_t"]), probes))
    return "\n".join(lines)[:MESSAGE_MAX]


class Bot:
    """Отвечает на сообщения в Telegram: кнопки заводов → сводка по заводу с кнопками управления (то же, что на карточке в панели: журнал, диск,
    сбросить тревоги, настройки «нет пульса», переименовать, новый токен, удалить) и заявки новых клиентов. Свой поток, длинный опрос
    getUpdates (наружу порты не открываем). Отвечает только чатам из chat_ids: чужим — молчание (в журнал только номер чата).

    Опасное — через подтверждение (новый токен, удаление). Токен уходит в чат одним сообщением и стирается оттуда через TOKEN_TTL_S секунд;
    что ещё надо стереть, лежит в БД (meta), поэтому перезапуск приёмника ничего не оставит в переписке. callback_data — «действие:id завода[:параметры]»."""

    POLL_S = 25
    PENDING_S = 300                  # сколько бот ждёт название после «Переименовать»
    TOKEN_TTL_S = 300                # через сколько стирать из чата сообщение с токеном
    DELETE_KEEP_S = 47 * 3600        # Telegram не даёт стирать сообщения бота старше 48 часов: дольше не пытаемся
    DELETE_KEY = "tg_delete"         # meta: что ещё стереть из чата — [[chat_id, message_id, когда], …]
    GONE = "Такого завода уже нет."
    # действие -> метод; pid обязателен у всех, кроме xx (отмена)
    ACTIONS = {"lg": "_act_log", "dk": "_act_disk", "ak": "_act_ack", "st": "_act_settings", "sv": "_act_set", "rn": "_act_rename",
               "tk": "_act_token_ask", "tky": "_act_token", "dl": "_act_delete_ask", "dly": "_act_delete", "xx": "_act_cancel"}
    CONFIRMS = ("tky", "dly")                # «Да» под вопросом: действует CONFIRM_TTL_S и только один раз (см. _once)
    CONFIRM_TTL_S = 600
    AFTER_PRESETS = (15, 30, 60, 120, 240)                       # «нет пульса через», мин
    REPEAT_PRESETS = ((0, "не напоминать"), (60, "1 ч"), (120, "2 ч"), (360, "6 ч"), (720, "12 ч"))

    def __init__(self, store, client: TelegramClient, poll_client: TelegramClient, chat_ids: list, clock: Callable[[], float] = time.time,
                 releases=None):
        self.store, self.client, self.poll_client, self.clock, self.releases = store, client, poll_client, clock, releases
        self.allowed = {str(c) for c in chat_ids}
        self.offset: Optional[int] = None
        self.state = {"last_error": "", "answered": 0}
        self._pending: dict = {}                     # chat -> {"pid", "t"}: ждём новое название завода
        self._used: dict = {}                        # (chat, message_id) подтверждений, которые уже отработали
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def keyboard(self) -> dict:
        names = self.store.plant_names()
        rows = [[{"text": n} for n in names[i:i + 2]] for i in range(0, len(names), 2)]
        rows.append([{"text": ALL_BUTTON}, {"text": REQ_BUTTON}])
        return {"keyboard": rows, "resize_keyboard": True, "is_persistent": True}

    # ------------------------------------------------------------------------------------------------------------ отправка

    def _say(self, chat, text: str, markup: Optional[dict] = None) -> bool:
        ok, _, err = self.client.send(chat, text, markup)
        if ok:
            self.state["answered"] += 1
        else:
            self.state["last_error"] = err
            log.warning("Telegram: ответ не ушёл — %s", err)
        return ok

    def _edit(self, chat, mid, text: str, markup: Optional[dict] = None) -> None:
        """Переписать сообщение. Без markup кнопки под ним исчезают."""
        payload = {"chat_id": chat, "message_id": mid, "text": text[:4000], "disable_web_page_preview": True}
        if markup is not None:
            payload["reply_markup"] = markup
        self.client.call("editMessageText", payload)

    def _plant(self, pid: int) -> Optional[dict]:
        return next((p for p in self.store.plants_view() if p["id"] == pid), None)

    @staticmethod
    def _btn(text: str, kind: str, pid: Optional[int] = None, *extra) -> dict:
        return {"text": text, "callback_data": ":".join([kind] + ([str(pid)] if pid is not None else []) + [str(x) for x in extra])}

    def plant_markup(self, p: dict) -> dict:
        """Кнопки под сводкой завода. «Сбросить тревоги» — только если есть что сбрасывать."""
        pid, b = p["id"], self._btn
        rows = [[b("📜 Журнал", "lg", pid), b("💾 Диск", "dk", pid)]]
        if any(not a.get("acked") for a in p.get("alerts") or []):
            rows.append([b("🔕 Сбросить тревоги", "ak", pid)])
        rows += [[b("⚙ Настройки", "st", pid), b("✏ Переименовать", "rn", pid)], [b("🔑 Новый токен", "tk", pid), b("🗑 Удалить", "dl", pid)]]
        return {"inline_keyboard": rows}

    # ------------------------------------------------------------------------------------------------------------ заявки

    @staticmethod
    def _enroll_message(req_id: int, name: str, version: str, ip: str) -> tuple:
        text = "🆕 Новый клиент просит токен: «%s»\nВерсия %s · адрес %s\nПринять — токен уйдёт ему сам." % (name, version or "—", ip or "—")
        markup = {"inline_keyboard": [[{"text": "✅ Принять", "callback_data": "ea:%d" % req_id}, {"text": "✖ Отклонить", "callback_data": "er:%d" % req_id}]]}
        return text, markup

    def announce_enroll(self, req_id: int, name: str, version: str, ip: str) -> None:
        """Новая заявка клиента: сообщение со звуком и кнопками «Принять / Отклонить» во все разрешённые чаты."""
        text, markup = self._enroll_message(req_id, name, version, ip)
        for chat in sorted(self.allowed):
            ok, _, err = self.client.send(chat, text, markup)
            if not ok:
                log.warning("Telegram: заявка клиента не ушла в чат — %s", err)

    def _enroll_answer(self, kind: str, req_id: int) -> str:
        if kind == "ea":
            try:
                res = self.store.enroll_approve(req_id)
                return "✅ Принят: «%s». Токен уйдёт клиенту при его следующей заявке (до минуты)." % res["name"]
            except LookupError:
                return "Заявка уже решена или устарела."
            except ValueError as e:
                return "Не принял: %s. Прими в панели под другим названием." % e
        return "✖ Отклонено." if self.store.enroll_reject(req_id) else "Заявка уже решена или устарела."

    # ------------------------------------------------------------------------------------------------------------ кнопки

    def _callback(self, cb: dict) -> Optional[str]:
        """Нажатие инлайн-кнопки. Чужие чаты — молчание. Заявки: ea:<id> принять, er:<id> отклонить; остальное — ACTIONS."""
        msg = cb.get("message") or {}
        chat = msg.get("chat") or {}
        if str(chat.get("id")) not in self.allowed or chat.get("type") != "private":
            log.info("Telegram: кнопка из чужого чата %s проигнорирована", chat.get("id"))
            return None
        kind, _, rest = str(cb.get("data") or "").partition(":")
        mid = msg.get("message_id")
        if kind in ("ea", "er") and rest.isdigit():
            reply = self._enroll_answer(kind, int(rest))
            self.client.call("answerCallbackQuery", {"callback_query_id": cb.get("id"), "text": reply[:190]})
            old = str(msg.get("text") or "").split("\n")[0]
            self._edit(chat["id"], mid, old + "\n" + reply)
            return reply
        method = self.ACTIONS.get(kind)
        parts = rest.split(":") if rest else []
        pid = int(parts[0]) if parts and parts[0].isdigit() else None
        if method is None or (pid is None and kind != "xx"):
            return None
        date = msg.get("date")
        if kind in self.CONFIRMS and isinstance(date, (int, float)) and self.clock() - date > self.CONFIRM_TTL_S:      # «Да» под старым вопросом не действует
            reply = "Вопрос устарел — нажми кнопку заново."
            self._edit(chat["id"], mid, reply)
        else:
            try:
                reply = getattr(self, method)(chat["id"], mid, pid, parts[1:]) or ""
            except LookupError:                                    # завод удалили из панели между нажатием и действием
                reply = self.GONE
            except Exception:
                log.exception("Telegram: кнопка %s не сработала", kind)
                reply = "Не получилось, смотри панель."
        self.client.call("answerCallbackQuery", {"callback_query_id": cb.get("id"), "text": reply[:190]})
        return reply or None

    def _act_log(self, chat, mid, pid, args) -> Optional[str]:
        p = self._plant(pid)
        if p is None:
            return self.GONE
        events = self.store.events(pid, 10)
        icons = {"critical": "🔴", "high": "🔴", "medium": "🟡"}
        lines = ["📜 %s — последние события:" % p["name"]] + ["%s %s %s" % (icons.get(e["level"], "•"), _dhm(e["t"]), e["text"]) for e in events]
        self._say(chat, "\n".join(lines if events else lines + ["Записей пока нет."])[:MESSAGE_MAX])
        return None

    def _act_disk(self, chat, mid, pid, args) -> Optional[str]:
        p = self._plant(pid)
        if p is None:
            return self.GONE
        d = disk_of(p.get("snapshot") or {})
        if d is None:
            self._say(chat, "💾 %s — данных о диске нет. Их присылают клиенты версии 2.1.31 и новее." % p["name"])
        else:
            self._say(chat, "💾 %s\n%s\nЗанято %d ГБ" % (p["name"], disk_line(d), round(d["total_gb"] - d["free_gb"])))
        return None

    def _act_ack(self, chat, mid, pid, args) -> str:
        if self._plant(pid) is None:
            return self.GONE
        n = self.store.ack_alerts(pid)
        p = self._plant(pid)
        if p is not None:
            self.client.call("editMessageReplyMarkup", {"chat_id": chat, "message_id": mid, "reply_markup": self.plant_markup(p)})
        return "Сброшено тревог: %d (в журнале останутся)" % n

    # --- настройки «нет пульса»

    def _settings_view(self, p: dict) -> tuple:
        s = self.store.plant_settings(p["id"])
        custom = self.store.plant_overrides(p["id"])
        pid, b = p["id"], self._btn
        mark = lambda on, text: ("✓ " if on else "") + text
        rows = [[b(mark(s["offline_after_min"] == m, "%d мин" % m), "sv", pid, "a", m) for m in self.AFTER_PRESETS],
                [b(mark(s["offline_repeat_min"] == m, label), "sv", pid, "r", m) for m, label in self.REPEAT_PRESETS],
                [b("🔔 Сообщения: %s — %s" % ("вкл" if s["offline_notify"] else "выкл", "выключить" if s["offline_notify"] else "включить"), "sv", pid, "n",
                   0 if s["offline_notify"] else 1)]]
        if custom:
            rows.append([b("↩ Вернуть общие значения", "sv", pid, "z")])
        text = ("⚙ %s — настройки «нет пульса»%s\nНет пульса через: %d мин (сверху)\nНапоминать: %s (ниже)\nСообщения в Telegram: %s" %
                (p["name"], " (свои)" if custom else " (общие)", s["offline_after_min"],
                 "раз в %d мин" % s["offline_repeat_min"] if s["offline_repeat_min"] else "не напоминать", "да" if s["offline_notify"] else "нет"))
        return text, {"inline_keyboard": rows}

    def _act_settings(self, chat, mid, pid, args) -> Optional[str]:
        p = self._plant(pid)
        if p is None:
            return self.GONE
        text, markup = self._settings_view(p)
        self._say(chat, text, markup)
        return None

    def _act_set(self, chat, mid, pid, args) -> Optional[str]:
        if self._plant(pid) is None:
            return self.GONE
        what = args[0] if args else ""
        val = int(args[1]) if len(args) > 1 and args[1].lstrip("-").isdigit() else None
        if what == "z":
            changes = {"offline_after_min": None, "offline_repeat_min": None, "offline_notify": None}
        elif what == "a" and val is not None:
            changes = {"offline_after_min": val}
        elif what == "r" and val is not None:
            changes = {"offline_repeat_min": val}
        elif what == "n" and val in (0, 1):
            changes = {"offline_notify": bool(val)}
        else:
            return None
        try:
            self.store.set_plant_settings(pid, changes)
        except ValueError as e:
            return str(e)
        p = self._plant(pid)
        if p is None:
            return self.GONE
        text, markup = self._settings_view(p)
        self._edit(chat, mid, text, markup)
        return "Сохранено"

    # --- переименование

    def _act_rename(self, chat, mid, pid, args) -> Optional[str]:
        p = self._plant(pid)
        if p is None:
            return self.GONE
        self._pending[str(chat)] = {"pid": pid, "t": self.clock()}
        self._say(chat, "✏ Как назвать «%s»? Пришли новое название (до %d символов)." % (p["name"], monitor_store.NAME_MAX),
                  {"inline_keyboard": [[self._btn("Отмена", "xx")]]})
        return None

    def _do_rename(self, chat, pend: dict, text: str) -> str:
        p = self._plant(pend["pid"])
        if p is None:
            self._pending.pop(str(chat), None)
            return self.GONE
        try:
            name = self.store.rename_plant(pend["pid"], text)
        except ValueError as e:                                   # занято / пусто / длинное: ждём ещё одну попытку
            return "Не вышло: %s. Пришли другое название или «Отмена»." % e
        except LookupError:                                       # завод удалили из панели, пока бот ждал название
            self._pending.pop(str(chat), None)
            return self.GONE
        self._pending.pop(str(chat), None)
        return "✏ Готово: «%s» → «%s». Токен, журнал и датчики остались. Имя сменилось только в панели и в боте — на самом заводе оно прежнее." % (p["name"], name)

    def _act_cancel(self, chat, mid, pid, args) -> str:
        self._pending.pop(str(chat), None)
        self._edit(chat, mid, "Отменено.")
        return "Отменено"

    # --- новый токен: подтверждение → выдача → стирание из чата

    def _act_token_ask(self, chat, mid, pid, args) -> Optional[str]:
        p = self._plant(pid)
        if p is None:
            return self.GONE
        self._say(chat, "🔑 Новый токен для «%s»?\nСтарый сразу перестанет работать: завод будет «нет связи», пока ты не вставишь новый на его странице «Мониторинг»." % p["name"],
                  {"inline_keyboard": [[self._btn("🔑 Да, выдать", "tky", pid), self._btn("Отмена", "xx")]]})
        return None

    def _act_token(self, chat, mid, pid, args) -> str:
        p = self._plant(pid)
        if p is None:
            self._edit(chat, mid, self.GONE)
            return self.GONE
        if not self._once(chat, mid):                              # второе нажатие того же «Да» (или повторная доставка) токен не меняет
            return "Уже выдан."
        self._edit(chat, mid, "🔑 Выдаю новый токен для «%s»…" % p["name"])         # кнопки под вопросом исчезают сразу
        try:
            token = self.store.rotate_token(pid)
        except LookupError:
            self._edit(chat, mid, self.GONE)
            return self.GONE
        ok, body, _, err = self.client.call("sendMessage", {
            "chat_id": chat, "parse_mode": "HTML", "protect_content": True, "disable_web_page_preview": True,
            "text": "🔑 Токен «%s» (старый больше не работает):\n<code>%s</code>\nНажми на токен — скопируется. Сообщение сотрётся через %d мин." %
                    (html.escape(p["name"]), html.escape(token), self.TOKEN_TTL_S // 60)})
        sent = ((body or {}).get("result") or {}).get("message_id") if ok else None
        if not ok:
            self.state["last_error"] = err
            log.warning("Telegram: токен не ушёл в чат — %s", err)
            msg = "⚠ Токен «%s» сменён, но в чат не дошёл. Старый уже не работает: нажми «Новый токен» ещё раз или возьми токен в панели." % p["name"]
            self._edit(chat, mid, msg)
            return "Токен не дошёл, выдай новый"
        if sent is not None:
            self._track_delete(chat, sent, self.clock() + self.TOKEN_TTL_S)
        else:
            log.warning("Telegram: у сообщения с токеном нет номера — стереть его из чата не смогу")
        self._edit(chat, mid, "🔑 Новый токен для «%s» выдан." % p["name"])
        return "Токен выдан"

    def _once(self, chat, mid) -> bool:
        """True — это сообщение с подтверждением ещё не отрабатывали. Помним последние 200: хватает против двойного нажатия и повторной доставки."""
        key = (chat, mid)
        if key in self._used:
            return False
        self._used[key] = True
        while len(self._used) > 200:
            self._used.pop(next(iter(self._used)))
        return True

    # --- удаление завода: подтверждение → удаление

    def _act_delete_ask(self, chat, mid, pid, args) -> Optional[str]:
        p = self._plant(pid)
        if p is None:
            return self.GONE
        self._say(chat, "🗑 Удалить «%s» вместе с журналом, тревогами и датчиками? Вернуть нельзя.\nТокен завода перестанет приниматься." % p["name"],
                  {"inline_keyboard": [[self._btn("🗑 Да, удалить", "dly", pid), self._btn("Отмена", "xx")]]})
        return None

    def _act_delete(self, chat, mid, pid, args) -> str:
        p = self._plant(pid)
        if p is None or not self._once(chat, mid) or not self.store.delete_plant(pid):
            self._edit(chat, mid, self.GONE)
            return self.GONE
        self._edit(chat, mid, "🗑 Удалён: «%s»." % p["name"])
        self._say(chat, "Заводов осталось: %d." % len(self.store.plant_names()), self.keyboard())      # кнопки с названиями обновились
        return "Удалён"

    # --- стирание сообщений с токенами

    def _deletes(self) -> list:
        try:
            v = json.loads(self.store.meta_get(self.DELETE_KEY) or "[]")
        except ValueError:
            return []
        return [x for x in v if isinstance(x, list) and len(x) == 3] if isinstance(v, list) else []

    def _track_delete(self, chat, mid, due: float) -> None:
        self.store.meta_set(self.DELETE_KEY, json.dumps(self._deletes() + [[chat, mid, due]]))

    def sweep_deletes(self) -> int:
        """Стереть из чата сообщения, у которых вышел срок. Нет связи с Telegram — оставляем на следующий раз (до DELETE_KEEP_S). Возвращает, сколько стёрто."""
        now, keep, done = self.clock(), [], 0
        items = self._deletes()
        for chat, mid, due in items:
            if due > now:
                keep.append([chat, mid, due])
                continue
            ok, body, _, _ = self.client.call("deleteMessage", {"chat_id": chat, "message_id": mid})
            if ok:
                done += 1
            elif (body or {}).get("error_code") not in (400, 403) and now - due < self.DELETE_KEEP_S:       # отказ Telegram окончательный; сбой связи — повторим
                keep.append([chat, mid, due])
            else:
                log.warning("Telegram: сообщение %s не стёрто из чата (Telegram отказал или прошло больше суток) — сотри его вручную", mid)
        if keep != items:
            self.store.meta_set(self.DELETE_KEY, json.dumps(keep))
        return done

    # ------------------------------------------------------------------------------------------------------------ текст

    def handle(self, update: dict) -> Optional[str]:
        """Один входящий апдейт. Возвращает текст ответа (или None, если отвечать не надо)."""
        if update.get("callback_query"):
            return self._callback(update["callback_query"])
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
        latest = self.releases.latest if self.releases else None
        cid = str(chat["id"])
        pend = self._pending.get(cid)
        if pend and now - pend["t"] > self.PENDING_S:
            self._pending.pop(cid, None)
            pend = None
        if key in MENU_WORDS or key in ALL_WORDS or key in REQ_WORDS or key in by_name:        # нажал кнопку меню — ожидание названия снимается
            self._pending.pop(cid, None)
            pend = None
        markup, requests = self.keyboard(), []
        if pend:
            if key in CANCEL_WORDS:
                self._pending.pop(cid, None)
                reply = "Отменено."
            else:
                reply = self._do_rename(cid, pend, text)
                markup = self.keyboard()                           # название на кнопке могло измениться
        elif key in by_name:                                       # название завода важнее служебных слов: завод «Все» не должен пропасть
            reply = format_plant(by_name[key], now, latest)
            markup = self.plant_markup(by_name[key])
        elif key in MENU_WORDS:
            reply = ("Нажми кнопку с названием завода внизу — пришлю всё по нему текстом и кнопками управления. «%s» — коротко по каждому, «%s» — кто ждёт токена." %
                     (ALL_BUTTON, REQ_BUTTON))
        elif key in ALL_WORDS:
            reply = format_all(plants, now, latest)
        elif key in REQ_WORDS:
            requests = self.store.enroll_pending()
            reply = "Ждут решения: %d." % len(requests) if requests else "Заявок на токен нет."
        else:
            reply = "Не понял. Нажми кнопку с названием завода внизу."
        if self._say(chat["id"], reply, markup):
            for r in requests:
                self._say(chat["id"], *self._enroll_message(r["id"], r["name"], r["version"], r["ip"]))
        return reply

    def skip_backlog(self) -> bool:
        """Вопросы и нажатия, сделанные пока приёмник не работал, не выполняем: после простоя они уже не новость, а старое «Да, удалить» нельзя исполнять
        задним числом. True — очередь пропущена (или пуста); False — Telegram не ответил, повторить: пока не получилось, бот не опрашивается."""
        ok, body, _, err = self.poll_client.call("getUpdates", {"offset": -1, "timeout": 0, "allowed_updates": ["message", "callback_query"]})
        if not ok:
            self.state["last_error"] = err
            return False
        res = (body or {}).get("result")
        if res:
            self.offset = res[-1]["update_id"] + 1
        return True

    def poll_once(self, timeout: int = POLL_S) -> tuple:
        """Один длинный опрос. (успех, сколько секунд подождать до следующего)."""
        payload = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
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
        tries = 0
        while not self._stop.is_set():                               # без пропуска старой очереди не опрашиваем: иначе старые «Да» исполнились бы
            try:
                if self.skip_backlog():
                    break
            except Exception:
                log.exception("Telegram: не удалось пропустить старые вопросы")
            tries += 1
            if tries == 1 or tries % 20 == 0:
                log.warning("Telegram: жду связи, чтобы пропустить старые вопросы — %s", self.state["last_error"])
            self._stop.wait(min(60, 5 * tries))
        fails = 0
        while not self._stop.is_set():
            try:
                self.sweep_deletes()
            except Exception:
                log.exception("Telegram: стирание сообщений с токенами")
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
