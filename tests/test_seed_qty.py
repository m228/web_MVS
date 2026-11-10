"""Количество затравки из ПЛК (поле seed_qty, 10.11): единица в настройках «ПЛК», в журнал пробы, в тренд и в отчёт по варкам.

Без железа и боевой базы (DATA_DIR → tmp, см. conftest.data_dir)."""
import time

import pytest

import cv_store
import db
import plate_config
import plc_source
import report
from microscope_service import PLC_KEYS

BASE = 1_760_000_000
OLD = 3 * 3600


def _row(t, sv, cook, seed=None):
    r = {"ts": time.strftime(cv_store.TS_FMT, time.localtime(t)), "t": float(t), "sv": sv,
         "stage": 7, "cook_time": cook, "fines_m3": 4.0, "vtot_m3": 1.0}
    if seed is not None:
        r["seed_qty"] = seed
    return r


def _boil(t0, seeds):
    return [_row(t0 + i * 90, 85.0 + i * 0.5, 600 + i * 90, s) for i, s in enumerate(seeds)]


def test_seed_field_in_config_both_protocols():
    sv = plate_config.DEFAULTS["sv_source"]
    assert sv["fields"]["seed_qty"]["reg"] == 0                     # по умолчанию не читается
    assert sv["opcua"]["fields"]["seed_qty"]["node"] == ""
    assert sv["seed_unit"] == "kg"
    assert "seed_qty" in plc_source.FIELDS and "seed_qty" in PLC_KEYS


def test_seed_unit_saved_and_public(data_dir):
    patch, _ = plc_source.normalize({"seed_unit": "l"})
    assert patch["seed_unit"] == "l"
    plate_config.save({"sv_source": patch})
    pub = plc_source.public()
    assert pub["seed_unit"] == "l" and pub["seed_units"]["l"] == "л"
    with pytest.raises(ValueError):
        plc_source.normalize({"seed_unit": "тонны"})
    plate_config.save({"sv_source": {"seed_unit": "мусор"}})        # руками испорченный конфиг — не падаем, берём кг
    assert plc_source.seed_unit() == "kg"


def test_seed_in_probe_row_and_journal_column(data_dir):
    row = cv_store._hist_row({"ts": "2026-11-10_10_00_00", "plc": {"seed_qty": 12.5, "temp_app": 70.0}, "summary": {}})
    assert row["seed_qty"] == 12.5
    assert "seed_qty" in db.ROW_COLS and "seed_qty" in cv_store.EXPORT_COLUMNS
    db.upsert_row("S1", row)
    assert db.read_rows("S1")[0]["seed_qty"] == 12.5


def test_seed_per_boil_in_report_and_trend(data_dir):
    a = _boil(BASE, [None, 10.0, 10.0, 10.0, 10.0, 10.0])            # уставка: стоит всю варку
    b = _boil(BASE + OLD, [0.0, 2.0, 5.0, 8.0, 8.0, None])          # счётчик: растёт до итога
    c = _boil(BASE + 2 * OLD, [None] * 6)                           # тег не назначен
    cv_store._HIST_FILLED.add("S2")
    db.upsert_many("S2", a + b + c)
    plate_config.save({"sv_source": {"seed_unit": "pct"}})
    rep = report.build("S2", BASE - 10, BASE + 3 * OLD)
    assert [x["seed_qty"] for x in rep["boils"]] == [10.0, 8.0, None]
    assert rep["seed_unit"] == "%"
    assert rep["totals"]["seed_qty"]["n"] == 2
    tr = cv_store.trend_range("S2", BASE - 10, BASE + OLD - 1, series=["seed_qty"])
    assert tr["series"]["seed_qty"] == [None, 10.0, 10.0, 10.0, 10.0, 10.0]
