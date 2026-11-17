"""Зона-цикл (палка из красной и синей половин на оси вала) и группа mirror: углы, рассинхрон, запись в ПЛК.

Кадры синтетические: на зелёном фоне цветная палка под заданным углом (от вертикали по часовой). Плюс регрессия на реальном видео
диффузии, если оно лежит на диске (E:\\grib_dif) — иначе пропускается.
"""
import json
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
        eng.process(g0, 10 + i * 5.0, empty)             # дольше coast_s (30 с) не видно — палка потеряна
    st = run(eng, 200, 160)                              # палка «выскочила» в другое место: поиск по всему кругу
    assert zone(st, "L")["valid"] and abs(me.ang_diff(zone(st, "L")["angle"], 200)) <= 2.0


def test_rod_hidden_for_a_while_is_held_and_edge_decoy_does_not_steal_it():
    """Палки не видно (переход по зелёному, блик), а рядом ровная красно-синяя кромка: угол держим по скорости, на кромку не прыгаем."""
    eng = me.CameraEngine(cam_cfg())
    spin(eng, 10, 6.0, seconds=20)                       # вращаются 6°/с: к t=20 левая ≈ 130°, правая ≈ 230°
    decoy = rod_frame({})
    for cx, cy in ((250, 270), (700, 270)):
        cv2.line(decoy, (cx, cy), (cx - 150, cy), RED, 3)      # красный влево (270°), синий вправо (90°) — как кромка кожуха
        cv2.line(decoy, (cx, cy), (cx + 150, cy), BLUE, 3)
    gray = cv2.cvtColor(decoy, cv2.COLOR_BGR2GRAY)
    for i in range(40):                                  # 10 с без палок
        t = 20.0 + i * 0.25
        st = eng.process(gray, t, decoy)
        z = zone(st, "L")
        assert z["valid"] and z["coast"] and z["bit"] == 0, (t, z)
        assert abs(me.ang_diff(z["angle"], 10 + 6.0 * t)) <= 4.0, (t, z["angle"])
    assert st["groups"][0]["valid"]                      # группа не слепнет: считается по удерживаемым углам
    img = rod_frame({(250, 270): (10 + 6.0 * 30.5) % 360, (700, 270): (360 - 10 - 6.0 * 30.5) % 360})
    st = eng.process(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), 30.5, img)
    z = zone(st, "L")
    assert z["valid"] and not z["coast"] and z["bit"] == 1
    assert abs(me.ang_diff(z["angle"], 10 + 6.0 * 30.5)) <= 2.0


def test_fat_colour_blobs_are_not_mistaken_for_the_rod():
    """Широкие красное/синее пятна (кожух, тень) — не палка: ищем только тонкие полоски."""
    img = rod_frame({(250, 270): 40, (700, 270): 320})
    cv2.rectangle(img, (290, 200), (430, 340), RED, -1)        # пятно справа от оси (≈90°) на всю длину луча ...
    cv2.rectangle(img, (70, 200), (210, 340), BLUE, -1)        # ... и синее напротив
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    eng = me.CameraEngine(cam_cfg())
    st = None
    for i in range(4):
        st = eng.process(gray, i * 0.25, img) or st
    assert abs(me.ang_diff(zone(st, "L")["angle"], 40)) <= 2.0


def test_coast_off_loses_the_rod_at_once():
    eng = me.CameraEngine(cam_cfg(zones=[{"id": "L", "type": "cycle", "center": [250, 270], "radius": 170, "coast_s": 0},
                                         {"id": "R", "type": "cycle", "center": [700, 270], "radius": 170, "coast_s": 0}], groups=[]))
    run(eng, 40, 320)
    empty = np.full((H, W, 3), (60, 130, 70), np.uint8)
    st = eng.process(cv2.cvtColor(empty, cv2.COLOR_BGR2GRAY), 5.0, empty)
    assert not zone(st, "L")["valid"]


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


# ───────────── таблица поправки зеркальности (перекос от угла камеры и объектива) ─────────────
def _bias(a):
    """Выдуманный перекос камеры: до ±8° в зависимости от положения левой палки."""
    return 8.0 * math.sin(math.radians(a - 20))


def test_build_mirror_table_follows_the_bias_and_fills_gaps():
    rng = np.random.default_rng(3)
    samples = [(a, 360 + _bias(a) + rng.normal(0, 0.4)) for a in rng.uniform(0, 360, 400) if not 100 < a < 118]   # пробел в 18°
    table, info = me.build_mirror_table(samples)
    assert len(table) == me.MIRROR_BINS and info["bins"] >= 33
    for i, v in enumerate(table):
        assert abs(v - _bias(i * 10)) <= 1.5, (i, v, _bias(i * 10))
    assert abs(me.mirror_corr(table, 95) - _bias(95)) <= 1.5 and abs(me.mirror_corr(table, 359.9) - _bias(359.9)) <= 1.5


@pytest.mark.parametrize("samples, text", [
    ([(a, 360.0) for a in range(0, 30)], "мало данных"),
    ([(a % 120, 360.0) for a in range(200)], "не весь круг"),
    ([(a, 360.0 + (80 if a < 180 else -80)) for a in range(0, 360, 2)] * 2, "не синхронны"),
])
def test_build_mirror_table_refuses_bad_data(samples, text):
    with pytest.raises(ValueError) as e:
        me.build_mirror_table(samples)
    assert text in str(e.value)


def test_mirror_corr_empty_table_is_zero_and_normalize_checks_length():
    assert me.mirror_corr(None, 123) == 0.0
    with pytest.raises(ValueError):
        cam_cfg(groups=[{"id": "g", "type": "mirror", "zones": ["L", "R"], "table": [0] * 10}])
    with pytest.raises(ValueError):
        cam_cfg(groups=[{"id": "g", "type": "mirror", "zones": ["L", "R"], "table": [0] * 35 + [99]}])


def test_table_removes_systematic_bias_in_delta():
    table = [round(_bias(i * 10), 1) for i in range(me.MIRROR_BINS)]
    plain = cam_cfg(groups=[{"id": "sync", "type": "mirror", "zones": ["L", "R"], "tol": 10}])
    fixed = cam_cfg(groups=[{"id": "sync", "type": "mirror", "zones": ["L", "R"], "tol": 10, "table": table}])
    for a in (20, 110, 200, 290):                   # палки «ровные», но камера даёт перекос _bias(a)
        right = (360 - a + _bias(a)) % 360
        d0 = run(me.CameraEngine(plain), a, right)["groups"][0]["delta"]
        g1 = run(me.CameraEngine(fixed), a, right)["groups"][0]
        assert abs(d0 - _bias(a)) <= 2.5, (a, d0)                 # без таблицы перекос виден целиком
        assert abs(g1["delta"]) <= 2.5 and g1["table"] is True, (a, g1["delta"])


def test_worker_collects_samples_and_endpoint_builds_table(tmp_path):
    import importlib.util
    from fastapi import HTTPException
    spec = importlib.util.spec_from_file_location("motion_server_cycle", MS / "server.py")
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    app = srv.create_app(tmp_path / "data")
    cfg = {"cameras": [{"id": "dif", "name": "d", "enabled": False, "url": "x", "frame_size": [W, H],
                        "zones": [{"id": "L", "type": "cycle", "center": [250, 270]}, {"id": "R", "type": "cycle", "center": [700, 270]}],
                        "groups": [{"id": "sync", "type": "mirror", "zones": ["L", "R"]}]}]}
    put = next(r.endpoint for r in app.router.routes if getattr(r, "path", "") == "/config" and "PUT" in r.methods)
    call = next(r.endpoint for r in app.router.routes if getattr(r, "path", "") == "/cameras/{cid}/mirror_table")
    put(cfg)
    with pytest.raises(HTTPException) as e:
        call("dif", {"group": "sync"})
    assert e.value.status_code == 400 and "мало данных" in e.value.detail
    with pytest.raises(HTTPException) as e:
        call("dif", {"group": "nope"})
    assert e.value.status_code == 404
    w = app.state.mgr.workers["dif"]
    for i in range(400):                             # кадры одного оборота: слева a, справа 360 − a + перекос
        a = (i * 0.9) % 360
        w._history.append((float(i), {"L": (a, 0.3, 1), "R": ((360 - a + _bias(a)) % 360, 0.3, 1)}, 1))
    w._history.append((999.0, {"L": (5.0, 0.3, 0), "R": (5.0, 0.3, 1)}, 1))        # палка не найдена — в выборку не идёт
    body = call("dif", {"group": "sync"})
    assert body["n"] == 400 and len(body["table"]) == 36
    assert all(abs(v - _bias(i * 10)) <= 1.5 for i, v in enumerate(body["table"]))


PNG_DIR = Path(r"E:\grib_dif")


@pytest.mark.skipif(not VIDEO.exists() or not list(PNG_DIR.glob("val_1*.png")), reason="видео и кадры диффузии не лежат на этом компьютере")
def test_real_table_from_video_predicts_other_days():
    """Таблица, снятая по видео 08.09, на кадрах 10–14.09 (другие дни) сжимает разброс Δ в разы: перекос — свойство камеры, а не случай."""
    cfg = {"id": "dif", "url": "x", "frame_size": [1920, 1080], "proc_fps": 4,
           "zones": [{"id": "L", "type": "cycle", "center": [318, 291], "radius": 340}, {"id": "R", "type": "cycle", "center": [1015, 316], "radius": 340}],
           "groups": [{"id": "sync", "type": "mirror", "zones": ["L", "R"], "tol": 15}]}
    eng = me.CameraEngine(me.normalize_camera(cfg))
    cap, t, samples = cv2.VideoCapture(str(VIDEO)), 0.0, []
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        st = eng.process(cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY), t, fr)
        t += 0.25
        if st and st["groups"][0]["valid"]:
            z = {x["id"]: x for x in st["zones"]}
            samples.append((z["L"]["angle"], z["L"]["angle"] + z["R"]["angle"]))
    table, _ = me.build_mirror_table(samples, min_bins=20, min_n=100)       # видео — чуть меньше одного оборота, пробелы дозаполняются
    cfg["groups"][0]["table"] = table
    raw, fixed = [], []
    for p in sorted(PNG_DIR.glob("val_1*.png")):
        img = cv2.imread(str(p))
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        for cfg_g, out in ((None, raw), (table, fixed)):
            c = json.loads(json.dumps(cfg))
            c["groups"][0]["table"] = cfg_g
            e = me.CameraEngine(me.normalize_camera(c))
            st = None
            for i in range(3):
                st = e.process(g, i * 0.25, img) or st
            if st["groups"][0]["valid"]:
                out.append(st["groups"][0]["delta"])
    assert len(fixed) >= 15
    # у каждого режима кадры могли отсеяться по-разному (одиночный кадр без слежения) — сравниваем разброс
    assert np.std(fixed) <= 0.6 * np.std(raw), (np.std(raw), np.std(fixed))
    assert max(abs(x) for x in fixed) <= 6.5


# ───────────── обороты ─────────────
def spin(eng, a0, dps, seconds=30.0, fps=4.0, still_after=None):
    """Палки вращаются: левая от a0 со скоростью dps град/с (+ по часовой), правая зеркально. Возвращает последнее состояние."""
    st = None
    for i in range(int(seconds * fps)):
        t = i / fps
        a = a0 + dps * (t if still_after is None else min(t, still_after))
        img = rod_frame({(250, 270): a % 360, (700, 270): (360 - a) % 360})
        st = eng.process(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), t, img) or st
    return st


@pytest.mark.parametrize("dps, a0", [(6.0, 10), (-6.0, 100), (4.5, 340), (-9.0, 30)])
def test_rpm_matches_rotation_speed_and_sign_through_wraparound(dps, a0):
    st = spin(me.CameraEngine(cam_cfg()), a0, dps)
    l, r = zone(st, "L")["rpm"], zone(st, "R")["rpm"]
    assert abs(l - dps / 6.0) <= 0.05 * abs(dps / 6.0) + 0.02, (dps, l)      # левая крутится со знаком dps
    assert abs(r + dps / 6.0) <= 0.05 * abs(dps / 6.0) + 0.02, (dps, r)      # правая — навстречу


def test_rpm_zero_when_standing_and_before_enough_data():
    eng = me.CameraEngine(cam_cfg())
    assert zone(spin(eng, 50, 6.0, seconds=1.5), "L")["rpm"] == 0.0        # данных мало
    st = spin(me.CameraEngine(cam_cfg()), 50, 0.0)
    assert abs(zone(st, "L")["rpm"]) <= 0.02 and zone(st, "L")["valid"]


def test_rpm_drops_to_zero_after_rod_is_lost_and_after_dead_camera():
    eng = me.CameraEngine(cam_cfg())
    spin(eng, 10, 6.0)
    empty = np.full((H, W, 3), (60, 130, 70), np.uint8)
    for i in range(8):
        st = eng.process(cv2.cvtColor(empty, cv2.COLOR_BGR2GRAY), 40 + i * 5.0, empty) or None
    assert st is not None and zone(st, "L")["rpm"] == 0.0 and not zone(st, "L")["valid"]
    assert zone(eng.dead_state(99.0, "нет"), "L")["rpm"] == 0.0


def test_rpm_out_writes_signed_hundredths():
    c = cam_cfg(zones=[{"id": "L", "type": "cycle", "center": [250, 270], "radius": 170, "rpm_out": {"kind": "holding", "address": 20}},
                       {"id": "R", "type": "cycle", "center": [700, 270], "radius": 170, "rpm_out": {"kind": "holding", "address": 21}}],
                 groups=[])
    eng = me.CameraEngine(c)
    st = spin(eng, 10, 6.0)                                      # 1 об/мин: левая +, правая −
    w = {(k, a): v for k, a, v in eng.compute_writes(st)}
    assert 95 <= w[("holding", 20)] <= 105
    assert 65536 - 105 <= w[("holding", 21)] <= 65536 - 95
    with pytest.raises(ValueError):
        cam_cfg(zones=[{"id": "L", "type": "cycle", "center": [1, 2], "rpm_out": {"kind": "coil", "address": 1}}])


@pytest.mark.skipif(not VIDEO.exists(), reason="видео диффузии не лежит на этом компьютере")
def test_real_video_rpm_is_steady_and_opposite():
    eng = me.CameraEngine(me.normalize_camera({
        "id": "dif", "url": "x", "frame_size": [1920, 1080], "proc_fps": 4,
        "zones": [{"id": "L", "type": "cycle", "center": [318, 291], "radius": 340}, {"id": "R", "type": "cycle", "center": [1015, 316], "radius": 340}]}))
    cap, t, ls, rs = cv2.VideoCapture(str(VIDEO)), 0.0, [], []
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        st = eng.process(cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY), t, fr)
        t += 0.25
        if st and t > 25:                                        # первые секунды окно оборотов ещё набирается
            z = {x["id"]: x for x in st["zones"]}
            ls.append(z["L"]["rpm"]); rs.append(z["R"]["rpm"])
    assert 0.65 <= np.median(rs) <= 0.80 and -0.80 <= np.median(ls) <= -0.65      # ≈ 1,1°/кадр × 4 кадра/с ÷ 6 = 0,73 об/мин, левая против часовой
    assert np.percentile(rs, 10) >= 0.55 and np.percentile(ls, 90) <= -0.55         # без сильных провалов


# ───────────── цвет красного конца ─────────────
def test_untouched_old_default_red_range_moves_to_new_but_custom_is_kept():
    base = {"id": "L", "type": "cycle", "center": [250, 270], "radius": 170}
    z_old = me._norm_cycle({**base, "red_hsv": list(me.CYCLE_RED_HSV_OLD)}, "L", "z")
    z_none = me._norm_cycle(dict(base), "L", "z")
    z_own = me._norm_cycle({**base, "red_hsv": [10, 170, 90, 100]}, "L", "z")
    assert z_old["red_hsv"] == z_none["red_hsv"] == list(me.CYCLE_RED_HSV)
    assert z_own["red_hsv"] == [10, 170, 90, 100]


FRAMES_DIR = Path(r"E:\grib_dif")


@pytest.mark.skipif(len(list(FRAMES_DIR.glob("*.png"))) < 40, reason="кадры завода (E:\\grib_dif) не лежат на этом компьютере")
def test_real_plant_frames_day_and_night_both_rods_found_and_mirror_consistent():
    """Реальные кадры одной камеры за несколько суток (день, вечер, ночь): без истории, по одному кадру, обе палки находятся почти всегда,
    а сумма углов зеркальна (разброс — известный «горб» объектива, не больше ~±12°)."""
    frames = sorted(FRAMES_DIR.glob("*.png"))
    both = good = 0
    for f in frames:
        im = cv2.resize(cv2.imdecode(np.fromfile(f, np.uint8), cv2.IMREAD_COLOR), (1280, 720))
        eng = me.CameraEngine(me.normalize_camera({
            "id": "dif", "url": "x", "frame_size": [1920, 1080], "proc_fps": 4,
            "zones": [{"id": "L", "type": "cycle", "center": [318, 291], "radius": 340},
                      {"id": "R", "type": "cycle", "center": [1015, 316], "radius": 340}],
            "groups": [{"id": "sync", "type": "mirror", "zones": ["L", "R"], "tol": 15}]}))
        gray = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
        eng.process(gray, 0.0, im)                       # первый кадр только заполняет буфер
        st = eng.process(gray, 0.25, im)
        g = st["groups"][0]
        if g["valid"]:
            both += 1
            good += abs(g["delta"]) <= 12.0
    assert both >= 0.97 * len(frames), (both, len(frames))
    assert good == both, (good, both)


# ───────────── тревога и удержание ─────────────
def test_mirror_alarm_is_not_changed_while_a_rod_is_held():
    """Пока одной палки не видно (угол достроен по скорости), тревогу ни включаем, ни гасим: достроенный угол врёт на градусы."""
    eng = me.CameraEngine(cam_cfg())
    spin(eng, 10, 6.0, seconds=20)                       # оба крутятся, Δ ≈ 0
    t = 20.0
    only_right = rod_frame({(700, 270): (360 - (10 + 6.0 * 20.0)) % 360})
    gray = cv2.cvtColor(only_right, cv2.COLOR_BGR2GRAY)
    for i in range(40):                                  # левую не видно 10 с, правая идёт «не в такт» (рассинхрон 20°, в пределах окна поиска)
        t = 20.0 + i * 0.25
        img = rod_frame({(700, 270): (360 - (10 + 6.0 * t) + 20) % 360})
        st = eng.process(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), t, img)
        g = st["groups"][0]
        assert zone(st, "L")["coast"] and g["hold"], (t, zone(st, "L"))
        assert g["delta"] is not None and (i < 8 or abs(g["delta"]) > 12)   # Δ показывается (первые 2 с — сглаживание правой)...
        assert g["alarm"] == 0, (t, g)                                  # ...а тревога не включается
    img = rod_frame({(250, 270): (10 + 6.0 * 30.25) % 360, (700, 270): (360 - (10 + 6.0 * 30.25) + 20) % 360})
    for k in range(6):                                   # левая вернулась, рассинхрон настоящий — тревога включается
        st = eng.process(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), 30.25 + k * 0.25, img)
    g = st["groups"][0]
    assert not g["hold"] and g["alarm"] == 1


def test_mirror_alarm_stays_on_through_a_hold_and_clears_after():
    eng = me.CameraEngine(cam_cfg())
    st = run(eng, 40, 280, n=10)                         # θ1 + θ2 = 320 → Δ = −40°: тревога
    assert st["groups"][0]["alarm"] == 1
    for k in range(30):                                  # левая пропала; правая плавно «возвращается в норму» (Δ → 0): без защиты тревога бы погасла
        right = min(320.0, 280.0 + 3.4 * k)
        img = rod_frame({(700, 270): right})
        st = eng.process(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), 3.0 + k * 0.25, img)
        g = st["groups"][0]
        assert g["hold"] and g["alarm"] == 1, (k, g["delta"], g["alarm"])
    assert abs(g["delta"]) < 7                           # Δ уже в норме, а тревога держится
    img = rod_frame({(250, 270): 40, (700, 270): 320})   # левая вернулась: теперь тревога гаснет как обычно
    for k in range(10):
        st = eng.process(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), 11.0 + k * 0.25, img)
    assert not st["groups"][0]["hold"] and st["groups"][0]["alarm"] == 0


# ───────────── крутится / стоит ─────────────
def drive(eng, plan, fps=4.0, hide_left=False):
    """plan [(секунд, град/с левого)] — валы крутятся зеркально; возвращает [(время, состояние)]. Время и угол идут сплошным потоком."""
    out, t, a = [], 0.0, 40.0
    for seconds, dps in plan:
        for _ in range(int(seconds * fps)):
            frames = {(700, 270): (360 - a) % 360}
            if not hide_left:
                frames[(250, 270)] = a % 360
            img = rod_frame(frames)
            st = eng.process(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), t, img)
            if st:
                out.append((t, st))
            t += 1.0 / fps
            a += dps / fps
    return out


def test_moving_and_standing_are_detected_within_about_ten_seconds():
    eng = me.CameraEngine(cam_cfg())
    run = drive(eng, [(20, 4.7), (40, 0.0), (30, 4.7)])
    at = lambda t: zone(next(s for tt, s in run if tt >= t), "L")
    assert at(19)["moving"] is True and at(19)["rpm"] != 0
    assert at(20 + 4)["moving"] is True                  # только что остановились: за 4 с ещё не ясно, остаётся «крутится»
    z = at(20 + 13)
    assert z["moving"] is False and z["rpm"] == 0.0      # за ~10 с стоит, обороты 0 сразу
    assert at(59)["moving"] is False
    assert at(60 + 13)["moving"] is True                 # поехал — через ~10 с снова «крутится»


def test_slow_start_after_a_stop_counts_as_moving():
    eng = me.CameraEngine(cam_cfg())
    run = drive(eng, [(15, 4.7), (20, 0.0), (40, 1.8)])    # 0,3 об/мин
    z = zone(next(s for tt, s in run if tt >= 15 + 20 + 14), "L")
    assert z["moving"] is True


def test_hidden_rod_takes_moving_state_from_its_partner_and_does_not_drift_when_partner_stands():
    eng = me.CameraEngine(cam_cfg())
    drive(eng, [(20, 4.7)])                              # крутятся, левую видно
    # дальше левую не видно, а оба вала остановились: ожидаемый угол левой не должен уезжать по прежней скорости
    out, t = [], 20.0
    a_stop = 40.0 + 4.7 * 20
    for i in range(int(25 * 4)):
        img = rod_frame({(700, 270): (360 - a_stop) % 360})
        st = eng.process(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), t, img)
        out.append((t, st))
        t += 0.25
    z = zone(out[-1][1], "L")
    assert z["coast"] and z["moving"] is False
    assert abs(me.ang_diff(z["angle"], a_stop)) <= 12.0     # без правки ушёл бы на 4,7°/с × 25 с ≈ 117°
    assert zone(out[-1][1], "R")["moving"] is False


def test_zone_state_has_moving_none_until_known_and_none_when_lost():
    eng = me.CameraEngine(cam_cfg())
    st = run(eng, 40, 320)
    assert zone(st, "L")["moving"] is None               # меньше stop_s данных — не знаем
    empty = np.full((H, W, 3), (60, 130, 70), np.uint8)
    for i in range(8):
        st = eng.process(cv2.cvtColor(empty, cv2.COLOR_BGR2GRAY), 10 + i * 5.0, empty) or st
    assert zone(st, "L")["moving"] is None and not zone(st, "L")["valid"]


def test_rpm_out_register_follows_speed_and_is_zero_when_standing():
    c = cam_cfg(zones=[{"id": "L", "type": "cycle", "center": [250, 270], "radius": 170, "rpm_out": {"kind": "holding", "address": 20}},
                       {"id": "R", "type": "cycle", "center": [700, 270], "radius": 170, "rpm_out": {"kind": "holding", "address": 21}}],
                 groups=[])
    eng = me.CameraEngine(c)
    run = drive(eng, [(25, 4.7), (30, 0.0)])
    w_at = lambda t: {(k, a): v for k, a, v in eng.compute_writes(next(s for tt, s in run if tt >= t))}
    w = w_at(24)
    assert 70 <= w[("holding", 20)] <= 85 or 65536 - 85 <= w[("holding", 20)] <= 65536 - 70       # ±0,79 об/мин ×100 (знак по стороне)
    assert 70 <= w[("holding", 21)] <= 85 or 65536 - 85 <= w[("holding", 21)] <= 65536 - 70
    w = w_at(25 + 15)                                    # остановились 15 с назад: «стоит», регистр 0 сразу, а не через окно 20 с
    assert w[("holding", 20)] == 0 and w[("holding", 21)] == 0


# ───────────── самоподстройка поправки (скользящее окно, форма без среднего) ─────────────
class _Rod:
    """Подставная палка для _mirror_group: только то, что читает группа."""
    def __init__(self, angle, moving=True):
        self.angle, self.valid, self.coast, self.moving = angle, True, False, moving


def _drive(eng, minutes, t0=0.0, shift=0.0, rpm=0.79, fps=4.0, noise=0.5, outliers=0.0, moving=True, seed=5):
    """Крутит валы `minutes` минут: слева θ, справа 360 − θ + перекос камеры + сдвиг; возвращает (время, [delta по кадрам])."""
    rng = np.random.default_rng(seed)
    g = eng.cfg["groups"][0]
    out, t, a = [], t0, rpm * 360.0 / 60.0 * t0 % 360
    for _ in range(int(minutes * 60 * fps)):
        t += 1.0 / fps
        a = (a + rpm * 360.0 / 60.0 / fps) % 360
        right = (360 - a + _bias(a) + shift + rng.normal(0, noise)) % 360
        if outliers and rng.random() < outliers:
            right = rng.uniform(0, 360)
        eng.cycles = {"L": _Rod(a, moving), "R": _Rod(right, moving)}
        out.append(eng._mirror_group(g, True, t)["delta"])
    return t, out


def _eng(**g):
    grp = {"id": "sync", "type": "mirror", "zones": ["L", "R"], "tol": 5}
    grp.update(g)
    return me.CameraEngine(cam_cfg(groups=[grp]))


def test_learn_defaults_and_bounds():
    g = cam_cfg()["groups"][0]
    assert g["learn"] is True and g["learn_min"] == 30
    assert "learn" not in me.CameraEngine(cam_cfg(groups=[{"id": "g", "type": "mirror", "zones": ["L", "R"], "learn": False}])).learners
    with pytest.raises(ValueError):
        cam_cfg(groups=[{"id": "g", "type": "mirror", "zones": ["L", "R"], "learn_min": 2}])


def test_learning_flattens_the_wave_but_keeps_a_real_shift_visible():
    eng = _eng()
    _, d = _drive(eng, 4)                           # первые минуты: форма ещё не выучена, волна ±8° видна
    assert np.max(np.abs(d)) > 6
    t, d = _drive(eng, 8, t0=240)                   # дальше: волна выучена, Δ около нуля
    late = np.abs(d[-4 * 60 * 4:])
    assert np.percentile(late, 95) <= 2.0, np.percentile(late, 95)
    st = eng._mirror_group(eng.cfg["groups"][0], True, t)
    assert st["learn"]["ready"] and st["learn"]["bins"] >= 30 and 10 < st["learn"]["amp"] < 20 and len(st["shape"]) == 36
    t, d = _drive(eng, 3, t0=t, shift=7.0)           # настоящий сдвиг валов на 7°: виден сразу и не «принимается за норму»
    assert np.median(d[-4 * 60:]) > 5.0
    t, d = _drive(eng, 40, t0=t, shift=7.0)          # даже через полное окно сдвиг остаётся в Δ
    assert 5.5 < np.median(d[-4 * 60:]) < 8.5, np.median(d[-4 * 60:])
    assert np.percentile(np.abs(np.array(d[-4 * 60:]) - 7.0), 95) <= 2.5     # а волна по-прежнему выучена


def test_learning_ignores_wrong_catches_and_stopped_shafts():
    eng = _eng()
    t, d = _drive(eng, 12, outliers=0.05)           # 5 % ошибочных захватов палки
    assert np.percentile(np.abs(d[-4 * 60 * 3:]), 90) <= 2.5
    shape = list(eng.learners["sync"].shape)
    _drive(eng, 15, t0=t, moving=False, shift=5.0)   # вал стоит (углы замерли): ничего не учится
    assert eng.learners["sync"].shape == pytest.approx(shape, abs=0.5)       # новых кадров нет — форма стоит (дрейф лишь от дозревания окна)


def test_learned_shape_survives_restart_and_is_dropped_with_another_table(tmp_path):
    path = tmp_path / "mirror_learn_dif.json"
    cfg = cam_cfg(groups=[{"id": "sync", "type": "mirror", "zones": ["L", "R"]}])
    eng = me.CameraEngine(cfg, path)
    t, _ = _drive(eng, 8)
    eng._learn_save(t + 100, force=True)
    assert path.exists()
    again = me.CameraEngine(cfg, path)
    assert again.learners["sync"].shape == pytest.approx(eng.learners["sync"].shape, abs=0.01) and not again.learners["sync"].fresh
    other = cam_cfg(groups=[{"id": "sync", "type": "mirror", "zones": ["L", "R"], "table": [1.0] * 36}])
    assert me.CameraEngine(other, path).learners["sync"].fresh                  # таблицу сменили — старая форма не подходит
    path.write_text("мусор", encoding="utf-8")
    assert me.CameraEngine(cfg, path).learners["sync"].fresh                    # битый файл не роняет сервис


def test_static_decoy_is_banned_while_partner_spins_and_real_rod_is_found():
    """Неподвижная «палка» (рамка, лист) на 198°: пока парная крутится, левая не должна на ней залипать — запрещаем этот угол и ищем настоящую."""
    eng = me.CameraEngine(cam_cfg())
    fps, t, a = 4.0, 0.0, 40.0
    st = None
    for seconds, real_visible in ((45, False), (45, True)):
        for _ in range(int(seconds * fps)):
            frames = {(700, 270): (360 - a) % 360}
            if real_visible:
                frames[(250, 270)] = a % 360
            img = rod_frame(frames)
            r = math.radians(198)
            dx, dy = math.sin(r), -math.cos(r)
            cv2.line(img, (250, 270), (int(250 + 150 * dx), int(270 + 150 * dy)), RED, 3)
            cv2.line(img, (250, 270), (int(250 - 150 * dx), int(270 - 150 * dy)), BLUE, 3)
            st = eng.process(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), t, img) or st
            t += 1.0 / fps
            a += 4.7 / fps
    left, right = zone(st, "L"), zone(st, "R")
    assert left["moving"] is True
    d = (left["angle"] + right["angle"] - 360 + 180) % 360 - 180
    assert abs(d) < 8
