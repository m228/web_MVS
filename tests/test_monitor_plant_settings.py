"""Настройки «нет пульса» для отдельного завода: порог, напоминание, сообщения в Telegram (завод с нестабильным интернетом)."""
import sys
from pathlib import Path

import pytest

MS = Path(__file__).resolve().parent.parent / "monitor_server"
sys.path.insert(0, str(MS))

import monitor_store as ms  # noqa: E402
from test_monitor_server import Clock, Srv, pulse, store  # noqa: E402,F401  (store — фикстура)

P = {"X-Panel": "1"}


@pytest.fixture
def srv(store):
    s = Srv(store)
    yield s
    s.close()


def notified(s):
    return [r["kind"] for r in s.notify_pending(s.clock() + 10**6)]


def make(tmp_path):
    clk = Clock()
    s = ms.Store(tmp_path / "m.db", clock=clk, notify={"fresh_s": 900, "offline_repeat_s": 21600})
    s.started_at = clk.t - 10 * 86400
    return s, clk


def test_defaults_and_overrides(store):
    pid, _ = store.create_plant("Кир")
    assert store.plant_settings(pid) == {"offline_after_min": 30, "offline_repeat_min": 360, "offline_notify": True}
    assert store.plants_view()[0]["settings_custom"] == []
    assert store.set_plant_settings(pid, {"offline_after_min": 180, "offline_notify": False}) == {
        "offline_after_min": 180, "offline_repeat_min": 360, "offline_notify": False}
    assert store.plants_view()[0]["settings_custom"] == ["offline_after_min", "offline_notify"]
    assert store.set_plant_settings(pid, {"offline_after_min": None, "offline_notify": None})["offline_after_min"] == 30
    assert store.plants_view()[0]["settings_custom"] == [], "None возвращает общее значение"
    store.set_plant_settings(pid, {"offline_after_min": 30})
    assert store.plants_view()[0]["settings_custom"] == [], "то же, что общее значение, своей настройкой не считается"


def test_validation_and_unknown_plant(store):
    pid, _ = store.create_plant("Кир")
    for bad in ({"offline_after_min": 4}, {"offline_after_min": 1441}, {"offline_after_min": "30"}, {"offline_after_min": True},
                {"offline_repeat_min": -1}, {"offline_notify": 1}, {"что-то": 1}, {}, [], None):
        with pytest.raises(ValueError):
            store.set_plant_settings(pid, bad)
    assert store.plant_settings(pid)["offline_after_min"] == 30, "неудачные попытки ничего не меняют"
    with pytest.raises(LookupError):
        store.set_plant_settings(999, {"offline_after_min": 60})
    assert store.set_plant_settings(pid, {"offline_repeat_min": 0})["offline_repeat_min"] == 0


def test_longer_threshold_keeps_plant_online_longer_and_other_plants_untouched(store):
    a, _ = store.create_plant("Кир")
    b, _ = store.create_plant("Буи")
    store.set_plant_settings(a, {"offline_after_min": 180})
    store.ingest(a, pulse(interval=300))
    store.ingest(b, pulse(interval=300))
    store.clk.t += 31 * 60
    out = store.check_offline()
    assert [o["plant"] for o in out] == ["Буи"], "Кир с порогом 3 часа ещё молчит терпимо"
    assert {p["name"]: p["status"] for p in store.plants_view()} == {"Кир": "online", "Буи": "offline"}
    store.clk.t += 150 * 60
    assert [o["plant"] for o in store.check_offline()] == ["Кир"]


def test_notify_off_keeps_panel_and_journal_but_sends_nothing(tmp_path):
    s, clk = make(tmp_path)
    pid, _ = s.create_plant("Кир")
    s.set_plant_settings(pid, {"offline_notify": False})
    s.ingest(pid, pulse())
    clk.t += 31 * 60
    assert len(s.check_offline()) == 1 and s.plants_view()[0]["status"] == "offline"
    assert any(e["kind"] == "offline" for e in s.events(pid)), "в журнале и панели «нет пульса» остаётся"
    assert notified(s) == []
    clk.t += 7 * 3600
    assert s.notify_offline_reminders(clk.t, 21600) == 0
    clk.t += 60
    s.ingest(pid, pulse())
    assert notified(s) == [], "и «связь восстановилась» без предыдущего сообщения не нужна"
    s.close()


def test_repeat_setting_per_plant(tmp_path):
    s, clk = make(tmp_path)
    a, _ = s.create_plant("Кир")
    b, _ = s.create_plant("Буи")
    for pid in (a, b):
        s.ingest(pid, pulse())
    s.set_plant_settings(a, {"offline_repeat_min": 0})                     # Кир: не напоминать
    s.set_plant_settings(b, {"offline_repeat_min": 60})                    # Буи: раз в час
    clk.t += 31 * 60
    s.check_offline()
    rows = s.notify_pending(clk.t + 1)
    s.notify_mark([r["id"] for r in rows], "sent", clk.t)
    clk.t += 61 * 60
    assert s.notify_offline_reminders(clk.t, 21600) == 1
    assert [r["plant_name"] for r in s.notify_pending(clk.t + 1)] == ["Буи"]
    s.close()


def test_settings_are_removed_with_plant(store):
    pid, _ = store.create_plant("Кир")
    store.set_plant_settings(pid, {"offline_after_min": 60})
    store.delete_plant(pid)
    assert store.meta_get("pcfg:%d" % pid) is None


def test_settings_over_http(srv, store):
    pid, _ = store.create_plant("Кир")
    store.ingest(pid, pulse())
    url = "/api/plants/%d/settings" % pid
    assert srv.json("POST", url, {"offline_after_min": 90}, password=None, headers=P)[0] == 401
    assert srv.json("POST", url, {"offline_after_min": 90})[0] == 403, "без заголовка X-Panel запись не принимается"
    code, j = srv.json("POST", url, {"offline_after_min": 90, "offline_notify": False}, headers=P)
    assert code == 200 and j["settings"] == {"offline_after_min": 90, "offline_repeat_min": 360, "offline_notify": False}
    v = srv.json("GET", "/api/plants")[1]["plants"][0]
    assert v["settings"]["offline_after_min"] == 90 and v["settings_custom"] == ["offline_after_min", "offline_notify"]
    assert srv.json("POST", url, {"offline_after_min": 1}, headers=P)[0] == 400
    assert srv.json("POST", url, {"offline_after_min": None, "offline_notify": None}, headers=P)[1]["settings"]["offline_after_min"] == 30
    assert srv.json("POST", "/api/plants/999/settings", {"offline_after_min": 60}, headers=P)[0] == 404
