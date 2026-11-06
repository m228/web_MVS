"""Камеры движения в пульсе: имя, работает ли, кадры в секунду, давность последнего кадра, запись; диск — в ГБ. Нет сервиса — нет блока."""
import json

import pytest

import sysmon
from test_sysmon import Clock, make_sources, sm  # noqa: F401  (sm — фикстура)

CAMS = [
    {"id": "c1", "name": "Вал 1", "enabled": True, "connected": True, "error": "", "fps": 24.96, "age_s": 0.4, "recording": "rec1", "result": {"x": 1}},
    {"id": "c2", "name": "Вал 2", "enabled": True, "connected": False, "error": "нет RTSP-потока " + "я" * 200, "fps": 0.0, "age_s": None, "recording": None},
    {"id": "c3", "name": "Лента", "enabled": False, "connected": False, "error": "", "fps": 0.0, "age_s": None, "recording": None},
]
DISK = {"free": 380_400_000_000, "total": 1_000_000_000_000}


def snap(**src):
    return sysmon.Agent(sources=make_sources(**src), clock=Clock()).snapshot()


def test_motion_block_in_snapshot(sm):
    s = snap(motion=lambda: {"cameras": CAMS}, disk=lambda: DISK)
    m = s["motion"]
    assert [c["name"] for c in m["cameras"]] == ["Вал 1", "Вал 2", "Лента"]
    c1, c2, c3 = m["cameras"]
    assert (c1["on"], c1["ok"], c1["fps"], c1["age_s"], c1["rec"], c1["error"]) == (True, True, 25.0, 0.0, True, None)
    assert (c2["ok"], c2["rec"], c2["age_s"]) == (False, False, None) and len(c2["error"]) == 80, "ошибка обрезается"
    assert c3["on"] is False
    assert "disk" not in m and s["disk"] == {"free_gb": 380.4, "total_gb": 1000.0}, "диск — общий блок снимка, не часть движения"
    assert "result" not in json.dumps(m), "внутренности сервиса движения в пульс не уходят"
    assert "Камеры движения: 1 из 2 работают" in s["text"] and "Диск: свободно 380.4 ГБ из 1000" in s["text"], "выключенная камера в счёт не идёт"


@pytest.mark.parametrize("value", [None, {}, {"cameras": []}, {"cameras": ["мусор"]}])
def test_no_motion_block_without_cameras(sm, value):
    s = snap(motion=lambda: value)
    assert "motion" not in s and "Камеры движения" not in s["text"]


def test_no_motion_source_or_broken_source_is_fine(sm):
    assert "motion" not in snap()

    def boom():
        raise RuntimeError("сервис упал")
    s = snap(motion=boom)
    assert "motion" not in s, "ошибка источника не роняет пульс"


def test_motion_without_disk_and_camera_limit(sm):
    many = [{"id": "c%d" % i, "name": "К%d" % i, "enabled": True, "connected": True} for i in range(40)]
    m = snap(motion=lambda: {"cameras": many})["motion"]
    assert len(m["cameras"]) == sysmon.MOTION_CAMS_MAX and "disk" not in m


def test_motion_works_on_plant_without_microscope(sm):
    s = snap(micro_on=lambda: False, motion=lambda: {"cameras": CAMS}, disk=lambda: DISK)
    assert "micro" not in s and s["motion"]["cameras"]


def test_disk_is_in_snapshot_of_every_plant(sm):
    micro = snap(disk=lambda: DISK)                                   # микроскоп без сервиса движения
    assert micro["disk"] == {"free_gb": 380.4, "total_gb": 1000.0} and "motion" not in micro
    assert "disk" not in snap(), "источника нет — блока нет"
    assert "disk" not in snap(disk=lambda: {"free": 1, "total": 0}), "диск без размера не показываем"
