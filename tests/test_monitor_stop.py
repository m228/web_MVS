"""«Приложение закрыто» и мягкое «нет пульса»: закрыли сами — красное сразу; пропал пульс — оранжевое через полчаса; закрыто для обновления и не вернулось — красное."""
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

MS = Path(__file__).resolve().parent.parent / "monitor_server"
sys.path.insert(0, str(MS))

import monitor_store as ms  # noqa: E402
import sysmon  # noqa: E402
from test_monitor_server import ev, pulse, store  # noqa: E402,F401  (store — фикстура)
from test_sysmon import Clock, make_sources, sm  # noqa: E402,F401  (sm — фикстура)


def kinds(store, pid):
    return [(e["kind"], e["level"], e["text"]) for e in store.events(pid)]


def test_manual_close_then_silence_is_one_red_message_and_a_quiet_record(tmp_path):
    clk = type("C", (), {"t": 1_000_000.0, "__call__": lambda self: self.t})()
    s = ms.Store(tmp_path / "m.db", clock=clk, notify={"fresh_s": 900})
    s.started_at = clk.t - 10 * 86400
    pid, _ = s.create_plant("Кир")
    s.ingest(pid, pulse())
    clk.t += 5
    s.ingest(pid, pulse(events=[ev("stop1", clk.t - 1, "Приложение закрыто", kind="app_stop", level="critical")]))
    assert [r["kind"] for r in s.notify_pending(clk.t + 1)] == ["app_stop"], "закрыли руками — сообщение сразу"
    assert s.plants_view()[0]["closed_t"] is not None, "в панели видно, что приложение закрыто"
    clk.t += 1900
    out = s.check_offline()
    assert len(out) == 1 and out[0]["level"] == "info" and "Приложение закрыто, пульса нет" in out[0]["text"]
    assert [r["kind"] for r in s.notify_pending(clk.t + 1)] == ["app_stop"], "про то же самое второй раз в чат не пишем"
    s.close()


def test_closed_for_update_and_never_came_back_is_critical(store):
    store.started_at = store.clk.t - 10 * 86400
    pid, _ = store.create_plant("Кир")
    store.ingest(pid, pulse(events=[ev("stop1", store.clk.t - 1, "Приложение закрыто для обновления", kind="app_stop", level="info")]))
    assert store.plants_view()[0]["closed_t"] is None, "закрытие ради обновления не красим"
    store.clk.t += 1900
    out = store.check_offline()
    assert out[0]["level"] == "critical" and "После обновления приложение не вернулось" in out[0]["text"]


def test_app_start_after_close_clears_closed_flag(store):
    pid, _ = store.create_plant("Кир")
    store.ingest(pid, pulse(events=[ev("stop1", store.clk.t - 10, "Приложение закрыто", kind="app_stop", level="critical")]))
    assert store.plants_view()[0]["closed_t"] is not None
    store.clk.t += 60
    store.ingest(pid, pulse(events=[ev("st1", store.clk.t - 1, "Приложение запущено", kind="start", level="info")]))
    assert store.plants_view()[0]["closed_t"] is None


def test_plain_silence_is_high_not_critical_and_after_half_an_hour(store):
    store.started_at = store.clk.t - 10 * 86400
    pid, _ = store.create_plant("Кир")
    store.ingest(pid, pulse(interval=300))
    store.clk.t += 25 * 60
    assert store.check_offline() == [], "25 минут тишины — ещё не повод"
    store.clk.t += 6 * 60
    out = store.check_offline()
    assert len(out) == 1 and out[0]["level"] == "high" and "31 мин" in out[0]["text"]


def test_view_all_sets_one_standard_for_every_plant(store):
    a, _ = store.create_plant("Кир")
    b, _ = store.create_plant("Буи")
    store.set_hidden_blocks(a, ["services"])
    assert store.set_hidden_blocks_all(["plate", "sensors", "plate"]) == ["sensors", "plate"]
    assert store.hidden_blocks(a) == store.hidden_blocks(b) == ["sensors", "plate"]
    with pytest.raises(ValueError):
        store.set_hidden_blocks_all(["alerts"])
    assert store.hidden_blocks(a) == ["sensors", "plate"], "неудачная попытка ничего не меняет"


# ---------------------------------------------------------------------------------------- клиент: событие при закрытии приложения

class Recv(BaseHTTPRequestHandler):
    bodies: list = []

    def do_POST(self):
        import json
        n = int(self.headers.get("Content-Length") or 0)
        Recv.bodies.append(json.loads(self.rfile.read(n).decode("utf-8")))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


@pytest.fixture
def receiver():
    Recv.bodies = []
    srv = HTTPServer(("127.0.0.1", 0), Recv)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_port
    srv.shutdown()


def configure(sm, url):
    sm.save_config({"enabled": True, "url": url, "token": "T" * 32, "allow_http": True})


@pytest.mark.parametrize("for_update, level", [(False, "critical"), (True, "info")])
def test_stop_sends_app_stop_event(sm, receiver, monkeypatch, for_update, level):
    configure(sm, receiver)
    monkeypatch.setattr(sm, "_update_restart_t", time.time() if for_update else None)
    ag = sm.Agent(sources=make_sources(), clock=time.time)
    ag.start()
    try:
        ag.stop()
    finally:
        sm.close()
    sent = [e for b in Recv.bodies for e in b["events"] if e["kind"] == "app_stop"]
    assert len(sent) == 1 and sent[0]["level"] == level
    assert ("для обновления" in sent[0]["text"]) == for_update


def test_stop_without_settings_does_nothing_and_does_not_fail(sm):
    ag = sm.Agent(sources=make_sources(), clock=time.time)
    ag.start()
    ag.stop()
    assert [e["kind"] for e in ag.recent if e["kind"] == "app_stop"] == ["app_stop"]


def test_note_update_restart_marks_time(sm, monkeypatch):
    monkeypatch.setattr(sm, "_update_restart_t", None)
    before = time.time()
    sm.note_update_restart()
    assert before <= sm._update_restart_t <= time.time()
