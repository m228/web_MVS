"""Автоподгонка палки (motion_autocal): оценка по ходу, хранилище кадров, подбор цвета и оси на синтетике.

Синтетика: бирюзовый фон с шумом (оттенок рядом с синей половиной палки, как на корпусе val_Down), палка из красной и синей половин,
вращающаяся ровно ≈0,8 об/мин, неподвижная красная «шестерня»-приманка шире палки и тонкая красная рейка. Стартовые настройки подгонки
намеренно плохие: ось сдвинута на ~10 px, синий диапазон захватывает фон. Плюс боевой разбор (CycleRT.step_profiles) = step.
"""
import math
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

MS =Path(__file__).resolve().parent.parent / "motion_service"
sys.path.insert(0, str(MS))

import motion_autocal as ac  # noqa: E402
import motion_engine as me  # noqa: E402

W, H = 800, 600
CX, CY, R = 400, 300, 170
FPS = 1.0
RPM = 0.8


def hsv_bgr(h, s, v):
    return tuple(int(x) for x in cv2.cvtColor(np.uint8([[[h, s, v]]]), cv2.COLOR_HSV2BGR)[0, 0])


BG, BLUE, RED, WHEEL = hsv_bgr(88, 150, 150), hsv_bgr(100, 210, 210), hsv_bgr(176, 220, 210), hsv_bgr(172, 190, 110)


def frame(angle, seed=0):
    rng = np.random.default_rng(seed)
    img = np.full((H, W, 3), BG, np.uint8)
    img = np.clip(img.astype(np.int16) + rng.integers(-10, 10, img.shape), 0, 255).astype(np.uint8)
    cv2.circle(img, (CX, CY - 40), 150, WHEEL, 18)                       # красная «шестерня» шире палки: фильтр «тонкого» её отсекает
    cv2.line(img, (40, 60), (300, 90), RED, 2)                           # тонкая красная рейка сбоку
    r = math.radians(angle)
    dx, dy = math.sin(r), -math.cos(r)
    cv2.line(img, (CX, CY), (int(CX + 160 * dx), int(CY + 160 * dy)), RED, 4)
    cv2.line(img, (CX, CY), (int(CX - 160 * dx), int(CY - 160 * dy)), BLUE, 4)
    return img


def make_store(n=130, rpm=RPM):
    st = ac.JpegStore(95)
    for i in range(n):
        st.add(i / FPS, frame((30 + 6.0 * rpm * i / FPS) % 360, seed=i))
    return st


def base_cfg(**over):
    c = {"id": "r", "name": "r", "r_min": 0.1, "red_hsv": [16, 165, 50, 55], "blue_hsv": [82, 135, 55, 60], "min_score": 0.4, "min_conf": 0.1,
         "jump_deg": 25.0, "coast_s": 30.0, "red_min": 0.12, "blue_alone": 0.6, "blue_w": 0.5, "blue_min": 0.3, "agree_deg": 20.0,
         "thin": 9, "rpm_window": 20.0, "stop_s": 10.0, "stop_deg": 5.0, "smooth": 3}
    c.update(over)
    return c


def test_step_profiles_same_as_step():
    cfg = dict(base_cfg(), center=[CX, CY], radius=R)
    a, b = me.CycleRT(cfg, [W, H], (W, H)), me.CycleRT(cfg, [W, H], (W, H))
    for i, ang in enumerate((10, 40, 70, 100, 130)):
        f = frame(ang)
        red, blue = b._liks(f[b.y0:b.y1, b.x0:b.x1])
        fa = a.step(f, i * 1.0)
        fb = b.step_profiles(red[b.ys, b.xs].mean(axis=1), blue[b.ys, b.xs].mean(axis=1), i * 1.0)
        assert fa == fb and a.angle == b.angle and a.src == b.src and a.conf == b.conf
    assert b.step_profiles(None, None, 9.0) is False and b.coast


def test_jpeg_store_roundtrip():
    st = ac.JpegStore(95)
    f = frame(50)
    st.add(3.5, f)
    g = st.get(0)
    assert len(st) == 1 and st.t == [3.5] and g.shape == f.shape and st.nbytes > 0
    assert np.abs(g.astype(int) - f.astype(int)).mean() < 6


def test_split_frames_covers_all_angles():
    tr, va = ac.split_frames(150)
    assert sorted(tr + va) == list(range(150)) and 0.25 < len(va) / 150 < 0.4
    angles = [(i * 4.8) % 360 for i in va]                                 # отложенные кадры вала в 4,8°/кадр охватывают весь круг
    assert len({int(a // 30) for a in angles}) >= 10


def test_score_track_prefers_steady_rotation():
    n = 120
    t = np.arange(n) * 1.0
    ok = np.ones(n, bool)
    good = ac.score_track(ok, (30 + 4.8 * t) % 360, t)
    assert good["score"] > 0.9 and 0.7 < good["rpm"] < 0.9 and good["revs"] > 1.4
    still = ac.score_track(ok, np.full(n, 120.0), t)                        # залипла на неподвижной цели
    assert still["score"] < 0.1
    rng = np.random.default_rng(3)
    noise = ac.score_track(ok, rng.uniform(0, 360, n), t)                  # прыгает куда попало
    assert noise["score"] < 0.2
    half = ok.copy()
    half[40:80] = False                                                    # большая дыра: найденность и охват падают
    assert ac.score_track(half, (30 + 4.8 * t) % 360, t)["score"] < good["score"] * 0.8
    assert ac.score_track(np.zeros(n, bool), np.zeros(n), t)["score"] == 0.0


def test_autotune_recovers_colours_and_axis():
    st = make_store()
    steps = []
    tn = ac.RodTuner(st, base_cfg(), (CX + 9, CY - 7), R, max_radius=R * 1.2, progress=lambda p, s: steps.append(p))
    res = tn.run()
    assert res["improved"], res["log"]
    assert res["after"]["found"] >= 0.95 and res["after_val"]["found"] >= 0.93, res["after"]
    assert res["after"]["score"] > res["before"]["score"]
    cx, cy = res["new"]["center"]
    assert math.hypot(cx - CX, cy - CY) <= 6.0, (cx, cy)                    # плато оценки оси ≈ ширина палки; от старта в 9 px это в разы ближе
    assert 0.7 < res["after"]["rpm"] < 0.9 and res["after"]["revs"] > 1.4
    assert steps and steps[-1] == 1.0 and all(b >= a - 1e-9 for a, b in zip(steps, steps[1:]))


def test_autotune_cancel():
    st = make_store(40)
    calls = []

    def cancelled():
        calls.append(1)
        return len(calls) > 3

    tn = ac.RodTuner(st, base_cfg(), (CX, CY), R, cancelled=cancelled)
    try:
        tn.run()
        assert False, "должна была сработать отмена"
    except ac.Cancelled:
        pass


# ───────────── задание на сервисе: запись + подгонка + применение ─────────────
class FakeClock:
    """Часы без ожидания: время идёт только от sleep, поэтому запись «100 с» в тесте мгновенна и не зависит от нагрузки на машину."""

    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeWorker:
    def __init__(self, zone_cfg, clock):
        self.cfg = {"zones": [zone_cfg], "frame_size": [W, H]}
        self.clock = clock
        self.t0 = clock.time()
        self.n = 0

    def autocal_frame(self):
        t = self.clock.time()
        self.n += 1
        return frame((30 + 4.8 * (t - self.t0)) % 360, seed=self.n), t


def test_job_records_tunes_and_reports(monkeypatch):
    import motion_autocal_job as aj
    clock = FakeClock()
    monkeypatch.setattr(aj, "time", clock)
    zone = me.normalize_camera({"id": "c", "url": "x", "frame_size": [W, H],
                                "zones": [{"id": "rod1", "name": "Вал левый", "type": "cycle", "center": [CX + 8, CY - 6], "radius": R}]})["zones"][0]
    w = FakeWorker(zone, clock)
    job = aj.AutocalJob("c", lambda cid: w, [], 100)
    job.start()
    seen = set()
    while job.is_alive():
        seen.add(job.status()["state"])
        time.sleep(0.05)
    st = job.status()
    assert st["state"] == "done", st
    assert "tuning" in seen
    r = st["results"]["rod1"]
    assert r["improved"] and r["after"]["found"] >= 0.9 and r["after_val"]["found"] >= 0.9, (r["before"], r["after"], r["after_val"], r["frames"])
    assert math.hypot(r["new"]["center"][0] - CX, r["new"]["center"][1] - CY) <= 6.5
    assert 0.6 < r["after"]["rpm"] < 1.0 and r["after"]["revs"] > 1.2
    assert r["old"]["center"] == [CX + 8, CY - 6] and len(r["after"]["sectors"]) == 12
    import json
    json.dumps(st)                                                          # статус целиком отдаётся в JSON


def test_job_fails_politely_without_frames(monkeypatch):
    import motion_autocal_job as aj
    clock = FakeClock()
    monkeypatch.setattr(aj, "time", clock)
    monkeypatch.setattr(aj, "NO_FRAME_S", 1.0)
    zone = me.normalize_camera({"id": "c", "url": "x", "frame_size": [W, H], "zones": [{"id": "rod1", "type": "cycle", "center": [CX, CY], "radius": R}]})["zones"][0]
    w = FakeWorker(zone, clock)
    w.autocal_frame = lambda: (None, 0.0)
    job = aj.AutocalJob("c", lambda cid: w, [], 75)
    job.start()
    job.join(20)
    st = job.status()
    assert st["state"] == "error" and "кадры" in st["error"]


def test_manager_apply_patches_only_improved_zones(tmp_path, monkeypatch):
    import server
    import motion_autocal_job as aj
    mgr = server.Manager(tmp_path)
    cam = {"id": "c", "name": "cam", "url": "x", "enabled": False, "frame_size": [W, H],
           "zones": [{"id": "a", "type": "cycle", "center": [10, 10], "radius": 100}, {"id": "b", "type": "cycle", "center": [20, 20], "radius": 100}]}
    mgr.cfg = me.normalize_config({"cameras": [cam]})
    applied = []
    monkeypatch.setattr(mgr, "apply", lambda cfg, save=True: applied.append(cfg) or cfg)
    job = aj.AutocalJob("c", lambda cid: None)
    new = {"center": [11.5, 12.5], "radius": 90.0, "red_hsv": [16, 155, 35, 55], "blue_hsv": [96, 135, 55, 60], "thin": 9, "blue_alone": 0.6}
    job.st.update(state="done", results={"a": {"improved": True, "new": new}, "b": {"improved": False, "new": new}})
    mgr.autocal["c"] = job
    out = mgr.autocal_apply("c", [])
    assert out == {"applied": ["a"], "skipped": ["b"]} and job.status()["applied"] == ["a"]
    za, zb = (z for z in applied[0]["cameras"][0]["zones"])
    assert za["center"] == [11.5, 12.5] and za["blue_hsv"] == [96, 135, 55, 60] and za["radius"] == 90.0
    assert zb["center"] == [20.0, 20.0] and zb["radius"] == 100.0           # не улучшившаяся палка не тронута
    with pytest.raises(KeyError):
        mgr.autocal_apply("nope", [])
