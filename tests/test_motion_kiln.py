"""Этапы печи: счётчик этапов, конфиг, движок на кадрах, запись кода этапа сценарием."""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "motion_service"))

import motion_engine as me  # noqa: E402
import motion_kiln as mk  # noqa: E402
from motion_scenarios import evaluate, source_bits  # noqa: E402

FULL, EMPTY = 1.0, 0.75
DT = 0.5


class Sim:
    """Прокручивает счётчик по сценарию: run(секунды, движется, вид окна) и запоминает смены этапов."""

    def __init__(self, **kw):
        self.k = mk.KilnRT(kw.get("drop", 12), kw.get("load", 10), kw.get("rot_wait", 60), mk.DEFAULT_CODES)
        self.t, self.seen, self.codes = 1000.0, ["wait_drop"], []

    def run(self, seconds, moving, look=FULL):
        for _ in range(int(round(seconds / DT))):
            self.codes.append(self.k.step(self.t, moving, {"z1": look, "z2": look}))
            if self.k.stage != self.seen[-1]:
                self.seen.append(self.k.stage)
            self.t += DT

    def cycle(self, rot=30):
        self.run(5, True)                       # сброс: движение, вид ещё «полный» (сглажен)
        self.run(3, True, EMPTY)                # потемнело
        self.run(12, False, EMPTY)
        self.run(7, True, EMPTY)                # засыпка
        self.run(2, False)
        if rot:
            self.run(rot, True)


def test_full_cycle_in_order_and_codes():
    s = Sim()
    s.run(60, False)
    s.cycle()
    s.run(30, False)
    assert s.seen == ["wait_drop", "drop", "wait_load", "load", "wait_rot", "rot", "wait_drop"]
    changes = [c for i, c in enumerate(s.codes) if i == 0 or c != s.codes[i - 1]]
    assert changes == [0, 1, 0, 2, 0, 3, 0]
    s.run(150, False)
    s.cycle()
    s.run(10, False)
    assert s.seen.count("drop") == 2 and s.k.cycles == 2


def test_motion_without_darkening_is_ignored():
    s = Sim()
    s.run(60, False)
    s.run(6, True)                              # свет, человек: движение есть, зона не темнеет
    s.run(60, False)
    s.run(60, True)
    s.run(30, False)
    assert s.seen == ["wait_drop"]


def test_slow_darkening_after_motion_end_still_counts():
    s = Sim()
    s.run(60, False)
    s.run(8, True)
    s.run(5, False)                             # сглаженная яркость ещё не упала
    s.run(4, False, EMPTY)
    assert s.seen[:3] == ["wait_drop", "drop", "wait_load"]


def test_darkening_compared_with_level_before_motion_not_absolute():
    s = Sim()
    s.run(60, False, 0.80)                      # в тени полная зона темнее, чем пустая на солнце
    s.run(6, True, 0.80)
    s.run(3, True, 0.66)
    assert "drop" in s.seen


def test_load_runs_into_rotation_without_pause():
    s = Sim()
    s.run(60, False)
    s.run(8, True, EMPTY)
    s.run(12, False, EMPTY)
    s.run(40, True)                             # засыпка и сразу прокрутка
    s.run(5, False)
    assert s.seen == ["wait_drop", "drop", "wait_load", "load", "rot", "wait_drop"]


def test_cycle_without_rotation_returns_to_waiting_drop():
    s = Sim()
    s.run(60, False)
    s.cycle(rot=0)
    s.run(70, False)
    assert s.seen[-2:] == ["wait_rot", "wait_drop"]
    s.cycle()
    assert s.seen.count("drop") == 2


def test_missed_loading_times_out_and_resyncs():
    s = Sim()
    s.run(60, False)
    s.run(8, True, EMPTY)
    s.run(mk.WAIT_LOAD_MAX_S + 5, False, EMPTY)
    assert s.k.stage == "wait_drop"


def test_start_in_the_middle_waits_for_next_drop():
    s = Sim()
    s.run(20, False, EMPTY)                     # запустились на пустой воронке
    s.run(7, True, EMPTY)
    s.run(30, True)                             # засыпка и прокрутка — вид только светлеет
    s.run(100, False)
    assert s.seen == ["wait_drop"]
    s.cycle()
    assert "drop" in s.seen


def test_drop_is_held_long_enough_for_plc():
    s = Sim()
    s.run(60, False)
    s.run(4, True)
    s.run(1, False, EMPTY)
    assert s.k.stage == "drop"
    s.run(mk.SHOW_MIN_S, False, EMPTY)
    assert s.k.stage == "wait_load"


def test_short_rotation_is_held_for_plc():
    s = Sim()
    s.run(60, False)
    s.cycle(rot=0)
    s.run(5, False)
    s.run(0.5, True)                            # прокрутку видно всего один кадр
    s.run(0.5, False)
    assert s.k.stage == "rot"
    s.run(mk.SHOW_MIN_S, False)
    assert s.k.stage == "wait_drop"


def test_candidate_does_not_survive_gap_in_frames():
    s = Sim()
    s.run(60, False)
    s.run(4, True)                              # движение, и тут камера застыла
    s.t += mk.GAP_S + 5
    s.run(20, False, EMPTY)                     # после разрыва свет другой — это не сброс
    assert s.seen == ["wait_drop"]


# ─── конфиг ───
ZONE = {"id": "z1", "name": "воронка", "metric": "diff", "on": 8, "off": 4, "fill_metric": "rel",
        "points": [[80, 80], [240, 80], [240, 180], [80, 180]]}


def cam(group=None, zone=None, **kw):
    return {"id": "c1", "url": "x", "zones": [dict(ZONE, **(zone or {}))],
            "groups": [dict({"id": "k", "name": "Печь", "type": "kiln", "zones": ["z1"]}, **(group or {}))], **kw}


def test_config_defaults_and_own_codes():
    g = me.normalize_camera(cam())["groups"][0]
    assert (g["drop_pct"], g["load_max_s"], g["rot_wait_s"]) == (12, 10, 60)
    assert g["codes"] == mk.DEFAULT_CODES
    g = me.normalize_camera(cam({"codes": {"wait_drop": 10, "wait_load": 20, "wait_rot": 30}}))["groups"][0]
    assert [g["codes"][s] for s in mk.STAGES] == [10, 1, 20, 2, 30, 3]
    assert me.normalize_camera(me.normalize_camera(cam()))["groups"][0]["codes"] == mk.DEFAULT_CODES


@pytest.mark.parametrize("zone", [{"fill_metric": "off"}, {"motion": False}, {"type": "presence"}, {"enabled": False}])
def test_config_rejects_unsuitable_zones(zone):
    with pytest.raises(ValueError):
        me.normalize_camera(cam(zone=zone))


def test_config_rejects_bad_codes():
    with pytest.raises(ValueError):
        me.normalize_camera(cam({"codes": [1, 2, 3]}))


def test_scenario_with_kiln_refuses_coil():
    with pytest.raises(ValueError, match="только в регистр"):
        me.normalize_config({"cameras": [cam()], "scenarios": [
            {"id": "s1", "sources": [{"camera": "c1", "group": "k"}], "out": {"kind": "coil", "address": 5}}]})


def test_scenario_with_kiln_passes_code_through():
    cfg = me.normalize_config({"cameras": [cam(), ], "scenarios": [
        {"id": "s1", "rule": "or", "sources": [{"camera": "c1", "zone": "z1"}, {"camera": "c1", "group": "k"}],
         "out": {"kind": "holding", "address": 700}}]})
    sc = cfg["scenarios"][0]
    assert sc["rule"] == "sum" and sc["sources"] == [{"camera": "c1", "group": "k"}]
    alive = {"c1": {"result": {"alive": True, "groups": [{"id": "k", "value": 3}], "zones": []}}}
    assert evaluate(sc["rule"], source_bits(sc["sources"], alive)) == 3
    alive["c1"]["result"]["alive"] = False
    assert evaluate(sc["rule"], source_bits(sc["sources"], alive)) == 0


# ─── движок на кадрах ───
def picture(rng, stones: bool, shift: int = 0):
    """Серый металл; в зоне — светлые «камни» (или пусто). shift перемешивает камни: так выглядит движение."""
    img = np.full((240, 320), 100, np.uint8)
    if stones:
        r = np.random.default_rng(shift)
        for _ in range(60):
            x, y = int(r.integers(85, 225)), int(r.integers(85, 165))
            img[y:y + 14, x:x + 14] = int(r.integers(160, 230))
    return np.clip(img.astype(int) + rng.integers(-1, 2, img.shape), 0, 255).astype(np.uint8)


def test_engine_walks_through_stages_on_frames():
    conf = me.normalize_camera(cam(zone={"smooth": 2, "fill_smooth": 3, "frames_to_on": 2, "frames_to_off": 2}, proc_fps=2))
    eng, rng = me.CameraEngine(conf), np.random.default_rng(0)
    t, seen, n = 5000.0, [], 0

    def play(seconds, stones, moving):
        nonlocal t, n
        for _ in range(int(seconds / DT)):
            n += 1
            st = eng.process(picture(rng, stones, n if moving else 7), t)
            t += DT
            if st:
                g = st["groups"][0]
                if not seen or seen[-1] != g["stage"]:
                    seen.append(g["stage"])

    play(20, True, False)
    play(5, True, True)
    play(4, False, True)                        # камни уходят
    play(12, False, False)
    play(6, True, True)                         # засыпка
    play(3, True, False)
    play(20, True, True)                        # прокрутка
    play(10, True, False)
    assert seen == ["wait_drop", "drop", "wait_load", "load", "wait_rot", "rot", "wait_drop"]
    st = eng.dead_state(t, "картинка застыла")
    assert st["groups"][0]["value"] == 0 and st["groups"][0]["stage"] is None and st["groups"][0]["bits"] == [0, 0, 0]
    eng.reset()
    assert eng.kilns["k"].stage == "wait_drop" and eng.kilns["k"].cycles == 0
