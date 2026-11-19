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
    # порядок стадий 5/6/7 → p1 g1 p2 g2 …; 3-я подкачка — своя фаза p3 (раньше вливалась в p2)
    stages = [3, 5, 5, 7, 7, 5, 7, 5]
    assert cv_store.phases_for_boil(stages) == [None, "p1", "p1", "g1", "g1", "p2", "g2", "p3"]


def test_phases_stabilization_has_own_phase():
    assert cv_store.phases_for_boil([5, 6, 6, 7, 6, 7, 8]) == ["p1", "s1", "s1", "g1", "s2", "g2", None]


def test_phases_by_substage_number():
    # номер = подэтап − 1: 72 → рост 1, 73 → рост 2; 70 / 71 / пусто / подстадия другого этапа — по порядку входа в стадию
    assert cv_store.phases_for_boil([7, 7, 7, 7], [70, 72, 73, 74]) == ["g1", "g1", "g2", "g3"]
    assert cv_store.phases_for_boil([5, 6], [53, 62]) == ["p2", "s1"]
    assert cv_store.phases_for_boil([6, 7], [70, 60]) == ["s1", "g1"]
    assert cv_store.phases_for_boil([5, 7, 5, 7], [None] * 4) == ["p1", "g1", "p2", "g2"]


def test_phases_special_substages():
    # расчистка / рост БП / рост ЗП — свои фазы, только по подстадии
    assert cv_store.phases_for_boil([5, 5, 7, 7], [50, 59, 78, 79]) == ["p1", "clr", "gbp", "gzp"]
    assert cv_store.phases_for_boil([5, 7], [59, 70]) == ["clr", "g1"]


def test_phases_none_inherits_previous():
    assert cv_store.phases_for_boil([5, None, 7]) == ["p1", "p1", "g1"]


def test_boil_summary_phase_blocks_include_stabilization():
    # стабилизация — своя плитка рассева (блок phases), но в итог варки входит (сита и порог у неё общие, как у финиша)
    def row(i, stage, phase, fines):
        r = {"ts": "2026-10-06_10_%02d_00" % i, "t": 1000.0 + i * 90, "sv": 86.0, "stage": stage, "phase": phase, "cook_time": 600 + 90 * i,
             "fines_m3": fines, "fines_m1": fines, "fines_m2": fines, "vtot_m1": 1.0, "vtot_m2": 1.0, "vtot_m3": 1.0, "good_n": 100}
        for m in ("m1", "m2", "m3", "area"):
            for b in range(7):
                r["sieve_%s_b%d" % (m, b)] = 100.0 if b == 1 else 0.0
        return r
    rows = [row(0, 5, "p1", 60.0), row(1, 6, "s1", 20.0), row(2, 7, "g1", 30.0), row(3, 6, "s2", 10.0), row(4, 7, "g2", 12.0), row(5, 8, None, 8.0)]
    sm = cv_store._boil_summary(rows, True)
    assert list(sm["phases"]) == ["p1", "s1", "g1", "s2", "g2"], "фазы в порядке появления в варке"
    assert sm["phases"]["s1"]["title"] == "стабилизация 1" and sm["phases"]["s2"]["title"] == "стабилизация 2"
    assert sm["counted"] == 3, "в итоге варки: стабилизации и проба без фазы (подкачка/рост — отдельно)"


def test_rephase_journal_relabels_once(data_dir):
    # старые пробы: стабилизация без фазы, 3-я подкачка названа p2 → один раз пересчитываем подписи; повторно не трогаем
    base = 1_760_300_000
    stages = [4, 5, 6, 7, 5, 7, 5, 8]
    old = ["x", "p1", None, "g1", "p2", "g2", "p2", None]
    rows = []
    for i, (st, ph) in enumerate(zip(stages, old)):
        r = _row(base + i * 90, 85.0 + i * 0.2, 600 + i * 90, stage=st)
        r["phase"] = None if ph == "x" else ph
        rows.append(r)
    _seed("RP1", rows)
    assert cv_store.rephase_journal("RP1") == 2                      # стабилизация → s1, 3-я подкачка → p3; остальные подписи уже верные
    ph =[r["phase"] for r in sorted(db.read_rows("RP1"), key=lambda r: r["t"])]
    assert ph == [None, "p1", "s1", "g1", "p2", "g2", "p3", None], ph
    assert cv_store.rephase_journal("RP1") == 0, "метка в meta: второй раз не запускается"


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
