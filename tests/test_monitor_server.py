"""monitor_server: хранилище (токены, дедупликация, достройка событий из снимка, «нет пульса»), HTTP-слой (пароль, токен, защита записи)
и сквозной прогон: настоящий клиент sysmon.Agent → настоящий приёмник (uvicorn на свободном порту) → карточка на панели."""
import base64
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

MS = Path(__file__).resolve().parent.parent / "monitor_server"
sys.path.insert(0, str(MS))

import importlib.util  # noqa: E402
import struct  # noqa: E402

import monitor_app  # noqa: E402
import monitor_store as ms  # noqa: E402

# точка входа загружается под своим именем: у других сайдкаров тоже есть server.py
_spec = importlib.util.spec_from_file_location("monitor_server_entry", MS / "server.py")
entry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(entry)

PANEL = (MS / "panel.html").read_text(encoding="utf-8")
ASSETS = entry.load_assets()
PASSWORD = "p4ssw0rd-Test"


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def store(tmp_path):
    clk = Clock()
    s = ms.Store(tmp_path / "monitor.db", clock=clk)
    s.clk = clk
    yield s
    s.close()


def pulse(alerts=None, events=None, start=None, interval=300, text="Кир · ПК работает 1 ч"):
    snap = {"plant": "Кир", "app_version": "2.0.34", "interval_s": interval, "text": text, "alerts": alerts or []}
    if start is not None:
        snap["start"] = start
    return {"schema": 1, "sent_t": 0, "snapshot": snap, "events": events or []}


def ev(eid, t, text="x", kind="alert", level="high", **data):
    return {"eid": eid, "t": t, "kind": kind, "level": level, "text": text, "data": data}


# ------------------------------------------------------------------------------------------------------------------ заводы

def test_token_is_stored_as_hash_and_found(store):
    pid, token = store.create_plant("  Кир  ")
    assert store.plant_by_token(token)["id"] == pid and store.plant_by_token(token)["name"] == "Кир"
    assert store.plant_by_token("чужой") is None and store.plant_by_token("") is None
    raw = store.db.execute("SELECT token_hash FROM plants").fetchone()[0]
    assert token not in raw and raw == ms.hash_token(token) and len(raw) == 64


def test_plant_name_rules(store):
    store.create_plant("Кир")
    for bad in ("", "   ", "x" * 41, "Кир"):
        with pytest.raises(ValueError):
            store.create_plant(bad)


def test_rotate_token_and_delete(store):
    pid, old = store.create_plant("Буи")
    new = store.rotate_token(pid)
    assert store.plant_by_token(old) is None and store.plant_by_token(new)["id"] == pid
    store.ingest(pid, pulse(alerts=[{"code": "a", "level": "high", "text": "t", "since": 1}], events=[ev("e1", 1)]))
    assert store.delete_plant(pid) is True and store.plants_view() == []
    assert store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0 and store.db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0
    with pytest.raises(LookupError):
        store.rotate_token(pid)
    with pytest.raises(LookupError):
        store.ingest(pid, pulse())


# --------------------------------------------------------------------------------------------------------------------- приём

def test_ingest_validates_format(store):
    pid, _ = store.create_plant("Кир")
    for bad in (None, [], {"schema": 2, "snapshot": {}}, {"schema": 1}, {"schema": 1, "snapshot": {}, "events": "x"}):
        with pytest.raises(ValueError):
            store.ingest(pid, bad)


def test_events_dedup_by_eid(store):
    pid, _ = store.create_plant("Кир")
    e = ev("same-id", store.clk.t - 5, "Камера: экспозиция 5000 вместо 170", code="camera_exposure", state="open")
    assert store.ingest(pid, pulse(events=[e]))["new_events"] == 1
    assert store.ingest(pid, pulse(events=[e]))["new_events"] == 0, "повторная доставка после обрыва дублей не даёт"
    assert len(store.events(pid)) == 1


def test_bad_event_fields_are_sanitized(store):
    pid, _ = store.create_plant("Кир")
    junk = [{"eid": "a", "t": "вчера", "kind": "k" * 100, "level": "ужас", "text": "т" * 1000, "data": "не словарь"},
            {"no": "eid"}, "строка", {"eid": "far", "t": store.clk.t + 10 * 86400, "text": "часы врут"}]
    store.ingest(pid, pulse(events=junk))
    rows = {e["text"][:5]: e for e in store.events(pid)}
    assert len(store.events(pid)) == 2
    long_ev = [e for e in store.events(pid) if e["text"].startswith("т")][0]
    assert len(long_ev["text"]) == ms.TEXT_MAX and long_ev["level"] == "info" and long_ev["t"] == store.clk.t
    assert [e for e in store.events(pid) if e["text"] == "часы врут"][0]["t"] == store.clk.t, "время дальше недели — берём время приёма"


def test_start_event_built_from_snapshot_once(store):
    pid, _ = store.create_plant("Кир")
    start = {"t": store.clk.t - 100, "reason": "app_restart", "level": "critical", "text": "Приложение перезапущено"}
    assert store.ingest(pid, pulse(start=start))["new_events"] == 1
    assert store.ingest(pid, pulse(start=start))["new_events"] == 0
    e = store.events(pid)[0]
    assert e["kind"] == "start" and e["level"] == "critical"
    # а если событие «start» пришло от самого завода — второго не появляется
    pid2, _ = store.create_plant("Буи")
    start2 = {"t": store.clk.t - 50, "reason": "pc_reboot", "level": "critical", "text": "ПК перезагрузился"}
    store.ingest(pid2, pulse(start=start2, events=[ev("cl-1", start2["t"], "ПК перезагрузился", kind="start", level="critical")]))
    assert len(store.events(pid2)) == 1


def test_alert_open_close_from_snapshot(store):
    pid, _ = store.create_plant("Кир")
    a = {"code": "camera_exposure", "level": "high", "text": "Камера: экспозиция 5000 вместо 170", "since": store.clk.t - 30}
    store.ingest(pid, pulse(alerts=[a]))
    view = store.plants_view()[0]
    assert [x["code"] for x in view["alerts"]] == ["camera_exposure"]
    assert [(e["kind"], e["state"]) for e in view["events"]] == [("alert", "open")]
    store.clk.t += 300
    store.ingest(pid, pulse(alerts=[a]))                        # тревога держится — новых записей нет
    assert len(store.events(pid)) == 1
    store.clk.t += 300
    store.ingest(pid, pulse(alerts=[]))
    view = store.plants_view()[0]
    assert view["alerts"] == []
    last = store.events(pid)[0]
    assert last["state"] == "closed" and last["text"].startswith("Снято:") and last["level"] == "info"


def test_alert_events_from_client_are_not_doubled(store):
    pid, _ = store.create_plant("Кир")
    since = store.clk.t - 20
    a = {"code": "overexposed", "level": "high", "text": "Кадры пересвечены", "since": since}
    store.ingest(pid, pulse(alerts=[a], events=[ev("o1", since, "Кадры пересвечены", code="overexposed", state="open")]))
    store.clk.t += 600
    store.ingest(pid, pulse(alerts=[], events=[ev("o2", store.clk.t - 5, "Снято: Кадры пересвечены", level="info", code="overexposed", state="closed")]))
    assert [e["state"] for e in store.events(pid)] == ["closed", "open"]


def test_snapshot_and_interval_saved(store):
    pid, _ = store.create_plant("Кир")
    assert store.plants_view()[0]["status"] == "waiting" and store.plants_view()[0]["snapshot"] is None
    store.ingest(pid, pulse(interval=600, text="привет"))
    v = store.plants_view()[0]
    assert v["status"] == "online" and v["interval_s"] == 600 and v["snapshot"]["text"] == "привет" and v["last_seen_t"] == store.clk.t
    store.ingest(pid, pulse(interval=5))                         # нелепый интервал не принимаем
    assert store.plants_view()[0]["interval_s"] == 600


# ------------------------------------------------------------------------------------------------------------ «нет пульса»

def test_offline_after_two_missed_pulses_and_back(store):
    store.started_at = store.clk.t - 10 * 86400                   # приёмник работает давно
    pid, _ = store.create_plant("Кир")
    store.ingest(pid, pulse(interval=300))
    limit = ms.offline_limit(300)
    assert limit == 720
    store.clk.t += limit - 1
    assert store.check_offline() == [] and store.plants_view()[0]["status"] == "online"
    store.clk.t += 2
    out = store.check_offline()
    assert len(out) == 1 and out[0]["level"] == "critical" and out[0]["plant"] == "Кир" and "12 мин" in out[0]["text"]
    assert store.plants_view()[0]["status"] == "offline"
    assert store.check_offline() == [], "тревога об одном и том же молчании — один раз"
    store.clk.t += 600
    store.ingest(pid, pulse())
    v = store.plants_view()[0]
    assert v["status"] == "online" and v["offline_since"] is None
    assert [e["kind"] for e in store.events(pid)][:2] == ["online", "offline"]
    assert "Связь восстановилась" in store.events(pid)[0]["text"]


def test_never_seen_plant_is_not_offline(store):
    store.started_at = store.clk.t - 10 * 86400
    store.create_plant("Новый")
    store.clk.t += 10 * 86400
    assert store.check_offline() == [] and store.plants_view()[0]["status"] == "waiting"


def test_grace_after_receiver_restart(store):
    pid, _ = store.create_plant("Кир")
    store.ingest(pid, pulse(interval=300))
    store.clk.t += 3600                                            # сам приёмник не работал час и только что запущен
    store.started_at = store.clk.t
    assert store.check_offline() == [] and store.plants_view()[0]["status"] == "checking", "сначала ждём пульса, а не кричим"
    store.clk.t += ms.offline_limit(300) + 1
    assert len(store.check_offline()) == 1 and store.plants_view()[0]["status"] == "offline"


def test_purge_old_events(store):
    pid, _ = store.create_plant("Кир")
    store.ingest(pid, pulse(events=[ev("old", store.clk.t - 86400, "старое")]))
    store.clk.t += 89 * 86400
    store.ingest(pid, pulse(events=[ev("new", store.clk.t - 86400, "свежее")]))
    store.clk.t += 2 * 86400
    assert store.purge() == 1 and [e["text"] for e in store.events(pid)] == ["свежее"]


# ------------------------------------------------------------------------------------------------------------- HTTP-слой

class Srv:
    def __init__(self, store, **kw):
        import uvicorn
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.base = "http://127.0.0.1:%d" % self.port
        self.app = monitor_app.create_app(store, PASSWORD, "9.9.9", PANEL, watchdog=False, assets=ASSETS, **kw)
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.05)
        assert self.server.started, "uvicorn не поднялся"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method, path, body=None, headers=None, password=PASSWORD, bearer=None, raw=None):
        h = dict(headers or {})
        if password is not None:
            h["Authorization"] = "Basic " + base64.b64encode(("max:" + password).encode()).decode()
        if bearer is not None:
            h["Authorization"] = "Bearer " + bearer
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            h.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with self.opener.open(req, timeout=10) as r:
                return r.status, r.read(), {k.lower(): v for k, v in r.headers.items()}
        except urllib.error.HTTPError as e:
            return e.code, e.read(), {k.lower(): v for k, v in e.headers.items()}

    def json(self, *a, **kw):
        code, body, hdr = self.call(*a, **kw)
        return code, (json.loads(body) if body else None)

    def close(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)


@pytest.fixture
def srv(store):
    s = Srv(store)
    yield s
    s.close()


def test_health_is_public_and_minimal(srv):
    code, j = srv.json("GET", "/health", password=None)
    assert code == 200 and j == {"ok": True, "service": "monitor", "version": "9.9.9"}


def test_panel_needs_password(srv):
    code, body, hdr = srv.call("GET", "/", password=None)
    assert code == 401 and "Basic" in hdr["www-authenticate"] and b"panel" not in body
    assert srv.call("GET", "/", password="не тот")[0] == 401
    code, body, hdr = srv.call("GET", "/")
    assert code == 200 and "Мониторинг заводов".encode() in body
    assert hdr["x-frame-options"] == "DENY" and hdr["x-content-type-options"] == "nosniff" and hdr["cache-control"] == "no-store"
    for path in ("/api/plants", "/api/plants/1/events"):
        assert srv.call("GET", path, password=None)[0] == 401


def test_password_guessing_is_throttled(store):
    clk = Clock()
    s = Srv(store, clock=clk)
    try:
        codes = [s.call("GET", "/api/plants", password="мимо%d" % i)[0] for i in range(10)]
        assert codes[:8] == [401] * 8 and codes[8:] == [429, 429]
        assert s.call("GET", "/api/plants")[0] == 429, "пока идёт блокировка, даже верный пароль не пускает"
        clk.t += monitor_app.AUTH_FAILS_WINDOW_S + 1
        assert s.call("GET", "/api/plants")[0] == 200
    finally:
        s.close()


def test_write_endpoints_need_panel_header_and_same_site(srv):
    assert srv.call("POST", "/api/plants", {"name": "Кир"})[0] == 403, "без X-Panel (так выглядит запрос с чужой страницы)"
    assert srv.call("POST", "/api/plants", {"name": "Кир"}, headers={"X-Panel": "1", "Sec-Fetch-Site": "cross-site"})[0] == 403
    code, j = srv.json("POST", "/api/plants", {"name": "Кир"}, headers={"X-Panel": "1", "Sec-Fetch-Site": "same-origin"})
    assert code == 200 and len(j["token"]) >= 30
    assert srv.call("DELETE", "/api/plants/%d" % j["id"])[0] == 403
    assert srv.json("POST", "/api/plants", {"name": "Кир"}, headers={"X-Panel": "1"})[0] == 400, "дубль названия"
    assert srv.json("POST", "/api/plants", {"name": ""}, headers={"X-Panel": "1"})[0] == 400


def test_ingest_auth_and_limits(srv, store):
    pid, token = store.create_plant("Кир")
    body = json.dumps(pulse()).encode()
    assert srv.call("POST", "/ingest", raw=body, password=None)[0] == 401
    assert srv.call("POST", "/ingest", raw=body, bearer="wrong-token")[0] == 401
    assert srv.call("POST", "/ingest", raw=body, password=PASSWORD)[0] == 401, "пароль панели — не токен завода"
    code, j = srv.json("POST", "/ingest", raw=body, bearer=token, password=None)
    assert code == 200 and j["ok"] is True
    assert srv.call("POST", "/ingest", raw="{не json".encode("utf-8"), bearer=token)[0] == 400
    assert srv.call("POST", "/ingest", raw=json.dumps({"schema": 9, "snapshot": {}}).encode(), bearer=token)[0] == 400
    huge = json.dumps(pulse(text="я" * (monitor_app.INGEST_MAX_BYTES))).encode()
    try:
        assert srv.call("POST", "/ingest", raw=huge, bearer=token)[0] == 413
    except OSError:
        pass                                                     # сервер ответил и закрыл соединение, не дожидаясь конца загрузки (на Windows клиент видит обрыв)
    assert "я" * 100 not in store.plants_view()[0]["snapshot"]["text"], "слишком большой пульс не разбирается и не сохраняется"


def test_full_cycle_through_panel_api(srv):
    code, j = srv.json("POST", "/api/plants", {"name": "Буи"}, headers={"X-Panel": "1"})
    pid, token = j["id"], j["token"]
    code, j = srv.json("GET", "/api/plants")
    assert code == 200 and j["version"] == "9.9.9" and [p["status"] for p in j["plants"]] == ["waiting"]
    srv.call("POST", "/ingest", raw=json.dumps(pulse(alerts=[{"code": "fault", "level": "high", "text": "Авария автомата: подвод", "since": time.time()}])).encode(),
             bearer=token)
    p = srv.json("GET", "/api/plants")[1]["plants"][0]
    assert p["status"] == "online" and p["alerts"][0]["text"].startswith("Авария")
    code, j = srv.json("GET", "/api/plants/%d/events?limit=10" % pid)
    assert code == 200 and j["events"][0]["kind"] == "alert"
    code, j = srv.json("POST", "/api/plants/%d/token" % pid, headers={"X-Panel": "1"})
    assert code == 200 and j["token"] != token
    assert srv.call("POST", "/ingest", raw=json.dumps(pulse()).encode(), bearer=token)[0] == 401, "старый токен после выдачи нового не работает"
    assert srv.json("DELETE", "/api/plants/%d" % pid, headers={"X-Panel": "1"})[1] == {"deleted": True}
    assert srv.json("GET", "/api/plants")[1]["plants"] == []
    assert srv.json("POST", "/api/plants/999/token", headers={"X-Panel": "1"})[0] == 404


# ------------------------------------------------------------------------------- сквозной прогон: sysmon.Agent → приёмник

def test_sysmon_agent_to_receiver_end_to_end(srv, store, tmp_path, monkeypatch):
    import sysmon
    pid, token = store.create_plant("Кир")
    sysmon.close()
    monkeypatch.setattr(sysmon, "DB_PATH", tmp_path / "sysmon.db")
    monkeypatch.setattr(sysmon, "CONFIG_PATH", tmp_path / "monitor.json")
    try:
        sysmon.save_config({"enabled": True, "url": srv.base, "token": token, "plant": "Кир", "system_proxy": False})
        src = {"cfg": lambda: {"camera_serial": "S1", "cv": {"enabled": True}}, "micro_on": lambda: True,
               "state": lambda: {"stage": 7, "stage_ok": True, "cyclic": True, "cycle_params": {"trigger_mode": "time"}},
               "status": lambda: {"connected": True}, "ext": lambda: {"uptime_s": 1000}, "cv": lambda: {"state": "ok"},
               "services": lambda: {"cv": "healthy"},
               "camera": lambda: {"serial": "S1", "streaming": True, "current": {"exposure_time": 5000.0, "exposure_auto": "Off"},
                                  "saved": {"exposure_time": 170.0, "exposure_auto": "Off"}},
               "probes": lambda serial, since: {"last_t": time.time(), "today": 7}}
        ag = sysmon.Agent(sources=src)
        ag.start_info = sysmon.register_start(ag.started_at, ag.boot, "2.0.34")
        ag._emit("start", "critical", ag.start_info["text"], {"reason": "first"}, t=ag.started_at)
        ag.tick()
        ag.tick(time.time() + 20)                                   # тревога экспозиции подтверждается со второй проверки
        res = ag.send()
        assert res["ok"] is True, res
        v = store.plants_view()[0]
        assert v["status"] == "online" and "Кир" in v["snapshot"]["text"]
        assert [a["code"] for a in v["alerts"]] == ["camera_exposure"] and "5000 вместо 170" in v["alerts"][0]["text"]
        kinds = [(e["kind"], e["state"]) for e in store.events(pid)]
        assert kinds.count(("alert", "open")) == 1, "тревога в журнале одна: от завода, без двойника из снимка"
        assert any(k == "start" for k, _ in kinds)
        assert sysmon.outbox_count() == 0
        assert token not in json.dumps(v["snapshot"], ensure_ascii=False)
    finally:
        sysmon.close()


# ------------------------------------------------------------------------------------------------- приложение: иконка, manifest

def _png_size(data: bytes):
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "не PNG"
    return struct.unpack(">II", data[16:24])


def test_icons_are_valid_png_and_ico():
    assert set(ASSETS) == {"/icon-192.png", "/icon-512.png", "/favicon.ico"}, "иконки не найдены в static\\"
    assert _png_size(ASSETS["/icon-192.png"][0]) == (192, 192) and _png_size(ASSETS["/icon-512.png"][0]) == (512, 512)
    ico = ASSETS["/favicon.ico"][0]
    reserved, kind, n = struct.unpack("<HHH", ico[:6])
    assert (reserved, kind) == (0, 1) and n == 6
    sizes = []
    for i in range(n):
        w, h, _c, _r, _planes, _bpp, length, offset = struct.unpack("<BBBBHHII", ico[6 + 16 * i:22 + 16 * i])
        sizes.append(w or 256)
        assert ico[offset:offset + 8] == b"\x89PNG\r\n\x1a\n" and offset + length <= len(ico), "запись иконки указывает мимо данных"
        assert _png_size(ico[offset:offset + length]) == (w or 256, h or 256)
    assert sizes == [256, 128, 64, 48, 32, 16]


def test_manifest_and_icons_are_public(srv):
    code, body, hdr = srv.call("GET", "/manifest.webmanifest", password=None)
    m = json.loads(body)
    assert code == 200 and hdr["content-type"].startswith("application/manifest+json")
    assert m["name"] == "web_MVS панель" and m["display"] == "standalone" and m["start_url"] == "/"
    for icon in m["icons"]:
        c, data, h = srv.call("GET", icon["src"], password=None)
        assert c == 200 and h["content-type"] == "image/png" and _png_size(data) == tuple(int(x) for x in icon["sizes"].split("x"))
    code, data, hdr = srv.call("GET", "/favicon.ico", password=None)
    assert code == 200 and hdr["content-type"] == "image/x-icon" and data[:4] == b"\x00\x00\x01\x00"
    assert srv.call("GET", "/", password=None)[0] == 401, "открытые только иконки и manifest, а не панель"


def test_panel_html_is_self_contained_app_page():
    assert 'rel="manifest"' in PANEL and "/icon-192.png" in PANEL and "<title>web_MVS панель</title>" in PANEL
    assert "http://" not in PANEL and "https://" not in PANEL, "панель не тянет ничего с чужих адресов (CDN может быть закрыт, и это утечка адреса)"
    assert "document.title" in PANEL, "в заголовке окна считаются заводы с проблемами"


def test_entry_password_created_once_and_env_wins(tmp_path, monkeypatch):
    monkeypatch.delenv("MONITOR_PASSWORD", raising=False)
    first = entry.load_password(tmp_path)
    assert len(first) >= 12 and json.loads((tmp_path / "server.json").read_text(encoding="utf-8")) == {"password": first}
    assert entry.load_password(tmp_path) == first, "второй запуск берёт тот же пароль"
    monkeypatch.setenv("MONITOR_PASSWORD", "из-окружения")
    assert entry.load_password(tmp_path) == "из-окружения"
    (tmp_path / "server.json").write_text("{битый", encoding="utf-8")
    monkeypatch.delenv("MONITOR_PASSWORD")
    assert len(entry.load_password(tmp_path)) >= 12, "битый файл не роняет запуск — создаётся новый пароль"


def test_panel_script_has_valid_javascript(tmp_path):
    """Опечатка в скрипте панели иначе видна только в браузере: страница вечно «Загрузка…». Проверяем разбор самим node (нет node — пропуск)."""
    import re
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node не установлен")
    code = re.search(r"<script>(.*?)</script>", PANEL, re.S).group(1)
    f = tmp_path / "panel.js"
    f.write_text(code, encoding="utf-8")
    r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
