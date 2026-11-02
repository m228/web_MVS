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
        monkeypatch.setattr(auto_update, "read_version", lambda: "2.0.49")
        self.ag = AutoUpdater(window_fn=lambda: self.window, cfg_fn=lambda: self.cfg, clock=self.clock,
                              frozen_fn=lambda: self.frozen, state_file=tmp_path / "auto_update.json")

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
