"""Версии компонентов: манифест релиза (updater.parse_manifest / addons_due / check_latest) и сборка (scripts/make_manifest.py)."""
import json
import sys
import zipfile
from pathlib import Path

import updater

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import make_manifest as mm  # noqa: E402


def test_parse_manifest():
    m = updater.parse_manifest({"release": "v3.0.1", "components": {"main": {"version": "3.0.1"}, "cv": {"version": "3.1.0", "sha": "x"},
                                                                    "bad": {"version": "oops"}, "video": "3.0.0"}})
    assert m == {"release": "3.0.1", "components": {"main": "3.0.1", "cv": "3.1.0", "video": "3.0.0"}}
    assert updater.parse_manifest({"components": {"cv": {"version": "3.0.0"}}}) is None, "без main — не манифест"
    assert updater.parse_manifest(None) is None and updater.parse_manifest({"components": []}) is None


def _install(tmp_path, monkeypatch, versions: dict):
    monkeypatch.setattr(updater, "DATA_DIR", tmp_path)
    for a in updater.ADDONS:
        if a["key"] in versions:
            d = tmp_path / a["dir"]
            d.mkdir(exist_ok=True)
            (d / a["marker"]).write_text("", encoding="utf-8")
            if versions[a["key"]]:
                (d / "VERSION").write_text(versions[a["key"]] + "\n", encoding="utf-8")


def test_addons_due_by_manifest_only_changed_installed(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, {"cv": "3.0.0", "video": "3.0.0", "motion": "3.0.0"})     # level не установлен
    monkeypatch.setitem(updater._latest_extra, "manifest", {"release": "3.0.1", "components": {
        "main": "3.0.1", "cv": "3.1.0", "video": "3.0.0", "motion": "3.0.0", "level": "3.0.5"}})
    assert updater.addons_due("v3.0.1") == [{"key": "cv", "current": "3.0.0", "latest": "3.1.0"}]


def test_addons_due_without_manifest_falls_back_to_tag(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, {"cv": "2.1.88", "video": ""})
    monkeypatch.setitem(updater._latest_extra, "manifest", None)
    due = {d["key"]: d for d in updater.addons_due("v3.0.0")}
    assert set(due) == {"cv", "video"} and due["video"]["current"] == "" and due["cv"]["latest"] == "3.0.0"


def test_check_latest_uses_manifest_main_version_and_flags(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, {"motion": "3.0.0"})
    monkeypatch.setattr(updater, "read_version", lambda: "3.0.0")
    monkeypatch.setattr(updater, "staged_version", lambda: "")
    monkeypatch.setattr(updater, "_fetch_latest_meta", lambda max_age=0.0: ("v3.0.7", {"name": "web_MVS_v3.0.7.zip"}, "Fix [URGENT] camera"))
    monkeypatch.setitem(updater._latest_extra, "published_at", "2026-11-16T10:00:00Z")
    monkeypatch.setitem(updater._latest_extra, "manifest", {"release": "3.0.7", "components": {"main": "3.0.0", "motion": "3.0.2"}})
    r = updater.check_latest(quiet=True)
    assert r["latest"] == "3.0.0" and r["update_available"] is False, "приложение не менялось — версия по манифесту, а не по тегу"
    assert r["urgent"] is True and r["published_at"] == "2026-11-16T10:00:00Z" and r["manifest"] is True
    assert r["addons_due"] == [{"key": "motion", "current": "3.0.0", "latest": "3.0.2"}]


# ───────────────────────────── scripts/make_manifest.py ─────────────────────────────
def _repo(tmp_path, release="3.0.1", main="3.0.0", **svc):
    (tmp_path / "RELEASE").write_text(release, encoding="utf-8")
    (tmp_path / "VERSION").write_text(main, encoding="utf-8")
    versions = {"cv": "3.0.0", "video": "3.0.0", "motion": "3.0.0", "level": "3.0.0"}
    versions.update(svc)
    (tmp_path / "component_versions.json").write_text(json.dumps(versions), encoding="utf-8")
    return tmp_path


def _zips(dist: Path, release: str, code="a"):
    dist.mkdir(exist_ok=True)
    for d in mm.SERVICES.values():
        with zipfile.ZipFile(dist / ("%s_v%s.zip" % (d, release)), "w") as z:
            z.writestr("server.py", "# " + code)
            z.writestr("VERSION", "x\n")


def test_zip_sha_ignores_version_file_and_dates(tmp_path):
    a, b = tmp_path / "a.zip", tmp_path / "b.zip"
    for p, ver in ((a, "1"), (b, "2")):
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("server.py", "same")
            z.writestr("VERSION", ver)
    assert mm.zip_sha(a) == mm.zip_sha(b)
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("server.py", "changed")
    assert mm.zip_sha(a) != mm.zip_sha(b)


def test_build_manifest_and_checks(tmp_path):
    repo = _repo(tmp_path)
    dist = tmp_path / "dist"
    _zips(dist, "3.0.1")
    cur = mm.build_manifest(dist, repo)
    assert cur["release"] == "3.0.1" and cur["components"]["main"] == {"version": "3.0.0"} and cur["components"]["cv"]["version"] == "3.0.0"
    assert mm.check_against_prev(cur, None) == [], "первого манифеста ещё нет — проверять не с чем"
    prev = json.loads(json.dumps(cur))
    prev["release"] = "3.0.0"
    assert mm.check_against_prev(cur, prev) == []                                          # ничего не менялось
    _zips(dist, "3.0.1", code="b")                                                         # код всех сервисов изменился, версии те же → стоп
    errs = mm.check_against_prev(mm.build_manifest(dist, repo), prev)
    assert len(errs) == 4 and "версия та же" in errs[0], errs
    _repo(tmp_path, cv="3.1.0")                                                            # поднять версию cv → ошибка только у остальных
    errs = mm.check_against_prev(mm.build_manifest(dist, tmp_path), prev)
    assert len(errs) == 3 and not any(e.startswith("cv:") for e in errs)
    assert any("RELEASE" in e for e in mm.check_against_prev(cur, json.loads(json.dumps(cur)))), "RELEASE не вырос"


def test_main_writes_components_json(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(mm, "ROOT", repo)
    dist = tmp_path / "dist"
    _zips(dist, "3.0.1")
    prev = tmp_path / "prev.json"
    prev.write_text(json.dumps({"release": "3.0.0", "components": {"main": {"version": "3.0.0"}}}), encoding="utf-8")
    assert mm.main(["--dist", str(dist), "--prev", str(prev)]) == 0
    data = json.loads((dist / "components.json").read_text(encoding="utf-8"))
    assert data["release"] == "3.0.1" and set(data["components"]) == {"main", "cv", "video", "motion", "level"}
    prev.write_text(json.dumps({"release": "3.0.1", "components": {}}), encoding="utf-8")
    assert mm.main(["--dist", str(dist), "--prev", str(prev)]) == 1, "RELEASE не вырос — сборка останавливается"


def test_write_service_versions(tmp_path):
    repo = _repo(tmp_path, cv="3.2.1")
    (repo / "cv_service").mkdir()
    mm.write_service_versions(repo)
    assert (repo / "cv_service" / "VERSION").read_text().strip() == "3.2.1"
    assert not (repo / "video_service").exists(), "нет папки — не создаём"


def test_repo_versions_are_consistent():
    v = mm.load_versions(ROOT)
    assert set(v) == {"release", "main", "cv", "video", "motion", "level"}


# ───────────────────────────── update.ps1: версии по манифесту ─────────────────────────────
import subprocess  # noqa: E402

import pytest  # noqa: E402


@pytest.mark.skipif(sys.platform != "win32", reason="update.ps1 - только Windows PowerShell")
def test_update_ps1_reads_versions_from_manifest(tmp_path):
    """Get-ManifestVersion берёт версию компонента из components.json релиза (ассет octet-stream, с BOM); нет манифеста — $null (версия по тегу)."""
    import http.server
    import threading

    body = ("﻿" + json.dumps({"release": "3.0.4", "components": {"main": {"version": "3.0.0"}, "cv": {"version": "3.1.0"},
                                                                       "motion": {"version": "oops"}}})).encode("utf-8")

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        src = (ROOT / "update.ps1").read_text(encoding="utf-8-sig")
        func = src[src.index("$script:manifest = $null"):src.index("function Get-ZipDirVersion")]
        url = "http://127.0.0.1:%d/components.json" % srv.server_address[1]
        script = ('$ErrorActionPreference="Stop"\n[Console]::OutputEncoding=[Text.Encoding]::UTF8\n'
                  'function Log([string]$m,[string]$c=""){ Write-Host ("LOG: "+$m) }\n'
                  'function Test-VersionString([string]$v) { return ($v -match "^\\d+(\\.\\d+){1,3}$") }\n'
                  'function Invoke-WithRetry([scriptblock]$Action, [int]$Tries = 3) { return (& $Action) }\n'
                  "$script:relCache = $null\n"
                  'function Get-Release { return [pscustomobject]@{ tag_name = "v3.0.4"; assets = @([pscustomobject]@{ name = "components.json"; browser_download_url = "%s" }) } }\n' % url
                  + func + "\n"
                  "'main=' + (Get-ManifestVersion 'main')\n'cv=' + (Get-ManifestVersion 'cv')\n"
                  "'motion=' + $(if ($null -eq (Get-ManifestVersion 'motion')) { 'null' } else { 'x' })\n"
                  "'video=' + $(if ($null -eq (Get-ManifestVersion 'video')) { 'null' } else { 'x' })\n")
        ps1 = tmp_path / "t.ps1"
        ps1.write_text(script, encoding="utf-8-sig")
        p = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120, stdin=subprocess.DEVNULL)
        out = p.stdout + p.stderr
        assert "main=3.0.0" in out and "cv=3.1.0" in out and "motion=null" in out and "video=null" in out, out
    finally:
        srv.shutdown()
