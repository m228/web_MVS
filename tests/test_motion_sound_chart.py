"""График звука камеры (motion 3.0.54): секунды уровня и этапа в motion.db, прореживание, уборка, /history и настоящий сервис со звуком из файла."""
import io
import json
import socket
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pytest
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "motion_service"))
sys.path.insert(0, str(ROOT / "tests"))

import motion_engine as me  # noqa: E402
import motion_sound as ms  # noqa: E402
import motion_store  # noqa: E402
import motion_trends  # noqa: E402
import server as srv  # noqa: E402
from test_motion_sound import make_kiln_sound, needs_ffmpeg, wait_for  # noqa: E402

SOUND = {"id": "s", "name": "Звук", "type": "kiln_sound"}


def cam(groups=(SOUND,), **kw):
    return dict({"id": "c1", "url": "x", "enabled": False, "groups": [dict(g) for g in groups]}, **kw)


@pytest.fixture
def store(tmp_path):
    s = motion_store.store_for(tmp_path)
    yield s
    motion_store.close_for(tmp_path)


# ─── хранилище ───
def test_store_keeps_seconds_and_survives_reopen(tmp_path, store):
    for i in range(10):
        store.add_sound("c1", 1000 + i, i * 1.5, 1 if i == 4 else 0)
    store.add_sound("c2", 1000, 99, 2)
    rows = store.sound_history("c1", 0, 2000, 1)
    assert [r[0] for r in rows] == list(range(1000, 1010)) and rows[4] == (1004, 6.0, 1)
    motion_store.close_for(tmp_path)
    assert motion_store.store_for(tmp_path).sound_history("c1", 1003, 1005, 1) == [(1003, 4.5, 0), (1004, 6.0, 1), (1005, 7.5, 0)]


def test_store_same_second_rewritten_and_unknown_camera_empty(store):
    store.add_sound("c1", 1000, 5, 0)
    store.add_sound("c1", 1000, 7, 1)              # часы подравняли назад: секунда пришла ещё раз
    assert store.sound_history("c1", 0, 2000, 1) == [(1000, 7.0, 1)]
    assert store.sound_history("нет", 0, 2000, 1) == []
    assert "нет" not in store._names                # запрос графика не заводит камер в базе


def test_thinning_takes_maximum_in_bucket(store):
    for i in range(100):
        store.add_sound("c1", 1000 + i, 40.0 if i == 33 else float(i % 7), {33: 1, 71: 2, 72: 3}.get(i, 0))
    rows = store.sound_history("c1", 1000, 1099, 10)
    assert len(rows) == 10 and [r[0] for r in rows] == list(range(1009, 1100, 10))      # время корзины — последняя её секунда
    assert rows[3][1:] == (40.0, 1) and rows[7][2] == 3 and rows[0][1:] == (6.0, 0)     # всплеск и этап не теряются


def test_purge_removes_old_sound_like_trends(tmp_path, store):
    now = time.time()
    for ts in (now - 70 * 86400, now - 61 * 86400, now - 3600, now - 10):
        store.add_sound("c1", ts, 10, 0)
    assert motion_trends.purge(tmp_path, motion_trends.KEEP_DAYS, now) >= 2
    assert [r[0] for r in store.sound_history("c1", 0, now + 5, 1)] == [int(now - 3600), int(now - 10)]


def test_disk_failure_keeps_buffer_and_reading(store, monkeypatch):
    store.add_sound("c1", 1000, 3, 0)
    store.flush()
    store.add_sound("c1", 1001, 4, 0)

    def broken():
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "flush", broken)
    assert store.sound_history("c1", 0, 2000, 1) == [(1000, 3.0, 0)]        # что уже на диске — отдаётся
    monkeypatch.undo()
    assert [r[0] for r in store.sound_history("c1", 0, 2000, 1)] == [1000, 1001]      # хвост не потерян


# ─── поток звука → история ───
def pcm(*parts):
    rng = np.random.default_rng(0)
    x = np.concatenate([rng.normal(0, amp, int(sec * ms.SR)) for sec, amp in parts])
    return io.BytesIO((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


def listener(sink):
    ticks = iter(range(10 ** 7))
    return ms.Listener("c1", "x", 30, "ffmpeg", clock=lambda: 5000.0 + next(ticks) * 0.1, sink=sink)


def test_listener_writes_one_point_per_second():
    got = []
    lis = listener(lambda *a: got.append(a))
    lis._read(pcm((40, 0.002), (3, 0.2), (4, 0.002), (3, 0.0), (3, 0.002)))
    secs = [p[1] for p in got]
    assert secs == sorted(set(secs)) and all(p[0] == "c1" for p in got)
    assert min(secs) >= 5000 + ms.BG_MIN_S - 1                                   # пока копится фон — на графике пусто
    loud = [p for p in got if p[2] >= 30]
    assert loud and {p[3] for p in loud} <= {0, 1} and any(p[3] == ms.HIST_STAGES.index("drop") for p in got)
    assert min(p[2] for p in got) > -20                                          # нули вместо звука в историю не идут
    assert len(got) <= 49                                                        # 53 с минус «слушаю», нули и незаконченная секунда


def test_listener_survives_broken_disk(caplog):
    calls = []

    def sink(*a):
        calls.append(a)
        if len(calls) % 5 == 0:                # как на деле: падает только та секунда, на которой пачка уходит на диск
            raise sqlite3.OperationalError("database or disk is full")

    lis = listener(sink)
    lis._read(pcm((45, 0.002)))                     # не падает
    assert len(calls) > 5 and lis.detector.bg is not None
    assert sum("история звука" in r.getMessage() for r in caplog.records) == 1    # в журнал — один раз


# ─── эндпоинт и конфиг ───
@contextmanager
def service(data: Path):
    """Настоящий сервис на свободном порту: -> call(путь, тело=None, **параметры) -> разобранный ответ."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(srv.create_app(data), host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    assert wait_for(lambda: server.started, 20, 0.05)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(path, body=None, **params):
        url = "http://127.0.0.1:%d%s" % (port, path) + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, None if body is None else json.dumps(body).encode(),
                                     {"Content-Type": "application/json"}, method="GET" if body is None else "PUT")
        with opener.open(req, timeout=20) as r:          # не 200 — исключение
            return json.load(r)

    try:
        yield call
    finally:
        server.should_exit = True
        th.join(30)
        assert not th.is_alive()


def test_history_endpoint_adds_sound(tmp_path):
    with service(tmp_path / "data") as call:
        call("/config", {"cameras": [cam(), dict(cam(groups=()), id="c2")]})
        st = motion_store.store_for(tmp_path / "data")
        now = int(time.time())
        for i in range(600):
            st.add_sound("c1", now - 600 + i, 35.0 if i == 500 else 2.0, 1 if i == 500 else 0)
        st.add_sound("c1", now - 5000, 50, 2)       # за окном
        r = call("/cameras/c1/history", window=900, points=100)
        s = r["sound"]
        assert s["step"] == 9 and len(s["t"]) == len(s["level"]) == len(s["stage"]) <= 101
        assert max(s["level"]) == 35.0 and s["stage"][s["level"].index(35.0)] == 1 and abs(s["now"] - time.time()) < 5
        assert r["t"] == [] and r["zones"] == {}                       # камера без картинки и зон: звук всё равно есть
        assert "sound" not in call("/cameras/c2/history", window=900)       # старый конфиг — ответ как раньше
        assert "sound" not in call("/cameras/c1/history", n=10)             # покадровый режим не трогаем


def test_history_endpoint_survives_broken_store(tmp_path, monkeypatch):
    with service(tmp_path / "data") as call:
        call("/config", {"cameras": [cam()]})

        def broken(*a, **kw):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(motion_store.MotionStore, "sound_history", broken)
        r = call("/cameras/c1/history", window=600)
        assert r["sound"]["t"] == [] and r["t"] == []


class NoCap:
    """Камера, которая не открывается: картинки нет."""

    def isOpened(self):
        return False

    def release(self):
        pass


def test_sound_switch_and_threshold_do_not_restart_video(tmp_path, monkeypatch):
    monkeypatch.setattr(ms.SoundHub, "start", lambda self: None)                # ffmpeg здесь не нужен
    motion = {"id": "z1", "name": "лента", "type": "motion", "points": [[0, 0], [10, 0], [10, 10]]}
    total = {"id": "g1", "name": "сумма", "type": "sum", "zones": ["z1"]}
    mgr = srv.Manager(tmp_path / "data", capture_factory=lambda *a: NoCap())
    mgr.start()
    try:
        base = {"id": "c1", "url": "x", "zones": [motion], "groups": [total]}
        mgr.apply({"cameras": [base]})
        w = mgr.workers["c1"]
        kept = w.cfg["groups"][0]
        for groups in ([total, SOUND], [total, dict(SOUND, threshold_db=22)], [SOUND, total], [total]):
            mgr.apply({"cameras": [dict(base, groups=groups)]})
            assert mgr.workers["c1"] is w, groups
            assert [g["id"] for g in w.cfg["groups"]] == [g["id"] for g in groups]
            assert next(g for g in w.cfg["groups"] if g["id"] == "g1") is kept
            snd = [g for g in (w.state()["result"] or {}).get("groups", []) if g["type"] == "kiln_sound"]
            assert len(snd) == sum(g is not total for g in groups)
            if snd:
                assert snd[0]["threshold"] == next(g for g in groups if g["id"] == "s").get("threshold_db", 30)
        mgr.apply({"cameras": [dict(base, groups=[dict(total, name="другая")])]})      # обычная группа — перезапуск, как раньше
        assert mgr.workers["c1"] is not w
    finally:
        mgr.shutdown()


def test_mirror_learning_saves_next_to_sound_group(tmp_path):
    """Синхронность валов запоминает форму на диск и тогда, когда у камеры есть ещё вычисление без таблицы (раньше — KeyError)."""
    rods = [{"id": i, "name": i, "type": "cycle", "center": [c, 50], "radius": 40} for i, c in (("a", 50), ("b", 150))]
    cfg = me.normalize_camera({"id": "c1", "url": "x", "zones": rods, "groups": [
        {"id": "m", "name": "валы", "type": "mirror", "zones": ["a", "b"]}, SOUND]})
    eng = me.CameraEngine(cfg, tmp_path / "learn.json")
    eng.learners["m"].dirty = True
    eng._learn_save(1000.0, force=True)
    assert (tmp_path / "learn.json").is_file()


def test_store_stops_after_shutdown(tmp_path):
    mgr = srv.Manager(tmp_path / "data")
    mgr.start()
    mgr.shutdown()
    mgr._sound_point("c1", 1000, 1.0, 0)                       # запоздавшая секунда не открывает базу заново
    assert str((tmp_path / "data").resolve()) not in motion_store._REG


# ─── настоящий ffmpeg и сервис целиком ───
@needs_ffmpeg
def test_service_records_sound_history_from_file(tmp_path):
    """Звук из файла идёт в реальном времени, картинки нет: уровень и этапы ложатся в базу, /history их отдаёт и после перезапуска."""
    src = tmp_path / "kiln.wav"
    make_kiln_sound(src)
    config = {"cameras": [cam(url=str(src), enabled=True)]}
    with service(tmp_path / "data") as call:
        call("/config", config)

        def history():
            return call("/cameras/c1/history", window=120, points=600)["sound"]

        assert wait_for(lambda: 2 in history()["stage"], 100, 1.0), history()
        time.sleep(3)
        s = history()
    assert s["step"] == 1 and 25 <= len(s["t"]) <= 80
    drop = [lv for lv, st in zip(s["level"], s["stage"]) if st == 1]
    load = [lv for lv, st in zip(s["level"], s["stage"]) if st == 2]
    assert max(drop) >= 30 and max(load) >= 30
    assert s["t"][s["stage"].index(1)] < s["t"][s["stage"].index(2)]
    quiet = [lv for lv, st in zip(s["level"], s["stage"]) if st == 0]
    assert float(np.median(quiet)) < 10
    with service(tmp_path / "data") as call:                                   # после перезапуска история на месте
        again = call("/cameras/c1/history", window=600, points=600)["sound"]
        assert set(s["t"]) <= set(again["t"])
