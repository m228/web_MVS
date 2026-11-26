"""Панель «Нагрузка»: расчёт скоростей, долей и запаса по подменённым счётчикам."""
import sys

import pytest

import host_load


def test_camera_key_ignores_password_and_substream():
    a = host_load.camera_key("rtsp://admin:p@ss@10.0.0.5:554/cam/realmonitor?channel=1&subtype=0")
    b = host_load.camera_key("rtsp://10.0.0.5/cam/realmonitor?channel=1&subtype=1")
    assert a == b == ("10.0.0.5", 1)
    assert host_load.camera_key("rtsp://u:x@10.0.0.5/cam/realmonitor?channel=2&subtype=0") == ("10.0.0.5", 2)
    assert host_load.camera_key("C:/video.mp4") is None


def test_estimate_takes_the_tightest_resource():
    e = host_load.estimate([("net", 5.0, 5.0, 5), ("cpu", 65.0, 50.0, 5), ("gpu", 10.0, 10.0, 5)], 25.0)
    assert e["limit_by"] == "cpu" and e["cameras"] == 2.0 and e["fps"] == 50
    assert e["by"]["net"] == 65.0 and e["by"]["gpu"] == 37.5


def test_estimate_none_without_cameras_and_zero_when_full():
    assert host_load.estimate([("cpu", 40.0, 20.0, 0)], 25.0) is None
    assert host_load.estimate([("cpu", 97.0, 40.0, 4)], 25.0)["cameras"] == 0.0


class Worker:
    def __init__(self, url, running=True, viewers=0, mbps=0.0, fps=25.0):
        self.rtsp_url, self.running, self._v = url, running, viewers
        self.metrics = {"fps": fps if running else 0.0, "bandwidth_mbps": mbps}

    def viewer_count(self):
        return self._v


class Probe:
    """Каждый вызов step() — прошло 4 с: адаптер принял 20 МБ (40 Мбит/с), процессор занят наполовину."""

    def __init__(self):
        self.n = 0

    def step(self):
        self.n += 1

    def adapters(self):
        return [{"index": 3, "name": "Камеры", "hardware": True, "link_bps": 10 ** 9, "rx": self.n * 20_000_000, "tx": self.n * 5_000_000},
                {"index": 4, "name": "Пустой виртуальный", "hardware": False, "link_bps": 10 ** 10, "rx": 0, "tx": 0}]

    def best_interface(self, host):
        return 3

    def system_times(self):
        return self.n * 500, self.n * 1000

    def process_time(self, pid):
        return self.n * {1: 200, 2: 100, 3: 50}[pid]

    def listening_pids(self):
        return {8768: 2, 8765: 3}

    def gpu(self):
        return [(1, "eng_3", 20.0), (2, "eng_3", 10.0), (99, "eng_3", 5.0), (99, "eng_9", 3.0)]


def _meter(probe, clock, workers, motion_url="http://127.0.0.1:8768"):
    cams = [{"url": "rtsp://a:b@10.0.0.%d/cam?channel=1&subtype=0" % i} for i in (1, 2)]
    return host_load.LoadMeter(
        workers_fn=lambda: workers, cameras_fn=lambda: cams,
        motion_cfg_fn=lambda: {"cameras": [{"id": "k1", "url": cams[0]["url"]}, {"id": "k2", "url": cams[1]["url"]}]},
        motion_state_fn=lambda: {"cameras": [{"id": "k1", "enabled": True, "connected": True},
                                             {"id": "k2", "enabled": True, "connected": False}]},
        motion_url_fn=lambda: motion_url, probe=probe, clock=lambda: clock[0], pid=1)


def test_rates_shares_streams_and_estimate():
    probe, clock = Probe(), [100.0]
    workers = {"A": Worker("rtsp://x@10.0.0.1/cam?channel=1&subtype=0", viewers=2, mbps=10.0),
               "B": Worker("rtsp://x@10.0.0.2/cam?channel=1&subtype=0")}
    meter = _meter(probe, clock, workers)
    assert meter.read()["ready"] is False
    probe.step()
    clock[0] += 4.0
    r = meter.read()
    assert r["ready"] is True
    assert [a["name"] for a in r["adapters"]] == ["Камеры"]
    a = r["adapters"][0]
    assert (a["rx_mbps"], a["tx_mbps"], a["link_mbps"], a["cameras"]) == (40.0, 10.0, 1000, 2)
    assert r["cpu"] == {"total": 50.0, "app": 20.0, "motion": 10.0, "other": 5.0, "cameras": 30.0}
    assert r["gpu"] == {"total": 35.0, "ours": 30.0}
    s = r["streams"]
    assert (s["app"], s["motion"], s["cameras"], s["decode_fps"], s["to_browsers_mbps"]) == (2, 1, 2, 75, 20.0)
    assert "keys" not in s
    assert r["estimate"]["limit_by"] == "cpu" and r["estimate"]["cameras"] == pytest.approx(2.3, abs=0.1)


def test_analysis_only_remembers_camera_fps_and_cache():
    probe, clock = Probe(), [100.0]
    workers = {"A": Worker("rtsp://x@10.0.0.1/cam?channel=1", fps=12.0)}
    meter = _meter(probe, clock, workers)
    meter.read()
    workers["A"] = Worker("rtsp://x@10.0.0.1/cam?channel=1", running=False)     # приложение камеру отпустило
    probe.step()
    clock[0] += 4.0
    r = meter.read()
    assert (r["streams"]["app"], r["streams"]["motion"], r["streams"]["fps"], r["streams"]["fps_assumed"]) == (0, 1, 12.0, False)
    clock[0] += 0.5
    assert meter.read() is r, "чаще раза в 2 с не пересчитываем"
    probe.step()
    clock[0] += 120.0
    assert meter.read()["ready"] is False, "после долгого перерыва замер начинается заново"


def test_remote_motion_service_is_not_counted():
    probe, clock = Probe(), [100.0]
    meter = _meter(probe, clock, {}, motion_url="http://10.9.9.9:8768")
    s = meter.read()["streams"]
    assert s["motion_remote"] is True and s["motion"] == 0 and s["cameras"] == 0


def test_broken_probe_does_not_raise():
    class Broken:
        def __getattr__(self, name):
            def boom(*a):
                raise OSError(name)
            return boom

    clock = [1.0]
    meter = _meter(Broken(), clock, {})
    meter.read()
    clock[0] += 4.0
    r = meter.read()
    assert r["cpu"] is None and r["gpu"] is None and r["adapters"] == [] and r["estimate"] is None


@pytest.mark.skipif(sys.platform != "win32", reason="счётчики Windows")
def test_real_windows_counters():
    probe = host_load.WinProbe()
    assert any(a["link_bps"] > 0 for a in probe.adapters())
    busy, total = probe.system_times()
    assert 0 <= busy <= total
    import os
    assert probe.process_time(os.getpid()) > 0
    assert probe.listening_pids()
