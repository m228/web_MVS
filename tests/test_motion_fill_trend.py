"""motion_service: тренды признака «вид окна» (tex / lum / rel) — ряд для графика и среднее / мин / макс за период."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "motion_service"))

from motion_fill_trend import FillTrend  # noqa: E402


def _feed(ft, t0, seconds, per_s=4, lum=lambda i: 100.0, ok=lambda i: True):
    for i in range(seconds * per_s):
        ts = t0 + i / per_s
        ft.add(ts, {"z": (1.0, lum(i), 0.5, 1 if lum(i) > 150 else 0)}, ok(i))


def test_stats_avg_min_max():
    ft = FillTrend()
    t0 = time.time() - 120
    _feed(ft, t0, 60, lum=lambda i: 100.0)          # первая минута «пусто»
    _feed(ft, t0 + 60, 60, lum=lambda i: 200.0)     # вторая «жом»
    s = ft.stats(300, now=t0 + 120)["z"]
    assert s["lum"]["min"] == 100.0 and s["lum"]["max"] == 200.0
    assert abs(s["lum"]["avg"] - 150.0) < 1.0
    assert s["tex"]["avg"] == 1.0 and s["rel"]["min"] == 0.5


def test_stats_window_and_bad_frames():
    ft = FillTrend()
    t0 = time.time() - 120
    _feed(ft, t0, 60, lum=lambda i: 100.0, ok=lambda i: False)     # битые кадры в статистику не идут
    _feed(ft, t0 + 60, 60, lum=lambda i: 200.0)
    assert ft.stats(300, now=t0 + 120)["z"]["lum"]["min"] == 200.0
    short = ft.stats(30, now=t0 + 120)["z"]
    assert short["lum"]["avg"] == 200.0


def test_history_series_and_squeeze():
    ft = FillTrend()
    t0 = time.time() - 600
    _feed(ft, t0, 600, per_s=3, lum=lambda i: 100.0 if i < 900 else 200.0)
    h = ft.history(600, 60)
    assert 50 <= len(h["t"]) <= 60 and len(h["t"]) == len(h["ok"])
    z = h["zones"]["z"]
    assert len(z["lum"]) == len(h["t"]) and z["lum"][0] == 100.0 and z["lum"][-1] == 200.0
    assert z["fill"][0] == 0 and z["fill"][-1] == 1
    short = ft.history(60, 600)
    assert short["t"][-1] - short["t"][0] <= 60.5


def test_no_fill_zones_nothing_stored():
    ft = FillTrend()
    ft.add(time.time(), {}, True)
    assert ft.history(60) == {} and ft.stats(60) == {}



def test_registry_survives_recreate_and_drops_changed_zone():
    from motion_fill_trend import fill_trend_for
    key = ("t", "cam_reg")
    t0 = time.time() - 60
    ft = fill_trend_for(key, {"z": "a"})
    _feed(ft, t0, 30)
    again = fill_trend_for(key, {"z": "a"})                     # камеру пересоздали (правка порога): ряд на месте
    assert again is ft and "z" in again.history(120)["zones"]
    fill_trend_for(key, {"z": "b"})                              # поменяли полигон / признак: значения прежнего окна больше не годятся
    assert ft.history(120) == {"t": [], "ok": [], "zones": {}} or "z" not in ft.history(120)["zones"]
    assert ft.stats(120) == {}
