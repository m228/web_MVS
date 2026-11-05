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


# ───────────── история углов: сжатие окна не должно портить сумму углов двух валов ─────────────
def _wrap(x):
    return (x + 180.0) % 360.0 - 180.0


def _rot_rows(seconds=3000, rpm=0.8):
    """Секундные строки: валы крутятся навстречу, сумма углов ровно 360 (Δ = 0); лента (не палка) — для проверки максимума."""
    w = rpm * 360.0 / 60.0
    return [(1000.0 + t, {"L": ((360.0 - w * t) % 360.0, 0.5, 1), "R": ((w * t) % 360.0, 0.5, 1), "M": (0.1 + (t % 7) * 0.01, 0.2, t % 5 == 0)}, 1)
            for t in range(seconds)]


def test_squeeze_keeps_both_angles_from_one_moment_so_delta_does_not_drift():
    import motion_camera as mc
    rows = _rot_rows()
    old = mc._squeeze(rows, 100)                                         # прежнее поведение: максимум углов в корзине
    assert max(abs(_wrap(z["L"][0] + z["R"][0])) for _, z, _ok in old) > 10          # воспроизводит «график уезжает при длинном окне»
    new = mc._squeeze(rows, 100, frozenset({"L", "R"}))
    assert len(new) <= 100
    assert max(abs(_wrap(z["L"][0] + z["R"][0])) for _, z, _ok in new) < 0.01      # оба вала из одной и той же секунды
    assert all(abs(a[1]["M"][0] - b[1]["M"][0]) >= 0 for a, b in zip(new, old))     # не-палки по-прежнему сжимаются максимумом
    assert max(z["M"][0] for _, z, _ok in new) == max(z["M"][0] for _, z, _ok in old)


def test_history_endpoint_squeezes_angles_without_drift(app):
    w = app.state.mgr.workers["dif"]
    for ts, z, ok in _rot_rows(1500):
        w._long.append((ts, z, ok))
    h = w.history(window_s=3600, points=60)
    assert len(h["t"]) <= 60
    assert max(abs(_wrap(a + b)) for a, b in zip(h["zones"]["L"]["flow"], h["zones"]["R"]["flow"])) < 0.01


def test_second_bucket_takes_the_latest_found_frame_not_the_max_angle(app):
    w = app.state.mgr.workers["dif"]
    frames = [(5000.0, 10.0, 1), (5000.4, 12.4, 1), (5000.8, 99.0, 0)]            # третий кадр: палку потеряли, угол достроен
    with w._lock:
        for ts, ang, bit in frames:
            w._push_history(ts, {"L": (ang, 0.5, bit), "M": (ts % 3, 0.2, 0)}, 1)
        w._push_history(5001.2, {"L": (20.0, 0.5, 1), "M": (0.0, 0.0, 0)}, 1)     # следующая секунда — прошлая уходит в длинную историю
        sec = w._long[-1]
    assert sec[1]["L"] == (12.4, 0.5, 1)                                          # самый поздний найденный кадр, бит «найдена хотя бы раз»
    assert sec[1]["M"][0] == max(5000.0 % 3, 5000.4 % 3, 5000.8 % 3)             # не-палка — максимум, как раньше


def test_mirror_stats_average_min_max_with_correction_skip_hold(app):
    w = app.state.mgr.workers["dif"]
    now = time.time()
    g = lambda d, cr, valid=True, hold=False: {"type": "mirror", "id": "sync", "valid": valid, "hold": hold, "delta": d, "corr": cr}
    with w._lock:
        for i, (d, cr) in enumerate([(1.0, 3.0), (-1.0, 5.0), (3.0, -2.0)]):
            w._mirror_stats_add(now - 100 - i * 20, [g(d, cr)])               # три разные 10-секундные корзины
        w._mirror_stats_add(now - 90, [g(50.0, 0.0, hold=True)])               # удержание — не считаем
        w._mirror_stats_add(now - 90, [g(None, 0.0, valid=False)])             # палки не видно — не считаем
        w._mirror_stats_add(now - 90, [{"type": "sum", "id": "other"}])
    s = w.stats(3600)["groups"]
    assert set(s) == {"sync"} and s["sync"]["n"] == 3
    assert s["sync"]["delta"] == {"avg": 1.0, "min": -1.0, "max": 3.0}
    assert s["sync"]["corr"] == {"avg": 2.0, "min": -2.0, "max": 5.0}
    assert w.stats(30)["groups"] == {}                                           # окно короче данных — пусто, не ошибка
