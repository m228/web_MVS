"""GigE SDK-поток: кадры с потерянными пакетами не показываем, счётчики и «медленные» кадры идут в метрики.

Камеры нет — поток подменён FakeStream: кадры 8×8 с заданным числом потерянных пакетов."""
import threading
import time

import numpy as np

from camera_core import SDK_MAX_BAD_FRAMES
from camera_core import gige_worker as gw


class FakeManager:
    def check(self):
        return True


def make_stream_cls(lost_plan, period=0.02):
    """lost_plan — потерянные пакеты по кадрам (дальше 0). Возвращает класс FakeStream."""
    state = {"i": 0, "resend": 0}

    class FakeStream:
        def __init__(self, info):
            self.last_lost = 0

        def open(self, settings=None):
            pass

        def read_config(self):
            return {}

        def grab(self, timeout_ms=300):
            time.sleep(period)
            i = state["i"]
            state["i"] += 1
            self.last_lost = lost_plan[i] if i < len(lost_plan) else 0
            if self.last_lost:
                state["resend"] += 5
            return 8, 8, "BayerRG8", np.full((8, 8), i, np.uint8)

        def net_stats(self):
            return {"frames": state["i"], "lost_frames": 0, "lost_packets": 0, "resend_req": state["resend"],
                    "resend_got": state["resend"], "mb": 0.1}

        def close(self):
            pass

    return FakeStream, state


def run_worker(monkeypatch, lost_plan, fps=None, n_frames=6, period=0.02):
    """Запускает generate(), собирает n_frames показанных кадров; возвращает (worker, число отданных кадров)."""
    cls, state = make_stream_cls(lost_plan, period)
    monkeypatch.setattr(gw, "_sdk_device_info", lambda serial: object())
    monkeypatch.setattr(gw.sdk_gige, "available", lambda: True)
    monkeypatch.setattr(gw.sdk_gige, "GigeSdkStream", cls)
    monkeypatch.setattr(gw, "_to_bgr", lambda raw, w, h, pf: np.zeros((8, 8, 3), np.uint8))
    w = gw.CameraWorker("LOST", FakeManager())
    got = 0
    for _ in w.generate(fps=fps):
        got += 1
        if got >= n_frames:
            break
    w.running = False
    return w, got, state


def test_clean_frames_all_shown(monkeypatch):
    w, got, _ = run_worker(monkeypatch, [])
    assert got == 6
    assert w.metrics["lost_frames"] == 0 and w.metrics["lost_packets"] == 0
    assert w.metrics["net"]["resend_got"] == 0


def test_frames_with_lost_packets_are_dropped(monkeypatch):
    # кадры 1 и 2 (с нуля) битые — наружу не уходят, счётчики растут, переспросы видны в net
    w, got, state = run_worker(monkeypatch, [0, 3, 7, 0, 0, 0, 0, 0])
    assert got == 6
    assert state["i"] == 8                       # SDK отдал 8 кадров, показано 6 (2 битых отброшено)
    assert w.metrics["lost_frames"] == 2
    assert w.metrics["lost_packets"] == 10
    assert w.metrics["net"]["resend_got"] == 10


def test_endless_bad_frames_are_shown_after_limit(monkeypatch):
    # сеть совсем плохая: все кадры битые — после SDK_MAX_BAD_FRAMES подряд показываем как есть (картинка не должна стоять)
    w, got, state = run_worker(monkeypatch, [2] * 100, n_frames=3)
    assert got == 3
    assert state["i"] == SDK_MAX_BAD_FRAMES + 3
    assert w.metrics["lost_frames"] == SDK_MAX_BAD_FRAMES + 3


def test_slow_frame_is_recorded_with_breakdown(monkeypatch):
    # fps=50 -> период 0.02 с; кадр с задержкой 0.1 с = «медленный» (порог 1,25 периода)
    cls, state = make_stream_cls([])
    orig_grab = cls.grab

    def slow_grab(self, timeout_ms=300):
        if state["i"] == 3:
            time.sleep(0.1)
        return orig_grab(self, timeout_ms)

    cls.grab = slow_grab
    monkeypatch.setattr(gw, "_sdk_device_info", lambda serial: object())
    monkeypatch.setattr(gw.sdk_gige, "available", lambda: True)
    monkeypatch.setattr(gw.sdk_gige, "GigeSdkStream", cls)
    monkeypatch.setattr(gw, "_to_bgr", lambda raw, w, h, pf: np.zeros((8, 8, 3), np.uint8))
    w = gw.CameraWorker("SLOW", FakeManager())
    got = 0
    for _ in w.generate(fps=50):
        got += 1
        if got >= 7:
            break
    w.running = False
    assert w.metrics["slow_n"] >= 1
    rec = w.metrics["slow"][0]
    assert rec["dt"] > 0.1 and set(rec) >= {"t", "dt", "proc_ms", "yield_ms", "resent", "lost"}
    assert rec["proc_ms"] < 50                   # задержка не у нас в обработке, а в grab (камера/линк)
