"""monitor_server/tray.py — надзор за сервером и значок в трее: запуск, перезапуск, подъём после падения, один значок на ПК,
сервер не остаётся «сиротой», без библиотеки значка не падаем. Сам значок на экране проверяется вручную (см. README)."""
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MS = ROOT / "monitor_server"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, MS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(MS))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(MS))
    return mod


tray = _load("monitor_tray_entry", "tray.py")
server = _load("monitor_server_light", "server.py")

SLEEP60 = [sys.executable, "-c", "import time; time.sleep(60)"]


def wait_for(cond, timeout=10.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


@pytest.fixture
def sup():
    made = []

    def make(cmd, **kw):
        s = tray.Supervisor(cmd, MS, backoff_base_s=kw.pop("backoff_base_s", 0.05), **kw)
        made.append(s)
        return s
    yield make
    for s in made:
        s.stop()


def test_supervisor_starts_child_and_stop_kills_it_for_good(sup):
    s = sup(SLEEP60)
    s.start()
    assert wait_for(lambda: s.pid is not None), "сервер не запустился"
    pid = s.pid
    s.stop()
    assert s.pid is None and s.status() == "остановлен"
    time.sleep(0.5)
    assert s.pid is None and s.proc.poll() is not None, "после остановки сервер не должен подниматься заново"
    assert not server.pid_alive(pid)


def test_restart_replaces_child_and_is_not_counted_as_crash(sup):
    s = sup(SLEEP60)
    s.start()
    assert wait_for(lambda: s.pid is not None)
    old = s.pid
    s.restart()
    assert wait_for(lambda: s.pid is not None and s.pid != old), "после «Перезапустить» не поднялся новый сервер"
    assert s.restarts == 1 and s.fast_fails == 0, "команда человека — не падение: пауз и счётчика быстрых падений быть не должно"
    assert not server.pid_alive(old), "старый сервер должен быть убит"


def test_crashed_child_is_respawned_with_growing_pause(sup):
    s = sup([sys.executable, "-c", "raise SystemExit(3)"], backoff_base_s=0.05)
    s.start()
    assert wait_for(lambda: s.restarts >= 3, timeout=10), "упавший сервер не поднимается снова"
    assert s.fast_fails >= 3 and s.status() in ("не запускается", "запускается")
    s.stop()


def test_long_lived_child_resets_fast_fail_counter(sup, monkeypatch):
    monkeypatch.setattr(tray, "OK_AFTER_S", 0.3)
    s = sup([sys.executable, "-c", "import time; time.sleep(0.6)"], backoff_base_s=0.05)
    s.start()
    assert wait_for(lambda: s.restarts >= 2, timeout=10)
    assert s.fast_fails == 0, "сервер проработал дольше порога — это уже не «падает сразу после старта»"
    s.stop()


def test_status_text_follows_the_child(sup):
    s = sup(SLEEP60)
    assert s.status() == "запускается"
    s.start()
    assert wait_for(lambda: s.pid is not None)
    t0 = s.started_t
    s.clock = lambda: t0 + 5
    assert s.status() == "работает"
    s.stop()


def test_spawn_failure_does_not_kill_the_supervisor(sup):
    s = sup([str(MS / "no_such_program.exe")], backoff_base_s=0.05)
    s.start()
    assert wait_for(lambda: s.fast_fails >= 2, timeout=10), "не запустилось — надзор должен продолжать пробовать"
    s.stop()


def test_single_instance_second_launch_is_refused():
    name = "Local\\web_MVS_monitor_tray_test_%d" % os.getpid()
    first = tray.acquire_single_instance(name)
    assert first, "первый запуск должен получить право"
    assert tray.acquire_single_instance(name) is None, "второй значок на том же ПК не нужен"


def test_panel_url_and_tooltip():
    assert tray.panel_url("11.11.11.12", 8769) == "http://11.11.11.12:8769/"
    assert tray.panel_url("0.0.0.0", 8769) == "http://127.0.0.1:8769/", "«все адреса» открываем через этот же ПК"

    class Fake:
        restarts = 2

        def status(self):
            return "работает"
    txt = tray.tooltip(Fake(), "2.0.45")
    assert txt == "web_MVS панель — работает (v2.0.45), перезапусков: 2" and len(txt) < 128


def test_missing_tray_library_keeps_server_running(sup, monkeypatch):
    # нет pystray/Pillow: значка не будет, но надзор и сервер живут
    monkeypatch.setitem(sys.modules, "pystray", None)
    s = sup(SLEEP60)
    assert tray.run_tray(s, "http://127.0.0.1:1/", MS / "static" / "icon-192.png", "x") is False


def test_server_pid_alive_and_parent_watch():
    assert server.pid_alive(os.getpid())
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert not server.pid_alive(p.pid), "завершившийся процесс — не жив"
    gone = []
    server.watch_parent(p.pid, interval_s=0.05, on_gone=lambda: gone.append(1))
    assert wait_for(lambda: gone, timeout=5), "сервер не заметил, что надзор пропал"


def test_server_import_is_light_for_the_supervisor():
    # надзор импортирует server.py только за load_listen/read_version: тяжёлый FastAPI ему не нужен
    code = "import sys; sys.path.insert(0, r'%s'); import server; print('fastapi' in sys.modules, 'monitor_app' in sys.modules)" % MS
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30).stdout.split()
    assert out == ["False", "False"], out


def test_autostart_runs_tray_and_dependencies_are_listed():
    ps1 = (MS / "install_autostart.ps1").read_text(encoding="utf-8-sig")
    assert '"tray.py"' in ps1 and "-NoTray" in ps1 and "Stop-ScheduledTask" in ps1, "автозапуск должен запускать трей и останавливать прежний экземпляр"
    req = (MS / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "pystray" in req and "pillow" in req
    assert "tray.py" in (MS / "run_tray.bat").read_text(encoding="utf-8")
    assert (MS / "static" / "icon-192.png").is_file(), "значок берётся из static\\icon-192.png"
