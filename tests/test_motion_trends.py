"""motion_service: тренды и снимки на диске — переживают перезапуск, запись CSV не останавливается молча, старое чистится."""
import json
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "motion_service"))

import motion_camera as mc  # noqa: E402
import motion_trends as mt  # noqa: E402
from test_motion_service import _load_server, _wait, _worker, make_frames, FakeCap  # noqa: E402


def _bucket(key, n=3):
    return [key, {"z1": [n, 1.5 * n, 0.4, 0.6, 3.0 * n, 0.9, 1.1]}]


def test_trend_log_roundtrip(tmp_path):
    now = time.time()
    t = mt.TrendLog(tmp_path, "c1")
    for i in range(5):
        t.add_long(now - 50 + i, {"z1": (0.5 + i, 1.0, 1), "gone": (9, 9, 1)}, 1)
    t.add_stats(_bucket(int((now - 100) // 10)))
    t.close()
    longs, stats = mt.TrendLog(tmp_path, "c1").load(now, 7200, 86400, {"z1"})
    assert len(longs) == 5 and longs[0][1] == {"z1": (0.5, 1.0, 1)}, "зона, которой больше нет в настройках, не возвращается"
    assert len(stats) == 1 and stats[0][1]["z1"][0] == 3 and stats[0][1]["z1"][1] == 4.5


def test_trend_log_skips_old_bad_and_truncated_rows(tmp_path):
    now = time.time()
    t = mt.TrendLog(tmp_path, "c1")
    t.add_long(now - 20000, {"z1": (1, 1, 1)}, 1)         # старше 2 часов
    t.add_long(now - 10, {"z1": (2, 2, 1)}, 1)
    t.close()
    f = next((tmp_path / "trends").glob("c1_????????.csv"))
    with open(f, "a", encoding="utf-8-sig") as fh:
        fh.write("мусор;без;чисел\n2026-11-02 14:00:00;12")     # битая и оборванная строки
    longs, _ = mt.TrendLog(tmp_path, "c1").load(now, 7200, 86400, {"z1"})
    assert [round(x[1]["z1"][0]) for x in longs] == [2]


def test_worker_restores_trends_after_restart(tmp_path):
    w, _ = _worker(tmp_path, make_frames(60))
    try:
        assert _wait(lambda: w.state()["connected"])
        # прошлую «жизнь» сервиса подменяем руками: точки в минувшие минуты
        with w._lock:
            for i in range(30):
                ts = time.time() - 200 + i
                w._push_history(ts, {"z1": (1.0, 2.0, 1), "z2": (0.1, 0.2, 0)}, 1)
                w._stats_add(ts + 100 * i, {"z1": (1.0, 2.0, 1), "z2": (0.1, 0.2, 0)})
    finally:
        w.stop()
    w2, _ = _worker(tmp_path, make_frames(60))
    try:
        h = w2.history(window_s=600, points=300)
        assert len(h["t"]) >= 20 and "z1" in h["zones"], "график не пустой после перезапуска"
        st = w2.stats(86400)
        assert st["zones"]["z1"]["n"] >= 1 and st["span_s"] > 60, "статистика не обнулилась"
    finally:
        w2.stop()


def test_shot_draws_zones_and_logs_state(tmp_path):
    w, _ = _worker(tmp_path, make_frames(60))
    try:
        assert _wait(lambda: (w.state()["result"] or {}).get("zones"))
        s = w.shot()
        assert s["jpeg"] and s["info"]["connected"] and set(s["info"]["zones"]) == {"z1", "z2"}
        img = cv2.imdecode(np.frombuffer(s["jpeg"], np.uint8), cv2.IMREAD_COLOR)
        assert img is not None and img.shape[0] > 0
        folder = mt.write_shot(tmp_path, time.time(), {"w": s, "dead": {"jpeg": None, "info": {"connected": False, "status": "нет кадров"}}})
        assert (folder / "w.jpg").is_file() and not (folder / "dead.jpg").exists()
        log = json.loads((folder / "log.json").read_text(encoding="utf-8"))
        assert log["cameras"]["dead"]["photo"] is False and log["cameras"]["w"]["photo"] is True
        rows = (folder.parent / "log.csv").read_text(encoding="utf-8-sig").strip().splitlines()
        assert rows[0].startswith("time;camera") and len(rows) == 1 + 2 + 1, "по строке на зону и одна на мёртвую камеру"
    finally:
        w.stop()


def test_record_rolls_to_next_file_instead_of_stopping(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "RECORD_MAX_BYTES", 600)
    w, _ = _worker(tmp_path, make_frames(60))
    try:
        w.start_record()
        assert _wait(lambda: len(list((tmp_path / "records").glob("*.csv"))) >= 3, 10), "файл заполнился — запись пошла в следующий"
        assert w.state()["recording"], "запись при этом не остановилась"
    finally:
        w.stop()


def test_purge_removes_only_old_files_and_folders(tmp_path):
    now = time.time()
    old, new = now - 20 * 86400, now - 86400
    for sub, name in (("records", "a.csv"), ("trends", "a.csv"), ("records", "b.csv")):
        d = tmp_path / sub
        d.mkdir(exist_ok=True)
        (d / name).write_text("x")
    os.utime(tmp_path / "records" / "a.csv", (old, old))
    os.utime(tmp_path / "trends" / "a.csv", (old, old))
    for day, t in (("20261001", old), ("20261101", new)):
        d = tmp_path / "shots" / day
        d.mkdir(parents=True)
        os.utime(d, (t, t))
    assert mt.purge(tmp_path, 10, now) == 3
    assert (tmp_path / "records" / "b.csv").exists() and not (tmp_path / "records" / "a.csv").exists()
    assert (tmp_path / "shots" / "20261101").exists() and not (tmp_path / "shots" / "20261001").exists()


def test_manager_auto_record_starts_on_first_run_but_manual_off_is_remembered(tmp_path):
    srv = _load_server()
    factory = lambda u, t: FakeCap(make_frames(40))
    cfg = {"cameras": [{"id": "w", "url": "fake", "hw_accel": "off", "proc_fps": 20, "zones": [{"id": "z1", "points": [[10, 40], [150, 40], [150, 200], [10, 200]]}]}]}
    (tmp_path / "motion_config.json").write_text(json.dumps(cfg), encoding="utf-8")
    m = srv.Manager(tmp_path, capture_factory=factory, auto_record=True, shots_min=0)
    m.start()
    try:
        assert m.rec_all and _wait(lambda: m.workers["w"].state()["recording"], 5), "первый запуск: запись включилась сама"
        m.record_all(False)
    finally:
        m.shutdown()
    m2 = srv.Manager(tmp_path, capture_factory=factory, auto_record=True, shots_min=0)
    m2.start()
    try:
        assert not m2.rec_all and not m2.workers["w"].state()["recording"], "выключили вручную — после перезапуска не пишется"
        folder = m2.take_shots()
        assert folder is not None and (folder / "log.json").is_file()
    finally:
        m2.shutdown()
