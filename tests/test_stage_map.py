"""Карта стадий «наша / другая» (10.11): код стадии чужого контроллера -> наша стадия 1..21 на входе в SvSource.

Без железа и боевой базы (DATA_DIR → tmp, см. conftest.data_dir). Живой прогон — против эмулятора OPC UA."""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import plate_config  # noqa: E402
import plc_source  # noqa: E402
import stage_map  # noqa: E402
from sv_source import SvSource  # noqa: E402

CUSTOM = {"stage_map": {"profile": "custom"}}


def test_default_profile_is_ours():
    assert plate_config.DEFAULTS["sv_source"]["stage_map"] == {"profile": "ours"}
    m = stage_map.StageMap(plate_config.DEFAULTS["sv_source"])
    assert not m.custom and m.translate(31) == (31, None)           # наш контроллер — код как есть


@pytest.mark.parametrize("code,ours", [
    (1, 1), (10, 1), (11, 1), (20, 2), (22, 2), (30, 3), (31, 3), (32, 3), (40, 4), (44, 4),
    (50, 5), (60, 7), (61, 7), (62, 7), (70, 8), (71, 8), (80, 10), (83, 10), (90, 11), (93, 11),
])
def test_custom_default_table(code, ours):
    assert stage_map.StageMap(CUSTOM).translate(code)[0] == ours


def test_custom_names_and_unknown():
    m = stage_map.StageMap(CUSTOM)
    assert m.translate(31) == (3, "СГУЩЕНИЕ") and m.translate(44) == (4, "ЗАТРАВКА МУ")
    assert m.translate(64) == (7, "ВАРКА") and m.translate(23) == (2, "НАБОР")   # точного кода нет — строка своего десятка
    assert m.translate(55) == (5, "КРИСТАЛЛИЗАЦИЯ") and m.translate(99) == (11, "ПРОПАРКА")
    assert m.translate(5) == (None, None) and m.translate(120) == (None, None)  # десятка нет — не угадываем
    assert m.translate(None) == (None, None)


def test_default_rows_all_valid_and_83_once():
    codes = [r["code"] for r in stage_map.DEFAULT_ROWS]
    assert len(codes) == len(set(codes))
    assert all(stage_map.OUR_MIN <= r["to"] <= stage_map.OUR_MAX for r in stage_map.DEFAULT_ROWS)


def test_saved_rows_override_default():
    m = stage_map.StageMap({"stage_map": {"profile": "custom", "rows": [{"code": 5, "name": "X", "to": 9}]}})
    assert m.translate(5) == (9, "X") and m.translate(31) == (None, None)


def test_normalize_validation():
    ok = stage_map.normalize({"profile": "custom", "rows": [{"code": "31", "name": " СГ ", "to": "3"}, {"code": 1, "to": 1}]})
    assert ok == {"profile": "custom", "rows": [{"code": 1, "name": "", "to": 1}, {"code": 31, "name": "СГ", "to": 3}]}
    for bad in ({"profile": "x"}, {"rows": [{"code": 1, "to": 0}]}, {"rows": [{"code": 1, "to": 22}]},
                {"rows": [{"code": 1, "to": 1}, {"code": 1, "to": 2}]}, {"rows": [{"code": "a", "to": 1}]},
                {"rows": [{"code": 70000, "to": 1}]}):
        with pytest.raises(ValueError):
            stage_map.normalize(bad)


def test_settings_roundtrip(data_dir):
    patch, _ = plc_source.normalize({"stage_map": {"profile": "custom", "rows": [{"code": 31, "name": "СГ", "to": 3}]}})
    cfg = plate_config.save({"sv_source": patch})
    pub = plc_source.public(cfg)["stage_map"]
    assert pub["profile"] == "custom" and pub["rows"] == [{"code": 31, "name": "СГ", "to": 3}]
    assert pub["default_rows"] == stage_map.DEFAULT_ROWS
    back, _ = plc_source.normalize({"stage_map": {"profile": "ours"}})
    assert plc_source.public(plate_config.save({"sv_source": back}))["stage_map"]["profile"] == "ours"


def test_sv_source_translates_before_glitch_filter():
    src = SvSource(dict(CUSTOM, host="x"))
    v = src._translate_stage({"sv": 80.0, "stage": 31})
    assert v["stage"] == 3 and src.status()["stage_raw"] == 31 and src.status()["stage_name"] == "СГУЩЕНИЕ"
    # 30 → 31 → 32 — это всё «сгущение»: для фильтра сбоев стадия не менялась
    for code in (30, 31, 32):
        out = src._sanitize(src._translate_stage({"stage": code}))
        assert out["stage"] == 3
    assert src._stage_n == 0 and src._stage_cand is None
    # неизвестный код — стадии нет
    assert src._translate_stage({"stage": 120})["stage"] is None


def test_sv_source_ours_unchanged():
    src = SvSource({"host": "x"})
    vals = {"stage": 31, "sv": 80.0}
    assert src._translate_stage(vals) is vals and src.status()["stage_raw"] is None


# ───────────────────────── живой прогон: эмулятор OPC UA ─────────────────────────
asyncua = pytest.importorskip("asyncua")
PORT = 48437


@pytest.fixture(scope="module")
def sim():
    from opcua_sim import start_sim
    s = start_sim(PORT, secure=False, animate=False)
    yield s
    s.stop()


def _wait(pred, timeout=15.0):
    t = time.time()
    while time.time() - t < timeout:
        if pred():
            return True
        time.sleep(0.1)
    return False


def test_live_opcua_custom_stage(sim):
    cfg = dict(CUSTOM, protocol="opcua", period_s=0.2, glitch_filter=False,
               opcua={"endpoint": sim.url, "security": "None", "mode": "Sign", "auth": "anonymous", "timeout_s": 3,
                      "fields": {"stage": {"node": sim.node_id("Mode"), "scale": 1}, "sv": {"node": sim.node_id("Brix"), "scale": 1}}})
    seen = []
    src = SvSource(cfg, on_update=lambda sv, st: seen.append(st))
    sim.set("Mode", 44)
    src.start()
    try:
        assert _wait(lambda: src.status()["connected"])
        st = src.status()
        assert st["stage"] == 4 and st["stage_raw"] == 44 and st["stage_name"] == "ЗАТРАВКА МУ"
        sim.set("Mode", 61)
        assert _wait(lambda: src.status()["stage"] == 7)
        assert seen[-1] == 7                                        # автомат получает НАШУ стадию
        sim.set("Mode", 5)                                          # у чужого контроллера 5 — нет в таблице
        assert _wait(lambda: src.status()["stage"] is None)
    finally:
        src.stop()


def test_our_names_from_table():
    """Названия НАШИХ стадий по таблице: несколько кодов в одну стадию — названия через « / » без повторов, коды по порядку."""
    assert stage_map.our_names({"stage_map": {"profile": "ours"}}) == {}                    # наш контроллер — свои названия
    n = stage_map.our_names(CUSTOM)                                                         # таблица по умолчанию
    assert n[3] == {"name": "СГУЩЕНИЕ", "codes": [30, 31, 32]}
    assert n[1]["name"] == "СТАРТ ШАГ / ПРЕДВ. ВАКУУМ" and n[7]["codes"] == [60, 61, 62]
    assert stage_map.public(CUSTOM)["names"]["8"]["name"] == "ИСТОЩЕНИЕ"                    # странице — ключи строками (JSON)
    assert stage_map.label(CUSTOM, 3, {3: "сгущение"}) == "3 СГУЩЕНИЕ"
    assert stage_map.label({"stage_map": {"profile": "ours"}}, 3, {3: "сгущение"}) == "3 сгущение"
    assert stage_map.label(CUSTOM, 9, {}) == "9" and stage_map.label(None, None) == "—"      # названия нет — просто номер


def test_describe_change():
    ours, rows = {"stage_map": {"profile": "ours"}}, {"stage_map": {"profile": "custom", "rows": [{"code": 30, "name": "СГ", "to": 3}]}}
    assert stage_map.describe_change(ours, ours) == ""
    assert stage_map.describe_change(rows, rows) == ""
    assert stage_map.describe_change(ours, {"stage_map": {"profile": "ours", "rows": stage_map.DEFAULT_ROWS}}) == ""   # те же умолчательные строки — не изменение
    new = {"stage_map": {"profile": "custom", "rows": [{"code": 30, "name": "СГ", "to": 4}, {"code": 40, "name": "ЗТ", "to": 4}]}}
    assert stage_map.describe_change(rows, new) == "строки: 30: «СГ» 3 → «СГ» 4, +40 ЗТ → 4"
    assert stage_map.describe_change(new, {"stage_map": {"profile": "custom", "rows": [{"code": 40, "name": "ЗТ", "to": 4}]}}) == "строки: −30"
