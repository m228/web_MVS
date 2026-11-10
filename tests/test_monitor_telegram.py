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
                elif outer.mode == "409":
                    code, resp = 409, {"ok": False, "description": "Conflict: terminated by other getUpdates request"}
                elif outer.mode == "401":
                    code, resp = 401, {"ok": False, "description": "Unauthorized " + parts[0]}
                elif outer.mode == "400":
                    code, resp = 400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
                elif method == "sendMessage":
                    resp = {"ok": True, "result": {"message_id": len(outer.requests)}}       # у каждого сообщения свой номер
                elif method == "getMe":
                    resp = {"ok": True, "result": {"username": "monitor_test_bot", "is_bot": True}}
                elif method == "getUpdates":
                    ups = [dict(u, update_id=u.get("update_id", i + 1)) for i, u in enumerate(outer.updates)]
                    off = body.get("offset")
                    ups = ups[-1:] if off == -1 else ([u for u in ups if u["update_id"] >= off] if off is not None else ups)
                    if not ups and body.get("timeout"):
                        time.sleep(0.2)                                 # «длинный опрос» без пустого цикла в тестах
                    resp = {"ok": True, "result": ups}
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
    e.clk.t += 31 * 60
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
    e.clk.t += 31 * 60
    e.store.check_offline()
    e.n.flush()
    e.clk.t += 60
    e.store.ingest(pid, pulse())
    e.n.flush()
    sent = e.tg.sent()
    assert sent[2].startswith("🟠 Кир — Нет пульса уже 31 мин") and sent[3].startswith("🟢 Кир — связь восстановилась")


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


def test_offline_reminder_every_six_hours_only_after_first_message(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    e.store.ingest(pid, pulse())
    e.clk.t += 31 * 60
    e.store.check_offline()
    e.n.flush()
    assert len(e.tg.sent()) == 1
    e.clk.t += 2 * 3600 + 100
    e.n.flush()
    assert len(e.tg.sent()) == 1, "через два часа напоминать рано"
    e.clk.t += 4 * 3600
    e.n.flush()
    assert len(e.tg.sent()) == 2 and "Всё ещё нет пульса" in e.tg.sent()[1] and e.tg.sent()[1].startswith("🟠")
    e.n.flush()
    assert len(e.tg.sent()) == 2, "не чаще раза в шесть часов"


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
    assert cfg["offline_repeat_s"] == 21600 and cfg["api_base"] == mt.API_BASE
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
    (tmp_path / "server.json").write_text(json.dumps({"telegram": {"token": TOKEN, "chat_ids": [CHAT], "morning": False}}), encoding="utf-8")
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


# ------------------------------------------------------------------------------------------------- бот: кнопки заводов и сводка

def snap_pulse(plant, text, alerts=None, micro=None, events=None):
    snap = {"plant": plant, "app_version": "2.0.46", "interval_s": 300, "text": text, "alerts": alerts or []}
    if micro:
        snap["micro"] = micro
    return {"schema": 1, "sent_t": 0, "snapshot": snap, "events": events or []}


KIR_TEXT = ("kir · ПК работает 3 ч 20 мин · приложение 1 ч (v2.0.46)\nВарка: стадия 7 Рост · проб сегодня 12\n"
            "Автокалибровка: готово, 2 ч 0 мин назад, ноль уползал на 12 мкм\nКамера: стримит, экспозиция 322\nТревоги: нет")
KIR_MICRO = {"positions": {
    "retract": {"t": 1_000_000.0 - 120, "ok": True, "gap": 11, "target": 20000, "calc": 20010, "enc": 19990, "ai": 20001, "timeout": False, "why": ""},
    "approach": {"t": 1_000_000.0 - 60, "ok": False, "gap": 214, "target": 500, "calc": 500, "enc": 500, "ai": 714.2, "timeout": False,
                 "why": "датчики разошлись на 214 мкм (допуск 200)"}}}


@pytest.fixture
def bot_env(env):
    e = env
    e.bot = mt.Bot(e.store, mt.TelegramClient(TOKEN, e.tg.url, timeout=5), mt.TelegramClient(TOKEN, e.tg.url, timeout=5), [CHAT], clock=e.clk)
    e.kir, _ = e.store.create_plant("Кир")
    e.bui, _ = e.store.create_plant("Буи")
    e.store.ingest(e.kir, snap_pulse("kir", KIR_TEXT, micro=KIR_MICRO, alerts=[
        {"code": "pos_approach", "level": "high", "text": "Подвод: датчики разошлись на 214 мкм · проб подряд: 2", "since": e.clk.t - 600},
        {"code": "x", "level": "medium", "text": "Что-то жёлтое", "since": e.clk.t - 300}],
        events=[ev("e1", e.clk.t - 900, "Приложение обновлено до v2.0.46", kind="start", level="info")]))
    return e


def msg(text, chat=CHAT, ctype="private", uid=None):
    u = {"message": {"chat": {"id": chat, "type": ctype}, "text": text}}
    if uid:
        u["update_id"] = uid
    return u


def test_keyboard_has_plant_buttons_in_rows_of_two_and_all_button(bot_env):
    kb = bot_env.bot.keyboard()
    assert kb["keyboard"] == [[{"text": "Буи"}, {"text": "Кир"}], [{"text": "Все заводы"}, {"text": "🆕 Заявки"}]]
    assert kb["resize_keyboard"] is True and kb["is_persistent"] is True
    bot_env.store.create_plant("Третий")
    assert bot_env.bot.keyboard()["keyboard"][1] == [{"text": "Третий"}], "новый завод появляется на кнопках сам"


def test_plant_button_gives_full_text_report(bot_env):
    e = bot_env
    reply = e.bot.handle(msg("Кир"))
    lines = reply.split("\n")
    assert lines[0] == "🟢 Кир — в сети, пульс только что"
    assert "ПК работает 3 ч 20 мин · приложение 1 ч (v2.0.46)" in lines[1] and "kir ·" not in lines[1], "имя завода не дублируется"
    assert "Варка: стадия 7 Рост · проб сегодня 12" in lines and "Камера: стримит, экспозиция 322" in lines
    assert not any(ln.startswith("Тревоги: нет") for ln in lines), "строка снимка «Тревоги: нет» заменяется настоящим списком"
    assert "Тревоги:" in lines
    assert any(ln.startswith("🔴 Подвод: датчики разошлись на 214 мкм · проб подряд: 2 (с ") for ln in lines)
    assert any(ln.startswith("🟡 Что-то жёлтое (с ") for ln in lines)
    assert "Датчики последней пробы, мкм:" in lines
    assert any(ln.startswith("Отвод (") and "расчётная 20010 · энкодер 19990 · датчик 20001, разница 11" in ln for ln in lines)
    assert any(ln.startswith("⚠ Подвод (") and "расчётная 500 · энкодер 500 · датчик 714, разница 214" in ln for ln in lines)
    assert "Последние события:" in lines and any("Приложение обновлено до v2.0.46" in ln for ln in lines)
    sent = e.tg.requests[-1]["body"]
    assert sent["chat_id"] == CHAT and sent["text"] == reply
    data = [b["callback_data"] for row in sent["reply_markup"]["inline_keyboard"] for b in row]
    assert data == ["lg:%d" % e.kir, "dk:%d" % e.kir, "mu:%d" % e.kir, "ak:%d" % e.kir, "st:%d" % e.kir, "rn:%d" % e.kir, "tk:%d" % e.kir, "dl:%d" % e.kir]
    assert all(len(d.encode()) <= 64 for d in data)


def test_plant_without_alerts_says_so_and_waiting_plant_is_short(bot_env):
    e = bot_env
    e.store.ingest(e.kir, snap_pulse("kir", KIR_TEXT, micro=None))
    assert "Тревог нет" in e.bot.handle(msg("кир")).split("\n"), "название можно писать с маленькой буквы"
    assert e.bot.handle(msg("Буи")) == "⚪ Буи — ещё не выходил на связь"


def test_offline_plant_is_red_in_report_and_summary(bot_env):
    e = bot_env
    e.clk.t += 31 * 60
    e.store.check_offline()
    rep = e.bot.handle(msg("Кир"))
    assert rep.startswith("🔴 Кир — нет связи, пульс 31 мин назад")
    allr = e.bot.handle(msg("Все заводы"))
    assert allr.split("\n")[0].startswith("Заводы на ") and "🔴 Кир — нет связи, пульс 31 мин назад, тревог: 2" in allr and "⚪ Буи — ещё не выходил на связь" in allr


def test_menu_words_and_unknown_text(bot_env):
    e = bot_env
    for w in ("/start", "/start@monitor_microscope_test_bot", "/help", "Меню"):
        assert "Нажми кнопку с названием завода" in e.bot.handle(msg(w)), w
    assert e.bot.handle(msg("/все")).startswith("Заводы на "), "«все» и «все заводы» — общая сводка"
    assert e.bot.handle(msg("что там у нас?")) == "Не понял. Нажми кнопку с названием завода внизу."
    assert e.bot.handle(msg("   ")) is None and e.bot.handle({"message": {"chat": {"id": CHAT, "type": "private"}}}) is None


def test_bot_answers_only_the_owner_private_chat(bot_env):
    e = bot_env
    before = len(e.tg.requests)
    assert e.bot.handle(msg("Кир", chat=999)) is None, "чужой чат — молчим"
    assert e.bot.handle(msg("Кир", chat=CHAT, ctype="group")) is None, "группа — молчим, даже если id совпал бы"
    assert e.bot.handle({"edited_message": {}}) is None and e.bot.handle({}) is None
    assert len(e.tg.requests) == before


def test_old_questions_after_downtime_are_not_answered(bot_env):
    e = bot_env
    e.tg.updates = [msg("Кир", uid=10), msg("Буи", uid=11)]                # спросили, пока приёмник не работал
    e.bot.skip_backlog()
    assert e.bot.offset == 12
    assert e.bot.poll_once(timeout=0) == (True, 0.0) and not e.tg.sent(), "старые вопросы не отвечаются"
    e.tg.updates.append(msg("Кир", uid=12))
    assert e.bot.poll_once(timeout=0)[0] and len(e.tg.sent()) == 1 and e.tg.sent()[0].startswith("🟢 Кир")
    assert e.bot.offset == 13
    assert e.bot.poll_once(timeout=0)[0] and len(e.tg.sent()) == 1, "тот же вопрос второй раз не отвечается"


def test_poll_errors_are_reported_and_do_not_raise(bot_env):
    e = bot_env
    e.tg.mode = "500"
    ok, wait = e.bot.poll_once(timeout=0)
    assert ok is False and "HTTP 500" in e.bot.state["last_error"]
    e.tg.mode = "409"
    ok, _ = e.bot.poll_once(timeout=0)
    assert ok is False and "уже слушает другая программа" in e.bot.state["last_error"] and TOKEN not in e.bot.state["last_error"]
    e.tg.mode = "ok"
    assert e.bot.poll_once(timeout=0)[0] and e.bot.state["last_error"] == ""


def test_failed_reply_is_recorded_not_raised(bot_env):
    e = bot_env
    e.tg.mode = "500"
    assert e.bot.handle(msg("Кир")) is not None
    assert "HTTP 500" in e.bot.state["last_error"] and e.bot.state["answered"] == 0


def test_long_report_is_cut_to_telegram_limit(bot_env):
    e = bot_env
    e.store.ingest(e.kir, snap_pulse("kir", "kir · " + "очень длинная строка " * 400))
    assert len(e.bot.handle(msg("Кир"))) <= mt.MESSAGE_MAX


def test_bot_thread_answers_a_question_end_to_end(tmp_path, tg, monkeypatch):
    monkeypatch.setenv("MONITOR_DATA", str(tmp_path))
    monkeypatch.setenv("MONITOR_PASSWORD", PASSWORD)
    monkeypatch.setenv("MONITOR_TELEGRAM_API", tg.url)
    (tmp_path / "server.json").write_text(json.dumps({"telegram": {"token": TOKEN, "chat_ids": [CHAT], "morning": False}}), encoding="utf-8")
    tg.updates = [msg("Кир", uid=1)]                                        # вопрос до запуска — не отвечаем
    srv = Srv(None, None, app=entry.build_app())                            # watchdog включён: поднимаются отправщик и бот
    try:
        time.sleep(0.5)
        assert not tg.sent(), "старый вопрос пропущен при старте"
        code, _ = srv.call("POST", "/api/plants", {"name": "Кир"}, headers={"X-Panel": "1"})
        assert code == 200
        tg.updates.append(msg("Кир", uid=2))
        end = time.time() + 10
        while time.time() < end and not tg.sent():
            time.sleep(0.1)
        assert tg.sent() and tg.sent()[0].startswith("⚪ Кир — ещё не выходил на связь")
        last_send = [r for r in tg.requests if r["method"] == "sendMessage"][-1]
        assert last_send["body"]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].startswith("lg:"), "под сводкой — кнопки управления"
    finally:
        srv.close()


# ------------------------------------------------------------------------------------------- обновление: «в историю» без звука

def silent_flags(tg):
    return [bool(r["body"].get("disable_notification")) for r in tg.requests if r["method"] == "sendMessage"]


def upd(eid, t, text, state, level="info"):
    return ev(eid, t, text, kind="update", level=level, state=state)


def test_update_events_are_queued_in_any_level(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    t = e.clk.t
    e.store.ingest(pid, pulse([upd("u1", t - 30, "Скачана v2.0.52, перезапущусь после варки", "staged"),
                               upd("u2", t - 20, "Обновлено 2.0.51 → 2.0.52 · плата ✓", "ok"),
                               upd("u3", t - 10, "Обновлено до 2.0.52, но не всё поднялось: камера не стримит", "warn", level="medium"),
                               upd("old", t - 3600, "Давнее", "ok")]))
    assert rows(e) == [("update", "staged", ""), ("update", "ok", ""), ("update", "warn", "")]


def test_quiet_update_messages_are_silent_and_the_warning_is_loud(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    t = e.clk.t
    e.store.ingest(pid, pulse([upd("u1", t - 30, "Скачана v2.0.52, перезапущусь после варки", "staged"),
                               upd("u2", t - 20, "Обновлено 2.0.51 → 2.0.52 · плата ✓", "ok"),
                               upd("u3", t - 10, "Обновлено до 2.0.52, но не всё поднялось: камера не стримит", "warn", level="medium")]))
    assert e.n.flush() == 2
    texts = e.tg.sent()
    flags = silent_flags(e.tg)
    by_text = dict(zip(texts, flags))
    loud = [t for t, f in by_text.items() if not f]
    quiet = [t for t, f in by_text.items() if f]
    assert len(loud) == 1 and loud[0].startswith("⚠️ Кир — Обновлено до 2.0.52, но не всё поднялось")
    assert len(quiet) == 1 and quiet[0].count("\n") == 1 and quiet[0].startswith("📦 Кир — Скачана v2.0.52") and "\n✅ Кир — Обновлено 2.0.51" in quiet[0]


def test_failed_update_is_silent_because_the_restart_alarm_already_rings(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    t = e.clk.t
    e.store.ingest(pid, pulse([ev("s1", t - 30, "Приложение перезапущено, предыдущая остановка нештатная", kind="start", level="critical"),
                               upd("u1", t - 20, "Обновление до v2.0.52 не удалось, работаю на v2.0.51", "failed")]))
    assert e.n.flush() == 2
    by_text = dict(zip(e.tg.sent(), silent_flags(e.tg)))
    assert by_text["🔴 Кир — Приложение перезапущено, предыдущая остановка нештатная · " + time.strftime("%H:%M", time.localtime(t - 30))] is False
    assert [v for k, v in by_text.items() if k.startswith("↩️")] == [True]


def test_alarms_stay_loud_even_with_quiet_messages_alongside(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    t = e.clk.t
    e.store.ingest(pid, pulse([upd("u1", t - 30, "Скачана v2.0.52, перезапущусь после варки", "staged"),
                               ev("a1", t - 20, "Камера: экспозиция 5000", code="camera_exposure", state="open")]))
    e.n.flush()
    by_text = {k.split(" — ")[0]: v for k, v in zip(e.tg.sent(), silent_flags(e.tg))}
    assert by_text == {"🔴 Кир": False, "📦 Кир": True}


def test_quiet_messages_do_not_eat_the_hourly_alarm_budget(env):
    e = env
    e.cfg["max_per_hour"] = 1
    pid, _ = e.store.create_plant("Кир")
    t = e.clk.t
    e.store.ingest(pid, pulse([upd("u1", t - 30, "Скачана v2.0.52", "staged")]))
    e.n.flush()
    e.store.ingest(pid, pulse([upd("u2", t - 25, "Обновлено 2.0.51 → 2.0.52", "ok")]))
    e.n.flush()
    e.store.ingest(pid, pulse([ev("s1", t - 5, "ПК перезагрузился", kind="start", level="critical")]))
    assert e.n.flush() == 1
    assert [s for s in e.tg.sent() if s.startswith("🔴")] != [], "тревога после двух тихих сообщений прошла"


def test_update_line_icons_and_quiet_rule():
    base = {"t": 1_000_000.0, "plant_name": "Кир", "text": "Текст", "kind": "update", "state": "", "level": "info"}
    assert mt.format_line(dict(base, state="staged")).startswith("📦 Кир — Текст")
    assert mt.format_line(dict(base, state="ok")).startswith("✅ Кир — Текст")
    assert mt.format_line(dict(base, state="warn")).startswith("⚠️ Кир — Текст")
    assert mt.format_line(dict(base, state="failed")).startswith("↩️ Кир — Текст")
    assert [mt.is_quiet(dict(base, state=s)) for s in ("staged", "ok", "failed", "warn")] == [True, True, True, False]
    assert not mt.is_quiet(dict(base, kind="alert", state="open"))


def test_client_sets_disable_notification_only_when_silent(env):
    e = env
    c = mt.TelegramClient(TOKEN, e.tg.url, timeout=5)
    assert c.send(CHAT, "громко")[0] and c.send(CHAT, "тихо", silent=True)[0]
    bodies = [r["body"] for r in e.tg.requests if r["method"] == "sendMessage"]
    assert "disable_notification" not in bodies[0] and bodies[1]["disable_notification"] is True


# ------------------------------------------------------------------------------------------------- бот: управление заводами с телефона

def cb(data, chat=CHAT, mid=70, ctype="private", cid="cb1"):
    return {"callback_query": {"id": cid, "data": data, "message": {"message_id": mid, "text": "сообщение бота\nвторая строка", "chat": {"id": chat, "type": ctype}}}}


def reqs(e, method):
    return [r["body"] for r in e.tg.requests if r["method"] == method]


def kinds(e):
    return [r["method"] for r in e.tg.requests]


def test_disk_line_in_plant_report_and_in_all_with_thresholds(bot_env):
    e = bot_env
    snap = snap_pulse("kir", KIR_TEXT)
    snap["snapshot"]["disk"] = {"free_gb": 120.4, "total_gb": 500.0}
    e.store.ingest(e.kir, snap)
    assert "Диск: свободно 120 из 500 ГБ (24 %)" in e.bot.handle(msg("Кир")).split("\n")
    assert "диск свободно 120/500 ГБ (24 %)" in e.bot.handle(msg("Все заводы"))
    for free, mark in ((49.0, "⚠ "), (20.0, "🔴 ")):                       # < 10 % — предупреждение, < 5 % — красный
        snap["snapshot"]["disk"] = {"free_gb": free, "total_gb": 500.0}
        e.store.ingest(e.kir, snap)
        assert any(ln.startswith(mark + "Диск: свободно") for ln in e.bot.handle(msg("Кир")).split("\n")), free
    snap["snapshot"].pop("disk")
    snap["snapshot"]["motion"] = {"cameras": [], "disk": {"free_gb": 300.0, "total_gb": 500.0}}      # клиенты 2.1.27 держали диск внутри «движения»
    e.store.ingest(e.kir, snap)
    assert "Диск: свободно 300 из 500 ГБ (60 %)" in e.bot.handle(msg("Кир"))
    snap["snapshot"].pop("motion")
    e.store.ingest(e.kir, snap)
    assert "Диск:" not in e.bot.handle(msg("Кир")), "нет данных — строки нет, цифр не выдумываем"


def test_disk_button_answers_with_free_used_and_without_data(bot_env):
    e = bot_env
    e.bot.handle(cb("dk:%d" % e.kir))
    assert "данных о диске нет" in e.tg.sent()[-1]
    snap = snap_pulse("kir", KIR_TEXT)
    snap["snapshot"]["disk"] = {"free_gb": 120.0, "total_gb": 500.0}
    e.store.ingest(e.kir, snap)
    e.bot.handle(cb("dk:%d" % e.kir))
    assert e.tg.sent()[-1] == "💾 Кир\nДиск: свободно 120 из 500 ГБ (24 %)\nЗанято 380 ГБ"
    assert reqs(e, "answerCallbackQuery")[-1]["callback_query_id"] == "cb1", "кнопка перестаёт крутиться"


def test_log_button_lists_ten_latest_events(bot_env):
    e = bot_env
    e.bot.handle(cb("lg:%d" % e.kir))
    text = e.tg.sent()[-1]
    assert text.startswith("📜 Кир — последние события:") and "Приложение обновлено до v2.0.46" in text
    e.bot.handle(cb("lg:%d" % e.bui))
    assert "Записей пока нет." in e.tg.sent()[-1]


def test_ack_button_clears_alerts_and_drops_its_own_button(bot_env):
    e = bot_env
    e.bot.handle(cb("ak:%d" % e.kir))
    assert reqs(e, "answerCallbackQuery")[-1]["text"] == "Сброшено тревог: 2 (в журнале останутся)"
    kir = next(p for p in e.store.plants_view() if p["id"] == e.kir)
    assert kir["alerts"] and all(a["acked"] for a in kir["alerts"])
    markup = reqs(e, "editMessageReplyMarkup")[-1]["reply_markup"]["inline_keyboard"]
    assert "ak:%d" % e.kir not in [b["callback_data"] for row in markup for b in row]
    e.bot.handle(cb("ak:%d" % e.kir))
    assert reqs(e, "answerCallbackQuery")[-1]["text"] == "Сброшено тревог: 0 (в журнале останутся)"


def test_rename_waits_for_name_and_validates(bot_env):
    e = bot_env
    e.bot.handle(cb("rn:%d" % e.kir))
    assert "Как назвать «Кир»?" in e.tg.sent()[-1]
    assert reqs(e, "sendMessage")[-1]["reply_markup"]["inline_keyboard"] == [[{"text": "Отмена", "callback_data": "xx"}]]
    assert e.bot.handle(msg("   ")) is None, "пустое сообщение — игнор"
    reply = e.bot.handle(msg("А" * 41))                                         # слишком длинное: ждём новую попытку, ожидание не теряем
    assert reply.startswith("Не вышло: Название завода — от 1 до 40 символов") and e.bot._pending
    reply = e.bot.handle(msg("Кир-2"))
    assert reply.startswith("✏ Готово: «Кир» → «Кир-2»") and not e.bot._pending
    assert "Кир-2" in e.store.plant_names() and "Кир" not in e.store.plant_names()
    kb = reqs(e, "sendMessage")[-1]["reply_markup"]["keyboard"]
    assert {"text": "Кир-2"} in [b for row in kb for b in row], "кнопки с названиями обновились"


def test_cancel_word_ends_rename_and_keeps_name(bot_env):
    e = bot_env
    e.bot.handle(cb("rn:%d" % e.kir))
    assert e.bot.handle(msg("Отмена")) == "Отменено." and not e.bot._pending
    assert e.store.plant_names() == ["Буи", "Кир"]


def test_menu_button_during_rename_is_navigation_not_a_name(bot_env):
    e = bot_env
    e.bot.handle(cb("rn:%d" % e.kir))
    assert e.bot.handle(msg("Буи")) == "⚪ Буи — ещё не выходил на связь", "нажал завод — это не новое имя"
    assert not e.bot._pending and e.store.plant_names() == ["Буи", "Кир"]


def test_rename_wait_expires_after_five_minutes(bot_env):
    e = bot_env
    e.bot.handle(cb("rn:%d" % e.kir))
    e.clk.t += 301
    assert e.bot.handle(msg("Новое имя")) == "Не понял. Нажми кнопку с названием завода внизу."
    assert e.store.plant_names() == ["Буи", "Кир"]


def test_new_token_needs_confirmation_then_old_token_dies(bot_env):
    e = bot_env
    pid, tok_old = e.store.create_plant("Третий")
    e.bot.handle(cb("tk:%d" % pid))
    assert "Старый сразу перестанет работать" in e.tg.sent()[-1]
    assert e.store.plant_by_token(tok_old) is not None, "без подтверждения токен не меняется"
    e.bot.handle(cb("tky:%d" % pid, mid=71))
    assert e.store.plant_by_token(tok_old) is None, "старый токен больше не принимается"
    tok_msg = [b for b in reqs(e, "sendMessage") if b.get("parse_mode") == "HTML"][-1]
    new = tok_msg["text"].split("<code>")[1].split("</code>")[0]
    assert e.store.plant_by_token(new)["id"] == pid and tok_msg["protect_content"] is True
    assert reqs(e, "editMessageText")[-1]["text"] == "🔑 Новый токен для «Третий» выдан." and reqs(e, "editMessageText")[-1]["message_id"] == 71


def test_token_message_is_deleted_after_five_minutes_and_survives_restart(bot_env):
    e = bot_env
    e.bot.handle(cb("tky:%d" % e.kir))
    idx = next(i for i, r in enumerate(e.tg.requests) if r["method"] == "sendMessage" and r["body"].get("parse_mode") == "HTML")
    assert e.bot.sweep_deletes() == 0 and "deleteMessage" not in kinds(e), "рано"
    bot2 = mt.Bot(e.store, mt.TelegramClient(TOKEN, e.tg.url, timeout=5), mt.TelegramClient(TOKEN, e.tg.url, timeout=5), [CHAT], clock=e.clk)   # «перезапуск»
    e.clk.t += 301
    assert bot2.sweep_deletes() == 1
    d = reqs(e, "deleteMessage")[-1]
    assert d["chat_id"] == CHAT and d["message_id"] == idx + 1, "стёрто именно сообщение с токеном"
    assert bot2.sweep_deletes() == 0 and e.store.meta_get(mt.Bot.DELETE_KEY) == "[]", "второй раз не стираем"


def test_failed_delete_is_retried_when_telegram_is_unreachable(bot_env):
    e = bot_env
    e.bot._track_delete(CHAT, 5, e.clk.t - 10)
    e.tg.mode = "500"
    assert e.bot.sweep_deletes() == 0
    assert e.bot._deletes() == [[CHAT, 5, e.clk.t - 10]], "связи нет — оставили на потом"
    e.tg.mode = "400"                                                           # Telegram ответил отказом (сообщения нет / не даёт) — повторять нечего
    assert e.bot.sweep_deletes() == 0 and e.bot._deletes() == []
    e.tg.mode = "500"
    e.bot._track_delete(CHAT, 6, e.clk.t - 10 - mt.Bot.DELETE_KEEP_S)
    e.bot.sweep_deletes()
    assert e.bot._deletes() == [], "старше 47 часов — Telegram уже не даст, бросаем"


def test_delete_needs_confirmation_and_removes_plant(bot_env):
    e = bot_env
    e.bot.handle(cb("dl:%d" % e.kir))
    assert "Удалить «Кир»" in e.tg.sent()[-1] and "Вернуть нельзя" in e.tg.sent()[-1]
    assert "Кир" in e.store.plant_names(), "без подтверждения завод на месте"
    e.bot.handle(cb("dly:%d" % e.kir, mid=72))
    assert e.store.plant_names() == ["Буи"]
    assert reqs(e, "editMessageText")[-1]["text"] == "🗑 Удалён: «Кир»."
    kb = reqs(e, "sendMessage")[-1]["reply_markup"]["keyboard"]
    assert {"text": "Кир"} not in [b for row in kb for b in row]
    e.bot.handle(cb("dly:%d" % e.kir, mid=73))                                  # повторное нажатие
    assert reqs(e, "editMessageText")[-1]["text"] == "Такого завода уже нет."


def test_cancel_button_removes_the_question(bot_env):
    e = bot_env
    e.bot.handle(cb("xx", mid=74))
    assert reqs(e, "editMessageText")[-1] == {"chat_id": CHAT, "message_id": 74, "text": "Отменено.", "disable_web_page_preview": True}
    assert e.store.plant_names() == ["Буи", "Кир"]


def test_settings_menu_changes_plant_settings_and_resets(bot_env):
    e = bot_env
    e.bot.handle(cb("st:%d" % e.kir))
    first = reqs(e, "sendMessage")[-1]
    assert first["text"].startswith("⚙ Кир — настройки «нет пульса» (общие)") and "30 мин" in first["text"]
    assert "✓ 30 мин" in str(first["reply_markup"]) and "↩" not in str(first["reply_markup"])
    e.bot.handle(cb("sv:%d:a:60" % e.kir, mid=75))
    assert e.store.plant_settings(e.kir)["offline_after_min"] == 60
    edit = reqs(e, "editMessageText")[-1]
    assert edit["message_id"] == 75 and "(свои)" in edit["text"] and "✓ 60 мин" in str(edit["reply_markup"])
    assert "↩ Вернуть общие значения" in str(edit["reply_markup"])
    e.bot.handle(cb("sv:%d:r:0" % e.kir))
    e.bot.handle(cb("sv:%d:n:0" % e.kir))
    s = e.store.plant_settings(e.kir)
    assert s["offline_repeat_min"] == 0 and s["offline_notify"] is False
    e.bot.handle(cb("sv:%d:z" % e.kir))
    assert e.store.plant_overrides(e.kir) == {}, "вернули общие значения"
    assert e.store.plant_settings(e.bui)["offline_after_min"] == 30, "другие заводы не тронуты"


def test_settings_garbage_is_ignored(bot_env):
    e = bot_env
    for data in ("sv:%d:a:abc" % e.kir, "sv:%d:a" % e.kir, "sv:%d:q:1" % e.kir, "sv:%d:n:5" % e.kir, "sv:abc:a:60", "sv:", "zz:1"):
        assert e.bot.handle(cb(data)) is None, data
    assert e.store.plant_overrides(e.kir) == {}
    assert e.bot.handle(cb("sv:%d:a:3" % e.kir)) == "Нет пульса через: от 5 до 1440 минут", "значение вне границ — сообщение, не падение"


def test_requests_button_lists_waiting_clients_with_accept_buttons(bot_env):
    e = bot_env
    assert e.bot.handle(msg("🆕 Заявки")) == "Заявок на токен нет."
    e.store.enroll_request("machine-id-0001", "ПК жома", "2.1.45", "10.0.0.7", "s" * 20)
    assert e.bot.handle(msg("заявки")) == "Ждут решения: 1."
    last = reqs(e, "sendMessage")[-1]
    assert last["text"].startswith("🆕 Новый клиент просит токен: «ПК жома»") and last["reply_markup"]["inline_keyboard"][0][0]["callback_data"].startswith("ea:")
    e.bot.handle(cb(last["reply_markup"]["inline_keyboard"][0][0]["callback_data"]))
    assert "ПК жома" in e.store.plant_names()


def test_buttons_from_foreign_chats_do_nothing(bot_env):
    e = bot_env
    before = len(e.tg.requests)
    for data in ("dly:%d" % e.kir, "tky:%d" % e.kir, "sv:%d:z" % e.kir, "ak:%d" % e.kir):
        assert e.bot.handle(cb(data, chat=999)) is None
        assert e.bot.handle(cb(data, ctype="group")) is None
    assert len(e.tg.requests) == before and e.store.plant_names() == ["Буи", "Кир"]


def test_buttons_for_missing_plant_say_so(bot_env):
    e = bot_env
    for kind in ("lg", "dk", "ak", "st", "rn", "tk"):
        e.bot.handle(cb("%s:9999" % kind))
    assert [r["text"] for r in reqs(e, "answerCallbackQuery")] == ["Такого завода уже нет."] * 6
    assert not e.bot._pending


def test_double_press_of_token_confirmation_issues_one_token(bot_env):
    e = bot_env
    data = "tky:%d" % e.kir
    e.bot.handle(cb(data, mid=80, cid="a"))
    first = [b for b in reqs(e, "sendMessage") if b.get("parse_mode") == "HTML"][-1]["text"].split("<code>")[1].split("</code>")[0]
    assert e.bot.handle(cb(data, mid=80, cid="b")) == "Уже выдан."
    assert len([b for b in reqs(e, "sendMessage") if b.get("parse_mode") == "HTML"]) == 1, "второй токен не выдан"
    assert e.store.plant_by_token(first)["id"] == e.kir, "выданный токен жив"
    e.bot.handle(cb(data, mid=81, cid="c"))                                     # новый вопрос — новое решение
    assert e.store.plant_by_token(first) is None


def test_token_that_did_not_reach_the_chat_is_reported_honestly(bot_env, monkeypatch):
    e = bot_env
    real = e.bot.client.call
    monkeypatch.setattr(e.bot.client, "call", lambda m, p=None: (False, None, 0.0, "нет связи") if m == "sendMessage" else real(m, p))
    assert e.bot.handle(cb("tky:%d" % e.kir, mid=82)) == "Токен не дошёл, выдай новый"
    assert "в чат не дошёл" in reqs(e, "editMessageText")[-1]["text"] and "выдан." not in reqs(e, "editMessageText")[-1]["text"]
    assert e.bot._deletes() == []


def test_stale_confirmation_does_nothing(bot_env):
    e = bot_env
    old = cb("dly:%d" % e.kir)
    old["callback_query"]["message"]["date"] = e.clk.t - 601
    assert e.bot.handle(old) == "Вопрос устарел — нажми кнопку заново."
    assert e.store.plant_names() == ["Буи", "Кир"]
    fresh = cb("dly:%d" % e.kir, mid=90)
    fresh["callback_query"]["message"]["date"] = e.clk.t - 30
    e.bot.handle(fresh)
    assert e.store.plant_names() == ["Буи"]


def test_skip_backlog_reports_failure_and_keeps_offset_empty(bot_env):
    e = bot_env
    e.tg.mode = "500"
    assert e.bot.skip_backlog() is False and e.bot.offset is None, "не вышло — очередь не считается пропущенной"
    e.tg.mode = "ok"
    e.tg.updates = [cb("dly:%d" % e.kir, cid="old")]
    assert e.bot.skip_backlog() is True and e.bot.offset == 2


def test_failed_action_still_answers_the_callback(bot_env, monkeypatch):
    e = bot_env
    monkeypatch.setattr(e.store, "ack_alerts", lambda *a, **k: 1 / 0)
    assert e.bot.handle(cb("ak:%d" % e.kir)) == "Не получилось, смотри панель."
    assert reqs(e, "answerCallbackQuery")[-1]["text"] == "Не получилось, смотри панель."


def test_broken_disk_in_snapshot_is_ignored_not_fatal(bot_env):
    e = bot_env
    for bad in ({"free_gb": "много", "total_gb": 500}, {"free_gb": float("nan"), "total_gb": 500}, {"free_gb": 600, "total_gb": 500},
                {"free_gb": 5, "total_gb": 0}, {"free_gb": True, "total_gb": 500}, "диск"):
        snap = snap_pulse("kir", KIR_TEXT)
        snap["snapshot"]["disk"] = bad
        e.store.ingest(e.kir, snap)
        assert "Диск:" not in e.bot.handle(msg("Кир")) and e.bot.handle(msg("Все заводы")).startswith("Заводы на "), bad
    snap["snapshot"].pop("disk")
    snap["snapshot"]["motion"] = "сломано"
    e.store.ingest(e.kir, snap)
    assert "Диск:" not in e.bot.handle(msg("Кир"))


def test_plant_named_like_a_menu_word_is_still_reachable(bot_env):
    e = bot_env
    e.store.create_plant("Все")
    assert e.bot.handle(msg("Все")) == "⚪ Все — ещё не выходил на связь"
    assert e.bot.handle(msg("Все заводы")).startswith("Заводы на ")


# ------------------------------------------------------------------------------------------------- «Не беспокоить» (тихий режим завода)

def mute_alert(eid, t, code="camera_exposure", state="open", text="Камера: экспозиция 5000 вместо 170"):
    return ev(eid, t, text, code=code, state=state)


def muted_rows(e):
    return [(r["status"], r["reason"]) for r in e.store.db.execute("SELECT status, reason FROM notify ORDER BY id")]


def test_mute_holds_telegram_back_but_journal_and_panel_keep_everything(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    e.store.ingest(pid, pulse())
    until = e.store.set_mute(pid, 120)
    assert until == e.clk.t + 7200 and e.store.mute_until(pid) == until
    e.store.ingest(pid, pulse([mute_alert("a1", e.clk.t - 20)]))
    assert rows(e) == [], "в очередь для Telegram ничего не попало"
    assert muted_rows(e) == [("muted", ms.MUTE_REASON)], "но запись осталась — для итога"
    e.n.flush()
    assert e.tg.sent() == []
    texts = [x["text"] for x in e.store.events(pid, 20)]
    assert any("Камера: экспозиция" in t for t in texts) and any(t.startswith("Тихий режим до ") for t in texts), "в журнале и событие, и пометка о тишине"
    view = next(p for p in e.store.plants_view() if p["id"] == pid)
    assert view["mute_until"] == until


def test_mute_validation_and_missing_plant(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    for bad in (0, -5, ms.MUTE_MAX_MIN + 1, "abc", True, 1.5, [60]):
        with pytest.raises(ValueError):
            e.store.set_mute(pid, bad)
    with pytest.raises(LookupError):
        e.store.set_mute(999, 60)
    assert e.store.mute_until(pid) is None
    e.store.set_mute(pid, None)                                                  # снимать нечего — тихо ничего не делает
    assert not any("Тихий режим" in x["text"] for x in e.store.events(pid, 20))
    assert e.store.set_mute(pid, ms.MUTE_MAX_MIN) == e.clk.t + 86400


def test_mute_ends_by_itself_and_alerts_flow_again(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    e.store.ingest(pid, pulse())
    e.store.set_mute(pid, 60)
    e.clk.t += 3601
    assert e.store.mute_until(pid) is None
    e.store.ingest(pid, pulse([mute_alert("a2", e.clk.t - 5)]))
    assert rows(e) == [("alert", "open", "camera_exposure")]


def test_alerts_queued_before_the_mute_are_held_and_counted(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    e.store.ingest(pid, pulse([mute_alert("a1", e.clk.t - 20)]))
    assert len(rows(e)) == 1
    e.store.set_mute(pid, 60)                                                    # нажали «Тихо» под сообщением, а в очереди ещё лежит соседняя тревога
    e.n.flush()
    assert e.tg.sent() == [] and muted_rows(e) == [("muted", ms.MUTE_REASON)]
    e.clk.t += 3601
    e.store.ingest(pid, pulse())
    e.n.flush()
    assert len(e.tg.sent()) == 1 and "Пока молчали: 1 событие" in e.tg.sent()[0]


def test_mute_end_sends_one_summary_with_what_is_still_open(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    alert = {"code": "camera_exposure", "level": "high", "text": "Камера: экспозиция 5000", "since": e.clk.t}
    e.store.ingest(pid, pulse(alerts=[alert]))
    e.store.set_mute(pid, 60)
    e.store.ingest(pid, pulse(alerts=[dict(alert, code="x2", text="Плата молчит")]))
    e.n.flush()
    assert e.tg.sent() == []
    e.clk.t += 3601
    e.store.ingest(pid, pulse(alerts=[alert, dict(alert, code="x2", text="Плата молчит")]))
    e.n.flush()
    assert len(e.tg.sent()) == 1
    text = e.tg.sent()[0]
    assert text.startswith("🔔 Кир — тишина закончилась (") and "Пока молчали: " in text
    assert "Сейчас открыто: " in text and "🔴 Плата молчит" in text
    e.n.flush()
    e.n.flush()
    assert len(e.tg.sent()) == 1, "итог один"


def test_manual_unmute_sends_summary_even_when_nothing_happened(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    e.store.ingest(pid, pulse())
    e.store.set_mute(pid, 120)
    e.store.set_mute(pid, None)
    assert e.store.mute_until(pid) is None
    assert any(x["text"] == "Тихий режим снят" for x in e.store.events(pid, 20))
    e.n.flush()
    assert len(e.tg.sent()) == 1 and "важных событий не было" in e.tg.sent()[0] and "красных тревог нет" in e.tg.sent()[0]


def test_summary_is_retried_until_telegram_answers(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    e.store.ingest(pid, pulse())
    e.store.set_mute(pid, 5)
    e.clk.t += 301
    e.tg.mode = "500"
    e.n.flush()
    assert e.tg.sent() == [] and e.n.state["last_error"]
    e.tg.mode = "ok"
    e.n.flush()
    assert e.tg.sent() == [], "после сбоя — пауза, а не новая попытка на каждом проходе"
    e.clk.t += mt.MUTE_RETRY_S + 1
    e.n.flush()
    assert len(e.tg.sent()) == 1
    e.n.flush()
    assert len(e.tg.sent()) == 1


def test_summary_of_a_long_gone_mute_is_dropped_quietly(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    e.store.ingest(pid, pulse())
    e.store.set_mute(pid, 60)
    e.clk.t += 3600 + ms.MUTE_MAX_MIN * 60 + 10                                  # приёмник не работал больше суток
    e.n.flush()
    assert e.tg.sent() == [] and e.store.mute_finished(e.clk.t) == []


def test_offline_reminders_are_silent_during_mute(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    e.store.ingest(pid, pulse())
    e.clk.t += 31 * 60
    e.store.check_offline()
    e.n.flush()
    assert len(e.tg.sent()) == 1
    e.store.set_mute(pid, 600)
    e.clk.t += 7 * 3600
    e.n.flush()
    assert len(e.tg.sent()) == 1, "напоминание «всё ещё нет пульса» в тишине не идёт"


def test_mute_survives_restart_and_dies_with_the_plant(env, tmp_path):
    e = env
    pid, _ = e.store.create_plant("Кир")
    until = e.store.set_mute(pid, 90)
    again = ms.Store(tmp_path / "monitor.db", clock=e.clk, notify={"fresh_s": 900})
    try:
        assert again.mute_until(pid) == until
    finally:
        again.close()
    assert e.store.delete_plant(pid) and e.store.meta_get("mute:%d" % pid) is None


def test_mute_until_morning_is_before_the_next_morning(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    until = e.store.set_mute(pid, "morning")
    lt = time.localtime(until)
    assert (lt.tm_hour, lt.tm_min) == (8, 0) and 0 < until - e.clk.t <= 86400
    assert e.store.mute_morning_due() == until


def test_garbage_mute_record_is_ignored(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    for junk in ("не json", "[]", '{"until": "завтра"}', '{"until": true}'):
        e.store.meta_set("mute:%d" % pid, junk)
        assert e.store.mute_until(pid) is None
    assert e.store.mute_finished(e.clk.t) == []


def test_alert_message_carries_quiet_button_but_good_news_does_not(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    e.store.ingest(pid, pulse([mute_alert("a1", e.clk.t - 20)]))
    e.n.flush()
    sent = [r["body"] for r in e.tg.requests if r["method"] == "sendMessage"]
    assert sent[-1]["reply_markup"]["inline_keyboard"][0][0] == {"text": "🔕 Тихо 2 ч", "callback_data": "mt:%d:120" % pid}
    e.store.ingest(pid, pulse([mute_alert("a1c", e.clk.t - 5, state="closed")]))
    e.n.flush()
    sent = [r["body"] for r in e.tg.requests if r["method"] == "sendMessage"]
    assert len(sent) == 2 and "reply_markup" not in sent[-1], "«снято» — хорошая весть, кнопка тишины не нужна"


def test_wants_mute_button_rule():
    assert mt.wants_mute_button([{"kind": "alert", "state": "open"}]) and mt.wants_mute_button([{"kind": "offline", "state": "open"}])
    assert not mt.wants_mute_button([{"kind": "online", "state": ""}]) and not mt.wants_mute_button([{"kind": "alert", "state": "closed"}])
    assert not mt.wants_mute_button([{"kind": "update", "state": "warn"}])


# --- бот

def test_bot_menu_button_opens_presets_and_sets_the_mute(bot_env):
    e = bot_env
    assert e.bot.handle(cb("mu:%d" % e.kir)) is None
    menu = reqs(e, "sendMessage")[-1]
    data = [b["callback_data"] for row in menu["reply_markup"]["inline_keyboard"] for b in row]
    assert data == ["ms:%d:60" % e.kir, "ms:%d:120" % e.kir, "ms:%d:240" % e.kir, "ms:%d:m" % e.kir] and "на сколько не беспокоить" in menu["text"]
    reply = e.bot.handle(cb("ms:%d:120" % e.kir, mid=71))
    until = e.clk.t + 7200
    assert e.store.mute_until(e.kir) == until and reply == "🔕 Тихо до " + time.strftime("%H:%M", time.localtime(until))
    edit = reqs(e, "editMessageText")[-1]
    assert edit["message_id"] == 71 and "тихо до" in edit["text"] and edit["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "ms:%d:0" % e.kir
    assert reqs(e, "answerCallbackQuery")[-1]["text"] == reply


def test_bot_unmute_and_report_shows_the_mute(bot_env):
    e = bot_env
    e.bot.handle(cb("ms:%d:60" % e.kir))
    report = e.bot.handle(msg("Кир"))
    assert "🔕 Не беспокоить до " in report
    sent = reqs(e, "sendMessage")[-1]
    first = [b for row in sent["reply_markup"]["inline_keyboard"] for b in row]
    assert any(b["callback_data"] == "mc:%d" % e.kir and b["text"].startswith("🔔 Снять тишину (до ") for b in first)
    assert "🔕 тихо до " in e.bot.handle(msg("Все заводы"))
    # «Снять» на сводке: сводка остаётся, обновляются только кнопки (там снова «Не беспокоить»)
    edits_before = len(reqs(e, "editMessageText"))
    assert e.bot.handle(cb("mc:%d" % e.kir, mid=72)) == "🔔 Тишина снята" and e.store.mute_until(e.kir) is None
    assert len(reqs(e, "editMessageText")) == edits_before, "текст сводки не трогаем"
    markup = reqs(e, "editMessageReplyMarkup")[-1]
    assert markup["message_id"] == 72 and "mu:%d" % e.kir in [b["callback_data"] for row in markup["reply_markup"]["inline_keyboard"] for b in row]
    assert "🔕 Не беспокоить до " not in e.bot.handle(msg("Кир"))
    # «Снять» в меню выбора срока правит само меню
    e.bot.handle(cb("ms:%d:60" % e.kir, mid=73))
    assert e.bot.handle(cb("ms:%d:0" % e.kir, mid=73)) == "🔔 Тишина снята"
    assert e.store.mute_until(e.kir) is None and "тишина снята" in reqs(e, "editMessageText")[-1]["text"]
    assert e.bot.handle(cb("mc:999")) == e.bot.GONE


def test_bot_quiet_button_under_an_alert_toggles(bot_env):
    e = bot_env
    reply = e.bot.handle(cb("mt:%d:120" % e.kir, mid=75))
    assert reply.startswith("🔕 Тихо до ") and e.store.mute_until(e.kir) is not None
    markup = reqs(e, "editMessageReplyMarkup")[-1]
    assert markup["message_id"] == 75 and markup["reply_markup"]["inline_keyboard"][0][0] == {"text": "🔔 Снять тишину", "callback_data": "mt:%d:0" % e.kir}
    assert e.bot.handle(cb("mt:%d:0" % e.kir, mid=75)) == "🔔 Тишина снята" and e.store.mute_until(e.kir) is None
    assert reqs(e, "editMessageReplyMarkup")[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "mt:%d:120" % e.kir


def test_bot_mute_until_morning(bot_env):
    e = bot_env
    e.bot.handle(cb("ms:%d:m" % e.kir))
    until = e.store.mute_until(e.kir)
    assert until is not None and 0 < until - e.clk.t <= 86400 and time.localtime(until).tm_hour == 8


def test_bot_mute_garbage_foreign_chat_and_missing_plant(bot_env):
    e = bot_env
    for bad in ("ms:%d:zzz" % e.kir, "ms:%d:99999" % e.kir, "ms:%d" % e.kir, "mt:%d:-5" % e.kir, "mt:%d:0.5" % e.kir):
        assert e.bot.handle(cb(bad)) is None, bad
    assert e.store.mute_until(e.kir) is None
    assert e.bot.handle(cb("mt:%d:60" % e.kir, chat=555)) is None and e.store.mute_until(e.kir) is None
    assert e.bot.handle(cb("mt:%d:60" % e.kir, ctype="group")) is None and e.store.mute_until(e.kir) is None
    assert e.bot.handle(cb("mt:999:60")) == e.bot.GONE
    assert e.bot.handle(cb("mu:999")) == e.bot.GONE


# --- панель и API

def test_mute_api_sets_and_clears(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    srv = Srv(e.store, e.n)
    try:
        path = "/api/plants/%d/mute" % pid
        assert srv.call("POST", path, {"minutes": 60})[0] == 403, "запись только из панели (X-Panel)"
        code, j = srv.call("POST", path, {"minutes": 60}, headers={"X-Panel": "1"})
        assert code == 200 and j == {"mute_until": e.clk.t + 3600} and e.store.mute_until(pid) == e.clk.t + 3600
        code, j = srv.call("POST", path, {"minutes": "morning"}, headers={"X-Panel": "1"})
        assert code == 200 and j["mute_until"] > e.clk.t
        for bad in ({"minutes": 0}, {"minutes": 99999}, {"minutes": "x"}, {}, [1]):
            assert srv.call("POST", path, bad, headers={"X-Panel": "1"})[0] == 400, bad
        assert srv.call("POST", "/api/plants/999/mute", {"minutes": 5}, headers={"X-Panel": "1"})[0] == 404
        code, j = srv.call("POST", path, {"minutes": None}, headers={"X-Panel": "1"})
        assert code == 200 and j == {"mute_until": None} and e.store.mute_until(pid) is None
        code, j = srv.call("GET", "/api/plants")
        assert code == 200 and "mute_until" in json.dumps(j)
    finally:
        srv.close()


def test_panel_has_mute_control():
    assert "function muteCtl" in PANEL and "data-mute" in PANEL and "/mute'" in PANEL and "'unmute'" in PANEL and "Тихо…" in PANEL


# --- находки независимого ревью тишины

def test_alert_opened_in_mute_and_closed_after_it_still_says_closed(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    e.store.ingest(pid, pulse())
    e.store.set_mute(pid, 60)
    e.store.ingest(pid, pulse([mute_alert("a1", e.clk.t - 20)]))
    assert rows(e) == []
    e.clk.t += 3601
    e.store.ingest(pid, pulse([mute_alert("a1c", e.clk.t - 5, state="closed")]))
    assert rows(e) == [("alert", "closed", "camera_exposure")], "итог сказал бы «открыто», а «снято» не пришло бы — теперь приходит"


def test_offline_in_mute_then_recovery_after_it_says_online(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    e.store.ingest(pid, pulse())
    e.store.set_mute(pid, 60)
    e.clk.t += 31 * 60
    e.store.check_offline()
    assert rows(e) == [] and muted_rows(e)[-1] == ("muted", ms.MUTE_REASON)
    e.clk.t += 40 * 60
    e.store.ingest(pid, pulse())
    assert [r[0] for r in rows(e)] == ["online"]


def test_offline_reminder_counts_from_the_end_of_mute_not_instantly(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    e.store.ingest(pid, pulse())
    e.store.set_mute(pid, 60)
    e.clk.t += 31 * 60
    e.store.check_offline()
    e.clk.t += 40 * 60
    e.n.flush()
    assert len(e.tg.sent()) == 1 and "нет связи" in e.tg.sent()[0], "после тишины — итог; напоминание сразу следом не нужно"
    e.clk.t += 3600 + 21600 + 100
    e.n.flush()
    assert len(e.tg.sent()) == 2 and "Всё ещё нет пульса" in e.tg.sent()[1]


def test_event_taken_out_of_the_queue_by_mute_is_counted_in_the_summary(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    e.store.ingest(pid, pulse([mute_alert("a1", e.clk.t - 20)]))        # лежит в очереди (скажем, Telegram не отвечал)
    e.clk.t += 100
    e.store.set_mute(pid, 60)
    e.n.flush()
    assert e.tg.sent() == []
    e.clk.t += 3601
    e.store.ingest(pid, pulse())
    e.n.flush()
    assert len(e.tg.sent()) == 1 and "Пока молчали: 1 событие" in e.tg.sent()[0]


def test_summary_waits_between_attempts_and_respects_retry_after(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    e.store.ingest(pid, pulse())
    e.store.set_mute(pid, 5)
    e.clk.t += 301
    e.tg.mode = "429"
    e.tg.retry_after = 200
    e.n.flush()
    n = len([r for r in e.tg.requests if r["method"] == "sendMessage"])
    e.tg.mode = "ok"
    e.clk.t += 100
    e.n.flush()
    assert len([r for r in e.tg.requests if r["method"] == "sendMessage"]) == n, "Telegram просил подождать 200 с"
    e.clk.t += 101
    e.n.flush()
    assert len(e.tg.sent()) == 1


def test_broken_mute_summary_does_not_stop_other_messages(env, monkeypatch):
    e = env
    pid, _ = e.store.create_plant("Кир")
    monkeypatch.setattr(e.store, "mute_finished", lambda now: 1 / 0)
    e.store.ingest(pid, pulse([mute_alert("a1", e.clk.t - 20)]))
    e.n.flush()
    assert len(e.tg.sent()) == 1 and "экспозиция" in e.tg.sent()[0]


def test_mute_record_without_from_is_ignored(env):
    e = env
    pid, _ = e.store.create_plant("Кир")
    e.store.meta_set("mute:%d" % pid, '{"until": 5}')
    assert e.store.mute_until(pid) is None and e.store.mute_finished(e.clk.t) == []
    e.n.flush()


def test_mute_until_morning_follows_the_configured_summary_time(tmp_path):
    clk = Clock()
    store = ms.Store(tmp_path / "m2.db", clock=clk, notify={"fresh_s": 900, "morning": (7, 30)})
    try:
        pid, _ = store.create_plant("Кир")
        lt = time.localtime(store.set_mute(pid, "morning"))
        assert (lt.tm_hour, lt.tm_min) == (7, 30)
    finally:
        store.close()
    src = (MS / "server.py").read_text(encoding="utf-8")
    assert '"morning": tg.get("morning")' in src, "общее время «утра» должно доходить до хранилища"


def test_panel_does_not_collapse_the_mute_list_while_it_is_open():
    assert "select[data-mute]" in PANEL and "focusout" in PANEL
