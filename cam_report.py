"""Отчёт камер за смену: из эпизодов cam_events + сколько фото и видео записано (по файлам в папках сохранения).

Смены как в отчёте микроскопа: день 08–20, ночь 20–08 (следующего дня), сутки — 08:00 → 08:00. Время — местное время сервера.
Всё считается по окну [t0, t1], идущая смена обрезается по «сейчас», эпизоды обрезаются по границам окна.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

import paths

MIN_STOP_S = 60.0          # остановкой зоны считаем «стоит» не короче этого (короче — мигание у порога, в счёт остановок не идёт)
TOP_STOPS = 5              # сколько самых долгих остановок показываем у зоны
PHOTO_EXT = (".png", ".jpg", ".jpeg")
VIDEO_EXT = (".avi", ".mp4")

CV_KINDS = ("cv_dead", "cv_frozen", "cv_corrupt")


def shift_window(date: str, part: str) -> tuple:
    """(t0, t1) в секундах Unix для даты начала смены (ГГГГ-ММ-ДД) и части: day | night | all."""
    d = datetime.strptime(date, "%Y-%m-%d").replace(hour=8, minute=0, second=0, microsecond=0)
    if part == "day":
        a, b = d, d + timedelta(hours=12)
    elif part == "night":
        a, b = d + timedelta(hours=12), d + timedelta(hours=24)
    elif part == "all":
        a, b = d, d + timedelta(hours=24)
    else:
        raise ValueError("part: day | night | all")
    return a.timestamp(), b.timestamp()


def current_shift(now: float) -> tuple:
    """(дата начала, часть) смены, которая идёт в момент now."""
    d = datetime.fromtimestamp(now)
    if d.hour < 8:
        return (d - timedelta(days=1)).strftime("%Y-%m-%d"), "night"
    return d.strftime("%Y-%m-%d"), ("day" if d.hour < 20 else "night")


def _clip(ep: dict, t0: float, t1: float, now: float) -> float:
    end = ep["t1"] if ep["t1"] is not None else now
    return max(0.0, min(end, t1) - max(ep["t0"], t0))


def summarize(episodes: Iterable[dict], t0: float, t1: float, now: float, min_stop: float = MIN_STOP_S) -> dict:
    """{"window_s": длина наблюдаемого окна, "cams": {cam: сводка}} по эпизодам, пересекающимся с окном. Сводка:
       link {lost_s, n}, gray {s, n}, cv {s, n}, zones [{id, name, run_s, stop_s, stops_n, stops_s, longest_s, top: [{t0, dur}]}]"""
    end = min(t1, now)
    out: dict = {}
    for ep in episodes:
        sec = _clip(ep, t0, t1, now)
        if sec <= 0:
            continue
        c = out.setdefault(ep["cam"], {"link": {"lost_s": 0.0, "n": 0}, "gray": {"s": 0.0, "n": 0}, "cv": {"s": 0.0, "n": 0},
                                      "_zones": {}})
        k = ep["kind"]
        if k == "link_lost":
            c["link"]["lost_s"] += sec
            c["link"]["n"] += 1
        elif k == "gray":
            c["gray"]["s"] += sec
            c["gray"]["n"] += 1
        elif k in CV_KINDS:
            c["cv"]["s"] += sec
            c["cv"]["n"] += 1
        elif k in ("zone_on", "zone_off"):
            z = c["_zones"].setdefault(ep["zone"], {"id": ep["zone"], "name": ep.get("info") or ep["zone"], "run_s": 0.0,
                                                    "stop_s": 0.0, "stops": []})
            z["name"] = ep.get("info") or z["name"]
            if k == "zone_on":
                z["run_s"] += sec
            else:
                z["stop_s"] += sec
                if sec >= min_stop:
                    z["stops"].append({"t0": max(ep["t0"], t0), "dur": round(sec, 1)})
    for c in out.values():
        zs = []
        for z in c.pop("_zones").values():
            stops = sorted(z.pop("stops"), key=lambda s: -s["dur"])
            z.update(stops_n=len(stops), stops_s=round(sum(s["dur"] for s in stops), 1),
                     longest_s=stops[0]["dur"] if stops else 0.0, top=stops[:TOP_STOPS])
            z["run_s"], z["stop_s"] = round(z["run_s"], 1), round(z["stop_s"], 1)
            zs.append(z)
        c["zones"] = sorted(zs, key=lambda z: z["name"])
        c["link"]["lost_s"] = round(c["link"]["lost_s"], 1)
        c["gray"]["s"] = round(c["gray"]["s"], 1)
        c["cv"]["s"] = round(c["cv"]["s"], 1)
    return {"window_s": max(0.0, end - t0), "cams": out}


def _camera_tag(serial: str) -> str:
    # как BaseCameraWorker._camera_tag: имя папки камеры (серийник без спецсимволов)
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(serial or "camera")).strip("_") or "camera"


def _count(dirs: Iterable[Path], exts: tuple, t0: float, t1: float) -> int:
    n = 0
    for d in dirs:
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.is_file() and e.name.lower().endswith(exts):
                        m = e.stat().st_mtime
                        if t0 <= m < t1:
                            n += 1
        except OSError:
            continue
    return n


def count_files(serial: str, t0: float, t1: float) -> dict:
    """Фото и видео камеры, записанные в окне (по времени изменения файла): папки <данные>/dataset|Videos/[<проект>/]<серийник>."""
    tag = _camera_tag(serial)
    out = {}
    for key, root, exts in (("photos", "dataset", PHOTO_EXT), ("videos", "Videos", VIDEO_EXT)):
        base = Path(paths.DATA_DIR) / root
        dirs = [base / tag]
        try:
            dirs += [p / tag for p in base.iterdir() if p.is_dir()]
        except OSError:
            pass
        out[key] = _count(dirs, exts, t0, t1)
    return out
