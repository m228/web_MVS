"""Время кадров валов: допуск выбора кадра, задержка камеры (latency_s), сравнение камер по времени съёмки, усреднение Δ, панель «Время кадров».

Камеры — заглушки с готовыми рядами углов, ПЛК выключен; один тест гоняет настоящий CameraWorker на синтетических кадрах палок.
"""
import sys
import time
from collections import deque
from pathlib import Path

import pytest

MS = Path(__file__).resolve().parent.parent / "motion_service"
sys.path.insert(0, str(MS))

import motion_engine as me  # noqa: E402
import motion_modbus as mm  # noqa: E402
import motion_shafts as ms  # noqa: E402
from motion_camera import CameraWorker  # noqa: E402
from test_motion_cycle import H, W, rod_frame  # noqa: E402
from test_motion_shafts import cfg_two_rows, zst  # noqa: E402


# ───────────────────────── track_at ─────────────────────────

def test_track_at_interpolates_along_the_short_arc_through_zero():
    s = [(0.0, 350.0, True, False), (1.0, 10.0, True, False)]
    a, valid, coast = ms.track_at(s, 0.5)
    assert abs(((a + 180) % 360) - 180) < 1e-6 and valid and not coast        # 350 → 10 через 0, а не через 180
    a, _, _ = ms.track_at(s, 0.25)
    assert abs(a - 355.0) < 1e-6


def test_track_at_rejects_outside_and_gaps_and_propagates_flags():
    s = [(0.0, 10.0, True, False), (3.0, 40.0, True, False)]
    assert ms.track_at(s, -0.1) is None and ms.track_at(s, 3.1) is None
    assert ms.track_at(s, 1.0) is None                                          # соседи дальше 1,5 с — не интерполируем
    assert ms.track_at(s, 1.0, max_gap=5.0) is not None
    s = [(0.0, 10.0, True, False), (0.5, 12.0, True, True), (1.0, 14.0, False, False)]
    assert ms.track_at(s, 0.2)[2] is True                                       # любой сосед в удержании — удержание
    assert ms.track_at(s, 0.7)[1] is False                                      # любой сосед невалиден — невалидно
    assert ms.track_at([(0.0, 5.0, True, False)], 0.0) is None


# ───────────────────────── конфиг ─────────────────────────

def test_latency_and_shafts_options_are_normalized():
    c = me.normalize_camera({"id": "a", "url": "x"})
    assert c["latency_s"] == 0.0
    assert me.normalize_camera({"id": "a", "url": "x", "latency_s": 2.15})["latency_s"] == 2.15
    with pytest.raises(ValueError):
        me.normalize_camera({"id": "a", "url": "x", "latency_s": -1})
    sh = me.normalize_shafts(None)
    assert sh["align"] is True and sh["avg_s"] == 4.0
    assert me.normalize_shafts({"align": False, "avg_s": 0})["avg_s"] == 0.0
    with pytest.raises(ValueError):
        me.normalize_shafts({"avg_s": 1000})
    assert me.normalize_config(me.normalize_config({}))["shafts"] == me.normalize_shafts(None)


# ───────────────────────── сравнение по времени съёмки ─────────────────────────

OM = 4.5            # °/с левых валов (правые зеркально)


def theta(t):
    return (100.0 + OM * t) % 360.0


class RodWorker:
    """Заглушка камеры: ряд палок с заданными моментами прихода кадров и задержкой, плюс state() с моментом последнего кадра."""

    def __init__(self, arrivals, lag, lat_zones):
        self.arrivals = arrivals
        self.lag = lag                      # настоящая задержка камеры, с (угол кадра = θ(приход − lag))
        self.zones = lat_zones              # {id зоны: "L"/"R"}
        self.timing = {"fps": 3.0, "dt_p50_ms": 350, "dt_p95_ms": 400}

    def rod_track(self, zone):
        side = self.zones[zone]
        out = []
        for a in self.arrivals:
            ang = theta(a - self.lag)
            out.append((a, ang if side == "L" else (360.0 - ang) % 360.0, True, False))
        return out

    def frame_timing(self):
        return dict(self.timing, latency_s=0.0)

    def state(self):
        last = self.arrivals[-1]
        zones = [zst(z, theta(last - self.lag) if s == "L" else (360.0 - theta(last - self.lag)) % 360.0, 0.75) for z, s in self.zones.items()]
        return {"result": {"alive": True, "ts": last, "zones": zones}}


def make_runner(lat_dn, lat_up, lag_up=2.0, avg_s=0.0):
    cfg = cfg_two_rows({"shafts": {"avg_s": avg_s}})
    cfg["modbus"]["enable"] = False
    for c in cfg["cameras"]:
        c["latency_s"] = lat_dn if c["id"] == "dn" else lat_up
    dn = RodWorker([k * 0.35 for k in range(1, 290)], 0.0, {"r_left": "L", "r_right": "R"})
    up = RodWorker([k * 0.5 for k in range(1, 200)], lag_up, {"l": "L", "r": "R"})

    class M:
        links = mm.LinkRegistry(lambda h, p: None)
        workers = {"dn": dn, "up": up}
    M.cfg = cfg
    return ms.ShaftRunner(M), dn, up


def test_pairs_are_compared_at_one_capture_time_when_latency_is_set():
    r, _, _ = make_runner(lat_dn=0.0, lat_up=2.0)                  # настоящая задержка верхней 2 с, в конфиге она указана
    r.tick(100.0)
    p = r.snapshot()["pairs"]["lnv"]
    assert p["valid"] and abs(p["angle_diff"]) < 0.3, p             # физически валы в фазе → Δ ≈ 0
    assert r.snapshot()["timing"]["aligned"] is True


def test_without_latency_the_cameras_disagree_by_omega_times_delay():
    r, _, _ = make_runner(lat_dn=0.0, lat_up=0.0)                  # задержка есть, а в конфиге нуль
    r.tick(100.0)
    p = r.snapshot()["pairs"]["lnv"]
    assert abs(p["angle_diff"] - OM * 2.0) < 0.3, p                 # 2 с × 4,5°/с = 9°


def test_align_off_uses_last_frames_like_before():
    r, _, _ = make_runner(lat_dn=0.0, lat_up=2.0)
    r.mgr.cfg["shafts"]["align"] = False
    r.tick(100.0)
    snap = r.snapshot()
    assert snap["timing"]["aligned"] is False and snap["pairs"]["lnv"]["valid"]


def test_missing_series_falls_back_to_latest_frames():
    r, _, up = make_runner(lat_dn=0.0, lat_up=2.0)
    up.arrivals = [99.9]                                            # камера только запустилась: ряд из одной точки
    r.tick(100.0)
    snap = r.snapshot()
    assert snap["timing"]["aligned"] is False and snap["pairs"]["lnv"]["valid"]


def test_timing_panel_reports_frame_skew_in_seconds_and_degrees():
    r, _, _ = make_runner(lat_dn=0.0, lat_up=2.0)
    r.tick(100.0)
    t = r.snapshot()["timing"]
    assert set(t["cameras"]) == {"dn", "up"} and t["cameras"]["dn"]["fps"] == 3.0
    assert t["compare_lag_s"] == pytest.approx(2.6, abs=0.01)       # макс. задержка 2,0 + запас 0,6
    p = t["pairs"]["lnv"]
    assert p["same_camera"] is False and p["skew_avg_s"] is not None
    assert p["skew_avg_deg"] == pytest.approx(p["skew_avg_s"] * 4.5, abs=0.05)      # 0,75 об/мин = 4,5°/с
    assert t["pairs"]["nlr"]["same_camera"] is True and t["pairs"]["nlr"]["skew_avg_s"] is None


# ───────────────────────── усреднение Δ ─────────────────────────

def test_averaging_smooths_noise_but_keeps_instant_value_and_real_offset():
    cfg = cfg_two_rows({"shafts": {"avg_s": 4.0}})
    cfg["modbus"]["enable"] = False

    class W_:
        def __init__(self):
            self.s = None

        def state(self):
            return self.s

    def st(vl):
        return {"dn": {"result": {"alive": True, "zones": [zst("r_left", 100.0, 0.75), zst("r_right", 260.0, 0.75)]}},
                "up": {"result": {"alive": True, "zones": [zst("l", vl, 0.75), zst("r", 260.0, 0.75)]}}}

    class M:
        links = mm.LinkRegistry(lambda h, p: None)
        workers = {"dn": W_(), "up": W_()}
    M.cfg = cfg
    r = ms.ShaftRunner(M)
    for t in range(1, 13):
        s = st(97.0 if t % 2 else 103.0)                              # Δ = nl − vl = ±3° по тикам
        M.workers["dn"].s, M.workers["up"].s = s["dn"], s["up"]
        r.tick(float(t))
    p = r.snapshot()["pairs"]["lnv"]
    assert abs(p["angle_diff_inst"]) == pytest.approx(3.0, abs=0.01)       # мгновенное — как есть
    assert abs(p["angle_diff"]) < 0.8                                      # среднее за 4 с почти гасит шум
    for t in range(13, 24):                                                # настоящий сдвиг +10° держится — среднее его не прячет
        s = st(90.0)
        M.workers["dn"].s, M.workers["up"].s = s["dn"], s["up"]
        r.tick(float(t))
    assert r.snapshot()["pairs"]["lnv"]["angle_diff"] == pytest.approx(10.0, abs=0.01)


# ───────────────────────── настоящий воркер камеры ─────────────────────────

class RodCap:
    """Файловый источник 25 к/с: кадры палок под разными углами по кругу."""

    def __init__(self):
        self.i = 0
        self.frames = [rod_frame({(250, 270): a, (700, 270): 360 - a}) for a in range(0, 80, 10)]

    def isOpened(self):
        return True

    def grab(self):
        self.i += 1
        return True

    def retrieve(self):
        return True, self.frames[self.i % len(self.frames)].copy()

    def read(self):
        self.grab()
        return self.retrieve()

    def release(self):
        pass

    def set(self, *_):
        return True

    def get(self, *_):
        return 25.0


def test_worker_keeps_rod_track_and_takes_every_second_frame_despite_jitter(tmp_path):
    raw = {"id": "dif", "url": "fake", "hw_accel": "off", "proc_fps": 12.5, "frame_size": [W, H], "latency_s": 1.25,
           "zones": [{"id": "L", "type": "cycle", "center": [250, 270], "radius": 170},
                     {"id": "R", "type": "cycle", "center": [700, 270], "radius": 170}]}
    cfg = me.normalize_camera(raw)
    mb = me.normalize_modbus({"enable": False})
    w = CameraWorker(cfg, mb, mm.LinkRegistry(lambda h, p: None), tmp_path, lambda url, tr: RodCap())
    w.start()
    try:
        t0 = time.time()
        while time.time() - t0 < 12 and len(w.rod_track("L")) < 30:
            time.sleep(0.1)
        tr = w.rod_track("L")
        assert len(tr) >= 30
        assert all(b[0] > a[0] for a, b in zip(tr, tr[1:])) and all(0.0 <= p[1] < 360.0 for p in tr)
        ft = w.frame_timing()
        # камера 25 к/с, просим 12,5: каждый второй кадр. Прежний порог «≥ интервала» из-за дрожания пропускал и этот кадр (≈ 8 к/с)
        assert ft["fps"] >= 10.5, ft
        assert ft["latency_s"] == 1.25 and ft["dt_p95_ms"] is not None
    finally:
        w.stop()
