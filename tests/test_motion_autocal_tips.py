"""Автоподгонка по светоотражателям (motion_autocal_tips + RodTuner.stage_tips): ось по прямым «кончик—кончик», цвет, длина и кольцо кончика.

Синтетика как в test_motion_tips: бирюзовый фон, палка из красной и синей половин, на концах — полоска из ЖЁЛТОЙ и БЕЛОЙ (пересвет) частей,
как на val_Down 24.11. Посев — только жёлтый ящик: подгонка должна добавить белый и найти ось, сбитую на 28 px.
"""
import math
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

MS = Path(__file__).resolve().parent.parent / "motion_service"
sys.path.insert(0, str(MS))

import motion_autocal as ac  # noqa: E402
import motion_autocal_tips as at  # noqa: E402
import motion_engine as me  # noqa: E402

W, H = 800, 600
CX, CY, R = 400, 300, 170
YELLOW_BOX = [18, 38, 85, 255, 170, 255]


def hsv_bgr(h, s, v):
    return tuple(int(x) for x in cv2.cvtColor(np.uint8([[[h, s, v]]]), cv2.COLOR_HSV2BGR)[0, 0])


BG, BLUE, RED, YEL = hsv_bgr(88, 150, 150), hsv_bgr(100, 210, 210), hsv_bgr(176, 220, 210), hsv_bgr(28, 200, 250)


def frame(angle, seed=0, static=False, strips=True):
    rng = np.random.default_rng(seed)
    img = np.full((H, W, 3), BG, np.uint8)
    img = np.clip(img.astype(np.int16) + rng.integers(-10, 10, img.shape), 0, 255).astype(np.uint8)
    if static:                                                           # неподвижный белый блик в кольце кончиков (люк, рамка)
        cv2.line(img, (int(CX + 160 * math.sin(math.radians(200))), int(CY - 160 * math.cos(math.radians(200)))),
                 (int(CX + 185 * math.sin(math.radians(200))) + 8, int(CY - 185 * math.cos(math.radians(200)))), (255, 255, 255), 6)
    r = math.radians(angle)
    dx, dy = math.sin(r), -math.cos(r)
    cv2.line(img, (CX, CY), (int(CX + 148 * dx), int(CY + 148 * dy)), RED, 4)
    cv2.line(img, (CX, CY), (int(CX - 148 * dx), int(CY - 148 * dy)), BLUE, 4)
    if strips:                                                           # жёлтая часть 150..172, белая 172..192; ширина 6 px
        for sg in (1, -1):
            cv2.line(img, (int(CX + sg * 150 * dx), int(CY + sg * 150 * dy)), (int(CX + sg * 172 * dx), int(CY + sg * 172 * dy)), YEL, 6)
            cv2.line(img, (int(CX + sg * 172 * dx), int(CY + sg * 172 * dy)), (int(CX + sg * 192 * dx), int(CY + sg * 192 * dy)), (255, 255, 255), 6)
    return img


def make_store(n=130, rpm=0.8, **kw):
    st = ac.JpegStore(95)
    for i in range(n):
        st.add(float(i), frame((30 + 6.0 * rpm * i) % 360, seed=i, **kw))
    return st


def zone(tips=True, **kw):
    z = {"id": "r", "type": "cycle", "center": [CX, CY], "radius": R}
    if tips:
        z["tip_hsv"] = [YELLOW_BOX]
    z.update(kw)
    return me.normalize_camera({"id": "c", "url": "x", "frame_size": [W, H], "zones": [z]})["zones"][0]


# ───────────── чистые функции ─────────────
def test_solve_axis_recovers_point_and_drops_outlier_lines():
    rng = np.random.default_rng(3)
    c0 = np.array([300.0, 200.0])
    lines = []
    for a in np.linspace(5, 175, 40):
        d = np.array([math.sin(math.radians(a)), -math.cos(math.radians(a))])
        p, q = c0 + 220 * d + rng.normal(0, 1.0, 2), c0 - 240 * d + rng.normal(0, 1.0, 2)
        lines.append((tuple(p), tuple(q)))
    lines += [((100.0, 100.0), (130.0, 120.0)), ((500.0, 50.0), (520.0, 90.0))]     # две чужие прямые мимо оси
    c, dist, use, angs = at.solve_axis(lines)
    assert np.hypot(*(c - c0)) < 1.5 and use.sum() >= 38 and len({int(a // 30) for a in angs}) == 6


def test_fit_boxes_finds_white_and_colour_parts_and_rejects_noise():
    rng = np.random.default_rng(1)
    yel = np.stack([rng.integers(26, 31, 300), rng.integers(120, 230, 300), rng.integers(225, 256, 300)], 1)
    wh = np.stack([rng.integers(0, 180, 200), rng.integers(0, 12, 200), rng.integers(240, 256, 200)], 1)
    dark = np.stack([rng.integers(80, 100, 400), rng.integers(100, 160, 400), rng.integers(60, 140, 400)], 1)       # фон под кромкой — слишком тёмный
    boxes = at.fit_boxes(np.concatenate([yel, wh, dark]).astype(np.uint8))
    assert len(boxes) == 2
    white, col = boxes
    assert white[:2] == [0, 179] and white[3] <= 110 and white[4] >= 205
    assert col[0] <= 26 and col[1] >= 30 and col[5] == 255
    assert at.fit_boxes(dark.astype(np.uint8)) == []                      # подбирать нечего


def test_to_params_length_ring_and_tip_thin():
    size = {"len": 50.0, "width_p90": 15.0, "start_p10": 240.0, "end_p95": 440.0}
    p = at.to_params(size, 400.0, 9, 170.0 / 400.0)
    assert p["tip_len"] == pytest.approx(0.9 * 50 / 400, abs=1e-3)
    assert 0.4 <= p["tip_r0"] < 0.6 and 1.0 <= p["tip_r1"] <= 1.5 and p["tip_r1"] == pytest.approx(440 / 400 + 0.08, abs=1e-3)
    assert p["tip_thin"] == math.ceil(15 * 0.425) + 3                       # 10: полоска шире палочного thin = 9
    assert at.to_params(size, 400.0, 12, 0.425)["tip_thin"] == 0           # палочного thin хватает — «как у палки»
    assert at.to_params(dict(size, start_p10=60.0), 400.0, 9, 0.425)["tip_r0"] == 0.4          # блик у ступицы кольцо к оси не тянет
    assert at.radius_floor(size) == pytest.approx(440 / 1.42, abs=0.5)     # меньше — кольцо (≤ 1,5 R) не достанет края кончиков
    assert at.to_params(size, at.radius_floor(size), 9, 0.5)["tip_r1"] <= 1.5


def _blob(x, y):
    return {"c": (float(x), float(y)), "area": 100, "pts": np.array([[x, y], [x + 1, y + 1]], np.float32)}


def test_axis_checked_accepts_true_tips_and_rejects_self_confirming_noise():
    """Без клина пары выбираются «≥150° относительно центра», и прямые сами проходят рядом со стартом. Настоящие кончики дают ту же точку
    с любого старта, случайные пятна — следуют за стартом, и такой ответ отбрасывается."""
    true_c = np.array([412.0, 291.0])
    rng = np.random.default_rng(7)
    tips, noise = [], []
    for i in range(70):
        a = math.radians(rng.uniform(0, 360))
        d = np.array([math.sin(a), -math.cos(a)])
        r1, r2 = rng.uniform(140, 170), rng.uniform(140, 170)
        tips.append({"blobs": [_blob(*(true_c + r1 * d)), _blob(*(true_c - r2 * d))], "angle": None})
        noise.append({"blobs": [_blob(*(np.array([400.0, 300.0]) + rng.uniform(-170, 170, 2))) for _ in range(6)], "angle": None})
    good = at.axis_best(tips, (400.0, 300.0), 170.0, False)
    assert good["reliable"] and math.hypot(good["center"][0] - 412, good["center"][1] - 291) <= 2.5, good
    bad = at.axis_best(noise, (400.0, 300.0), 170.0, False)
    assert not bad["reliable"], bad


def test_drop_static_and_pick_pair():
    def blob(x, y):
        return {"c": (x, y), "area": 100, "pts": np.array([[x, y]], np.float32)}
    frames = [[blob(100, 100), blob(300 + 10 * i, 200)] for i in range(20)]
    out = at.drop_static(frames)
    assert all(len(f) == 1 for f in out)                                    # (100,100) стоит в 100 % кадров — блик; бегущее пятно осталось
    bl = [blob(400 + 150, 300), blob(400 - 150, 300 + 5), blob(400, 300 - 150)]            # 90°, 270° и 0°
    pr = at.pick_pair(bl, (400, 300), 170.0)
    assert pr is not None and {round(b["c"][0]) for b in pr} == {550, 250}
    assert at.pick_pair(bl, (400, 300), 170.0, angle=90.0) is not None             # клин вокруг 90° и напротив — те же два пятна
    assert at.pick_pair(bl, (400, 300), 170.0, angle=0.0) is None                  # на 0° пятно есть, напротив (180°) нет
    assert at.pick_pair([blob(550, 300)], (400, 300), 170.0, angle=90.0) is None


# ───────────── подгонка на синтетике ─────────────
def tune(store, z, start=(CX + 22, CY - 18)):
    tn = ac.RodTuner(store, z, start, R, max_radius=R * 1.2)
    return tn, tn.run()


def test_tuner_finds_axis_colour_and_size_from_strips():
    z = zone()
    tn, res = tune(make_store(), z)
    tips = res["tips"]
    assert tips["applied"] and tips["chosen"] in ("seed+white", "fitted"), tips
    cx, cy = res["new"]["center"]
    assert math.hypot(cx - CX, cy - CY) <= 3.0, (cx, cy, tips)             # старт был в 28 px от оси
    assert tn.center_locked and res["improved"]
    new = res["new"]
    assert len(new["tip_hsv"]) == 2 and any(b[2] == 0 and b[3] <= 110 for b in new["tip_hsv"])      # белая часть полоски добавлена
    assert 0.9 * 40 / R * 0.7 < new["tip_len"] < 0.9 * 46 / R * 1.3        # полоска 150..192 ≈ 42 px
    assert new["tip_r0"] < 0.95 and new["tip_r1"] >= 1.0
    assert res["after"]["found"] >= 0.95 and res["after_val"]["found"] >= 0.93
    assert res["old"]["tip_hsv"] == [YELLOW_BOX]                            # посев остался в «было»
    import json
    json.dumps(res["tips"])


def test_tuner_ignores_static_glare_in_tip_ring():
    z = zone()
    _, res = tune(make_store(static=True), z)
    cx, cy = res["new"]["center"]
    assert res["tips"]["applied"] and math.hypot(cx - CX, cy - CY) <= 3.5, (cx, cy, res["tips"])


def test_tuner_skips_tip_stage_when_shaft_stands_still():
    z = zone()
    _, res = tune(make_store(n=60, rpm=0.0), z)
    tips = res["tips"]
    assert tips["ran"] and not tips["applied"] and "прямых" in tips["reason"], tips
    assert res["new"]["tip_hsv"] == res["old"]["tip_hsv"]                  # ничего не трогали


def test_tuner_without_tips_is_unchanged():
    z = zone(tips=False)
    tn, res = tune(make_store(), z)
    assert res["tips"] == {"ran": False, "applied": False, "reason": "отражатели не заданы"}
    assert not any(k in res["new"] for k in ac.TIP_KEYS) and not tn.center_locked


# ───────────── задание, применение и сторож ─────────────
def test_job_pack_apply_and_watch_keys():
    import motion_autocal_job as aj
    import motion_autocal_watch as aw
    z = zone()
    _, res = tune(make_store(), z)
    g = {"x0": 0, "y0": 0}
    pk = aj.AutocalJob._pack(z, res, g, 1.0, 1.0, 1.0)
    assert pk["new"]["tip_hsv"] == res["new"]["tip_hsv"] and pk["old"]["tip_hsv"] == z["tip_hsv"] and pk["tips"]["applied"]
    import json
    json.dumps(aj.jsonable({"r": pk}))
    zp_tip, zp_plain = aw.zone_params(z), aw.zone_params(zone(tips=False))
    assert set(aw.TIP_ZONE_KEYS) <= set(zp_tip) and not set(aw.TIP_ZONE_KEYS) & set(zp_plain)    # у зоны без отражателей их ключи не участвуют
    assert zp_plain == aw.zone_params({k: v for k, v in zone(tips=False).items() if k in aw.ZONE_KEYS})   # и «было» из подгонки (без tip_*) с ней совпадает


def test_manager_apply_writes_tip_keys(tmp_path, monkeypatch):
    import server
    import motion_autocal_job as aj
    mgr = server.Manager(tmp_path)
    cam = {"id": "c", "name": "cam", "url": "x", "enabled": False, "frame_size": [W, H],
           "zones": [{"id": "a", "type": "cycle", "center": [10, 10], "radius": 100, "tip_hsv": [YELLOW_BOX]}]}
    mgr.cfg = me.normalize_config({"cameras": [cam]})
    applied = []
    monkeypatch.setattr(mgr, "apply", lambda cfg, save=True: applied.append(cfg) or cfg)
    job = aj.AutocalJob("c", lambda cid: None)
    new = {"center": [11.5, 12.5], "radius": 90.0, "red_hsv": [16, 155, 35, 55], "blue_hsv": [96, 135, 55, 60], "thin": 9, "blue_alone": 0.6,
           "tip_hsv": [[0, 179, 0, 60, 235, 255], YELLOW_BOX], "tip_len": 0.11, "tip_r0": 0.7, "tip_r1": 1.2, "tip_thin": 10}
    job.st.update(state="done", results={"a": {"improved": True, "new": new}})
    mgr.autocal["c"] = job
    assert mgr.autocal_apply("c", []) == {"applied": ["a"], "skipped": []}
    z = applied[0]["cameras"][0]["zones"][0]
    assert z["tip_hsv"] == new["tip_hsv"] and z["tip_len"] == 0.11 and z["tip_r0"] == 0.7 and z["tip_r1"] == 1.2 and z["tip_thin"] == 10
    assert z["center"] == [11.5, 12.5]


def test_tip_thin_widens_tip_mask_only():
    """tip_thin шире thin: полоска, которую палочный thin вырезал бы как пятно, ловится. 0 — как у палки (по умолчанию)."""
    assert zone()["tip_thin"] == 0 and zone(tip_thin=14)["tip_thin"] == 14
    with pytest.raises(ValueError):
        zone(tip_thin=99)
    thick = frame(60, strips=False)
    cv2.line(thick, (int(CX + 160 * math.sin(math.radians(60))), int(CY - 160 * math.cos(math.radians(60)))),
             (int(CX + 190 * math.sin(math.radians(60))), int(CY - 190 * math.cos(math.radians(60)))), (255, 255, 255), 22)       # полоска толще thin = 9
    a = me.CycleRT(zone(thin=9), [W, H], (W, H))
    b = me.CycleRT(zone(thin=9, tip_thin=25), [W, H], (W, H))
    sa = a._tip_profile(a._tipmask(thick[a.ty0:a.ty1, a.tx0:a.tx1], [[0, 179, 0, 60, 235, 255]]))
    sb = b._tip_profile(b._tipmask(thick[b.ty0:b.ty1, b.tx0:b.tx1], [[0, 179, 0, 60, 235, 255]]))
    assert sb.max() > sa.max() + 0.2
