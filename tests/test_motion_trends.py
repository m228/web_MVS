"""motion_service: тренды, записи и снимки в SQLite (motion.db) — переживают перезапуск, CSV только выгрузка, старое чистится."""
import json
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "motion_service"))

import motion_camera as mc  # noqa: E402
import motion_records as mr  # noqa: E402
import motion_store as ms  # noqa: E402
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


def test_trend_log_skips_old_points_and_zones_not_in_config(tmp_path):
    now = time.time()
    t = mt.TrendLog(tmp_path, "c1")
    t.add_long(now - 20000, {"z1": (1, 1, 1)}, 1)         # старше 2 часов
    t.add_long(now - 10, {"z1": (2, 2, 1)}, 1)
    t.close()
    longs, _ = mt.TrendLog(tmp_path, "c1").load(now, 7200, 86400, {"z1"})
    assert [round(x[1]["z1"][0]) for x in longs] == [2]


def test_legacy_csv_trends_move_to_db_once_and_bad_rows_are_skipped(tmp_path):
    now = time.time()
    d = tmp_path / "trends"
    d.mkdir()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 30))
    (d / "c1_20261102.csv").write_text("time;ts;zone;flow;diff;bit;ok\n%s;%.1f;z1;0.5;1.25;1;1\nмусор;без;чисел\n2026-11-02 14:00:00;12" % (stamp, now - 30),
                                      encoding="utf-8-sig")
    (d / "c1_20261102_stats.csv").write_text("time;key;zone;n;sum_flow;min_flow;max_flow;sum_diff;min_diff;max_diff\n%s;%d;z1;3;4.5;1.0;2.0;6.0;1.5;2.5\n"
                                             % (stamp, int((now - 30) // 10)), encoding="utf-8-sig")
    old = d / "c1_20260101.csv"                              # старше двух суток — не переносится
    old.write_text("time;ts;zone;flow;diff;bit;ok\n%s;%.1f;z1;9;9;1;1\n" % (stamp, now - 30), encoding="utf-8-sig")
    os.utime(old, (now - 5 * 86400, now - 5 * 86400))
    st = ms.store_for(tmp_path)
    assert st.import_legacy_trends(tmp_path) == 0, "второй раз тот же файл не переносится"
    longs, stats = mt.TrendLog(tmp_path, "c1").load(now, 7200, 86400, {"z1"})
    assert len(longs) == 1 and longs[0][1]["z1"] == (0.5, 1.25, 1), "битые и оборванные строки пропущены, старый файл не тронут"
    assert stats[0][1]["z1"] == [3, 4.5, 1.0, 2.0, 6.0, 1.5, 2.5]


def test_corrupt_db_is_set_aside_and_service_starts_with_a_new_one(tmp_path):
    (tmp_path / "motion.db").write_bytes(b"this is not a sqlite database" * 200)
    st = ms.store_for(tmp_path)
    st.add_trend("c1", time.time(), {"z1": (1, 2, 1)}, 1)
    assert len(st.load_trend("c1", time.time(), 600, 600, {"z1"})[0]) == 1
    assert (tmp_path / "motion.db.corrupt").is_file(), "битая база отложена, а не потеряна"


def test_legacy_import_survives_nul_tail_and_broken_utf8(tmp_path):
    now = time.time()
    d = tmp_path / "trends"
    d.mkdir()
    good = "%s;%.1f;z1;0.5;1.0;1;1\n" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 5)), now - 5)
    (d / "c1_20261102.csv").write_bytes(("time;ts;zone;flow;diff;bit;ok\n" + good).encode("utf-8-sig") + b"\x00\x00\x00\xd0\n" + b"\xd0")
    st = ms.store_for(tmp_path)                                  # не падает: сервис стартует
    assert len(st.load_trend("c1", now, 7200, 86400, {"z1"})[0]) == 1


def test_nan_is_stored_as_zero_not_as_huge_value(tmp_path):
    now = time.time()
    st = ms.store_for(tmp_path)
    st.add_trend("c1", now, {"z1": (float("nan"), float("inf"), 1)}, 1)
    z = st.load_trend("c1", now + 1, 600, 600, {"z1"})[0][0][1]["z1"]
    assert z[0] == 0.0 and z[1] == 1e11, "NaN -> 0, бесконечность -> предел, а не переполнение"


def test_flush_failure_is_not_retried_on_every_frame_and_data_is_kept(tmp_path):
    import sqlite3

    class Broken:
        calls = 0

        def __init__(self, real):
            self.real = real

        def executemany(self, *a):
            Broken.calls += 1
            raise sqlite3.OperationalError("database or disk is full")

        def __getattr__(self, name):
            return getattr(self.real, name)

    st = ms.store_for(tmp_path)
    st.add_trend("c1", time.time(), {"z1": (1, 1, 1)}, 1)
    st._c = Broken(st._c)
    st._flushed = 0.0
    for _ in range(5):
        try:
            st.maybe_flush()
        except sqlite3.Error:
            pass
    assert Broken.calls == 1, "после сбоя следующая попытка — через FLUSH_S, а не на каждом кадре"
    assert len(st._tr) == 1, "строки остаются в памяти до следующей попытки"


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
        rows = ms.store_for(tmp_path).shots(0, time.time() + 10)
        assert len(rows) == 2 + 1 and {r["cam"] for r in rows} == {"w", "dead"}, "по строке на зону и одна на мёртвую камеру"
        assert [r["photo"] for r in rows if r["cam"] == "dead"] == [0]
    finally:
        w.stop()


def test_record_goes_to_db_and_exports_to_the_old_csv_format(tmp_path):
    w, _ = _worker(tmp_path, make_frames(60))
    try:
        name = w.start_record()
        assert name and w.state()["recording"] == name
        st0 = ms.store_for(tmp_path)
        assert _wait(lambda: st0.runs() and sum(1 for r in st0.run_rows(st0.runs()[0]) if r[1] is not None) >= 3, 10)
        w.flush_record()
    finally:
        w.stop()
    st = ms.store_for(tmp_path)
    run = st.runs()[0]
    assert run["t1"] is not None, "остановка записи закрывает запуск"
    lines = st.export_run_csv(run).decode("utf-8-sig").splitlines()
    assert lines[0].split(";")[0] == "time" and lines[0].endswith("alive;status") and "z1_flow" in lines[0]
    assert len(lines) > 3 and len(lines[1].split(";")) == len(lines[0].split(";"))
    assert not (tmp_path / "records").exists(), "CSV-файлы больше не пишутся"


def test_unfinished_run_is_closed_on_last_row_after_crash(tmp_path):
    st = ms.MotionStore(tmp_path / "motion.db")
    run = st.rec_start("cam", ["z1"], set(), 1000.0)
    st.rec_add("cam", 1001.0, 1, "ok", {"z1": {"flow": 1, "diff": 2, "bit": 1}})
    st.rec_add("cam", 1002.5, 1, "ok", {"z1": {"flow": 1, "diff": 2, "bit": 1}})
    st.flush()
    st._c.close()                                               # «упали» без rec_stop
    st2 = ms.MotionStore(tmp_path / "motion.db")
    r = st2.runs()[0]
    assert r["id"] == run and r["t1"] == 1002.5
    st2.close()


def test_purge_removes_only_old_raw_rows_and_jpeg_but_keeps_stats_and_shot_log(tmp_path):
    now = time.time()
    old, new = now - 70 * 86400, now - 86400
    st = ms.store_for(tmp_path)
    for ts in (old, new):
        st.add_trend("c1", ts, {"z1": (1, 1, 1)}, 1)
        st.add_stat("c1", int(ts // 10), {"z1": [3, 1.0, 0.1, 0.9, 2.0, 0.2, 0.8]})
        st.rec_add("c1", ts, 1, "ok", {"z1": {"flow": 1, "diff": 1, "bit": 1}})
    st.add_shot(old, "c1", "z1", True, True, "ok", 3.0, 1.0, 1.0, 1, True)
    for sub, name in (("records", "a.csv"), ("trends", "a.csv"), ("records", "b.csv")):       # CSV прежних версий убираются по возрасту
        d = tmp_path / sub
        d.mkdir(exist_ok=True)
        (d / name).write_text("x")
    os.utime(tmp_path / "records" / "a.csv", (old, old))
    os.utime(tmp_path / "trends" / "a.csv", (old, old))
    for day, t in (("20261001", now - 20 * 86400), ("20261101", new)):
        d = tmp_path / "shots" / day
        d.mkdir(parents=True)
        os.utime(d, (t, t))
    mt.purge(tmp_path, 60, now, shots_days=10)
    assert len(st.load_trend("c1", now, 80 * 86400, 80 * 86400, {"z1"})[0]) == 1, "старая секундная точка убрана, свежая осталась"
    assert len(st.load_trend("c1", now, 80 * 86400, 80 * 86400, {"z1"})[1]) == 2, "корзины статистики хранятся навсегда"
    assert len(st.shots(0, now + 10)) == 1, "сводка снимков остаётся"
    assert (tmp_path / "records" / "b.csv").exists() and not (tmp_path / "records" / "a.csv").exists() and not (tmp_path / "trends" / "a.csv").exists()
    assert (tmp_path / "shots" / "20261101").exists() and not (tmp_path / "shots" / "20261001").exists(), "JPEG чистятся по своим суткам"


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
