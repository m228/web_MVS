"""«Сок есть / пусто» по движению в кружке стакана (зона clarity, поля mv_*).

Кадры синтетические. Пустой стакан — неподвижная картинка + шум сжатия (разный в каждом кадре); стакан с соком — то же + крупные пятна, которые
двигаются от кадра к кадру (рябь, течение). Метрика подобрана на роликах: пустые стаканы 22.11 и полные 19.11 (см. tasks/todo_juice-flow.md).
"""
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

MS = Path(__file__).resolve().parent.parent / "motion_service"
sys.path.insert(0, str(MS))

import motion_engine as me  # noqa: E402

W, H = 640, 360
CENTERS = [(120 + 120 * i, 180) for i in range(4)]
BASE = (60, 110, 150)           # BGR: светлый жёлто-бурый — осадок или мутный сок


def cfg_for(**over):
    zs = [{"id": "g%d" % (i + 1), "name": "Стакан %d" % (i + 1), "type": "clarity", "center": list(c), "radius": 30, "smooth_s": 0, "hold_s": 0, "scale": 2,
           "mv_on": True, "out": {"kind": "holding", "address": 70 + i}, **over.get("zone", {})} for i, c in enumerate(CENTERS)]
    c = {"id": "juice", "url": "x", "frame_size": [W, H], "proc_fps": 2, "zones": zs, "clarity_refs": "own"}
    return me.normalize_camera(c)


class Scene:
    """Кадры: фон-текстура (неподвижный), круги цвета BASE, шум сжатия; moving — номера кругов, где крупные пятна ползают."""

    def __init__(self, moving=(), noise=4, seed=3):
        self.moving, self.noise = set(moving), noise
        self.rng = np.random.default_rng(seed)
        self.tex = cv2.GaussianBlur(self.rng.normal(0, 18, (H, W)).astype(np.float32), (0, 0), 3)
        self.blob = {i: self.rng.uniform(-14, 14, 2) for i in self.moving}

    def frame(self):
        img = np.full((H, W, 3), (20, 22, 24), np.float32)
        for (cx, cy) in CENTERS:
            cv2.circle(img, (cx, cy), 30, BASE, -1)
        img += self.tex[:, :, None]
        for i in self.moving:
            self.blob[i] = np.clip(self.blob[i] + self.rng.normal(0, 7, 2), -22, 22)      # блуждающее пятно — рябь на поверхности
            cx, cy = CENTERS[i]
            blob = np.zeros((H, W), np.float32)
            cv2.circle(blob, (int(cx + self.blob[i][0]), int(cy + self.blob[i][1])), 9, 1.0, -1)
            img += 55 * cv2.GaussianBlur(blob, (0, 0), 3)[:, :, None]
        img += self.rng.normal(0, self.noise, img.shape)
        return np.clip(img, 0, 255).astype(np.uint8)


def feed(eng, scene, seconds, t0=0.0, dt=0.5, dup=0):
    """Кадры подряд; dup — сколько дублей (тот же кадр) на каждый новый: как у камеры, что отдаёт новый кадр раз в 2 с."""
    st = None
    t = t0
    n = int(seconds / dt)
    img = None
    for k in range(n):
        if img is None or dup == 0 or k % (dup + 1) == 0:
            img = scene.frame()
        st = eng.process(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), t, img) or st
        t += dt
    return st, t


def z(st, zid):
    return next(q for q in st["zones"] if q["id"] == zid)


def test_defaults_and_validation():
    c = cfg_for(zone={"mv_on": False})["zones"][0]
    assert c["mv_on"] is False and (c["mv_hi"], c["mv_lo"]) == (me.CLARITY_MV_HI, me.CLARITY_MV_LO)
    assert (c["mv_win_s"], c["mv_smooth_s"], c["mv_hold_s"]) == (me.CLARITY_MV_WIN_S, me.CLARITY_MV_SMOOTH_S, me.CLARITY_MV_HOLD_S)
    with pytest.raises(ValueError):
        cfg_for(zone={"mv_hi": 1.0, "mv_lo": 1.0})


def test_empty_vs_juice():
    eng = me.CameraEngine(cfg_for())
    st, _ = feed(eng, Scene(moving=(1, 3)), 70)
    assert [z(st, "g%d" % i)["juice"] for i in (1, 2, 3, 4)] == [0, 1, 0, 1]
    assert z(st, "g1")["empty"] and not z(st, "g2")["empty"]
    assert z(st, "g1")["move"] < 0.6 < 1.5 < z(st, "g2")["move"]


def test_duplicates_do_not_count_as_motion():
    """Камера отдаёт новый кадр раз в 4 шага: дубли пропускаются, пустой стакан пустым и остаётся, а движущийся — движется."""
    eng = me.CameraEngine(cfg_for())
    st, _ = feed(eng, Scene(moving=(1,)), 110, dup=3)
    assert z(st, "g1")["empty"] and z(st, "g2")["juice"] == 1


def test_empty_gates_turbidity_and_plc_writes_zero():
    eng = me.CameraEngine(cfg_for())
    st, _ = feed(eng, Scene(moving=(1,)), 70)
    a = z(st, "g1")
    assert a["empty"] and a["turb"] == 0.0 and a["flow"] == 0.0 and a["valid"] is False and a["bit"] == 0 and a["level_name"] == "empty"
    w = dict((addr, v) for _, addr, v in eng.compute_writes(st))
    assert w[70] == 0                                   # пустой стакан: 0 (решение Макса), а не 65535 «нет данных»
    assert z(st, "g2")["diff"] == z(st, "g2")["move"] > 1.0     # в историю (diff) уходит движение


def test_off_by_default_keeps_old_behaviour():
    eng = me.CameraEngine(cfg_for(zone={"mv_on": False}))
    st, _ = feed(eng, Scene(), 40)
    a = z(st, "g1")
    assert a["juice"] is None and a["empty"] is False and a["move"] == 0.0
    assert dict((addr, v) for _, addr, v in eng.compute_writes(st))[70] == me.CLARITY_NO_DATA     # нет эталонов — по-прежнему 65535


def test_hold_delays_change_and_color_restarts_when_juice_returns():
    eng = me.CameraEngine(cfg_for(zone={"mv_hold_s": 30}))
    st, t = feed(eng, Scene(), 45)                      # пусто: первое значение берём сразу
    assert z(st, "g2")["empty"]
    st, t = feed(eng, Scene(moving=(1,)), 20, t0=t)     # сок вернулся, но держится меньше 30 с — ещё «пусто»
    assert z(st, "g2")["empty"]
    st, t = feed(eng, Scene(moving=(1,)), 60, t0=t)
    assert z(st, "g2")["juice"] == 1 and not z(st, "g2")["empty"]


def test_move_sample_gives_noise_floor_for_empty_button():
    eng = me.CameraEngine(cfg_for())
    feed(eng, Scene(), 70)
    r = eng.clarity_sample([], 20)["g1"]["move"]
    assert 0 < r["median"] <= r["floor"] < 0.6 and r["n"] >= 3
    eng2 = me.CameraEngine(cfg_for(zone={"mv_on": False}))
    feed(eng2, Scene(), 30)
    assert eng2.clarity_sample([], 20)["g1"]["move"] is None        # определение выключено — ключа нет, цвет снимается как раньше


def test_reset_forgets_motion():
    eng = me.CameraEngine(cfg_for())
    feed(eng, Scene(), 70)
    eng.reset()
    st, _ = feed(eng, Scene(), 4, t0=500.0)
    assert z(st, "g1")["juice"] is None and z(st, "g1")["move"] == 0.0      # после потери кадра состояние снова неизвестно


def test_unknown_state_is_no_data_not_turbidity_of_sediment():
    """Пока движение не посчитано (старт сервиса), мутность по цвету осадка не выдаём и в ПЛК не пишем: 65535 «нет данных», а не число."""
    eng = me.CameraEngine(cfg_for())
    st, _ = feed(eng, Scene(), 6)
    a = z(st, "g1")
    assert a["juice"] is None and a["valid"] is False and not a["empty"] and "считаю движение" in a["reason"]
    assert dict((addr, v) for _, addr, v in eng.compute_writes(st))[70] == me.CLARITY_NO_DATA


def test_juice_base_rows_mark_empty():
    import juice_base as jb
    eng = me.CameraEngine(cfg_for())
    st, _ = feed(eng, Scene(moving=(1,)), 70)
    rows = {r[0]: r for r in jb.rows_for(st, eng.cfg["zones"])}
    assert rows["g1"][6] == "empty" and rows["g1"][5] == "" and rows["g2"][6] != "empty"
