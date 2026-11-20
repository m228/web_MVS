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
    return [dict({"id": "g%d" % (i + 1), "name": "Стакан %d" % (i + 1), "type": "clarity", "center": list(c), "radius": 26, "smooth_s": 0, "hold_s": 0}, **over)   # тесты шкалы — без сглаживания по времени
            for i, c in enumerate(CENTERS)]


def cam_cfg(zs=None, **over):
    c = {"id": "juice", "url": "x", "frame_size": [W, H], "proc_fps": 4, "zones": zs if zs is not None else zones(), "clarity_refs": "own"}   # тесты шкалы — на своих эталонах
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


def test_clarity_step_records_perf():
    from perf_stats import PERF
    PERF.drop("juice:")
    zs, _, _ = calibrated_cfg()
    run(me.CameraEngine(cam_cfg(zs)), frame({CENTERS[0]: CLEAN}), n=3)
    s = PERF.snapshot()["juice:juice"]
    assert s["of"] == len(CENTERS) and s["n"] >= 1


# ───────────── общий «чистый» камеры: не снимать каждый стакан ─────────────
def test_camera_clean_is_normalized():
    assert cam_cfg(clarity_clean=[19, -5, 0])["clarity_clean"] == [19.0, -5.0, 0.0]
    assert cam_cfg()["clarity_clean"] is None
    with pytest.raises(ValueError):
        cam_cfg(clarity_clean=[1, 2])


def test_zone_without_own_clean_uses_camera_clean_and_shift():
    zs, dirty_lab, clean_lab = calibrated_cfg()
    shift = [round(d - c, 2) for d, c in zip(dirty_lab, clean_lab)]
    for z in zs:
        z["clean"] = None                                   # ни у одного стакана своего «чистого»
    eng = me.CameraEngine(cam_cfg(zs, clarity_clean=clean_lab, clarity_shift=shift))
    st = run(eng, frame({CENTERS[0]: CLEAN, CENTERS[1]: blend(0.5), CENTERS[2]: DIRTY}))
    a, b, c = zone(st, "g1"), zone(st, "g2"), zone(st, "g3")
    assert a["valid"] and a["ref_default"] and a["turb"] < 5 and a["level"] == 0
    assert b["valid"] and 30 < b["turb"] < 70 and b["level"] == 1
    assert c["valid"] and c["turb"] > 90 and c["level"] == 2


def test_own_clean_wins_over_camera_clean():
    zs, _, clean_lab = calibrated_cfg(zones(dirty_mode="own"))
    st = run(me.CameraEngine(cam_cfg(zs, clarity_clean=[90, 0, 0])), frame({CENTERS[0]: CLEAN}))
    z = zone(st, "g1")
    assert z["ref_default"] is False and z["calibrated"] is True


def test_no_camera_clean_falls_back_to_median_of_own_cleans():
    zs, dirty_lab, clean_lab = calibrated_cfg()
    shift = [round(d - c, 2) for d, c in zip(dirty_lab, clean_lab)]
    for z in zs[3:]:
        z["clean"] = None                                   # «чистый» снят у первых трёх, у остальных нет, общего камеры тоже нет
    st = run(me.CameraEngine(cam_cfg(zs, clarity_shift=shift)), frame({CENTERS[5]: DIRTY, CENTERS[6]: CLEAN}))
    d, c = zone(st, "g6"), zone(st, "g7")
    assert d["valid"] and d["ref_default"] and d["level"] == 2
    assert c["valid"] and c["level"] == 0


# ───────────── режим «один эталон на все стаканы» ─────────────
def test_refs_mode_default_is_shared_and_validated():
    base = {"id": "juice", "url": "x", "frame_size": [W, H], "zones": zones()}
    assert me.normalize_camera(dict(base))["clarity_refs"] == "shared"
    assert me.normalize_camera(dict(base, clarity_refs="own"))["clarity_refs"] == "own"
    with pytest.raises(ValueError):
        me.normalize_camera(dict(base, clarity_refs="x"))


def test_shared_refs_ignore_own_cleans_and_use_one_scale():
    zs, dirty_lab, clean_lab = calibrated_cfg()
    for i, z in enumerate(zs):
        z["clean"] = [clean_lab[0] + 6 * i, clean_lab[1], clean_lab[2]]   # у каждого стакана «свой» чистый — в общем режиме не нужен
    shift = [round(d - c, 2) for d, c in zip(dirty_lab, clean_lab)]
    eng = me.CameraEngine(cam_cfg(zs, clarity_refs="shared", clarity_clean=clean_lab, clarity_shift=shift))
    st = run(eng, frame({CENTERS[0]: CLEAN, CENTERS[1]: CLEAN, CENTERS[2]: DIRTY}))
    a, b, c = zone(st, "g1"), zone(st, "g2"), zone(st, "g3")
    assert a["valid"] and b["valid"] and abs(a["turb"] - b["turb"]) < 2 and a["turb"] < 5
    assert c["valid"] and c["turb"] > 90 and c["level"] == 2


def test_shared_refs_without_camera_clean_use_median_of_own():
    zs, dirty_lab, clean_lab = calibrated_cfg()
    zs[3:] and [z.update(clean=None) for z in zs[3:]]
    shift = [round(d - c, 2) for d, c in zip(dirty_lab, clean_lab)]
    st = run(me.CameraEngine(cam_cfg(zs, clarity_refs="shared", clarity_shift=shift)), frame({CENTERS[5]: DIRTY, CENTERS[6]: CLEAN}))
    assert zone(st, "g6")["level"] == 2 and zone(st, "g7")["level"] == 0 and zone(st, "g7")["valid"]


# ───────────── сглаживание по времени и выдержка смены состояния ─────────────
def _feed(eng, seq, dt=0.25):
    """seq — [(цвет BGR или None→чистый, число кадров)] → список (мутность, уровень) после каждого кадра у g1."""
    out, now = [], 0.0
    for col, n in seq:
        img = frame({CENTERS[0]: col})
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        for _ in range(n):
            now += dt
            st = eng.process(gray, now, img)
            if st is None:                                          # первый кадр только заполняет буфер
                continue
            z = zone(st, "g1")
            out.append((z["turb"], z["level"], z["valid"]))
    return out


def _smooth_cfg(**over):
    zs, dirty_lab, clean_lab = calibrated_cfg(zones(dirty_mode="own"))
    for z in zs:
        z.update(over)
    return cam_cfg(zs)


def test_smoothing_defaults():
    z = cam_cfg([{"id": "g1", "type": "clarity", "center": [80, 180], "radius": 26}])["zones"][0]
    assert z["smooth_s"] == 30.0 and z["hold_s"] == 20.0


def test_median_window_ignores_short_spike():
    eng = me.CameraEngine(_smooth_cfg(smooth_s=20, hold_s=0))
    res = _feed(eng, [(CLEAN, 60), (DIRTY, 6), (CLEAN, 60)])      # 1,5 секунды «грязного» посреди чистого
    assert max(t for t, _, ok in res if ok) < 10                    # выброс до значения не дошёл
    eng2 = me.CameraEngine(_smooth_cfg(smooth_s=0, hold_s=0))      # без сглаживания по времени — доходит
    assert max(t for t, _, ok in _feed(eng2, [(CLEAN, 60), (DIRTY, 6), (CLEAN, 60)]) if ok) > 20


def test_hold_delays_state_change_and_ignores_blips():
    eng = me.CameraEngine(_smooth_cfg(smooth_s=0, hold_s=10))
    res = _feed(eng, [(CLEAN, 40), (DIRTY, 24), (CLEAN, 40)])     # 6 секунд грязного: короче выдержки 10 с
    assert all(lv == 0 for _, lv, ok in res if ok)
    eng2 = me.CameraEngine(_smooth_cfg(smooth_s=0, hold_s=10))
    res2 = _feed(eng2, [(CLEAN, 40), (DIRTY, 80)])                 # 20 секунд грязного — состояние сменилось, но не сразу
    lv = [l for _, l, ok in res2 if ok]
    assert lv[40] == 0 and lv[-1] == 2


def test_long_burst_is_waited_out_then_real_change_accelerates():
    """Пар/выброс 8 с — почти не двигает значение; настоящее изменение — сначала медленно, потом разгоняется и догоняет за ~полминуты."""
    burst = me.CameraEngine(_smooth_cfg(smooth_s=30, hold_s=0))
    res = _feed(burst, [(CLEAN, 120), (DIRTY, 32), (CLEAN, 120)])          # 8 секунд «грязного»
    assert max(t for t, _, ok in res if ok) < 30
    real = me.CameraEngine(_smooth_cfg(smooth_s=30, hold_s=0))
    res = [t for t, _, ok in _feed(real, [(CLEAN, 480), (DIRTY, 480)]) if ok]   # 120 с чистого, затем 120 с грязного (4 кадра/с)
    after = lambda sec: res[480 - 1 + sec * 4]                             # мутность через sec секунд после ступени (первый кадр буфер — минус 1)
    assert after(5) < 35 and after(15) < after(30) < after(60)             # сначала медленно, потом ускоряется
    assert after(60) > 85                                                  # и догоняет


def test_smoothing_off_is_instant_like_before():
    eng = me.CameraEngine(_smooth_cfg(smooth_s=0, hold_s=0))
    res = [t for t, _, ok in _feed(eng, [(CLEAN, 20), (DIRTY, 24)]) if ok]
    assert res[-1] > 90


def test_first_state_after_start_is_immediate():
    """После запуска первое достоверное состояние берётся сразу: грязный стакан не «прозрачный» все hold_s секунд."""
    eng = me.CameraEngine(_smooth_cfg(smooth_s=0, hold_s=20))
    res = _feed(eng, [(DIRTY, 6)])
    ok = [lv for _, lv, v in res if v]
    assert ok and ok[0] == 2 and ok[-1] == 2
