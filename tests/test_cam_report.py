"""Отчёт камер за смену: окна смен, обрезка эпизодов по границам, остановки зон, подсчёт фото/видео, API."""
import json
import os
from datetime import datetime

import pytest

import cam_events
import cam_report
import paths

from test_api import get


def ts(h, m=0, day=30):
    return datetime(2026, 10, day, h, m).timestamp()


def ep(cam, kind, t0, t1, zone="", info=""):
    return {"cam": cam, "kind": kind, "zone": zone, "info": info, "t0": t0, "t1": t1}


def test_shift_windows_and_current_shift():
    assert cam_report.shift_window("2026-10-30", "day") == (ts(8), ts(20))
    assert cam_report.shift_window("2026-10-30", "night") == (ts(20), ts(8, day=31))
    assert cam_report.shift_window("2026-10-30", "all") == (ts(8), ts(8, day=31))
    with pytest.raises(ValueError):
        cam_report.shift_window("2026-10-30", "noon")
    assert cam_report.current_shift(ts(9)) == ("2026-10-30", "day")
    assert cam_report.current_shift(ts(21)) == ("2026-10-30", "night")
    assert cam_report.current_shift(ts(3, day=31)) == ("2026-10-30", "night"), "ночью до 08:00 — ещё прошлая ночная смена"


def test_summarize_clips_to_window_and_counts_stops():
    t0, t1 = ts(8), ts(20)
    eps = [
        ep("c1", "link_lost", ts(7, 50), ts(8, 10)),                       # начался до смены: считаем с 08:00 → 10 мин
        ep("c1", "gray", ts(9), ts(9) + 5),
        ep("c1", "cv_frozen", ts(10), ts(10, 5)),
        ep("c1", "zone_on", ts(11), ts(12), "z1", "Шнек"),                 # 60 мин
        ep("c1", "zone_off", ts(12), ts(12, 20), "z1", "Шнек"),            # остановка 20 мин
        ep("c1", "zone_off", ts(13), ts(13) + 3, "z1", "Шнек"),            # мигание 3 с: время идёт, в остановки — нет
        ep("c1", "zone_off", ts(14), ts(14, 2), "z1", "Шнек"),             # остановка 2 мин
        ep("c1", "zone_on", ts(19, 50), None, "z1", "Шнек"),               # идёт: считаем до «сейчас»
        ep("c2", "link_lost", ts(21), ts(22)),                             # вне окна — не попадает
    ]
    now = ts(19, 59)
    r = cam_report.summarize(eps, t0, t1, now)
    assert set(r["cams"]) == {"c1"} and r["window_s"] == now - t0
    c = r["cams"]["c1"]
    assert c["link"] == {"lost_s": 600.0, "n": 1}
    assert c["gray"] == {"s": 5.0, "n": 1} and c["cv"] == {"s": 300.0, "n": 1}
    z = c["zones"][0]
    assert z["name"] == "Шнек" and z["run_s"] == 3600.0 + 9 * 60, "60 мин + 9 мин идущего эпизода до «сейчас»"
    assert z["stop_s"] == 1200.0 + 3 + 120
    assert z["stops_n"] == 2 and z["longest_s"] == 1200.0 and [s["dur"] for s in z["top"]] == [1200.0, 120.0]
    # идущая смена: окно обрезано «сейчас», а не концом смены
    assert cam_report.summarize([], t0, t1, now)["window_s"] == now - t0


def test_count_files_by_mtime_across_projects(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    t0, t1 = ts(8), ts(20)

    def mk(rel, when):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
        os.utime(p, (when, when))

    mk("dataset/rtsp_2/a.png", ts(9))
    mk("dataset/Завод один/rtsp_2/b.jpg", ts(10))
    mk("dataset/Завод один/rtsp_2/old.jpg", ts(7))          # до смены
    mk("dataset/Завод один/rtsp_3/other.jpg", ts(10))       # другая камера
    mk("dataset/rtsp_2/notes.txt", ts(10))                  # не картинка
    mk("Videos/Завод один/rtsp_2/v.avi", ts(11))
    assert cam_report.count_files("rtsp_2", t0, t1) == {"photos": 2, "videos": 1}
    assert cam_report.count_files("net_takoy", t0, t1) == {"photos": 0, "videos": 0}


def test_report_api(data_dir, monkeypatch):
    import rtsp_store
    monkeypatch.setattr(cam_events, "DB_PATH", data_dir / "cam_events.db")
    monkeypatch.setattr(cam_events, "_conn", None)
    cam_events._open.clear()
    rtsp_store.save({"url": "rtsp://127.0.0.1:554/live", "label": "Шнек", "serial": "rtsp_7"})
    rtsp_store.save({"url": "rtsp://127.0.0.1:554/two", "label": "Пусто", "serial": "rtsp_8"})
    cam_events.sync("rtsp_7", ["zone_on", "zone_off"], {("zone_on", "z1"): "Лента"}, ts(9))
    cam_events.sync("rtsp_7", ["zone_on", "zone_off"], {("zone_off", "z1"): "Лента"}, ts(10))
    cam_events.sync("rtsp_7", ["zone_on", "zone_off"], {}, ts(10, 30))
    data = json.loads(get("/api/cam/report", "date=2026-10-30&part=day")[1])
    assert data["part"] == "day" and data["t0"] == ts(8)
    by = {c["cam"]: c for c in data["cameras"]}
    assert set(by) == {"rtsp_7", "rtsp_8"}, "камеры без событий тоже в отчёте"
    z = by["rtsp_7"]["zones"][0]
    assert by["rtsp_7"]["label"] == "Шнек" and z["name"] == "Лента" and z["run_s"] == 3600.0 and z["stop_s"] == 1800.0
    assert by["rtsp_8"]["zones"] == [] and by["rtsp_8"]["files"] == {"photos": 0, "videos": 0}
    assert "error" in json.loads(get("/api/cam/report", "date=2026-10-30&part=noon")[1])
    cam_events._conn.close()
    cam_events._conn = None
