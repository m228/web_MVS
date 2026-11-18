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


def test_parse_manifest_keeps_where_archives_lie():
    m = updater.parse_manifest({"release": "3.0.5", "components": {
        "main": {"version": "3.0.4", "tag": "v3.0.4", "asset": "web_MVS_v3.0.4.zip"},
        "motion": {"version": "3.0.2", "tag": "v3.0.5", "asset": "motion_service_v3.0.2.zip"}, "cv": {"version": "3.0.0"}}})
    assert m["components"] == {"main": "3.0.4", "motion": "3.0.2", "cv": "3.0.0"}
    assert m["assets"] == {"main": {"tag": "v3.0.4", "asset": "web_MVS_v3.0.4.zip"}, "motion": {"tag": "v3.0.5", "asset": "motion_service_v3.0.2.zip"}}


class _Resp:
    def __init__(self, body):
        self._b = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_github(monkeypatch, routes: dict, calls: list):
    def fn(req, timeout=0):
        url = getattr(req, "full_url", req)
        calls.append(url)
        if url not in routes:
            raise OSError("нет маршрута " + url)
        return _Resp(routes[url])
    monkeypatch.setattr(updater.urllib.request, "urlopen", fn)
    for k in ("manifest", "published_at"):              # _fetch_latest_meta пишет их в общий словарь: вернуть как было, не влиять на другие тесты
        monkeypatch.setitem(updater._latest_extra, k, updater._latest_extra.get(k))


def _partial_release_routes():
    """Последний релиз v3.0.5 — частичный: только motion и манифест; приложение 3.0.4 лежит в релизе v3.0.4."""
    manifest = {"release": "3.0.5", "components": {
        "main": {"version": "3.0.4", "tag": "v3.0.4", "asset": "web_MVS_v3.0.4.zip"},
        "motion": {"version": "3.0.2", "tag": "v3.0.5", "asset": "motion_service_v3.0.2.zip"}}}
    return {
        updater.API_LATEST: {"tag_name": "v3.0.5", "published_at": "2026-11-18T10:00:00Z", "body": "", "assets": [
            {"name": "motion_service_v3.0.2.zip", "browser_download_url": "http://x/m.zip"},
            {"name": "components.json", "browser_download_url": "http://x/components.json"}]},
        "http://x/components.json": manifest,
        "https://api.github.com/repos/%s/releases/tags/v3.0.4" % updater.REPO: {"tag_name": "v3.0.4", "assets": [
            {"name": "cv_service_v3.0.4.zip", "browser_download_url": "http://x/cv.zip"},
            {"name": "web_MVS_v3.0.4.zip", "browser_download_url": "http://x/app.zip", "size": 5}]},
    }


def test_partial_release_main_archive_taken_from_older_release(tmp_path, monkeypatch):
    """Клиент, пропустивший v3.0.4: в последнем релизе приложения нет, но манифест говорит, где оно лежит — обновление видно и качается оттуда."""
    calls = []
    _fake_github(monkeypatch, _partial_release_routes(), calls)
    monkeypatch.setattr(updater, "DATA_DIR", tmp_path)
    monkeypatch.setattr(updater, "read_version", lambda: "3.0.3")
    monkeypatch.setattr(updater, "staged_version", lambda: "")
    r = updater.check_latest(quiet=True)
    assert r["latest"] == "3.0.4" and r["update_available"] is True and r["has_asset"] is True
    tag, asset, _ = updater._fetch_latest_meta()
    assert tag == "v3.0.5" and asset["name"] == "web_MVS_v3.0.4.zip" and asset["browser_download_url"] == "http://x/app.zip"
    assert any(c.endswith("/releases/tags/v3.0.4") for c in calls)


def test_partial_release_no_extra_request_when_app_is_current(tmp_path, monkeypatch):
    calls = []
    _fake_github(monkeypatch, _partial_release_routes(), calls)
    monkeypatch.setattr(updater, "DATA_DIR", tmp_path)
    monkeypatch.setattr(updater, "read_version", lambda: "3.0.4")
    monkeypatch.setattr(updater, "staged_version", lambda: "")
    r = updater.check_latest(quiet=True)
    assert r["update_available"] is False and r["latest"] == "3.0.4"
    assert not any("releases/tags" in c for c in calls), "приложение актуально — архив искать незачем"


def test_main_asset_fallbacks(monkeypatch):
    found = []
    monkeypatch.setattr(updater, "release_asset", lambda tag, name: found.append((tag, name)) or None)
    monkeypatch.setattr(updater, "read_version", lambda: "3.0.0")
    old = {"release": "3.0.0", "components": {"main": "3.0.0"}}                            # манифест без tag/asset — как раньше, по маске
    assert updater._main_asset(old, {"name": "web_MVS_v3.0.0.zip"}) == {"name": "web_MVS_v3.0.0.zip"} and not found
    man = {"release": "3.0.5", "components": {"main": "3.0.4"}, "assets": {"main": {"tag": "v3.0.4", "asset": "web_MVS_v3.0.4.zip"}}}
    same = {"name": "web_MVS_v3.0.4.zip"}
    assert updater._main_asset(man, same) is same and not found, "архив уже в последнем релизе"
    assert updater._main_asset(man, None) is None and found == [("v3.0.4", "web_MVS_v3.0.4.zip")], "не нашёлся — None, а не падение"


# ───────────────────────────── scripts/make_manifest.py ─────────────────────────────
def _repo(tmp_path, release="3.0.1", main="3.0.0", **svc):
    (tmp_path / "RELEASE").write_text(release, encoding="utf-8")
    (tmp_path / "VERSION").write_text(main, encoding="utf-8")
    versions = {"cv": "3.0.0", "video": "3.0.0", "motion": "3.0.0", "level": "3.0.0"}
    versions.update(svc)
    (tmp_path / "component_versions.json").write_text(json.dumps(versions), encoding="utf-8")
    return tmp_path


def _zips(dist: Path, code="a", **versions):
    """Архивы сервисов, названные по версии компонента (по умолчанию 3.0.0), и main, если указана его версия (main="3.0.5")."""
    dist.mkdir(exist_ok=True)
    ver = {"cv": "3.0.0", "video": "3.0.0", "motion": "3.0.0", "level": "3.0.0"}
    ver.update({k: v for k, v in versions.items() if k != "main"})
    for key in mm.SERVICES:
        with zipfile.ZipFile(dist / mm.asset_name(key, ver[key]), "w") as z:
            z.writestr("server.py", "# " + code)
            z.writestr("VERSION", "x\n")
    if versions.get("main"):
        with zipfile.ZipFile(dist / mm.asset_name("main", versions["main"]), "w") as z:
            z.writestr("_internal/VERSION", versions["main"])


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
    _zips(dist)
    cur = mm.build_manifest(dist, repo)
    assert cur["release"] == "3.0.1" and cur["components"]["main"] == {"version": "3.0.0"} and cur["components"]["cv"]["version"] == "3.0.0"
    assert mm.check_against_prev(cur, None) == [], "первого манифеста ещё нет — проверять не с чем"
    prev = json.loads(json.dumps(cur))
    prev["release"] = "3.0.0"
    assert mm.check_against_prev(cur, prev) == []                                          # ничего не менялось
    _zips(dist, code="b")                                                                  # код всех сервисов изменился, версии те же → стоп
    errs = mm.check_against_prev(mm.build_manifest(dist, repo), prev)
    assert len(errs) == 4 and "версия та же" in errs[0], errs
    _repo(tmp_path, cv="3.1.0")                                                            # поднять версию cv → ошибка только у остальных
    _zips(dist, code="b", cv="3.1.0")
    errs = mm.check_against_prev(mm.build_manifest(dist, tmp_path), prev)
    assert len(errs) == 3 and not any(e.startswith("cv:") for e in errs)
    assert any("RELEASE" in e for e in mm.check_against_prev(cur, json.loads(json.dumps(cur)))), "RELEASE не вырос"


def test_main_writes_components_json(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(mm, "ROOT", repo)
    dist = tmp_path / "dist"
    _zips(dist)
    prev = tmp_path / "prev.json"
    prev.write_text(json.dumps({"release": "3.0.0", "components": {"main": {"version": "3.0.0"}}}), encoding="utf-8")
    assert mm.main(["--dist", str(dist), "--prev", str(prev)]) == 0
    data = json.loads((dist / "components.json").read_text(encoding="utf-8"))
    assert data["release"] == "3.0.1" and set(data["components"]) == {"main", "cv", "video", "motion", "level"}
    prev.write_text(json.dumps({"release": "3.0.1", "components": {}}), encoding="utf-8")
    assert mm.main(["--dist", str(dist), "--prev", str(prev)]) == 1, "RELEASE не вырос — сборка останавливается"


def _prev_v304():
    """Манифест выложенного релиза v3.0.4: без tag/asset (их ещё не было), у сервисов хеши."""
    return {"release": "3.0.4", "components": {
        "main": {"version": "3.0.4"}, "cv": {"version": "3.0.0", "sha": "c"}, "video": {"version": "3.0.0", "sha": "v"},
        "motion": {"version": "3.0.1", "sha": "m"}, "level": {"version": "3.0.0", "sha": "l"}}}


def test_make_plan_builds_only_raised_versions(tmp_path):
    prev = _prev_v304()
    plan = mm.make_plan(_repo(tmp_path, release="3.0.5", main="3.0.4", motion="3.0.2"), prev)
    assert plan["main"] is False and plan["motion"] is True
    assert not plan["cv"] and not plan["video"] and not plan["level"]
    assert plan["versions"]["motion"] == "3.0.2" and plan["versions"]["main"] == "3.0.4"
    assert mm.make_plan(_repo(tmp_path, release="3.0.5", main="3.0.10"), prev)["main"] is True, "3.0.10 > 3.0.4 (числами, не строкой)"
    allp = mm.make_plan(tmp_path, None)
    assert all(allp[k] for k in ("main", "cv", "video", "motion", "level")), "нет прошлого манифеста — собираем всё"
    cmd = tmp_path / "plan.cmd"
    mm.write_plan_cmd(cmd, mm.make_plan(_repo(tmp_path, release="3.0.5", main="3.0.4", motion="3.0.2"), prev))
    text = cmd.read_text(encoding="ascii")
    assert 'set "BUILD_main=0"' in text and 'set "BUILD_motion=1"' in text and 'set "VER_motion=3.0.2"' in text


def test_partial_release_only_changed_zip_is_published(tmp_path, monkeypatch, capsys):
    """Изменился только motion: в релиз уходит один его архив + манифест; у остальных tag/asset — прошлый релиз."""
    repo = _repo(tmp_path, release="3.0.5", main="3.0.4", cv="3.0.0", motion="3.0.2")
    monkeypatch.setattr(mm, "ROOT", repo)
    dist = tmp_path / "dist"
    _zips(dist, motion="3.0.2")                                                  # build.bat пакует все сервисы, main не собирается
    prev = tmp_path / "prev.json"
    prev_data = _prev_v304()
    cur = mm.build_manifest(dist, repo)
    for key in ("cv", "video", "level"):
        prev_data["components"][key]["sha"] = cur["components"][key]["sha"]      # код тот же, что в прошлом релизе
    prev.write_text(json.dumps(prev_data), encoding="utf-8")
    assert mm.main(["--dist", str(dist), "--prev", str(prev)]) == 0, capsys.readouterr().out
    data = json.loads((dist / "components.json").read_text(encoding="utf-8"))
    c = data["components"]
    assert c["motion"]["version"] == "3.0.2" and c["motion"]["tag"] == "v3.0.5" and c["motion"]["asset"] == "motion_service_v3.0.2.zip"
    assert c["main"] == {"version": "3.0.4", "tag": "v3.0.4", "asset": "web_MVS_v3.0.4.zip"}, "приложение не менялось — архив в прошлом релизе"
    assert c["cv"]["tag"] == "v3.0.4" and c["cv"]["asset"] == "cv_service_v3.0.4.zip" and c["cv"]["sha"] == prev_data["components"]["cv"]["sha"]
    assert sorted(p.name for p in dist.glob("*.zip")) == ["motion_service_v3.0.2.zip"], "лишние архивы убраны из dist"
    assert (dist / "release_files.txt").read_text(encoding="utf-8").split() == ["dist\\motion_service_v3.0.2.zip", "dist\\components.json"]
    notes = (dist / "release_notes.txt").read_text(encoding="utf-8")
    assert "motion 3.0.2" in notes.splitlines()[0] and "cv 3.0.0" in notes and "приложение 3.0.4" in notes.splitlines()[1]


def test_partial_release_with_main_and_nothing_changed(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path, release="3.0.5", main="3.0.6")
    monkeypatch.setattr(mm, "ROOT", repo)
    dist = tmp_path / "dist"
    _zips(dist, main="3.0.6")
    prev = tmp_path / "prev.json"
    prev_data = _prev_v304()
    prev_data["components"]["motion"]["version"] = "3.0.0"
    cur = mm.build_manifest(dist, repo)
    for key in ("cv", "video", "motion", "level"):
        prev_data["components"][key]["sha"] = cur["components"][key]["sha"]
    prev.write_text(json.dumps(prev_data), encoding="utf-8")
    assert mm.main(["--dist", str(dist), "--prev", str(prev)]) == 0
    files = (dist / "release_files.txt").read_text(encoding="utf-8").split()
    assert files == ["dist\\web_MVS_v3.0.6.zip", "dist\\components.json"]
    # ничего не изменилось (версии как в прошлом релизе) → выкладывать нечего
    repo2 = _repo(tmp_path, release="3.0.6", main="3.0.4", motion="3.0.1")
    monkeypatch.setattr(mm, "ROOT", repo2)
    prev_data["components"]["motion"]["version"] = "3.0.1"
    prev.write_text(json.dumps(prev_data), encoding="utf-8")
    dist2 = tmp_path / "dist2"
    _zips(dist2, motion="3.0.1")
    capsys.readouterr()
    assert mm.main(["--dist", str(dist2), "--prev", str(prev)]) == 1
    assert "выкладывать нечего" in capsys.readouterr().out


def test_partial_release_stops_if_service_code_changed_without_version(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path, release="3.0.5", main="3.0.4", motion="3.0.1")
    monkeypatch.setattr(mm, "ROOT", repo)
    dist = tmp_path / "dist"
    _zips(dist, code="new", motion="3.0.1")
    prev = tmp_path / "prev.json"
    prev.write_text(json.dumps(_prev_v304()), encoding="utf-8")                  # хеш cv в прошлом релизе другой, версия та же
    assert mm.main(["--dist", str(dist), "--prev", str(prev)]) == 1
    assert "версия та же" in capsys.readouterr().out


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


@pytest.mark.skipif(sys.platform != "win32", reason="update.ps1 - только Windows PowerShell")
def test_update_ps1_finds_component_asset_in_older_release(tmp_path):
    """Get-ComponentAsset: архив по tag/asset манифеста (в последнем релизе или в старом — через releases/tags), без записи — по маске в последнем."""
    import http.server
    import threading

    manifest = {"release": "3.0.5", "components": {
        "main": {"version": "3.0.4", "tag": "v3.0.4", "asset": "web_MVS_v3.0.4.zip"},
        "cv": {"version": "3.0.0", "tag": "v3.0.4", "asset": "cv_service_v3.0.4.zip"},
        "motion": {"version": "3.0.2", "tag": "v3.0.5", "asset": "motion_service_v3.0.2.zip"},
        "video": {"version": "3.0.0"}}}
    body = ("﻿" + json.dumps(manifest)).encode("utf-8")

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
        script = ('$ErrorActionPreference="Stop"\n[Console]::OutputEncoding=[Text.Encoding]::UTF8\n$repo = "o/r"\n'
                  'function Log([string]$m,[string]$c=""){ Write-Host ("LOG: "+$m) }\n'
                  'function Test-VersionString([string]$v) { return ($v -match "^\\d+(\\.\\d+){1,3}$") }\n'
                  'function Invoke-WithRetry([scriptblock]$Action, [int]$Tries = 3) { return (& $Action) }\n'
                  'function Invoke-RestMethod { param($Uri, $Headers, [switch]$UseBasicParsing); Write-Host ("REST: " + $Uri)\n'
                  '  return [pscustomobject]@{ tag_name = "v3.0.4"; assets = @([pscustomobject]@{ name = "cv_service_v3.0.4.zip" }, [pscustomobject]@{ name = "web_MVS_v3.0.4.zip" }) } }\n'
                  "$script:relCache = $null\n"
                  'function Get-Release { return [pscustomobject]@{ tag_name = "v3.0.5"; assets = @([pscustomobject]@{ name = "motion_service_v3.0.2.zip" }, '
                  '[pscustomobject]@{ name = "video_service_v3.0.5.zip" }, [pscustomobject]@{ name = "components.json"; browser_download_url = "%s" }) } }\n' % url
                  + func + "\n"
                  "foreach ($k in 'motion','cv','main','video','level') { $d = @{ Key = $k; Asset = ($k -replace '^main$','web_MVS') + '*.zip' }\n"
                  "  $a = Get-ComponentAsset $d; 'asset ' + $k + '=' + $(if ($a) { $a.name } else { 'null' }) }\n")
        ps1 = tmp_path / "t.ps1"
        ps1.write_text(script, encoding="utf-8-sig")
        p = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120, stdin=subprocess.DEVNULL)
        out = p.stdout + p.stderr
        assert "asset motion=motion_service_v3.0.2.zip" in out, out                  # лежит в последнем релизе
        assert "asset cv=cv_service_v3.0.4.zip" in out and "asset main=web_MVS_v3.0.4.zip" in out, out   # в старом релизе v3.0.4
        assert out.count("REST: https://api.github.com/repos/o/r/releases/tags/v3.0.4") == 1, "релиз v3.0.4 запрашивается один раз"
        assert "asset video=video_service_v3.0.5.zip" in out, out                   # записи tag/asset нет — по маске в последнем релизе
        assert "asset level=null" in out, out
    finally:
        srv.shutdown()
