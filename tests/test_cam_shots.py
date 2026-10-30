"""Снимок по событию: остановка зоны / зависание, удержавшиеся N секунд, сохраняют кадр; серые кадры и мигание не сохраняются."""
import time

import cv2
import numpy as np
import pytest

import cam_events
import cam_monitor

from test_api import get


@pytest.fixture
def ev(tmp_path, monkeypatch):
    monkeypatch.setattr(cam_events, "DB_PATH", tmp_path / "cam_events.db")
    monkeypatch.setattr(cam_events, "_conn", None)
    monkeypatch.setattr(cam_events, "_beat", 0.0)
    monkeypatch.setattr(cam_events, "_beat_saved", 0.0)
    cam_events._open.clear()
    yield cam_events
    if cam_events._conn is not None:
        cam_events._conn.close()
    cam_events._open.clear()


def jpeg(grey=False):
    img = np.full((96, 128, 3), 128, np.uint8) if grey else np.random.RandomState(3).randint(0, 255, (96, 128, 3)).astype(np.uint8)
    return cv2.imencode(".jpg", img)[1].tobytes()


def zone_state(bit, zid="z1"):
    return {"cameras": [{"id": "c1", "enabled": True, "result": {"alive": True, "zones": [
        {"id": zid, "name": "Шнек", "enabled": True, "bit": bit}]}}]}


def frozen_state():
    return {"cameras": [{"id": "c1", "enabled": True, "result": {"alive": False, "reason": "картинка застыла", "zones": []}}]}


class Rig:
    def __init__(self, frames, cfg=None):
        self.t = 1000.0
        self.state = zone_state(0)
        self.frames = list(frames)           # что отдаёт сервис КЗ на очередной запрос кадра
        self.asked = 0

        def shot(url, cam):
            self.asked += 1
            f = self.frames.pop(0) if self.frames else self.frames_last
            self.frames_last = f
            return (f, 128, 96)

        self.frames_last = jpeg()
        self.mon = cam_monitor.Monitor(lambda: {}, lambda: "u", lambda url: self.state, clock=lambda: self.t,
                                       shot_fn=shot, cfg_fn=lambda: cfg, async_shots=False)

    def step(self, dt=1.0):
        self.t += dt
        self.mon.tick()


def shots(ev):
    return [e for e in ev.episodes(now=5000.0) if e["shot"]]


def test_stop_held_long_enough_saves_one_frame(ev):
    r = Rig([jpeg()])
    for _ in range(29):
        r.step()
    assert r.asked == 0 and not shots(ev), "30 секунд ещё не прошло"
    for _ in range(5):
        r.step()
    got = shots(ev)
    assert len(got) == 1 and got[0]["kind"] == "zone_off" and r.asked == 1, "один кадр на событие"
    p = ev.shot_file(got[0]["id"])
    assert p is not None and p.is_file() and p.suffix == ".jpg" and "c1" in str(p)
    for _ in range(100):
        r.step()
    assert r.asked == 1, "событие продолжается — второй кадр не снимаем"


def test_grey_frame_is_rejected_then_retried(ev):
    r = Rig([jpeg(grey=True), jpeg(grey=True), jpeg()])
    for _ in range(31):
        r.step()
    assert r.asked == 1 and not shots(ev), "первая попытка — серая каша, не сохраняем"
    for _ in range(4):
        r.step()
    assert r.asked == 2 and not shots(ev)
    for _ in range(4):
        r.step()
    assert r.asked == 3 and len(shots(ev)) == 1, "третья попытка — живой кадр"


def test_grey_forever_stops_after_three_tries(ev):
    r = Rig([jpeg(grey=True)] * 10)
    for _ in range(120):
        r.step()
    assert r.asked == cam_monitor.SHOT_TRIES and not shots(ev)


def test_flapping_zone_never_reaches_hold(ev):
    r = Rig([jpeg()])
    for _ in range(6):                       # 20 с стоит → крутится → снова стоит: удержание считается заново
        r.state = zone_state(0)
        for _ in range(20):
            r.step()
        r.state = zone_state(1)
        r.step()
    assert r.asked == 0 and not shots(ev)


def test_cooldown_and_daily_limit(ev):
    r = Rig([jpeg()], cfg={"cooldown_s": 300, "daily_limit": 2})
    for _ in range(35):
        r.step()
    assert len(shots(ev)) == 1
    r.state = zone_state(1)
    r.step()
    r.state = zone_state(0)                  # новая остановка той же зоны через минуту — внутри паузы 300 с
    for _ in range(40):
        r.step()
    assert len(shots(ev)) == 1, "пауза между снимками по зоне"
    r.state = zone_state(1)
    r.step(400)
    r.state = zone_state(0, "z2")            # другая зона: пауза не мешает; это второй снимок за день — в пределах лимита 2
    for _ in range(35):
        r.step()
    assert len(shots(ev)) == 2
    r.state = zone_state(1, "z2")
    r.step(400)
    r.state = zone_state(0, "z3")            # третий за день — лимит
    for _ in range(35):
        r.step()
    assert len(shots(ev)) == 2


def test_frozen_picture_triggers_and_disabled_config_does_not(ev):
    r = Rig([jpeg()])
    r.state = frozen_state()
    for _ in range(35):
        r.step()
    assert [e["kind"] for e in shots(ev)] == ["cv_frozen"]
    off = Rig([jpeg()], cfg={"enabled": False})
    for _ in range(60):
        off.step()
    assert off.asked == 0


def test_motion_service_gap_restarts_the_hold(ev):
    r = Rig([jpeg()])
    for _ in range(25):
        r.step()
    r.state = {"error": "сервис движения не отвечает"}
    r.step()
    r.state = zone_state(0)
    r.step(cam_monitor.MOTION_RETRY_S + 1)
    for _ in range(20):
        r.step()
    assert r.asked == 0, "после пропажи сервиса удержание считается заново, а не с позапрошлого раза"


def test_purge_shots_removes_only_old_day_folders(ev):
    base = ev.shots_dir()
    old = base / "c1" / "2026-01-01"
    new = base / "c1" / "2026-10-29"
    other = base / "c1" / "keep_me"
    for d in (old, new, other):
        d.mkdir(parents=True)
        (d / "a.jpg").write_bytes(b"x")
    now = time.mktime((2026, 10, 30, 12, 0, 0, 0, 0, -1))
    assert ev.purge_shots(now) == 1
    assert not old.exists() and new.exists() and other.exists(), "чужие папки и свежие дни не трогаем"


def test_shot_api_serves_file_and_rejects_escape(data_dir, monkeypatch):
    monkeypatch.setattr(cam_events, "DB_PATH", data_dir / "cam_events.db")
    monkeypatch.setattr(cam_events, "_conn", None)
    cam_events._open.clear()
    cam_events.sync("c1", ["zone_off"], {("zone_off", "z1"): "Шнек"}, 100.0)
    ep = cam_events.open_id("c1", "zone_off", "z1")
    f = cam_events.shots_dir() / "c1" / "2026-10-30" / "x.jpg"
    f.parent.mkdir(parents=True)
    f.write_bytes(jpeg())
    cam_events.set_shot(ep, "c1/2026-10-30/x.jpg")
    status, body = get("/api/cam/shot", "id=%d" % ep)
    assert status == 200 and body[:2] == b"\xff\xd8"
    assert get("/api/cam/shot", "id=999")[0] == 404
    cam_events.set_shot(ep, "../cam_events.db")              # подмена пути в базе наружу event_shots
    assert get("/api/cam/shot", "id=%d" % ep)[0] == 404
    cam_events._conn.close()
    cam_events._conn = None


def test_old_database_without_shot_column_is_migrated(ev):
    import sqlite3
    c = sqlite3.connect(str(ev.DB_PATH))
    c.execute("CREATE TABLE episodes (id INTEGER PRIMARY KEY AUTOINCREMENT, cam TEXT NOT NULL, kind TEXT NOT NULL, "
              "zone TEXT NOT NULL DEFAULT '', info TEXT NOT NULL DEFAULT '', t0 REAL NOT NULL, t1 REAL)")
    c.execute("INSERT INTO episodes (cam, kind, t0, t1) VALUES ('c1', 'link_lost', 10, 20)")
    c.commit()
    c.close()
    rows = ev.episodes(now=100.0)
    assert len(rows) == 1 and rows[0]["shot"] is False, "база 2.0.17 открывается, колонка кадра добавлена сама"
