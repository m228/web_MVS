"""Журнал событий камер: хранилище эпизодов (cam_events), фоновый монитор (cam_monitor), «серая каша» в RTSP-воркере."""
import json
import time

import numpy as np
import pytest

import cam_events
import cam_monitor
from camera_core.frame_quality import frame_garbage, garbage_ratio
from camera_core.rtsp_worker import RtspCameraWorker

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


def test_sync_opens_and_closes_episodes_per_scope(ev):
    ev.sync("c1", ["a", "b"], {("a", ""): "x", ("b", "z1"): "зона"}, 100.0)
    ev.sync("c1", ["c"], {("c", ""): ""}, 101.0)
    # «a» пропала — закрылась; «b» продолжается; чужой kind «c» не тронут (он не в scope вызова)
    ev.sync("c1", ["a", "b"], {("b", "z1"): "зона"}, 105.0)
    rows = {(r["kind"], r["zone"]): r for r in ev.episodes("c1", now=110.0)}
    assert rows[("a", "")]["t1"] == 105.0 and rows[("a", "")]["dur"] == 5.0
    assert rows[("b", "z1")]["t1"] is None and rows[("b", "z1")]["dur"] == 10.0, "идущий эпизод считается до «сейчас»"
    assert rows[("c", "")]["t1"] is None
    assert len(ev.episodes("c1", now=110.0)) == 3, "повторный sync одного и того же не плодит строки"


def test_filters_window_kinds_min_duration(ev):
    ev.sync("c1", ["a"], {("a", ""): ""}, 100.0)
    ev.sync("c1", ["a"], {}, 102.0)                      # 2 с
    ev.sync("c1", ["a"], {("a", ""): ""}, 200.0)
    ev.sync("c1", ["a"], {}, 260.0)                      # 60 с
    ev.sync("c2", ["b"], {("b", ""): ""}, 300.0)
    assert [r["t0"] for r in ev.episodes(now=400.0)] == [300.0, 200.0, 100.0]
    assert [r["t0"] for r in ev.episodes("c1", min_s=5, now=400.0)] == [200.0]
    assert [r["cam"] for r in ev.episodes(kinds=["b"], now=400.0)] == ["c2"]
    # окно пересечения: эпизод 200–260 попадает в окно 250–270, 100–102 — нет
    assert [r["t0"] for r in ev.episodes("c1", t_from=250, t_to=270, now=400.0)] == [200.0]


def test_recover_closes_hanging_at_last_heartbeat(ev):
    ev.sync("c1", ["a"], {("a", ""): ""}, 100.0)
    ev.heartbeat(150.0)                                  # пульс ушёл на диск
    ev._open.clear()                                     # «перезапуск»: память потеряна
    assert ev.recover() == 1
    r = ev.episodes("c1", now=1000.0)[0]
    assert r["t1"] == 150.0, "закрыто на пульсе, а не на сегодняшнем дне"
    ev.sync("c1", ["a"], {("a", ""): ""}, 1000.0)        # после восстановления новые эпизоды открываются нормально
    assert len(ev.episodes("c1", now=1001.0)) == 2


def test_purge_removes_only_old_finished(ev):
    day = 86400.0
    ev.sync("c1", ["a"], {("a", ""): ""}, 0.0)
    ev.sync("c1", ["a"], {}, 10.0)                       # старый, завершён
    ev.sync("c1", ["b"], {("b", ""): ""}, 5.0)           # старый, но идёт — не трогаем
    now = (ev.RETENTION_DAYS + 1) * day
    assert ev.purge(now) == 1
    assert [r["kind"] for r in ev.episodes("c1", now=now)] == ["b"]


# ---------- монитор ----------
class FakeWorker:
    def __init__(self):
        self.reconnecting = False
        self.gray_now = False
        self.gray_ratio = 0.0


def cv_cam(enabled=True, result=None, error=""):
    return {"id": "c1", "enabled": enabled, "error": error, "result": result}


def zones(on, off=()):
    return [{"id": i, "name": "Зона " + i, "enabled": True, "bit": 1} for i in on] + \
           [{"id": i, "name": "Зона " + i, "enabled": True, "bit": 0} for i in off]


def test_monitor_collects_link_gray_cv_and_zone_episodes(ev):
    clock = {"t": 1000.0}
    w = FakeWorker()
    motion = {"cameras": []}
    mon = cam_monitor.Monitor(lambda: {"c1": w}, lambda: "u", lambda url: motion, clock=lambda: clock["t"])

    def step(dt=1.0):
        clock["t"] += dt
        mon.tick()

    motion["cameras"] = [cv_cam(result={"alive": True, "zones": zones(["z1"], ["z2"])})]
    step()
    w.reconnecting = True
    step()
    w.reconnecting, w.gray_now, w.gray_ratio = False, True, 0.5
    step()
    w.gray_now = False
    motion["cameras"] = [cv_cam(result={"alive": True, "zones": zones(["z2"], ["z1"])})]   # z1 встала, z2 закрутилась
    step()
    motion["cameras"] = [cv_cam(result={"alive": True, "corrupt": True, "reason": "битый поток", "zones": zones(["z2"])})]
    step()
    motion["cameras"] = [cv_cam(result={"alive": False, "reason": "картинка застыла", "zones": []})]
    step()
    motion["cameras"] = [cv_cam(result={"alive": True, "zones": zones(["z2"], ["z1"])})]
    step()

    rows = ev.episodes("c1", now=clock["t"] + 1)
    by = {}
    for r in rows:
        by.setdefault((r["kind"], r["zone"]), []).append(r)
    assert by[("link_lost", "")][0]["dur"] == 1.0
    assert by[("gray", "")][0]["info"] == "50% кадра"
    assert len(by[("cv_corrupt", "")]) == 1 and len(by[("cv_frozen", "")]) == 1
    # z1: сначала «крутится», потом «стоит» (кадры с кашей/застывшей картинкой зону не пишут)
    assert [r["kind"] for r in sorted(by[("zone_on", "z1")] + by[("zone_off", "z1")], key=lambda r: r["t0"])][:2] == ["zone_on", "zone_off"]
    # во время каши и фриза зона открытого эпизода не имеет: состояние неизвестно
    cv_bad = by[("cv_corrupt", "")][0]
    for k, lst in by.items():
        if k[0] in ("zone_on", "zone_off"):
            for r in lst:
                assert r["t1"] is not None and r["t1"] <= cv_bad["t0"] or r["t0"] >= by[("cv_frozen", "")][0]["t1"], k


def test_monitor_motion_service_down_closes_and_backs_off(ev):
    clock = {"t": 0.0}
    calls = []
    state = {"cameras": [cv_cam(result={"alive": True, "zones": zones(["z1"])})]}

    def fake_state(url):
        calls.append(clock["t"])
        return state

    mon = cam_monitor.Monitor(lambda: {}, lambda: "u", fake_state, clock=lambda: clock["t"])
    clock["t"] = 1.0
    mon.tick()
    assert ev.episodes("c1", now=2.0)[0]["t1"] is None
    state = {"error": "сервис движения не отвечает"}
    clock["t"] = 2.0
    mon.tick()
    assert ev.episodes("c1", now=3.0)[0]["t1"] == 2.0, "сервис пропал — эпизод закрыт"
    clock["t"] = 3.0
    mon.tick()
    clock["t"] = 4.0
    mon.tick()
    assert calls == [1.0, 2.0], "после ошибки сервис не долбим дольше MOTION_RETRY_S"
    clock["t"] = 2.0 + cam_monitor.MOTION_RETRY_S + 0.1
    mon.tick()
    assert len(calls) == 3


def test_disabled_cv_camera_writes_nothing(ev):
    mon = cam_monitor.Monitor(lambda: {}, lambda: "u", lambda url: {"cameras": [cv_cam(enabled=False)]}, clock=lambda: 5.0)
    mon.tick()
    assert ev.episodes(now=6.0) == []


# ---------- серая каша ----------
def test_garbage_ratio_flat_grey_vs_live():
    flat = np.full((96, 128), 128, np.uint8)
    noisy = np.random.RandomState(0).randint(0, 255, (96, 128)).astype(np.uint8)
    assert garbage_ratio(flat) == 1.0
    assert garbage_ratio(noisy) == 0.0
    half = noisy.copy()
    half[:48] = 128
    assert 0.45 < garbage_ratio(half) < 0.55
    assert frame_garbage(np.dstack([flat] * 3)) == 1.0


def test_worker_samples_quality_once_per_period():
    w = RtspCameraWorker("t_q", None, "rtsp://example/x")
    grey = np.full((96, 128, 3), 128, np.uint8)
    live = np.random.RandomState(1).randint(0, 255, (96, 128, 3)).astype(np.uint8)
    w._sample_quality(grey, 100.0)
    assert w.gray_now and w.gray_frames == 1 and w.gray_samples == 1
    w._sample_quality(live, 100.5)                      # раньше периода — выборка пропущена
    assert w.gray_now and w.gray_samples == 1
    w._sample_quality(live, 101.1)
    assert not w.gray_now and w.gray_samples == 2 and w.gray_frames == 1


# ---------- API ----------
def test_events_api_returns_labels_and_filters(data_dir, monkeypatch):
    import rtsp_store
    monkeypatch.setattr(cam_events, "DB_PATH", data_dir / "cam_events.db")
    monkeypatch.setattr(cam_events, "_conn", None)
    cam_events._open.clear()
    rtsp_store.save({"url": "rtsp://127.0.0.1:554/live", "label": "Шнек", "serial": "rtsp_7"})
    now = time.time()
    cam_events.sync("rtsp_7", ["link_lost", "gray"], {("link_lost", ""): ""}, now - 600)
    cam_events.sync("rtsp_7", ["link_lost", "gray"], {}, now - 540)
    cam_events.sync("rtsp_7", ["link_lost", "gray"], {("gray", ""): "30% кадра"}, now - 5)
    status, body = get("/api/cam/events", "hours=1")
    data = json.loads(body)
    assert status == 200 and [e["kind"] for e in data["events"]] == ["gray", "link_lost"]
    assert data["events"][0]["label"] == "Шнек" and data["events"][0]["t1"] is None
    assert data["events"][1]["dur"] == 60.0
    only = json.loads(get("/api/cam/events", "kinds=link_lost&min_s=30")[1])["events"]
    assert [e["kind"] for e in only] == ["link_lost"]
    cam_events._conn.close()
    cam_events._conn = None


def test_link_lost_starts_at_first_failed_attempt(ev):
    # reconnecting включается только после паузы перед повтором, а счётчик попыток — сразу после неудачи
    w = FakeWorker()
    w.reconnect_attempts = 1
    mon = cam_monitor.Monitor(lambda: {"c1": w}, lambda: "u", lambda url: {"cameras": []}, clock=lambda: 50.0)
    mon.tick()
    assert [r["kind"] for r in ev.episodes("c1", now=51.0)] == ["link_lost"]
