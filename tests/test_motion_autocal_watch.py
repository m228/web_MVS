"""Сторож подгонки валов (motion_autocal_watch): когда запускает, применяет, сверяет и откатывает. Менеджер, задание и тренды — заглушки."""
import copy
import json
import sys
from pathlib import Path

import pytest

MS = Path(__file__).resolve().parent.parent / "motion_service"
sys.path.insert(0, str(MS))

import motion_autocal_watch as aw  # noqa: E402
import motion_engine as me  # noqa: E402

OLD = {"center": [100.0, 100.0], "radius": 200.0, "red_hsv": [16, 165, 50, 55], "blue_hsv": [82, 135, 55, 60], "thin": 9, "blue_alone": 0.6}
NEW = {"center": [102.0, 99.0], "radius": 190.0, "red_hsv": [14, 160, 44, 55], "blue_hsv": [84, 140, 85, 66], "thin": 9, "blue_alone": 0.6}
WATCH = {"on": True, "min_pct": 95.0, "check_min": 30.0, "cooldown_h": 2.0}


def zone(zid, **kw):
    z = {"id": zid, "name": "Вал " + zid, "type": "cycle", "enabled": True}
    z.update(copy.deepcopy(OLD))
    z.update(kw)
    return z


class FakeJob:
    def __init__(self, st, started):
        self.st = dict(st, started=started)
        self.cancelled = False

    def status(self):
        return json.loads(json.dumps(self.st))

    def cancel(self):
        self.cancelled = True


class FakeMgr:
    def __init__(self, tmp, watch=None, zones=None):
        self.dir = tmp
        self.cfg = {"cameras": [{"id": "c1", "name": "val_Down", "autocal_watch": dict(watch or WATCH), "zones": zones or [zone("rod1"), zone("rod2")]}]}
        self.autocal = {}
        self.started = []
        self.applied_cfgs = []
        self.now = 0.0

    def autocal_start(self, cid, zones, seconds):
        self.started.append((cid, list(zones), seconds))
        self.autocal[cid] = FakeJob({"state": "recording", "results": {}}, self.now)

    def autocal_apply(self, cid, zones):
        cam = self.cfg["cameras"][0]
        res = self.autocal[cid].st["results"]
        done = []
        for z in cam["zones"]:
            if z["id"] in zones and res[z["id"]]["improved"]:
                z.update(copy.deepcopy(res[z["id"]]["new"]))
                done.append(z["id"])
        return {"applied": done, "skipped": []}

    def apply(self, cfg, save=True):
        self.applied_cfgs.append(cfg)
        self.cfg = cfg
        return cfg


def result(zid, improved=True, found=(0.55, 0.95), old=OLD, new=NEW):
    return {"name": "Вал " + zid, "improved": improved, "old": copy.deepcopy(old), "new": copy.deepcopy(new),
            "before_val": {"found": found[0]}, "after_val": {"found": found[1]}}


class Env:
    """Часы, тренды (видимость задаём по зонам) и сам сторож."""

    def __init__(self, tmp, **kw):
        self.mgr = FakeMgr(tmp, **kw)
        self.t = 1000.0
        self.vis = {"rod1": 100.0, "rod2": 100.0}
        self.standing = False                          # вал сейчас стоит (по скорости)
        self.w = aw.AutocalWatch(self.mgr, clock=lambda: self.t, vis_fn=self._vis, stand_fn=lambda cid: self.standing)
        self.w._started = self.t - 10000.0

    def _vis(self, cid, zids, t0, t1):
        n = int(t1 - t0)
        return {z: (n, int(n * self.vis[z] / 100.0)) for z in zids}

    def tick(self, dt=0.0):
        self.t += dt
        self.mgr.now = self.t
        self.w.tick()

    @property
    def st(self):
        return self.w.st["c1"]

    def kinds(self):
        return [e["kind"] for e in self.w.events]


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def finish_job(env, results, state="done"):
    env.mgr.autocal["c1"].st.update(state=state, results=results)


def test_healthy_no_start(env):
    env.tick()
    assert env.mgr.started == [] and env.kinds() == ["check"] and env.w.recent() == []
    env.tick(60)                                    # следующая проверка только через 30 минут
    assert len(env.w.events) == 1
    env.tick(1800)
    assert len(env.w.events) == 2


def test_warmup_after_service_start(env):
    env.w._started = env.t
    env.vis["rod1"] = 50.0
    env.tick(10)
    assert env.mgr.started == []
    env.tick(100)
    assert env.mgr.started


def test_low_visibility_starts_only_bad_zones(env):
    env.vis["rod1"] = 60.0
    env.tick()
    assert env.mgr.started == [("c1", ["rod1"], aw.SEED_SECONDS)]
    assert env.st["phase"] == "running" and env.st["before"] == {"rod1": 60.0}
    assert env.w.recent()[0]["kind"] == "start"


def test_standing_shaft_blocks_start_until_a_window_has_passed(env):
    env.vis["rod1"] = 5.0                            # на остановке палки не находятся — видимость падает сама
    env.standing = True
    env.tick()
    assert env.mgr.started == [] and env.kinds() == ["check"] and "стоял" in env.w.events[-1]["msg"]
    env.standing = False                             # вал поехал, но окно проверки (30 мин) ещё захватывает остановку
    env.tick(600)
    assert env.mgr.started == [] and len(env.w.events) == 1
    env.tick(1200)                                   # остановка ушла из окна, видимость всё ещё низкая — теперь настоящая просадка
    assert env.mgr.started == [("c1", ["rod1"], aw.SEED_SECONDS)]


def test_stop_in_verify_window_postpones_rollback(env):
    env.vis["rod1"] = 80.0
    env.tick()
    finish_job(env, {"rod1": result("rod1")})
    env.tick(60)
    env.vis["rod1"] = 20.0                           # «хуже, чем до» — но только потому, что вал встал
    env.standing = True
    env.tick(aw.VERIFY_SETTLE_S + 1800 + 60)
    assert env.kinds()[-1] != "rollback" and env.st["phase"] == "verifying"
    env.standing = False
    env.vis["rod1"] = 97.0
    env.tick(1800 + 300)                             # остановка вышла из окна сверки
    assert env.kinds()[-1] == "confirm" and env.mgr.applied_cfgs == []


def test_long_stop_in_verify_never_rolls_back_blindly(env):
    env.vis["rod1"] = 80.0
    env.tick()
    finish_job(env, {"rod1": result("rod1")})
    env.tick(60)
    env.vis["rod1"] = 0.0
    env.standing = True
    for _ in range(40):                              # стоит больше 3 часов, тик за тиком (сторож проходит каждые 30 с)
        env.tick(300)
    assert env.st["phase"] == "verifying" and env.mgr.applied_cfgs == [] and env.kinds()[-1] == "apply"


def test_off_does_nothing(tmp_path):
    e = Env(tmp_path, watch={**WATCH, "on": False})
    e.vis["rod1"] = 10.0
    e.tick()
    assert e.mgr.started == [] and e.w.events == type(e.w.events)()


def test_threshold_is_configurable(tmp_path):
    e = Env(tmp_path, watch={**WATCH, "min_pct": 90.0})
    e.vis["rod1"] = 92.0
    e.tick()
    assert e.mgr.started == []


def test_no_data_no_decision(tmp_path):
    e = Env(tmp_path)
    e.w.vis_fn = lambda cid, zids, t0, t1: {z: (10, 0) for z in zids}      # камера молчала: секунд с картинкой мало
    e.tick()
    assert e.mgr.started == []


def test_improved_is_applied_and_confirmed(env):
    env.vis["rod1"] = 55.0
    env.tick()
    finish_job(env, {"rod1": result("rod1")})
    env.tick(60)
    assert env.st["phase"] == "verifying" and env.st["applied"] == ["rod1"]
    assert env.mgr.cfg["cameras"][0]["zones"][0]["red_hsv"] == NEW["red_hsv"]
    env.vis["rod1"] = 97.0
    env.tick(aw.VERIFY_SETTLE_S + 1800 + 60)
    assert env.kinds()[-1] == "confirm" and env.st["phase"] == "idle"
    assert env.mgr.applied_cfgs == []               # откатов не было
    assert env.mgr.cfg["cameras"][0]["zones"][0]["red_hsv"] == NEW["red_hsv"]


def test_worse_after_apply_is_rolled_back(env):
    env.vis["rod1"] = 80.0
    env.tick()
    finish_job(env, {"rod1": result("rod1")})
    env.tick(60)
    env.vis["rod1"] = 70.0                           # после подгонки хуже, чем до (80)
    env.tick(aw.VERIFY_SETTLE_S + 1800 + 60)
    assert env.kinds()[-1] == "rollback"
    z = env.mgr.cfg["cameras"][0]["zones"][0]
    assert {k: z[k] for k in aw.ZONE_KEYS} == OLD
    assert env.st["cooldown_until"] >= env.t + aw.ROLLBACK_COOLDOWN_H * 3600 - 1
    assert env.w.recent()[-1]["level"] == "warn"


def test_slightly_worse_within_margin_is_kept(env):
    env.vis["rod1"] = 80.0
    env.tick()
    finish_job(env, {"rod1": result("rod1")})
    env.tick(60)
    env.vis["rod1"] = 79.5
    env.tick(aw.VERIFY_SETTLE_S + 1800 + 60)
    assert env.kinds()[-1] == "confirm"


def test_rollback_skipped_if_edited_by_hand(env):
    env.vis["rod1"] = 80.0
    env.tick()
    finish_job(env, {"rod1": result("rod1")})
    env.tick(60)
    env.mgr.cfg["cameras"][0]["zones"][0]["radius"] = 123.0       # человек подправил радиус
    env.vis["rod1"] = 50.0
    env.tick(aw.VERIFY_SETTLE_S + 1800 + 60)
    assert env.kinds()[-1] == "rollback" and env.mgr.applied_cfgs == []
    assert env.mgr.cfg["cameras"][0]["zones"][0]["radius"] == 123.0


def test_small_gain_is_not_applied(env):
    env.vis["rod1"] = 80.0
    env.tick()
    finish_job(env, {"rod1": result("rod1", found=(0.90, 0.91))})
    env.tick(60)
    assert env.kinds()[-1] == "skip" and env.st["phase"] == "idle"
    assert env.mgr.cfg["cameras"][0]["zones"][0]["red_hsv"] == OLD["red_hsv"]
    env.tick(1800)                                   # кулдаун 2 ч: просадка есть, а второй запуск — нет
    assert len(env.mgr.started) == 1
    env.tick(2 * 3600)
    assert len(env.mgr.started) == 2


def test_not_improved_is_skipped(env):
    env.vis["rod1"] = 80.0
    env.tick()
    finish_job(env, {"rod1": result("rod1", improved=False)})
    env.tick(60)
    assert env.kinds()[-1] == "skip"


def test_manual_edit_during_tuning_blocks_apply(env):
    env.vis["rod1"] = 80.0
    env.tick()
    env.mgr.cfg["cameras"][0]["zones"][0]["thin"] = 7            # во время подбора правили руками
    finish_job(env, {"rod1": result("rod1")})
    env.tick(60)
    assert env.kinds()[-1] == "skip" and env.mgr.cfg["cameras"][0]["zones"][0]["red_hsv"] == OLD["red_hsv"]


def test_job_error_sets_cooldown(env):
    env.vis["rod1"] = 80.0
    env.tick()
    env.mgr.autocal["c1"].st.update(state="error", error="камера не отдаёт кадры")
    env.tick(60)
    assert env.kinds()[-1] == "error" and env.st["phase"] == "idle" and env.st["cooldown_until"] > env.t


def test_job_too_long_is_cancelled(env):
    env.vis["rod1"] = 80.0
    env.tick()
    env.tick(aw.JOB_LIMIT_S + 10)
    assert env.mgr.autocal["c1"].cancelled and env.kinds()[-1] == "error"


def test_busy_other_job_retries_soon(env):
    env.vis["rod1"] = 80.0

    def busy(cid, zones, seconds):
        raise RuntimeError("подгонка уже идёт")

    env.mgr.autocal_start = busy
    env.tick()
    assert env.st["phase"] == "idle" and env.st["next_check"] <= env.t + 301


def test_state_survives_restart(env, tmp_path):
    env.vis["rod1"] = 80.0
    env.tick()
    finish_job(env, {"rod1": result("rod1")})
    env.tick(60)
    again = aw.AutocalWatch(env.mgr, clock=lambda: env.t, vis_fn=env._vis)
    assert again.st["c1"]["phase"] == "verifying" and again.st["c1"]["applied"] == ["rod1"]
    env.st["phase"] = "running"                      # подгонка в памяти пропала вместе с процессом
    env.w._save()
    assert aw.AutocalWatch(env.mgr, clock=lambda: env.t).st["c1"]["phase"] == "idle"


def test_events_are_logged_to_jsonl(env):
    env.vis["rod1"] = 80.0
    env.tick()
    rows = [json.loads(x) for x in env.w.log_path.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["kind"] == "start" and rows[-1]["cam"] == "c1"


def test_config_normalization_defaults_and_limits():
    cam = me.normalize_config({"cameras": [{"id": "a", "name": "A", "url": "rtsp://x", "zones": []}]})["cameras"][0]
    assert cam["autocal_watch"] == {"on": False, "min_pct": 95.0, "check_min": 30.0, "cooldown_h": 2.0}
    with pytest.raises(ValueError):
        me.normalize_config({"cameras": [{"id": "a", "name": "A", "url": "rtsp://x", "zones": [], "autocal_watch": {"on": True, "min_pct": 10}}]})
