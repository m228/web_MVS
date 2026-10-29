"""Дельта-обновление: докачка только изменившихся файлов (delta_update) и откат на полную загрузку (updater._try_delta)."""
import os
import re
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import delta_update as du  # noqa: E402


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        srv = self.server
        if self.path.startswith("/redirect/") and srv.redirect:
            self.send_response(302)
            self.send_header("Location", "/blob/" + self.path[len("/redirect/"):])
            self.end_headers()
            return
        name = self.path.split("/")[-1]
        data = (srv.root / name).read_bytes()
        rng = self.headers.get("Range")
        m = re.fullmatch(r"bytes=(\d+)-(\d+)", rng or "")
        if m and not srv.ignore_range:
            a, b = int(m.group(1)), min(int(m.group(2)), len(data) - 1)
            part = data[a:b + 1]
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (a, b, len(data)))
            self.send_header("Content-Length", str(len(part)))
            self.end_headers()
            self.wfile.write(part)
            srv.sent += len(part)
        else:
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            srv.sent += len(data)


@pytest.fixture
def server(tmp_path):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.root, srv.ignore_range, srv.redirect, srv.sent = tmp_path / "www", False, False, 0
    srv.root.mkdir()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.base = "http://127.0.0.1:%d" % srv.server_address[1]
    yield srv
    srv.shutdown()
    srv.server_close()


def _mkzip(path, files):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in files.items():
            z.writestr(n, d)


def _install(app, files):
    for n, d in files.items():
        p = app / n
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(d)


def _read(zpath):
    with zipfile.ZipFile(zpath) as z:
        assert z.testzip() is None
        return {n: z.read(n) for n in z.namelist() if not n.endswith("/")}


BIG = os.urandom(3 * 1024 * 1024)          # несжимаемый «cv2.dll»
OLD = {"web_MVS.exe": os.urandom(400_000), "_internal/lib.dll": BIG, "_internal/page/a.html": b"<p>old</p>" * 50, "_internal/VERSION": b"1.0.0\n"}
NEW = dict(OLD, **{"web_MVS.exe": os.urandom(400_000), "_internal/page/a.html": b"<p>new</p>" * 50, "_internal/page/b.js": b"x=1", "_internal/VERSION": b"1.0.1\n"})


def test_delta_downloads_only_changed_files_and_builds_full_zip(server, tmp_path):
    app = tmp_path / "app"
    _install(app, OLD)
    _mkzip(server.root / "new.zip", NEW)
    out = tmp_path / "out.zip"
    size = (server.root / "new.zip").stat().st_size
    st = du.fetch_delta(server.base + "/new.zip", size, app, out)
    assert _read(out) == NEW, "собранный архив по содержимому равен архиву релиза"
    assert st["changed"] == 4 and st["files"] == 5 - 0 and st["fetched"] < size / 2, st
    assert server.sent < size / 2, "по сети ушло заметно меньше, чем весь архив"


def test_delta_follows_redirect_to_storage_url(server, tmp_path):
    server.redirect = True
    app = tmp_path / "app"
    _install(app, OLD)
    _mkzip(server.root / "new.zip", NEW)
    st = du.fetch_delta(server.base + "/redirect/new.zip", (server.root / "new.zip").stat().st_size, app, tmp_path / "o.zip")
    assert _read(tmp_path / "o.zip") == NEW and st["changed"] == 4


def test_tampered_local_file_with_same_size_is_refetched(server, tmp_path):
    app = tmp_path / "app"
    _install(app, NEW)
    (app / "_internal/lib.dll").write_bytes(b"\0" * len(BIG))               # размер тот же, содержимое испорчено
    _mkzip(server.root / "new.zip", NEW)
    st = du.fetch_delta(server.base + "/new.zip", (server.root / "new.zip").stat().st_size, app, tmp_path / "o.zip", min_gain=0.0)
    assert _read(tmp_path / "o.zip") == NEW and st["changed"] == 1


def test_nothing_changed_downloads_almost_nothing(server, tmp_path):
    app = tmp_path / "app"
    _install(app, NEW)
    _mkzip(server.root / "new.zip", NEW)
    st = du.fetch_delta(server.base + "/new.zip", (server.root / "new.zip").stat().st_size, app, tmp_path / "o.zip")
    assert st["changed"] == 0 and _read(tmp_path / "o.zip") == NEW


def test_everything_changed_is_not_worth_it(server, tmp_path):
    app = tmp_path / "app"
    _install(app, {n: os.urandom(len(d)) for n, d in NEW.items()})
    _mkzip(server.root / "new.zip", NEW)
    with pytest.raises(du.DeltaError, match="выигрыш"):
        du.fetch_delta(server.base + "/new.zip", (server.root / "new.zip").stat().st_size, app, tmp_path / "o.zip")
    assert not (tmp_path / "o.zip").exists()


def test_server_without_range_support_raises(server, tmp_path):
    server.ignore_range = True
    _mkzip(server.root / "new.zip", NEW)
    with pytest.raises(du.DeltaError, match="Range"):
        du.fetch_delta(server.base + "/new.zip", (server.root / "new.zip").stat().st_size, tmp_path / "app", tmp_path / "o.zip")


def test_updater_falls_back_to_none_when_delta_fails(server, tmp_path, monkeypatch):
    import updater
    server.ignore_range = True
    _mkzip(server.root / "new.zip", NEW)
    monkeypatch.setattr(updater, "_app_dir", lambda: tmp_path / "app")
    part = tmp_path / "x.zip.part"
    asset = {"browser_download_url": server.base + "/new.zip"}
    assert updater._try_delta(asset, "v1.0.1", (server.root / "new.zip").stat().st_size, part, 0) is None and not part.exists()


def test_updater_rejects_delta_zip_of_wrong_version(server, tmp_path, monkeypatch):
    import updater
    app = tmp_path / "app"
    _install(app, OLD)
    _mkzip(server.root / "new.zip", NEW)
    monkeypatch.setattr(updater, "_app_dir", lambda: app)
    part = tmp_path / "x.zip.part"
    asset = {"browser_download_url": server.base + "/new.zip"}
    size = (server.root / "new.zip").stat().st_size
    assert updater._try_delta(asset, "v9.9.9", size, part, 0) is None, "в релизе тег другой — собранному архиву не верим"
    st = updater._try_delta(asset, "v1.0.1", size, part, 0)
    assert st and st["changed"] == 4 and updater._zip_version(part) == "1.0.1"


REAL_OLD, REAL_NEW = ROOT / "dist" / "web_MVS_v2.0.3.zip", ROOT / "dist" / "web_MVS_v2.0.6.zip"


@pytest.mark.skipif(not (REAL_OLD.exists() and REAL_NEW.exists()), reason="нет настоящих архивов в dist")
def test_real_release_archives_2_0_3_to_2_0_6(server, tmp_path):
    app = tmp_path / "app"
    with zipfile.ZipFile(REAL_OLD) as z:
        z.extractall(app)
    (server.root / REAL_NEW.name).write_bytes(REAL_NEW.read_bytes())
    out = tmp_path / "out.zip"
    st = du.fetch_delta(server.base + "/" + REAL_NEW.name, REAL_NEW.stat().st_size, app, out)
    with zipfile.ZipFile(out) as a, zipfile.ZipFile(REAL_NEW) as b:
        assert a.testzip() is None
        assert {i.filename: (i.CRC, i.file_size) for i in a.infolist()} == {i.filename: (i.CRC, i.file_size) for i in b.infolist()}
    assert st["fetched"] < 25_000_000 < REAL_NEW.stat().st_size, st
    print("real 2.0.3->2.0.6: fetched %.1f MB of %.1f MB, files changed %d of %d" % (st["fetched"] / 1e6, REAL_NEW.stat().st_size / 1e6, st["changed"], st["files"]))
