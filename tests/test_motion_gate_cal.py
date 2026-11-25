"""Подгонка линейки шибера кнопками «это 0 %» / «это 100 %» (page/static/js/multi/19-gauge.js, вид «стрелка»).

Расчёт живёт в странице, а проценты считает сервис — поэтому JS гоняется настоящим node, а результат проверяется боевым ruler_level.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "motion_service"))

import motion_engine as me  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))
import test_motion_gate as tg  # noqa: E402  синтетический кадр шибера: линейка с метками и полоска-указатель

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="нет node")
JS = ROOT / "page" / "static" / "js" / "multi" / "19-gauge.js"
# разметка с настоящего ролика: [низ слева, низ справа, верх справа, верх слева]; стороны наклонены и чуть сходятся
RULER = [[918, 787], [990, 786], [1046, 136], [975, 131]]


def js(expr, data):
    code = ("const vm=require('vm'),fs=require('fs');const c={data:%s};vm.createContext(c);"
            "vm.runInContext(fs.readFileSync(%s,'utf8'),c);process.stdout.write(JSON.stringify(vm.runInContext(%s,c)));"
            % (json.dumps(data), json.dumps(str(JS)), json.dumps(expr)))
    return json.loads(subprocess.run([NODE, "-e", code], capture_output=True, check=True, encoding="utf-8").stdout)


def point(level, x=1100.0, ruler=RULER):
    """Точка справа от линейки на высоте level (доля линейки; за краями — продолжение сторон)."""
    bl, br, tr, tl = ruler
    a = [bl[i] + level * (tl[i] - bl[i]) for i in range(2)]
    b = [br[i] + level * (tr[i] - br[i]) for i in range(2)]
    k = (x - a[0]) / (b[0] - a[0])
    return [x, a[1] + k * (b[1] - a[1])]


def cal(pt, full, flip=False, ruler=RULER):
    return js("ggCalibrate(data.z, data.pt, data.full)", {"z": {"ruler": ruler, "flip": flip}, "pt": pt, "full": full})


def pct(ruler, pt, flip=False):
    t = me.ruler_level(ruler, pt)
    return 100.0 * (1.0 - t if flip else t)


def test_level_matches_service_and_goes_beyond_edges():
    for level in (0.04, 0.3, 0.77, 0.99):
        got = js("ggLevelOf(data.r, data.p)", {"r": RULER, "p": point(level)})
        assert got == pytest.approx(me.ruler_level(RULER, point(level)), abs=1e-4)
    for level in (-0.2, 1.15):                                  # за краем сервис зажимает в 0..1, странице нужна настоящая высота
        assert js("ggLevelOf(data.r, data.p)", {"r": RULER, "p": point(level)}) == pytest.approx(level, abs=1e-4)
    assert js("ggLevelOf(data.r, data.p)", {"r": RULER, "p": point(-0.4)}) is None


@pytest.mark.parametrize("full,level", [(False, 0.249), (True, 0.751)])
def test_biggest_mistake_is_undone_by_same_button(full, level):
    wrong = cal(point(level), full)["ruler"]                     # нажали не в крайнем положении
    right = cal(point(1.0 if full else 0.0), full, ruler=wrong)  # поставили шибер куда надо и нажали снова
    for a, b in zip(right["ruler"], RULER):
        assert a == pytest.approx(b, abs=0.3)


def run_button(states, flip=False, answer=True, dirty=False, full=False, zone=None):
    """ggOnCal целиком: states[0] — что на экране в момент нажатия, states[1] — что ответил сервис после вопроса."""
    zone = dict({"id": "g", "ruler": RULER, "flip": flip, "cut": None}, **(zone or {}))
    zone = {k: v for k, v in zone.items() if v != "нет ключа"}
    code = (
        "(async () => { const out = {alerts: [], asked: [], edits: 0};"
        "const zone = data.zone;"
        "globalThis.MV = {online: true, dirty: data.dirty, states: {c: data.states[0]}};"
        "globalThis.alert = (m) => out.alerts.push(m); globalThis.confirm = (m) => { out.asked.push(m); return data.answer; };"
        "globalThis.mvApi = async () => data.states[1]; globalThis.mvCam = () => ({id: 'c', zones: [zone]});"
        "globalThis.mdEdit = (f) => { f(); out.edits += 1; }; globalThis.log = {success: () => {}};"
        "await ggOnCal({id: 'c', zones: [zone]}, zone, data.full); out.ruler = zone.ruler; out.cut = zone.cut; return out; })()")
    return js_async(code, {"zone": zone, "states": states, "answer": answer, "dirty": dirty, "full": full})


def js_async(code, data):
    wrapper = ("const vm=require('vm'),fs=require('fs');const c={data:%s};vm.createContext(c);"
               "vm.runInContext(fs.readFileSync(%s,'utf8'),c);Promise.resolve(vm.runInContext(%s,c)).then((r)=>process.stdout.write(JSON.stringify(r)));"
               % (json.dumps(data), json.dumps(str(JS)), json.dumps(code)))
    return json.loads(subprocess.run([NODE, "-e", wrapper], capture_output=True, check=True, encoding="utf-8").stdout)


def state(level, t=None, age=0.1, alive=True, **zone):
    z = dict({"id": "g", "valid": True, "held": False, "ptr": point(level), "t": max(0.0, min(1.0, level)) if t is None else t}, **zone)
    return {"age_s": age, "result": {"alive": alive, "zones": [z]}}


def test_button_applies_after_confirm():
    out = run_button([state(0.05), state(0.05)])
    assert out["edits"] == 1 and not out["alerts"] and "5,0 %" in out["asked"][0]
    assert pct(out["ruler"], point(0.05)) == pytest.approx(0.0, abs=0.05)


def test_button_cancel_changes_nothing():
    out = run_button([state(0.05), state(0.05)], answer=False)
    assert out["edits"] == 0 and out["ruler"] == RULER


@pytest.mark.parametrize("now", [state(0.05, age=8), state(0.05, held=True), state(0.05, valid=False, ptr=None), state(0.05, alive=False),
                                 state(0.18, t=0.0), {}])
def test_button_refuses_before_asking(now):
    out = run_button([now, state(0.05)])
    assert out["alerts"] and not out["asked"] and out["edits"] == 0


def test_button_refuses_while_unsaved():
    out = run_button([state(0.05), state(0.05)], dirty=True)
    assert out["alerts"] and not out["asked"] and out["edits"] == 0


@pytest.mark.parametrize("later", [state(0.15), state(0.05, held=True), {"error": "нет связи"}])
def test_button_refuses_if_pointer_changed_while_asking(later):
    out = run_button([state(0.05), later])
    assert out["asked"] and out["alerts"] and out["edits"] == 0 and out["ruler"] == RULER


def test_button_with_flip():
    out = run_button([state(0.95), state(0.95)], flip=True)
    assert out["edits"] == 1 and out["ruler"][:2] == RULER[:2]
    assert pct(out["ruler"], point(0.95), flip=True) == pytest.approx(0.0, abs=0.05)


def test_button_remembers_real_ruler_once():
    first = run_button([state(0.05), state(0.05)])
    assert first["cut"] == RULER and first["ruler"] != RULER
    again = run_button([state(0.05, t=0.0), state(0.05, t=0.0)], zone={"ruler": first["ruler"], "cut": first["cut"]})
    assert again["edits"] == 1 and again["cut"] == RULER          # повторная подгонка не подменяет настоящую линейку шкалой


def test_button_refuses_on_old_service():
    out = run_button([state(0.05), state(0.05)], zone={"cut": "нет ключа"})
    assert "3.0.43" in out["alerts"][0] and not out["asked"] and out["edits"] == 0


@pytest.mark.parametrize("old,expect", [({"cut": RULER}, None), ({"cut": None}, None), ({}, "нет ключа")])
def test_new_marking_drops_remembered_ruler(old, expect):
    code = ("(() => { const zone = Object.assign({id: 'g', type: 'gate'}, data.old); const cam = {id: 'c', frame_size: [1920, 1080], zones: [zone]};"
            "globalThis.mdEdit = (f) => f(); globalThis.log = {success: () => {}}; globalThis.closeMarkModal = () => {};"
            "globalThis.MD = {}; globalThis.renderMotionDock = () => {};"
            "ggMarkSave({gk: 'gate', replace: 'g', ruler: data.ruler, pts: [[1025, 520], [1210, 520], [1210, 800]]}, cam, 1920, 1080); return zone; })()")
    zone = js_async(code, {"old": old, "ruler": RULER})
    assert zone["ruler"] == RULER and zone.get("cut", "нет ключа") == expect


def real_zone(ruler, cut=None, track=None):
    cfg = me.normalize_camera({"id": "sh", "url": "x", "frame_size": [960, 540], "zones": [{
        "id": "g", "type": "gate", "ruler": ruler, "cut": cut, "points": track or tg.TRACK}]})
    return cfg["zones"][0]


@pytest.mark.parametrize("remembered", [False, True])
def test_marks_stay_cut_after_calibration(remembered):
    """Зона указателя заезжает на линейку, линейку укоротили на четверть, указателя на кадре нет."""
    short = [list(p) for p in tg.RULER]
    short[0][1] = short[1][1] = 370
    zone = real_zone(short, cut=tg.RULER if remembered else None)
    rt = me.GateRT(zone, [960, 540], (960, 540))
    found = rt.step(tg.frame(None), 0.0)
    assert found is (not remembered)                              # без cut метка за новой стороной читается как указатель
    if remembered:
        assert not rt.valid and rt.area == 0
        assert rt.step(tg.frame(370), 1.0) and rt.pct == pytest.approx(0.0, abs=1.0)    # сам указатель виден, шкала новая


def test_longer_ruler_does_not_cut_pointer():
    """Линейку удлинили (указатель ниже старой стороны) и он заходит на её ширину: вырез остаётся прежним, пятно целое."""
    long = [list(p) for p in tg.RULER]
    long[0][1] = long[1][1] = 520
    track = [[300, 40], [560, 40], [560, 535], [300, 535]]
    img = tg.frame(None)
    img[488:502, 330:520] = tg.RED                                # ниже настоящей линейки (низ y=470)
    full, cut = (me.GateRT(real_zone(long, cut=c, track=track), [960, 540], (960, 540)) for c in (tg.RULER, None))
    assert full.step(img, 0.0) and cut.step(img, 0.0)
    assert full.area > cut.area


def test_cut_validation():
    assert real_zone(tg.RULER)["cut"] is None
    assert real_zone(tg.RULER, cut=[tg.RULER[2], tg.RULER[0], tg.RULER[3], tg.RULER[1]])["cut"] == real_zone(tg.RULER)["ruler"]
    with pytest.raises(ValueError):
        real_zone(tg.RULER, cut=tg.RULER[:3])


@pytest.mark.parametrize("level", [0.04, -0.06, 0.2])
def test_zero_here(level):
    res = cal(point(level), False)
    assert res["pct"] == pytest.approx(100 * level, abs=0.02)
    assert res["ruler"][2:] == RULER[2:]                        # верх (100 %) не двигается
    assert pct(res["ruler"], point(level)) == pytest.approx(0.0, abs=0.05)
    assert pct(res["ruler"], point(0.5)) == pytest.approx(100 * (0.5 - level) / (1 - level), abs=0.05)
    assert pct(res["ruler"], point(1.0)) == pytest.approx(100.0, abs=0.05)


@pytest.mark.parametrize("level", [0.9, 1.1])
def test_full_here(level):
    res = cal(point(level), True)
    assert res["ruler"][:2] == RULER[:2]                        # низ (0 %) не двигается
    assert pct(res["ruler"], point(level)) == pytest.approx(100.0, abs=0.05)
    assert pct(res["ruler"], point(0.0)) == pytest.approx(0.0, abs=0.05)
    assert pct(res["ruler"], point(0.5)) == pytest.approx(100 * 0.5 / level, abs=0.05)


def test_flip_moves_the_other_side():
    zero = cal(point(0.93), False, flip=True)                   # «0 % сверху»: ноль — верхняя сторона
    assert zero["ruler"][:2] == RULER[:2] and zero["pct"] == pytest.approx(7.0, abs=0.02)
    assert pct(zero["ruler"], point(0.93), flip=True) == pytest.approx(0.0, abs=0.05)
    full = cal(point(0.06), True, flip=True)
    assert full["ruler"][2:] == RULER[2:]
    assert pct(full["ruler"], point(0.06), flip=True) == pytest.approx(100.0, abs=0.05)


def test_repeat_does_not_accumulate():
    first = cal(point(0.05), False)["ruler"]
    again = cal(point(0.05), False, ruler=first)["ruler"]
    for a, b in zip(first, again):
        assert a == pytest.approx(b, abs=0.2)


@pytest.mark.parametrize("level,full", [(0.3, False), (-0.37, False), (0.05, True), (0.7, True), (1.37, True)])
def test_too_far_is_rejected(level, full):
    assert cal(point(level), full) is None


def test_service_accepts_new_ruler():
    res = cal(point(0.05), False)
    cfg = me.normalize_camera({"id": "sh", "url": "x", "frame_size": [1920, 1080], "zones": [{
        "id": "g1", "type": "gate", "ruler": res["ruler"], "points": [[1025, 520], [1210, 520], [1210, 800]]}]})
    assert cfg["zones"][0]["ruler"] == res["ruler"]            # порядок углов и дробные координаты сервис не меняет
