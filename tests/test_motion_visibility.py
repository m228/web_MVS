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
                        "zones": [{"id": "L", "name": "Вал левый", "type": "cycle", "center": [250, 270],
                                   "tip_hsv": [[0, 179, 0, 60, 235, 255]]},
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
        w._store.add_trend("dif", base + i, {"L": (0.0, 0.5 if i % 10 < 8 else 0.0, 1 if i % 10 < 8 else 0), "R": (0.0, 0.5, 1), "M": (0.0, 0.0, 0)}, ok)
    return base


def test_coast_without_confidence_is_not_counted_as_found(app):
    """Удержание угла (бит 1, уверенность 0) — палка не найдена: так «залипшая» палка не рисует 100 %."""
    w = app.state.mgr.workers["dif"]
    base = (int(time.time()) // 600 - 3) * 600
    for i in range(600):
        w._store.add_trend("dif", base + i, {"L": (0.0, 0.0, 1), "R": (0.0, 0.5, 1), "M": (0.0, 0.0, 0)}, 1)
    raw = w._store.visibility("dif", {"L", "R"}, base, base + 600, 600)
    assert sum(f for _, f in raw["L"].values()) == 0
    assert sum(f for _, f in raw["R"].values()) == 600


def test_rod_found_by_reflectors_without_colour_confidence_is_counted(app, tmp_path):
    """Палку ведут отражатели, цвет в части оборота не различается (бит 1, уверенность 0): отражатель виден — «найдена».
    Без отражателя, без бита (удержание) или с отражателем только у другого вала — по-прежнему не найдена."""
    import motion_autocal_report as rep
    w = app.state.mgr.workers["dif"]
    base = (int(time.time()) // 600 - 3) * 600
    for i in range(0, 600, 10):         # случаи разнесены на 10 с, чтобы отражатель одного не засчитался соседу
        k = i // 10 % 5                 # 0 — виден в ту же секунду; 1 — виден в следующей (точка тренда шире секунды); дальше — не считать:
        bit = int(k != 2)               # 2 — удержание (бит 0); 3 — отражателя нет (строка «не виден»); 4 — виден только через 2 с
        w._store.add_trend("dif", base + i + 0.6, {"L": (200.0, 0.0, bit), "R": (10.0, 0.0, 1)}, 1)
        sec = base + i + {1: 1, 4: 2}.get(k, 0)
        w._store.add_tip("dif", sec, {"L": (k == 1, k not in (1, 3)), "R": (False, False)})
    raw = w._store.visibility("dif", {"L", "R"}, base, base + 600, 600)
    assert [sum(x) for x in zip(*raw["L"].values())] == [60, 24]          # k == 0 и k == 1
    assert sum(f for _, f in raw["R"].values()) == 0                      # отражатель левого вала правому не помогает
    loss = w._store.loss_map("dif", {"L", "R"}, base, base + 600)
    assert sum(f for _, f in loss["L"].values()) == 24 and sum(f for _, f in loss["R"].values()) == 0
    w._store.flush()
    slots = rep.load_trend(tmp_path / "data" / "motion.db", "dif", base, base + 600)
    assert sum(f for _, f in slots["L"].values()) == 24 and sum(f for _, f in slots["R"].values()) == 0


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


# ───────────── видимость отражателей: красный и синий конец по секундам ─────────────
def _zs(lvl_tr, lvl_tb, coast=False, tip_on=True, zid="L", typ="cycle"):
    return {"id": zid, "type": typ, "tip_on": tip_on, "lvl_tr": lvl_tr, "lvl_tb": lvl_tb, "coast": coast}


def test_tip_visibility_share_by_end_and_only_for_zones_with_tips(app):
    w = app.state.mgr.workers["dif"]
    base = (int(time.time()) // 600 - 3) * 600
    for i in range(600):                                   # красный виден 8 с из 10, синий — 5 с из 10
        w._store.add_tip("dif", base + i, {"L": (i % 10 < 8, i % 10 < 5)})
    r = w.visibility(3600)
    assert set(r["tips"]) == {"L"}                          # у правого вала отражателей нет — линии нет
    assert r["tips_avg"]["L"] == {"r": 80.0, "b": 50.0}
    j = r["t"].index(base + 30)
    assert r["tips"]["L"]["r"][j] == pytest.approx(80.0, abs=4) and r["tips"]["L"]["b"][j] == pytest.approx(50.0, abs=4)
    assert r["tips"]["L"]["r"][0] is None                   # до начала данных — пусто, а не 0 %


def test_tip_second_bucket_needs_live_frame_threshold_and_no_coast(app):
    w = app.state.mgr.workers["dif"]
    t0 = 7000.0
    with w._lock:
        w._tip_add(t0 + 0.1, [_zs(0.1, 0.1)])                                  # слабый кончик — не виден
        w._tip_add(t0 + 0.5, [_zs(0.5, 0.1)])                                  # красный виден хотя бы в одном кадре секунды
        w._tip_add(t0 + 0.9, [_zs(0.9, 0.9, coast=True)])                      # достроено по скорости — не считается
        w._tip_add(t0 + 0.9, [_zs(0.9, 0.9, tip_on=False, zid="R")])           # отражатели не настроены — зоны нет
        w._tip_add(t0 + 0.9, [_zs(0.9, 0.9, typ="motion", zid="M")])           # не палка
        w._tip_add(t0 + 1.2, [_zs(0.0, 0.0)])                                  # следующая секунда — прошлая уходит на диск
    raw = w._store.tip_visibility("dif", {"L", "R", "M"}, t0, t0 + 1, 1)
    assert raw["L"] == {int(t0): (1, 1, 0)} and raw["R"] == {} and raw["M"] == {}


def test_tip_rows_are_purged_with_raw_trends(app):
    w = app.state.mgr.workers["dif"]
    w._store.add_tip("dif", 1000.0, {"L": (True, True)})
    w._store.add_tip("dif", time.time(), {"L": (True, True)})
    w._store.purge_raw(time.time() - 86400, 0)
    raw = w._store.tip_visibility("dif", {"L"}, 0, time.time() + 10, 100000)
    assert sum(n for n, _, _ in raw["L"].values()) == 1


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


# ── карта потерь: по секторам угла и часам суток ──

def _day_start():
    t = time.localtime(time.time() - 86400)
    return int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1)))


def _fill_loss(w):
    """Вчера: час 0 — палка на 90°, всегда найдена; час 1 — на 200°: 600 с найдена, 600 с удержание (бит 0, угол 200), 600 с полная потеря (угла нет),
    ещё 1800 с камера «мёртвая» (ok=0) — в расчёт не входят."""
    base = _day_start()
    for i in range(3600):
        w._store.add_trend("dif", base + i, {"L": (90.0, 0.5, 1), "R": (10.0, 0.5, 1)}, 1)
    for i in range(3600, 3600 + 1800):
        k = (i - 3600) // 600
        z = (200.0, 0.5, 1) if k == 0 else (200.0, 0.0, 0) if k == 1 else (0.0, 0.0, 0)
        w._store.add_trend("dif", base + i, {"L": z, "R": (10.0, 0.5, 1)}, 1)
    for i in range(5400, 7200):
        w._store.add_trend("dif", base + i, {"L": (200.0, 0.5, 1), "R": (10.0, 0.5, 1)}, 0)
    return base


def test_loss_map_by_sector_and_hour(app):
    w = app.state.mgr.workers["dif"]
    _fill_loss(w)
    r = w.loss_map(3 * 86400)
    assert r["sector_deg"] == 15 and set(r["zones"]) == {"L", "R"}
    z = r["zones"]["L"]
    assert z["hour"]["n"][0] == 3600 and z["hour"]["f"][0] == 3600
    assert z["hour"]["n"][1] == 1800 and z["hour"]["f"][1] == 600          # мёртвые 1800 с не считаются; потеря угла — в часе есть
    assert z["sector"]["n"][6] == 3600 and z["sector"]["f"][6] == 3600     # 90° → сектор 6
    assert z["sector"]["n"][13] == 1200 and z["sector"]["f"][13] == 600    # 200° → сектор 13: найдена + удержание; полная потеря в сектор не попадает
    assert sum(z["sector"]["n"]) == 3600 + 1200
    assert z["avg"] == pytest.approx(100.0 * 4200 / 5400, abs=0.1) and z["seconds"] == 5400
    assert r["zones"]["R"]["sector"]["n"][0] == 5400 and r["zones"]["R"]["avg"] == 100.0


def test_loss_map_window_and_endpoint(app):
    w = app.state.mgr.workers["dif"]
    _fill_loss(w)
    call = next(r.endpoint for r in app.router.routes if getattr(r, "path", "") == "/cameras/{cid}/loss_map")
    assert call("dif", 3 * 86400)["zones"]["L"]["seconds"] == 5400
    assert call("dif", 1e9)["window_s"] == 14 * 86400                     # окно ограничено
    assert w.loss_map(3600)["zones"]["L"]["seconds"] == 0                 # час назад данных нет — пусто, не ошибка
    assert w.loss_map(3600)["zones"]["L"]["avg"] is None
