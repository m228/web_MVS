"""motion_lvldec — перенос настроек LVLDec (config.ini рядом с LVLDec.exe) в камеру КЗ с группой «уровень».

Берём: адрес камеры, зоны-стёкла ([zones] lower / upper), параметры распознавания и сглаживания, адрес ПЛК и регистр Total
(панель кладёт их в группу-уровень и в сценарий записи с собственной, ВЫКЛЮЧЕННОЙ связью с ПЛК).
Не берём (в сервисе их роль играют другие механизмы): heartbeat-счётчик, перезапуск по залипанию значения и плановый перезапуск,
сжатие кадра перед моделью. Запись в ПЛК после переноса ВЫКЛЮЧЕНА: пока LVLDec.exe пишет в тот же регистр, второй писатель не нужен.
"""
from __future__ import annotations

import configparser
from typing import Optional


def parse_zone(text: str) -> Optional[list]:
    """Строка "x,y x,y …" → [[x, y], …] или None, если зона пустая или в ней меньше трёх точек (как в LVLDec)."""
    pts = []
    for tok in (text or "").split():
        try:
            x, y = tok.split(",")
            pts.append([int(round(float(x))), int(round(float(y)))])
        except ValueError:
            raise ValueError("зона: точка %r — нужно x,y" % tok)
    return pts if len(pts) >= 3 else None


def import_ini(text: str) -> dict:
    """Текст config.ini LVLDec → фрагмент для камеры: {url, model, zones{lower, upper}, group{…}, plc{host, port, device_id, address}, notes[…]}.
    Ошибка разбора → ValueError с понятным текстом."""
    cp = configparser.ConfigParser(inline_comment_prefixes=(";", "#"), interpolation=None)
    try:
        cp.read_string(text.lstrip("﻿"))
    except configparser.Error as e:
        raise ValueError("это не config.ini LVLDec: %s" % e)
    if not cp.has_section("detection") and not cp.has_section("zones"):
        raise ValueError("это не config.ini LVLDec: нет секций [detection] и [zones]")

    def f(sec, key, default):
        try:
            return cp.getfloat(sec, key, fallback=default)
        except ValueError:
            raise ValueError("[%s] %s: нужно число" % (sec, key))

    zones = {"lower": parse_zone(cp.get("zones", "lower", fallback="")), "upper": parse_zone(cp.get("zones", "upper", fallback=""))}
    notes = []
    if not zones["lower"]:
        notes.append("нижняя зона в config.ini не задана — разметь стёкла на кадре")
    if not zones["upper"]:
        notes.append("верхней зоны нет — уровень считается по одному стеклу")
    hb = int(f("plc", "heartbeat_register", -1))
    if hb >= 0:
        notes.append("heartbeat-счётчик (регистр %d) не переносится — «камера жива» даёт статус камеры" % hb)
    if f("runtime", "value_stuck_sec", 0) > 0 or f("runtime", "restart_hours", 0) > 0:
        notes.append("перезапуск по залипанию значения и плановый перезапуск не переносятся — их роль у сторожа кадра и сервиса")
    if int(f("detection", "resize_w", 0)) > 0 or int(f("detection", "resize_h", 0)) > 0:
        notes.append("сжатие кадра resize_w/resize_h не переносится — модель сама приводит кадр к своему размеру")
    model = cp.get("detection", "model", fallback="").strip()
    if model:
        notes.append("модель %s загрузи на вкладке «Уровень» (файл лежит в data\\ рядом с LVLDec.exe)" % model)
    notes.append("запись в ПЛК выключена — включи, когда LVLDec.exe перестанет писать в тот же регистр")
    return {
        "url": cp.get("camera", "rtsp_url", fallback="").strip(),
        "model": model,
        "zones": zones,
        "group": {
            "conf": f("detection", "conf", 0.35),
            "imgsz": int(f("detection", "imgsz", 0)),
            "infer_interval_s": max(0.2, f("detection", "infer_interval_sec", 2.0)),
            "alpha": f("detection", "alpha", 0.93),
            "warmup_s": f("runtime", "warmup_sec", 10.0),
            "glass1_min": f("detection", "glass1_min", 15.0),
            "glass2_min": f("detection", "glass2_min", 20.0),
            "glass1_full": f("detection", "glass1_full", 100.0),
            "glass2_full": f("detection", "glass2_full", 100.0),
        },
        # куда LVLDec писал Total: панель положит это в сценарий записи (связь с ПЛК выключена — включает человек)
        "plc": {"host": cp.get("plc", "ip", fallback="").strip(), "port": int(f("plc", "port", 502)),
                "device_id": int(f("plc", "device_id", 1)), "address": int(f("plc", "mb_address", 0))},
        "notes": notes,
    }
