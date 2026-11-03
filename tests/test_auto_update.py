"""Автообновление (auto_update): проверка раз в N минут, предзагрузка, перезапуск в окне «варки нет», отмена, защита от зацикливания.

updater, часы и окно — подмены: сети, exe и ПЛК не нужно."""
import pytest

import auto_update
from auto_update import AutoUpdater, FIRST_CHECK_S, HOLD_S, MAX_ATTEMPTS


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class Env:
    """Подмена updater: релиз «2.0.50» при установленной «2.0.49»; считает вызовы."""

    def __init__(self, monkeypatch, tmp_path):
        self.clock = Clock()
        self.window = (True, "")
        self.cfg = {"enabled": True, "interval_min": 30, "auto_restart": True, "warn_s": 10}
        self.check = {"current": "2.0.49", "latest": "2.0.50", "update_available": True, "staged": None, "downloading": False}
        self.download_result = {"ok": True, "version": "2.0.50"}
        self.checks, self.downloads, self.runs = 0, 0, []
        self.run_result = {"ok": True}
        self.frozen = True
        u = auto_update.updater
        monkeypatch.setattr(u, "check_latest", self._check)
        monkeypatch.setattr(u, "download_latest", self._download)
        monkeypatch.setattr(u, "run_update_script", self._run)
        monkeypatch.setattr(u, "components_info", lambda: {"components": [{"key": "main", "installed": True},
                                                                           {"key": "motion", "installed": True},
                                                                           {"key": "cv", "installed": False}]})
        self.version = "2.0.49"
        monkeypatch.setattr(auto_update, "read_version", lambda: self.version)
        self.state_file = tmp_path / "auto_update.json"
        self.wall = 1_800_000_000.0
        self.reports = []                  # то, что ушло бы на приёмник: (kind, level, text, data)
        self.reason = None                 # причина старта по sysmon (app_update — обновили кнопкой)
        self.h = {"micro_on": True, "plate": True, "plc": True, "cam": True, "rtsp": 3, "services": {"cv": "healthy", "motion": "healthy"},
                  "cyclic": True, "manual": False, "alerts": {}}
        h = self.h
        self.src = {"micro_on": lambda: h["micro_on"], "plate_connected": lambda: h["plate"], "plc_connected": lambda: h["plc"],
                    "camera_streaming": lambda: h["cam"], "rtsp_running": lambda: h["rtsp"], "services": lambda: dict(h["services"]),
                    "automat": lambda: (h["cyclic"], h["manual"]), "alerts": lambda: dict(h["alerts"])}
        self.ag = self.new_agent()

    def new_agent(self):
        """Новое приложение после перезапуска: тот же файл состояния, свои часы с нуля."""
        self.clock = Clock()
        return AutoUpdater(window_fn=lambda: self.window, cfg_fn=lambda: self.cfg, clock=self.clock,
                           frozen_fn=lambda: self.frozen, state_file=self.state_file, report_fn=self._report, wall=lambda: self.wall,
                           health_src=self.src, start_reason_fn=lambda: self.reason)

    def _report(self, kind, level, text, data):
        self.reports.append((kind, level, text, data))
        return True

    def _check(self, quiet=False):
        self.checks += 1
        return dict(self.check)

    def _download(self, extract=True, delta_only=False):
        self.downloads += 1
        if self.download_result.get("ok"):
            self.check["staged"] = self.download_result["version"]
        return dict(self.download_result)

    def _run(self, port, **kw):
        self.runs.append((port, kw))
        return dict(self.run_result)

    def tick(self, advance=0):
        self.clock.advance(advance)
        self.ag.tick()

    def first_check(self):
        self.tick(FIRST_CHECK_S + 1)


@pytest.fixture
def env(monkeypatch, tmp_path):
    return Env(monkeypatch, tmp_path)


def test_nothing_before_first_check_delay(env):
    env.tick(10)
    assert env.checks == 0


def test_not_frozen_or_disabled_does_nothing(env):
    env.frozen = False
    env.first_check()
    assert env.checks == 0
    env.frozen = True
    env.cfg["enabled"] = False
    env.tick(10)
    assert env.checks == 0


def test_no_update_means_no_download(env):
    env.check["update_available"] = False
    env.first_check()
    assert env.checks == 1 and env.downloads == 0


def test_update_is_downloaded_once(env):
    env.first_check()
    assert env.downloads == 1
    assert env.ag.state()["staged"] == "2.0.50"
    env.tick(30 * 60 + 1)                              # следующая проверка: архив уже скачан
    assert env.checks == 2 and env.downloads == 1


def test_check_interval_is_from_config(env):
    env.cfg["interval_min"] = 30
    env.first_check()
    env.tick(29 * 60)
    assert env.checks == 1
    env.tick(2 * 60)
    assert env.checks == 2


def test_failed_download_backs_off_up_to_an_hour(env):
    env.download_result = {"ok": False, "error": "нет связи"}
    env.first_check()
    assert env.downloads == 1 and env.ag.state()["error"] == "нет связи"
    env.tick(31 * 60)                                  # первый сбой: повтор через обычные 30 мин
    assert env.downloads == 2
    env.tick(31 * 60)                                  # второй: пауза выросла до 60 мин, через 31 рано
    assert env.downloads == 2
    env.tick(30 * 60)
    assert env.downloads == 3
    env.tick(61 * 60)                                  # потолок — час, не 2 часа
    assert env.downloads == 4


def test_check_error_does_not_kill_state(env):
    env.check = {"current": "2.0.49", "latest": None, "update_available": False, "error": "нет GitHub"}
    env.first_check()
    st = env.ag.state()
    assert st["error"] == "нет GitHub" and st["staged"] is None and env.downloads == 0


def test_restart_after_window_held_and_warning(env):
    env.first_check()
    env.tick(5)                                        # окно открылось
    assert env.ag.state()["auto"]["restart_in_s"] is None
    env.tick(HOLD_S)                                   # держится HOLD_S — пошёл отсчёт
    assert env.ag.state()["auto"]["restart_in_s"] == 10
    env.tick(5)
    assert env.runs == [] and env.ag.state()["auto"]["restart_in_s"] == 5
    env.tick(5)                                        # отсчёт вышел
    assert len(env.runs) == 1
    port, kw = env.runs[0]
    assert port == 8000 and kw["staged"] is True and kw["no_browser"] is True
    assert kw["components"] == "main,motion"           # основное + установленные дополнения
    env.tick(60)
    assert len(env.runs) == 1                          # второй раз не запускаем


def test_closed_window_blocks_and_reports_reason(env):
    env.window = (False, "идёт варка (стадия 7)")
    env.first_check()
    env.tick(HOLD_S + 60)
    assert env.runs == []
    assert env.ag.state()["auto"]["waiting_for"] == "идёт варка (стадия 7)"


def test_window_closing_during_countdown_resets_it(env):
    env.first_check()
    env.tick(5)
    env.tick(HOLD_S)
    assert env.ag.state()["auto"]["restart_in_s"] == 10
    env.window = (False, "идёт проба или разбор")
    env.tick(5)
    assert env.ag.state()["auto"]["restart_in_s"] is None
    env.window = (True, "")
    env.tick(5)
    env.tick(5)                                        # окно снова открылось — HOLD_S заново
    assert env.runs == [] and env.ag.state()["auto"]["restart_in_s"] is None


def test_cancel_holds_until_next_window(env):
    env.first_check()
    env.tick(5)
    env.tick(HOLD_S)
    st = env.ag.cancel()
    assert st["auto"]["restart_in_s"] is None
    env.tick(HOLD_S + 60)
    assert env.runs == []                              # в этом же окне больше не предлагаем
    env.window = (False, "идёт варка (стадия 3)")      # следующая варка...
    env.tick(10)
    env.window = (True, "")                            # ...и следующая пропарка
    env.tick(5)
    env.tick(HOLD_S)
    assert env.ag.state()["auto"]["restart_in_s"] == 10
    env.tick(10)
    assert len(env.runs) == 1


def test_attempts_limit_per_version(env):
    for _ in range(MAX_ATTEMPTS):
        env.window = (True, "")
        env.first_check() if env.checks == 0 else env.tick(1)
        env.tick(5)
        env.tick(HOLD_S)
        env.tick(10)
        env.window = (False, "идёт варка (стадия 2)")  # обновление не прошло, версия та же, завод снова варит
        env.tick(5)
        env.ag._launched = False                       # приложение живо: апдейтер откатился
    assert len(env.runs) == MAX_ATTEMPTS
    env.window = (True, "")
    env.tick(5)
    env.tick(HOLD_S + 20)
    assert len(env.runs) == MAX_ATTEMPTS               # третьей попытки нет
    assert "отключён" in env.ag.state()["auto"]["waiting_for"]


def test_auto_restart_can_be_switched_off(env):
    env.cfg["auto_restart"] = False
    env.first_check()
    env.tick(HOLD_S + 60)
    assert env.runs == [] and env.ag.state()["staged"] == "2.0.50"   # скачивать продолжает, перезапуск — только кнопкой


def test_failed_launch_is_not_hammered(env):
    env.run_result = {"ok": False, "error": "update.ps1 не найден"}
    env.first_check()
    env.tick(5)
    env.tick(HOLD_S)
    env.tick(10)
    assert len(env.runs) == 1
    env.tick(HOLD_S + 60)
    assert len(env.runs) == 1


def test_staged_version_not_newer_than_current_is_ignored(env):
    env.check.update(latest="2.0.49", update_available=False, staged="2.0.49")
    env.first_check()
    env.tick(HOLD_S + 60)
    assert env.runs == []


def test_config_bounds(monkeypatch):
    monkeypatch.setattr(auto_update.plate_config, "load", lambda: {"auto_update": {"interval_min": 0, "warn_s": 1}})
    cfg = auto_update.load_config()
    assert cfg["interval_min"] == 1.0 and cfg["warn_s"] == 3
    monkeypatch.setattr(auto_update.plate_config, "load", lambda: {})
    assert auto_update.load_config() == auto_update.DEFAULT_CFG


# ---------- клиенты без микроскопа: ночное окно ----------

def _at(h, m=0):
    import time
    return time.mktime((2026, 11, 3, h, m, 0, 0, 0, -1))


@pytest.mark.parametrize("h,open_", [(19, False), (20, True), (23, True), (0, True), (7, True), (8, False), (12, False)])
def test_night_window_20_to_08_wraps_midnight(h, open_):
    ok, why = auto_update.night_window({"night_from": 20, "night_to": 8}, _at(h, 30) if h not in (19, 8) else _at(h, 59))
    assert ok is open_ and (ok or "20:00–08:00" in why)


def test_night_window_same_day_range_and_always_open():
    cfg = {"night_from": 2, "night_to": 5}
    assert auto_update.night_window(cfg, _at(3))[0] is True and auto_update.night_window(cfg, _at(5))[0] is False
    assert auto_update.night_window({"night_from": 0, "night_to": 24}, _at(13))[0] is True
    assert auto_update.night_window({"night_from": 7, "night_to": 7}, _at(13))[0] is True, "равные часы — окно открыто всегда"
    assert auto_update.night_window({}, _at(22))[0] is True, "без настроек — 20:00–08:00 по умолчанию"


def test_night_window_closed_while_model_check_runs(monkeypatch):
    import model_check
    monkeypatch.setitem(model_check.STATE, "running", True)
    ok, why = auto_update.night_window({"night_from": 20, "night_to": 8}, _at(23))
    assert ok is False and "проверка модели" in why


def test_night_config_defaults_and_bounds(monkeypatch):
    assert auto_update.DEFAULT_CFG["night_from"] == 20 and auto_update.DEFAULT_CFG["night_to"] == 8
    monkeypatch.setattr(auto_update.plate_config, "load", lambda: {"auto_update": {"night_from": 99, "night_to": -5}})
    cfg = auto_update.load_config()
    assert cfg["night_from"] == 23 and cfg["night_to"] == 0
    monkeypatch.setattr(auto_update.plate_config, "load", lambda: {"auto_update": {"night_from": "x"}})
    assert auto_update.load_config()["night_from"] == 20


class _NoMicro:
    """Клиент без микроскопа: окно решает ночное правило, а не микроскопное (оно всегда закрыто)."""

    def __init__(self, env, night=(True, ""), micro=False):
        self.night, self.micro = night, micro
        env.window = (False, "микроскоп выключен")
        env.ag = AutoUpdater(window_fn=lambda: env.window, cfg_fn=lambda: env.cfg, clock=env.clock, frozen_fn=lambda: env.frozen,
                             state_file=env.state_file, report_fn=env._report, wall=lambda: env.wall, health_src=env.src,
                             start_reason_fn=lambda: env.reason, micro_fn=lambda: self.micro, night_fn=lambda cfg: self.night)


def test_without_microscope_night_window_restarts(env):
    _NoMicro(env)
    env.first_check()
    env.tick(HOLD_S + 5)
    env.tick(env.cfg["warn_s"])
    assert len(env.runs) == 1 and env.runs[0][1]["staged"] is True


def test_without_microscope_daytime_waits_and_says_why(env):
    nm = _NoMicro(env, night=(False, "ждёт ночного окна 20:00–08:00"))
    env.first_check()
    env.tick(HOLD_S + 60)
    assert env.runs == [] and env.ag.state()["auto"]["waiting_for"] == "ждёт ночного окна 20:00–08:00"
    nm.night = (True, "")                                # наступила ночь
    env.tick(5)                                          # окно открылось
    env.tick(HOLD_S)                                     # держится HOLD_S — пошёл отсчёт
    env.tick(env.cfg["warn_s"])
    assert len(env.runs) == 1


def test_with_microscope_the_night_rule_is_not_used(env):
    _NoMicro(env, night=(True, ""), micro=True)          # микроскоп работает: решает он (окно закрыто — варка), ночь не помогает
    env.window = (False, "идёт варка (стадия 7)")
    env.first_check()
    env.tick(HOLD_S + 60)
    assert env.runs == [] and env.ag.state()["auto"]["waiting_for"] == "идёт варка (стадия 7)"


def test_night_cancel_holds_until_window_closes_and_opens_again(env):
    nm = _NoMicro(env)
    env.first_check()
    env.tick(HOLD_S + 1)
    env.ag.cancel()
    env.tick(60)
    assert env.runs == [] and env.ag.state()["auto"]["waiting_for"].startswith("отменено")
    nm.night = (False, "день")                           # утро — окно закрылось, отмена сброшена
    env.tick(10)
    nm.night = (True, "")                                # следующая ночь
    env.tick(5)
    env.tick(HOLD_S)
    env.tick(env.cfg["warn_s"])
    assert len(env.runs) == 1


# ---------- правки updater, на которые опирается автообновление ----------

def _make_zip(path, version):
    import zipfile
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("_internal/VERSION", version)
        zf.writestr("web_MVS.exe", b"x")


def test_download_latest_does_not_redownload_staged_archive(monkeypatch, tmp_path):
    import updater
    staging = tmp_path / ".update"
    staging.mkdir()
    z = staging / "web_MVS_v2.0.50.zip"
    _make_zip(z, "2.0.50")
    monkeypatch.setattr(updater, "STAGING", staging)
    monkeypatch.setattr(updater, "STAGED", staging / "staged")
    monkeypatch.setattr(updater, "is_frozen", lambda: True)
    monkeypatch.setattr(updater, "_fetch_latest_meta", lambda max_age=0.0: ("v2.0.50", {"name": z.name, "size": 1}, ""))
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *a, **k: pytest.fail("архив скачан второй раз"))
    before = z.stat().st_mtime_ns
    r = updater.download_latest(extract=False)
    assert r == {"ok": True, "version": "2.0.50", "already": True}
    assert z.is_file() and z.stat().st_mtime_ns == before
    # распаковка нужна, а её нет — «уже скачано» не считается (дальше пошло бы обычное скачивание)
    assert updater.staged_version() == "2.0.50"


def test_quiet_check_logs_only_on_change(monkeypatch):
    import updater
    logged = []
    monkeypatch.setattr(updater, "log_event", lambda *a, **k: logged.append(a))
    monkeypatch.setattr(updater, "_last_check_logged", None)
    monkeypatch.setattr(updater, "_fetch_latest_meta", lambda max_age=0.0: ("v9.9.9", {"name": "web_MVS_v9.9.9.zip"}, ""))
    monkeypatch.setattr(updater, "read_version", lambda: "1.0.0")
    monkeypatch.setattr(updater, "staged_version", lambda: "")
    for _ in range(3):
        updater.check_latest(quiet=True)
    assert len(logged) == 1
    updater.check_latest()                              # ручная проверка пишет всегда
    assert len(logged) == 2


# ---------- сообщения об обновлении и проверка «всё ли поднялось» ----------

from auto_update import HEALTH_FIRST_S, HEALTH_MAX_S, HEALTH_RETRY_S, PENDING_MAX_S


def updates(env):
    return [(lvl, d["state"], text) for kind, lvl, text, d in env.reports if kind == "update"]


def restart_into_new_version(env):
    """Автоперезапуск прошёл до запуска update.ps1; дальше «новое приложение» на 2.0.50 с тем же файлом состояния."""
    env.first_check()
    env.tick(5)
    env.tick(HOLD_S)
    env.tick(10)
    assert len(env.runs) == 1
    env.version = "2.0.50"
    env.wall += 60
    env.ag = env.new_agent()
    env.ag.resume()


def test_staged_message_once_per_downloaded_version(env):
    env.first_check()
    assert updates(env) == [("info", "staged", "Скачана v2.0.50, перезапущусь после варки")]
    env.tick(30 * 60 + 1)
    assert len(updates(env)) == 1


def test_staged_message_without_auto_restart_and_not_for_already_staged(env):
    env.cfg["auto_restart"] = False
    env.first_check()
    assert updates(env)[0][2] == "Скачана v2.0.50, жду перезапуска по кнопке"
    env.reports.clear()
    env.download_result = {"ok": True, "version": "2.0.50", "already": True}
    env.check["staged"] = None
    env.tick(30 * 60 + 1)
    assert env.reports == []


def test_launch_writes_marker_with_state_before_restart(env):
    import json
    env.h["cam"], env.h["rtsp"] = True, 2
    env.first_check()
    env.tick(5)
    env.tick(HOLD_S)
    env.tick(10)
    data = json.loads(env.state_file.read_text(encoding="utf-8"))
    p = data["pending"]
    assert (p["from"], p["to"], p["t"]) == ("2.0.49", "2.0.50", env.wall)
    assert p["pre"]["camera_streaming"] and p["pre"]["rtsp_running"] == 2 and p["pre"]["services_ok"] == ["cv", "motion"]
    assert data["attempts"] == {"2.0.50": 1}, "счётчик попыток не потерян рядом с меткой"


def test_failed_launch_does_not_leave_marker(env):
    env.run_result = {"ok": False, "error": "update.ps1 не найден"}
    env.first_check()
    env.tick(5)
    env.tick(HOLD_S)
    env.tick(10)
    import json
    assert "pending" not in json.loads(env.state_file.read_text(encoding="utf-8"))


def test_everything_back_gives_one_ok_message(env):
    restart_into_new_version(env)
    env.reports.clear()
    env.tick(HEALTH_FIRST_S - 1)
    assert env.reports == []
    env.tick(2)
    assert len(env.reports) == 1
    kind, level, text, data = env.reports[0]
    assert (level, data["state"]) == ("info", "ok")
    assert text.startswith("Обновлено 2.0.49 → 2.0.50 · плата ✓ · ПЛК ✓ · камера ✓ · RTSP 3 из 3 ✓ · автомат ✓ · сервисы ✓")
    env.tick(HEALTH_MAX_S)
    assert len(env.reports) == 1, "итог один"
    assert "pending" not in __import__("json").loads(env.state_file.read_text(encoding="utf-8"))


def test_slow_recovery_waits_and_reports_ok_once(env):
    restart_into_new_version(env)
    env.reports.clear()
    env.h["cam"], env.h["rtsp"] = False, 1                # камеры подключаются со страницы не сразу
    env.tick(HEALTH_FIRST_S + 1)
    assert env.reports == []
    env.h["cam"], env.h["rtsp"] = True, 3
    env.tick(HEALTH_RETRY_S)
    assert [u[1] for u in updates(env)] == ["ok"]


def test_problems_after_deadline_give_loud_warning_with_reasons(env):
    restart_into_new_version(env)
    env.reports.clear()
    env.h["cam"] = False
    env.h["services"]["cv"] = "gave_up"
    for _ in range(12):
        env.tick(HEALTH_RETRY_S)
    assert len(env.reports) == 1
    kind, level, text, data = env.reports[0]
    assert (level, data["state"]) == ("medium", "warn")
    assert "камера микроскопа не стримит" in text and "сервис CV не отвечает" in text and "в порядке: плата, ПЛК" in text
    assert data["bad"] and data["ok"]


def test_new_red_alert_is_a_problem_but_old_one_is_not(env):
    env.h["alerts"] = {"pos_approach": {"level": "high", "text": "Подвод: датчики разошлись"}}   # была ещё до обновления
    restart_into_new_version(env)
    env.reports.clear()
    env.h["alerts"]["camera_exposure"] = {"level": "high", "text": "Камера: экспозиция 5000 вместо 170"}
    for _ in range(12):
        env.tick(HEALTH_RETRY_S)
    assert "новая красная тревога: Камера: экспозиция" in updates(env)[0][2]
    assert "Подвод" not in updates(env)[0][2]


def test_same_version_after_launch_means_failed_update(env):
    env.first_check()
    env.tick(5)
    env.tick(HOLD_S)
    env.tick(10)
    env.wall += 120
    env.ag = env.new_agent()                           # апдейтер откатился: приложение снова на 2.0.49
    env.reports.clear()
    env.ag.resume()
    assert updates(env) == [("info", "failed", "Обновление до v2.0.50 не удалось, работаю на v2.0.49")]
    env.tick(HEALTH_MAX_S + 10)
    assert len(env.reports) == 1, "проверка «всё поднялось» после отката не запускается"


def test_stale_marker_is_ignored(env):
    restart_into_new_version(env)
    env.reports.clear()
    env.wall += PENDING_MAX_S + 100
    import json
    st = json.loads(env.state_file.read_text(encoding="utf-8"))
    st["pending"] = {"from": "2.0.49", "to": "2.0.50", "t": env.wall - PENDING_MAX_S - 50, "pre": None}
    env.state_file.write_text(json.dumps(st), encoding="utf-8")
    env.ag = env.new_agent()
    env.ag.resume()
    env.tick(HEALTH_MAX_S + 10)
    assert env.reports == []


def test_manual_update_gets_absolute_checks_only(env):
    env.version, env.reason = "2.0.50", "app_update"   # метки нет: обновили кнопкой
    env.h["rtsp"], env.h["cam"] = 0, False             # «было/стало» сравнивать не с чем — на них не ругаемся
    env.ag.resume()
    env.tick(HEALTH_FIRST_S + 1)
    kind, level, text, data = env.reports[0]
    assert (level, data["state"]) == ("info", "ok")
    assert text.startswith("Обновлено до v2.0.50 · плата ✓ · ПЛК ✓ · сервисы ✓") and "камера" not in text


def test_manual_update_still_catches_dead_plate(env):
    env.check["update_available"] = False                 # новее релиза нет — «скачана» не шлём
    env.version, env.reason = "2.0.50", "app_update"
    env.h["plate"] = False
    env.ag.resume()
    for _ in range(10):
        env.tick(HEALTH_RETRY_S)
    assert updates(env)[0][1] == "warn" and "плата микроскопа не отвечает" in updates(env)[0][2]


def test_plain_restart_and_dev_run_do_not_start_health(env):
    env.check["update_available"] = False                 # новее релиза нет — «скачана» не шлём
    env.reason = "app_restart"
    env.ag.resume()
    env.tick(HEALTH_MAX_S + 10)
    assert env.reports == []
    env.reason, env.frozen = "app_update", False
    env.ag.resume()
    env.tick(HEALTH_MAX_S + 10)
    assert env.reports == []


def test_health_runs_even_if_auto_update_is_switched_off(env):
    env.cfg["enabled"] = False
    env.version, env.reason = "2.0.50", "app_update"
    env.ag.resume()
    env.tick(HEALTH_FIRST_S + 1)
    assert updates(env)[0][1] == "ok" and env.checks == 0


def test_broken_report_channel_does_not_break_the_thread(env):
    def boom(*a):
        raise RuntimeError("приёмник недоступен")
    env.ag._report_fn = boom
    env.first_check()                                  # «скачана» не отправилось — но проверка и скачивание отработали
    assert env.downloads == 1 and env.ag.state()["staged"] == "2.0.50"


def test_health_source_failure_is_reported_as_problem_not_crash(env):
    env.check["update_available"] = False                 # новее релиза нет — «скачана» не шлём
    env.version, env.reason = "2.0.50", "app_update"
    env.src["plate_connected"] = lambda: (_ for _ in ()).throw(RuntimeError("нет платы"))
    env.ag.resume()
    for _ in range(10):
        env.tick(HEALTH_RETRY_S)
    assert updates(env)[0][1] == "warn"
