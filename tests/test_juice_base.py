"""База «Сок»: метка стакана, настройка сбора, запись кадра и строк, чистка старых дней."""
import csv
import sys
import time
from pathlib import Path

import numpy as np
import pytest

MS = Path(__file__).resolve().parent.parent / "motion_service"
sys.path.insert(0, str(MS))

import juice_base as jb  # noqa: E402
import motion_engine as me  # noqa: E402


def cam(**over):
    c = {"id": "juice", "url": "x", "frame_size": [640, 360], "zones": [
        {"id": "g1", "name": "С1", "type": "clarity", "center": [80, 180], "radius": 26, **over.pop("zone", {})}]}
    c.update(over)
    return me.normalize_camera(c)


def test_defaults_and_values():
    c = cam()
    assert c["juice_base"] == {"on": False, "every_min": 2}
    assert c["zones"][0]["label"] == ""
    c = cam(juice_base={"on": True, "every_min": 5}, zone={"label": "dirty"})
    assert c["juice_base"] == {"on": True, "every_min": 5} and c["zones"][0]["label"] == "dirty"


def test_bad_label_rejected():
    with pytest.raises(ValueError):
        cam(zone={"label": "грязь"})


def state(valid=True):
    return {"zones": [
        {"id": "g1", "name": "С1", "type": "clarity", "lab": [18.0, 2.0, -5.0], "turb": 7.0, "level_name": "clear", "valid": valid},
        {"id": "m1", "name": "x", "type": "motion"}]}


def test_rows_for():
    rows = jb.rows_for(state(), cam(zone={"label": "turbid"})["zones"])
    assert rows == [["g1", "С1", 18.0, 2.0, -5.0, 7.0, "clear", 1, "turbid"]]


def test_save_writes_frame_and_csv(tmp_path):
    ts = time.time()
    frame = np.full((360, 640, 3), 90, np.uint8)
    rows = jb.rows_for(state(), cam()["zones"])
    p = jb.save(tmp_path, "juice", ts, frame, rows)
    assert p and p.exists()
    jb.save(tmp_path, "juice", ts + 120, frame, rows)
    with open(p.parent / "index.csv", encoding="utf-8-sig") as fh:
        data = list(csv.DictReader(fh))
    assert len(data) == 2 and data[0]["zone"] == "g1" and data[0]["L"] == "18.00" and data[0]["level"] == "clear"
    assert jb.save(tmp_path, "juice", ts, None, rows) is None and jb.save(tmp_path, "juice", ts, frame, []) is None


def test_purge_old_days(tmp_path):
    now = time.time()
    frame = np.zeros((10, 10, 3), np.uint8)
    rows = jb.rows_for(state(), cam()["zones"])
    jb.save(tmp_path, "juice", now - 20 * 86400, frame, rows)
    jb.save(tmp_path, "juice", now, frame, rows)
    assert jb.purge(tmp_path, "juice", now) == 1
    assert len(list(jb.base_dir(tmp_path, "juice").iterdir())) == 1
