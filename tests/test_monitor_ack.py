"""Сброс тревоги в панели и журнал ошибок: сброшенная тревога уходит с карточки, но остаётся в журнале; вернётся, если станет хуже или появится снова."""
import sqlite3
import sys
from pathlib import Path

import pytest

MS = Path(__file__).resolve().parent.parent / "monitor_server"
sys.path.insert(0, str(MS))

import monitor_store as ms  # noqa: E402
from test_monitor_server import Clock, Srv, ev, pulse, store  # noqa: E402,F401  (store — фикстура)

P = {"X-Panel": "1"}


def alert(code="approach", level="medium", text="Подвод: датчик плавает", t=0):
    return {"code": code, "level": level, "text": text, "since": t}


@pytest.fixture
def srv(store):
    s = Srv(store)
    yield s
    s.close()


def view(store):
    return store.plants_view()[0]["alerts"]


def test_ack_hides_alert_but_keeps_it_open_and_logged(store):
    pid, _ = store.create_plant("Кир")
    store.ingest(pid, pulse(alerts=[alert(t=store.clk.t - 5)]))
    assert [a["acked"] for a in view(store)] == [False]
    assert store.ack_alerts(pid, "approach") == 1
    assert [a["acked"] for a in view(store)] == [True], "тревога всё ещё открыта у завода, просто сброшена"
    assert store.ack_alerts(pid, "approach") == 0, "повторный сброс ничего не делает"
    texts = [(e["state"], e["text"]) for e in store.events(pid) if e["kind"] == "alert"]
    assert ("ack", "Сброшено: Подвод: датчик плавает") in texts
    store.ingest(pid, pulse(alerts=[alert(t=store.clk.t - 5)]))
    assert [a["acked"] for a in view(store)] == [True], "следующий пульс с той же тревогой её не возвращает"


def test_ack_comes_back_when_worse_or_after_gone_and_back(store):
    pid, _ = store.create_plant("Кир")
    store.ingest(pid, pulse(alerts=[alert(level="medium", t=store.clk.t - 5)]))
    store.ack_alerts(pid, "approach")
    store.ingest(pid, pulse(alerts=[alert(level="high", t=store.clk.t - 5)]))
    assert [a["acked"] for a in view(store)] == [False], "стала красной — сброс снят"
    store.ack_alerts(pid)
    store.ingest(pid, pulse(alerts=[alert(level="medium", t=store.clk.t - 5)]))
    assert [a["acked"] for a in view(store)] == [True], "полегчало — остаётся сброшенной"
    store.ingest(pid, pulse(alerts=[]))
    assert view(store) == [], "завод перестал присылать — тревога снята совсем"
    store.clk.t += 600
    store.ingest(pid, pulse(alerts=[alert(level="medium", t=store.clk.t - 5)]))
    assert [a["acked"] for a in view(store)] == [False], "появилась снова — открыта заново"


def test_ack_all_and_other_plants_untouched(store):
    a, _ = store.create_plant("Кир")
    b, _ = store.create_plant("Буи")
    two = [alert("x", "high", "Красное", store.clk.t - 5), alert("y", "medium", "Жёлтое", store.clk.t - 5)]
    store.ingest(a, pulse(alerts=two))
    store.ingest(b, pulse(alerts=two[:1]))
    assert store.ack_alerts(a) == 2
    by = {p["name"]: [x["acked"] for x in p["alerts"]] for p in store.plants_view()}
    assert by == {"Кир": [True, True], "Буи": [False]}
    assert store.ack_alerts(a, "нет-такой") == 0


def test_errors_filter_keeps_alerts_and_offline_only(store):
    pid, _ = store.create_plant("Кир")
    store.ingest(pid, pulse(events=[ev("a", store.clk.t - 50, "Открыта", kind="alert", code="x", state="open"),
                                    ev("b", store.clk.t - 40, "Запуск", kind="start", level="info"),
                                    ev("c", store.clk.t - 30, "Плата", kind="plate_reboot")]))
    store.clk.t += 3600
    store.check_offline()
    kinds_all = {e["kind"] for e in store.events(pid)}
    kinds_err = {e["kind"] for e in store.events(pid, errors_only=True)}
    assert {"start", "alert", "plate_reboot", "offline"} <= kinds_all
    assert kinds_err == {"alert", "plate_reboot", "offline"}


def test_ack_is_not_sent_to_telegram(tmp_path):
    clk = Clock()
    s = ms.Store(tmp_path / "m.db", clock=clk, notify={"fresh_s": 900})
    try:
        pid, _ = s.create_plant("Кир")
        s.ingest(pid, pulse(alerts=[alert(level="high", t=clk.t - 5)]))
        before = s.notify_count()
        s.ack_alerts(pid)
        assert s.notify_count() == before, "сброс — решение Макса в панели, в чат об этом не пишем"
    finally:
        s.close()


def test_old_database_gets_ack_columns(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(str(path))
    db.executescript("CREATE TABLE alerts (plant_id INTEGER NOT NULL, code TEXT NOT NULL, level TEXT NOT NULL, text TEXT NOT NULL, since_t REAL NOT NULL, "
                     "PRIMARY KEY (plant_id, code)); INSERT INTO alerts VALUES (1, 'x', 'high', 'Старая', 1.0);")
    db.commit()
    db.close()
    s = ms.Store(path)
    try:
        cols = {r["name"] for r in s.db.execute("PRAGMA table_info(alerts)")}
        assert {"acked_t", "ack_level"} <= cols
        assert s.db.execute("SELECT acked_t FROM alerts").fetchone()["acked_t"] is None, "старые тревоги не сброшены"
    finally:
        s.close()


def test_ack_over_http(srv, store):
    pid, _ = store.create_plant("Кир")
    store.ingest(pid, pulse(alerts=[alert("x", "high", "Красное", store.clk.t - 5), alert("y", "medium", "Жёлтое", store.clk.t - 5)]))
    url = "/api/plants/%d/alerts/ack" % pid
    assert srv.json("POST", url, {"code": "x"}, password=None, headers=P)[0] == 401
    assert srv.json("POST", url, {"code": "x"})[0] == 403, "без заголовка X-Panel запись не принимается"
    assert srv.json("POST", url, {"code": "x"}, headers=P) == (200, {"acked": 1})
    assert srv.json("POST", url, {}, headers=P) == (200, {"acked": 1}), "без кода — все оставшиеся"
    plants = srv.json("GET", "/api/plants")[1]["plants"]
    assert [a["acked"] for a in plants[0]["alerts"]] == [True, True]
    err = srv.json("GET", "/api/plants/%d/events?errors=1" % pid)[1]["events"]
    assert [e["state"] for e in err].count("ack") == 2
    assert srv.json("POST", "/api/plants/999/alerts/ack", {}, headers=P) == (200, {"acked": 0})
