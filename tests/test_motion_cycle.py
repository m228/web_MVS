"""Зона-цикл (палка из красной и синей половин на оси вала) и группа mirror: углы, рассинхрон, запись в ПЛК.

Кадры синтетические: на зелёном фоне цветная палка под заданным углом (от вертикали по часовой). Плюс регрессия на реальном видео
диффузии, если оно лежит на диске (E:\\grib_dif) — иначе пропускается.
"""
import math
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

MS = Path(__file__).resolve().parent.parent / "motion_service"
sys.path.insert(0, str(MS))

import motion_engine as me  # noqa: E402

W, H = 960, 540
RED, BLUE = (40, 40, 230), (230, 110, 40)       # BGR
VIDEO = Path(r"E:\grib_dif\val_08_09_2026_16_05_32.avi")


def rod_frame(angles: dict, size=(W, H)):
    """{центр: угол красного конца} → цветной кадр с фоном-«шумом» и палками."""
    rng = np.random.default_rng(1)
    img = np.full((size[1], size[0], 3), (60, 130, 70), np.uint8)
    img = np.clip(img.astype(np.int16) + rng.integers(-12, 12, img.shape), 0, 255).astype(np.uint8)
    cv2.rectangle(img, (40, 40), (90, 60), (30, 40, 120), -1)        # статичная «красная» помеха сбоку (ржавчина, рамка окна)
    for (cx, cy), a in angles.items():
        r = math.radians(a)
        dx, dy = math.sin(r), -math.cos(r)
        cv2.line(img, (cx, cy), (int(cx + 150 * dx), int(cy + 150 * dy)), RED, 3)
        cv2.line(img, (cx, cy), (int(cx - 150 * dx), int(cy - 150 * dy)), BLUE, 3)
    return img


def cam_cfg(**over):
    c = {"id": "dif", "url": "x", "frame_size": [W, H], "proc_fps": 4,
         "zones": [{"id": "L", "type": "cycle", "center": [250, 270], "radius": 170, "out": {"kind": "holding", "address": 10}},
                   {"id": "R", "type": "cycle", "center": [700, 270], "radius": 170, "out": {"kind": "holding", "address": 11}}],
         "groups": [{"id": "sync", "type": "mirror", "zones": ["L", "R"], "tol": 10, "out": {"kind": "holding", "address": 12}}]}
    c.update(over)
    return me.normalize_camera(c)


def run(eng, left, right, n=6):
    """n одинаковых кадров подряд (первый только заполняет буфер) → состояние последнего."""
    img = rod_frame({(250, 270): left, (700, 270): right})
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    st = None
    for i in range(n):
        st = eng.process(gray, i * 0.25, img) or st
    return st


def zone(st, zid):
    return next(z for z in st["zones"] if z["id"] == zid)


def test_normalize_cycle_zone_and_mirror_group():
    c = cam_cfg()
    z = c["zones"][0]
    assert z["type"] == "cycle" and z["points"] == [] and z["center"] == [250.0, 270.0] and z["radius"] == 170.0
    g = c["groups"][0]
    assert g["type"] == "mirror" and g["mode"] == "mirror" and g["tol"] == 10 and g["ref"] == 0


@pytest.mark.parametrize("bad, text", [
    ({"zones": [{"id": "L", "type": "cycle"}]}, "центр"),
    ({"zones": [{"id": "L", "type": "cycle", "center": [1, 2], "radius": 5}]}, ".radius"),
    ({"zones": [{"id": "L", "type": "cycle", "center": [1, 2], "red_hsv": [1, 2, 3]}]}, "четыре"),
    ({"groups": [{"id": "g", "type": "mirror", "zones": ["L"]}]}, "две зоны"),
    ({"groups": [{"id": "g", "type": "mirror", "zones": ["L", "R"], "mode": "x"}]}, "режим"),
])
def test_normalize_cycle_errors(bad, text):
    with pytest.raises(ValueError) as e:
        cam_cfg(**bad)
    assert text in str(e.value)


def test_mirror_needs_cycle_zones():
    with pytest.raises(ValueError):
        me.normalize_camera({"id": "c", "url": "x", "zones": [
            {"id": "a", "points": [[0, 0], [9, 0], [9, 9]]}, {"id": "b", "points": [[0, 0], [9, 0], [9, 9]]}],
            "groups": [{"id": "g", "type": "mirror", "zones": ["a", "b"]}]})


@pytest.mark.parametrize("a", [0, 17.5, 90, 133, 180, 225.5, 270, 301, 359])
def test_angle_found_within_two_degrees(a):
    eng = me.CameraEngine(cam_cfg())
    st = run(eng, a, (360 - a) % 360)
    got = zone(st, "L")["angle"]
    assert abs(me.ang_diff(got, a)) <= 2.0, (a, got)
    assert zone(st, "L")["valid"] and zone(st, "R")["valid"]


def test_mirror_delta_zero_and_no_alarm_when_synced():
    eng = me.CameraEngine(cam_cfg())
    st = run(eng, 40, 320)
    g = st["groups"][0]
    assert g["valid"] and abs(g["delta"]) <= 2.5 and g["alarm"] == 0


def test_mirror_alarm_and_signed_delta_when_one_rod_is_off():
    eng = me.CameraEngine(cam_cfg())
    st = run(eng, 40 + 30, 320, n=12)          # левая ушла на +30°
    g = st["groups"][0]
    assert 27 <= g["delta"] <= 33 and g["alarm"] == 1 and g["bits"] == [1]
    eng2 = me.CameraEngine(cam_cfg())
    g2 = run(eng2, 40 - 25, 320, n=12)["groups"][0]
    assert -28 <= g2["delta"] <= -22 and g2["alarm"] == 1
    w = dict(((k, a), v) for k, a, v in eng2.compute_writes(run(eng2, 40 - 25, 320, n=12)))
    assert w[("holding", 12)] == (int(round(g2["delta"] * 10)) & 0xFFFF) and w[("holding", 12)] > 60000   # знаковое → дополнительный код


def test_same_mode_compares_equal_angles():
    c = cam_cfg(groups=[{"id": "sync", "type": "mirror", "mode": "same", "zones": ["L", "R"], "tol": 10}])
    st = run(me.CameraEngine(c), 75, 75)
    assert abs(st["groups"][0]["delta"]) <= 2.5 and st["groups"][0]["alarm"] == 0


def test_ref_shifts_zero_point():
    c = cam_cfg(groups=[{"id": "sync", "type": "mirror", "zones": ["L", "R"], "tol": 10, "ref": 8}])
    st = run(me.CameraEngine(c), 40, 320)
    assert -10.5 <= st["groups"][0]["delta"] <= -5.5       # синхронные валы при ref=8 дают −8


def test_writes_angle_in_tenths_and_status():
    eng = me.CameraEngine(cam_cfg(status_out={"kind": "coil", "address": 3}))
    st = run(eng, 90, 270)
    w = {(k, a): v for k, a, v in eng.compute_writes(st)}
    assert abs(w[("holding", 10)] - 900) <= 20 and abs(w[("holding", 11)] - 2700) <= 20
    assert w[("coil", 3)] == 1


def test_no_rod_found_gives_zeros_and_no_alarm():
    eng = me.CameraEngine(cam_cfg())
    empty = np.full((H, W, 3), (60, 130, 70), np.uint8)
    gray = cv2.cvtColor(empty, cv2.COLOR_BGR2GRAY)
    st = None
    for i in range(10):
        st = eng.process(gray, i * 0.25, empty) or st
    assert not zone(st, "L")["valid"] and st["groups"][0]["alarm"] == 0 and st["groups"][0]["delta"] is None
    assert {(k, a): v for k, a, v in eng.compute_writes(st)}[("holding", 12)] == 0


def test_gray_stream_without_color_frame_does_not_crash():
    eng = me.CameraEngine(cam_cfg())
    g = np.zeros((H, W), np.uint8)
    st = None
    for i in range(4):
        st = eng.process(g, i * 0.25) or st
    assert not zone(st, "L")["valid"]


def test_dead_camera_zeroes_everything():
    eng = me.CameraEngine(cam_cfg())
    run(eng, 40, 320)
    d = eng.dead_state(9.0, "нет кадров")
    assert not d["alive"] and zone(d, "L")["angle"] == 0.0 and d["groups"][0]["alarm"] == 0 and d["groups"][0]["delta"] is None


def test_single_false_peak_does_not_pull_the_angle():
    """Захваченную палку держим около прошлого угла: одиночный кадр с чужим цветом (красная вспышка) угол не уводит."""
    eng = me.CameraEngine(cam_cfg())
    run(eng, 40, 320)
    img = rod_frame({(250, 270): 40, (700, 270): 320})
    img[270:272, 250:420] = RED                          # ложный «красный луч» вправо (≈90°) без синего напротив
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    st = eng.process(gray, 5.0, img)
    assert abs(me.ang_diff(zone(st, "L")["angle"], 40)) <= 3.0


def test_rod_lost_then_found_again_over_whole_circle():
    eng = me.CameraEngine(cam_cfg())
    run(eng, 40, 320)
    empty = np.full((H, W, 3), (60, 130, 70), np.uint8)
    g0 = cv2.cvtColor(empty, cv2.COLOR_BGR2GRAY)
    for i in range(8):
        eng.process(g0, 10 + i * 0.25, empty)
    st = run(eng, 200, 160)                              # палка «выскочила» в другое место: поиск по всему кругу
    assert zone(st, "L")["valid"] and abs(me.ang_diff(zone(st, "L")["angle"], 200)) <= 2.0


@pytest.mark.skipif(not VIDEO.exists(), reason="видео диффузии не лежит на этом компьютере")
def test_real_diffusion_video_mirror_within_tolerance():
    """Реальные валы: на всех кадрах палки найдены, рассинхрон небольшой (камера и объектив дают «горб» до ~11°), тревоги при tol=15 нет."""
    eng = me.CameraEngine(me.normalize_camera({
        "id": "dif", "url": "x", "frame_size": [1920, 1080], "proc_fps": 4,
        "zones": [{"id": "L", "type": "cycle", "center": [318, 291], "radius": 340},
                  {"id": "R", "type": "cycle", "center": [1015, 316], "radius": 340}],
        "groups": [{"id": "sync", "type": "mirror", "zones": ["L", "R"], "tol": 15}]}))
    cap = cv2.VideoCapture(str(VIDEO))
    deltas, alarms, t = [], 0, 0.0
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        st = eng.process(cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY), t, fr)
        t += 0.25
        if st:
            g = st["groups"][0]
            if g["delta"] is not None:
                deltas.append(g["delta"])
            alarms += g["alarm"]
    assert len(deltas) >= 230
    assert max(abs(d) for d in deltas) <= 15 and alarms == 0
    assert abs(float(np.median(deltas))) <= 6
