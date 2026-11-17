"""Четыре вала на двух камерах: скорости и разности (НЛВ, НПВ, ВЛВ, ВПВ) — motion_shafts.py.

Без камер и сети: камеры — заглушки с готовым state(), ПЛК — фейковый клиент.
"""
import sys
from pathlib import Path

import pytest

MS = Path(__file__).resolve().parent.parent / "motion_service"
sys.path.insert(0, str(MS))

import motion_engine as me  # noqa: E402
import motion_modbus as mm  # noqa: E402
import motion_shafts as ms  # noqa: E402

SQUARE = [[0, 0], [10, 0], [10, 10], [0, 10]]


def cycle(zid, cx):
    return {"id": zid, "name": zid, "type": "cycle", "center": [cx, 300], "radius": 100, "points": SQUARE}


def cam(cid, row, zones):
    return {"id": cid, "name": cid, "url": "x", "frame_size": [1920, 1080], "row": row, "zones": zones}


def cfg_two_rows(extra=None):
    raw = {"cameras": [cam("dn", "lower", [cycle("r_right", 1400), cycle("r_left", 500)]),      # порядок в конфиге не по x — сортируем сами
                       cam("up", "upper", [cycle("l", 500), cycle("r", 1400)])],
           "modbus": {"enable": True, "host": "plc", "write_period_s": 0.2}}
    raw.update(extra or {})
    return me.normalize_config(raw)


def zst(zid, angle, rpm, valid=True, coast=False):
    return {"id": zid, "angle": angle, "rpm": rpm, "valid": valid, "coast": coast}


def states(dn=(100.0, 260.0, 5.0, -5.0), up=(100.0, 260.0, 4.0, -4.0), alive=True):
    """dn/up = (угол левого, угол правого, rpm левого, rpm правого)."""
    return {"dn": {"result": {"alive": alive, "zones": [zst("r_left", dn[0], dn[2]), zst("r_right", dn[1], dn[3])]}},
            "up": {"result": {"alive": alive, "zones": [zst("l", up[0], up[2]), zst("r", up[1], up[3])]}}}


def test_camera_row_validated_and_shafts_block_always_full():
    cfg = cfg_two_rows()
    assert [c["row"] for c in cfg["cameras"]] == ["lower", "upper"]
    assert set(cfg["shafts"]["shafts"]) == set(me.SHAFT_IDS) and set(cfg["shafts"]["pairs"]) == set(me.PAIR_IDS)
    assert cfg["shafts"]["pairs"]["nlr"] == {"ref": 0.0, "tol_angle": 0.0, "tol_rpm": 0.0, "out_angle": None, "out_rpm": None}
    with pytest.raises(ValueError, match="ряд валов"):
        me.normalize_config({"cameras": [{"id": "a", "url": "x", "row": "middle"}]})
    assert me.normalize_config({"cameras": [{"id": "a", "url": "x"}]})["cameras"][0]["row"] == ""        # старый конфиг без поля
    assert me.normalize_config({})["shafts"] == me.normalize_shafts(None)


def test_shafts_block_validates_and_survives_normalize_twice():
    raw = {"shafts": {"shafts": {"nl": {"out_rpm": {"kind": "holding", "address": 600}}},
                      "pairs": {"lnv": {"ref": 12.5, "tol_angle": 8, "tol_rpm": 1.5, "out_angle": {"kind": "holding", "address": 610}}}}}
    cfg = me.normalize_config(raw)
    assert cfg["shafts"]["shafts"]["nl"]["out_rpm"]["address"] == 600
    assert cfg["shafts"]["pairs"]["lnv"]["ref"] == 12.5 and cfg["shafts"]["pairs"]["lnv"]["out_angle"]["address"] == 610
    assert me.normalize_config(cfg) == cfg                                  # фронт шлёт обратно то, что получил
    with pytest.raises(ValueError, match="ref"):
        me.normalize_config({"shafts": {"pairs": {"nlr": {"ref": 400}}}})
    with pytest.raises(ValueError, match="вид вывода"):
        me.normalize_config({"shafts": {"pairs": {"nlr": {"out_rpm": {"kind": "coil", "address": 1}}}}})


def test_assign_shafts_left_right_by_x_and_row():
    a = ms.assign_shafts(cfg_two_rows()["cameras"])
    assert a == {"nl": ("dn", "r_left"), "np": ("dn", "r_right"), "vl": ("up", "l"), "vp": ("up", "r")}
    assert [ms.shaft_short(s) for s in ms.SHAFT_ORDER] == ["НЛВ", "НПВ", "ВЛВ", "ВПВ"]
    assert [ms.pair_short(p) for p in ms.PAIR_ORDER] == ["НЛВ–НПВ", "ВЛВ–ВПВ", "НЛВ–ВЛВ", "НПВ–ВПВ"]


def test_assign_ignores_camera_without_row_disabled_and_second_camera_of_same_row():
    cams = [cam("a", "", [cycle("z", 500)]), cam("b", "lower", [cycle("z", 1500)]), cam("c", "lower", [cycle("z", 400), cycle("y", 1500)])]
    cams[1]["enabled"] = False
    a = ms.assign_shafts(me.normalize_config({"cameras": cams})["cameras"])
    assert a == {"nl": ("c", "z"), "np": ("c", "y")}              # a без ряда, b выключена → берётся c
    one = ms.assign_shafts(me.normalize_config({"cameras": [cam("p", "upper", [cycle("z", 1500)])]})["cameras"])
    assert one == {"vp": ("p", "z")}                              # одна зона правее середины кадра → правый вал


def test_compute_synced_rows_give_zero_diffs():
    # нижний ряд: 100 + 260 = 360 (зеркально); верхний такой же; левые одинаковы (100−100), правые одинаковы
    snap = ms.compute(cfg_two_rows(), states())
    p = snap["pairs"]
    assert p["nlr"]["angle_diff"] == 0.0 and p["vlr"]["angle_diff"] == 0.0
    assert p["lnv"]["angle_diff"] == 0.0 and p["rnv"]["angle_diff"] == 0.0
    assert snap["shafts"]["nl"]["rpm"] == 5.0 and snap["shafts"]["np"]["rpm"] == -5.0
    # скорость по модулю: левый и правый крутятся навстречу, это не рассинхрон
    assert p["nlr"]["rpm_diff"] == 0.0 and p["vlr"]["rpm_diff"] == 0.0
    assert p["lnv"]["rpm_diff"] == 1.0 and p["rnv"]["rpm_diff"] == 1.0         # 5 − 4 и |−5| − |−4|


def test_compute_angle_diff_signed_and_wraps():
    snap = ms.compute(cfg_two_rows(), states(dn=(110.0, 260.0, 5, -5)))          # левый нижний ушёл на +10°
    p = snap["pairs"]
    assert p["nlr"]["angle_diff"] == 10.0 and p["lnv"]["angle_diff"] == 10.0     # левый нижний отстал/убежал от обоих соседей
    assert p["vlr"]["angle_diff"] == 0.0 and p["rnv"]["angle_diff"] == 0.0
    wrap = ms.compute(cfg_two_rows(), states(dn=(5.0, 260.0, 5, -5), up=(355.0, 260.0, 4, -4)))
    assert wrap["pairs"]["lnv"]["angle_diff"] == 10.0                            # 5 − 355 = −350 ≡ +10, а не −350


def test_compute_ref_shifts_zero_point():
    cfg = cfg_two_rows({"shafts": {"pairs": {"lnv": {"ref": 20}}}})
    assert ms.compute(cfg, states(dn=(120.0, 260.0, 5, -5)))["pairs"]["lnv"]["angle_diff"] == 0.0


def test_compute_no_data_is_none_not_zero():
    dead = ms.compute(cfg_two_rows(), states(alive=False))
    assert all(not p["valid"] and p["angle_diff"] is None and p["rpm_diff"] is None for p in dead["pairs"].values())
    lost = states()
    lost["dn"]["result"]["zones"][0]["valid"] = False                            # у НЛВ палку не видно
    snap = ms.compute(cfg_two_rows(), lost)
    assert snap["shafts"]["nl"]["valid"] is False and snap["shafts"]["nl"]["rpm"] is None
    assert not snap["pairs"]["nlr"]["valid"] and not snap["pairs"]["lnv"]["valid"]
    assert snap["pairs"]["vlr"]["valid"] and snap["pairs"]["rnv"]["valid"]       # остальные пары без НЛВ живы
    none = ms.compute(cfg_two_rows(), {})                                        # состояний камер нет вообще
    assert not any(s["valid"] for s in none["shafts"].values())


class FakeClient:
    connected = True

    def __init__(self, host, port):
        self.log = []

    def connect(self):
        return True

    def close(self):
        pass

    def write_register(self, address, value, device_id=1):
        self.log.append(("holding", address, value))

        class R:
            def isError(self):
                return False
        return R()


def runner(cfg, st):
    clients = []

    def factory(host, port):
        c = FakeClient(host, port)
        clients.append(c)
        return c

    class W:
        def __init__(self, s):
            self.s = s

        def state(self):
            return self.s

    class M:
        links = mm.LinkRegistry(factory)
        workers = {k: W(v) for k, v in st.items()}

    M.cfg = cfg
    return ms.ShaftRunner(M), clients, M


def test_runner_writes_diffs_with_scales_and_negative_as_uint16_only_when_address_set():
    cfg = cfg_two_rows({"shafts": {"shafts": {"nl": {"out_rpm": {"kind": "holding", "address": 600}}},
                                   "pairs": {"nlr": {"out_angle": {"kind": "holding", "address": 610},
                                                     "out_rpm": {"kind": "holding", "address": 611}}}}})
    r, clients, M = runner(cfg, states(dn=(95.0, 260.0, 5.0, -4.5)))
    r.tick(100.0)
    log = clients[0].log
    assert ("holding", 600, 500) in log                         # скорость НЛВ ×100
    assert ("holding", 610, (-50) & 0xFFFF) in log              # 95 + 260 − 360 = −5° → ×10 = −50 в uint16
    assert ("holding", 611, 50) in log                          # |5| − |−4.5| = 0.5 об/мин ×100
    assert len(log) == 3                                        # адресов нет у остальных — в ПЛК их не пишем
    n = len(log)
    r.tick(100.05)
    assert len(clients[0].log) == n                             # не изменилось и период не вышел
    M.workers["dn"].s = states(dn=(95.0, 260.0, 5.0, -4.0))["dn"]
    r.tick(100.1)
    assert ("holding", 611, 100) in clients[0].log


def test_runner_does_not_write_when_plc_disabled_and_dead_camera_writes_zeros():
    cfg = cfg_two_rows({"shafts": {"pairs": {"nlr": {"out_angle": {"kind": "holding", "address": 610}}}}})
    cfg["modbus"]["enable"] = False
    r, clients, _ = runner(cfg, states())
    r.tick(100.0)
    assert clients == []
    cfg["modbus"]["enable"] = True
    r2, clients2, _ = runner(cfg, states(alive=False))
    r2.tick(100.0)
    assert clients2[0].log == [("holding", 610, 0)]             # камера мертва — ноль, как у сценариев


def test_runner_alarm_has_debounce_and_tol_zero_means_off():
    cfg = cfg_two_rows({"shafts": {"pairs": {"nlr": {"tol_angle": 5, "tol_rpm": 0}}}})
    r, _, M = runner(cfg, states(dn=(120.0, 260.0, 5, -5)))     # Δ = +20° > допуска 5°
    r.tick(1.0)
    assert r.snapshot()["pairs"]["nlr"]["alarm_angle"] == 0     # дребезг: сразу не включаем
    r.tick(2.0)
    r.tick(3.0)
    snap = r.snapshot()["pairs"]["nlr"]
    assert snap["alarm_angle"] == 1 and snap["alarm_rpm"] == 0  # tol_rpm = 0 — тревога по скорости выключена
    M.workers["dn"].s = states(dn=(100.0, 260.0, 5, -5))["dn"]
    for t in range(4, 12):
        r.tick(float(t))
    assert r.snapshot()["pairs"]["nlr"]["alarm_angle"] == 0


def test_history_window_and_downsample():
    r, _, M = runner(cfg_two_rows(), states())
    for i in range(100):
        r.tick(1000.0 + i)
    h = r.history("nlr", window_s=0, points=10)
    assert len(h["t"]) == 10 and len(h["angle"]) == 10 and len(h["rpm"]) == 10
    assert r.history("nope")["t"] == []


def test_http_calibrate_sets_ref_and_state_exposes_shafts(tmp_path):
    import importlib.util
    import json
    import socket
    import threading
    import time
    import urllib.error
    import urllib.request

    import uvicorn

    spec = importlib.util.spec_from_file_location("motion_server_shafts", MS / "server.py")
    srv = importlib.util.module_from_spec(spec)
    sys.modules["motion_server_shafts"] = srv
    spec.loader.exec_module(srv)

    class ClosedCap:                       # камер в этом тесте нет: поток «не открылся», воркер просто ждёт переподключения
        def isOpened(self):
            return False

        def release(self):
            pass

        def read(self):
            return False, None

        def set(self, *a):
            return False

        def get(self, *a):
            return 0.0

    def never_open(*a, **k):
        return ClosedCap()

    app = srv.create_app(tmp_path / "data", capture_factory=never_open, client_factory=lambda h, p: FakeClient(h, p))
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()

    def http(path, body=None, method=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data, method=method or ("POST" if data else "GET"),
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    try:
        t0 = time.time()
        while not server.started and time.time() - t0 < 10:
            time.sleep(0.05)
        assert server.started
        mgr = app.state.mgr
        assert http("/config", cfg_two_rows(), "PUT")[0] == 200
        code, body = http("/shafts/calibrate", {"pair": "lnv"})
        assert code == 409                                              # данных ещё нет — эталон не снимаем
        assert http("/shafts/calibrate", {"pair": "zzz"})[0] == 404

        class W:                                       # заглушка воркера: готовое state(), остальное как у настоящего
            def __init__(self, s):
                self.s = s

            def state(self):
                return self.s

            def is_alive(self):
                return False

            def stop(self):
                pass
        for w in list(mgr.workers.values()):
            w.stop()
        mgr.workers = {k: W(v) for k, v in states(dn=(130.0, 230.0, 5, -5)).items()}   # нижний левый впереди верхнего на 30°
        mgr.shafts.tick()
        st = http("/state")[1]
        assert st["shafts"]["pairs"]["lnv"]["angle_diff"] == 30.0 and st["shafts"]["shafts"]["nl"]["short"] == "НЛВ"
        code, body = http("/shafts/calibrate", {"pair": "lnv"})
        assert code == 200 and body == {"pair": "lnv", "ref": 30.0}
        assert mgr.cfg["shafts"]["pairs"]["lnv"]["ref"] == 30.0         # лежит в конфиге
        assert json.loads((tmp_path / "data" / "motion_config.json").read_text(encoding="utf-8"))["shafts"]["pairs"]["lnv"]["ref"] == 30.0
        mgr.workers = {k: W(v) for k, v in states(dn=(130.0, 230.0, 5, -5)).items()}    # apply пересоздал воркеры — подменяем заново
        mgr.shafts.tick()
        assert http("/state")[1]["shafts"]["pairs"]["lnv"]["angle_diff"] == 0.0        # после эталона — ноль
        h = http("/shafts/history?pair=lnv&window=0&points=50")[1]
        assert h["pair"] == "lnv" and len(h["t"]) >= 2 and h["angle"][-1] == 0.0
        assert http("/shafts/history?pair=bad")[0] == 404
    finally:
        server.should_exit = True
        th.join(5)
