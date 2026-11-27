"""Камеры движения в пульсе: имя, работает ли, кадры в секунду, давность последнего кадра, запись; диск — в ГБ. Нет сервиса — нет блока.
Неисправной камера становится, когда сервис движения держит её мёртвой дольше MOTION_BAD_S (connected сам по себе ничего не решает)."""
import json

import pytest

import sysmon
from test_sysmon import Clock, make_sources, sm  # noqa: F401  (sm — фикстура)

CAMS = [
    {"id": "c1", "name": "Вал 1", "enabled": True, "connected": True, "error": "", "fps": 24.96, "age_s": 0.4, "recording": "rec1",
     "result": {"alive": True, "reason": ""}},
    {"id": "c2", "name": "Вал 2", "enabled": True, "connected": False, "error": "нет RTSP-потока " + "я" * 200, "fps": 0.0, "age_s": None,
     "recording": None, "result": None},
    {"id": "c3", "name": "Лента", "enabled": False, "connected": False, "error": "камера выключена", "fps": 0.0, "age_s": None, "recording": None,
     "result": None},
]
DISK = {"free": 380_400_000_000, "total": 1_000_000_000_000}

ALIVE = {"id": "c1", "name": "Вал 1", "enabled": True, "connected": True, "error": "", "result": {"alive": True, "reason": ""}}


def dead(reason, **over):
    return {**ALIVE, "result": {"alive": False, "reason": reason}, **over}


def snap(**src):
    return sysmon.Agent(sources=make_sources(**src), clock=Clock()).snapshot()


class Plant:
    """Агент, которому по ходу теста меняют ответ сервиса движения; проверки идут тем же шагом, что и в приложении."""

    def __init__(self, cam=ALIVE):
        self.cam = cam                    # None — сервис движения молчит
        self.clock = Clock()
        src = make_sources(motion=lambda: None if self.cam is None else {"cameras": [self.cam]})
        self.agent = sysmon.Agent(sources=src, clock=self.clock)

    def run(self, seconds, cam="same"):
        if cam != "same":
            self.cam = cam
        for _ in range(int(seconds // sysmon.WATCH_EVERY_S)):
            self.clock.t += sysmon.WATCH_EVERY_S
            self.agent.tick()
        return self

    def block(self):
        s = self.agent.snapshot()
        c = s["motion"]["cameras"][0]
        return c["ok"], c["error"], s["text"]


def settled(cams, **src):
    """Снимок после того, как камеры наблюдали дольше порога."""
    clock = Clock()
    a = sysmon.Agent(sources=make_sources(motion=lambda: {"cameras": cams}, **src), clock=clock)
    for _ in range(sysmon.MOTION_BAD_S // sysmon.WATCH_EVERY_S + 1):
        a.tick()
        clock.t += sysmon.WATCH_EVERY_S
    return a.snapshot()


def test_motion_block_in_snapshot(sm):
    s = settled(CAMS, disk=lambda: DISK)
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


@pytest.mark.parametrize("cam, why", [
    (dead("картинка застыла"), "картинка застыла"),
    (dead("битый поток"), "битый поток"),
    (dead("нет кадров", connected=False, error="нет связи с источником"), "нет связи с источником"),
    (dead("нет свежих результатов анализа", error="нет свежих результатов анализа"), "нет свежих результатов анализа"),      # motion 3.0.47
    (dead("сбой анализа кадра", error="сбой анализа кадра"), "сбой анализа кадра"),
    (dead(""), "нет данных"),
    ({**ALIVE, "result": None}, "нет результата анализа"),                                  # поток открыт, а результата так и нет
    ({**ALIVE, "result": None, "connected": False}, "нет связи"),
    ({**ALIVE, "result": None, "connected": False, "error": "не задан адрес потока"}, "не задан адрес потока"),
])
def test_dead_camera_is_faulty_with_reason(sm, cam, why):
    ok, error, text = Plant(cam).run(sysmon.MOTION_BAD_S + 30).block()
    assert (ok, error) == (False, why)
    assert "Камеры движения: 0 из 1 работают · не работают: Вал 1 — " + why in text


def test_short_outage_is_not_a_fault(sm):
    p = Plant().run(60)
    p.run(sysmon.MOTION_BAD_S - 30, dead("поток оборвался"))
    assert p.block()[:2] == (True, None), "обрыв короче порога"
    p.run(sysmon.MOTION_RECOVER_S + 30, ALIVE)
    p.run(15, dead("поток оборвался"))
    assert p.block()[:2] == (True, None), "после перерыва в порядке счёт начинается заново"


def test_closed_stream_with_alive_result_is_fine(sm):
    cam = {**ALIVE, "connected": False, "error": "поток оборвался"}         # сервис движения ещё держит биты, в ПЛК камера «жива»
    ok, error, text = Plant(cam).run(sysmon.MOTION_BAD_S * 3).block()
    assert (ok, error) == (True, None) and "не работают" not in text


def test_camera_startup_is_not_a_fault(sm):
    p = Plant({**ALIVE, "connected": False, "result": None})                 # ждёт очереди на открытие
    assert p.run(45).block()[0] is True
    assert p.run(15, dead("нет кадров", connected=False, error="нет связи с источником")).block()[0] is True    # первое открытие не вышло
    assert p.run(15, {**ALIVE, "result": None}).block()[0] is True           # открылся, копится буфер кадров
    assert p.run(sysmon.MOTION_BAD_S * 2, ALIVE).block()[:2] == (True, None)


def test_flapping_frozen_camera_is_found_and_stays_faulty(sm):
    """Застывшая картинка: ~23 с «мёртвая», потом поток переоткрыт и ~10 с «живая», по кругу (так ведёт себя motion_camera)."""
    p, seen = Plant(), []
    for t in range(0, 600, sysmon.WATCH_EVERY_S):
        p.run(sysmon.WATCH_EVERY_S, ALIVE if t % 33 < 10 else dead("картинка застыла"))
        seen.append((t, p.block()[:2]))
    assert all(v == (False, "картинка застыла") for t, v in seen if t > sysmon.MOTION_BAD_S + 45), seen


def test_recovery_clears_fault(sm):
    p = Plant(dead("битый поток")).run(sysmon.MOTION_BAD_S + 30)
    assert p.run(sysmon.MOTION_RECOVER_S - 30, ALIVE).block()[0] is False, "пока рано"
    assert p.run(45).block()[:2] == (True, None)


def test_silent_motion_service_restarts_the_count(sm):
    p = Plant(dead("нет кадров")).run(30)
    p.run(600, None)                                                          # сервис не отвечал, потом поднялся: камеры снова запускаются
    assert p.run(15, dead("нет кадров")).block()[0] is True
    assert p.run(sysmon.MOTION_BAD_S + 15).block()[0] is False


def test_one_missed_answer_keeps_the_count(sm):
    p = Plant(dead("нет кадров")).run(60)
    p.run(15, None)
    assert p.run(60, dead("нет кадров")).block()[0] is False


def test_disabled_camera_is_never_watched(sm):
    p = Plant(dead("нет кадров")).run(sysmon.MOTION_BAD_S + 30)
    s = p.run(15, {**ALIVE, "enabled": False, "result": None, "connected": False}).agent.snapshot()
    assert s["motion"]["cameras"][0]["error"] is None and "0 из 0 работают" in s["text"] and "не работают" not in s["text"]
    assert p.run(15, dead("нет кадров")).block()[0] is True, "включили снова — это запуск"


def test_long_fault_list_is_cut_in_text(sm):
    many = [dead("нет кадров", id="c%d" % i, name="К%d" % i) for i in range(5)]
    text = settled(many)["text"]
    assert "К0 — нет кадров; К1 — нет кадров; К2 — нет кадров; и ещё 2" in text and "К3" not in text


def test_disk_is_in_snapshot_of_every_plant(sm):
    micro = snap(disk=lambda: DISK)                                   # микроскоп без сервиса движения
    assert micro["disk"] == {"free_gb": 380.4, "total_gb": 1000.0} and "motion" not in micro
    assert "disk" not in snap(), "источника нет — блока нет"
    assert "disk" not in snap(disk=lambda: {"free": 1, "total": 0}), "диск без размера не показываем"
