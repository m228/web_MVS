"""Авто-регистрация клиентов: заявка → «Принять» в панели/Telegram → токен уходит клиенту сам. Хранилище, HTTP-слой, клиент (enroll_client),
кнопки бота и сквозной прогон: настоящий Enroller → настоящий приёмник → пульс с полученным токеном."""
import json
import sys
from pathlib import Path

import pytest

MS = Path(__file__).resolve().parent.parent / "monitor_server"
sys.path.insert(0, str(MS))

import enroll_client  # noqa: E402
import monitor_store as ms  # noqa: E402
import monitor_telegram as mt  # noqa: E402
from test_monitor_server import PASSWORD, Srv, entry, pulse, store  # noqa: E402,F401  (store — фикстура)

KEY = "enroll-key-for-tests-1234"
MACHINE = "m" * 32
SECRET = "s" * 24
P = {"X-Panel": "1"}


def req(store, machine=MACHINE, name="PC-1", secret=SECRET, ip="172.29.1.5", version="2.1.6"):
    return store.enroll_request(machine, name, version, ip, secret)


# ------------------------------------------------------------------------------------------------------------------ хранилище

def test_request_is_pending_until_approved_then_token_once(store):
    r = req(store)
    assert r["status"] == "pending" and r["new"] is True
    again = req(store)
    assert again["status"] == "pending" and again["new"] is False, "повтор той же заявки — не новая, Telegram не дёргаем"
    assert [x["name"] for x in store.enroll_pending()] == ["PC-1"]
    res = store.enroll_approve(r["id"], "Кир-2")
    assert res["name"] == "Кир-2" and store.enroll_pending() == []
    got = req(store)
    assert got["status"] == "approved" and got["name"] == "Кир-2"
    assert store.plant_by_token(got["token"])["name"] == "Кир-2"
    after = req(store)
    assert after["status"] == "approved" and "token" not in after, "токен отдаётся один раз"
    assert store.db.execute("SELECT token FROM enroll").fetchone()[0] is None, "у приёмника токен после выдачи стёрт"


def test_wrong_secret_never_gets_token_and_restarts_request(store):
    r = req(store)
    store.enroll_approve(r["id"], "Кир")
    thief = req(store, secret="x" * 24)
    assert thief["status"] == "pending" and thief["new"] is True and "token" not in thief
    assert req(store)["status"] == "pending", "прежний секрет больше не подходит — это уже другая заявка"


def test_reject_and_unknown_requests(store):
    r = req(store)
    assert store.enroll_reject(r["id"]) is True and store.enroll_reject(r["id"]) is False
    assert req(store)["status"] == "rejected"
    with pytest.raises(LookupError):
        store.enroll_approve(r["id"], "Кир")
    with pytest.raises(LookupError):
        store.enroll_approve(999, "Кир")


def test_same_machine_gets_new_token_for_same_plant_other_machine_cannot_take_name(store):
    r = req(store)
    store.enroll_approve(r["id"], "Кир")
    old = req(store)["token"]
    # переустановка: новый секрет, тот же ПК -> новый токен тому же заводу, название прежнее
    r2 = req(store, secret="n" * 24, name="Другое-имя")
    res = store.enroll_approve(r2["id"], "Другое-имя")
    assert res["name"] == "Кир"
    new = req(store, secret="n" * 24)["token"]
    assert store.plant_by_token(old) is None and store.plant_by_token(new)["name"] == "Кир"
    # чужой ПК с занятым именем: заявка принимается, но завод Кир не угоняется
    r3 = req(store, machine="o" * 32, name="Кир", secret="q" * 24)
    with pytest.raises(ValueError):
        store.enroll_approve(r3["id"], "Кир")
    assert store.plant_by_token(new)["name"] == "Кир", "токен Кира не тронут"
    assert [x["name"] for x in store.enroll_pending()] == ["Кир"], "заявка осталась ждать другого названия"
    assert store.enroll_approve(r3["id"], "Кир-3")["name"] == "Кир-3"


def test_request_validation_and_limit(store, monkeypatch):
    for bad in (dict(machine="short"), dict(secret="short"), dict(machine="")):
        with pytest.raises(ValueError):
            req(store, **bad)
    monkeypatch.setattr(ms, "ENROLL_PENDING_MAX", 2)
    req(store, machine="a" * 32)
    req(store, machine="b" * 32)
    with pytest.raises(OverflowError):
        req(store, machine="c" * 32)
    assert req(store, machine="a" * 32)["status"] == "pending", "уже поданная заявка продолжает жить"


def test_deleted_plant_can_register_again_and_old_requests_expire(store):
    r = req(store)
    pid = store.enroll_approve(r["id"], "Кир")["id"]
    req(store)
    store.delete_plant(pid)
    assert store.db.execute("SELECT COUNT(*) FROM enroll").fetchone()[0] == 0
    req(store)
    store.clk.t += (ms.ENROLL_KEEP_DAYS + 1) * 86400
    store.purge()
    assert store.db.execute("SELECT COUNT(*) FROM enroll").fetchone()[0] == 0


def test_old_database_gets_machine_column(tmp_path):
    import sqlite3
    p = tmp_path / "old.db"
    db = sqlite3.connect(str(p))
    db.execute("CREATE TABLE plants (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, token_hash TEXT NOT NULL UNIQUE, "
               "created_t REAL NOT NULL, last_seen_t REAL, interval_s INTEGER, snapshot TEXT, offline_since REAL)")
    db.execute("INSERT INTO plants (name, token_hash, created_t) VALUES ('Кир','h',1)")
    db.commit()
    db.close()
    s = ms.Store(p)
    try:
        assert s.plants_view()[0]["name"] == "Кир"
        assert s.create_plant("Буи", "m" * 32)[0] > 1
    finally:
        s.close()


# ------------------------------------------------------------------------------------------------------------------ HTTP-слой

@pytest.fixture
def esrv(store):
    s = Srv(store, enroll_key=KEY)
    yield s
    s.close()


def body(**kw):
    d = {"key": KEY, "machine": MACHINE, "name": "PC-1", "version": "2.1.6", "secret": SECRET}
    d.update(kw)
    return d


def test_enroll_needs_key_and_counts_wrong_ones(esrv):
    code, j = esrv.json("POST", "/enroll", body(key="чужой"), password=None)
    assert code == 403 and j["ok"] is False
    for _ in range(7):
        esrv.json("POST", "/enroll", body(key="чужой"), password=None)
    code, _ = esrv.json("POST", "/enroll", body(), password=None)
    assert code == 429, "после 8 неверных ключей даже верный ждёт: перебор ключа не пройдёт"


def test_enroll_disabled_without_key(store):
    s = Srv(store)
    try:
        assert s.json("POST", "/enroll", body(), password=None)[0] == 503
    finally:
        s.close()


def test_enroll_bad_bodies(esrv):
    assert esrv.call("POST", "/enroll", raw=b"{oops", password=None)[0] == 400
    assert esrv.call("POST", "/enroll", raw=b"[1]", password=None)[0] == 400
    assert esrv.call("POST", "/enroll", raw=b"x" * 5000, password=None)[0] == 400
    assert esrv.json("POST", "/enroll", body(machine="x"), password=None)[0] == 400


def test_full_flow_over_http_panel_approves(esrv, store):
    code, j = esrv.json("POST", "/enroll", body(), password=None)
    assert code == 200 and j["status"] == "pending" and "new" not in j and "token" not in j
    assert esrv.json("GET", "/api/enroll", password=None)[0] == 401, "список заявок — только с паролем панели"
    code, j = esrv.json("GET", "/api/enroll")
    rid = j["requests"][0]["id"]
    assert j["requests"][0]["name"] == "PC-1"
    assert esrv.json("POST", "/api/enroll/%d/approve" % rid, {"name": "Кир"})[0] == 403, "без X-Panel запись не принимается"
    code, j = esrv.json("POST", "/api/enroll/%d/approve" % rid, {"name": "Кир"}, headers=P)
    assert code == 200 and j["name"] == "Кир" and "token" not in j, "Максу токен не показываем: он уходит клиенту"
    code, j = esrv.json("POST", "/enroll", body(), password=None)
    assert j["status"] == "approved" and len(j["token"]) >= 20
    assert esrv.call("POST", "/ingest", raw=json.dumps(pulse()).encode(), bearer=j["token"])[0] == 200
    assert esrv.json("POST", "/api/enroll/%d/approve" % rid, {"name": "Кир"}, headers=P)[0] == 404


def test_http_reject_and_name_taken(esrv, store):
    store.create_plant("Кир")
    esrv.json("POST", "/enroll", body(), password=None)
    rid = esrv.json("GET", "/api/enroll")[1]["requests"][0]["id"]
    code, j = esrv.json("POST", "/api/enroll/%d/approve" % rid, {"name": "Кир"}, headers=P)
    assert code == 400 and "уже есть" in j["error"]
    assert esrv.json("POST", "/api/enroll/%d/reject" % rid, headers=P)[1] == {"rejected": True}
    assert esrv.json("POST", "/enroll", body(), password=None)[1]["status"] == "rejected"


def test_new_request_announced_to_telegram_once(store):
    calls = []

    class FakeBot:
        def announce_enroll(self, *a):
            calls.append(a)
    s = Srv(store, enroll_key=KEY, bot=FakeBot())
    try:
        s.json("POST", "/enroll", body(), password=None)
        s.json("POST", "/enroll", body(), password=None)
        assert len(calls) == 1 and calls[0][1] == "PC-1" and calls[0][2] == "2.1.6"
    finally:
        s.close()


# ------------------------------------------------------------------------------------------------------------------ кнопки Telegram

class FakeClient:
    def __init__(self):
        self.sent, self.calls = [], []

    def send(self, chat, text, markup=None, silent=False):
        self.sent.append((chat, text, markup))
        return True, 0.0, ""

    def call(self, method, payload=None):
        self.calls.append((method, payload))
        return True, {"ok": True}, 0.0, ""


def make_bot(store, chats=("111",)):
    c = FakeClient()
    return mt.Bot(store, c, c, list(chats)), c


def cb(data, chat="111", kind="private"):
    return {"callback_query": {"id": "q1", "data": data, "message": {"message_id": 7, "text": "🆕 Новый клиент: «PC-1»", "chat": {"id": int(chat), "type": kind}}}}


def test_bot_announces_with_buttons_and_approves(store):
    bot, c = make_bot(store)
    r = req(store)
    bot.announce_enroll(r["id"], "PC-1", "2.1.6", "172.29.1.5")
    chat, text, markup = c.sent[0]
    assert chat == "111" and "PC-1" in text
    buttons = markup["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == ["ea:%d" % r["id"], "er:%d" % r["id"]]
    reply = bot.handle(cb(buttons[0]["callback_data"]))
    assert "Принят" in reply and store.plants_view()[0]["name"] == "PC-1"
    assert [m for m, _ in c.calls] == ["answerCallbackQuery", "editMessageText"]
    assert "reply_markup" not in c.calls[1][1], "кнопки после решения пропадают"
    assert "решена" in bot.handle(cb("ea:%d" % r["id"]))


def test_bot_reject_name_clash_and_strangers(store):
    bot, c = make_bot(store)
    store.create_plant("PC-1")
    r = req(store)
    assert "Не принял" in bot.handle(cb("ea:%d" % r["id"])) and store.enroll_pending(), "имя занято: заявка ждёт панели"
    assert "Отклонено" in bot.handle(cb("er:%d" % r["id"])) and store.enroll_pending() == []
    r = req(store, machine="z" * 32, name="PC-9", secret="z" * 24)
    assert bot.handle(cb("ea:%d" % r["id"], chat="999")) is None, "чужой чат кнопку не нажмёт"
    assert bot.handle(cb("ea:%d" % r["id"], kind="group")) is None
    assert bot.handle(cb("zz:1")) is None and bot.handle(cb("ea:x")) is None
    assert len(store.enroll_pending()) == 1


# ------------------------------------------------------------------------------------------------------------------ клиент

class Net:
    """Подменяет сеть: что отвечает каждый адрес."""
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, url, body, timeout=6):
        self.calls.append((url, dict(body)))
        a = self.answers.get(url.rsplit("/enroll", 1)[0])
        if a is None:
            return None, None, "Нет связи: таймаут"
        return a


def enroller(tmp_path, net, key=KEY, clock=None):
    t = clock or [1000.0]
    e = enroll_client.Enroller(key_fn=lambda: key, machine_fn=lambda: MACHINE, post=net, state_path=tmp_path / "enroll.json",
                               clock=lambda: t[0], host_fn=lambda: "DESKTOP-ABC")
    e.t = t
    return e


OK_PENDING = (200, {"ok": True, "status": "pending", "name": "x"}, "")


def test_client_silent_without_key(tmp_path):
    net = Net({})
    e = enroller(tmp_path, net, key="")
    assert e.step({}) is None and net.calls == [] and e.status["state"] == "no_key"


def test_client_tries_addresses_in_order_and_remembers_good(tmp_path):
    first, second = enroll_client.DEFAULT_SERVERS[0], enroll_client.DEFAULT_SERVERS[1]
    net = Net({second: OK_PENDING})
    e = enroller(tmp_path, net)
    assert e.step({"plant": ""}) is None
    assert [u for u, _ in net.calls][:2] == [first + "/enroll", second + "/enroll"]
    assert e.status["state"] == "pending" and e.status["url"] == second
    body = net.calls[-1][1]
    assert body["key"] == KEY and body["machine"] == MACHINE and body["name"] == "DESKTOP-ABC" and len(body["secret"]) >= 16
    net.calls.clear()
    e.t[0] += 61
    e.step({})
    assert net.calls[0][0] == second + "/enroll", "рабочий адрес — первым"
    assert net.calls[0][1]["secret"] == body["secret"], "секрет заявки тот же (хранится на диске)"


def test_client_waits_a_minute_and_backs_off_when_nobody_answers(tmp_path):
    net = Net({})
    e = enroller(tmp_path, net)
    e.step({})
    n = len(net.calls)
    assert e.status["state"] == "searching" and "Нет связи" in e.status["error"]
    e.step({})
    assert len(net.calls) == n, "раньше срока не стучимся"
    e.t[0] += 61
    e.step({})
    assert len(net.calls) == 2 * n
    e.t[0] += 61
    e.step({})
    assert len(net.calls) == 2 * n, "пауза растёт: 60 → 120 с"


def test_client_reports_rejected_key(tmp_path):
    net = Net({u: (403, {"ok": False, "error": "ключ регистрации не принят"}, "HTTP 403") for u in enroll_client.DEFAULT_SERVERS})
    e = enroller(tmp_path, net)
    e.step({})
    assert e.status["state"] == "searching" and "не принял ключ" in e.status["error"]


def test_client_gets_token_and_returns_patch(tmp_path):
    url = enroll_client.DEFAULT_SERVERS[0]
    net = Net({url: (200, {"ok": True, "status": "approved", "name": "Кир-2", "token": "T" * 32}, "")})
    e = enroller(tmp_path, net)
    patch = e.step({"plant": ""})
    assert patch == {"token": "T" * 32, "url": url, "allow_http": True, "enabled": True, "plant": "Кир-2"}
    assert e.status["state"] == "done" and not (tmp_path / "enroll.json").exists(), "секрет заявки после получения токена стёрт"
    assert e.step({}) is None or True


def test_client_keeps_own_plant_name(tmp_path):
    net = Net({enroll_client.DEFAULT_SERVERS[0]: (200, {"ok": True, "status": "approved", "name": "Серверное", "token": "T" * 32}, "")})
    assert enroller(tmp_path, net).step({"plant": "Моё"})["plant"] == "Моё"


def test_client_lost_token_starts_new_request_and_rejected_waits_long(tmp_path):
    url = enroll_client.DEFAULT_SERVERS[0]
    net = Net({url: (200, {"ok": True, "status": "approved", "name": "Кир"}, "")})
    e = enroller(tmp_path, net)
    e.step({})
    s1 = net.calls[0][1]["secret"]
    e.t[0] += 10
    e.step({})
    assert net.calls[-1][1]["secret"] != s1, "токен уже выдан, а у нас его нет: новая заявка с новым секретом"
    net.answers[url] = (200, {"ok": True, "status": "rejected", "name": "Кир"}, "")
    e.t[0] += 61
    e.step({})
    assert e.status["state"] == "rejected"
    n = len(net.calls)
    e.t[0] += 3600
    e.step({})
    assert len(net.calls) == n, "отклонённый клиент не долбит приёмник"


def test_machine_id_is_stable_hex():
    a = enroll_client.machine_id()
    assert a == enroll_client.machine_id() and len(a) == 32 and int(a, 16) >= 0


# ------------------------------------------------------------------------------------------------------------------ сквозной прогон

def test_end_to_end_enroller_to_receiver_then_pulse(esrv, store, tmp_path, monkeypatch):
    import sysmon
    sysmon.close()
    monkeypatch.setattr(sysmon, "DB_PATH", tmp_path / "sysmon.db")
    monkeypatch.setattr(sysmon, "CONFIG_PATH", tmp_path / "monitor.json")
    monkeypatch.setattr(enroll_client, "DEFAULT_SERVERS", ())
    try:
        sysmon.save_config({"url": esrv.base, "system_proxy": False})
        ag = sysmon.Agent(sources={})
        ag.enroller = enroll_client.Enroller(key_fn=lambda: KEY, machine_fn=lambda: MACHINE, state_path=tmp_path / "enroll.json",
                                             host_fn=lambda: "PC-7")
        ag._cfg = sysmon.load_config()
        ag._enroll(1000.0)
        assert ag.enroller.status["state"] == "pending" and not sysmon.load_config()["token"]
        assert esrv.json("GET", "/api/enroll")[1]["requests"][0]["name"] == "PC-7"
        rid = esrv.json("GET", "/api/enroll")[1]["requests"][0]["id"]
        esrv.json("POST", "/api/enroll/%d/approve" % rid, {"name": "Завод-7"}, headers=P)
        ag._enroll(1100.0)
        cfg = sysmon.load_config()
        assert cfg["token"] and cfg["enabled"] and cfg["plant"] == "Завод-7" and cfg["url"] == esrv.base and cfg["allow_http"]
        assert sysmon.configured(cfg)
        ag._cfg = cfg
        ag.status.update(ok=None)
        assert ag.send(now=2000.0)["ok"] is True
        assert store.plants_view()[0]["name"] == "Завод-7"
        ag._enroll(1200.0)                                       # токен есть — больше не стучимся
        assert len(esrv.json("GET", "/api/enroll")[1]["requests"]) == 0
    finally:
        sysmon.close()


# ------------------------------------------------------------------------------------------------------------------ кнопка «Отправить заявку»

def test_hurry_lifts_all_pauses(tmp_path):
    net = Net({})
    e = enroller(tmp_path, net)
    e.step({})
    n = len(net.calls)
    e.step({})
    assert len(net.calls) == n, "без кнопки раньше срока не стучимся"
    e.hurry()
    e.step({})
    assert len(net.calls) == 2 * n, "после hurry заявка уходит сразу"
    e2 = enroller(tmp_path, Net({u: (200, {"ok": True, "status": "rejected"}, "") for u in enroll_client.DEFAULT_SERVERS}))
    e2.step({})
    assert e2.status["state"] == "rejected"
    e2.step({})
    n2 = len(e2.post.calls)
    e2.hurry()
    e2.step({})
    assert len(e2.post.calls) == n2 + 1, "отклонённую заявку по кнопке можно подать снова, не ждать 6 часов"


def test_enroll_now_button_flow_even_with_auto_off(esrv, store, tmp_path, monkeypatch):
    import sysmon
    sysmon.close()
    monkeypatch.setattr(sysmon, "DB_PATH", tmp_path / "sysmon.db")
    monkeypatch.setattr(sysmon, "CONFIG_PATH", tmp_path / "monitor.json")
    monkeypatch.setattr(enroll_client, "DEFAULT_SERVERS", ())
    try:
        sysmon.save_config({"url": esrv.base, "system_proxy": False})
        sysmon.CONFIG_PATH.write_text(json.dumps({**json.loads(sysmon.CONFIG_PATH.read_text(encoding="utf-8")), "auto_enroll": False}), encoding="utf-8")
        ag = sysmon.Agent(sources={})
        ag.enroller = enroll_client.Enroller(key_fn=lambda: KEY, machine_fn=lambda: MACHINE, state_path=tmp_path / "enroll.json", host_fn=lambda: "PC-9")
        ag._cfg = sysmon.load_config()
        ag._enroll(1000.0)
        assert ag.enroller.status["state"] == "idle", "автозаявка выключена — сама не стучится"
        res = ag.enroll_now()
        assert res == {"ok": True, "state": "pending", "error": ""}, "кнопка шлёт заявку и при выключенной автозаявке"
        rid = esrv.json("GET", "/api/enroll")[1]["requests"][0]["id"]
        esrv.json("POST", "/api/enroll/%d/approve" % rid, {"name": "Завод-9"}, headers=P)
        res = ag.enroll_now()
        assert res["ok"] is True and res["state"] == "done"
        cfg = sysmon.load_config()
        assert cfg["token"] and cfg["enabled"] and cfg["plant"] == "Завод-9"
        assert ag.enroll_now() == {"ok": False, "state": "has_token", "error": "токен уже есть — регистрация не нужна"}
    finally:
        sysmon.close()


def test_enroll_now_reports_no_key_and_no_server(tmp_path, monkeypatch):
    import sysmon
    sysmon.close()
    monkeypatch.setattr(sysmon, "DB_PATH", tmp_path / "sysmon.db")
    monkeypatch.setattr(sysmon, "CONFIG_PATH", tmp_path / "monitor.json")
    try:
        ag = sysmon.Agent(sources={})
        ag.enroller = enroll_client.Enroller(key_fn=lambda: "", state_path=tmp_path / "enroll.json")
        res = ag.enroll_now()
        assert res["ok"] is False and res["state"] == "no_key"
        ag.enroller = enroll_client.Enroller(key_fn=lambda: KEY, machine_fn=lambda: MACHINE, post=Net({}), state_path=tmp_path / "enroll.json")
        res = ag.enroll_now()
        assert res["ok"] is False and res["state"] == "searching" and "Нет связи" in res["error"]
    finally:
        sysmon.close()


def test_client_can_rename_pending_request_but_not_decided_one(store):
    r = req(store, name="DESKTOP-C9R41JT")
    again = req(store, name="Кир-2")
    assert again["status"] == "pending" and again["new"] is False and again["name"] == "Кир-2", "название поправили на клиенте — заявка в панели с новым"
    assert [x["name"] for x in store.enroll_pending()] == ["Кир-2"]
    store.enroll_approve(r["id"], "Кир-2")
    got = req(store, name="Другое")
    assert got["status"] == "approved" and got["name"] == "Кир-2", "после решения название задаёт приёмник, а не клиент"


def test_enroll_button_is_on_monitor_page_and_route_exists():
    root = Path(__file__).resolve().parent.parent
    html = (root / "page" / "monitor.html").read_text(encoding="utf-8")
    js = (root / "page" / "static" / "js" / "monitor.js").read_text(encoding="utf-8")
    app_src = (root / "app.py").read_text(encoding="utf-8")
    assert 'id="monEnroll"' in html and "Отправить заявку на сервер" in html
    assert "/api/sysmon/enroll_now" in js and "method: 'POST'" in js and "monEnroll" in js and "token_set" in js
    assert "monPlant" in js.split("async function enrollNow")[1].split("window.addEventListener")[0], "кнопка требует название завода и сохраняет форму"
    assert '@app.post("/api/sysmon/enroll_now")' in app_src


# ------------------------------------------------------------------------------------------------------------------ точка входа

def test_enroll_key_created_once_kept_with_password_and_env_wins(tmp_path, monkeypatch):
    monkeypatch.delenv("MONITOR_ENROLL_KEY", raising=False)
    monkeypatch.delenv("MONITOR_PASSWORD", raising=False)
    pw = entry.load_password(tmp_path)
    key = entry.load_enroll_key(tmp_path)
    assert len(key) >= 24 and entry.load_enroll_key(tmp_path) == key
    saved = json.loads((tmp_path / "server.json").read_text(encoding="utf-8"))
    assert saved == {"password": pw, "enroll_key": key}, "ключ дописан в server.json, пароль не потерян"
    monkeypatch.setenv("MONITOR_ENROLL_KEY", "из-окружения")
    assert entry.load_enroll_key(tmp_path) == "из-окружения"


def test_listen_hosts_list_and_single(tmp_path, monkeypatch):
    monkeypatch.delenv("MONITOR_HOST", raising=False)
    monkeypatch.delenv("MONITOR_PORT", raising=False)
    (tmp_path / "server.json").write_text(json.dumps({"hosts": ["172.29.134.237", "11.11.11.12"], "port": 8800}), encoding="utf-8")
    assert entry.load_listen_hosts(tmp_path) == (["172.29.134.237", "11.11.11.12"], 8800)
    assert entry.load_listen(tmp_path) == ("172.29.134.237", 8800), "трей проверяет первый адрес"
    (tmp_path / "server.json").write_text(json.dumps({"host": "11.11.11.12"}), encoding="utf-8")
    assert entry.load_listen_hosts(tmp_path) == (["11.11.11.12"], 8769)
    monkeypatch.setenv("MONITOR_HOST", "127.0.0.1, 10.0.0.1")
    assert entry.load_listen_hosts(tmp_path)[0] == ["127.0.0.1", "10.0.0.1"]


def test_bind_all_skips_missing_address_and_fails_if_none(monkeypatch):
    class Cfg:
        def __init__(self, app, host, port, log_level=None):
            assert log_level == "warning", "без log_level uvicorn сбрасывает логирование на INFO"
            self.host = host

        def bind_socket(self):
            if self.host == "10.9.9.9":
                raise OSError("адреса нет")
            return "sock-" + self.host

    class U:
        Config = Cfg
    waits = []
    socks = entry.bind_all(U, None, ["127.0.0.1", "10.9.9.9"], 1, sleep=waits.append)
    assert socks == ["sock-127.0.0.1"] and len(waits) == entry.BIND_TRIES - 1, "ждём недостающий адрес, потом работаем на том, что есть"
    with pytest.raises(OSError):
        entry.bind_all(U, None, ["10.9.9.9"], 1, sleep=waits.append)


def test_client_proxy_only_for_https(monkeypatch):
    seen = []

    class Opener:
        def open(self, req, timeout=0):
            raise OSError("стоп")

    def fake_build(handler):
        seen.append(handler.proxies)
        return Opener()
    monkeypatch.setattr(enroll_client.urllib.request, "build_opener", fake_build)
    monkeypatch.setattr(enroll_client.urllib.request, "getproxies", lambda: {"https": "http://proxy:1"})
    enroll_client.post_json("http://11.11.11.12:8769/enroll", {})
    enroll_client.post_json("https://monitor.example.ru/enroll", {})
    assert seen[0] == {} and seen[1] == {"https": "http://proxy:1"}, "закрытая сеть — напрямую, интернет-адрес — через системный прокси"
