"""Сторож подгонки валов в сборе: настройка камеры не перезапускает поток, события идут в журнал приложения, отчёт «день / ночь» собирается."""
import json
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

MS = Path(__file__).resolve().parent.parent / "motion_service"
sys.path.insert(0, str(MS))

import cam_monitor  # noqa: E402
import logger  # noqa: E402
import motion_autocal_report as rep  # noqa: E402
import motion_engine as me  # noqa: E402
import server as srv  # noqa: E402


def _cam(**over):
    cam = {"id": "c1", "name": "val_Down", "url": "rtsp://x", "zones": [], "frame_size": [1920, 1080]}
    cam.update(over)
    return me.normalize_config({"cameras": [cam]})["cameras"][0]


def test_switching_watch_does_not_restart_camera():
    old, new = _cam(), _cam(autocal_watch={"on": True, "min_pct": 90})
    worker = SimpleNamespace(cfg=old)
    assert srv._strip_hot(old) == srv._strip_hot(new)
    assert srv._hot_update(worker, new) is True and worker.cfg["autocal_watch"] == {"on": True, "min_pct": 90.0, "check_min": 30.0, "cooldown_h": 2.0}
    assert srv._hot_update(SimpleNamespace(cfg=_cam()), _cam(proc_fps=5)) is False        # другие настройки по-прежнему перезапускают


def test_monitor_moves_watch_events_to_journal_once():
    ev = {"id": int(time.time() * 1000), "ts": time.time(), "cam": "c1", "kind": "apply", "level": "info", "msg": "применил новые настройки", "data": {"zones": ["rod1"]}}
    mon = cam_monitor.Monitor(lambda: {}, lambda: "u", lambda url: {"cameras": [], "autocal_events": [ev]}, clock=lambda: 5.0)
    before = logger.get_events(0)["last_id"]
    mon.tick()
    mon.tick()
    items = [i for i in logger.get_events(before)["items"] if "Автоподгонка валов" in i["message"]]
    assert len(items) == 1 and "применил новые настройки" in items[0]["message"] and items[0]["payload"]["kind"] == "apply"


def make_data(tmp: Path, now: float, day_pct: float, night_pct: float, days: int = 3):
    """data сервиса: секундные тренды камеры c1/rod1 (днём day_pct %, ночью night_pct %) и конфиг (без правок за всё время)."""
    db = sqlite3.connect(str(tmp / "motion.db"))
    db.execute("CREATE TABLE names (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)")
    db.execute("CREATE TABLE trend (cam INTEGER NOT NULL, ts INTEGER NOT NULL, zone INTEGER NOT NULL, flow INTEGER NOT NULL, diff INTEGER NOT NULL, "
               "bit INTEGER NOT NULL, ok INTEGER NOT NULL, PRIMARY KEY (cam, ts, zone)) WITHOUT ROWID")
    db.executemany("INSERT INTO names VALUES (?,?)", [(1, "c1"), (2, "rod1")])
    start = int(now - days * 86400)
    rows = []
    for s in range(start, int(now)):
        pct = day_pct if rep.is_day(s) else night_pct
        found = s % 100 < pct
        rows.append((1, s * 10, 2, 100, 5 if found else 0, 1 if found else 0, 1))
    db.executemany("INSERT INTO trend VALUES (?,?,?,?,?,?,?)", rows)
    db.commit()
    db.close()
    cam = {"id": "c1", "name": "val_Down", "url": "rtsp://x", "frame_size": [1920, 1080],
           "zones": [{"id": "rod1", "name": "Вал левый", "type": "cycle", "enabled": True, "points": [], "center": [100, 100], "radius": 50}]}
    (tmp / "motion_config.json").write_text(json.dumps({"version": 1, "cameras": [cam]}), encoding="utf-8")
    (tmp / "backups").mkdir()
    return cam


def test_report_day_night_verdict_and_thresholds(tmp_path):
    now = time.time()
    make_data(tmp_path, now, day_pct=98, night_pct=92)
    cam = me.normalize_config(json.loads((tmp_path / "motion_config.json").read_text(encoding="utf-8")))["cameras"][0]
    page = rep.build_report(tmp_path, "c1", cam, days=3, shots=False, now=now)
    assert "<title>Валы: день и ночь</title>" in page
    assert "палки находятся днём в 98 %" in page and "ночью в 92 %" in page and "ночью ниже на 6 п.п." in page
    assert "Какой порог выбрать" in page and "ниже 95 %" in page


def test_service_endpoints_report_watch_and_state(tmp_path):
    from fastapi import HTTPException
    now = time.time()
    cam = make_data(tmp_path, now, 98, 92)
    cam["enabled"] = False                                          # поток не открываем
    cam["autocal_watch"] = {"on": True}
    (tmp_path / "motion_config.json").write_text(json.dumps({"version": 1, "cameras": [cam]}), encoding="utf-8")
    app = srv.create_app(tmp_path)                                   # без lifespan: потоки камер не поднимаем, вызываем обработчики напрямую
    app.state.mgr.load()
    ep = {r.path: r.endpoint for r in app.routes}
    r = ep["/cameras/{cid}/autocal/report"]("c1", days=3, shots=0)
    assert r.media_type.startswith("text/html") and "Валы: день и ночь" in r.body.decode("utf-8")
    try:
        ep["/cameras/{cid}/autocal/report"]("nope")
        raise AssertionError("нет камеры — должна быть 404")
    except HTTPException as e:
        assert e.status_code == 404
    assert set(ep["/autocal/watch"]()) == {"cameras", "events"}
    assert ep["/state"]()["autocal_events"] == []
    assert app.state.mgr.cfg["cameras"][0]["autocal_watch"]["on"] is True


def test_report_without_data_does_not_crash(tmp_path):
    now = time.time()
    cam = make_data(tmp_path, now, 98, 92, days=0)
    page = rep.build_report(tmp_path, "c1", me.normalize_config({"cameras": [cam]})["cameras"][0], days=3, shots=False, now=now)
    assert "вывод о влиянии света сделать нельзя" in page
