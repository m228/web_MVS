"""Видимость палок: доля времени, когда палка найдена, окнами за период (по секундным трендам на диске)."""
import importlib.util
import sys
import time
from pathlib import Path

import pytest

MS = Path(__file__).resolve().parent.parent / "motion_service"
sys.path.insert(0, str(MS))


@pytest.fixture()
def app(tmp_path):
    spec = importlib.util.spec_from_file_location("motion_server_vis", MS / "server.py")
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    app = srv.create_app(tmp_path / "data")
    cfg = {"cameras": [{"id": "dif", "name": "d", "enabled": False, "url": "x", "frame_size": [960, 540],
                        "zones": [{"id": "L", "name": "Вал левый", "type": "cycle", "center": [250, 270]},
                                  {"id": "R", "name": "Вал правый", "type": "cycle", "center": [700, 270]},
                                  {"id": "M", "name": "Лента", "type": "motion", "points": [[10, 10], [50, 10], [50, 50]]}],
                        "groups": []}]}
    put = next(r.endpoint for r in app.router.routes if getattr(r, "path", "") == "/config" and "PUT" in r.methods)
    put(cfg)
    return app


def _fill(w, seconds=1200):
    """20 минут секундных трендов: левая найдена 8 секунд из 10, правая всегда; 100 секунд камера «мёртвая» (ok=0)."""
    base = (int(time.time()) // 600 - 3) * 600
    for i in range(seconds):
        ok = 0 if 300 <= i < 400 else 1
        w._store.add_trend("dif", base + i, {"L": (0.0, 0.0, 1 if i % 10 < 8 else 0), "R": (0.0, 0.0, 1), "M": (0.0, 0.0, 0)}, ok)
    return base


def test_visibility_share_and_dead_camera_is_not_counted_as_lost_rod(app):
    w = app.state.mgr.workers["dif"]
    base = _fill(w)
    r = w.visibility(3600)
    assert r["step_s"] == 30 and set(r["zones"]) == {"L", "R"}          # лента (не палка) в график не идёт
    assert r["names"]["L"] == "Вал левый"
    assert r["avg"]["L"] == pytest.approx(80.0, abs=0.2) and r["avg"]["R"] == 100.0     # мёртвые 100 с в среднее не входят
    ts = r["t"]
    i = ts.index(base + 330)                                              # окно целиком внутри «мёртвых» секунд: данных нет, не 0 %
    assert r["zones"]["L"][i] is None and r["zones"]["R"][i] is None
    j = ts.index(base + 30)
    assert r["zones"]["L"][j] == pytest.approx(80.0, abs=4) and r["zones"]["R"][j] == 100.0
    assert r["zones"]["L"][0] is None                                     # до начала записи данных нет


@pytest.mark.parametrize("window, step", [(300, 30), (3600, 30), (86400, 1200), (3 * 86400, 3600), (30 * 86400, 3600)])
def test_visibility_step_keeps_points_reasonable_and_window_is_capped(app, window, step):
    r = app.state.mgr.workers["dif"].visibility(window)
    assert r["step_s"] == step and len(r["t"]) <= 170 and r["window_s"] <= 7 * 86400


def test_visibility_endpoint(app):
    call = next(r.endpoint for r in app.router.routes if getattr(r, "path", "") == "/cameras/{cid}/visibility")
    _fill(app.state.mgr.workers["dif"])
    r = call("dif", 3600)
    assert r["avg"]["R"] == 100.0 and len(r["t"]) == len(r["zones"]["L"])
