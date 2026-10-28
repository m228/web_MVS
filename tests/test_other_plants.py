"""Другие заводы на графиках отчёта (other_plants.py): чужая база только на чтение, свою не трогает."""
import sqlite3

import pytest

import db
import other_plants


def _fill(path, serial, n=12, sv0=80.0):
    """База «чужого завода»: одна варка из n проб."""
    db.set_path(path)
    for i in range(n):
        db.upsert_row(serial, {"ts": "2026-10-06_10_%02d_00" % i, "t": 1000.0 + i * 90, "sv": sv0 + i * 0.8, "stage": 7, "cook_time": 600 + i * 90,
                               "fines_m3": 2.0, "mean": 200.0 + i, "count": 50.0, "model": "m", "fines_from_sv": 82.0})
    db.close()


@pytest.fixture
def plants(data_dir, tmp_path, monkeypatch):
    base = tmp_path / "database"
    (base / "kir").mkdir(parents=True)
    own = db.DB_PATH
    _fill(base / "kir" / "mvs.db", "KIRCAM")
    db.set_path(own)
    db.upsert_row("OWN", {"ts": "2026-10-06_09_00_00", "t": 900.0, "sv": 80.0, "stage": 7, "cook_time": 600})
    monkeypatch.setattr(other_plants, "BASE", base)
    return base


def test_names_only_folders_with_db(plants):
    (plants / "empty").mkdir()
    assert other_plants.names() == ["kir"]


def test_no_database_folder(data_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(other_plants, "BASE", tmp_path / "нет такой")
    assert other_plants.names() == [] and other_plants.report_all(0, 1e12) == []


def test_foreign_read_does_not_touch_own_db(plants):
    with db.read_only_at(plants / "kir" / "mvs.db"):
        assert db.serials() == ["KIRCAM"]
        with pytest.raises(sqlite3.OperationalError):          # запись в чужую базу невозможна
            db.conn().execute("DELETE FROM journal")
    assert db.serials() == ["OWN"]                             # после выхода — снова своя база


def test_report_all_reads_foreign_boil(plants):
    res = other_plants.report_all(0, 1e12)
    assert len(res) == 1 and res[0]["name"] == "kir" and res[0]["serial"] == "KIRCAM" and res[0]["fines_from_sv"] == 82.0
    assert db.serials() == ["OWN"]
