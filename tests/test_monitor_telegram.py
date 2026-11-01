"""Уведомления приёмника в Telegram: что шлём (и чего нет), очередь без потерь и дублей, повторы, потолок, токен нигде не светится,
мастер подключения. Telegram — фейковый локальный сервер."""
import base64
import getpass
import importlib.util
import json
import logging
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MS = ROOT / "monitor_server"
sys.path.insert(0, str(MS))

import monitor_app  # noqa: E402
import monitor_store as ms  # noqa: E402
import monitor_telegram as mt  # noqa: E402

_spec = importlib.util.spec_from_file_location("monitor_telegram_setup_entry", MS / "telegram_setup.py")
setup_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup_mod)
_spec2 = importlib.util.spec_from_file_location("monitor_server_tg_entry", MS / "server.py")
entry = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(entry)

TOKEN = "1234567890:AAFAKE-test_token_for_unit_tests_only"
CHAT = 111222333
PANEL = (MS / "panel.html").read_text(encoding="utf-8")
PASSWORD = "p4ssw0rd-Test"


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeTelegram:
    """Фейковый Bot API: пишет запросы, отвечает по режиму (ok / 500 / 429 / 401 / 400)."""

    def __init__(self):
        self.mode = "ok"
        self.retry_after = 0
        self.requests = []
        self.updates = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8") or "{}")
                parts = self.path.strip("/").split("/")
                method = parts[-1]
                outer.requests.append({"path": self.path, "method": method, "body": body})
                code, resp = 200, {"ok": True, "result": {}}
                if outer.mode == "500":
                    code, resp = 500, {"ok": False}
                elif outer.mode == "429":
                    code, resp = 429, {"ok": False, "parameters": {"retry_after": outer.retry_after}}
                elif outer.mode == "401":
                    code, resp = 401, {"ok": False, "description": "Unauthorized " + parts[0]}
                elif outer.mode == "400":
                    code, resp = 400, {"ok": False, "description": "Bad Request: chat not found"}
                elif method == "getMe":
                    resp = {"ok": True, "result": {"username": "monitor_test_bot", "is_bot": True}}
                elif method == "getUpdates":
                    resp = {"ok": True, "result": outer.updates}
                outer.requests[-1]["code"] = code
                data = json.dumps(resp).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def sent(self):
        return [r["body"]["text"] for r in self.requests if r["method"] == "sendMessage" and r.get("code") == 200]   # только принятые

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def tg():
    t = FakeTelegram()
    yield t
    t.close()


@pytest.fixture
def env(tmp_path, tg):
    clk = Clock()
    store = ms.Store(tmp_path / "monitor.db", clock=clk, notify={"fresh_s": 900})
    cfg = dict(mt.DEFAULTS, token=TOKEN, chat_ids=[CHAT], api_base=tg.url)
    notifier = mt.Notifier(store, mt.TelegramClient(TOKEN, tg.url, timeout=5), cfg, clock=clk)

    class E:
        pass
    e = E()
    e.store, e.clk, e.cfg, e.n, e.tg = store, clk, cfg, notifier, tg
    yield e
    store.close()


def pulse(events=None, alerts=None):
    return {"schema": 1, "sent_t": 0, "snapshot": {"plant": "Кир", "app_version": "2.0.46", "interval_s": 300, "text": "x", "alerts": alerts or []},
            "events": events or []}


def ev(eid, t, text, kind="alert", level="high", **data):
    return {"eid": eid, "t": t, "kind": kind, "level": level, "text": text, "data": data}


def rows(e):
    return [(r["kind"], r["state"], r["code"]) for r in e.store.notify_pending(e.clk.t + 10**6)]


# --------------------------------------------------------------------------------------------------------------------- правила

def test_only_fresh_critical_and_red_events_are_queued(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    t = e.clk.t
    e.store.ingest(pid, pulse([
        ev("s1", t - 30, "ПК перезагрузился", kind="start", level="critical"),
        ev("a1", t - 20, "Камера: экспозиция 5000 вместо 170", code="camera_exposure", state="open"),
        ev("m1", t - 10, "Подвод: датчики разошлись", level="medium", code="pos_approach", state="open"),
        ev("u1", t - 9, "Обновление до 2.0.46", kind="start", level="info"),
        ev("p1", t - 8, "Плата микроскопа перезагрузилась", kind="plate_reboot", level="high"),
        ev("old", t - 3600, "Давний старт", kind="start", level="critical"),
    ]))
    assert rows(e) == [("start", "", ""), ("alert", "open", "camera_exposure"), ("plate_reboot", "", "")]


def test_closed_and_online_only_if_the_opening_was_notified(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    t = e.clk.t
    e.store.ingest(pid, pulse([ev("o1", t - 50, "Камера: экспозиция 5000", code="camera_exposure", state="open"),
                               ev("c1", t - 40, "Снято: Камера: экспозиция 5000", level="info", code="camera_exposure", state="closed"),
                               ev("c2", t - 39, "Снято: Что-то, о чём не писали", level="info", code="other", state="closed")]))
    assert rows(e) == [("alert", "open", "camera_exposure"), ("alert", "closed", "camera_exposure")]


def test_yellow_to_red_escalation_notifies_only_the_red_and_its_clearing(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    t = e.clk.t
    e.store.ingest(pid, pulse([ev("y1", t - 60, "Подвод: датчики разошлись", level="medium", code="pos_approach", state="open")]))
    assert rows(e) == [], "жёлтое — только в панели"
    e.store.ingest(pid, pulse([ev("r1", t - 30, "Подвод: датчики разошлись · проб подряд: 2", level="high", code="pos_approach", state="open")]))
    assert rows(e) == [("alert", "open", "pos_approach")]
    e.store.ingest(pid, pulse([ev("c1", t - 10, "Снято: Подвод", level="info", code="pos_approach", state="closed")]))
    assert rows(e) == [("alert", "open", "pos_approach"), ("alert", "closed", "pos_approach")]


def test_offline_then_online_and_no_online_for_plain_reconnect(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    e.store.ingest(pid, pulse())
    e.clk.t += 13 * 60
    assert len(e.store.check_offline()) == 1
    e.clk.t += 60
    e.store.ingest(pid, pulse())                                   # вернулся пульс -> событие «связь восстановилась»
    assert rows(e) == [("offline", "", ""), ("online", "", "")]
    e.store.ingest(pid, pulse())
    assert len(rows(e)) == 2, "обычные пульсы больше ничего не добавляют"


def test_no_queue_when_notifications_are_not_configured(tmp_path):
    s = ms.Store(tmp_path / "m.db", clock=Clock())
    pid, _ = s.create_plant("Кир")
    s.ingest(pid, pulse([ev("s1", s.clock() - 5, "ПК перезагрузился", kind="start", level="critical")]))
    assert s.notify_count() == 0, "без настроек Telegram очередь не копится (иначе при подключении хлынут старые события)"
    s.close()


# ------------------------------------------------------------------------------------------------------------------ отправка

def test_events_of_one_plant_go_in_one_message_and_plants_in_separate(env):
    e = env
    a, _ = e.store.create_plant("Кир")
    b, _ = e.store.create_plant("Буи")
    t = e.clk.t
    e.store.ingest(a, pulse([ev("s1", t - 30, "ПК перезагрузился (не работал 12 мин)", kind="start", level="critical"),
                             ev("a1", t - 20, "Камера: экспозиция 5000", code="camera_exposure", state="open")]))
    e.store.ingest(b, pulse([ev("s2", t - 10, "Приложение перезапущено", kind="start", level="critical")]))
    assert e.n.flush() == 2
    msgs = e.tg.sent()
    assert len(msgs) == 2 and msgs[0].count("\n") == 1 and msgs[0].startswith("🔴 Кир — ПК перезагрузился (не работал 12 мин) · ")
    assert msgs[1].startswith("🔴 Буи — Приложение перезапущено · ")
    assert all(r["body"]["chat_id"] == CHAT for r in e.tg.requests)
    assert e.n.flush() == 0 and len(e.tg.sent()) == 2, "отправленное повторно не уходит"


def test_message_texts_for_closed_and_online(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    t = e.clk.t
    e.store.ingest(pid, pulse([ev("o1", t - 50, "Камера: экспозиция 5000", code="camera_exposure", state="open")]))
    e.n.flush()
    e.store.ingest(pid, pulse([ev("c1", t - 40, "Снято: Камера: экспозиция 5000", level="info", code="camera_exposure", state="closed")]))
    e.n.flush()
    assert e.tg.sent()[1].startswith("🟢 Кир — снято: Камера: экспозиция 5000 · ")
    e.clk.t += 13 * 60
    e.store.check_offline()
    e.n.flush()
    e.clk.t += 60
    e.store.ingest(pid, pulse())
    e.n.flush()
    sent = e.tg.sent()
    assert sent[2].startswith("🔴 Кир — Нет пульса уже 13 мин") and sent[3].startswith("🟢 Кир — связь восстановилась")


def test_queue_survives_restart_without_duplicates(env, tmp_path):
    e = env
    pid, _ = e.store.create_plant("Кир")
    e.store.ingest(pid, pulse([ev("s1", e.clk.t - 5, "ПК перезагрузился", kind="start", level="critical")]))
    e.tg.mode = "500"
    assert e.n.flush() == 0 and e.store.notify_count() == 1, "Telegram недоступен — событие ждёт"
    e.store.close()
    store2 = ms.Store(tmp_path / "monitor.db", clock=e.clk, notify={"fresh_s": 900})        # «перезапуск приёмника»
    n2 = mt.Notifier(store2, mt.TelegramClient(TOKEN, e.tg.url, timeout=5), e.cfg, clock=e.clk)
    e.tg.mode = "ok"
    e.clk.t += 3600 * 0.4                                                                   # пауза повтора прошла
    assert n2.flush() == 1 and n2.flush() == 0
    assert len(e.tg.sent()) == 1, "после перезапуска ушло ровно одно сообщение"
    store2.close()
    e.store = ms.Store(tmp_path / "monitor.db", clock=e.clk)                                # для закрытия в фикстуре


def test_failed_send_is_retried_later_with_growing_pause(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    e.store.ingest(pid, pulse([ev("s1", e.clk.t - 5, "ПК перезагрузился", kind="start", level="critical")]))
    e.tg.mode = "500"
    e.n.flush()
    r = e.store.notify_pending(e.clk.t + 10**6)[0]
    assert r["tries"] == 1 and r["next_try_t"] >= e.clk.t + 30 and "HTTP 500" in r["reason"]
    assert e.n.flush() == 0 and len(e.tg.requests) == 1, "до срока повтора не долбим"
    e.clk.t += 31
    e.n.flush()
    assert e.store.notify_pending(e.clk.t + 10**6)[0]["next_try_t"] >= e.clk.t + 60, "пауза растёт"
    e.tg.mode = "ok"
    e.clk.t += 61
    assert e.n.flush() == 1 and len(e.tg.sent()) == 1


def test_telegram_429_retry_after_is_respected(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    e.store.ingest(pid, pulse([ev("s1", e.clk.t - 5, "ПК перезагрузился", kind="start", level="critical")]))
    e.tg.mode, e.tg.retry_after = "429", 500
    e.n.flush()
    assert e.store.notify_pending(e.clk.t + 10**6)[0]["next_try_t"] >= e.clk.t + 500


def test_hourly_cap_mutes_the_rest_and_sends_one_summary(env):
    e = env
    e.cfg["max_per_hour"] = 3
    for i in range(5):
        pid, _ = e.store.create_plant("Завод%d" % i)
        e.store.ingest(pid, pulse([ev("s%d" % i, e.clk.t - 5, "ПК перезагрузился", kind="start", level="critical")]))
    assert e.n.flush() == 4
    sent = e.tg.sent()
    assert len(sent) == 4 and sent[3].startswith("⚠ Слишком много событий за час: ещё 2")
    assert e.store.notify_count() == 0
    e.clk.t += 60
    assert e.n.flush() == 0


def test_events_pending_too_long_are_dropped_not_sent(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    e.store.ingest(pid, pulse([ev("s1", e.clk.t - 5, "ПК перезагрузился", kind="start", level="critical")]))
    e.tg.mode = "500"
    e.n.flush()
    e.tg.mode = "ok"
    e.clk.t += e.cfg["max_age_s"] + 100
    assert e.n.flush() == 0 and not e.tg.sent(), "через два часа это уже не новость"
    assert e.store.notify_count() == 0


def test_offline_reminder_every_two_hours_only_after_first_message(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    e.store.ingest(pid, pulse())
    e.clk.t += 13 * 60
    e.store.check_offline()
    e.n.flush()
    assert len(e.tg.sent()) == 1
    e.clk.t += 3600
    e.n.flush()
    assert len(e.tg.sent()) == 1, "через час напоминать рано"
    e.clk.t += 3700
    e.n.flush()
    assert len(e.tg.sent()) == 2 and "Всё ещё нет пульса" in e.tg.sent()[1]
    e.n.flush()
    assert len(e.tg.sent()) == 2, "не чаще раза в два часа"


def test_token_is_masked_everywhere(env, caplog):
    e = env
    pid, _ = e.store.create_plant("Кир")
    e.store.ingest(pid, pulse([ev("s1", e.clk.t - 5, "ПК перезагрузился", kind="start", level="critical")]))
    e.tg.mode = "401"
    with caplog.at_level(logging.DEBUG):
        e.n.flush()
        ok, err = e.n.send_test()
    assert not ok and "не принял токен бота (HTTP 401)" in err and TOKEN not in err
    state = json.dumps(e.n.public_state(), ensure_ascii=False)
    assert TOKEN not in state and str(CHAT) not in state
    assert TOKEN not in caplog.text and TOKEN not in json.dumps(e.store.notify_pending(e.clk.t + 10**6), ensure_ascii=False)
    # сеть недоступна: ошибка тоже без токена (адрес с токеном есть в тексте исключения urllib)
    bad = mt.TelegramClient(TOKEN, "http://127.0.0.1:1", timeout=2)
    ok, _, _, err = bad.call("getMe")
    assert not ok and TOKEN not in err


def test_chat_not_found_message(env):
    e = env
    e.tg.mode = "400"
    ok, err = e.n.send_test()
    assert not ok and "чат не найден" in err.lower()


# ------------------------------------------------------------------------------------------------------------------- настройки

def test_load_config_variants(tmp_path):
    p = tmp_path / "server.json"
    assert mt.load_config(tmp_path) is None
    p.write_text(json.dumps({"password": "x"}), encoding="utf-8")
    assert mt.load_config(tmp_path) is None, "блока нет — Telegram выключен"
    p.write_text(json.dumps({"telegram": {"token": TOKEN}}), encoding="utf-8")
    assert mt.load_config(tmp_path) is None, "нет чата — слать некуда"
    p.write_text(json.dumps({"telegram": {"token": " " + TOKEN + " ", "chat_ids": [CHAT, "222"], "fresh_min": 5, "max_per_hour": 7}}), encoding="utf-8")
    cfg = mt.load_config(tmp_path)
    assert cfg["token"] == TOKEN and cfg["chat_ids"] == [CHAT, "222"] and cfg["fresh_s"] == 300 and cfg["max_per_hour"] == 7
    assert cfg["offline_repeat_s"] == 7200 and cfg["api_base"] == mt.API_BASE
    p.write_text("{сломано", encoding="utf-8")
    assert mt.load_config(tmp_path) is None


# --------------------------------------------------------------------------------------------------------- HTTP-слой приёмника

class Srv:
    def __init__(self, store, notifier, app=None):
        import uvicorn
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.base = "http://127.0.0.1:%d" % port
        app = app or monitor_app.create_app(store, PASSWORD, "9.9.9", PANEL, watchdog=False, notifier=notifier)
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.05)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method, path, body=None, headers=None, password=PASSWORD, bearer=None):
        h = dict(headers or {})
        if password is not None:
            h["Authorization"] = "Basic " + base64.b64encode(("max:" + password).encode()).decode()
        if bearer is not None:
            h["Authorization"] = "Bearer " + bearer
        data = json.dumps(body).encode() if body is not None else None
        if data is not None:
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with self.opener.open(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as ex:
            raw = ex.read()
            return ex.code, (json.loads(raw) if raw[:1] in (b"{", b"[") else None)

    def close(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)


def test_api_state_and_test_message(env):
    srv = Srv(env.store, env.n)
    try:
        assert srv.call("GET", "/api/telegram", password=None)[0] == 401
        code, j = srv.call("GET", "/api/telegram")
        assert code == 200 and j["configured"] is True and j["ok"] is True and j["pending"] == 0
        assert TOKEN not in json.dumps(j)
        assert srv.call("POST", "/api/telegram/test")[0] == 403, "запись только из панели (X-Panel)"
        code, j = srv.call("POST", "/api/telegram/test", headers={"X-Panel": "1"})
        assert code == 200 and j == {"ok": True, "error": ""} and env.tg.sent()[-1].startswith("✅ web_MVS панель: Telegram подключён")
        env.tg.mode = "401"
        code, j = srv.call("POST", "/api/telegram/test", headers={"X-Panel": "1"})
        assert j["ok"] is False and "токен" in j["error"] and TOKEN not in json.dumps(j)
        assert srv.call("GET", "/api/telegram")[1]["ok"] is False, "после ошибки панель покажет «нет связи»"
    finally:
        srv.close()


def test_api_without_notifier_says_not_configured(tmp_path):
    store = ms.Store(tmp_path / "m.db", clock=Clock())
    srv = Srv(store, None)
    try:
        assert srv.call("GET", "/api/telegram")[1] == {"configured": False}
        code, j = srv.call("POST", "/api/telegram/test", headers={"X-Panel": "1"})
        assert code == 400 and "telegram_setup.bat" in j["error"]
    finally:
        srv.close()
        store.close()


def test_pulse_with_red_alert_reaches_telegram_through_real_ingest(env):
    srv = Srv(env.store, env.n)
    try:
        pid, token = env.store.create_plant("Кир")
        t = time.time()
        env.clk.t = t
        body = pulse([ev("s1", t - 2, "ПК перезагрузился (не работал 12 мин)", kind="start", level="critical")])
        code, j = srv.call("POST", "/ingest", body, bearer=token, password=None)
        assert code == 200, j
        assert env.n.flush() == 1
        assert env.tg.sent()[0].startswith("🔴 Кир — ПК перезагрузился (не работал 12 мин)")
    finally:
        srv.close()


def test_build_app_enables_notifier_only_when_configured(tmp_path, monkeypatch, tg):
    monkeypatch.setenv("MONITOR_DATA", str(tmp_path))
    monkeypatch.setenv("MONITOR_PASSWORD", PASSWORD)
    monkeypatch.setenv("MONITOR_TELEGRAM_API", tg.url)
    srv = Srv(None, None, app=entry.build_app())
    try:
        assert srv.call("GET", "/api/telegram")[1] == {"configured": False}, "блока telegram нет — приёмник работает как раньше"
    finally:
        srv.close()
    (tmp_path / "server.json").write_text(json.dumps({"telegram": {"token": TOKEN, "chat_ids": [CHAT]}}), encoding="utf-8")
    srv = Srv(None, None, app=entry.build_app())
    try:
        code, j = srv.call("GET", "/api/telegram")
        assert code == 200 and j["configured"] is True and TOKEN not in json.dumps(j)
        assert srv.call("POST", "/api/telegram/test", headers={"X-Panel": "1"})[1]["ok"] is True
        assert tg.sent() == [mt.TEST_TEXT], "тест из панели дошёл до Telegram через настоящую сборку приложения"
    finally:
        srv.close()


# ------------------------------------------------------------------------------------------------------------ мастер подключения

def updates(*items):
    return {"result": [{"message": {"chat": c}} for c in items]}


def test_private_chats_ignores_groups_and_channels():
    got = setup_mod.private_chats(updates({"id": 5, "type": "private", "first_name": "Макс", "username": "mak"},
                                          {"id": -100, "type": "group", "title": "Цех"},
                                          {"id": -200, "type": "channel"},
                                          {"id": 5, "type": "private", "first_name": "Макс"}))
    assert got == {5: "Макс (@mak)"}
    assert setup_mod.private_chats({}) == {} and setup_mod.private_chats(None) == {}


def test_save_telegram_keeps_the_rest_of_server_json(tmp_path):
    p = tmp_path / "server.json"
    p.write_text(json.dumps({"password": "pw", "host": "11.11.11.12", "telegram": {"token": "old", "max_per_hour": 9}}), encoding="utf-8")
    setup_mod.save_telegram(p, TOKEN, [CHAT])
    cfg = json.loads(p.read_text(encoding="utf-8"))
    assert cfg["password"] == "pw" and cfg["host"] == "11.11.11.12"
    assert cfg["telegram"] == {"token": TOKEN, "chat_ids": [CHAT], "max_per_hour": 9}
    setup_mod.save_telegram(tmp_path / "new" / "server.json", TOKEN, [CHAT])
    assert (tmp_path / "new" / "server.json").is_file()


def test_wizard_end_to_end(tmp_path, tg, monkeypatch, capsys):
    tg.updates = [{"message": {"chat": {"id": CHAT, "type": "private", "first_name": "Макс", "username": "mak"}}}]
    monkeypatch.setattr(getpass, "getpass", lambda *_: TOKEN)
    monkeypatch.setattr("builtins.input", lambda *_: "")
    assert setup_mod.main(data=tmp_path, api_base=tg.url) == 0
    saved = json.loads((tmp_path / "server.json").read_text(encoding="utf-8"))
    assert saved["telegram"] == {"token": TOKEN, "chat_ids": [CHAT]}
    assert tg.sent()[-1].startswith("✅ web_MVS панель: Telegram подключён")
    out = capsys.readouterr().out
    assert TOKEN not in out, "токен мастер не печатает"


def test_wizard_without_messages_to_bot_saves_nothing(tmp_path, tg, monkeypatch, capsys):
    monkeypatch.setattr(getpass, "getpass", lambda *_: TOKEN)
    monkeypatch.setattr("builtins.input", lambda *_: "")
    assert setup_mod.main(data=tmp_path, api_base=tg.url) == 1
    assert not (tmp_path / "server.json").exists()
    assert "Напиши боту" in capsys.readouterr().out


def test_wizard_bad_token_stops(tmp_path, tg, monkeypatch, capsys):
    tg.mode = "401"
    monkeypatch.setattr(getpass, "getpass", lambda *_: TOKEN)
    assert setup_mod.main(data=tmp_path, api_base=tg.url) == 1
    out = capsys.readouterr().out
    assert "Бот не отвечает" in out and TOKEN not in out and not (tmp_path / "server.json").exists()


def test_panel_has_telegram_chip_and_test_button():
    assert 'id="tg"' in PANEL and 'id="tgTest"' in PANEL and "/api/telegram" in PANEL and "Telegram: не настроен" in PANEL
