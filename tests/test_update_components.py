"""Выбор компонентов обновления: основное приложение + дополнения (cv_service, video_service, motion_service).

Бэкенд (updater.components_info / parse_components / run_update_script) и сам update.ps1 — на «песочнице»: фальшивый
дополнительный сервис в zip (с маркером, VERSION и своим setup.bat, который создаёт .venv), без сети и без админа.
"""
import subprocess
import time
import sys
import zipfile
from pathlib import Path

import pytest

import updater

ROOT = Path(__file__).resolve().parent.parent


def test_parse_components_known_keys_only():
    assert updater.parse_components("main, CV;bogus motion main") == ["main", "cv", "motion"]
    assert updater.parse_components("") == [] and updater.parse_components(None) == []


def test_components_info_versions_installed_and_remembered(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "DATA_DIR", tmp_path)
    (tmp_path / "video_service").mkdir()
    (tmp_path / "video_service" / "render.py").write_text("", encoding="utf-8")
    (tmp_path / "video_service" / "VERSION").write_text("v1.9.10\r\n", encoding="utf-8")
    (tmp_path / "update_choice.json").write_text('{"components": ["main", "video", "bogus"]}', encoding="utf-8")
    info = updater.components_info()
    by = {c["key"]: c for c in info["components"]}
    assert list(by) == ["main", "cv", "video", "motion"]
    assert by["video"]["installed"] is True and by["video"]["version"] == "1.9.10"
    assert by["cv"]["installed"] is False and by["cv"]["heavy"] is True and by["motion"]["installed"] is False
    assert info["remembered"] == ["main", "video"], "неизвестные ключи в прошлом выборе отбрасываются"


def test_run_update_script_passes_selection(tmp_path, monkeypatch):
    (tmp_path / "update.ps1").write_text("", encoding="utf-8")
    monkeypatch.setattr(updater, "is_frozen", lambda: True)
    monkeypatch.setattr(updater, "_app_dir", lambda: tmp_path)
    calls = []
    monkeypatch.setattr(updater.subprocess, "Popen", lambda cmd, **kw: calls.append(cmd))
    r = updater.run_update_script(8000, components="motion,cv", yes=True)
    cmd = calls[-1]
    assert r["ok"] and r["restart"] is False and r["components"] == ["motion", "cv"]
    assert cmd[cmd.index("-Components") + 1] == "motion,cv" and "-NoMenu" in cmd and "-Yes" in cmd
    r = updater.run_update_script(8000)                                    # без выбора — как раньше: только основное приложение
    assert r["components"] == ["main"] and r["restart"] is True and "-Yes" not in calls[-1]
    r = updater.run_update_script(8000, staged=True, components="motion")
    assert r["ok"] is False and "основное приложение" in r["error"], "скачанный архив — это основное приложение"


def test_run_update_script_no_browser_only_if_script_knows_it(tmp_path, monkeypatch):
    """Первое обновление идёт СТАРЫМ update.ps1: незнакомый -NoBrowser уронил бы запуск, поэтому ключ передаётся только знающему его скрипту."""
    monkeypatch.setattr(updater, "is_frozen", lambda: True)
    monkeypatch.setattr(updater, "_app_dir", lambda: tmp_path)
    calls = []
    monkeypatch.setattr(updater.subprocess, "Popen", lambda cmd, **kw: calls.append(cmd))
    (tmp_path / "update.ps1").write_text("param([switch]$Force)", encoding="utf-8")
    r = updater.run_update_script(8000, no_browser=True)
    assert r["ok"] and r["no_browser"] is False and "-NoBrowser" not in calls[-1]
    (tmp_path / "update.ps1").write_text("param([switch]$Force, [switch]$NoBrowser)", encoding="utf-8")
    r = updater.run_update_script(8000, no_browser=True)
    assert r["ok"] and r["no_browser"] is True and "-NoBrowser" in calls[-1]
    r = updater.run_update_script(8000)                                    # без просьбы — ключа нет
    assert r["no_browser"] is False and "-NoBrowser" not in calls[-1]


def test_update_log_tail_since(tmp_path, monkeypatch):
    """Страница ловит ход и ошибку апдейтера по update.log: только строки с момента запуска, без метки времени, BOM и мусор пропускаются."""
    import time as _t
    monkeypatch.setattr(updater, "is_frozen", lambda: True)
    monkeypatch.setattr(updater, "_app_dir", lambda: tmp_path)
    assert updater.update_log_tail() == [], "файла нет — пусто"
    since = _t.mktime(_t.strptime("2026-10-30 12:00:00", "%Y-%m-%d %H:%M:%S"))
    (tmp_path / "update.log").write_bytes(("﻿2026-10-30 11:59:00 старая строка\r\n"
                                           "2026-10-30 12:00:01 скачиваю web_MVS_v2.0.27.zip (77,0 МБ)...\r\n"
                                           "не строка лога\r\n"
                                           "2026-10-30 12:03:10 ОШИБКА: не удалось узнать последнюю версию\r\n").encode("utf-8"))
    assert updater.update_log_tail(since) == ["скачиваю web_MVS_v2.0.27.zip (77,0 МБ)...", "ОШИБКА: не удалось узнать последнюю версию"]
    assert len(updater.update_log_tail(0)) == 3 and updater.update_log_tail(0, limit=1) == ["ОШИБКА: не удалось узнать последнюю версию"]
    monkeypatch.setattr(updater, "is_frozen", lambda: False)
    assert updater.update_log_tail(since) == [], "из исходников — пусто"


# ───────────────────────────── update.ps1 на песочнице ─────────────────────────────
pytestmark_win = pytest.mark.skipif(sys.platform != "win32", reason="update.ps1 — только Windows PowerShell")


def _fake_addon_zip(path: Path, version: str):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("render.py", "# fake")
        z.writestr("VERSION", version + "\r\n")
        z.writestr("requirements.txt", "# none\r\n")
        z.writestr("setup.bat", "@echo off\r\nmkdir .venv\\Scripts\r\ntype nul > .venv\\Scripts\\python.exe\r\necho setup done\r\npause\r\n")


def _run_update(root: Path, zips: Path, *args):
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(root / "update.ps1"), "-Root", str(root),
           "-NoElevate", "-NoMenu", "-ZipDir", str(zips), *args]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, stdin=subprocess.DEVNULL)
    return p.returncode, p.stdout + p.stderr


@pytestmark_win
def test_update_ps1_parses():
    cmd = ["powershell", "-NoProfile", "-Command",
           "$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile('%s',[ref]$null,[ref]$e); $e.Count" % (ROOT / "update.ps1")]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout.strip()
    assert out == "0", "в update.ps1 ошибки разбора: %s" % out


@pytestmark_win
def test_update_ps1_installs_updates_and_skips_addon(tmp_path):
    root, zips = tmp_path / "каталог с пробелом", tmp_path / "zips"
    root.mkdir(); zips.mkdir()
    (root / "update.ps1").write_bytes((ROOT / "update.ps1").read_bytes())
    _fake_addon_zip(zips / "video_service_v2.0.0.zip", "2.0.0")

    code, out = _run_update(root, zips, "-Components", "video")                    # первая установка: файлы + setup.bat
    assert code == 0, out
    assert (root / "video_service" / "render.py").is_file() and (root / "video_service" / ".venv" / "Scripts" / "python.exe").is_file(), out
    assert (root / "video_service" / "VERSION").read_text().strip() == "2.0.0"
    assert '"components":["video"]' in (root / "update_choice.json").read_text(encoding="utf-8-sig").replace(" ", ""), "выбор запомнен"

    code, out = _run_update(root, zips, "-Components", "video")                    # та же версия — ничего не меняем
    assert code == 0 and "уже последняя версия" in out, out

    (zips / "video_service_v2.0.0.zip").unlink()
    _fake_addon_zip(zips / "video_service_v2.0.1.zip", "2.0.1")
    code, out = _run_update(root, zips, "-Components", "video")                    # новая версия поверх; .venv остаётся
    assert code == 0 and "2.0.0 → 2.0.1" in out, out
    assert (root / "video_service" / "VERSION").read_text().strip() == "2.0.1"
    assert (root / "video_service" / ".venv" / "Scripts" / "python.exe").is_file(), ".venv не трогаем"

    code, out = _run_update(root, zips, "-Components", "bogus")
    assert code == 1 and "неизвестный компонент" in out, out
    code, out = _run_update(root, zips, "-Components", "motion")                   # в папке нет архива motion_service
    assert code == 1 and "нет архива" in out, out
    with zipfile.ZipFile(zips / "cv_service_v2.0.1.zip", "w") as z:
        z.writestr("detector.py", "# fake")
        z.writestr("VERSION", "2.0.1\r\n")
    code, out = _run_update(root, zips, "-Components", "cv")                       # тяжёлая первая установка без -Yes
    assert code == 1 and "-Yes" in out, out


@pytestmark_win
def test_update_ps1_save_download_progress_and_stall(tmp_path):
    """Save-Download (WebClient вместо медленного Invoke-WebRequest): файл скачивается целиком, ход пишется в лог, «повисшая» связь отменяется."""
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            n = 6 * 1024 * 1024
            self.send_response(200)
            self.send_header("Content-Length", str(n))
            self.end_headers()
            if self.path == "/ok":
                for _ in range(6):
                    self.wfile.write(b"x" * (1024 * 1024))
                    self.wfile.flush()
                    time.sleep(0.5)
            else:                                           # /stall: отдал мегабайт и замолчал
                self.wfile.write(b"x" * (1024 * 1024))
                self.wfile.flush()
                time.sleep(15)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        src = (ROOT / "update.ps1").read_text(encoding="utf-8-sig")
        func = src[src.index("function Save-Download"):src.index("# ---------- запуск и остановка приложения")]
        out_ok, out_stall = tmp_path / "ok.bin", tmp_path / "stall.bin"
        script = ('$ErrorActionPreference="Stop"\n[Console]::OutputEncoding=[Text.Encoding]::UTF8\n'
                  'function Log([string]$m,[string]$c=""){ Write-Host ("LOG: "+$m) }\n' + func +
                  "\nSave-Download 'http://127.0.0.1:%d/ok' '%s' (6MB) 1 20\n" % (port, out_ok) +
                  "'DONE ' + (Get-Item '%s').Length\n" % out_ok +
                  "try { Save-Download 'http://127.0.0.1:%d/stall' '%s' (6MB) 1 3; 'NOSTALL' } catch { 'STALL ' + $_.Exception.Message }\n" % (port, out_stall))
        ps1 = tmp_path / "t.ps1"
        ps1.write_text(script, encoding="utf-8-sig")
        p = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=90, stdin=subprocess.DEVNULL)
        out = p.stdout + p.stderr
        assert "DONE %d" % (6 * 1024 * 1024) in out, out
        assert "LOG: скачано" in out and "из 6,0 МБ" in out.replace(".", ","), "ход скачивания пишется в лог: " + out
        assert "STALL скачивание остановилось" in out, out
    finally:
        srv.shutdown()
