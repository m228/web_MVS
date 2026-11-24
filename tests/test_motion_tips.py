"""Светоотражатели на концах палки (tip_hsv): маска, кольцо поиска, слежение без цвета, разворот на 180°, неподвижные блики, превью.

Синтетика как в test_motion_autocal: бирюзовый фон, палка из красной и синей половин, на концах — белые кончики («пересвет»)
чуть дальше окружности поиска. «Слепой сектор» — половины палки закрашены цветом фона, видны только кончики."""
import math
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

MS = Path(__file__).resolve().parent.parent / "motion_service"
sys.path.insert(0, str(MS))

import motion_engine as me  # noqa: E402

W, H = 800, 600
CX, CY, R = 400, 300, 170
WHITE_BOX = [0, 179, 0, 60, 235, 255]


def hsv_bgr(h, s, v):
    return tuple(int(x) for x in cv2.cvtColor(np.uint8([[[h, s, v]]]), cv2.COLOR_HSV2BGR)[0, 0])


BG, BLUE, RED = hsv_bgr(88, 150, 150), hsv_bgr(100, 210, 210), hsv_bgr(176, 220, 210)


def frame(angle, seed=0, colour=True, tips=True, static=False, coff=0.0, ends=(1, -1)):
    """coff — палка по цвету довёрнута на столько градусов от настоящей (кончики — на настоящем месте); ends — какие кончики рисовать: 1 — красный, −1 — синий."""
    rng = np.random.default_rng(seed)
    img = np.full((H, W, 3), BG, np.uint8)
    img = np.clip(img.astype(np.int16) + rng.integers(-10, 10, img.shape), 0, 255).astype(np.uint8)
    if static:                                                           # неподвижный белый блик в кольце кончиков (люк, рамка) на 200°
        a = math.radians(200)
        p = (int(CX + 168 * math.sin(a)), int(CY - 168 * math.cos(a)))
        cv2.line(img, p, (p[0] + 12, p[1] + 3), (255, 255, 255), 6)
    rc = math.radians(angle + coff)
    if colour:
        cdx, cdy = math.sin(rc), -math.cos(rc)
        cv2.line(img, (CX, CY), (int(CX + 150 * cdx), int(CY + 150 * cdy)), RED, 4)
        cv2.line(img, (CX, CY), (int(CX - 150 * cdx), int(CY - 150 * cdy)), BLUE, 4)
    r = math.radians(angle)
    dx, dy = math.sin(r), -math.cos(r)
    if tips:
        for sg in ends:
            cv2.line(img, (int(CX + sg * 152 * dx), int(CY + sg * 152 * dy)), (int(CX + sg * 176 * dx), int(CY + sg * 176 * dy)), (255, 255, 255), 6)
    return img


def zone(**kw):
    return me.normalize_camera({"id": "c", "url": "x", "frame_size": [W, H], "zones": [dict({"id": "r", "type": "cycle", "center": [CX, CY], "radius": R}, **kw)]})["zones"][0]


def rt(**kw):
    return me.CycleRT(zone(**kw), [W, H], (W, H))


def adiff(a, b):
    return abs((a - b + 180.0) % 360.0 - 180.0)


def test_tip_config_normalize_and_defaults():
    z = zone()
    assert z["tip_hsv"] == [] and z["tip_w"] == 1.0 and z["tip_r1"] >= 1.0
    z = zone(tip_hsv=[WHITE_BOX, [20, 38, 60, 255, 200, 255]], tip_w=0.5)
    assert z["tip_hsv"] == [WHITE_BOX, [20, 38, 60, 255, 200, 255]] and z["tip_w"] == 0.5
    with pytest.raises(ValueError):
        zone(tip_hsv=[[0, 179, 0, 60]])                                  # нужен «ящик» из шести чисел
    with pytest.raises(ValueError):
        zone(tip_hsv=[[0, 179, 90, 60, 235, 255]])                       # «от» насыщенности выше «до»
    assert not rt().tip_on and rt(tip_hsv=[WHITE_BOX]).tip_on and not rt(tip_hsv=[WHITE_BOX], tip_w=0).tip_on


def test_tip_mask_sees_tips_beyond_radius_and_not_rod():
    z = rt(tip_hsv=[WHITE_BOX])
    f = frame(60, colour=True, tips=True)
    st = z._tip_profile(z._tipmask(f[z.ty0:z.ty1, z.tx0:z.tx1]))
    assert adiff(float(np.argmax(st)), 60) <= 2 or adiff(float(np.argmax(st)), 240) <= 2
    assert st[60] >= 0.8 and st[240] >= 0.8                              # оба кончика, хоть и за окружностью поиска (152..176 при R = 170)
    assert st[150] < 0.05 and st[330] < 0.05
    st0 = z._tip_profile(z._tipmask(frame(60, tips=False)[z.ty0:z.ty1, z.tx0:z.tx1]))
    assert st0.max() < 0.05                                              # цветная палка без кончиков — не отражатель


def test_tips_hold_rod_through_colour_blind_sector():
    """Половины палки пропадают на 60–150° (слепой сектор): без отражателей — удержание по скорости и потеря, с ними — палка видна и угол точный."""
    on, off = rt(tip_hsv=[WHITE_BOX]), rt()
    found_on = found_off = 0
    errs = []
    for i in range(220):
        ang = (10 + 4.8 * i * 0.5) % 360                                 # 0,8 об/мин, кадр раз в 0,5 с
        blind = 60 <= ang <= 150
        f = frame(ang, seed=i, colour=not blind)
        a, b = on.step(f, i * 0.5), off.step(f, i * 0.5)
        if blind:
            found_on += a
            found_off += b
            if a:
                errs.append(adiff(on.last, ang))
                assert on.src == "tip"
    n_blind = sum(1 for i in range(220) if 60 <= (10 + 4.8 * i * 0.5) % 360 <= 150)
    assert found_off == 0                                                # без цвета палка не находится (только удержание)
    assert found_on >= 0.95 * n_blind
    assert max(errs) < 2.5


def test_tips_agree_with_colour_and_stay_accurate():
    z = rt(tip_hsv=[WHITE_BOX])
    errs = []
    for i in range(120):
        ang = (200 + 4.8 * i * 0.5) % 360
        assert z.step(frame(ang, seed=i), i * 0.5)
        errs.append(adiff(z.last, ang))
        assert z.lvl_t >= 0.8
    assert np.percentile(errs, 95) < 1.5


def test_flip_corrected_by_colour():
    """Отражатели не знают, где красный конец: стрелку, ведомую ими задом наперёд, цвет разворачивает, когда вал прошёл TIP_FLIP_DEG при голосе «за»."""
    z = rt(tip_hsv=[WHITE_BOX])
    for i in range(10):                                                  # захват честный
        z.step(frame((30 + 2.4 * i) % 360, seed=i), i * 0.5)
    z.last = (z.last + 180.0) % 360.0                                    # подменяем: слежение перевёрнуто
    z.raw.clear()
    turned = None
    for i in range(10, 80):
        ang = (30 + 2.4 * i) % 360
        z.step(frame(ang, seed=i), i * 0.5)
        if adiff(z.last, ang) < 3:
            turned = i
            break
    assert turned is not None, "разворот по цвету не случился"
    assert (turned - 10) * 2.4 >= me.TIP_FLIP_DEG - 5                   # не раньше, чем вал прошёл TIP_FLIP_DEG: один сектор не разворачивает


def test_one_sector_colour_vote_does_not_flip():
    """Цвет «за» разворот только в одном секторе (косой центр, пятно), дальше — «против»: стрелка не разворачивается."""
    z = rt(tip_hsv=[WHITE_BOX])
    for i in range(160):
        ang = (30 + 4.8 * i * 0.5) % 360
        sector = 100 <= ang <= 160                                       # здесь цвета «перевёрнуты»: красная и синяя половины поменялись местами
        f = frame((ang + 180) % 360, seed=i, tips=False) if sector else frame(ang, seed=i, tips=False)
        if sector:                                                       # кончики — на настоящем месте
            r = math.radians(ang)
            for sg in (1, -1):
                cv2.line(f, (int(CX + sg * 152 * math.sin(r)), int(CY - sg * 152 * math.cos(r))), (int(CX + sg * 176 * math.sin(r)), int(CY - sg * 176 * math.cos(r))), (255, 255, 255), 6)
        else:
            f = frame(ang, seed=i)
        z.step(f, i * 0.5)
        if z.valid and not z.coast:
            assert adiff(z.last, ang) < 10, "стрелка развернулась на %.0f° (кадр %d)" % (z.last, i)


def test_static_glare_learned_and_ignored():
    """Неподвижный белый блик в кольце кончиков: пока вал крутится, он уходит в «неподвижные» и не уводит стрелку,
    когда палка проходит мимо и затем пропадает по цвету."""
    z = rt(tip_hsv=[WHITE_BOX])
    for i in range(160):                                                 # 80 с вращения — блик выучен
        z.step(frame((30 + 4.8 * i * 0.5) % 360, seed=i, static=True), i * 0.5)
    assert z.tip_bg is not None and z.tip_bg.max() > me.TIP_BG_MAX
    st = z._tip_profile(z._tip_static(z._tipmask(frame(30, static=True, tips=False)[z.ty0:z.ty1, z.tx0:z.tx1]), 80.0))
    assert st.max() < 0.05                                               # блик из маски убран
    errs = []
    for i in range(160, 260):                                            # палка без цвета (только кончики) — блик на 200° не держит стрелку
        ang = (30 + 4.8 * i * 0.5) % 360
        if z.step(frame(ang, seed=i, static=True, colour=False), i * 0.5):
            errs.append(adiff(z.last, ang))
    assert len(errs) >= 90 and max(errs) < 3


def test_static_map_not_learned_while_stopped():
    z = rt(tip_hsv=[WHITE_BOX])
    for i in range(40):
        z.step(frame(70, seed=i), i * 0.5)                               # вал стоит: кончики на месте
    assert z.moving is False
    m0 = float(z.tip_bg.max())
    for i in range(40, 140):
        z.step(frame(70, seed=i), i * 0.5)
    assert float(z.tip_bg.max()) <= m0 + 1e-6                            # пока стоит — в блики не пишем
    assert z.valid and adiff(z.angle, 70) < 2


def test_no_tips_config_changes_nothing():
    """Без tip_hsv разбор тот же, что по цвету: профиль отражателей не считается, src без «tip», lvl_t = 0."""
    a = rt()
    b = rt(tip_hsv=[WHITE_BOX], tip_w=0)
    for i in range(40):
        f = frame((30 + 2.4 * i) % 360, seed=i)
        assert a.step(f, i * 0.5) == b.step(f, i * 0.5)
        assert a.last == b.last and a.src == b.src and a.lvl_t == 0.0


def test_preview_shows_tips_and_state_has_lvl_t():
    z = rt(tip_hsv=[WHITE_BOX])
    f = frame(45)
    img = z.mask_preview(f, max_w=400)
    yellow = (img[:, :, 0] < 40) & (img[:, :, 1] > 180) & (img[:, :, 2] > 230)
    assert yellow.sum() > 20
    img0 = z.mask_preview(f, max_w=400, tip_hsv=[])                      # пробные «без отражателей»
    assert ((img0[:, :, 0] < 40) & (img0[:, :, 1] > 180) & (img0[:, :, 2] > 230)).sum() == 0
    cam = {"id": "c", "url": "x", "frame_size": [W, H], "zones": [{"id": "r", "type": "cycle", "center": [CX, CY], "radius": R, "tip_hsv": [WHITE_BOX]}]}
    eng = me.CameraEngine(me.normalize_camera(cam))
    for i in range(6):
        g = frame(45 + i, seed=i)
        st = eng.process(cv2.cvtColor(g, cv2.COLOR_BGR2GRAY), i * 0.5, g)
    zs = [x for x in st["zones"] if x["id"] == "r"][0]
    assert zs["valid"] and zs["lvl_t"] >= 0.8


# ---- «вести по отражателям» (tip_lead) и видимость каждого конца ----

def test_tip_lead_config_default_off():
    assert zone()["tip_lead"] is False and zone(tip_lead=True)["tip_lead"] is True


def test_tip_lead_takes_angle_from_tips_not_colour_mean():
    """Палка по цвету довёрнута на 6° (изгиб, косая разметка цвета), кончики на настоящем месте:
    по цвету — среднее и ошибка ~2°, по отражателям — угол кончиков, ошибка почти ноль; цвет по-прежнему виден (src both)."""
    mix, lead = rt(tip_hsv=[WHITE_BOX]), rt(tip_hsv=[WHITE_BOX], tip_lead=True)
    e_mix, e_lead = [], []
    for i in range(120):
        ang = (40 + 4.8 * i * 0.5) % 360
        f = frame(ang, seed=i, coff=6.0)
        assert mix.step(f, i * 0.5) and lead.step(f, i * 0.5)
        e_mix.append(adiff(mix.last, ang))
        e_lead.append(adiff(lead.last, ang))
        assert lead.lead == "tip" and lead.src in ("both", "red", "blue") and mix.lead == "color"
    assert np.mean(e_mix) > 1.2
    assert np.percentile(e_lead, 95) < 1.0


def test_tip_lead_falls_back_to_colour_without_tips():
    z = rt(tip_hsv=[WHITE_BOX], tip_lead=True)
    errs = []
    for i in range(60):
        ang = (40 + 4.8 * i * 0.5) % 360
        assert z.step(frame(ang, seed=i, tips=False), i * 0.5)
        errs.append(adiff(z.last, ang))
        assert z.lead == "color" and z.lvl_tr == 0.0 and z.lvl_tb == 0.0
    assert max(errs) < 3


def test_tip_lead_keeps_direction_from_colour():
    """Отражатели одинаковы на обоих концах: стрелка по ним не переворачивается — красный конец там, где красная половина."""
    z = rt(tip_hsv=[WHITE_BOX], tip_lead=True)
    for i in range(80):
        ang = (250 + 4.8 * i * 0.5) % 360
        assert z.step(frame(ang, seed=i), i * 0.5)
        assert adiff(z.last, ang) < 3


def test_tip_end_visibility_per_end():
    """Виден только кончик красного конца — lvl_tr высокий, lvl_tb около нуля; и наоборот. В состоянии зоны — tip_on, lvl_tr, lvl_tb, lead."""
    z = rt(tip_hsv=[WHITE_BOX])
    for i in range(20):
        z.step(frame((80 + 2.4 * i) % 360, seed=i, ends=(1,)), i * 0.5)
    assert z.lvl_tr >= 0.8 and z.lvl_tb < 0.1
    for i in range(20, 40):
        z.step(frame((80 + 2.4 * i) % 360, seed=i, ends=(-1,)), i * 0.5)
    assert z.lvl_tb >= 0.8 and z.lvl_tr < 0.1
    cam = {"id": "c", "url": "x", "frame_size": [W, H], "zones": [{"id": "r", "type": "cycle", "center": [CX, CY], "radius": R, "tip_hsv": [WHITE_BOX], "tip_lead": True}]}
    eng = me.CameraEngine(me.normalize_camera(cam))
    for i in range(6):
        g = frame(45 + i, seed=i)
        st = eng.process(cv2.cvtColor(g, cv2.COLOR_BGR2GRAY), i * 0.5, g)
    zs = [x for x in st["zones"] if x["id"] == "r"][0]
    assert zs["tip_on"] is True and zs["lvl_tr"] >= 0.8 and zs["lvl_tb"] >= 0.8 and zs["lead"] == "tip"
    eng0 = me.CameraEngine(me.normalize_camera(dict(cam, zones=[{"id": "r", "type": "cycle", "center": [CX, CY], "radius": R}])))
    for i in range(2):                                                   # первый кадр — только запомнить (состояния ещё нет)
        st0 = eng0.process(cv2.cvtColor(g, cv2.COLOR_BGR2GRAY), i * 0.5, g)
    assert [x for x in st0["zones"] if x["id"] == "r"][0]["tip_on"] is False
