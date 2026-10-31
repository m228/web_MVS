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
def test_update_ps1_save_download_resumes_after_drops(tmp_path):
    """Save-Download докачивает с места обрыва (HTTP Range): обрыв связи, редирект, сервер без Range, зависание, неверная sha256."""
    import hashlib
    import http.server
    import threading

    data = bytes(i % 251 for i in range(4 * 1024 * 1024))
    sha = hashlib.sha256(data).hexdigest()
    hits = {}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            path = self.path
            n = hits[path] = hits.get(path, 0) + 1
            if path == "/redir":
                self.send_response(302)
                self.send_header("Location", "/drop2")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            rng = self.headers.get("Range")
            start = 0
            if rng and path != "/norange":
                start = int(rng.split("=")[1].split("-")[0])
                self.send_response(206)
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, len(data) - 1, len(data)))
            else:
                self.send_response(200)
            self.send_header("Content-Length", str(len(data) - start))
            self.end_headers()
            body = data[start:]
            if path in ("/drop", "/drop2", "/norange") and n == 1:      # первая попытка: мегабайт и обрыв
                self.wfile.write(body[:1024 * 1024])
                self.wfile.flush()
                self.close_connection = True
                return
            if path == "/stall":                                        # в первый раз отдал полмегабайта, дальше молчит
                if n == 1:
                    self.wfile.write(body[:512 * 1024])
                    self.wfile.flush()
                time.sleep(20)
                return
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        src = (ROOT / "update.ps1").read_text(encoding="utf-8-sig")
        func = src[src.index("function Receive-Part"):src.index("# ---------- запуск и остановка приложения")]
        dl = tmp_path / "dl"
        base = "http://127.0.0.1:%d" % port
        size = len(data)
        calls = []
        for name, path, sh in [("a_drop", "/drop", sha), ("b_redir", "/redir", sha), ("c_norange", "/norange", sha)]:
            calls.append("Save-Download '%s%s' '%s' %d '%s' 1 10 3 1\n'%s ' + (Get-FileHash '%s' -Algorithm SHA256).Hash.ToLower()" % (
                base, path, tmp_path / (name + ".bin"), size, sh, name, tmp_path / (name + ".bin")))
        calls.append("try { Save-Download '%s/bad' '%s' %d '%s' 1 10 3 1; 'NOBAD' } catch { 'BAD ' + $_.Exception.Message }" % (
            base, tmp_path / "bad.bin", size, "0" * 64))
        calls.append("try { Save-Download '%s/stall' '%s' %d '' 1 2 2 1; 'NOSTALL' } catch { 'STALL ' + $_.Exception.Message }" % (
            base, tmp_path / "stall.bin", size))
        script = ('$ErrorActionPreference="Stop"\n[Console]::OutputEncoding=[Text.Encoding]::UTF8\n'
                  'function Log([string]$m,[string]$c=""){ Write-Host ("LOG: "+$m) }\n'
                  "$script:DlDir = '%s'\n" % dl + func + "\n" + "\n".join(calls) + "\n")
        ps1 = tmp_path / "t.ps1"
        ps1.write_text(script.replace("\\n", "\n"), encoding="utf-8-sig")
        p = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180, stdin=subprocess.DEVNULL)
        out = p.stdout + p.stderr
        for name in ("a_drop", "b_redir", "c_norange"):
            assert "%s %s" % (name, sha) in out, out                    # файл целиком и sha256 сходится
        assert "докачиваю с 1,0 МБ" in out.replace(".", ","), "после обрыва докачка идёт с места обрыва: " + out
        assert hits.get("/drop2", 0) >= 2, "редирект + Range: " + str(hits)
        assert "LOG: контрольная сумма подтверждена" in out, out
        assert "BAD контрольная сумма архива не совпала" in out, out
        assert "STALL не удалось докачать" in out and "следующий запуск продолжит" in out, out
        assert (dl / "stall.bin.part").is_file(), "недокачанное сохраняется для следующего запуска"
        assert not (tmp_path / "stall.bin").exists()
    finally:
        srv.shutdown()
