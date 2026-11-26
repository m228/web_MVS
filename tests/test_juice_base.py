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


def test_rows_without_references_keep_color_and_blank_turb():
    st = state()
    st["zones"][0].update(valid=False, turb=0.0, level_name="clear")
    rows = jb.rows_for(st, cam()["zones"])
    assert rows[0][2:5] == [18.0, 2.0, -5.0] and rows[0][5] == "" and rows[0][6] == "" and rows[0][7] == 0
    assert jb.worth_saving(rows) is True                    # цвет есть — снимок пишем, хотя эталонов нет


def test_not_worth_saving_without_color():
    st = state()
    st["zones"][0].update(valid=False, lab=None)
    assert jb.worth_saving(jb.rows_for(st, cam()["zones"])) is False


def test_camera_tick_writes_even_without_references(tmp_path):
    """Сквозная проверка: на стенде эталоны не были сняты (valid = False у всех), и база не писалась ни разу."""
    from types import SimpleNamespace
    import motion_camera as mc
    st = state(valid=False)
    st.update(ts=time.time(), alive=True)
    cfg = cam(juice_base={"on": True, "every_min": 2}, zone={"label": ""})
    stub = SimpleNamespace(cfg=cfg, data_dir=tmp_path, id="juice", _juice_last=0.0, _juice_purged=0.0)
    mc.CameraWorker._juice_tick(stub, st, np.full((360, 640, 3), 90, np.uint8), True)
    files = list((tmp_path / "juice_base" / "juice").rglob("*.jpg"))
    assert len(files) == 1 and (files[0].parent / "index.csv").exists()
