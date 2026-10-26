"""Характеризационные тесты cv_store: нарезка журнала на варки, фазы, финиш, защёлка «Мука с СВ».

Это самый сложный и не покрытый тестами модуль — и будущая цель оптимизации чтения журнала.
Тесты фиксируют ТЕКУЩЕЕ поведение на синтетике, чтобы любую оптимизацию можно было сверить
«до/после». Без железа, DATA_DIR → tmp (фикстура data_dir патчит cv_store.BASE/HISTORY)."""
import time

import cv_store
import db


def _seed(serial, rows):
    cv_store._HIST_FILLED.add(serial)          # не лезть в cv_results (их нет) — только журнал
    db.upsert_many(serial, rows)


def _row(t, sv, cook, stage=7, phase=None, fines=4.0):
    return {"ts": time.strftime(cv_store.TS_FMT, time.localtime(t)), "t": float(t), "sv": sv,
            "stage": stage, "cook_time": cook, "fines_m3": fines, "vtot_m3": 1.0}


def test_phases_for_boil_order():
    # порядок стадий 5/7 → p1 g1 p2 g2; 3-я подкачка вливается в p2
    stages = [3, 5, 5, 7, 7, 5, 7, 5]
    assert cv_store.phases_for_boil(stages) == [None, "p1", "p1", "g1", "g1", "p2", "g2", "p2"]


def test_phases_none_inherits_previous():
    assert cv_store.phases_for_boil([5, None, 7]) == ["p1", "p1", "g1"]


def test_group_boils_splits_on_long_gap(data_dir):
    base = 1_760_000_000
    rows = [_row(base + i * 90, 85.0 + i, 600 + i * 90) for i in range(4)]
    rows += [_row(base + 4 * 90 + cv_store.BOIL_GAP_S + 100, 86.0, 600)]   # пауза > BOIL_GAP_S → новая варка
    _seed("B1", rows)
    groups = cv_store.boil_groups("B1")
    assert len(groups) == 2 and len(groups[0]) == 4 and len(groups[1]) == 1


def test_group_boils_splits_on_cook_time_drop(data_dir):
    base = 1_760_100_000
    rows = [_row(base, 85, 600), _row(base + 90, 86, 690), _row(base + 180, 80.5, 20, stage=3)]
    _seed("B2", rows)
    groups = cv_store.boil_groups("B2")
    assert len(groups) == 2


def test_finish_probes_selects_last_sv_window():
    # финиш — пробы в пределах fin_sv от конечного СВ (медиана последних 5 с мукой)
    rows = [{"sv": 80.0}, {"sv": 86.0}, {"sv": 88.0}, {"sv": 88.5}, {"sv": 89.0}]
    sel, ref = cv_store.finish_probes(rows, fin_sv=2.0)
    assert ref == 88.0           # медиана последних 5
    assert all(r["sv"] >= 86.0 for r in sel) and {80.0} not in [{r["sv"]} for r in sel]


def test_fines_gate_latches_from_threshold(data_dir, monkeypatch):
    # мука считается с первой пробы, где СВ достигло порога, и до конца варки (просадка не гасит)
    monkeypatch.setattr(cv_store, "_volume_cfg_now", lambda: {"volume": {"fines_from_sv": 88.0}})
    base = 1_760_200_000
    rows = [_row(base + i * 90, sv, 600 + i * 90, fines=5.0)
            for i, sv in enumerate((80.0, 85.0, 88.0, 86.0, 90.0))]
    for r in rows:
        r["fsieve_m3_b0"] = 1.0
    out = cv_store._apply_fines_gate([dict(r) for r in rows])
    # до достижения 88 (первые две) — обнулено; с третьей и далее (включая просадку 86) — считается
    assert out[0]["fsieve_m3_b0"] is None and out[1]["fsieve_m3_b0"] is None
    assert out[2]["fsieve_m3_b0"] == 1.0 and out[3]["fsieve_m3_b0"] == 1.0 and out[4]["fsieve_m3_b0"] == 1.0


def test_boils_counts_probes_and_finish(data_dir):
    base = 1_760_300_000
    rows = [_row(base + i * 90, 85.0 + i * 0.5, 600 + i * 90, fines=4.0 + i) for i in range(6)]
    _seed("B3", rows)
    out = cv_store.boils("B3", now=base + 6 * 90 + 10)
    assert len(out) == 1 and out[0]["n"] == 6
    assert out[0]["finished"] is False          # последняя проба свежая → варка идёт


def test_export_rows_columns(data_dir):
    base = 1_760_400_000
    _seed("B4", [_row(base + i * 90, 88.0, 600 + i * 90) for i in range(3)])
    rows = cv_store.export_rows("B4")
    assert len(rows) == 3
    assert set(cv_store.EXPORT_COLUMNS).issubset(rows[0].keys())
