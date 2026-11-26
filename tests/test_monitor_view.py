"""«Вид» карточки завода в панели приёмника: какие блоки скрыты, хранится в приёмнике (а не в браузере) и переживает перезапуск."""
import sys
from pathlib import Path

import pytest

MS = Path(__file__).resolve().parent.parent / "monitor_server"
sys.path.insert(0, str(MS))

import monitor_store as ms  # noqa: E402
from test_monitor_server import Srv, pulse, store  # noqa: E402,F401  (store — фикстура)

P = {"X-Panel": "1"}


@pytest.fixture
def srv(store):
    s = Srv(store)
    yield s
    s.close()


def test_hidden_blocks_default_and_roundtrip(store):
    pid, _ = store.create_plant("Кир")
    assert store.hidden_blocks(pid) == ["plate"], "пока «Вид» не настраивали, подробности про плату скрыты"
    assert store.plants_view()[0]["hidden_blocks"] == ["plate"]
    assert store.set_hidden_blocks(pid, ["camera", "pc", "camera"]) == ["pc", "camera"], "дубли убираются, порядок как в карточке"
    assert store.plants_view()[0]["hidden_blocks"] == ["pc", "camera"]
    assert store.set_hidden_blocks(pid, []) == []
    assert store.hidden_blocks(pid) == [], "пустой выбор — это «показывать всё», а не «вернуть умолчание»"


def test_hidden_blocks_validation(store):
    pid, _ = store.create_plant("Кир")
    store.set_hidden_blocks(pid, ["pc"])
    for bad in (["alerts"], ["pc", "что-то"], "pc", None, {"pc": 1}):
        with pytest.raises(ValueError):
            store.set_hidden_blocks(pid, bad)
    assert store.hidden_blocks(pid) == ["pc"], "неудачные попытки ничего не меняют"
    with pytest.raises(LookupError):
        store.set_hidden_blocks(999, [])
    assert "alerts" not in ms.VIEW_BLOCKS, "тревоги скрыть нельзя"


def test_hidden_blocks_are_per_plant_and_removed_with_plant(store):
    a, _ = store.create_plant("Кир")
    b, _ = store.create_plant("Буи")
    store.set_hidden_blocks(a, ["services"])
    assert store.hidden_blocks(b) == ["plate"]
    store.delete_plant(a)
    assert store.meta_get("view:%d" % a) is None, "после удаления завода его настройки не остаются"


def test_view_over_http_needs_panel_header_and_password(srv, store):
    pid, _ = store.create_plant("Кир")
    store.ingest(pid, pulse())
    url = "/api/plants/%d/view" % pid
    assert srv.json("POST", url, {"hidden": ["pc"]}, password=None, headers=P)[0] == 401
    assert srv.json("POST", url, {"hidden": ["pc"]})[0] == 403, "без заголовка X-Panel запись не принимается"
    assert srv.json("POST", url, {"hidden": ["pc", "brew"]}, headers=P) == (200, {"hidden": ["pc", "brew"]})
    assert srv.json("GET", "/api/plants")[1]["plants"][0]["hidden_blocks"] == ["pc", "brew"]
    assert srv.json("POST", url, {"hidden": ["nope"]}, headers=P)[0] == 400
    assert srv.json("POST", url, ["pc"], headers=P)[0] == 400
    assert srv.json("POST", "/api/plants/999/view", {"hidden": []}, headers=P)[0] == 404


def test_hidden_blocks_survive_store_restart(tmp_path):
    path = tmp_path / "m.db"
    s1 = ms.Store(path)
    pid, _ = s1.create_plant("Кир")
    s1.set_hidden_blocks(pid, ["camera"])
    s1.close()
    s2 = ms.Store(path)
    assert s2.hidden_blocks(pid) == ["camera"]
    s2.close()
