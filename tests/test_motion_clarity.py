"""Зона-прозрачность (круг на стакане с соком): мутность 0..100 между эталонами «чистый» и «грязный», калибровка, запись в ПЛК.

Кадры синтетические: тёмный фон и закрашенные круги цветом сока (BGR). Цвет «чистого» тёмный, «грязного» светлый жёлто-бурый,
как на фото стаканов у заводчан.
"""
import importlib.util
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

MS = Path(__file__).resolve().parent.parent / "motion_service"
sys.path.insert(0, str(MS))

import motion_engine as me  # noqa: E402

W, H = 640, 360
CLEAN = (28, 32, 36)            # BGR: тёмный прозрачный стакан — виден тёмный поддон
DIRTY = (70, 120, 160)          # BGR: светлый жёлто-бурый мутный сок
CENTERS = [(80 + 80 * i, 180) for i in range(7)]


def blend(t: float, a=CLEAN, b=DIRTY):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def frame(colors, size=(W, H), r=26, bg=(20, 22, 24), noise=0, seed=1):
    """{центр: цвет BGR} → кадр. noise — шум матрицы (± градаций)."""
    img = np.full((size[1], size[0], 3), bg, np.uint8)
    for (cx, cy), col in colors.items():
        cv2.circle(img, (cx, cy), r, col, -1)
    if noise:
        rng = np.random.default_rng(seed)
        img = np.clip(img.astype(np.int16) + rng.integers(-noise, noise + 1, img.shape), 0, 255).astype(np.uint8)
    return img


def zones(**over):
    return [dict({"id": "g%d" % (i + 1), "name": "Стакан %d" % (i + 1), "type": "clarity", "center": list(c), "radius": 26}, **over)
            for i, c in enumerate(CENTERS)]


def cam_cfg(zs=None, **over):
    c = {"id": "juice", "url": "x", "frame_size": [W, H], "proc_fps": 4, "zones": zs if zs is not None else zones()}
    c.update(over)
    return me.normalize_camera(c)


_CLOCK: dict = {}


def run(eng, img, n=8):
    """n одинаковых кадров подряд (первый только заполняет буфер) → состояние последнего. Время идёт вперёд на 0,25 с за кадр у каждого движка."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    st = None
    for _ in range(n):
        _CLOCK[id(eng)] = _CLOCK.get(id(eng), 0.0) + 0.25
        st = eng.process(gray, _CLOCK[id(eng)], img) or st
    return st


def zone(st, zid):
    return next(z for z in st["zones"] if z["id"] == zid)


def calibrated_cfg(zs=None, **over):
    """Снять «чистый» и «грязный» по кадрам тем же способом, каким это делает интерфейс (clarity_sample), и записать в конфиг."""
    zs = zs if zs is not None else zones()
    eng = me.CameraEngine(cam_cfg(zs, **over))
    run(eng, frame({c: CLEAN for c in CENTERS}))
    clean = eng.clarity_sample([], 30)
    run(eng, frame({c: DIRTY for c in CENTERS}), n=8)
    dirty = eng.clarity_sample(["g1"], 1.0)
    for z in zs:
        z["clean"] = clean[z["id"]]["lab"]
        z["dirty"] = dirty["g1"]["lab"] if z.get("dirty_mode") == "own" else None
    return zs, dirty["g1"]["lab"], clean["g1"]["lab"]


# ───────────── конфиг ─────────────
def test_normalize_clarity_defaults():
    z = cam_cfg()["zones"][0]
    assert z["type"] == "clarity" and z["points"] == [] and z["center"] == [80.0, 180.0] and z["radius"] == 26.0
    assert z["core"] == 0.7 and z["glare_pct"] == 10 and z["smooth"] == 15 and z["dirty_mode"] == "shared"
    assert z["clean"] is None and z["dirty"] is None and z["out"] is None


@pytest.mark.parametrize("bad, text", [
    ({"center": None}, "центр"),
    ({"radius": 1}, ".radius"),
    ({"clean": [1, 2]}, "три числа"),
    ({"clean": [150, 0, 0]}, ".clean"),
    ({"dirty_mode": "x"}, "dirty_mode"),
    ({"out": {"kind": "coil", "address": 1}}, "вид вывода"),
])
def test_normalize_clarity_errors(bad, text):
    z = dict(zones()[0], **bad)
    with pytest.raises(ValueError) as e:
        cam_cfg([z])
    assert text in str(e.value)


def test_camera_shift_is_normalized():
    assert cam_cfg(clarity_shift=[30, 5, 40])["clarity_shift"] == [30.0, 5.0, 40.0]
    assert cam_cfg()["clarity_shift"] is None
    with pytest.raises(ValueError):
        cam_cfg(clarity_shift=[1, 2])


# ───────────── шкала 0..100 ─────────────
def test_not_calibrated_gives_no_value_and_65535():
    eng = me.CameraEngine(cam_cfg())
    st = run(eng, frame({c: CLEAN for c in CENTERS}))
    z = zone(st, "g1")
    assert z["valid"] is False and z["calibrated"] is False and "чистый" in z["reason"]
    assert z["lab"] is not None and z["bit"] == 0                 # цвет виден (для калибровки), мутности нет


def test_scale_clean_dirty_and_middle():
    zs2, dirty_lab, clean_lab = calibrated_cfg(zones(dirty_mode="own"))
    eng = me.CameraEngine(cam_cfg(zs2))
    cols = {CENTERS[0]: CLEAN, CENTERS[1]: DIRTY, CENTERS[2]: blend(0.5)}
    st = run(eng, frame(cols))
    assert zone(st, "g1")["turb"] <= 3 and zone(st, "g1")["valid"]
    assert zone(st, "g2")["turb"] >= 97
    assert 35 <= zone(st, "g3")["turb"] <= 65, zone(st, "g3")["turb"]      # середина цвета — середина шкалы (Lab нелинеен по BGR, допуск широкий)


def test_value_is_monotonic_with_dirt():
    zs, _, _ = calibrated_cfg(zones(dirty_mode="own"))
    eng = me.CameraEngine(cam_cfg(zs))
    vals = []
    for t in (0, 0.2, 0.4, 0.6, 0.8, 1.0):
        st = run(eng, frame({CENTERS[0]: blend(t)}), n=40)               # 40 кадров — сглаживание (15) успевает перейти на новый цвет
        vals.append(zone(st, "g1")["turb"])
    assert vals == sorted(vals) and vals[0] <= 3 and vals[-1] >= 97, vals


def test_clamps_beyond_references():
    zs, _, _ = calibrated_cfg(zones(dirty_mode="own", off_max=0))
    eng = me.CameraEngine(cam_cfg(zs))
    st = run(eng, frame({CENTERS[0]: (10, 10, 12), CENTERS[1]: (130, 190, 230)}), n=40)
    assert zone(st, "g1")["turb"] == 0.0 and zone(st, "g2")["turb"] == 100.0
    assert zone(st, "g2")["raw"] > 100.0


def test_light_change_moves_value_moderately():
    """Общая яркость ±20 % сдвигает мутность меньше чем на 25 пунктов (≈ 1 пункт на 1 % яркости; эталоны сняты при обычном свете). Убрать нельзя: яркость — главный
    признак мутности, поэтому свет и экспозиция камеры должны быть постоянными, а при их смене эталоны снимают заново."""
    zs, _, _ = calibrated_cfg(zones(dirty_mode="own"))
    base = {CENTERS[0]: CLEAN, CENTERS[1]: blend(0.5), CENTERS[2]: DIRTY}
    ref = run(me.CameraEngine(cam_cfg(zs)), frame(base), n=40)
    for k in (0.8, 1.2):
        cols = {c: tuple(min(255, int(round(v * k))) for v in col) for c, col in base.items()}
        st = run(me.CameraEngine(cam_cfg(zs)), frame(cols), n=40)
        for zid in ("g1", "g2", "g3"):
            assert abs(zone(st, zid)["turb"] - zone(ref, zid)["turb"]) < 25, (k, zid, zone(st, zid)["turb"], zone(ref, zid)["turb"])


def test_noise_and_glare_do_not_break_value():
    zs, _, _ = calibrated_cfg(zones(dirty_mode="own"))
    img = frame({CENTERS[0]: blend(0.5)}, noise=6)
    cv2.circle(img, (CENTERS[0][0] + 6, CENTERS[0][1] - 5), 3, (255, 255, 255), -1)         # блик в круге
    clean_img = frame({CENTERS[0]: blend(0.5)}, noise=6)
    a = zone(run(me.CameraEngine(cam_cfg(zs)), img, n=40), "g1")["turb"]
    b = zone(run(me.CameraEngine(cam_cfg(zs)), clean_img, n=40), "g1")["turb"]
    assert abs(a - b) < 6, (a, b)


def test_color_off_the_scale_is_not_trusted_and_value_is_held():
    zs, _, _ = calibrated_cfg(zones(dirty_mode="own"))
    eng = me.CameraEngine(cam_cfg(zs))
    st = run(eng, frame({CENTERS[0]: blend(0.5)}), n=40)
    good = zone(st, "g1")["turb"]
    assert zone(st, "g1")["valid"]
    st = run(eng, frame({CENTERS[0]: (40, 200, 40)}), n=40)                  # ярко-зелёное: не «мутнее», а просто другое
    z = zone(st, "g1")
    assert z["valid"] is False and z["bit"] == 0 and "не на шкале" in z["reason"]
    assert z["turb"] == good                                                  # держит последнее хорошее значение


def test_circle_outside_frame_and_gray_frame_are_not_valid():
    zs = zones()[:1]
    zs[0]["center"] = [5000, 5000]
    st = run(me.CameraEngine(cam_cfg(zs)), frame({}))
    assert zone(st, "g1")["valid"] is False and "вне кадра" in zone(st, "g1")["reason"]
    eng = me.CameraEngine(cam_cfg())
    gray = np.full((H, W), 40, np.uint8)
    st = None
    for i in range(4):
        st = eng.process(gray, i * 0.25, None) or st
    assert zone(st, "g1")["valid"] is False and "цвета" in zone(st, "g1")["reason"]


def test_frame_size_mismatch_rescales_circle():
    """Размечали на 640×360, а камера отдаёт вдвое больше: круг должен остаться на том же стакане."""
    zs, _, _ = calibrated_cfg(zones(dirty_mode="own"))
    eng = me.CameraEngine(cam_cfg(zs))
    big = {(c[0] * 2, c[1] * 2): (DIRTY if i == 1 else CLEAN) for i, c in enumerate(CENTERS)}
    st = run(eng, frame(big, size=(W * 2, H * 2), r=52), n=40)
    assert zone(st, "g1")["turb"] <= 3 and zone(st, "g2")["turb"] >= 97


# ───────────── общий «грязный» ─────────────
def test_shared_dirty_shift_works_for_zones_with_different_light():
    """«Грязный» — один общий сдвиг цвета, «чистый» у каждого стакана свой (свет разный): у тёмного и светлого стакана шкала одна."""
    zs, dirty_lab, clean_lab = calibrated_cfg()
    shift = [round(d - c, 2) for d, c in zip(dirty_lab, clean_lab)]
    # второй стакан светлее: его «чистый» — другой цвет, сдвиг к «грязному» прежний (в Lab)
    lighter = (50, 56, 62)
    eng0 = me.CameraEngine(cam_cfg(zs, clarity_shift=shift))
    run(eng0, frame({CENTERS[1]: lighter}), n=8)
    zs[1]["clean"] = eng0.clarity_sample(["g2"], 30)["g2"]["lab"]
    eng = me.CameraEngine(cam_cfg(zs, clarity_shift=shift))
    st = run(eng, frame({CENTERS[0]: CLEAN, CENTERS[1]: lighter}), n=40)
    assert zone(st, "g1")["turb"] <= 3 and zone(st, "g2")["turb"] <= 3
    assert zone(st, "g1")["valid"] and zone(st, "g2")["valid"]
    st = run(eng, frame({CENTERS[0]: DIRTY}), n=40)
    assert zone(st, "g1")["turb"] >= 95


def test_shared_without_shift_is_not_calibrated():
    zs, _, _ = calibrated_cfg()
    st = run(me.CameraEngine(cam_cfg(zs)), frame({CENTERS[0]: CLEAN}))
    z = zone(st, "g1")
    assert z["valid"] is False and z["calibrated"] is False and "грязный" in z["reason"]


def test_references_too_close_are_rejected():
    zs = zones(dirty_mode="own")
    zs[0]["clean"], zs[0]["dirty"] = [20, 0, 0], [21, 0, 0]
    st = run(me.CameraEngine(cam_cfg(zs)), frame({CENTERS[0]: CLEAN}))
    assert zone(st, "g1")["valid"] is False and "совпадают" in zone(st, "g1")["reason"]


# ───────────── ПЛК ─────────────
def test_writes_value_or_65535():
    zs, _, _ = calibrated_cfg(zones(dirty_mode="own"))
    for i, z in enumerate(zs):
        z["out"] = {"kind": "holding", "address": 100 + i}
    cfg = cam_cfg(zs)
    eng = me.CameraEngine(cfg)
    st = run(eng, frame({CENTERS[0]: CLEAN, CENTERS[1]: DIRTY, CENTERS[2]: blend(0.5)}), n=40)
    w = {a: v for k, a, v in eng.compute_writes(st) if k == "holding"}
    assert w[100] == 0 and w[101] == 100 and 35 <= w[102] <= 65
    dead = eng.dead_state(99.0, "нет кадров")
    assert all(v == me.CLARITY_NO_DATA for k, a, v in eng.compute_writes(dead) if 100 <= a < 107)
    plain = me.CameraEngine(cam_cfg(zones()))                                 # без эталонов
    st = run(plain, frame({c: CLEAN for c in CENTERS}))
    assert plain.compute_writes(st) == []                                     # вывода не задано — в ПЛК ничего


def test_no_out_by_default_writes_nothing():
    zs, _, _ = calibrated_cfg(zones(dirty_mode="own"))
    eng = me.CameraEngine(cam_cfg(zs))
    st = run(eng, frame({CENTERS[0]: CLEAN}))
    assert eng.compute_writes(st) == []


# ───────────── калибровка ─────────────
def test_sample_median_and_spread():
    eng = me.CameraEngine(cam_cfg())
    run(eng, frame({c: CLEAN for c in CENTERS}, noise=5), n=30)
    r = eng.clarity_sample(["g1"], 30)["g1"]
    assert r["n"] >= 25 and len(r["lab"]) == 3 and r["spread"] < 6
    assert list(eng.clarity_sample(["g2", "g3"], 30)) == ["g2", "g3"]
    assert len(eng.clarity_sample([], 30)) == 7


def test_sample_without_frames_is_an_error_text():
    eng = me.CameraEngine(cam_cfg())
    r = eng.clarity_sample([], 30)
    assert r == {}                                                            # круги строятся на первом кадре: до него эталонов брать не с чего
    run(eng, frame({c: CLEAN for c in CENTERS}), n=2)
    assert "error" in eng.clarity_sample(["g1"], 30)["g1"]                    # один кадр — мало


def test_endpoint_returns_samples_and_404(tmp_path):
    from fastapi import HTTPException
    spec = importlib.util.spec_from_file_location("motion_server_clarity", MS / "server.py")
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    app = srv.create_app(tmp_path / "data")
    cfg = {"cameras": [{"id": "juice", "name": "j", "enabled": False, "url": "x", "frame_size": [W, H], "zones": zones()}]}
    put = next(r.endpoint for r in app.router.routes if getattr(r, "path", "") == "/config" and "PUT" in r.methods)
    call = next(r.endpoint for r in app.router.routes if getattr(r, "path", "") == "/cameras/{cid}/clarity_sample")
    put(cfg)
    with pytest.raises(HTTPException) as e:
        call("nope", {})
    assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:                                   # камера выключена и кадров не было — кругов ещё нет
        call("juice", {"seconds": 30})
    assert e.value.status_code == 404
    w = app.state.mgr.workers["juice"]
    run(w.engine, frame({c: CLEAN for c in CENTERS}), n=10)
    body = call("juice", {"zones": ["g1", "g2"], "seconds": 30})
    assert sorted(body["zones"]) == ["g1", "g2"] and len(body["zones"]["g1"]["lab"]) == 3


def test_history_keeps_last_value_not_maximum():
    """Мутность в истории — самое позднее хорошее значение секунды, а не максимум (чистый стакан после мутного не должен «залипать» на 100)."""
    spec = importlib.util.spec_from_file_location("motion_camera_clarity", MS / "motion_camera.py")
    mc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mc)
    rows = [(float(i), {"g1": (90.0 if i < 5 else 5.0, 40.0, 1)}, 1) for i in range(10)]
    out = mc._squeeze(rows, 2, frozenset({"g1"}))
    assert out[-1][1]["g1"][0] == 5.0


# ───────────── три состояния: прозрачный / мутный / грязный ─────────────
def level_of(eng, t, n=30):
    st = run(eng, frame({c: blend(t) for c in CENTERS}), n=n)
    z = zone(st, "g1")
    return z["level"], z["level_name"], z["turb"]


def test_level_defaults_and_validation():
    z = cam_cfg()["zones"][0]
    assert (z["t_turb"], z["t_dirty"], z["hyst"]) == (12.0, 70.0, 3.0)
    with pytest.raises(ValueError) as e:
        cam_cfg([dict(zones()[0], t_turb=60, t_dirty=40)])
    assert "порог" in str(e.value)


def test_level_three_states():
    zs, _, _ = calibrated_cfg(zones(dirty_mode="own"))
    for t, want in ((0.0, "clear"), (0.05, "clear"), (0.3, "turbid"), (0.5, "turbid"), (0.9, "dirty"), (1.0, "dirty")):
        eng = me.CameraEngine(cam_cfg(zs))                      # новый движок на каждое значение — без гистерезиса от прошлого
        lv, name, turb = level_of(eng, t)
        assert name == want, (t, turb, name)
        assert lv == me.CLARITY_LEVELS.index(want)


def test_level_hysteresis_holds_state_near_threshold():
    zs, _, _ = calibrated_cfg(zones(dirty_mode="own", t_turb=20, t_dirty=70, hyst=5))
    eng = me.CameraEngine(cam_cfg(zs))
    assert level_of(eng, 0.30)[1] == "turbid"                   # выше порога 20 — мутный
    t_in = 0.17                                                 # чуть ниже порога (17 < 20), но выше 20 − 5 = 15: остаётся мутным
    lv, name, turb = level_of(eng, t_in)
    assert 15 < turb < 20 and name == "turbid"
    assert level_of(eng, 0.05)[1] == "clear"                    # ушли ниже 15 — снова прозрачный
    assert level_of(eng, t_in)[1] == "clear"                    # в «мёртвой зоне» снизу вверх состояние не меняется, пока порог не пройден


def test_level_not_valid_when_uncalibrated():
    eng = me.CameraEngine(cam_cfg())
    z = zone(run(eng, frame({c: CLEAN for c in CENTERS})), "g1")
    assert z["valid"] is False and z["level_name"] == "clear"
