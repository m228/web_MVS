"""motion_sync: остановки лент и сбои камер из корзин motion.db -> очередь sysmon -> таблица приёмника и блок «Остановки лент · 5 сут»."""
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "motion_service"))
sys.path.insert(0, str(ROOT / "monitor_server"))

import monitor_store as mon  # noqa: E402
import motion_store as mst  # noqa: E402
import motion_sync as msy  # noqa: E402
import sysmon  # noqa: E402

NOW = 1_700_000_000.0
ZC = {"name": "Лента", "metric": "flow", "on": 0.5, "off": 0.2}


def _bins(spec, t0=NOW - 4 * 3600):
    """spec = [(секунд, значение flow)] подряд корзинами по 10 с; значение None — дыра (нет кадров в норме)."""
    out, k = [], int(t0 // 10)
    for sec, v in spec:
        for _ in range(sec // 10):
            if v is not None:
                out.append((k, 3, v, v, v, v))
            k += 1
    return out


def _kinds(eps):
    return [(e["kind"], int(e["t1"] - e["t0"])) for e in eps]


# -------------------------------------------------------------------------------------------------------------- эпизоды

def test_stop_found_and_short_dip_ignored():
    bins = _bins([(600, 1.0), (300, 0.05), (600, 1.0), (60, 0.05), (600, 1.0)])        # 5 мин стоит и 1 мин (меньше порога)
    eps = msy.zone_episodes(bins, ZC, NOW)
    stops = [e for e in eps if e["kind"] == "stop"]
    assert _kinds(stops) == [("stop", 300)] and not stops[0]["open"]
    assert stops[0]["min"] == pytest.approx(0.05)


def test_hysteresis_between_thresholds_keeps_state():
    # 0.3 между off=0.2 и on=0.5: после «крутится» остаётся «крутится», после «стоит» остаётся «стоит»
    run = msy.zone_episodes(_bins([(600, 1.0), (600, 0.3), (600, 1.0)]), ZC, NOW)
    assert not [e for e in run if e["kind"] == "stop"]
    stop = msy.zone_episodes(_bins([(300, 0.05), (600, 0.3), (300, 1.0)]), ZC, NOW)
    assert _kinds([e for e in stop if e["kind"] == "stop"]) == [("stop", 900)]


def test_near_threshold_only_while_running():
    # 0.21 — выше off (0.2), но в пределах 15 %: «близко»; 0.8 — нормально
    eps = msy.zone_episodes(_bins([(300, 0.8), (120, 0.21), (300, 0.8)]), ZC, NOW)
    assert [e["kind"] for e in eps] == ["near"] and eps[0]["min"] == pytest.approx(0.21)
    assert not msy.zone_episodes(_bins([(300, 0.8), (30, 0.21), (300, 0.8)]), ZC, NOW)       # короче минуты — не считаем


def test_camera_gap_is_not_a_stop():
    # посередине дыра 10 минут (нет кадров в норме): камера не в порядке, а «лента стоит» в дыру не судим
    bins = _bins([(300, 1.0), (600, None), (300, 1.0)])
    assert not msy.zone_episodes(bins, ZC, NOW)
    gaps = msy.camera_gaps(sorted({b[0] for b in bins}), NOW, NOW - 86400)
    assert len(gaps) == 1 and 590 <= gaps[0][1] - gaps[0][0] <= 600
    assert not msy.camera_gaps(sorted({b[0] for b in _bins([(300, 1.0), (60, None), (300, 1.0)])}), NOW, 0)      # минута — не сбой


def test_bins_without_any_motion_math_are_not_a_stop():
    # зону держали «по яркости» (flow = diff = 0), потом вернули движение: за тот отрезок остановок не выдумываем
    assert not msy.zone_episodes(_bins([(300, 1.0), (1800, 0.0), (300, 1.0)]), ZC, NOW)
    eps = msy.zone_episodes(_bins([(1800, 0.0), (300, 1.0), (300, 0.05), (300, 1.0)]), ZC, NOW)
    assert _kinds([e for e in eps if e["kind"] == "stop"]) == [("stop", 300)]


def test_open_stop_at_the_end():
    eps = msy.zone_episodes(_bins([(600, 1.0), (300, 0.05)], t0=NOW - 900), ZC, NOW)
    assert eps[0]["kind"] == "stop" and eps[0]["open"] is True


# ----------------------------------------------------------------------------------------- сбор из настоящей motion.db

def _make_db(tmp_path):
    cfg = {"cameras": [{"id": "c1", "name": "Лента 1", "zones": [
        {"id": "z1", "name": "Вход", "type": "motion", "metric": "flow", "on": 0.5, "off": 0.2},
        {"id": "lvl", "name": "Уровень", "type": "level"}, {"id": "p", "type": "presence", "on": 1, "off": 0.5, "invert": True}]}]}
    (tmp_path / "motion_config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    st = mst.MotionStore(tmp_path / "motion.db")
    t0 = NOW - 3 * 3600
    spec = [(1200, 1.0), (300, 0.05), (1200, 1.0), (900, None), (1200, 1.0)]
    k = int(t0 // 10)
    for sec, v in spec:
        for _ in range(sec // 10):
            if v is not None:
                st.add_stat("c1", k, {"z1": [3, 3 * v, v, v, 3 * v, v, v]})
            k += 1
    st.flush()
    st.close()
    return tmp_path / "motion.db", tmp_path / "motion_config.json"


def test_collect_reads_real_db(tmp_path):
    db, cfg = _make_db(tmp_path)
    assert list(msy.load_zones(cfg)["c1"]["zones"]) == ["z1"]           # уровень и «жом» не судим
    res = msy.collect(db, cfg, now=NOW)
    kinds = sorted(e["kind"] for e in res["episodes"])
    assert kinds == ["camera", "stop"] and res["cams"] == ["c1"]
    cam = next(e for e in res["episodes"] if e["kind"] == "camera")
    assert 880 <= cam["t1"] - cam["t0"] <= 900 and cam["reason"]
    eids = [e["eid"] for e in res["episodes"]]
    assert len(set(eids)) == 2 and all(e.startswith("mo-") for e in eids)
    assert [e["eid"] for e in msy.collect(db, cfg, now=NOW)["episodes"]] == [e["eid"] for e in res["episodes"]]       # eid стабилен между прогонами


def test_collect_without_db_or_zones(tmp_path):
    assert msy.collect(tmp_path / "нет.db", tmp_path / "нет.json", now=NOW)["episodes"] == []


# ------------------------------------------------------------------------------- sysmon: очередь, дописывание, подтверждение

@pytest.fixture
def sm(tmp_path, monkeypatch):
    sysmon.close()
    monkeypatch.setattr(sysmon, "DB_PATH", tmp_path / "sysmon.db")
    monkeypatch.setattr(sysmon, "CONFIG_PATH", tmp_path / "monitor.json")
    sysmon.save_config({"enabled": True, "url": "https://x.example", "token": "T" * 24})
    yield sysmon
    sysmon.close()


def _agent(sm, results):
    a = sm.Agent(sources={"motion_episodes": lambda: results[0]}, clock=lambda: NOW)
    a._cfg = sm.load_config()
    return a


def _res(*eps, to=NOW):
    return {"from": to - 5 * 86400, "to": to, "cams": ["c1"], "episodes": list(eps)}


def _ep(t0, t1, kind="stop", open_=False):
    return {"kind": kind, "cam": "c1", "cam_name": "Лента 1", "zone": "z1", "zone_name": "Вход", "t0": t0, "t1": t1, "open": open_, "min": 0.05, "off": 0.2,
            "eid": "mo-%s-c1-z1-%d" % (kind, t0)}


def test_sync_queues_only_new_and_changed(sm):
    box = [_res(_ep(NOW - 1000, NOW - 700), _ep(NOW - 500, NOW - 300, open_=True))]
    a = _agent(sm, box)
    r = a.sync_motion(NOW, force=True)
    assert r["queued"] == 2
    kinds = [e["kind"] for e in sm.outbox_batch()]
    assert kinds.count("motion") == 2 and kinds.count("motion_sync") == 1
    # повтор без изменений: эпизоды в очереди уже есть — не дублируются
    assert a.sync_motion(NOW + 1, force=True)["queued"] == 0
    # сервер принял -> очередь пуста; повтор без изменений по-прежнему ничего не кладёт
    sm.outbox_done([e["eid"] for e in sm.outbox_batch()])
    assert a.sync_motion(NOW + 2, force=True)["queued"] == 0
    # остановка закончилась (изменился конец и open) — уходит обновление
    box[0] = _res(_ep(NOW - 1000, NOW - 700), _ep(NOW - 500, NOW - 200))
    assert a.sync_motion(NOW + 3, force=True)["queued"] == 1


def test_unconfirmed_and_lost_from_queue_is_resent(sm):
    box = [_res(_ep(NOW - 1000, NOW - 700))]
    a = _agent(sm, box)
    a.sync_motion(NOW, force=True)
    with sysmon._lock:                                  # очередь потеряли (предел), подтверждения не было
        sysmon._db().execute("DELETE FROM outbox")
        sysmon._db().commit()
    assert a.sync_motion(NOW + 1, force=True)["queued"] == 1


def test_motion_flood_does_not_evict_urgent_events(sm):
    sm.outbox_add("alert", "high", "важное", {"code": "x"})
    for i in range(sm.MOTION_KEEP + 50):
        sm.outbox_add("motion", "info", "", {"kind": "stop"}, eid="mo-%d" % i)
    batch = sm.outbox_batch(5)
    assert batch[0]["kind"] == "alert"                  # срочное идёт первым и не вытеснено
    assert sum(1 for e in sm.outbox_batch(10000) if e["kind"] == "motion") == sm.MOTION_KEEP


def test_not_time_yet_and_off(sm):
    a = _agent(sm, [_res(_ep(NOW - 1000, NOW - 700))])
    assert a.sync_motion(NOW) == {}                      # меньше MOTION_FIRST_DELAY_S после старта
    a.started_at = NOW - 1000
    assert a.sync_motion(NOW)["queued"] == 1
    assert a.sync_motion(NOW + 60) == {}                 # следующая — через motion_sync_h часов


# -------------------------------------------------------------------------------- приёмник: таблица, обновление, сводка

@pytest.fixture
def store(tmp_path):
    t = [NOW + 100]
    s = mon.Store(tmp_path / "monitor.db", clock=lambda: t[0])
    s.clock_t = t
    yield s
    s.close()


def _ingest(store, pid, events):
    return store.ingest(pid, {"schema": 1, "snapshot": {"t": NOW}, "events": events})


def _ev(eid, kind, data):
    return {"eid": eid, "t": NOW, "kind": kind, "level": "info", "text": "", "data": data}


def test_receiver_stores_updates_and_summarizes(store):
    pid, _ = store.create_plant("Кир")
    ep = _ep(NOW - 600, NOW - 300, open_=True)
    sync = _ev("mo-sync-1", "motion_sync", {"from": NOW - 5 * 86400, "to": NOW, "cams": ["c1"]})
    _ingest(store, pid, [_ev(ep["eid"], "motion", ep), sync])
    m = store.motion(pid)
    assert m["cover"]["cams"] == ["c1"] and m["count"] == {"stop": 1, "near": 0, "camera": 0} and m["stop"][0]["open"] is True
    # то же событие позже: остановка закончилась — запись обновляется, а не дублируется
    ep2 = dict(ep, t1=NOW - 100, open=False)
    _ingest(store, pid, [_ev(ep["eid"], "motion", ep2)])
    m = store.motion(pid)
    assert m["count"]["stop"] == 1 and m["stop"][0]["open"] is False and m["stop_s"] == pytest.approx(500)
    # в журнал событий они не попадают
    assert not [e for e in store.events(pid, 50) if e["kind"] in ("motion", "motion_sync")]
    # в карточке панели
    view = [p for p in store.plants_view() if p["id"] == pid][0]
    assert view["motion5"]["count"]["stop"] == 1


def test_receiver_without_data_and_bad_input(store):
    pid, _ = store.create_plant("Буи")
    assert store.motion(pid)["cover"] is None
    _ingest(store, pid, [_ev("a", "motion", {"kind": "bogus", "t0": 1, "t1": 2}), _ev("b", "motion", {"kind": "stop", "t0": 5, "t1": 1}),
                         _ev("c", "motion", {"kind": "stop"}), _ev("d", "motion_sync", {})])
    m = store.motion(pid)
    assert m["count"] == {"stop": 0, "near": 0, "camera": 0} and m["cover"] is None


def test_old_episodes_not_in_window_and_purged(store):
    pid, _ = store.create_plant("Усп")
    old = _ep(NOW - 10 * 86400, NOW - 10 * 86400 + 300)
    _ingest(store, pid, [_ev(old["eid"], "motion", old)])
    assert store.motion(pid, 5)["count"]["stop"] == 0 and store.motion(pid, 30)["count"]["stop"] == 1
    store.clock_t[0] = NOW + 100 + 100 * 86400
    store.purge()
    assert store.motion(pid, 90)["count"]["stop"] == 0


def test_panel_has_stops_block_and_view_key():
    assert "stops" in mon.VIEW_BLOCKS
    html = (ROOT / "monitor_server" / "panel.html").read_text(encoding="utf-8")
    assert "stopsHtml" in html and "Остановки лент" in html


# -------------------------------------------------------------------------------- глубина базы и заводы с микроскопом

def test_collect_from_is_first_record_not_window_edge(tmp_path):
    db, cfg = _make_db(tmp_path)                         # данные только за последние ~3 часа
    res = msy.collect(db, cfg, now=NOW)
    assert NOW - 3 * 3600 - 20 <= res["from"] <= NOW - 3 * 3600 + 20 and res["from"] > NOW - msy.DAYS * 86400


def test_plant_with_microscope_sends_no_motion(sm):
    a = sm.Agent(sources={"motion_episodes": lambda: _res(_ep(NOW - 1000, NOW - 700)), "micro_on": lambda: True}, clock=lambda: NOW)
    a._cfg = sm.load_config()
    assert a.sync_motion(NOW, force=True) == {}
    assert not [e for e in sm.outbox_batch() if e["kind"].startswith("motion")]


def test_receiver_motion_window_follows_plant_depth(store):
    pid, _ = store.create_plant("Лен")
    store.motion(pid, None)                              # завод ещё ничего не присылал — не падаем
    _ingest(store, pid, [_ev("mo-sync-2", "motion_sync", {"from": NOW - 12 * 86400, "to": NOW, "cams": ["c1"]})])
    m = store.motion(pid, None)
    assert m["cover"]["from"] == pytest.approx(NOW - 12 * 86400) and m["days"] == pytest.approx(12, abs=0.1)
