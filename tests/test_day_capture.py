"""Суточная съёмка новой RTSP-камеры (day_capture.py): раз в 20 мин ровно сутки, старые камеры при первом запуске не трогаем, чистка через неделю."""
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

import day_capture as dc

T0 = time.mktime((2026, 11, 3, 18, 0, 0, 0, 0, -1))
STEP = 20 * 60


def live_frame(v=90):
    img = np.full((72, 128, 3), v, np.uint8)
    img[::2, ::3] = (30, 40, 200)                # фактура, чтобы кадр не считался серой кашей
    return img


class Env:
    def __init__(self, tmp_path):
        self.cams = []
        self.frame = live_frame()
        self.calls = 0
        self.state = tmp_path / "day_capture.json"
        self.out = tmp_path / "day_shots"

    def grab(self, cam):
        self.calls += 1
        return self.frame

    def make(self):
        return dc.DayCapture(lambda: list(self.cams), self.grab, self.state, self.out)


def cam(url="rtsp://u:p@10.0.0.5/1", label="val", serial="rtsp_1"):
    return {"url": url, "label": label, "serial": serial}


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def files(env):
    return sorted(env.out.rglob("*.jpg"))


def test_cameras_already_in_base_at_first_run_are_not_filmed(env):
    env.cams = [cam()]
    d = env.make()
    assert d.run_once(T0) == 0 and env.calls == 0 and not files(env)
    assert d.run_once(T0 + 5 * STEP) == 0                    # и позже тоже: они «известные»
    assert env.state.is_file()


def test_new_camera_is_filmed_every_20_min_for_exactly_one_day(env):
    env.cams = [cam("rtsp://old")]
    d = env.make()
    d.run_once(T0)
    env.cams.append(cam())                                   # добавили и подключили: попала в базу
    t = T0 + 30
    d.run_once(t)
    assert len(files(env)) == 1                              # первый кадр сразу
    for k in range(1, 26 * 60):                  # минута за минутой, 26 часов
        t += 60
        d.run_once(t)
    shots = files(env)
    assert len(shots) == 73                                  # 0, 20, 40 мин … 24 ч → 73 кадра, потом хватит
    assert all("val" in str(p) for p in shots)
    by_url = d._plans["rtsp://u:p@10.0.0.5/1"]
    assert by_url["done"] and by_url["count"] == 73
    assert not [p for p in env.out.rglob("*") if "old" in str(p)]


def test_frame_is_saved_full_size_in_date_folder(env):
    env.cams = []
    d = env.make()
    d.run_once(T0)
    env.cams = [cam()]
    d.run_once(T0 + 10)
    p = files(env)[0]
    assert p.parent.name == time.strftime("%Y-%m-%d", time.localtime(T0 + 10)) and p.parent.parent.name == "val"
    assert cv2.imdecode(np.fromfile(p, np.uint8), cv2.IMREAD_COLOR).shape == (72, 128, 3)


def test_state_survives_restart_and_does_not_reseed(env):
    env.cams = []
    d = env.make()
    d.run_once(T0)
    env.cams = [cam()]
    d.run_once(T0 + 10)
    d2 = env.make()                                          # перезапуск приложения через 5 минут
    d2.run_once(T0 + 310)
    assert len(files(env)) == 1                              # рано: следующий кадр через 20 мин от первого
    d2.run_once(T0 + 10 + STEP)
    assert len(files(env)) == 2


def test_failed_grab_is_retried_and_day_counts_from_first_good_frame(env):
    env.cams = []
    d = env.make()
    d.run_once(T0)
    env.cams = [cam()]
    env.frame = None                                         # камера не отвечает
    d.run_once(T0 + 10)
    assert not files(env) and d._plans["rtsp://u:p@10.0.0.5/1"]["start"] is None
    d.run_once(T0 + 20)
    assert env.calls == 1                                    # повтор не раньше чем через минуту
    env.frame = live_frame()
    d.run_once(T0 + 100)
    p = d._plans["rtsp://u:p@10.0.0.5/1"]
    assert len(files(env)) == 1 and p["start"] == T0 + 100


def test_grey_mush_frame_is_not_saved(env):
    env.cams = []
    d = env.make()
    d.run_once(T0)
    env.cams = [cam()]
    env.frame = np.full((72, 128, 3), 128, np.uint8)         # декодер стёр картинку
    d.run_once(T0 + 10)
    assert not files(env)


def test_camera_that_never_connects_is_given_up_after_a_week(env):
    env.cams = []
    d = env.make()
    d.run_once(T0)
    env.cams = [cam()]
    env.frame = None
    t = T0 + 10
    while t < T0 + 8 * 86400:
        d.run_once(t)
        t += 3600
    assert d._plans["rtsp://u:p@10.0.0.5/1"]["done"] and not files(env)


def test_removed_camera_is_dropped_from_plan(env):
    env.cams = []
    d = env.make()
    d.run_once(T0)
    env.cams = [cam()]
    d.run_once(T0 + 10)
    env.cams = []
    d.run_once(T0 + 20)
    assert d._plans == {}


def test_corrupt_state_file_is_treated_as_first_run_not_as_everything_new(env):
    env.cams = [cam()]
    env.state.write_text("{битый json", encoding="utf-8")
    d = env.make()
    d.run_once(T0)
    assert env.calls == 0


def test_purge_deletes_only_old_day_folders(env):
    d = env.make()
    now = T0 + 20 * 86400
    old = env.out / "val" / "2026-11-03"
    fresh = env.out / "val" / time.strftime("%Y-%m-%d", time.localtime(now - 2 * 86400))
    odd = env.out / "val" / "notes"
    only_old = env.out / "gone" / "2026-11-01"
    for p in (old, fresh, odd, only_old):
        p.mkdir(parents=True)
        (p / "a.jpg").write_bytes(b"x")
    assert d.purge(now) == 2
    assert not old.exists() and fresh.exists() and odd.exists()
    assert not (env.out / "gone").exists()                   # опустевшая папка камеры убрана
