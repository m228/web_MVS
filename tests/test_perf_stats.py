"""Замеры времени обработки картинок: статистика окна, копии модуля, запись замеров движком движения."""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "motion_service"))

import perf_stats as ps  # noqa: E402
import motion_engine as me  # noqa: E402


def test_snapshot_stats():
    p = ps.Perf(window=100)
    for i in range(1, 101):
        p.record("a:x", float(i), got=2, of=3, budget_ms=200.0)
    s = p.snapshot()["a:x"]
    assert s["n"] == 100 and s["last"] == 100.0 and s["max"] == 100.0
    assert abs(s["avg"] - 50.5) < 0.01 and 94 <= s["p95"] <= 96
    assert s["got"] == 2 and s["of"] == 3
    assert abs(s["load"] - 25.25) < 0.1


def test_window_forgets_old():
    p = ps.Perf(window=5)
    for v in (1000, 1000, 1, 1, 1, 1, 1):
        p.record("k", v)
    s = p.snapshot()["k"]
    assert s["max"] == 1.0 and s["n"] == 7


def test_no_budget_no_load():
    p = ps.Perf()
    p.record("k", 5.0)
    assert p.snapshot()["k"]["load"] is None


def test_copies_identical():
    assert (ROOT / "perf_stats.py").read_bytes() == (ROOT / "motion_service" / "perf_stats.py").read_bytes()


def test_engine_records_zones_and_shafts():
    ps.PERF.drop("tcam:")
    cam = me.normalize_camera({"id": "tcam", "url": "x", "proc_fps": 3, "frame_size": [640, 360], "zones": [
        {"id": "z1", "name": "z1", "type": "motion", "points": [[10, 10], [200, 10], [200, 200], [10, 200]]},
        {"id": "c1", "name": "c1", "type": "cycle", "center": [400, 180], "radius": 80},
    ]})
    eng = me.CameraEngine(cam)
    frame = np.full((360, 640, 3), 90, np.uint8)
    gray = frame[:, :, 0]
    for i in range(4):
        eng.process(gray, 0.33 * i, frame)
    snap = ps.PERF.snapshot()
    assert "tcam:zones" in snap and "tcam:shafts" in snap
    assert snap["tcam:zones"]["of"] == 1 and snap["tcam:shafts"]["of"] == 1
    assert snap["tcam:shafts"]["budget_ms"] > 300 and snap["tcam:shafts"]["avg"] >= 0
