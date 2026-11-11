"""Советы по подводу от разломов: расчёт по строкам СВ-таблицы, запись подвода в журнал проб и ленту, потолок зазора.

Без платы и камеры; база — во временном каталоге (conftest.data_dir).
"""
import time

import approach_advice
import cv_store
import db

SP = [40000, 100, 600, 1400, 1600]
SVSP = [0.0, 75.0, 81.0, 85.0, 90.0]


def _r(ts, row, sp, ai, zones, **kw):
    return {"ts": ts, "ap_row": row, "ap_sp": sp, "ap_ai": ai, "frac_zones": zones, **kw}


def _by(res):
    return {r["row"]: r for r in res["rows"]}


def test_row_with_fracture_gets_advice_sp_plus_step():
    rows = [_r("a", 2, 600, 640, 1), _r("b", 2, 600, 610, 0), _r("c", 2, 600, 590, 0)]
    r = _by(approach_advice.compute(rows, SP, SVSP, step_um=100, max_gap_um=1500))[2]
    assert (r["n"], r["n_frac"], r["share"]) == (3, 1, 0.33)
    assert r["status"] == "advice" and r["proposed"] == 700
    assert r["ai_frac"] == 640.0 and r["ai_ok"] == 600.0        # аналог: на разломной и в среднем на чистых


def test_clean_row_and_empty_row_have_no_advice():
    res = _by(approach_advice.compute([_r("a", 1, 100, 90, 0)], SP, SVSP))
    assert res[1]["status"] == "ok" and res[1]["proposed"] is None
    assert res[2]["status"] == "none" and res[2]["n"] == 0 and res[2]["share"] is None


def test_gap_ceiling_1500_is_never_exceeded():
    rows = [_r("d", 3, 1400, 1450, 2), _r("e", 4, 1600, 1650, 1)]
    res = _by(approach_advice.compute(rows, SP, SVSP, step_um=200, max_gap_um=1500))
    assert res[3]["proposed"] == 1500, res[3]                   # 1400 + 200 → обрезано до потолка
    assert res[4]["status"] == "ceiling" and res[4]["proposed"] is None, res[4]   # SP уже выше потолка — совета нет


def test_probes_at_other_sp_are_ignored():
    # таблицу поправили (SP строки теперь 600, раньше 500): пробы при 500 к текущей настройке не относятся
    rows = [_r("a", 2, 500, 500, 3), _r("b", 2, 600, 610, 0)]
    r = _by(approach_advice.compute(rows, SP, SVSP))[2]
    assert r["n"] == 1 and r["n_frac"] == 0 and r["status"] == "ok"


def test_min_zones_threshold():
    rows = [_r("a", 2, 600, 640, 1), _r("b", 2, 600, 650, 2)]
    r = _by(approach_advice.compute(rows, SP, SVSP, min_zones=2))[2]
    assert r["n_frac"] == 1 and r["status"] == "advice"


def test_unused_table_rows_are_skipped():
    rows = approach_advice.compute([], SP + [0, 0], SVSP + [0.0, 0.0])["rows"]
    assert [r["row"] for r in rows] == [1, 2, 3, 4]


def test_approach_goes_to_journal_row_and_back(data_dir):
    result = {"ts": "2026-11-10_12_00_00", "stage": 7, "sv": 88.0,
              "approach": {"row": 5, "sp_row": 1000.0, "ai": 1042.5, "enc": 1000.0, "calc": 1000.0, "timeout": False}}
    row = cv_store._hist_row(result)
    assert (row["ap_row"], row["ap_sp"], row["ap_ai"], row["ap_enc"], row["ap_calc"], row["ap_to"]) == (5, 1000.0, 1042.5, 1000.0, 1000.0, 0)
    old = cv_store._hist_row({"ts": "2026-11-10_12_05_00", "stage": 7})   # проба без подвода (старая / ручной снимок)
    assert old["ap_row"] is None and old["ap_to"] is None
    db.upsert_row("S1", row)
    got = db.read_rows("S1")[0]
    assert got["ap_ai"] == 1042.5 and got["ap_row"] == 5
    col = db.conn().execute("SELECT ap_row, ap_ai FROM journal WHERE serial='S1'").fetchone()
    assert (col[0], col[1]) == (5, 1042.5)                      # столбцы созданы миграцией схемы, не только row_json


def test_service_hands_last_approach_to_one_probe_only():
    from microscope_service import MicroscopeService
    s = MicroscopeService()
    s._on_probe_positions({"t": time.time(), "step": "отвод", "row": 3, "ai": 1.0})
    assert s._take_approach() is None                           # отвод — не подвод
    s._on_probe_positions({"t": time.time(), "step": "подвод", "row": 3, "sp_row": 1400.0, "ai": 1450.0, "enc": 1400.0,
                           "calc": 1400.0, "sv": 85.0, "target": 1400.0, "timeout": False})
    ap = s._take_approach()
    assert ap["row"] == 3 and ap["ai"] == 1450.0 and ap["sp_row"] == 1400.0
    assert s._take_approach() is None                           # одна запись — одна проба
    s._on_probe_positions({"t": time.time() - 3600, "step": "подвод", "row": 3, "ai": 1.0})
    assert s._take_approach() is None                           # час назад — уже не «эта» проба


def _svc_with_table():
    from microscope_service import MicroscopeService
    import plate_config
    plate_config.save({"SP": [40000, 100, 600, 800, 1000, 1400, 1600], "SVSP": [0, 75, 81, 83, 85, 88, 95]})
    s = MicroscopeService()
    s.cfg = plate_config.load()
    return s


def test_apply_advice_writes_sp_row_and_can_be_undone(data_dir):
    import plate_config
    s = _svc_with_table()
    r = s.approach_apply(4, 1000, 1100, True)                        # строка СВ от 85: 1000 -> 1100
    assert r == {"status": "ok", "row": 4, "sp": 1100, "on": True}
    assert plate_config.load()["SP"][4] == 1100 and s.cfg["SP"][4] == 1100
    assert plate_config.load()["SP"][3] == 800 and plate_config.load()["SVSP"][4] == 85   # соседние строки и СВ не тронуты
    assert plate_config.load()["approach_correction"]["applied"]["4"]["active"] is True
    back = s.approach_apply(4, None, None, False)                    # галочку сняли — прежний зазор
    assert back["status"] == "ok" and back["sp"] == 1000 and plate_config.load()["SP"][4] == 1000
    assert plate_config.load()["approach_correction"]["applied"]["4"]["active"] is False


def test_apply_advice_refuses_stale_ceiling_and_unknown_row(data_dir):
    import plate_config
    s = _svc_with_table()
    assert "изменилась" in s.approach_apply(4, 900, 1000, True)["error"]            # совет считался от другого SP
    assert "предельного" in s.approach_apply(5, 1400, 1600, True)["error"]          # выше 1500
    assert "отодвигать" in s.approach_apply(4, 1000, 900, True)["error"]            # не дальше
    assert "нет в таблице" in s.approach_apply(40, 1000, 1100, True)["error"]
    assert plate_config.load()["SP"] == [40000, 100, 600, 800, 1000, 1400, 1600]    # ни один отказ ничего не записал
    s.approach_apply(4, 1000, 1100, True)
    plate_config.save({"SP": [40000, 100, 600, 800, 1250, 1400, 1600]})            # таблицу потом правили руками
    s.cfg = plate_config.load()
    assert "менялся" in s.approach_apply(4, None, None, False)["error"]             # откат не затирает ручную правку


def test_advice_marks_applied_row(data_dir):
    s = _svc_with_table()
    s.approach_apply(4, 1000, 1100, True)
    rows = s.approach_advice("S1")["rows"]
    r4 = [r for r in rows if r["row"] == 4][0]
    assert r4["applied"]["from"] == 1000 and r4["applied"]["to"] == 1100
    assert not [r for r in rows if r["row"] == 3][0].get("applied")
    s.approach_apply(4, None, None, False)
    assert "applied" not in [r for r in s.approach_advice("S1")["rows"] if r["row"] == 4][0]


def test_apply_advice_moves_live_fsm_table(data_dir):
    class _F:
        SP = [40000, 100, 600, 800, 1000, 1400, 1600]
    s = _svc_with_table()
    s.fsm = _F()
    s.approach_apply(4, 1000, 1100, True)
    assert s.fsm.SP[4] == 1100                                       # автомат подхватил без перезапуска
    s.approach_apply(4, None, None, False)
    assert s.fsm.SP[4] == 1000
