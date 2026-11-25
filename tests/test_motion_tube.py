"""Уровнемер «столбик» (зона tube): уровень по двум цветам, разметка прямоугольником / четырьмя точками / многоугольником, направления,
качество («колонку не видно»), удержание, «мягкие» настройки без перезапуска камеры, регистр в ПЛК.

Кадры синтетические: серый фон, узкая колонка, до уровня — цвет «заполнено», дальше — «пусто».
"""
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

MS = Path(__file__).resolve().parent.parent / "motion_service"
sys.path.insert(0, str(MS))

import motion_camera as mcam  # noqa: E402
import motion_engine as me  # noqa: E402
import motion_modbus  # noqa: E402
import motion_scenarios as ms  # noqa: E402
import server  # noqa: E402

W, H = 640, 480
BACK, RED, WHITE = (120, 125, 120), (45, 40, 150), (205, 225, 230)      # BGR
TOP, BOT = 40, 440                                                        # шкала: y=440 — 0 %, y=40 — 100 %
RECT = [[300, BOT], [320, BOT], [320, TOP], [300, TOP]]


def lab(bgr):
    x = cv2.cvtColor(np.array([[bgr]], np.float32) / 255.0, cv2.COLOR_BGR2Lab)[0, 0]
    return [round(float(v), 1) for v in x]


FILL, EMPTY = lab(RED), lab(WHITE)


def column(pct, x=(300, 320), tilt=0, size=(W, H)):
    """Колонка с уровнем pct %; tilt — на сколько точек верх смещён вправо."""
    img = np.full((size[1], size[0], 3), BACK, np.uint8)
    edge = lambda y: tilt * (BOT - y) / float(BOT - TOP)
    level = BOT - (BOT - TOP) * pct / 100.0
    for a, b, col in ((TOP, level, WHITE), (level, BOT, RED)):
        if b - a >= 1:
            cv2.fillPoly(img, [np.array([[x[0] + edge(a), a], [x[1] + edge(a), a], [x[1] + edge(b), b], [x[0] + edge(b), b]]).astype(np.int32)], col)
    return img


def tube(**over):
    z = {"id": "t1", "name": "Уровнемер 1", "type": "tube", "points": RECT, "fill_colors": [FILL], "empty_colors": [EMPTY], "smooth": 1}
    z.update(over)
    return z


def cam(**over):
    c = {"id": "lv", "url": "x", "frame_size": [W, H], "proc_fps": 5, "zones": [tube()]}
    c.update(over)
    return me.normalize_camera(c)


def run(eng, img, t0=0.0, n=3, dt=0.2):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    st = None
    for i in range(n):
        st = eng.process(gray, t0 + i * dt, img) or st
    return st


def zone(img, **over):
    return run(me.CameraEngine(cam(zones=[tube(**over)])), img)["zones"][0]


# ───────────────────────────── настройки ─────────────────────────────
def test_defaults_and_second_normalize_keeps_config():
    z = cam(zones=[{"id": "t1", "type": "tube", "points": RECT}])["zones"][0]
    assert (z["direction"], z["tol"], z["min_quality"], z["hold_s"], z["smooth"]) == ("up", 12, 80, 60, 5)
    assert z["fill_colors"] == [] and z["empty_colors"] == [] and z["out"] is None
    assert me.normalize_camera(cam())["zones"][0] == cam()["zones"][0]


@pytest.mark.parametrize("bad, text", [
    (dict(points=RECT[:2]), "меньше трёх точек"),
    (dict(points=[[10, 10], [20, 10], [20, 11], [10, 11]]), "слишком мала"),
    (dict(direction="наискосок"), "направление"),
    (dict(fill_colors=[[1, 2]]), "три числа"),
    (dict(fill_colors=[FILL] * 7), "не длиннее"),
    (dict(tol=0), "не меньше"),
])
def test_normalize_errors(bad, text):
    with pytest.raises(ValueError, match=text):
        cam(zones=[tube(**bad)])


# ───────────────────────────── уровень ─────────────────────────────
@pytest.mark.parametrize("pct", [0, 5, 25, 50, 77, 95, 100])
def test_level_rect(pct):
    z = zone(column(pct))
    assert z["valid"] and z["bit"] == 1 and not z["held"] and z["type"] == "tube"
    assert z["pct"] == pytest.approx(pct, abs=0.6) and z["flow"] == z["pct"] == z["value"]
    assert z["quality"] >= 97 and z["diff"] == z["quality"]


@pytest.mark.parametrize("pct", [10, 60])
def test_loose_rect_with_background_around_narrow_column(pct):
    """Два клика по диагонали с запасом по бокам: колонка занимает пятую часть ширины."""
    z = zone(column(pct, x=(300, 314)), points=[[270, BOT], [340, BOT], [340, TOP], [270, TOP]])
    assert z["valid"] and z["pct"] == pytest.approx(pct, abs=0.6)


@pytest.mark.parametrize("pct", [15, 50, 85])
def test_tilted_column_four_points_in_any_order(pct):
    pts = [[340, TOP], [300, BOT], [360, TOP], [320, BOT]]                # наклон 40 точек, клики вразнобой
    z = zone(column(pct, tilt=40), points=pts)
    assert z["valid"] and z["pct"] == pytest.approx(pct, abs=0.8) and z["quality"] >= 95


@pytest.mark.parametrize("pct", [20, 70])
def test_polygon_with_more_points(pct):
    pts = [[300, BOT], [320, BOT], [320, 240], [320, TOP], [300, TOP]]
    assert zone(column(pct), points=pts)["pct"] == pytest.approx(pct, abs=0.6)


@pytest.mark.parametrize("direction, rotate", [
    ("down", cv2.ROTATE_180), ("right", cv2.ROTATE_90_CLOCKWISE), ("left", cv2.ROTATE_90_COUNTERCLOCKWISE)])
def test_other_directions(direction, rotate):
    img = cv2.rotate(column(30), rotate)
    mark = np.zeros((H, W), np.uint8)
    cv2.fillPoly(mark, [np.array(RECT, np.int32)], 255)
    x, y, w, h = cv2.boundingRect(cv2.rotate(mark, rotate))
    pts = [[x, y], [x + w - 1, y], [x + w - 1, y + h - 1], [x, y + h - 1]]
    c = cam(frame_size=[img.shape[1], img.shape[0]], zones=[tube(points=pts, direction=direction)])
    z = run(me.CameraEngine(c), img)["zones"][0]
    assert z["valid"] and z["pct"] == pytest.approx(30, abs=0.8)


def test_other_resolution_than_marking():
    z = run(me.CameraEngine(cam()), cv2.resize(column(40), (W * 2, H * 2), interpolation=cv2.INTER_NEAREST))["zones"][0]
    assert z["pct"] == pytest.approx(40, abs=0.6)


def test_stuck_flap_and_dim_light():
    img = column(40)
    img[100:108, 300:321] = RED                                           # залипший флажок высоко в «пусто»
    assert zone(img)["pct"] == pytest.approx(40, abs=0.6)
    dim = (column(40).astype(np.float32) * 0.9).astype(np.uint8)          # свет чуть притух
    assert zone(dim)["pct"] == pytest.approx(40, abs=0.6)


def test_second_color_sample_covers_shadow():
    img = column(40)
    img[:150] = (img[:150].astype(np.float32) * 0.55).astype(np.uint8)    # верх колонки в тени
    assert not zone(img)["valid"]
    shade = lab(tuple(int(v * 0.55) for v in WHITE))
    z = zone(img, empty_colors=[EMPTY, shade])
    assert z["valid"] and z["pct"] == pytest.approx(40, abs=0.6)


def test_dark_shadow_beside_column_does_not_raise_level():
    """Прямоугольник с фоном: тень сбоку похожа на тёмное «заполнено», но узкая — по ней строку заполненной не считаем."""
    img = column(30, x=(300, 320))
    img[TOP:BOT - 160, 280:284] = RED                                     # 4 точки «своего» цвета сбоку выше уровня
    z = zone(img, points=[[275, BOT], [325, BOT], [325, TOP], [275, TOP]])
    assert z["valid"] and z["pct"] == pytest.approx(30, abs=0.6)


# ───────────────────────────── не видно ─────────────────────────────
def test_no_colors_no_value():
    z = zone(column(40), fill_colors=[])
    assert not z["valid"] and "не заданы цвета" in z["reason"]


@pytest.mark.parametrize("cover", [(0, 480), (60, 300), (200, 440)])
def test_covered_column_is_not_zero(cover):
    """Перед колонкой серый предмет (человек): не «0 %», а «не читается»."""
    img = column(50)
    img[cover[0]:cover[1], 250:370] = (70, 75, 80)
    z = zone(img)
    assert not z["valid"] and "не читается" in z["reason"] and z["pct"] == 0


@pytest.mark.parametrize("level, cover", [(50, (30, 50)), (50, (45, 65)), (15, (0, 20)), (85, (80, 100)), (50, (40, 52))])
def test_partly_covered_at_the_level_is_not_a_wrong_number(level, cover):
    """Рука или труба поперёк колонки у самой границы: где уровень, неизвестно — не выдаём ближайший край перекрытия за значение."""
    img = column(level)
    y = lambda pct: int(BOT - (BOT - TOP) * pct / 100.0)
    img[y(cover[1]):y(cover[0]) + 1, 250:370] = (70, 75, 80)
    z = zone(img, min_quality=50)
    assert not z["valid"] and "не читается" in z["reason"], z


def test_cover_away_from_the_level_does_not_matter():
    img = column(30)
    img[90:150, 250:370] = (70, 75, 80)                                   # 15 % шкалы высоко в «пусто»
    z = zone(img)
    assert z["valid"] and z["pct"] == pytest.approx(30, abs=0.6)


def test_frozen_picture_does_not_extend_hold():
    eng = me.CameraEngine(cam(zones=[tube(hold_s=3)]))
    img, gray = column(60), cv2.cvtColor(column(60), cv2.COLOR_BGR2GRAY)
    run(eng, img, 0.0, 4)
    for i in range(1, 200):                                               # 40 с тех же кадров, с переоткрытиями потока
        if i % 50 == 0:
            eng.reset()
        st = eng.process(gray, 0.6 + i * 0.2, img, fresh=False) or None
        if st and st["ts"] > 25:
            assert not st["zones"][0]["valid"], st["zones"][0]
    assert run(eng, column(70), 61.0, 3)["zones"][0]["pct"] == pytest.approx(70, abs=0.6)


def test_old_median_dropped_after_long_gap():
    eng = me.CameraEngine(cam(zones=[tube(smooth=5, hold_s=60)]))
    run(eng, column(30), 0.0, 8)
    run(eng, np.full((H, W, 3), BACK, np.uint8), 2.0, 3)
    assert run(eng, column(80), 50.0, 1)["zones"][0]["pct"] == pytest.approx(80, abs=0.6)


@pytest.mark.parametrize("change", [dict(tol=1.0), dict(fill_colors=[[5.0, 0.0, 0.0]]), dict(min_quality=100, empty_colors=[[5.0, 0.0, 0.0]])])
def test_changing_detector_settings_drops_held_value(change):
    c = cam()
    eng = me.CameraEngine(c)
    noisy = column(40)
    noisy[::7] = BACK
    run(eng, noisy, 0.0, 4)
    c["zones"][0].update(change)
    z = run(eng, np.full((H, W, 3), BACK, np.uint8), 1.0, 1)["zones"][0]
    assert not z["valid"], "значение, полученное прежними настройками, не держим"


def test_broken_frames_follow_gauge_hold():
    eng = me.CameraEngine(cam(zones=[tube(hold_s=3)]))
    st = run(eng, column(40))
    held = eng.held_zones(st["zones"], 2.0)[0]
    assert held["valid"] and held["held"]
    assert not eng.held_zones(st["zones"], 10.0)[0]["valid"]


class _StuckCap:
    """Захват залип: grab() успешен, кадр всегда один и тот же — и после переоткрытия тоже."""

    def __init__(self, frame):
        self.frame = frame

    def isOpened(self):
        return True

    def grab(self):
        time.sleep(0.02)
        return True

    def retrieve(self):
        return True, self.frame.copy()

    def release(self):
        pass

    def set(self, *_):
        return True

    def get(self, *_):
        return 0.0


def test_worker_stuck_stream_ends_in_no_data_despite_reopening(tmp_path, monkeypatch):
    monkeypatch.setattr(mcam, "OPEN_GAP_S", 0.0)
    monkeypatch.setattr(mcam, "FREEZE_REOPEN_S", 0.0)
    opened = []

    def factory(url, tr):
        opened.append(1)
        return _StuckCap(column(60))

    cfg = cam(url="rtsp://h/cam", hw_accel="off", proc_fps=20, freeze_timeout_s=1, reconnect_delay_s=0.5, corrupt_ratio=0,       # ровный серый фон
              zones=[tube(hold_s=1)])                                                                    # синтетики детектор «каши» принял бы за битый кадр
    w = mcam.CameraWorker(cfg, me.normalize_modbus({}), motion_modbus.LinkRegistry(), tmp_path, factory)
    w.start()
    try:
        value = lambda: ms.source_bits([{"camera": "lv", "zone": "t1"}], {"lv": w.state()}, analog=True, now=time.time())[0]
        t0 = time.time()
        while value() == -1 and time.time() - t0 < 8:
            time.sleep(0.05)
        assert value() == pytest.approx(600, abs=6)
        time.sleep(4.0)                                    # застыла (1 с) + «держать» (1 с) уже вышли
        seen = []
        for _ in range(60):                                # ещё 6 с с переоткрытиями потока
            seen.append(value())
            time.sleep(0.1)
        assert len(opened) >= 3 and set(seen) == {-1}, (len(opened), sorted(set(seen)))
    finally:
        w.stop()


def test_grayscale_camera_is_no_data():
    gray = cv2.cvtColor(cv2.cvtColor(column(50), cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
    assert not zone(gray)["valid"]


def test_hold_then_no_data_then_new_value():
    eng = me.CameraEngine(cam(zones=[tube(hold_s=2, smooth=5)]))
    run(eng, column(30), 0.0, 8)
    blind = np.full((H, W, 3), BACK, np.uint8)
    held = run(eng, blind, 1.6, 3)["zones"][0]
    assert held["valid"] and held["held"] and held["pct"] == pytest.approx(30, abs=0.6) and "не читается" in held["reason"]
    assert not run(eng, blind, 5.0, 2)["zones"][0]["valid"]
    assert run(eng, column(80), 6.0, 1)["zones"][0]["pct"] == pytest.approx(80, abs=0.6), "после пропажи старое в медиану не идёт"


def test_dead_camera_holds_a_minute():
    eng = me.CameraEngine(cam())
    run(eng, column(65))
    z = eng.dead_state(30.0, "поток оборвался")["zones"][0]
    assert z["valid"] and z["held"] and z["pct"] == pytest.approx(65, abs=0.6) and z["reason"] == "нет кадров"
    assert not eng.dead_state(90.0, "поток оборвался")["zones"][0]["valid"]


def test_resolution_change_keeps_held_value():
    eng = me.CameraEngine(cam())
    run(eng, column(65))
    blind = np.full((H * 2, W * 2, 3), BACK, np.uint8)
    z = run(eng, blind, 1.0, 3)["zones"][0]
    assert z["valid"] and z["held"] and z["pct"] == pytest.approx(65, abs=0.6)


def test_removing_colors_drops_held_value():
    c = cam()
    eng = me.CameraEngine(c)
    run(eng, column(65))
    c["zones"][0]["fill_colors"] = []
    assert not run(eng, column(65), 1.0, 1)["zones"][0]["valid"]


# ───────────────────────────── «мягкие» настройки ─────────────────────────────
class _Worker:
    def __init__(self, cfg):
        self.cfg = cfg


def test_colors_change_without_camera_restart():
    old = cam(zones=[tube(fill_colors=[], empty_colors=[])])
    eng = me.CameraEngine(old)
    assert not run(eng, column(40))["zones"][0]["valid"]
    new = cam(zones=[tube(tol=14, hold_s=5, min_quality=70, smooth=2)])
    assert server._hot_update(_Worker(old), new) is True
    z = run(eng, column(40), 1.0, 3)["zones"][0]
    assert z["valid"] and z["pct"] == pytest.approx(40, abs=0.6) and eng.gauges["t1"].hist.maxlen == 2


@pytest.mark.parametrize("change", [dict(points=[[290, BOT], [330, BOT], [330, TOP], [290, TOP]]), dict(direction="down"), dict(enabled=False)])
def test_geometry_change_restarts_camera(change):
    assert server._hot_update(_Worker(cam()), cam(zones=[tube(**change)])) is False


# ───────────────────────────── превью, ПЛК ─────────────────────────────
def test_preview_fits_box_and_marks_colors():
    eng = me.CameraEngine(cam())
    img = column(40)
    run(eng, img)
    t = eng.gauges["t1"]
    red = lambda m: int(((m[:, :, 2] > 230) & (m[:, :, 1] < 80) & (m[:, :, 0] < 80)).sum())
    m = t.preview(img, 480)
    assert max(m.shape[:2]) == 480 and red(m) > 200
    assert red(t.preview(img, 480, fill=[[5.0, 0.0, 0.0]])) == 0                       # пробный цвет, который не ловит ничего
    raw = t.preview(img, 900, raw=True)
    assert max(raw.shape[:2]) == 900
    assert {tuple(int(v) for v in raw[y, x]) for y in range(100, 800, 50) for x in (raw.shape[1] // 2,)} <= {BACK, RED, WHITE}, "без смешивания цветов"


def test_scenario_and_register_minus_one():
    cfg = me.normalize_config({"cameras": [{"id": "lv", "url": "x", "frame_size": [W, H], "zones": [tube(), tube(id="t2")]}],
                               "scenarios": [{"id": "s1", "sources": [{"camera": "lv", "zone": "t2"}], "out": {"kind": "holding", "address": 7}}]})
    sc = cfg["scenarios"][0]
    assert sc["rule"] == "value" and sc["scale"] == 10
    st = run(me.CameraEngine(cfg["cameras"][0]), column(40))
    assert ms.source_bits(sc["sources"], {"lv": {"result": st}}, analog=True, now=1.0) == [pytest.approx(400, abs=6)]
    written = []

    class Client:
        connected = True

        def write_register(self, address, value, device_id=1):
            written.append((address, value))

            class R:
                def isError(self):
                    return False
            return R()

    link = motion_modbus.ModbusLink("p", client_factory=lambda *_: Client())
    assert link.write([("holding", 7, ms.NO_DATA)]) and written == [(7, 65535)], "−1 в регистре — все единицы: в ПЛК INT читает −1"
