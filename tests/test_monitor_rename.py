"""Переименование завода в панели приёмника: токен, журнал и датчики остаются, название меняется в карточке, боте и ещё не отправленных сообщениях."""
import sys
from pathlib import Path

import pytest

MS = Path(__file__).resolve().parent.parent / "monitor_server"
sys.path.insert(0, str(MS))

import monitor_store as ms  # noqa: E402
from test_monitor_server import PANEL, Srv, pulse, store  # noqa: E402,F401  (store — фикстура)

P = {"X-Panel": "1"}


@pytest.fixture
def srv(store):
    s = Srv(store)
    yield s
    s.close()


def test_rename_keeps_token_history_and_updates_pending_messages(store):
    pid, token = store.create_plant("DESKTOP-C9R41JT")
    store.ingest(pid, pulse(alerts=[{"code": "x", "level": "high", "text": "Что-то красное", "since": store.clk.t - 5}]))
    assert store.plant_by_token(token)["id"] == pid
    assert store.rename_plant(pid, "  Склад   новый ") == "Склад новый", "лишние пробелы убираются"
    assert store.plant_names() == ["Склад новый"]
    assert store.plant_by_token(token)["id"] == pid, "токен прежний — завод не отваливается"
    assert store.events(pid), "журнал на месте"
    assert {r["plant_name"] for r in store.db.execute("SELECT plant_name FROM notify WHERE plant_id=?", (pid,))} <= {"Склад новый"}, \
        "неотправленные сообщения идут уже с новым названием"
    assert store.ingest(pid, pulse())["new_events"] >= 0, "пульс под новым названием принимается"


def test_rename_validation(store):
    a, _ = store.create_plant("Кир")
    store.create_plant("Буи")
    with pytest.raises(ValueError):
        store.rename_plant(a, "   ")
    with pytest.raises(ValueError):
        store.rename_plant(a, "я" * (ms.NAME_MAX + 1))
    with pytest.raises(ValueError, match="уже есть"):
        store.rename_plant(a, "Буи")
    assert store.plant_names() == ["Буи", "Кир"], "неудачные попытки ничего не меняют"
    with pytest.raises(LookupError):
        store.rename_plant(999, "Нет такого")
    assert store.rename_plant(a, "Кир") == "Кир", "то же название — не ошибка"


def test_rename_over_http_needs_panel_header_and_password(srv, store):
    pid, _ = store.create_plant("DESKTOP-1")
    assert srv.json("POST", "/api/plants/%d/rename" % pid, {"name": "Кир-2"}, password=None, headers=P)[0] == 401
    assert srv.json("POST", "/api/plants/%d/rename" % pid, {"name": "Кир-2"})[0] == 403, "без заголовка X-Panel запись не принимается"
    code, j = srv.json("POST", "/api/plants/%d/rename" % pid, {"name": "Кир-2"}, headers=P)
    assert (code, j) == (200, {"name": "Кир-2"})
    assert [p["name"] for p in srv.json("GET", "/api/plants")[1]["plants"]] == ["Кир-2"]
    code, j = srv.json("POST", "/api/plants/%d/rename" % pid, {"name": ""}, headers=P)
    assert code == 400 and "Название" in j["error"]
    store.create_plant("Буи")
    code, j = srv.json("POST", "/api/plants/%d/rename" % pid, {"name": "Буи"}, headers=P)
    assert code == 400 and "уже есть" in j["error"]
    assert srv.json("POST", "/api/plants/999/rename", {"name": "Х"}, headers=P)[0] == 404


def test_panel_has_rename_button_and_hides_clients_own_name():
    assert 'data-a="rename"' in PANEL and "Переименовать" in PANEL
    assert "/rename'" in PANEL and "function cardText" in PANEL
    assert "snap.plant + ' · '" in PANEL, "имя, которым назвался клиент (DESKTOP-… ·), убирается из текста карточки"
