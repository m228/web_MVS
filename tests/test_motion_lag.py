"""Очередь кадров камеры: QueueLag (отставание по времени потока), догонялка в CameraWorker, недоверие к Δ верх/низ при очереди.

20.11 на заводе обработка шла на 8,9 к/с при камере 10 к/с: кадры копились в очереди, обрабатывались все подряд, и стрелка val_Up
отставала на 4–6 с (±20–25° у вала при 4,2°/с). Метка времени кадра — «когда взяли», очередь была невидима ни выравниванию, ни видимости.
"""
import sys
import time
from pathlib import Path

import cv2
import pytest

MS = Path(__file__).resolve().parent.parent / "motion_service"
sys.path.insert(0, str(MS))

import motion_camera as mc  # noqa: E402
import motion_engine as me  # noqa: E402
import motion_modbus as mm  # noqa: E402
import motion_shafts as ms  # noqa: E402
from motion_camera import CameraWorker, QueueLag  # noqa: E402
from test_motion_cycle import H, W, rod_frame  # noqa: E402
from test_motion_shafts import cfg_two_rows, runner, states  # noqa: E402


# ───────────────────────── QueueLag на синтетике ─────────────────────────

def stream(n, fps=10.0, delay=1.7, jitter=0.02, t0=1000.0):
    """[(приход, время потока мс)] без очереди: кадр i снят в i/fps, приходит через постоянную задержку камеры ± дрожание."""
    return [(t0 + i / fps + delay + jitter * ((i % 3) - 1), i / fps * 1000.0) for i in range(n)]


def feed(q, pts):
    return [q.update(w, p) for w, p in pts]


def test_steady_stream_has_no_lag_whatever_the_constant_camera_delay():
    lags = feed(QueueLag(), stream(300, delay=4.2))                  # у val_Up задержка кадра 4 с — это не очередь
    assert all(v is None for v in lags[:mc.LAG_GOOD_N]) and all(v is not None for v in lags[mc.LAG_GOOD_N:])
    assert max(v for v in lags if v is not None) < 0.06


def test_stall_shows_up_as_lag_then_drains_when_frames_are_taken_fast():
    pts = stream(100)
    wall = pts[-1][0]
    for i in range(100, 220):                                        # обработка встала на 5 с, дальше кадры из очереди берём по 10 мс
        nominal = 1000.0 + i / 10.0 + 1.7
        wall = nominal + 5.0 if i == 100 else max(nominal, wall + 0.01)
        pts.append((wall, i / 10.0 * 1000.0))
    lags = feed(QueueLag(), pts)
    assert lags[99] < 0.06 and lags[100] == pytest.approx(5.0, abs=0.1)
    assert all(b <= a + 1e-9 for a, b in zip(lags[100:160], lags[101:161]))       # очередь только тает
    assert lags[170] < 0.1 and lags[-1] < 0.06


def test_slow_consumer_makes_lag_grow_linearly():
    """Цикл 105 мс на кадр при камере 100 мс: отставание +0,05 с в секунду (локальный опыт 20.11 дал 3,77 с за 72 с)."""
    pts = [(1000.0 + i * 0.105, i * 100.0) for i in range(700)]       # ≈ 73 с
    lags = feed(QueueLag(), pts)
    assert lags[-1] == pytest.approx(699 * 0.005, abs=0.05) and lags[-1] > mc.LAG_CATCHUP_S


def test_stream_without_time_gives_no_lag():
    assert all(v is None for v in feed(QueueLag(), [(1000.0 + i * 0.1, 25.0) for i in range(100)]))        # заглушка: константа
    assert all(v is None for v in feed(QueueLag(), [(1000.0 + i * 0.1, 0.0) for i in range(100)]))
    assert QueueLag().update(1000.0, None) is None and QueueLag().update(1000.0, float("nan")) is None


def test_stream_restart_resets_the_base():
    pts = stream(100)
    t = pts[-1][0] + 3.0                                              # переподключение: время потока снова с нуля, часы идут дальше
    pts += [(t + i / 10.0, i * 100.0) for i in range(60)]
    lags = feed(QueueLag(), pts)
    assert all(v is None for v in lags[100:100 + mc.LAG_GOOD_N])      # пока не убедились, что время потока снова растёт — не судим
    assert max(v for v in lags[100 + mc.LAG_GOOD_N:] if v is not None) < 0.06      # прежняя база не делает из паузы очередь


def test_camera_clock_drift_is_not_lag():
    q = QueueLag()
    worst = 0.0
    for i in range(36000):                                            # час при 10 к/с, часы камеры отстают на 100 ppm (0,36 с за час)
        t = i / 10.0
        v = q.update(1000.0 + t, t * (1.0 - 1e-4) * 1000.0)
        if v is not None:
            worst = max(worst, v)
    assert worst < 0.1


# ───────────────────────── настоящий воркер: догонялка ─────────────────────────

class QueueCap:
    """Поток 20 к/с. На кадре stall_at процессор «встаёт» на stall_s секунд, за это время в очереди копятся кадры; их grab() отдаёт без пауз
    (как из сокета). Время потока CAP_PROP_POS_MSEC растёт по 50 мс на кадр."""
    FPS = 20.0

    _frames = None                      # кадры строим один раз на весь файл: каждый — 12 МБ временных массивов

    def __init__(self, stall_at=40, stall_s=1.5):
        if QueueCap._frames is None:
            QueueCap._frames = [rod_frame({(250, 270): a, (700, 270): 360 - a}) for a in range(0, 80, 10)]
        self.frames = QueueCap._frames
        self.t0 = time.time()
        self.m = 0
        self.stall_at, self.stall_s = stall_at, stall_s
        self.pos_ms = 0.0

    def isOpened(self):
        return True

    def grab(self):
        self.m += 1
        if self.m == self.stall_at:
            time.sleep(self.stall_s)
        wait = self.t0 + self.m / self.FPS - time.time()               # кадр ещё не снят — ждём его; снят давно (очередь) — отдаём сразу
        time.sleep(wait if wait > 0 else 0.003)                        # 3 мс — цена декода кадра из очереди
        self.pos_ms = self.m / self.FPS * 1000.0
        return True

    def retrieve(self):
        return True, self.frames[self.m % len(self.frames)].copy()

    def release(self):
        pass

    def set(self, *_):
        return True

    def get(self, prop, *_):
        return self.pos_ms if prop == cv2.CAP_PROP_POS_MSEC else 25.0


class NoPosCap(QueueCap):
    def get(self, prop, *_):
        return 25.0


def _run_worker(tmp_path, cap, seconds):
    raw = {"id": "dif", "url": "rtsp://fake/cam", "hw_accel": "off", "proc_fps": 10, "frame_size": [W, H],      # не файл: у файла очередь не меряем
           "zones": [{"id": "L", "type": "cycle", "center": [250, 270], "radius": 170},
                     {"id": "R", "type": "cycle", "center": [700, 270], "radius": 170}]}
    w = CameraWorker(me.normalize_camera(raw), me.normalize_modbus({"enable": False}), mm.LinkRegistry(lambda h, p: None), tmp_path,
                     lambda url, tr: cap)
    w.start()
    try:
        time.sleep(seconds)
        return w.frame_timing(), w.state(), w.rod_track("L")
    finally:
        w.stop()


def test_worker_drops_queued_frames_and_keeps_processed_ones_fresh(tmp_path):
    ft, st, tr = _run_worker(tmp_path, QueueCap(), 7.0)
    assert ft["skipped"] >= 10, ft                                    # очередь после просадки выброшена, а не разобрана по порядку
    assert ft["lag_max_s"] is not None and ft["lag_max_s"] <= mc.LAG_CATCHUP_S + 0.1, ft
    assert st["lag_s"] is not None and st["lag_s"] <= mc.LAG_CATCHUP_S + 0.1
    assert len(tr) >= 30 and all(b[0] > a[0] for a, b in zip(tr, tr[1:]))        # ряд валов по времени съёмки идёт строго вперёд
    assert st["result"]["alive"]


def test_worker_without_catchup_would_process_the_old_frames(tmp_path, monkeypatch):
    """Контроль: порог выключен — кадры из очереди обрабатываются, и отставание обработанных кадров видно (≈1,5 с)."""
    monkeypatch.setattr(mc, "LAG_CATCHUP_S", 1e6)
    ft, _, _ = _run_worker(tmp_path, QueueCap(), 6.0)
    assert ft["skipped"] == 0 and ft["lag_max_s"] >= 1.0, ft


def test_worker_with_stream_without_time_behaves_as_before(tmp_path):
    ft, st, tr = _run_worker(tmp_path, NoPosCap(), 5.0)
    assert ft["lag_s"] is None and ft["lag_max_s"] is None and ft["skipped"] == 0
    assert st["lag_s"] is None and len(tr) >= 20


# ───────────────────────── недоверие к Δ верх/низ при очереди ─────────────────────────

def _tol_runner():
    cfg = cfg_two_rows({"shafts": {"pairs": {"lnv": {"tol_angle": 5}, "nlr": {"tol_angle": 5}}}})
    cfg["modbus"]["enable"] = False
    return runner(cfg, states(dn=(100.0, 260.0, 5, -5), up=(120.0, 260.0, 4, -4)))     # левые валы разных камер разошлись на 20° > допуска 5°


def _bins(r, pid="lnv"):
    return sum(len(d) for d in r.learners[pid][1].bins)


def test_cross_camera_pair_in_queue_is_not_trusted_and_does_not_learn():
    r, _, M = _tol_runner()
    M.workers["up"].s["lag_s"] = 2.0                                  # кадры верхней камеры стоят в очереди 2 с
    for t in range(1, 12):
        r.tick(float(t))
    p = r.snapshot()["pairs"]["lnv"]
    assert p["stale"] is True and p["alarm_angle"] == 0               # 20° — это задержка, а не вал: тревога не включилась
    assert _bins(r) == 0 and r.hist["lnv"][-1][3] is True            # самоподстройка не училась, точка в истории — «в удержании»
    assert r.live(12.0)["pairs"]["lnv"]["stale"] is True
    assert r.snapshot()["pairs"]["nlr"]["stale"] is False             # оба вала пары из одного кадра нижней камеры: очередь им не мешает


def test_same_pair_without_queue_alarms_and_learns():
    r, _, _ = _tol_runner()
    for t in range(1, 12):
        r.tick(float(t))
    p = r.snapshot()["pairs"]["lnv"]
    assert p["stale"] is False and p["alarm_angle"] == 1 and _bins(r) > 0


def test_queue_neither_raises_nor_clears_the_alarm():
    r, _, M = _tol_runner()
    for t in range(1, 6):
        r.tick(float(t))
    assert r.snapshot()["pairs"]["lnv"]["alarm_angle"] == 1
    M.workers["up"].s = states(dn=(100.0, 260.0, 5, -5), up=(100.0, 260.0, 4, -4))["up"]      # валы в норме, но кадры верхней в очереди
    M.workers["up"].s["lag_s"] = 2.0
    for t in range(6, 20):
        r.tick(float(t))
    assert r.snapshot()["pairs"]["lnv"]["alarm_angle"] == 1           # очередь не гасит тревогу...
    del M.workers["up"].s["lag_s"]
    for t in range(20, 30):
        r.tick(float(t))
    assert r.snapshot()["pairs"]["lnv"]["alarm_angle"] == 0           # ...а свежие кадры гасят как обычно


def test_stale_threshold_is_above_catchup_threshold():
    assert ms.STALE_LAG_S > mc.LAG_CATCHUP_S
