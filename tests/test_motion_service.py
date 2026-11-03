"""motion_service: ядро (метрики, гистерезис, группы, выводы), канал Modbus, рабочий поток камеры и HTTP-сервер.

Без камеры и ПЛК: кадры синтетические (бегущая текстура слева, неподвижная справа), Modbus — поддельный клиент.
Сервис поднимается настоящим uvicorn на свободном порту, источник — mp4, записанный тут же.
"""
import importlib.util
import io
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import json
import zipfile
from pathlib import Path

import cv2
import numpy as np
import pytest

MS = Path(__file__).resolve().parent.parent / "motion_service"
sys.path.insert(0, str(MS))

import motion_engine as me  # noqa: E402
import motion_modbus as mm  # noqa: E402
import motion_camera as mcam  # noqa: E402
from motion_camera import CameraWorker, decoder_kind, garbage_ratio, hw_params, stream_url  # noqa: E402


@pytest.fixture(autouse=True)
def _no_open_gap(monkeypatch):
    """Очередь на открытие потоков в тестах не ждём (проверяется отдельным тестом)."""
    monkeypatch.setattr(mcam, "OPEN_GAP_S", 0.0)


def test_open_turn_spaces_openings_without_holding_the_slow_one():
    mcam._open_next = 0.0
    t = [100.0]
    slept = []

    def sleep(s):
        slept.append(round(s, 3))
        t[0] += s
    waits = [mcam.wait_open_turn(2.0, clock=lambda: t[0], sleep=sleep) for _ in range(3)]
    assert waits == [0.0, 2.0, 2.0] and slept == [2.0, 2.0]
    t[0] += 30
    assert mcam.wait_open_turn(2.0, clock=lambda: t[0], sleep=sleep) == 0.0

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


class DropCap(FakeCap):
    """Поток, который на drop_at-м кадре обрывается (grab -> False), как RTSP-камера при сбое."""

    def __init__(self, frames, drop_at):
        super().__init__(frames)
        self.drop_at = drop_at

    def grab(self):
        if self.i >= self.drop_at:
            return False
        return super().grab()


class ClosedCap(FakeCap):
    def isOpened(self):
        return False


def _worker(tmp_path, frames, frozen=False, cap_factory=None, **cam):
    raw = {"id": "w", "url": "fake", "hw_accel": "off", "proc_fps": 20, "freeze_timeout_s": 1, "zones": [
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

    w = CameraWorker(cfg, mb, mm.LinkRegistry(factory), tmp_path, cap_factory or (lambda url, tr: FakeCap(frames, frozen)))
    w.start()
    return w, clients


def _drop_and_read_bits(tmp_path, caps):
    """Гоняет воркер по списку источников (по одному на открытие), пишет CSV -> строки z1_bit с живыми кадрами ПОСЛЕ обрыва."""
    frames = make_frames(40)
    opened = []

    def factory(url, tr):
        opened.append(1)
        return caps[min(len(opened), len(caps)) - 1](frames)
    w, _ = _worker(tmp_path, frames, cap_factory=factory, url="rtsp://h/cam", reconnect_delay_s=0.5, freeze_timeout_s=1.2)
    try:
        assert _wait(lambda: (w.state()["result"] or {}).get("zones") and w.state()["result"]["zones"][0]["bit"] == 1)
        name = w.start_record()
        assert _wait(lambda: len(opened) >= len(caps), 12)
        assert _wait(lambda: (w.state()["result"] or {}).get("alive") and w.state()["result"]["zones"][0]["bit"] == 1, 12)
        time.sleep(0.6)
        w.stop_record()
    finally:
        w.stop()
    rows = [r.split(";") for r in (tmp_path / "records" / name).read_text(encoding="utf-8-sig").splitlines()[1:]]
    ev = [i for i, r in enumerate(rows) if r[-1] == "событие: источник открыт"]
    assert ev, "переподключение должно попасть в запись"
    return [r[3] for r in rows[ev[0]:] if r[-1] == "ok"]


def test_short_stream_drop_keeps_zone_bit(tmp_path):
    """Обрыв короче freeze_timeout_s не даёт ложного «стоит»: после переподключения бит зоны не падает в 0."""
    bits = _drop_and_read_bits(tmp_path, [lambda f: DropCap(f, 30), lambda f: FakeCap(f)])
    assert bits and set(bits) == {"1"}, bits


def test_long_stream_drop_resets_zone_bit(tmp_path):
    """Обрыв дольше freeze_timeout_s — состояния с нуля: после ожидания бит набирается заново (0 -> 1)."""
    caps = [lambda f: DropCap(f, 30)] + [lambda f: ClosedCap(f)] * 3 + [lambda f: FakeCap(f)]
    bits = _drop_and_read_bits(tmp_path, caps)
    assert "0" in bits and bits[-1] == "1", bits


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


def test_record_has_status_column_and_connection_events(tmp_path):
    w, _ = _worker(tmp_path, make_frames(40), frozen=True)
    try:
        assert _wait(lambda: (w.state()["result"] or {}).get("alive") is False, 10)
        name = w.start_record()
        w._note_conn(False, "поток оборвался")                     # как при обрыве RTSP: кадров нет, но событие в файле остаётся
        time.sleep(0.5)
        w.stop_record()
        lines = (tmp_path / "records" / name).read_text(encoding="utf-8-sig").splitlines()
        assert lines[0].endswith(";alive;status")
        assert any(l.endswith(";0;картинка застыла") for l in lines[1:]), "причина по каждой строке сбоя"
        ev = [l.split(";") for l in lines if l.endswith("событие: поток оборвался")]
        assert ev and ev[0][-2] == "" and ev[0][1] == "", "у события alive и значения зон пустые"
    finally:
        w.stop()


def test_worker_stats_avg_min_max_per_zone_only_good_frames(tmp_path):
    """Среднее / мин / макс flow и diff по зоне за окно: в норме avg между min и max, движущаяся зона сильнее неподвижной;
    застывшая камера в статистику не идёт."""
    w, _ = _worker(tmp_path, make_frames(40))
    try:
        assert _wait(lambda: (w.state()["result"] or {}).get("zones") and w.state()["result"]["zones"][0]["bit"] == 1)
        assert _wait(lambda: w.stats(600)["zones"].get("z1", {}).get("n", 0) > 5)
        st = w.stats(600)
        assert st["window_s"] == 600 and 0 < st["span_s"] <= 600
        for z in ("z1", "z2"):
            for m in ("flow", "diff"):
                v = st["zones"][z][m]
                assert v["min"] <= v["avg"] <= v["max"], (z, m, v)
        assert st["zones"]["z1"]["diff"]["max"] > st["zones"]["z2"]["diff"]["max"], "в левой зоне движение, в правой нет"
        assert w.stats(1)["window_s"] == 10.0, "окно не короче одной корзины"
    finally:
        w.stop()
    w, _ = _worker(tmp_path, make_frames(40), frozen=True)
    try:
        assert _wait(lambda: (w.state()["result"] or {}).get("alive") is False, 10)
        n0 = sum(z["n"] for z in w.stats(600)["zones"].values())
        time.sleep(0.5)
        assert sum(z["n"] for z in w.stats(600)["zones"].values()) == n0, "застывшие кадры в среднее не идут"
    finally:
        w.stop()


def test_records_summary_zone_values_names_and_rotating_share(tmp_path):
    import motion_records as mr
    t0 = time.mktime((2026, 10, 30, 19, 0, 0, 0, 0, -1))
    path = tmp_path / "cam_1_20261030_190000.csv"
    stamp = lambda i: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t0 + i)) + ".000"
    rows = ["time;z1_flow;z1_diff;z1_bit;alive;status"]
    rows += ["%s;1.0;2.0;1;1;ok" % stamp(0), "%s;3.0;6.0;1;1;ok" % stamp(1), "%s;0.2;0.4;0;1;ok" % stamp(2),
             "%s;9.0;99.0;1;0;картинка застыла" % stamp(3), "%s;;;;;событие: поток оборвался" % stamp(4)]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8-sig")
    zs = mr.zone_stats(path)["z1"]
    assert zs["n"] == 3, "строки со сбоем и события не считаются"
    assert zs["flow"] == pytest.approx((1.4, 0.2, 3.0)) and zs["diff"] == pytest.approx((2.8, 0.4, 6.0))
    assert zs["bit"] == pytest.approx(2 / 3)
    txt = mr.summary_text([path], {"cam_1": {"name": "Лента", "zones": {"z1": "Шнек"}}})
    assert "Шнек (z1): diff ср 2.80 мин 0.40 макс 6.00 · flow ср 1.40 мин 0.20 макс 3.00 · крутится 67 %" in txt
    assert "z1: diff" in mr.summary_text([path]), "без имён — по id"


def test_recording_survives_service_restart_and_summary_shows_break(tmp_path, monkeypatch):
    """Включённая запись (все камеры и одной камеры) после перезапуска сервиса (обновление) продолжается сама в новом файле;
    выключили вручную — после перезапуска не пишется; перерыв между файлами виден в сводке."""
    import motion_records as mr
    srv = _load_server()
    data = tmp_path / "data"
    raw = {"id": "w", "name": "Лента", "url": "fake", "hw_accel": "off", "proc_fps": 20, "freeze_timeout_s": 1, "zones": [
        {"id": "z1", "points": LEFT, "metric": "diff", "smooth": 2, "frames_to_on": 2, "frames_to_off": 2}]}
    factory = lambda u, t: FakeCap(make_frames(200))

    def boot():
        m = srv.Manager(data, capture_factory=factory)
        m.start()
        return m

    m1 = boot()
    try:
        m1.apply({"cameras": [raw]})
        assert not m1.workers["w"].state()["recording"]
        m1.record_all(True)
        assert _wait(lambda: m1.workers["w"].state()["recording"], 5)
        first = m1.workers["w"].state()["recording"]
        time.sleep(0.8)
    finally:
        m1.shutdown()
    assert json.loads((data / "recording.json").read_text())["all"] is True, "включённая запись запомнена на диске"

    time.sleep(1.2)                                           # новый сервис — новый файл (имя с секундами)
    m2 = boot()
    try:
        assert _wait(lambda: m2.workers["w"].state()["recording"], 5), "после перезапуска запись пошла сама"
        second = m2.workers["w"].state()["recording"]
        assert second != first
        time.sleep(0.8)
        m2.record_all(False)
        assert not m2.workers["w"].state()["recording"]
        assert m2.record_camera("w", True) and m2.workers["w"].state()["recording"]      # запись одной камеры
    finally:
        m2.shutdown()

    m3 = boot()
    try:
        assert _wait(lambda: m3.workers["w"].state()["recording"], 5), "запись одной камеры тоже переживает перезапуск"
        assert m3.record_camera("w", False) is None
    finally:
        m3.shutdown()
    m4 = boot()
    try:
        time.sleep(0.6)
        assert not m4.workers["w"].state()["recording"], "выключили вручную — после перезапуска не пишется"
    finally:
        m4.shutdown()

    files = sorted((data / "records").glob("w_*.csv"))
    assert len(files) >= 3
    assert {mr.camera_of(p) for p in files} == {"w"}
    monkeypatch.setattr(mr, "GAP_MIN_S", 0.5)                 # в тесте перезапуски идут секунду-две
    txt = mr.summary_text(files)
    assert "Перерывы между файлами записи" in txt and "  w: " in txt


def _rec_row(t0, dt, alive, status, zones=1):
    ts = t0 + dt
    cells = ["", "", ""] * zones if alive is None else ["0.5", "0.4", "1"] * zones
    return ";".join([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) + ".%03d" % int(ts % 1 * 1000)] + cells + ["" if alive is None else str(alive), status])


def test_records_summary_finds_frozen_gap_and_events_and_zip(tmp_path):
    import io
    import zipfile
    import motion_records as mr
    rec = tmp_path / "records"
    rec.mkdir()
    t0 = time.mktime((2026, 10, 30, 19, 0, 0, 0, 0, -1))
    rows = [_rec_row(t0, i, 1, "ok") for i in range(10)]
    rows += [_rec_row(t0, i, 0, "картинка застыла") for i in range(10, 15)]
    rows += [_rec_row(t0, i, 1, "ok") for i in range(15, 25)]
    rows += [_rec_row(t0, 24.5, None, "событие: поток оборвался")]
    rows += [_rec_row(t0, i, 1, "ok") for i in range(45, 50)]                # 20 с без единой строки — пропуск
    path = rec / "cam_1_20261030_190000.csv"
    path.write_text("\n".join(["time;z1_flow;z1_diff;z1_bit;alive;status"] + rows + ["2026-10-30 19:00:5"]) + "\n", encoding="utf-8-sig")
    a = mr.analyze(mr.read_rows(path))
    assert a["rows"] == 30 and [(round(e - s), what) for s, e, what in a["bad"]] == [(5, "картинка застыла"), (21, "нет данных (кадров и строк не было)")]
    assert a["events"][0][1] == "поток оборвался"
    txt = mr.summary_text([path])
    assert "Сбои были: cam_1 (2)" in txt and "картинка застыла" in txt and "поток оборвался" in txt
    (rec / "cam_2_20261030_190000.csv").write_text("time;z1_flow;z1_diff;z1_bit;alive;status\n" + "\n".join(_rec_row(t0, i, 1, "ok") for i in range(30)) + "\n", encoding="utf-8-sig")
    assert "cam_2 — файл" in mr.summary_text(sorted(rec.glob("*.csv"))) and "в норме: 100.0 %" in mr.summary_text(sorted(rec.glob("*.csv")))
    z = mr.build_zip(rec, now=time.time())
    with zipfile.ZipFile(io.BytesIO(z)) as zf:
        assert sorted(zf.namelist()) == ["cam_1_20261030_190000.csv", "cam_2_20261030_190000.csv", "summary.txt"]
    assert mr.build_zip(rec, days=1, now=time.time() + 5 * 86400) is None, "старше days — в архив не идут"
    assert mr.build_zip(tmp_path / "нет_такой") is None


def test_worker_frozen_camera_gives_zeros_and_status_zero(tmp_path):
    w, clients = _worker(tmp_path, make_frames(40), frozen=True)
    try:
        assert _wait(lambda: (w.state()["result"] or {}).get("alive") is False, 10)
        st = w.state()["result"]
        assert st["reason"] == "картинка застыла" and all(z["bit"] == 0 for z in st["zones"])
        assert _wait(lambda: clients and ("holding", 9, 0, 1) in clients[0].log), "в ПЛК ушёл status=0"
    finally:
        w.stop()


def test_garbage_ratio_detects_grey_mush_not_live_frames():
    rng = np.random.default_rng(3)
    live = np.clip(rng.normal(60, 6, (H, W)), 0, 255).astype(np.uint8)      # тёмная лента с шумом матрицы
    white = np.full((H, W), 250, np.uint8)
    mush = np.full((H, W), 128, np.uint8)
    mush[:H // 5, :W // 3] = live[:H // 5, :W // 3]                         # кусок картинки уцелел
    assert garbage_ratio(live) < 0.05 and garbage_ratio(white) < 0.05
    assert garbage_ratio(mush) > 0.6


def test_worker_ignores_grey_mush_and_keeps_last_state(tmp_path):
    mush = np.full((H, W), 128, np.uint8)
    w, _ = _worker(tmp_path, make_frames(40) + [mush] * 12, freeze_timeout_s=3, corrupt_hold_s=0.1)
    try:
        assert _wait(lambda: (w.state()["result"] or {}).get("corrupt") is True, 10)
        st = w.state()["result"]
        assert st["alive"] and [z["bit"] for z in st["zones"]] == [1, 0], "результаты не сбрасываются, держим последние"
        assert _wait(lambda: not (w.state()["result"] or {}).get("corrupt") and w.state()["result"]["zones"][0]["bit"] == 1, 10)
    finally:
        w.stop()


def test_worker_long_mush_becomes_dead_state(tmp_path):
    mush = np.full((H, W), 128, np.uint8)
    w, _ = _worker(tmp_path, [mush], freeze_timeout_s=1)
    try:
        assert _wait(lambda: (w.state()["result"] or {}).get("reason") == "битый поток", 10)
    finally:
        w.stop()


# ───────────────────────────── сценарии записи в ПЛК ─────────────────────────────
def _two_cams_cfg(scenarios):
    zone = {"id": "z1", "points": LEFT, "out": None}
    return {"cameras": [{"id": "a", "url": "x", "zones": [zone]}, {"id": "b", "url": "y", "zones": [zone]}],
            "modbus": {"enable": True, "host": "plc", "write_period_s": 0.2}, "scenarios": scenarios}


def test_scenario_rules_or_and_sum_and_dead_camera_gives_zero():
    import motion_scenarios as sc
    st = {"a": {"result": {"alive": True, "zones": [{"id": "z1", "bit": 1}]}},
          "b": {"result": {"alive": False, "zones": [{"id": "z1", "bit": 1}]}}}   # камера b мертва — её бит не считается
    srcs = [{"camera": "a", "zone": "z1"}, {"camera": "b", "zone": "z1"}, {"camera": "c", "zone": "z9"}]
    bits = sc.source_bits(srcs, st)
    assert bits == [1, 0, 0]
    assert (sc.evaluate("or", bits), sc.evaluate("and", bits), sc.evaluate("sum", bits)) == (1, 0, 1)
    assert sc.evaluate("and", []) == 0 and sc.evaluate("or", []) == 0


def test_scenario_normalize_drops_missing_sources_and_validates():
    cfg = me.normalize_config(_two_cams_cfg([{"id": "s1", "rule": "or", "out": {"kind": "holding", "address": 5},
                                              "sources": [{"camera": "a", "zone": "z1"}, {"camera": "b", "zone": "zz"}, {"camera": "q", "zone": "z1"}]}]))
    assert cfg["scenarios"][0]["sources"] == [{"camera": "a", "zone": "z1"}] and cfg["scenarios"][0]["out"]["address"] == 5
    with pytest.raises(ValueError, match="правило"):
        me.normalize_config(_two_cams_cfg([{"id": "s1", "rule": "xor"}]))
    with pytest.raises(ValueError, match="повторяются"):
        me.normalize_config(_two_cams_cfg([{"id": "s1"}, {"id": "s1"}]))


def test_scenario_runner_writes_one_register_for_two_cameras(tmp_path):
    clients = []

    def factory(host, port):
        c = FakeClientNew(host, port)
        clients.append(c)
        return c

    import motion_scenarios as sc

    class W:  # камера a крутится, b нет
        def __init__(self, bit):
            self.bit = bit

        def state(self):
            return {"result": {"alive": True, "zones": [{"id": "z1", "bit": self.bit}]}}

    class M:
        links = mm.LinkRegistry(factory)
        workers = {"a": W(0), "b": W(1)}
        cfg = me.normalize_config(_two_cams_cfg([{"id": "s1", "rule": "or", "out": {"kind": "holding", "address": 7},
                                                  "sources": [{"camera": "a", "zone": "z1"}, {"camera": "b", "zone": "z1"}]}]))
    r = sc.ScenarioRunner(M)
    r.tick(100.0)
    assert ("holding", 7, 1, 1) in clients[0].log
    n = len(clients[0].log)
    r.tick(100.05)                            # значение то же и период не вышел — повторной записи нет
    assert len(clients[0].log) == n
    M.workers["b"].bit = 0
    r.tick(100.1)
    assert ("holding", 7, 0, 1) in clients[0].log and r.snapshot()[0]["value"] == 0


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

        # запись сразу по всем камерам, правка настроек не рвёт её, архив = CSV + summary.txt
        code, ra, _ = _http(port, "/record", {"on": True})
        assert code == 200 and list(ra["recording"]) == ["belt"] and ra["recording"]["belt"].endswith(".csv")
        cfg2 = json.loads(json.dumps(cfg))
        cfg2["cameras"][0]["name"] = "Лента 2"
        assert _http(port, "/config", cfg2, "PUT")[0] == 200
        assert _wait(lambda: app.state.mgr.workers["belt"].state()["recording"], 5), "камера перезапущена правкой — запись продолжилась"
        time.sleep(0.6)
        code, zbytes, zh = _http(port, "/records.zip")
        assert code == 200 and zbytes[:2] == b"PK" and "motion_records_" in {k.lower(): v for k, v in zh.items()}["content-disposition"]
        with zipfile.ZipFile(io.BytesIO(zbytes)) as z:
            names = z.namelist()
            assert "summary.txt" in names and any(n.startswith("belt_") and n.endswith(".csv") for n in names)
            assert "belt" in z.read("summary.txt").decode("utf-8-sig")
        assert _http(port, "/record", {"on": False})[1]["recording"] == {} and not app.state.mgr.rec_all
        assert _wait(lambda: not app.state.mgr.workers["belt"].state()["recording"], 3)

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


# ───────────────────────────── «CV» в мультипросмотре: включение камеры ─────────────────────────────
def test_vision_endpoint_enables_rtsp_camera_in_motion_service(monkeypatch, data_dir):
    import app as appmod
    import motion_client
    import rtsp_store

    cfg = {"version": 1, "modbus": {"enable": False}, "cameras": []}
    puts = []
    monkeypatch.setattr(rtsp_store, "load", lambda: [{"serial": "rtsp_1", "url": "rtsp://u:p@10.0.0.5:554/x", "label": "Лента"}])
    monkeypatch.setattr(motion_client, "get_config", lambda url: json.loads(json.dumps(cfg)))
    monkeypatch.setattr(motion_client, "put_config", lambda url, c: puts.append(c) or c)

    r = appmod.motion_vision({"serial": "rtsp_1", "enabled": True, "name": "Лента"})
    cam = r["cameras"][0]
    assert cam["id"] == "rtsp_1" and cam["enabled"] is True and cam["url"] == "rtsp://u:p@10.0.0.5:554/x" and cam["name"] == "Лента"

    cfg["cameras"] = [dict(cam, zones=[{"id": "z1", "name": "A", "points": LEFT}])]       # зона уже размечена
    r = appmod.motion_vision({"serial": "rtsp_1", "enabled": False})
    assert r["cameras"][0]["enabled"] is False and len(r["cameras"][0]["zones"]) == 1, "выключили — зоны остались"

    assert "error" in appmod.motion_vision({"serial": "../x", "enabled": True}), "серийник проверяется"
    monkeypatch.setattr(rtsp_store, "load", lambda: [])
    assert "error" in appmod.motion_vision({"serial": "rtsp_9", "enabled": True, "url": "file:///etc/passwd"}), "только rtsp://"
    n = len(puts)
    cfg["cameras"] = []
    assert appmod.motion_vision({"serial": "rtsp_9", "enabled": False})["cameras"] == [] and len(puts) == n, "выключать нечего — в сервис не пишем"
    monkeypatch.setattr(rtsp_store, "load", lambda: [{"serial": "rtsp_1", "url": "rtsp://u:p@10.0.0.5:554/x"}])
    monkeypatch.setattr(motion_client, "get_config", lambda url: {"error": "сервис не отвечает"})
    assert appmod.motion_vision({"serial": "rtsp_1", "enabled": True})["error"] == "сервис не отвечает"

def test_long_history_window_squeeze_and_max_keeps_bursts(tmp_path):
    """Длинная история: по точке в секунду, окно режется от «сейчас», ужимается максимумом (всплеск не теряется)."""
    w, _ = _worker(tmp_path, make_frames(2))
    w.stop()
    base = 1_000_000.0
    with w._lock:
        for i in range(0, 3000):             # 150 с по 20 кадров/с: в длинную историю идёт по точке в секунду
            ts = base + i * 0.05
            w._push_history(ts, {"z1": (0.1, 1.0 if i == 1234 else 0.2, 1 if i == 1234 else 0)}, 1)
    full = w.history(window_s=7200, points=40)
    assert len(full["t"]) <= 40 and full["t"][-1] > full["t"][0]
    assert max(full["zones"]["z1"]["diff"]) == 1.0, "всплеск в одном кадре переживает ужатие"
    assert 1 in full["zones"]["z1"]["bit"]
    last_ts = full["t"][-1]
    win = w.history(window_s=30, points=600)
    assert win["t"][-1] == last_ts, "правый край окна — «сейчас»"
    assert win["t"][-1] - win["t"][0] <= 31
    # старый режим по числу кадров не сломан
    assert len(w.history(10)["t"]) == 10


# ───────────────────── аппаратное декодирование и подпоток ─────────────────────
def test_camera_config_hw_accel_and_substream_defaults_and_validation():
    c = me.normalize_camera({"id": "c1", "url": "x"})
    assert c["hw_accel"] == "auto" and c["substream"] is False
    c = me.normalize_camera({"id": "c1", "url": "x", "hw_accel": "AUTO", "substream": True})
    assert c["hw_accel"] == "auto" and c["substream"] is True
    with pytest.raises(ValueError, match="hw_accel"):
        me.normalize_camera({"id": "c1", "url": "x", "hw_accel": "cuda"})


def test_stream_url_swaps_dahua_subtype_only_when_asked():
    u = "rtsp://admin:pw@10.0.0.5:554/cam/realmonitor?channel=1&subtype=0"
    assert stream_url(u, True).endswith("channel=1&subtype=1")
    assert stream_url(u, False) == u
    assert stream_url("rtsp://10.0.0.5/live", True) == "rtsp://10.0.0.5/live", "без subtype= адрес не трогаем"
    assert stream_url("C:/video/a.mp4?subtype=0", True) == "C:/video/a.mp4?subtype=0", "не RTSP — не трогаем"


def test_hw_params_only_for_hardware_modes():
    assert hw_params("off") == []
    for mode in ("auto", "d3d11"):
        p = hw_params(mode)
        assert p[0] == cv2.CAP_PROP_HW_ACCELERATION and p[1] > 0
        assert cv2.CAP_PROP_HW_DEVICE not in p, "устройство без явного режима OpenCV не принимает"


def test_decoder_kind_reads_capture_property():
    class C:
        def __init__(self, v):
            self.v = v

        def get(self, *_):
            return self.v
    assert decoder_kind(C(0.0)) == "cpu" and decoder_kind(C(2.0)) == "gpu" and decoder_kind(C(25.0)) == ""


class _Cap:
    def __init__(self, opened):
        self.opened = opened

    def isOpened(self):
        return self.opened

    def release(self):
        pass


def _plain_worker(tmp_path, factory, **cam):
    cfg = me.normalize_camera(dict({"id": "w", "url": "rtsp://h/cam?subtype=0", "hw_accel": "off"}, **cam))
    return CameraWorker(cfg, me.normalize_modbus({}), mm.LinkRegistry(lambda h, p: None), tmp_path, factory)


def test_worker_open_passes_substream_and_hw_mode_to_factory(tmp_path):
    calls = []

    def factory(url, tr, hw=None):
        calls.append((url, hw))
        return _Cap(True)
    w = _plain_worker(tmp_path, factory, hw_accel="d3d11", substream=True)
    assert w._open(w.cfg["url"], w.cfg).isOpened()
    assert calls == [("rtsp://h/cam?subtype=1", "d3d11")]


def test_worker_falls_back_to_cpu_when_hardware_open_fails(tmp_path):
    calls = []

    def factory(url, tr, hw=None):
        calls.append(hw)
        return _Cap(hw is None)                     # с аппаратным режимом не открывается, программно — да
    w = _plain_worker(tmp_path, factory, hw_accel="auto")
    assert w._open(w.cfg["url"], w.cfg).isOpened() and w._hw_off
    assert calls == ["auto", None]
    calls.clear()
    assert w._open(w.cfg["url"], w.cfg).isOpened()
    assert calls == [None], "после отката воркер больше не просит аппаратный режим"


def test_worker_dead_camera_does_not_disable_hardware(tmp_path):
    w = _plain_worker(tmp_path, lambda url, tr, hw=None: _Cap(False), hw_accel="auto")
    assert not w._open(w.cfg["url"], w.cfg).isOpened() and not w._hw_off, "камера мертва и так, дело не в видеоядре"


def test_worker_gives_up_hardware_after_three_opens_without_frames(tmp_path):
    w = _plain_worker(tmp_path, lambda url, tr, hw=None: _Cap(True), hw_accel="auto")
    w._hw_giveup_check(False)
    w._hw_giveup_check(False)
    assert not w._hw_off
    w._hw_giveup_check(True)                        # кадры были — это обычный обрыв связи, счётчик не растёт
    assert w._hw_fail == 2 and not w._hw_off
    w._hw_giveup_check(False)
    assert w._hw_off
    w2 = _plain_worker(tmp_path, lambda url, tr, hw=None: _Cap(True))   # аппаратное не запрошено — не трогаем
    for _ in range(5):
        w2._hw_giveup_check(False)
    assert not w2._hw_off


def test_worker_state_reports_decoder(tmp_path):
    w, _ = _worker(tmp_path, make_frames(10))
    try:
        assert _wait(lambda: w.state()["connected"])
        assert w.state()["decoder"] == "", "подменённый источник способ декодирования не сообщает"
    finally:
        w.stop()


def test_track_flow_off_skips_optical_flow_for_diff_zone(monkeypatch):
    """Оптический поток — самая дорогая часть анализа: зона на diff с выключенным track_flow его не считает (flow = 0)."""
    calls = []
    real = me.flow_metric
    monkeypatch.setattr(me, "flow_metric", lambda *a, **k: calls.append(1) or real(*a, **k))
    f0, f1 = make_frames(2)

    def zone(**kw):
        z = me.normalize_camera({"id": "t", "url": "x", "zones": [dict({"id": "z1", "points": LEFT, "smooth": 2}, **kw)]})["zones"][0]
        return z, me.ZoneRT(z, None, (W, H))

    z, zr = zone(metric="diff")
    assert z["track_flow"] is True, "по умолчанию как раньше: flow считается"
    zr.step(f0, f1, 0.3)
    assert calls and zr.flow > 0
    calls.clear()
    z, zr = zone(metric="diff", track_flow=False)
    zr.step(f0, f1, 0.3)
    assert z["track_flow"] is False and not calls and zr.flow == 0.0 and zr.diff > 0
    z, zr = zone(metric="flow", track_flow=False)
    zr.step(f0, f1, 0.3)
    assert calls and zr.flow > 0, "зона на flow считает его всегда, даже при track_flow=false"


# ───────────────────────────── вид окна: жом есть / пусто / движется ─────────────────────────────
def grain_frames(n, move_left=False, seed=3):
    """Левая половина — крупа (шум по пикселям, как жом в окне), правая — гладкая стенка. Крупа может ехать (3 px/кадр)."""
    rng = np.random.default_rng(seed)
    grain = rng.integers(60, 200, (H, W)).astype(np.float32)
    out = []
    for i in range(n):
        left = np.roll(grain, 3 * i if move_left else 0, axis=1)[:, :W // 2]
        right = np.full((H, W // 2), 90.0, np.float32)     # не 128: ровный серый ~128 детектор считает битым потоком
        f = np.hstack([left, right]) + rng.normal(0, 1.0, (H, W))
        out.append(np.clip(f, 0, 255).astype(np.uint8))
    return out


def fill_cfg(**kw):
    fill = dict(fill_metric="tex", fill_smooth=3, fill_frames=3)
    fill.update(kw)
    return me.normalize_camera({"id": "c", "zones": [
        dict({"id": "z1", "points": LEFT, "metric": "diff", "out": {"kind": "holding", "address": 4, "value": "state"}}, **fill),
        dict({"id": "z2", "points": RIGHT, "metric": "diff", "out": {"kind": "holding", "address": 5, "value": "state"}}, **fill)]})


def test_fill_defaults_off_and_validation():
    z = me.normalize_camera({"id": "c", "zones": [{"id": "z1", "points": LEFT, "out": {"kind": "coil", "address": 1}}]})["zones"][0]
    assert z["fill_metric"] == "off" and z["fill_on"] == 0 and z["out"]["value"] == "bit", "без настройки всё как раньше"
    z = me.normalize_camera({"id": "c", "zones": [{"id": "z1", "points": LEFT}]})["zones"][0]
    assert z["out"] is None
    for bad, part in [({"fill_metric": "цвет"}, "вида окна"), ({"fill_on": 2, "fill_off": 3}, "off"),
                      ({"out": {"kind": "coil", "address": 1, "value": "x"}}, "значение вывода")]:
        with pytest.raises(ValueError, match=part):
            me.normalize_camera({"id": "c", "zones": [dict({"id": "z1", "points": LEFT}, **bad)]})


def test_fill_texture_separates_grain_from_smooth_wall():
    eng, st = run_engine(fill_cfg(), grain_frames(30))
    z = {x["id"]: x for x in st["zones"]}
    assert z["z1"]["tex"] > 10 * z["z2"]["tex"], z
    assert z["z1"]["fill"] is None and z["z1"]["state"] is None, "пороги не заданы — только измерение, решения нет"
    off = me.normalize_camera({"id": "c", "zones": [{"id": "z1", "points": LEFT}]})
    _, st = run_engine(off, grain_frames(10))
    assert "tex" not in st["zones"][0], "признак выключен — поля вида окна в состоянии нет"


def test_fill_three_states_pulp_standing_empty_standing_moving():
    cfg = fill_cfg(fill_on=6.0, fill_off=3.0)
    _, st = run_engine(cfg, grain_frames(30))
    z = {x["id"]: x for x in st["zones"]}
    assert (z["z1"]["bit"], z["z1"]["fill"], z["z1"]["state"]) == (0, 1, 1), "жом есть и стоит"
    assert (z["z2"]["bit"], z["z2"]["fill"], z["z2"]["state"]) == (0, 0, 0), "пусто и стоит"
    _, st = run_engine(cfg, grain_frames(30, move_left=True))
    z = {x["id"]: x for x in st["zones"]}
    assert (z["z1"]["bit"], z["z1"]["state"]) == (1, 2), "жом едет"
    assert z["z2"]["state"] == 0
    eng = me.CameraEngine(cfg)
    dead = eng.dead_state(1.0, "нет кадров")
    assert [x["state"] for x in dead["zones"]] == [0, 0], "камера мёртвая — состояние 0"


def test_fill_invert_means_pulp_when_value_is_low():
    cfg = fill_cfg(fill_on=6.0, fill_off=3.0, fill_invert=True)
    _, st = run_engine(cfg, grain_frames(30))
    z = {x["id"]: x for x in st["zones"]}
    assert (z["z1"]["fill"], z["z2"]["fill"]) == (0, 1), "в режиме «жом при низком» гладкое окно — это жом"


def test_fill_state_goes_to_plc_and_falls_back_to_bit_without_thresholds():
    cfg = fill_cfg(fill_on=6.0, fill_off=3.0)
    eng, st = run_engine(cfg, grain_frames(30))
    assert eng.compute_writes(st) == [("holding", 4, 1), ("holding", 5, 0)]
    eng, st = run_engine(cfg, grain_frames(30, move_left=True))
    assert eng.compute_writes(st)[0] == ("holding", 4, 2)
    eng, st = run_engine(fill_cfg(), grain_frames(30, move_left=True))      # пороги не заданы — пишется бит движения
    assert eng.compute_writes(st) == [("holding", 4, 1), ("holding", 5, 0)]


def test_record_gets_fill_columns_only_for_zones_with_fill(tmp_path):
    w, _ = _worker(tmp_path, grain_frames(40, move_left=True), zones=[
        {"id": "z1", "points": LEFT, "metric": "diff", "fill_metric": "tex", "fill_smooth": 2},
        {"id": "z2", "points": RIGHT, "metric": "diff"}])
    try:
        assert _wait(lambda: (w.state()["result"] or {}).get("alive"))
        name = w.start_record()
        time.sleep(0.6)
        w.stop_record()
        lines = (tmp_path / "records" / name).read_text(encoding="utf-8-sig").splitlines()
    finally:
        w.stop()
    head = lines[0].split(";")
    assert head == ["time", "z1_flow", "z1_diff", "z1_bit", "z1_tex", "z1_lum", "z1_rel", "z1_fill", "z2_flow", "z2_diff", "z2_bit", "alive", "status"]
    rows = [l.split(";") for l in lines[1:] if l.endswith(";ok")]
    assert rows and all(len(r) == len(head) for r in rows) and float(rows[-1][4]) > 5 and float(rows[-1][5]) > 50


def window_frame(win_value, metal=200.0, scale=1.0, seed=5):
    """Светлый металл с окном: левая половина кадра — окно (win_value), вокруг металл. scale — «свет» (солнце, ночь) на весь кадр."""
    rng = np.random.default_rng(seed)
    f = np.full((H, W), metal, np.float32)
    f[60:180, 40:140] = win_value
    return np.clip(f * scale + rng.normal(0, 1.5, (H, W)), 0, 255).astype(np.uint8)


def test_fill_rel_is_brightness_relative_to_metal_and_ignores_overall_light():
    """rel = яркость окна / яркость металла вокруг: тёмное окно (пусто) ≈ 0.65, светлое (жом) ≈ 0.92 — при любом общем свете."""
    z = me.normalize_camera({"id": "c", "zones": [{"id": "z1", "points": [[50, 70], [130, 70], [130, 170], [50, 170]],
                                                   "fill_metric": "rel", "fill_smooth": 1}]})["zones"][0]
    got = {}
    for name, win in (("empty", 130.0), ("pulp", 185.0)):
        for scale in (1.0, 0.6):
            zr = me.ZoneRT(z, None, (W, H))
            f = window_frame(win, scale=scale)
            zr.step(f, f, 0.3)
            got[(name, scale)] = zr.rel
    assert abs(got[("empty", 1.0)] - 0.65) < 0.03 and abs(got[("pulp", 1.0)] - 0.925) < 0.03, got
    assert abs(got[("empty", 0.6)] - got[("empty", 1.0)]) < 0.03 and abs(got[("pulp", 0.6)] - got[("pulp", 1.0)]) < 0.03, "общий свет rel не меняет"
    assert zr.ring is not None


def test_fill_rel_decides_pulp_with_thresholds_and_zone_at_frame_edge_still_works():
    cfg = me.normalize_camera({"id": "c", "zones": [
        {"id": "z1", "points": [[50, 70], [130, 70], [130, 170], [50, 170]], "fill_metric": "rel", "fill_on": 0.8, "fill_off": 0.74,
         "fill_smooth": 2, "fill_frames": 2},
        {"id": "z2", "points": [[0, 0], [60, 0], [60, 40], [0, 40]], "fill_metric": "rel", "fill_on": 0.8, "fill_off": 0.74}]})
    eng = me.CameraEngine(cfg)
    st = None
    for i in range(8):
        st = eng.process(window_frame(185.0, seed=i), float(i)) or st
    assert st["zones"][0]["fill"] == 1 and st["zones"][0]["state"] == 1
    for i in range(8, 16):
        st = eng.process(window_frame(130.0, seed=i), float(i)) or st
    assert st["zones"][0]["fill"] == 0 and st["zones"][0]["state"] == 0
    assert st["zones"][1]["rel"] >= 0, "окно у края кадра: полоса вокруг обрезается, расчёт не падает"


def test_scenario_signal_pulp_sums_windows_with_pulp_and_move_ors_motion():
    """Шахта: один сценарий считает окна с жомом (уровень 0..3), другой — есть ли движение в окнах."""
    import motion_scenarios as sc
    zs = [{"id": "w3", "bit": 0, "fill": 1}, {"id": "w2", "bit": 1, "fill": 1}, {"id": "w1", "bit": 1, "fill": 1}, {"id": "w0", "bit": 0}]
    st = {"a": {"result": {"alive": True, "zones": zs}}}
    srcs = [{"camera": "a", "zone": z} for z in ("w3", "w2", "w1")]
    assert sc.evaluate("sum", sc.source_bits(srcs, st, "pulp")) == 3
    assert sc.evaluate("or", sc.source_bits(srcs, st, "move")) == 1 and sc.evaluate("sum", sc.source_bits(srcs, st)) == 2
    assert sc.source_bits([{"camera": "a", "zone": "w0"}], st, "pulp") == [0], "без вида окна жома нет"
    st["a"]["result"]["alive"] = False
    assert sc.source_bits(srcs, st, "pulp") == [0, 0, 0], "камера мертва — 0"


def test_scenario_signal_default_move_and_validation():
    cfg = me.normalize_config(_two_cams_cfg([{"id": "s1"}, {"id": "s2", "signal": "pulp"}]))
    assert [s["signal"] for s in cfg["scenarios"]] == ["move", "pulp"]
    with pytest.raises(ValueError, match="сигнал"):
        me.normalize_config(_two_cams_cfg([{"id": "s1", "signal": "x"}]))


def test_scenario_group_source_takes_group_value_and_sum_adds_values():
    """Источник сценария — группа («вычисление»): её значение (наработка %, уровень) идёт в сценарий как есть, sum плюсует значения."""
    import motion_scenarios as sc
    st = {"a": {"result": {"alive": True, "zones": [{"id": "z1", "bit": 1}], "groups": [{"id": "g1", "value": 78}]}},
          "b": {"result": {"alive": False, "zones": [], "groups": [{"id": "g1", "value": 5}]}}}
    one = sc.source_bits([{"camera": "a", "group": "g1"}], st)
    assert one == [78] and sc.evaluate("sum", one) == 78 and sc.evaluate("or", one) == 1
    mixed = sc.source_bits([{"camera": "a", "group": "g1"}, {"camera": "a", "zone": "z1"}, {"camera": "b", "group": "g1"}, {"camera": "a", "group": "нет"}], st)
    assert mixed == [78, 1, 0, 0] and sc.evaluate("sum", mixed) == 79 and sc.evaluate("and", mixed) == 0


def test_scenario_normalize_keeps_group_sources_and_drops_missing_or_mirror():
    cfg = me.normalize_config({"cameras": [{"id": "a", "zones": [{"id": "z1", "points": LEFT}, {"id": "z2", "points": RIGHT}],
                                            "groups": [{"id": "g1", "type": "sum", "zones": ["z1", "z2"]}]}],
                               "scenarios": [{"id": "s1", "rule": "sum", "sources": [{"camera": "a", "group": "g1"}, {"camera": "a", "group": "g1"},
                                                                                       {"camera": "a", "group": "g9"}, {"camera": "a", "zone": "z1"}]}]})
    assert cfg["scenarios"][0]["sources"] == [{"camera": "a", "group": "g1"}, {"camera": "a", "zone": "z1"}]
