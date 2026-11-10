"""Затравка по видам (10.11): сларри и из маточника МУ — рядом с общей seed_qty. У каждого вида свой тег и своя единица (кг / л / %).

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
KINDS = ("seed_qty", "seed_slurry", "seed_mu")


def _row(t, cook, **seed):
    r = {"ts": time.strftime(cv_store.TS_FMT, time.localtime(t)), "t": float(t), "sv": 85.0, "stage": 7, "cook_time": cook,
         "fines_m3": 4.0, "vtot_m3": 1.0}
    r.update({k: v for k, v in seed.items() if v is not None})
    return r


def test_kinds_in_config_both_protocols():
    sv = plate_config.DEFAULTS["sv_source"]
    for k in KINDS:
        assert sv["fields"][k]["reg"] == 0 and sv["opcua"]["fields"][k]["node"] == ""      # по умолчанию не читаются
        assert k in plc_source.FIELDS and k in PLC_KEYS and k in report.SEED_KEYS
        assert k in db.ROW_COLS and k in cv_store.EXPORT_COLUMNS
    assert sv["seed_slurry_unit"] == "kg" and sv["seed_mu_unit"] == "kg"
    assert set(plc_source.SEED_FIELDS) == set(KINDS) == set(report.SEED_KEYS)


def test_units_independent_and_validated(data_dir):
    patch, _ = plc_source.normalize({"seed_unit": "kg", "seed_slurry_unit": "l", "seed_mu_unit": "pct"})
    plate_config.save({"sv_source": patch})
    pub = plc_source.public()
    assert {k: v["unit"] for k, v in pub["seed_fields"].items()} == {"seed_qty": "kg", "seed_slurry": "l", "seed_mu": "pct"}
    assert pub["seed_fields"]["seed_mu"]["unit_key"] == "seed_mu_unit" and pub["seed_fields"]["seed_slurry"]["title"].endswith("сларри")
    assert plc_source.seed_unit(None, "seed_slurry") == "l" and plc_source.seed_unit(None, "seed_qty") == "kg"
    for bad in ({"seed_slurry_unit": "тонны"}, {"seed_mu_unit": ""}):
        with pytest.raises(ValueError):
            plc_source.normalize(bad)
    plate_config.save({"sv_source": {"seed_mu_unit": "мусор"}})     # испорченный конфиг — не падаем, берём кг
    assert plc_source.seed_unit(None, "seed_mu") == "kg"


def test_only_slurry_unit_saved_survives_reload(data_dir):
    """Конфиг, где со страницы сохранена только единица сларри, не принимают за старый слепок и не выкидывают."""
    plate_config.save({"sv_source": {"seed_slurry_unit": "l"}})
    assert plc_source.seed_unit(None, "seed_slurry") == "l"


def test_kinds_in_probe_row_and_journal(data_dir):
    row = cv_store._hist_row({"ts": "2026-11-10_10_00_00", "plc": {"seed_slurry": 40.0, "seed_mu": 3.5}, "summary": {}})
    assert row["seed_slurry"] == 40.0 and row["seed_mu"] == 3.5 and row["seed_qty"] is None
    db.upsert_row("S1", row)
    back = db.read_rows("S1")[0]
    assert back["seed_slurry"] == 40.0 and back["seed_mu"] == 3.5


def test_kinds_per_boil_report_units_and_trend(data_dir):
    a = [_row(BASE + i * 90, 600 + i * 90, seed_slurry=20.0 if i else None, seed_mu=None) for i in range(6)]       # только сларри
    b = [_row(BASE + OLD + i * 90, 600 + i * 90, seed_slurry=15.0, seed_mu=[0.0, 1.0, 2.0, 3.0, 3.0, None][i]) for i in range(6)]   # оба вида
    c = [_row(BASE + 2 * OLD + i * 90, 600 + i * 90) for i in range(6)]                                              # теги не назначены
    cv_store._HIST_FILLED.add("S3")
    db.upsert_many("S3", a + b + c)
    plate_config.save({"sv_source": {"seed_slurry_unit": "l", "seed_mu_unit": "pct"}})
    rep = report.build("S3", BASE - 10, BASE + 3 * OLD)
    assert [x["seed_slurry"] for x in rep["boils"]] == [20.0, 15.0, None]
    assert [x["seed_mu"] for x in rep["boils"]] == [None, 3.0, None]
    assert [x["seed_qty"] for x in rep["boils"]] == [None, None, None]
    assert rep["seed_units"] == {"seed_qty": "кг", "seed_slurry": "л", "seed_mu": "%"} and rep["seed_unit"] == "кг"
    assert rep["totals"]["seed_slurry"]["n"] == 2 and rep["totals"]["seed_mu"]["n"] == 1
    tr = cv_store.trend_range("S3", BASE + OLD - 1, BASE + 2 * OLD - 1, series=["seed_slurry", "seed_mu"])
    assert tr["series"]["seed_slurry"] == [15.0] * 6 and tr["series"]["seed_mu"][:5] == [0.0, 1.0, 2.0, 3.0, 3.0]
