"""Автообновление по компонентам (v3.0): выдержка релиза, метка [urgent], обновление одних сервисов без перезапуска приложения, окна сервисов.

Подмены те же, что в test_auto_update (Env): сети, exe и ПЛК не нужно."""
import time as _time

import pytest

import auto_update
from auto_update import HOLD_S
from test_auto_update import Env


@pytest.fixture
def env(monkeypatch, tmp_path):
    return Env(monkeypatch, tmp_path)


def _iso(wall: float) -> str:
    return _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(wall))


def _go(env):
    env.first_check()
    env.tick(5)
    env.tick(HOLD_S)
    env.tick(10)


def test_soak_holds_fresh_release_and_installs_after(env):
    env.cfg["soak_h"] = 2
    env.check["published_at"] = _iso(env.wall - 30 * 60)           # вышел 30 мин назад
    env.first_check()
    env.tick(HOLD_S + 60)
    assert env.downloads == 0 and env.runs == []
    assert "выдержка" in env.ag.state()["auto"]["waiting_for"]
    env.wall += 2 * 3600                                           # прошло ещё два часа
    env.tick(31 * 60)                                              # следующая проверка (interval 30 мин)
    assert env.downloads == 1
    env.tick(5)
    env.tick(HOLD_S)
    env.tick(10)
    assert len(env.runs) == 1


def test_urgent_release_skips_soak(env):
    env.cfg["soak_h"] = 2
    env.check.update(published_at=_iso(env.wall - 60), urgent=True)
    env.first_check()
    assert env.downloads == 1


def test_soak_zero_does_not_hold(env):
    env.cfg["soak_h"] = 0
    env.check["published_at"] = _iso(env.wall)
    env.first_check()
    assert env.downloads == 1


def test_services_only_update_does_not_restart_app(env):
    """Изменился только сервис: приложение не скачиваем и не перезапускаем (без staged и без метки «обновляюсь»)."""
    env.check.update(update_available=False, latest="2.0.49", addons_due=[{"key": "cv", "current": "3.0.0", "latest": "3.1.0"}])
    env.window = (True, "")                                         # окно микроскопа открыто (cv ждёт его)
    _go(env)
    assert env.downloads == 0
    assert len(env.runs) == 1
    port, kw = env.runs[0]
    assert kw["components"] == "cv" and not kw.get("staged")
    assert not env.state_file.exists() or "pending" not in env.state_file.read_text(encoding="utf-8")
    assert env.ag._launched is False, "приложение живо — агент не «в перезапуске»"
    env.tick(60)
    assert len(env.runs) == 1, "сразу второй раз не запускаем"


def test_motion_update_waits_for_day_service_window_not_microscope_window(env):
    env.check.update(update_available=False, addons_due=[{"key": "motion", "current": "3.0.0", "latest": "3.0.1"}])
    env.window = (True, "")                                         # у микроскопа окно открыто, но motion его не использует
    env.ag._service_fn = lambda cfg: (False, "ждёт окна 08:00–10:00")
    env.first_check()
    env.tick(HOLD_S + 60)
    assert env.runs == []
    assert env.ag.state()["auto"]["waiting_for"] == "ждёт окна 08:00–10:00"
    env.ag._service_fn = lambda cfg: (True, "")                     # наступило 08:00
    env.tick(5)
    env.tick(HOLD_S)
    env.tick(10)
    assert [r[1]["components"] for r in env.runs] == ["motion"]


def test_main_and_due_service_go_together_only_due_components(env):
    env.check["addons_due"] = [{"key": "motion", "current": "3.0.0", "latest": "3.0.1"}]
    env.ag._service_fn = lambda cfg: (True, "")
    _go(env)
    assert env.runs[0][1]["components"] == "main,motion" and env.runs[0][1]["staged"] is True


def test_failed_service_update_is_not_hammered(env):
    env.check.update(update_available=False, addons_due=[{"key": "cv", "current": "3.0.0", "latest": "3.1.0"}])
    env.window = (True, "")
    for _ in range(auto_update.MAX_ATTEMPTS + 2):
        env.ag._busy_until = 0.0
        env.ag._cancelled = False
        env.ag._close_window("")
        _go(env)
    assert len(env.runs) == auto_update.MAX_ATTEMPTS


def test_hours_window_service_keys():
    cfg = dict(auto_update.DEFAULT_CFG)
    t9 = _time.mktime((2026, 11, 16, 9, 0, 0, 0, 0, -1))
    t23 = _time.mktime((2026, 11, 16, 23, 0, 0, 0, 0, -1))
    assert auto_update.hours_window(cfg, t9, keys=("service_from", "service_to"))[0] is True
    ok, why = auto_update.hours_window(cfg, t23, keys=("service_from", "service_to"))
    assert ok is False and "08:00–10:00" in why
