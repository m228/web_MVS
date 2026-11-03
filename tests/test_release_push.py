"""«Толчок» о релизе: приёмник отвечает на GET /wait, клиент (release_push) просит автообновление проверить GitHub сразу."""
import sys
import threading
import time
from pathlib import Path

import pytest

MS = Path(__file__).resolve().parent.parent / "monitor_server"
sys.path.insert(0, str(MS))

import monitor_app  # noqa: E402
import release_push  # noqa: E402
import sysmon  # noqa: E402
from test_monitor_server import Srv, store  # noqa: E402,F401  (store — фикстура)


class Rel:
    def __init__(self, latest="2.1.8"):
        self.latest = latest


# ------------------------------------------------------------------------------------------------------------------ приёмник

def test_wait_needs_token(store):
    s = Srv(store, releases=Rel())
    try:
        assert s.call("GET", "/wait?known=", password=None)[0] == 401
        assert s.call("GET", "/wait?known=", password=None, bearer="wrong-token")[0] == 401
    finally:
        s.close()


def test_wait_answers_at_once_when_version_differs(store):
    _, token = store.create_plant("Кир")
    s = Srv(store, releases=Rel("2.1.8"))
    try:
        t0 = time.monotonic()
        code, j = s.json("GET", "/wait?known=", password=None, bearer=token)
        assert code == 200 and j == {"ok": True, "latest": "2.1.8", "changed": True} and time.monotonic() - t0 < 2
        assert s.json("GET", "/wait?known=2.1.7", password=None, bearer=token)[1]["changed"] is True
    finally:
        s.close()


def test_wait_holds_then_returns_new_version_as_soon_as_it_appears(store, monkeypatch):
    monkeypatch.setattr(monitor_app, "WAIT_S", 10)
    _, token = store.create_plant("Кир")
    rel = Rel("2.1.8")
    s = Srv(store, releases=rel)
    try:
        threading.Timer(1.5, lambda: setattr(rel, "latest", "2.1.9")).start()
        t0 = time.monotonic()
        code, j = s.json("GET", "/wait?known=2.1.8", password=None, bearer=token)
        waited = time.monotonic() - t0
        assert j["latest"] == "2.1.9" and j["changed"] is True
        assert 1.0 < waited < 5, "держит запрос, но отвечает сразу, как версия появилась (%.1f с)" % waited
    finally:
        s.close()


def test_wait_times_out_unchanged_and_works_without_releases(store, monkeypatch):
    monkeypatch.setattr(monitor_app, "WAIT_S", 1)
    _, token = store.create_plant("Кир")
    s = Srv(store, releases=Rel("2.1.8"))
    try:
        assert s.json("GET", "/wait?known=2.1.8", password=None, bearer=token)[1] == {"ok": True, "latest": "2.1.8", "changed": False}
    finally:
        s.close()
    s2 = Srv(store)                                    # проверка релизов выключена: версии нет, запрос не крутится вхолостую
    try:
        assert s2.json("GET", "/wait?known=", password=None, bearer=token)[1] == {"ok": True, "latest": None, "changed": False}
    finally:
        s2.close()


# ------------------------------------------------------------------------------------------------------------------ клиент

CFG = {"enabled": True, "url": "http://11.11.11.12:8769", "token": "T" * 32, "system_proxy": True, "allow_http": True}


def listener(answers, installed="2.1.7", cfg=None):
    calls, fired = [], []

    def get(url, token, via_proxy, timeout=0):
        calls.append((url, token, via_proxy))
        a = answers.pop(0) if len(answers) > 1 else answers[0]
        return a

    ln = release_push.ReleaseListener(fired.append, cfg_fn=lambda: cfg or CFG, get=get, installed_fn=lambda: installed)
    ln.calls, ln.fired = calls, fired
    return ln


def ok(latest):
    return 200, {"ok": True, "latest": latest, "changed": True}, ""


def test_client_asks_update_check_when_newer_released_once():
    ln = listener([ok("2.1.8"), ok("2.1.8"), ok("2.1.9")])
    assert ln.step() == release_push.REPOLL_S
    assert ln.fired == ["2.1.8"] and ln.calls[0][0].endswith("/wait?known=")
    ln.step()
    assert ln.fired == ["2.1.8"], "о той же версии второй раз не просим"
    assert ln.calls[1][0].endswith("/wait?known=2.1.8"), "в запрос идёт последняя известная версия"
    ln.step()
    assert ln.fired == ["2.1.8", "2.1.9"]


def test_client_ignores_same_or_older_and_missing_latest():
    ln = listener([ok("2.1.7"), ok("2.1.6"), (200, {"ok": True, "latest": None, "changed": False}, "")])
    for _ in range(3):
        ln.step()
    assert ln.fired == []


def test_client_idle_when_not_configured_and_goes_direct_in_closed_network():
    ln = listener([ok("2.1.8")], cfg={**CFG, "enabled": False})
    assert ln.step() == release_push.IDLE_S and ln.calls == []
    ln = listener([ok("2.1.8")])
    ln.step()
    assert ln.calls[0][2] is False, "http в закрытой сети — мимо системного прокси"
    ln = listener([ok("2.1.8")], cfg={**CFG, "url": "https://monitor.example.ru", "allow_http": False})
    ln.step()
    assert ln.calls[0][2] is True


def test_client_backs_off_on_errors_and_waits_long_for_old_receiver():
    ln = listener([(None, None, "Нет связи"), (None, None, "Нет связи"), (404, None, "HTTP 404"), ok("2.1.7")])
    assert [ln.step(), ln.step()] == [15, 30]
    assert ln.step() == release_push.BACKOFF[-1] and "старый" in ln.status["error"]
    assert ln.step() == release_push.REPOLL_S and ln.fails == 0, "связь вернулась — пауза сброшена"


def test_loop_survives_exception(monkeypatch):
    ln = listener([ok("2.1.8")])
    monkeypatch.setattr(ln, "step", lambda: (_ for _ in ()).throw(RuntimeError("сбой")))
    waits = []
    monkeypatch.setattr(ln._stop, "wait", lambda d: (waits.append(d), ln._stop.set())[0])
    ln._loop()
    assert waits == [release_push.BACKOFF[-1]]


def test_default_callback_asks_auto_update_with_spread(monkeypatch):
    got = []

    class Ag:
        def check_now(self, d):
            got.append(d)
    import auto_update
    monkeypatch.setattr(auto_update, "agent", lambda: Ag())
    release_push._check_updates("2.1.8")
    assert len(got) == 1 and 0 <= got[0] <= release_push.SPREAD_S
    monkeypatch.setattr(auto_update, "agent", lambda: None)
    release_push._check_updates("2.1.8")             # автообновление не запущено — тихо ничего
