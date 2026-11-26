"""Этапы печи по звуку (motion_service/motion_sound.py): детектор, конфиг, сценарий без картинки и настоящий ffmpeg из файла."""
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "motion_service"))

import motion_engine as me  # noqa: E402
import motion_sound as ms  # noqa: E402
import motion_videorec as vr  # noqa: E402
from motion_scenarios import evaluate, source_bits  # noqa: E402

FFMPEG = vr.find_ffmpeg()
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="нет ffmpeg (задайте MOTION_FFMPEG)")
QUIET, STONES = -40.0, -2.0          # уровни блока, дБ: фон и камни (на 38 дБ громче)


def wait_for(cond, timeout=40.0, step=0.2):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(step)
    return False


class Sim:
    """Подаёт детектору уровни по 0,1 с и запоминает смены этапов и коды."""

    def __init__(self, thr=30):
        self.d, self.t, self.seen = ms.SoundDetector(thr), 1000.0, []

    def run(self, seconds, db=QUIET, jitter=1.0, rng=np.random.default_rng(1)):
        for _ in range(int(round(seconds * 10))):
            s = self.d.step(self.t, db + float(rng.uniform(-jitter, jitter)))
            if not self.seen or self.seen[-1] != s:
                self.seen.append(s)
            self.t += 0.1

    def cycle(self):
        self.run(3, STONES)                     # сброс
        self.run(15)
        self.run(7, STONES + 4)                 # засыпка
        self.run(200)


def test_listens_then_drop_and_load():
    s = Sim()
    s.run(20)
    assert s.seen == ["listen"]
    s.run(40)
    for _ in range(3):
        s.cycle()
    assert s.seen == ["listen", "wait"] + ["drop", "wait", "load", "wait"] * 3
    assert (s.d.drops, s.d.loads) == (3, 3)


def test_detects_within_a_second_and_holds_for_plc():
    s = Sim()
    s.run(60)
    s.run(0.9, STONES)
    assert s.d.stage == "wait"                  # короче секунды — ещё не событие
    s.run(0.6, STONES)
    assert s.d.stage == "drop"
    seen_at = s.t
    while s.d.stage == "drop":
        s.run(0.1)
    assert s.t - seen_at >= ms.HOLD_S - 0.7


def test_weaker_sound_is_ignored():
    s = Sim()
    s.run(60)
    for _ in range(5):
        s.run(4, QUIET + 24)                    # соседняя печь, удар вдали: тише порога
        s.run(20)
    assert (s.d.drops, s.d.loads) == (0, 0) and s.d.stage == "wait"
    s.d.threshold = 20                          # порог меняется на ходу
    s.run(4, QUIET + 24)
    assert s.d.drops == 1


def test_short_knock_is_ignored():
    s = Sim()
    s.run(60)
    for _ in range(6):
        s.run(0.4, STONES)
        s.run(10)
    assert (s.d.drops, s.d.loads) == (0, 0)


def test_second_event_too_late_is_new_drop():
    s = Sim()
    s.run(60)
    s.run(3, STONES)
    s.run(ms.PAIR_S[1] + 10)
    s.run(3, STONES)
    s.run(5)
    assert (s.d.drops, s.d.loads) == (2, 0)


def test_long_noise_drops_the_code_until_quiet():
    s = Sim()
    s.run(60)
    s.run(ms.LONG_S + 20, STONES)               # дождь, пар, техника
    assert s.d.stage == "noise"
    s.run(10)
    assert s.d.stage == "wait" and (s.d.drops, s.d.loads) == (1, 0)
    s.run(60)
    s.cycle()
    assert (s.d.drops, s.d.loads) == (2, 1)


def test_reset_forgets_pair_but_keeps_background():
    s = Sim()
    s.run(60)
    s.run(3, STONES)
    s.d.reset()                                 # звук оборвался и вернулся
    s.run(15)
    s.run(5, STONES)
    assert s.d.stage == "drop" and s.d.loads == 0 and "listen" not in s.seen[1:]


def test_clock_set_back_does_not_freeze_the_code():
    s = Sim()
    s.run(60)
    s.run(2, STONES)
    assert s.d.stage == "drop"
    s.t -= 3600                                 # часы ПК перевели на час назад
    s.run(5)
    assert s.d.stage == "wait"
    s.cycle()
    assert (s.d.drops, s.d.loads) == (2, 1)


def test_digital_silence_does_not_become_background():
    s = Sim()
    s.run(20, -120.0, jitter=0.0)               # микрофон выключен: камера шлёт нули
    s.run(60)
    assert (s.d.drops, s.d.loads) == (0, 0) and s.d.stage == "wait"
    s.cycle()
    assert (s.d.drops, s.d.loads) == (1, 1)


def test_level_prefers_high_band():
    t = np.arange(ms.BLOCK) / ms.SR
    low, high = np.sin(2 * np.pi * 300 * t).astype(np.float32) * 0.5, np.sin(2 * np.pi * 4000 * t).astype(np.float32) * 0.5
    assert ms.level_db(high) - ms.level_db(low) > 40


def test_cmd_takes_only_audio():
    cmd = ms.build_cmd("ffmpeg", "rtsp://u:p@h/x")
    assert cmd[cmd.index("-allowed_media_types") + 1] == "audio" and "-re" not in cmd and cmd[-1] == "pipe:1"


# ─── конфиг и сценарий ───
def cam(group=None, **kw):
    return {"id": "c1", "url": "x", "groups": [dict({"id": "s", "name": "Звук", "type": "kiln_sound"}, **(group or {}))], **kw}


def test_config_without_zones_and_defaults():
    g = me.normalize_camera(cam())["groups"][0]
    assert g["zones"] == [] and g["out"] is None and g["threshold_db"] == 30 and g["codes"] == ms.DEFAULT_CODES
    g = me.normalize_camera(cam({"threshold_db": 25, "codes": {"wait": 10}, "zones": ["нет"], "out": {"kind": "holding", "address": 1}}))["groups"][0]
    assert (g["threshold_db"], g["codes"]["wait"], g["codes"]["drop"], g["zones"], g["out"]) == (25, 10, 1, [], None)
    assert me.normalize_camera(me.normalize_camera(cam()))["groups"][0]["codes"] == ms.DEFAULT_CODES


@pytest.mark.parametrize("bad", [{"threshold_db": 5}, {"threshold_db": 90}, {"codes": [1]}, {"codes": {"drop": 70000}}])
def test_config_rejects(bad):
    with pytest.raises(ValueError):
        me.normalize_camera(cam(bad))


def test_only_one_per_camera():
    c = cam()
    c["groups"].append({"id": "s2", "name": "ещё", "type": "kiln_sound"})
    with pytest.raises(ValueError, match="только одно"):
        me.normalize_camera(c)


def test_scenario_takes_code_even_with_dead_picture():
    cfg = me.normalize_config({"cameras": [cam()], "scenarios": [
        {"id": "s1", "rule": "or", "sources": [{"camera": "c1", "group": "s"}], "out": {"kind": "holding", "address": 701}}]})
    sc = cfg["scenarios"][0]
    assert sc["rule"] == "sum"

    def value(alive, **g):
        states = {"c1": {"result": {"alive": alive, "zones": [], "groups": [dict({"id": "s", "value": 2}, **g)]}}}
        return evaluate(sc["rule"], source_bits(sc["sources"], states))

    assert value(False, self_alive=True) == 2          # картинки нет, звук есть
    assert value(False, self_alive=False) == 0
    assert value(False) == 0                           # обычная группа мёртвой камеры — по-прежнему 0
    with pytest.raises(ValueError, match="только в регистр"):
        me.normalize_config({"cameras": [cam()], "scenarios": [{"id": "s1", "sources": [{"camera": "c1", "group": "s"}],
                                                                "out": {"kind": "coil", "address": 1}}]})


def test_engine_leaves_sound_group_to_the_listener():
    eng = me.CameraEngine(me.normalize_camera(cam()))
    st = eng.dead_state(100.0, "нет кадров")
    assert st["groups"] == [] and eng.compute_writes(st) == []


def test_group_state_codes():
    g = me.normalize_camera(cam({"codes": {"wait": 10, "drop": 11, "load": 12}}))["groups"][0]
    none = ms.group_state(g, None, 100.0, "нет ffmpeg")
    assert none["value"] == 0 and not none["self_alive"] and none["error"] == "нет ffmpeg"

    class Fake:
        def __init__(self, stage):
            self.stage = stage

        def snapshot(self, now):
            return {"stage": self.stage, "stage_name": "", "since_s": 0, "level": 0, "series": [], "drops": 0, "loads": 0, "drop_ago_s": None, "error": ""}

    got = {s: ms.group_state(g, Fake(s), 1.0) for s in ("listen", "wait", "drop", "load", "noise")}
    assert [got[s]["value"] for s in got] == [10, 10, 11, 12, 10] and all(x["self_alive"] for x in got.values())
    assert got["drop"]["bits"] == [1, 0] and got["load"]["bits"] == [0, 1]
    assert ms.group_state(g, Fake(None), 1.0)["value"] == 0


def test_hub_follows_config(monkeypatch):
    cams = [me.normalize_camera(cam())]
    started = []
    monkeypatch.setattr(ms.Listener, "start", lambda self: started.append((self.cid, self.detector.threshold)))
    monkeypatch.setattr(ms.Listener, "join", lambda self, timeout=None: None)
    monkeypatch.setattr(ms.Listener, "is_alive", lambda self: True)
    hub = ms.SoundHub(lambda: cams, ffmpeg=lambda: "ffmpeg")
    hub.sync()
    first = hub.listeners["c1"]
    cams[0]["groups"][0]["threshold_db"] = 22
    hub.sync()
    assert hub.listeners["c1"] is first and first.detector.threshold == 22 and started == [("c1", 30.0)]       # порог — без переподключения
    cams[0]["enabled"] = False
    hub.sync()
    assert not hub.listeners and "КЗ" in hub.states(cams[0])[0]["error"]
    assert hub.states({"id": "c2", "groups": []}) == []
    no = ms.SoundHub(lambda: [me.normalize_camera(cam())], ffmpeg=lambda: None)
    no.sync()
    assert not no.listeners and "ffmpeg" in no.states(me.normalize_camera(cam()))[0]["error"]


# ─── настоящий ffmpeg и сервис целиком ───
def make_kiln_sound(path: Path, seconds=75):
    """Тихий шум, на 40-й секунде 3 с «камней» (широкая полоса), на 58-й ещё 6 с."""
    rng = np.random.default_rng(0)
    x = rng.normal(0, 0.002, seconds * ms.SR)
    for a, dur in ((40, 3), (58, 6)):
        x[a * ms.SR:(a + dur) * ms.SR] += rng.normal(0, 0.2, dur * ms.SR)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(ms.SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


@needs_ffmpeg
def test_service_hears_stages_from_file(tmp_path):
    """Сервис целиком: звук из файла идёт в реальном времени, «камера» картинки не даёт, а этапы по звуку считаются."""
    import server as srv
    src = tmp_path / "kiln.wav"
    make_kiln_sound(src)
    mgr = srv.Manager(tmp_path / "data")
    mgr.start()
    try:
        mgr.apply({"cameras": [cam(url=str(src), enabled=True)], "scenarios": [
            {"id": "s1", "sources": [{"camera": "c1", "group": "s"}], "out": {"kind": "holding", "address": 701}}]})
        seen = []

        def look():
            g = next(g for g in mgr.workers["c1"].state()["result"]["groups"] if g["type"] == "kiln_sound")
            mgr.scenarios.tick()
            v = mgr.scenarios.status["s1"]["value"]
            if g["stage"] and (not seen or seen[-1] != (g["stage"], v)):
                seen.append((g["stage"], v))
            return ("load", 2) in seen

        assert wait_for(look, 90, 0.5), (seen, mgr.workers["c1"].state()["result"])
        assert seen[:5] == [("listen", 0), ("wait", 0), ("drop", 1), ("wait", 0), ("load", 2)], seen
    finally:
        mgr.shutdown()
    assert not mgr.sound.listeners
