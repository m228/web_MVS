"""Команды и настройки «вниз»: «Обновить» и «что присылать» — хранилище, /wait, API панели, кнопки Telegram, клиент (sysmon + release_push)."""
import json
import sys
import threading
import time
from pathlib import Path

import pytest

MS = Path(__file__).resolve().parent.parent / "monitor_server"
sys.path.insert(0, str(MS))

import monitor_app  # noqa: E402
import monitor_store as ms  # noqa: E402
import monitor_telegram as mt  # noqa: E402
import release_push  # noqa: E402
import sysmon  # noqa: E402
from test_monitor_server import Srv, store  # noqa: E402,F401  (store — фикстура)

P = {"X-Panel": "1"}


class Rel:
    latest = "2.1.8"


# ------------------------------------------------------------------------------------------------------------------ хранилище

def test_config_merge_bumps_version_and_validates(store):
    pid, _ = store.create_plant("Кир")
    assert store.plant_config(pid) == {"v": 0, "config": {}}
    r = store.set_plant_config(pid, {"send_positions": False, "interval_s": 600})
    assert r == {"v": 1, "config": {"send_positions": False, "interval_s": 600}}
    assert store.set_plant_config(pid, {"send_camera": False})["config"] == {"send_positions": False, "interval_s": 600, "send_camera": False}
    assert store.set_plant_config(pid, {"send_camera": False})["v"] == 2, "то же значение версию не поднимает"
    for bad in ({"interval_s": 5}, {"interval_s": 99999}, {"interval_s": True}, {"send_micro": "нет"}, {"gap_far_um": 10}, {}, None, [1]):
        with pytest.raises(ValueError):
            store.set_plant_config(pid, bad)
    with pytest.raises(LookupError):
        store.set_plant_config(999, {"send_micro": True})


def test_commands_queue_dedup_take_once_and_expire(store):
    pid, _ = store.create_plant("Кир")
    c1 = store.command_add(pid, "update")
    assert store.command_add(pid, "update") == c1, "вторая такая же, пока первую не забрали, не создаётся"
    assert store.commands_pending(pid) == 1
    assert store.commands_take(pid) == [{"id": c1, "kind": "update"}]
    assert store.commands_take(pid) == [] and store.commands_pending(pid) == 0
    assert store.command_add(pid, "update") != c1, "забрана — можно ставить новую"
    store.clk.t += ms.COMMAND_TTL_S + 1
    assert store.commands_take(pid) == [], "за сутки не забрали — устарела"
    with pytest.raises(ValueError):
        store.command_add(pid, "format-disk")
    with pytest.raises(LookupError):
        store.command_add(999, "update")


def test_plants_view_shows_config_and_pending_command_and_delete_cleans(store):
    pid, _ = store.create_plant("Кир")
    store.set_plant_config(pid, {"send_micro": False})
    store.command_add(pid, "update")
    v = store.plants_view()[0]
    assert v["config"] == {"v": 1, "config": {"send_micro": False}} and v["cmd_pending"] == 1
    store.delete_plant(pid)
    assert store.db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 0


def test_old_database_gets_config_columns(tmp_path):
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
        assert s.plant_config(1) == {"v": 0, "config": {}}
        assert s.set_plant_config(1, {"send_camera": False})["v"] == 1
    finally:
        s.close()


# ------------------------------------------------------------------------------------------------------------------ HTTP

@pytest.fixture
def srv(store):
    s = Srv(store, releases=Rel())
    yield s
    s.close()


def wait(srv, token, cv=0, known="2.1.8"):
    return srv.json("GET", "/wait?known=%s&cv=%d" % (known, cv), password=None, bearer=token)


def test_wait_hands_out_command_at_once_and_only_once(store, srv):
    pid, token = store.create_plant("Кир")
    store.command_add(pid, "update")
    t0 = time.monotonic()
    code, j = wait(srv, token)
    assert code == 200 and j["commands"] == [{"id": 1, "kind": "update"}] and time.monotonic() - t0 < 2
    assert store.commands_pending(pid) == 0


def test_wait_sends_config_only_when_version_differs(store, srv, monkeypatch):
    monkeypatch.setattr(monitor_app, "WAIT_S", 1)
    pid, token = store.create_plant("Кир")
    store.set_plant_config(pid, {"send_camera": False, "interval_s": 900})
    j = wait(srv, token, cv=-1)[1]
    assert j["config_v"] == 1 and j["config"] == {"send_camera": False, "interval_s": 900}
    j = wait(srv, token, cv=1)[1]
    assert j["config_v"] == 1 and "config" not in j and j["commands"] == [], "версия совпала — ждём и ничего не шлём"


def test_wait_wakes_up_on_command_added_during_hold(store, srv, monkeypatch):
    monkeypatch.setattr(monitor_app, "WAIT_S", 10)
    pid, token = store.create_plant("Кир")
    threading.Timer(1.2, lambda: store.command_add(pid, "update")).start()
    t0 = time.monotonic()
    j = wait(srv, token)[1]
    assert j["commands"] and 0.8 < time.monotonic() - t0 < 5, "команда доезжает за секунды, не ждёт конца WAIT_S"


def test_panel_api_update_update_all_and_config(store, srv):
    a, _ = store.create_plant("Кир")
    b, _ = store.create_plant("Буи")
    assert srv.json("POST", "/api/plants/%d/update" % a, password=None)[0] == 401
    assert srv.json("POST", "/api/plants/%d/update" % a)[0] == 403, "без X-Panel запись не принимается"
    assert srv.json("POST", "/api/plants/%d/update" % a, headers=P)[0] == 200
    assert srv.json("POST", "/api/plants/999/update", headers=P)[0] == 404
    assert srv.json("POST", "/api/update_all", headers=P)[1] == {"sent": 2}
    assert store.commands_pending(a) == 1 and store.commands_pending(b) == 1, "у Кира команда та же, не задвоилась"
    code, j = srv.json("POST", "/api/plants/%d/config" % a, {"send_positions": False}, headers=P)
    assert code == 200 and j == {"v": 1, "config": {"send_positions": False}}
    assert srv.json("GET", "/api/plants/%d/config" % a)[1] == j
    assert srv.json("POST", "/api/plants/%d/config" % a, {"gap_far_um": 1}, headers=P)[0] == 400
    assert srv.json("POST", "/api/plants/%d/config" % a, headers=P, raw=b"{oops")[0] == 400
    assert srv.json("POST", "/api/plants/999/config", {"send_micro": True}, headers=P)[0] == 404
    assert srv.json("GET", "/api/plants/999/config")[0] == 404


# ------------------------------------------------------------------------------------------------------------------ Telegram

class FakeClient:
    def __init__(self):
        self.sent, self.calls = [], []

    def send(self, chat, text, markup=None, silent=False):
        self.sent.append((chat, text, markup))
        return True, 0.0, ""

    def call(self, method, payload=None):
        self.calls.append((method, payload))
        return True, {"ok": True}, 0.0, ""


def bot_for(store, chats=("111",)):
    c = FakeClient()
    return mt.Bot(store, c, c, list(chats)), c


def cb(data, chat="111", kind="private", mid=7):
    return {"callback_query": {"id": "q1", "data": data, "message": {"message_id": mid, "text": "x", "chat": {"id": int(chat), "type": kind}}}}


def toast(c):
    return [p["text"] for m, p in c.calls if m == "answerCallbackQuery"][-1]


def test_update_button_queues_command_and_confirms(store):
    pid, _ = store.create_plant("Кир")
    bot, c = bot_for(store)
    bot.handle(cb("up:%d" % pid))
    assert store.commands_pending(pid) == 1
    assert "Кир" in c.sent[-1][1] and "Обновить" in c.sent[-1][1] and "Кир" in toast(c)
    bot.handle(cb("up:999"))
    assert toast(c) == "Завода нет."


def test_update_all_button(store):
    a, _ = store.create_plant("Кир")
    b, _ = store.create_plant("Буи")
    bot, c = bot_for(store)
    bot.handle(cb("ua"))
    assert store.commands_pending(a) == 1 and store.commands_pending(b) == 1
    assert "Буи" in c.sent[-1][1] and "Кир" in c.sent[-1][1]


def test_data_menu_toggle_and_interval(store):
    pid, _ = store.create_plant("Кир")
    bot, c = bot_for(store)
    bot.handle(cb("cf:%d" % pid))
    text, markup = c.sent[-1][1], c.sent[-1][2]
    assert "Кир" in text and "Тревоги приходят всегда" in text
    rows = markup["inline_keyboard"]
    assert [b["callback_data"] for r in rows[:2] for b in r] == ["tg:%d:%s" % (pid, k) for k in ms.CONFIG_FLAGS]
    assert all(b["text"].startswith("✅") for r in rows[:2] for b in r), "по умолчанию присылается всё"
    assert [b["callback_data"] for b in rows[-1]] == ["iv:%d:%d" % (pid, m) for m in (1, 5, 15, 30)]
    bot.handle(cb("tg:%d:send_positions" % pid))
    assert store.plant_config(pid)["config"] == {"send_positions": False}
    edit = [p for m, p in c.calls if m == "editMessageText"][-1]
    assert edit["message_id"] == 7 and edit["reply_markup"]["inline_keyboard"][0][0]["text"].startswith("⬜"), "кнопка сразу показывает новое состояние"
    bot.handle(cb("tg:%d:send_positions" % pid))
    assert store.plant_config(pid)["config"] == {"send_positions": True}
    bot.handle(cb("iv:%d:15" % pid))
    assert store.plant_config(pid)["config"]["interval_s"] == 900
    edit = [p for m, p in c.calls if m == "editMessageText"][-1]
    assert [b["text"] for b in edit["reply_markup"]["inline_keyboard"][-1]] == ["1 мин", "5 мин", "● 15 мин", "30 мин"]


def test_buttons_from_strangers_and_garbage_do_nothing(store):
    pid, _ = store.create_plant("Кир")
    bot, c = bot_for(store)
    assert bot.handle(cb("up:%d" % pid, chat="999")) is None and bot.handle(cb("up:%d" % pid, kind="group")) is None
    assert store.commands_pending(pid) == 0 and c.calls == []
    for bad in ("tg:%d:gap_far_um" % pid, "tg:x:send_micro", "iv:%d:abc" % pid, "iv:%d" % pid, "zz:1"):
        bot.handle(cb(bad))
    assert store.plant_config(pid) == {"v": 0, "config": {}}


def test_summary_messages_carry_buttons(store):
    pid, _ = store.create_plant("Кир")
    bot, c = bot_for(store)
    bot.handle({"message": {"chat": {"id": 111, "type": "private"}, "text": "Кир"}})
    assert [b["callback_data"] for b in c.sent[-1][2]["inline_keyboard"][0]] == ["up:%d" % pid, "cf:%d" % pid]
    bot.handle({"message": {"chat": {"id": 111, "type": "private"}, "text": "Все заводы"}})
    assert c.sent[-1][2]["inline_keyboard"][0][0]["callback_data"] == "ua"


# ------------------------------------------------------------------------------------------------------------------ клиент: настройки

@pytest.fixture
def mon(tmp_path, monkeypatch):
    sysmon.close()
    monkeypatch.setattr(sysmon, "DB_PATH", tmp_path / "sysmon.db")
    monkeypatch.setattr(sysmon, "CONFIG_PATH", tmp_path / "monitor.json")
    monkeypatch.setattr(sysmon, "REMOTE_PATH", tmp_path / "monitor_remote.json")
    yield tmp_path
    sysmon.close()


def test_remote_config_overlays_file_but_is_never_written_into_it(mon):
    assert sysmon.load_remote() == {"v": None, "config": {}}
    sysmon.save_config({"url": "http://127.0.0.1:1", "token": "T" * 32, "enabled": True, "interval_s": 300})
    sysmon.save_remote(3, {"interval_s": 900, "send_camera": False, "gap_far_um": 5, "token": "evil"})
    assert sysmon.load_remote() == {"v": 3, "config": {"interval_s": 900, "send_camera": False}}, "лишние ключи отброшены"
    cfg = sysmon.load_config()
    assert cfg["interval_s"] == 900 and cfg["send_camera"] is False and cfg["token"] == "T" * 32 and cfg["gap_far_um"] == 150
    sysmon.save_config({"plant": "Кир"})                          # правка на странице не должна утащить присланное в monitor.json
    saved = json.loads((mon / "monitor.json").read_text(encoding="utf-8"))
    assert saved["interval_s"] == 300 and saved["send_camera"] is True and saved["plant"] == "Кир"
    assert sysmon.public_config()["remote_keys"] == ["interval_s", "send_camera"]


def test_remote_file_broken_is_ignored(mon):
    (mon / "monitor_remote.json").write_text("{битый", encoding="utf-8")
    assert sysmon.load_remote()["v"] is None and sysmon.load_config()["send_camera"] is True


SRC = {"cfg": lambda: {"camera_serial": "S1", "cv": {"enabled": True}}, "micro_on": lambda: True,
       "state": lambda: {"stage": 7, "stage_ok": True, "cyclic": True}, "status": lambda: {"connected": True}, "ext": lambda: {"uptime_s": 1000},
       "cv": lambda: {"state": "ok"}, "services": lambda: {"cv": "healthy"},
       "camera": lambda: {"serial": "S1", "streaming": True, "current": {"exposure_time": 170.0}, "saved": {"exposure_time": 170.0}},
       "probes": lambda serial, since: {"last_t": time.time(), "today": 7}}


def test_snapshot_drops_switched_off_parts_but_keeps_alerts(mon):
    sysmon.save_config({"url": "http://127.0.0.1:1", "token": "T" * 32, "enabled": True})
    ag = sysmon.Agent(sources=SRC)
    full = ag.snapshot(None, sysmon.load_config())
    assert full["camera"]["set"] and full["services"] == {"cv": "ok"} and "micro" in full
    sysmon.save_remote(1, {"send_camera": False, "send_services": False, "send_micro": False})
    cfg = sysmon.load_config()
    snap = ag.snapshot(None, cfg)
    assert snap["camera"] == {"set": False} and snap["services"] == {} and "micro" not in snap and "alerts" in snap
    assert "Камера" not in snap["text"] and "Сервисы" not in snap["text"] and "Варка" not in snap["text"]


def test_positions_not_queued_when_switched_off_but_still_judged(mon):
    sysmon.save_config({"url": "http://127.0.0.1:1", "token": "T" * 32, "enabled": True})
    ag = sysmon.Agent(sources=SRC)
    info = {"step": "подвод", "target": 400, "calc": 401, "enc": 399, "ai": 405, "timeout": False, "sv": 88.0, "stage": 7, "t": time.time()}
    ag._cfg = sysmon.load_config()
    ag.add_positions(info)
    assert sysmon.outbox_count() == 1
    sysmon.save_remote(1, {"send_positions": False})
    ag._cfg = sysmon.load_config()
    ag.add_positions(dict(info, t=info["t"] + 1))
    assert sysmon.outbox_count() == 1 and len(ag.positions) == 2, "в очередь не идёт, но правила тревог пробу видят"


# ------------------------------------------------------------------------------------------------------------------ клиент: канал /wait

CFG = {"enabled": True, "url": "http://11.11.11.12:8769", "token": "T" * 32, "system_proxy": True, "allow_http": True}


def push(answers, remote=None):
    calls, cmds, saved = [], [], []
    store_ = {"v": None, "config": {}} if remote is None else remote

    def get(url, token, via_proxy, timeout=0):
        calls.append(url)
        return answers.pop(0) if len(answers) > 1 else answers[0]

    ln = release_push.ReleaseListener(lambda v: None, cfg_fn=lambda: CFG, get=get, installed_fn=lambda: "2.1.8", on_command=cmds.append,
                                      remote_load=lambda: store_, remote_save=lambda v, c: saved.append((v, c)))
    ln.calls, ln.cmds, ln.saved = calls, cmds, saved
    return ln


def reply(**kw):
    return 200, {"ok": True, "latest": "2.1.8", "changed": False, "commands": [], "config_v": 0, **kw}, ""


def test_client_runs_update_command_and_applies_config():
    ln = push([reply(commands=[{"id": 1, "kind": "update"}], config_v=2, config={"send_camera": False, "interval_s": 900})])
    assert ln.cv == -1
    ln.step()
    assert ln.calls[0].endswith("&cv=-1") and ln.cmds == ["update"]
    assert ln.saved == [(2, {"send_camera": False, "interval_s": 900})] and ln.cv == 2
    ln.step()
    assert ln.calls[1].endswith("&cv=2"), "приёмнику сообщаем, какую версию настроек уже применили"


def test_client_ignores_unknown_commands_and_missing_config():
    ln = push([reply(commands=[{"id": 1, "kind": "format-disk"}, "мусор", None], config_v=5)])
    ln.step()
    assert ln.cmds == [] and ln.saved == [], "незнакомая команда не выполняется; версия без config не применяется"


def test_client_resumes_with_saved_config_version():
    ln = push([reply()], remote={"v": 4, "config": {}})
    ln.step()
    assert ln.cv == 4 and ln.calls[0].endswith("&cv=4")


def test_default_command_asks_auto_update_for_update_only(monkeypatch):
    got = []

    class Ag:
        def request_update(self):
            got.append(1)
    import auto_update
    monkeypatch.setattr(auto_update, "agent", lambda: Ag())
    release_push._command("update")
    release_push._command("other")
    assert got == [1]
    monkeypatch.setattr(auto_update, "agent", lambda: None)
    release_push._command("update")                         # автообновление не запущено — тихо ничего
