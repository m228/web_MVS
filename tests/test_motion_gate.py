"""Зона-шибер (цветной указатель рядом с линейкой): проценты заполнения, удержание, сценарий в ПЛК.

Кадры синтетические: серая линейка с красными метками и красная полоска-указатель справа на заданной высоте. Плюс прогон на настоящих
роликах шибера, если они лежат на диске (E:\\grib_dif\\shuber) — иначе пропускается.
"""
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

MS = Path(__file__).resolve().parent.parent / "motion_service"
sys.path.insert(0, str(MS))

import motion_engine as me  # noqa: E402
import motion_scenarios as ms  # noqa: E402

W, H = 960, 540
RED = (40, 30, 220)                                     # BGR
RULER = [[300, 470], [360, 470], [360, 70], [300, 70]]  # низ y=470 (0 %), верх y=70 (100 %)
TRACK = [[340, 40], [560, 40], [560, 510], [340, 510]]  # нарочно заезжает на линейку: её метки браться не должны
VIDEOS = sorted(Path(r"E:\grib_dif\shuber\rtsp_2").glob("*.avi")) if Path(r"E:\grib_dif\shuber\rtsp_2").is_dir() else []


def frame(y=None, size=(W, H), x=(420, 520), thick=14):
    """y — высота центра указателя (None — указателя нет)."""
    img = np.full((size[1], size[0], 3), (120, 125, 120), np.uint8)
    for yy in range(90, 470, 50):                       # красные метки на линейке
        cv2.rectangle(img, (302, yy), (358, yy + 12), RED, -1)
    if y is not None:
        cv2.rectangle(img, (x[0], int(y - thick // 2)), (x[1], int(y + thick // 2)), RED, -1)
    return img


def gate(**over):
    z = {"id": "g1", "name": "Шибер", "type": "gate", "ruler": RULER, "points": TRACK, "smooth": 1}
    z.update(over)
    return z


def cam(**over):
    c = {"id": "sh", "url": "x", "frame_size": [W, H], "proc_fps": 5, "zones": [gate()]}
    c.update(over)
    return me.normalize_camera(c)


def run(eng, img, t0=0.0, n=3, dt=0.2):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    st = None
    for i in range(n):
        st = eng.process(gray, t0 + i * dt, img) or st
    return st


# ───────────────────────────── настройки ─────────────────────────────
def test_normalize_orders_corners_and_defaults():
    z = cam(zones=[gate(ruler=[[360, 70], [300, 470], [300, 70], [360, 470]])])["zones"][0]
    assert z["ruler"] == [[300, 470], [360, 470], [360, 70], [300, 70]]
    assert z["red_hsv"] == list(me.GATE_RED_HSV) and z["out"] is None and not z["flip"]
    assert me.normalize_camera(cam())["zones"][0] == cam()["zones"][0], "повторная проверка конфиг не меняет"


@pytest.mark.parametrize("bad, text", [
    (dict(ruler=RULER[:3]), "четыре угла"),
    (dict(points=TRACK[:2]), "меньше трёх точек"),
    (dict(ruler=[[100, 100], [400, 101], [400, 103], [100, 102]]), "вертикально"),
    (dict(red_hsv=[1, 2, 3]), "четыре числа"),
])
def test_normalize_errors(bad, text):
    with pytest.raises(ValueError, match=text):
        cam(zones=[gate(**bad)])


# ───────────────────────────── геометрия ─────────────────────────────
@pytest.mark.parametrize("y, pct", [(470, 0), (370, 25), (270, 50), (170, 75), (70, 100), (500, 0), (50, 100)])
def test_level_straight_ruler(y, pct):
    assert me.ruler_level(me.order_ruler(RULER), (470, y)) * 100 == pytest.approx(pct, abs=0.01)


def test_level_follows_slanted_sides():
    """Нижняя сторона горизонтальна, верхняя наклонена: линия уровня для точки справа продолжает наклон между ними."""
    r = [[300, 470], [360, 470], [360, 90], [300, 70]]
    assert me.ruler_level(r, (480, 470)) == pytest.approx(0.0, abs=1e-6)
    assert me.ruler_level(r, (480, 130)) == pytest.approx(1.0, abs=1e-6)       # верхняя сторона на x=480 проходит через y=130
    mid = me.ruler_level(r, (480, 300))                                           # середина: y(0.5) = 270 + (180/60)·10 = 300
    assert mid == pytest.approx(0.5, abs=1e-6)


# ───────────────────────────── на кадрах ─────────────────────────────
@pytest.mark.parametrize("y, pct", [(430, 10), (270, 50), (110, 90)])
def test_pointer_gives_percent(y, pct):
    st = run(me.CameraEngine(cam()), frame(y))
    z = st["zones"][0]
    assert z["valid"] and z["bit"] == 1 and not z["held"]
    assert z["pct"] == pytest.approx(pct, abs=0.6) and z["flow"] == z["pct"] == z["value"]
    assert z["area"] > 1000 and z["box"][0] == pytest.approx(420, abs=2) and z["ptr"][1] == pytest.approx(y, abs=1.5)


def test_ruler_marks_are_not_the_pointer():
    z = run(me.CameraEngine(cam()), frame(None))["zones"][0]
    assert not z["valid"] and z["bit"] == 0 and z["pct"] == 0 and "не найден" in z["reason"]


def test_flip_puts_zero_on_top():
    z = run(me.CameraEngine(cam(zones=[gate(flip=True)])), frame(430))["zones"][0]
    assert z["pct"] == pytest.approx(90, abs=0.6) and z["t"] == pytest.approx(0.1, abs=0.006)


def test_small_spot_is_ignored_but_reported():
    img = frame(None)
    cv2.rectangle(img, (450, 200), (460, 208), RED, -1)
    z = run(me.CameraEngine(cam()), img)["zones"][0]
    assert not z["valid"] and 0 < z["area"] < 200


def test_hold_then_lost_then_new_value_without_old_history():
    eng = me.CameraEngine(cam(zones=[gate(hold_s=2, smooth=5)]))
    run(eng, frame(430), 0.0, 8)
    held = run(eng, frame(None), 1.6, 3)["zones"][0]
    assert held["valid"] and held["held"] and held["pct"] == pytest.approx(10, abs=0.6)
    gone = run(eng, frame(None), 5.0, 2)["zones"][0]
    assert not gone["valid"] and not gone["held"]
    back = run(eng, frame(110), 6.0, 1)["zones"][0]
    assert back["pct"] == pytest.approx(90, abs=0.6), "после пропажи старые 10 % в медиану не идут"


def test_zero_hold_keeps_smoothing():
    """hold_s = 0 («не держать») не должен отключать медиану, пока указатель виден; пропал — сразу «нет данных»."""
    eng = me.CameraEngine(cam(zones=[gate(hold_s=0, smooth=5)]))
    run(eng, frame(430), 0.0, 8)
    assert run(eng, frame(110), 1.6, 1)["zones"][0]["pct"] == pytest.approx(10, abs=0.6)
    assert not run(eng, frame(None), 1.8, 1)["zones"][0]["valid"]


def test_median_ignores_one_jump():
    eng = me.CameraEngine(cam(zones=[gate(smooth=5)]))
    run(eng, frame(430), 0.0, 8)
    z = run(eng, frame(110), 1.6, 1)["zones"][0]
    assert z["pct"] == pytest.approx(10, abs=0.6)


def test_other_resolution_than_marking():
    z = run(me.CameraEngine(cam()), cv2.resize(frame(270), (W * 2, H * 2)))["zones"][0]
    assert z["pct"] == pytest.approx(50, abs=0.6) and z["box"][0] == pytest.approx(420, abs=2), "точки — в кадре разметки"


def test_dead_camera_and_no_direct_plc_write():
    eng = me.CameraEngine(cam())
    st = run(eng, frame(270))
    assert eng.compute_writes(st) == []
    dead = eng.dead_state(10.0, "нет связи")["zones"][0]
    assert not dead["valid"] and dead["reason"] == "нет кадров"


def test_preview_mask_and_raw():
    eng = me.CameraEngine(cam())
    run(eng, frame(270))
    g = eng.gates["g1"]
    img = frame(270)
    marked = lambda m: int(((m[:, :, 2] > 230) & (m[:, :, 1] < 80) & (m[:, :, 0] < 80)).sum())     # красные точки маски (рамки жёлтые и голубые)
    m = g.mask_preview(img, max_w=300)
    assert m.shape[1] == 300 and marked(m) > 100
    vx0, _, vx1, _ = g.view
    assert g.mask_preview(img, max_w=vx1 - vx0, raw=True).shape[1] == vx1 - vx0
    off = g.mask_preview(img, red_hsv=[0, 179, 255, 255], max_w=300)          # пробный цвет, который не ловит ничего
    assert marked(off) == 0


# ───────────────────────────── сценарий в ПЛК ─────────────────────────────
def full(sources, **sc):
    zones = [gate(), {"id": "z1", "points": TRACK}]
    s = {"id": "s1", "sources": sources, "out": {"kind": "holding", "address": 7}}
    s.update(sc)
    return me.normalize_config({"cameras": [{"id": "sh", "url": "x", "zones": zones}], "scenarios": [s]})["scenarios"][0]


G, Z = {"camera": "sh", "zone": "g1"}, {"camera": "sh", "zone": "z1"}


def test_scenario_with_gate_is_single_value():
    s = full([Z, G], rule="sum")
    assert s["rule"] == "value" and s["sources"] == [G]
    assert full([Z], rule="value")["rule"] == "or", "шибер убрали — сценарий снова обычный"
    assert full([{"camera": "sh", "zone": "нет"}], rule="value") == dict(full([], rule="value")), "зону удалили — остаётся value и пишет «нет данных»"
    with pytest.raises(ValueError, match="только в регистр"):
        full([G], out={"kind": "coil", "address": 1})


def test_scenario_value_and_no_data():
    st = run(me.CameraEngine(cam()), frame(270))
    states = {"sh": {"result": st}}
    assert ms.evaluate("value", ms.source_bits([G], states, analog=True)) == pytest.approx(500, abs=6)
    lost = run(me.CameraEngine(cam()), frame(None))
    for bad in ({"sh": {"result": lost}}, {"sh": {"result": dict(st, alive=False)}}, {}):
        assert ms.evaluate("value", ms.source_bits([G], bad, analog=True)) == 65535
    assert ms.evaluate("value", []) == 65535


def test_scenario_runner_writes_percent_times_ten():
    written = []

    class Link:
        last_error = ""

        def write(self, items):
            written.extend(items)
            return True

    class Links:
        def get(self, *_):
            return Link()

    st = run(me.CameraEngine(cam()), frame(430))

    class Worker:
        def state(self):
            return {"result": st}

    class Mgr:
        links = Links()
        workers = {"sh": Worker()}
        cfg = {"modbus": {"enable": True, "host": "p", "port": 502, "device_id": 1, "write_period_s": 1}, "scenarios": [full([G])]}

    r = ms.ScenarioRunner(Mgr())
    r.tick(1.0)
    kind, addr, value = written[0]
    assert (kind, addr) == ("holding", 7) and value == pytest.approx(100, abs=6)


# ───────────────────────────── настоящие ролики ─────────────────────────────
@pytest.mark.skipif(len(VIDEOS) < 3, reason="нет роликов шибера на диске")
def test_real_videos_pointer_always_found():
    c = me.normalize_camera({"id": "sh", "url": "x", "frame_size": [1920, 1080], "proc_fps": 5, "zones": [
        gate(ruler=[[918, 787], [990, 786], [1046, 136], [975, 131]], points=[[1025, 520], [1210, 520], [1210, 800], [1025, 800]], smooth=5)]})
    span = []
    for path in VIDEOS:
        eng, cap, vals, n = me.CameraEngine(c), cv2.VideoCapture(str(path)), [], 0
        while True:
            ok, fr = cap.read()
            if not ok:
                break
            st = eng.process(cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY), n / 5.0, fr)
            n += 1
            if st:
                z = st["zones"][0]
                assert z["valid"] and not z["held"], "%s кадр %d: %s" % (path.name, n, z)
                vals.append(z["pct"])
        span.append((min(vals), max(vals)))
    # 15:56 — стоит внизу; 16:00 — едет сверху вниз; 16:04 — поднимается
    assert span[0][1] - span[0][0] < 0.5 and span[0] == pytest.approx((6.5, 6.6), abs=0.5)
    assert span[1] == pytest.approx((19.9, 29.5), abs=0.8) and span[2] == pytest.approx((20.1, 26.9), abs=0.8)
