"""motion_service: ядро (метрики, гистерезис, группы, выводы), канал Modbus, рабочий поток камеры и HTTP-сервер.

Без камеры и ПЛК: кадры синтетические (бегущая текстура слева, неподвижная справа), Modbus — поддельный клиент.
Сервис поднимается настоящим uvicorn на свободном порту, источник — mp4, записанный тут же.
"""
import importlib.util
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

MS = Path(__file__).resolve().parent.parent / "motion_service"
sys.path.insert(0, str(MS))

import motion_engine as me  # noqa: E402
import motion_modbus as mm  # noqa: E402
from motion_camera import CameraWorker  # noqa: E402

W, H = 320, 240
LEFT = [[10, 40], [150, 40], [150, 200], [10, 200]]
RIGHT = [[170, 40], [310, 40], [310, 200], [170, 200]]


def _texture(rng, w=W * 2):
    t = rng.integers(0, 256, (H, w)).astype(np.float32)
    t = cv2.GaussianBlur(t, (0, 0), 2.0)
    t = (t - t.min()) / (t.max() - t.min()) * 255
    return t.astype(np.uint8)


def make_frames(n, move_left=True, move_right=False, flicker=0.0, seed=1):
    """Серые кадры: левая половина — бегущая текстура (3 px/кадр), правая — неподвижная + шум матрицы."""
    rng = np.random.default_rng(seed)
    tl, tr = _texture(rng), _texture(rng)
    out = []
    for i in range(n):
        left = np.roll(tl, 3 * i if move_left else 0, axis=1)[:, :W // 2]
        right = np.roll(tr, 3 * i if move_right else 0, axis=1)[:, :W // 2]
        f = np.hstack([left, right]).astype(np.float32)
        f += rng.normal(0, 1.0, f.shape)
        if flicker:
            f += flicker * (1 if i % 2 else -1)
        out.append(np.clip(f, 0, 255).astype(np.uint8))
    return out


def cam_cfg(**kw):
    base = {"id": "c1", "name": "Лента", "url": "x", "proc_fps": 3,
            "zones": [{"id": "z1", "name": "Лента 1", "points": LEFT, "metric": kw.pop("metric", "flow"),
                       "out": {"kind": "holding", "address": 10}},
                      {"id": "z2", "name": "Лента 2", "points": RIGHT, "metric": "flow",
                       "out": {"kind": "coil", "address": 5}}]}
    base.update(kw)
    return me.normalize_camera(base)


def run_engine(cfg, frames):
    eng = me.CameraEngine(cfg)
    st = None
    for i, f in enumerate(frames):
        s = eng.process(f, 1000.0 + i / 3.0)
        st = s or st
    return eng, st


# ───────────────────────────── конфиг ─────────────────────────────
def test_normalize_defaults_and_metric_thresholds():
    c = cam_cfg()
    z1, z2 = c["zones"]
    assert (z1["on"], z1["off"]) == (0.9, 0.6) and c["proc_fps"] == 3 and c["transport"] == "tcp"
    zd = me.normalize_camera({"id": "d", "zones": [{"id": "a", "points": LEFT, "metric": "diff"}]})["zones"][0]
    assert (zd["on"], zd["off"]) == (4.0, 2.0), "пороги diff — в градациях серого, не как у потока"
    assert me.normalize_camera({"id": "p", "zones": [{"id": "a", "points": LEFT, "type": "presence"}]})["zones"][0]["invert"] is True


@pytest.mark.parametrize("bad,part", [
    ({"id": "бук вы", "zones": []}, "id камеры"),
    ({"id": "c", "zones": [{"id": "a", "points": [[0, 0], [1, 1]]}]}, "меньше трёх точек"),
    ({"id": "c", "zones": [{"id": "a", "points": LEFT, "on": 1, "off": 2}]}, "больше порога включения"),
    ({"id": "c", "zones": [{"id": "a", "points": LEFT}, {"id": "a", "points": LEFT}]}, "повторяются"),
    ({"id": "c", "zones": [{"id": "a", "points": LEFT}], "groups": [{"id": "g", "type": "sum", "zones": ["zz"]}]}, "нет таких зон"),
    ({"id": "c", "zones": [{"id": "a", "points": LEFT, "out": {"kind": "float", "address": 1}}]}, "вид вывода"),
    ({"id": "c", "transport": "ftp"}, "transport"),
])
def test_normalize_rejects_bad_config(bad, part):
    with pytest.raises(ValueError) as e:
        me.normalize_camera(bad)
    assert part in str(e.value)


def test_normalize_config_global():
    cfg = me.normalize_config({"modbus": {"enable": True, "host": "10.0.0.5"}, "cameras": [{"id": "a"}, {"id": "b"}]})
    assert cfg["modbus"]["host"] == "10.0.0.5" and cfg["modbus"]["port"] == 502 and len(cfg["cameras"]) == 2
    with pytest.raises(ValueError):
        me.normalize_config({"cameras": [{"id": "a"}, {"id": "a"}]})


# ───────────────────────────── метрики и гистерезис ─────────────────────────────
def test_diff_metric_separates_moving_from_static():
    fr = make_frames(4)
    zr = me.ZoneRT(cam_cfg()["zones"][0], None, (W, H))
    zs = me.ZoneRT(cam_cfg()["zones"][1], None, (W, H))
    moving = me.diff_metric(fr[1][zr.bbox[1]:zr.bbox[1] + zr.bbox[3], zr.bbox[0]:zr.bbox[0] + zr.bbox[2]],
                            fr[2][zr.bbox[1]:zr.bbox[1] + zr.bbox[3], zr.bbox[0]:zr.bbox[0] + zr.bbox[2]], zr.mask, 5, True)
    static = me.diff_metric(fr[1][zs.bbox[1]:zs.bbox[1] + zs.bbox[3], zs.bbox[0]:zs.bbox[0] + zs.bbox[2]],
                            fr[2][zs.bbox[1]:zs.bbox[1] + zs.bbox[3], zs.bbox[0]:zs.bbox[0] + zs.bbox[2]], zs.mask, 5, True)
    assert moving > 4.0 > 2.0 > static


def test_flicker_compensation_ignores_global_brightness_swing():
    fr = make_frames(6, move_left=False, flicker=12.0)       # всё неподвижно, лампа мигает ±12
    zs = me.ZoneRT(cam_cfg()["zones"][0], None, (W, H))
    x, y, w, h = zs.bbox
    a, b = fr[2][y:y + h, x:x + w], fr[3][y:y + h, x:x + w]
    raw = me.diff_metric(a, b, zs.mask, 5, False)
    comp = me.diff_metric(a, b, zs.mask, 5, True)
    assert raw > 10 and comp < 2.0, (raw, comp)


def test_hysteresis_debounce_and_invert():
    h = me.Hysteresis(0.9, 0.6, False, 3, 4)
    seq = [1.5, 1.5, 0, 1.5, 1.5, 1.5]                 # два «да», провал (сбрасывает), потом три подряд
    assert [h.update(v) for v in seq] == [0, 0, 0, 0, 0, 1]
    assert [h.update(0.3) for _ in range(4)] == [1, 1, 1, 0]
    inv = me.Hysteresis(0.9, 0.6, True, 2, 2)           # presence: активна при НИЗКОМ значении
    assert [inv.update(v) for v in (0.2, 0.2, 1.5, 1.5)] == [0, 1, 1, 0]


def test_work_timer_and_monotonic():
    t = me.WorkTimer(10, "all")
    assert t.update(100.0, True) == 1 and t.update(100.5, True) == 1
    assert t.update(101.0, True) == 2 and t.update(101.4, False) == 1, "красный кадр делает секунду простоем (agg=all)"
    fixed, level = me.monotonic_level({"a": 0, "b": 1, "c": 0}, ["a", "b", "c"])
    assert level == 2 and fixed == {"a": 1, "b": 1, "c": 0}


# ───────────────────────────── движок ─────────────────────────────
@pytest.mark.parametrize("metric", ["flow", "diff"])
def test_engine_moving_zone_on_static_zone_off(metric):
    cfg = cam_cfg(metric=metric)
    eng, st = run_engine(cfg, make_frames(30))
    z = {x["id"]: x for x in st["zones"]}
    assert z["z1"]["bit"] == 1 and z["z2"]["bit"] == 0, z
    assert z["z1"]["flow"] > z["z2"]["flow"] and z["z1"]["diff"] > z["z2"]["diff"]
    assert st["alive"] is True


def test_engine_both_belts_moving_and_all_stopped():
    cfg = me.normalize_camera({"id": "c", "zones": [
        {"id": "z1", "points": LEFT, "metric": "diff", "out": {"kind": "holding", "address": 0}},
        {"id": "z2", "points": RIGHT, "metric": "diff", "out": {"kind": "holding", "address": 1}}]})
    _, st = run_engine(cfg, make_frames(30, move_left=True, move_right=True))
    assert [z["bit"] for z in st["zones"]] == [1, 1]
    _, st = run_engine(cfg, make_frames(30, move_left=False, move_right=False))
    assert [z["bit"] for z in st["zones"]] == [0, 0]


def test_engine_scales_zones_to_other_resolution():
    cfg = cam_cfg()
    cfg["frame_size"] = [W // 2, H // 2]               # зоны размечены на кадре вдвое меньше, чем отдаёт камера
    eng = me.CameraEngine(cfg)
    eng.process(make_frames(1)[0], 0.0)
    assert eng.zones[0].bbox[0] == 20 and eng.zones[0].bbox[2] == 281, "x 10→20, правый край 150→300; boundingRect включает границу"


def test_engine_groups_and_writes():
    cfg = me.normalize_camera({"id": "c", "status_out": {"kind": "holding", "address": 99}, "zones": [
        {"id": "w1", "points": LEFT, "metric": "diff", "out": {"kind": "holding", "address": 0}},
        {"id": "w2", "points": RIGHT, "metric": "diff", "out": {"kind": "coil", "address": 3}}],
        "groups": [{"id": "lvl", "type": "monotonic", "zones": ["w1", "w2"], "order": ["w2", "w1"], "out": {"kind": "holding", "address": 20}},
                   {"id": "cnt", "type": "sum", "zones": ["w1", "w2"], "out": {"kind": "bits", "address": 21}},
                   {"id": "bt", "type": "sum", "zones": ["w1", "w2"], "out": {"kind": "coils", "address": 40}},
                   {"id": "wk", "type": "worktimer", "zones": ["w1", "w2"], "min_active": 1, "window_s": 10, "scale": 100,
                    "out": {"kind": "holding", "address": 22}}]})
    eng, st = run_engine(cfg, make_frames(30))        # w1 движется, w2 стоит
    w = {(k, a): v for k, a, v in eng.compute_writes(st)}
    assert w[("holding", 0)] == 1 and w[("coil", 3)] == 0
    assert w[("holding", 20)] == 2, "monotonic: сработало верхнее (w1, второе снизу) → уровень 2"
    assert w[("holding", 21)] == 0b01, "bits: w1 → бит 0"
    assert (w[("coil", 40)], w[("coil", 41)]) == (1, 0)
    assert 0 < w[("holding", 22)] <= 100
    assert w[("holding", 99)] == 1, "status: камера жива"
    dead = eng.dead_state(2000.0, "тест")
    zero = {(k, a): v for k, a, v in eng.compute_writes(dead)}
    assert zero[("holding", 0)] == zero[("holding", 20)] == zero[("holding", 21)] == zero[("holding", 22)] == 0
    assert zero[("holding", 99)] == 0, "камера мертва → status 0 и нули"


# ───────────────────────────── Modbus ─────────────────────────────
class FakeRes:
    def __init__(self, err=False, regs=(7,)):
        self._e, self.registers = err, list(regs)

    def isError(self):
        return self._e


class FakeClientNew:                                   # pymodbus ≥ 3.10: device_id=
    connected = True

    def __init__(self, host, port):
        self.log, self.fail = [], False

    def connect(self):
        return True

    def close(self):
        pass

    def write_register(self, address, value, device_id=1):
        self.log.append(("holding", address, value, device_id))
        return FakeRes(self.fail)

    def write_coil(self, address, value, device_id=1):
        self.log.append(("coil", address, value, device_id))
        return FakeRes(self.fail)

    def read_holding_registers(self, address, count=1, device_id=1):
        return FakeRes(self.fail, [42])


class FakeClientOld(FakeClientNew):                    # pymodbus 3.6–3.9: slave=
    def write_register(self, address, value, slave=1):
        return FakeClientNew.write_register(self, address, value, slave)

    def write_coil(self, address, value, slave=1):
        return FakeClientNew.write_coil(self, address, value, slave)

    def read_holding_registers(self, address, count=1, slave=1):
        return FakeClientNew.read_holding_registers(self, address, count, slave)


@pytest.mark.parametrize("cls", [FakeClientNew, FakeClientOld])
def test_modbus_link_both_pymodbus_generations(cls):
    link = mm.ModbusLink("h", 502, 7, client_factory=cls)
    assert link.write([("holding", 1, 2), ("coil", 3, 1), ("holding", 4, 70000)]) is True
    assert link._client.log == [("holding", 1, 2, 7), ("coil", 3, True, 7), ("holding", 4, 70000 & 0xFFFF, 7)]
    assert link.read_holding(0) == (True, "42")
    link._client.fail = True
    assert link.write([("holding", 1, 2)]) is False and link.ok is False and link.last_error


def test_modbus_link_dead_plc_does_not_raise():
    link = mm.ModbusLink("127.0.0.1", 1, 1)               # порт 1 — никто не слушает
    assert link.write([("holding", 0, 1)]) is False
    ok, info = link.read_holding(0)
    assert ok is False and info


# ───────────────────────────── рабочий поток камеры ─────────────────────────────
class FakeCap:
    """Источник из списка кадров; frozen=True — одна и та же картинка (камера зависла)."""

    def __init__(self, frames, frozen=False, delay=0.02):
        self.frames, self.i, self.frozen, self.delay, self._f = frames, 0, frozen, delay, None

    def isOpened(self):
        return True

    def grab(self):
        time.sleep(self.delay)
        self._f = self.frames[0 if self.frozen else self.i % len(self.frames)]
        self.i += 1
        return True

    def retrieve(self):
        return True, cv2.cvtColor(self._f, cv2.COLOR_GRAY2BGR)

    def release(self):
        pass

    def set(self, *_):
        return True

    def get(self, *_):
        return 25.0


def _wait(cond, timeout=8.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _worker(tmp_path, frames, frozen=False, **cam):
    raw = {"id": "w", "url": "fake", "proc_fps": 20, "freeze_timeout_s": 1, "zones": [
        {"id": "z1", "points": LEFT, "metric": "diff", "smooth": 2, "frames_to_on": 2, "frames_to_off": 2,
         "out": {"kind": "holding", "address": 0}},
        {"id": "z2", "points": RIGHT, "metric": "diff", "smooth": 2, "frames_to_on": 2, "frames_to_off": 2,
         "out": {"kind": "holding", "address": 1}}], "status_out": {"kind": "holding", "address": 9}}
    raw.update(cam)
    cfg = me.normalize_camera(raw)
    mb = me.normalize_modbus({"enable": True, "host": "plc", "write_period_s": 0.2})
    clients = []

    def factory(host, port):
        c = FakeClientNew(host, port)
        clients.append(c)
        return c

    w = CameraWorker(cfg, mb, mm.LinkRegistry(factory), tmp_path, lambda url, tr: FakeCap(frames, frozen))
    w.start()
    return w, clients


def test_worker_detects_motion_writes_plc_and_reports_snapshot(tmp_path):
    w, clients = _worker(tmp_path, make_frames(40))
    try:
        assert _wait(lambda: (w.state()["result"] or {}).get("zones") and w.state()["result"]["zones"][0]["bit"] == 1)
        st = w.state()["result"]
        assert [z["bit"] for z in st["zones"]] == [1, 0] and st["alive"]
        assert _wait(lambda: clients and ("holding", 0, 1, 1) in clients[0].log)
        assert ("holding", 9, 1, 1) in clients[0].log, "status=1, пока камера жива"
        jpg, (fw, fh) = w.snapshot_jpeg(160)
        assert jpg[:2] == b"\xff\xd8" and (fw, fh) == (W, H)
        assert _wait(lambda: len(w.history(50)["t"]) > 3)
        h = w.history(50)
        assert set(h["zones"]) == {"z1", "z2"}
        name = w.start_record()
        time.sleep(0.4)
        w.stop_record()
        txt = (tmp_path / "records" / name).read_text(encoding="utf-8-sig")
        assert txt.splitlines()[0].startswith("time;z1_flow;z1_diff;z1_bit") and len(txt.splitlines()) > 2
    finally:
        w.stop()


def test_worker_frozen_camera_gives_zeros_and_status_zero(tmp_path):
    w, clients = _worker(tmp_path, make_frames(40), frozen=True)
    try:
        assert _wait(lambda: (w.state()["result"] or {}).get("alive") is False, 10)
        st = w.state()["result"]
        assert st["reason"] == "картинка застыла" and all(z["bit"] == 0 for z in st["zones"])
        assert _wait(lambda: clients and ("holding", 9, 0, 1) in clients[0].log), "в ПЛК ушёл status=0"
    finally:
        w.stop()


# ───────────────────────────── HTTP-сервер целиком ─────────────────────────────
def _load_server():
    spec = importlib.util.spec_from_file_location("motion_server", MS / "server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["motion_server"] = mod
    spec.loader.exec_module(mod)
    return mod


def _http(port, path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data,
                                 method=method or ("POST" if data else "GET"), headers={"Content-Type": "application/json"} if data else {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=5) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if r.headers.get_content_type() == "application/json" else raw), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode()), {}


def test_server_end_to_end_with_video_file(tmp_path):
    import uvicorn
    video = tmp_path / "belt.mp4"
    vw = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 10, (W, H))
    for f in make_frames(60):
        vw.write(cv2.cvtColor(f, cv2.COLOR_GRAY2BGR))
    vw.release()
    assert video.stat().st_size > 1000

    writes = []

    class Cl(FakeClientNew):
        def write_register(self, address, value, device_id=1):
            writes.append((address, value))
            return FakeRes()

    srv = _load_server()
    app = srv.create_app(tmp_path / "data", client_factory=lambda h, p: Cl(h, p))
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    try:
        assert _wait(lambda: server.started, 10)
        code, h, _ = _http(port, "/health")
        assert code == 200 and h["service"] == "motion" and h["cameras"] == 0

        cfg = {"modbus": {"enable": True, "host": "plc", "write_period_s": 0.2}, "cameras": [{
            "id": "belt", "name": "Ленты", "url": str(video), "proc_fps": 5, "freeze_timeout_s": 30,
            "zones": [{"id": "l1", "name": "Лента 1", "points": LEFT, "metric": "diff", "smooth": 2, "frames_to_on": 2,
                       "out": {"kind": "holding", "address": 0}},
                      {"id": "l2", "name": "Лента 2", "points": RIGHT, "metric": "diff", "smooth": 2, "frames_to_on": 2,
                       "out": {"kind": "holding", "address": 1}}]}]}
        code, new, _ = _http(port, "/config", cfg, "PUT")
        assert code == 200 and new["cameras"][0]["zones"][0]["on"] == 4.0
        assert (tmp_path / "data" / "motion_config.json").is_file()

        def bits():
            _, st, _ = _http(port, "/state")
            r = st["cameras"][0]["result"] if st["cameras"] else None
            return [z["bit"] for z in r["zones"]] if r else None
        assert _wait(lambda: bits() == [1, 0], 15), bits()
        assert _wait(lambda: (0, 1) in writes and (1, 0) in writes, 5), writes[-6:]

        code, jpg, hdr = _http(port, "/cameras/belt/snapshot?w=200")
        assert code == 200 and jpg[:2] == b"\xff\xd8" and {k.lower(): v for k, v in hdr.items()}["x-frame-w"] == str(W)
        assert _http(port, "/cameras/nope/state")[0] == 404
        assert _wait(lambda: len(_http(port, "/cameras/belt/history?n=20")[1]["t"]) > 2, 10)
        code, hist, _ = _http(port, "/cameras/belt/history?n=20")
        assert code == 200 and set(hist["zones"]) == {"l1", "l2"}
        code, rec, _ = _http(port, "/cameras/belt/record", {"on": True})
        assert code == 200 and rec["recording"].endswith(".csv")
        assert _http(port, "/cameras/belt/record", {"on": False})[1]["recording"] is None

        bad = json.loads(json.dumps(cfg))
        bad["cameras"][0]["zones"][0]["on"], bad["cameras"][0]["zones"][0]["off"] = 1, 5
        code, err, _ = _http(port, "/config", bad, "PUT")
        assert code == 400 and "порог выключения" in err["detail"]
        assert _http(port, "/config")[1]["cameras"][0]["zones"][0]["on"] == 4.0, "плохой конфиг не применился"

        code, ping, _ = _http(port, "/modbus/ping", {"host": "plc", "address": 0})
        assert code == 200 and ping["ok"] is True and ping["info"] == "42"

        # конфиг пережил «перезапуск»: второй менеджер на том же каталоге поднимает ту же камеру
        mgr2 = srv.Manager(tmp_path / "data", capture_factory=lambda u, t: FakeCap(make_frames(5)))
        mgr2.load()
        assert [c["id"] for c in mgr2.cfg["cameras"]] == ["belt"]
    finally:
        server.should_exit = True
        th.join(8)
