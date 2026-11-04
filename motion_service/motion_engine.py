"""engine — ядро анализа движения по зонам-полигонам (без сети и без потоков: только кадры → состояния → записи в ПЛК).

Из детекторов шахты (shnek_fill_detector) и ленты (belt_detector) вынесено общее: по каждой зоне считаем две метрики —
  flow — оптический поток Farneback (устойчив к мерцанию света и сжатию видео; так работают шахта и лента жома),
  diff — разность текущего и предыдущего кадра в зоне (простой «датчик вращения»; с вычитанием общей смены яркости,
         чтобы мерцание ламп и автоэкспозиция не давали ложного «движения»).
Какая метрика решает — поле zone.metric; вторая всё равно считается и показывается (для калибровки порогов).

Дальше как в старых детекторах: сглаживание → гистерезис on/off + debounce (frames_to_on / frames_to_off) → бит зоны.
Зона type=motion активна при ВЫСОКОЙ метрике (движется), type=presence — при НИЗКОЙ (густой ровный жом гасит поток).

Группы зон (groups): sum — сколько зон активно; monotonic — уровень снизу вверх (верхнее окно полное → нижние тоже, шахта);
worktimer — сколько секунд (в % окна) за последнее окно были активны зоны (лента жома).

Выход в ПЛК описывается полями out у зоны / группы / камеры (status_out), compute_writes() превращает состояние в список записей.
"""
from __future__ import annotations

import copy
import json
import os
import re
from collections import deque
from typing import Optional

import cv2
import numpy as np

CALIB_DT = 0.33          # интервал между кадрами, при котором калибровались пороги потока (камеры ~3 fps)
FB = dict(pyr_scale=0.5, levels=3, winsize=15, iterations=3, poly_n=5, poly_sigma=1.2, flags=0)
MIN_FLOW_SIDE = 16       # зона меньше этого (px) для Farneback не годится — поток = 0
ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")

ZONE_TYPES = ("motion", "presence", "cycle")
METRICS = ("flow", "diff")
GROUP_TYPES = ("sum", "monotonic", "worktimer", "mirror")
MIRROR_MODES = ("mirror", "same")
CYCLE_WORK_R = 170       # радиус (px), до которого сжимается область вокруг оси палки перед поиском: быстро и не зависит от разрешения камеры
CYCLE_RED_HSV = (16, 165, 50, 55)     # красный: H ≤ h_max или H ≥ h_min (круг оттенков), S ≥ s_min, V ≥ v_min; мягче прежнего: красный конец в сумерках бледнеет и желтеет
CYCLE_RED_HSV_OLD = (12, 163, 70, 80)  # прежний умолчательный красный: зоны, сохранённые с ним нетронутыми, переходят на новый (свои значения не трогаем)
CYCLE_BLUE_HSV = (82, 135, 55, 60)    # синий/голубой: h_lo ≤ H ≤ h_hi, S ≥ s_min, V ≥ v_min (голубая половина тоже сюда)
HW_ACCEL_MODES = ("off", "auto", "d3d11")    # аппаратное декодирование видео: выкл / любое доступное / Direct3D 11 (видеоядро Intel, AMD, NVIDIA)
ZONE_OUT_KINDS = ("holding", "coil")
SCENARIO_SIGNALS = ("move", "pulp")    # что сценарий берёт у зон: бит «движется» или «жом есть» (вид окна)
ZONE_OUT_VALUES = ("bit", "state")     # что зона пишет в ПЛК: бит «движется» или состояние окна 0 пусто / 1 жом стоит / 2 движется
FILL_METRICS = ("off", "tex", "lum", "rel")   # признак «вид окна» (есть ли жом): выкл / зернистость / яркость / яркость относительно металла вокруг окна
FILL_TEX_BLUR = 5                      # ядро размытия для зернистости: tex = средняя |кадр − размытый кадр| в зоне
FILL_RING_MARGIN = 1.0                 # «металл вокруг окна» для rel: полоса шириной в высоту окна вокруг него, без самого окна и его рамки
FILL_RING_GAP = 0.25                   # рамка окна (доля высоты), которая в металл не входит
GROUP_OUT_KINDS = ("holding", "bits", "coils")
AGGS = ("any", "majority", "all")

# Пороги по умолчанию зависят от метрики: поток — безразмерный (0.3..2), diff — градации серого (после размытия).
THRESH_DEFAULT = {"flow": (0.9, 0.6), "diff": (4.0, 2.0)}

DEFAULT_MODBUS = {"enable": False, "host": "192.168.1.20", "port": 502, "device_id": 1, "write_period_s": 1.0}


def default_config() -> dict:
    return {"version": 1, "modbus": dict(DEFAULT_MODBUS), "cameras": [], "scenarios": []}


# ───────────────────────────── конфиг: проверка и значения по умолчанию ─────────────────────────────
def _num(v, name, lo=None, hi=None, integer=False):
    try:
        x = int(v) if integer else float(v)
    except (TypeError, ValueError):
        raise ValueError("%s: нужно число, а пришло %r" % (name, v))
    if lo is not None and x < lo:
        raise ValueError("%s: не меньше %s" % (name, lo))
    if hi is not None and x > hi:
        raise ValueError("%s: не больше %s" % (name, hi))
    return x


def _out(o, kinds, name, allow_none=True, values=None):
    if not o or not o.get("kind"):
        return None
    kind = o.get("kind")
    if kind not in kinds:
        raise ValueError("%s: вид вывода %r не из %s" % (name, kind, list(kinds)))
    res = {"kind": kind, "address": _num(o.get("address", 0), name + ".address", 0, 65535, True)}
    if values:      # что писать: у зоны — бит или состояние окна; остальные выводы значение не выбирают
        v = o.get("value", values[0])
        if v not in values:
            raise ValueError("%s: значение вывода %r не из %s" % (name, v, list(values)))
        res["value"] = v
    return res


def _fill_cfg(z: dict, where: str) -> dict:
    """Признак «вид окна» — есть ли в окне жом. Нужен, чтобы отличить «шахта пустая и стоит» от «жом есть, но стоит»: по движению
    эти два состояния одинаковы. fill_on = 0 — пороги не заданы: признак только измеряется (в панели и записи), решения нет."""
    metric = z.get("fill_metric", "off")
    if metric not in FILL_METRICS:
        raise ValueError("%s: признак вида окна %r не из %s" % (where, metric, list(FILL_METRICS)))
    on = _num(z.get("fill_on", 0), where + ".fill_on", 0)
    off = _num(z.get("fill_off", 0), where + ".fill_off", 0)
    invert = bool(z.get("fill_invert", False))
    if on > 0 and off > on:        # как у порогов движения: on — верхний порог, off — нижний; fill_invert лишь меняет, при каком из них «жом есть»
        raise ValueError("%s: порог off вида окна (%s) больше порога on (%s)" % (where, off, on))
    return {"fill_metric": metric, "fill_on": on, "fill_off": off, "fill_invert": invert,
            "fill_smooth": _num(z.get("fill_smooth", 15), where + ".fill_smooth", 1, 600, True),
            "fill_frames": _num(z.get("fill_frames", 10), where + ".fill_frames", 1, 1000, True)}


def _norm_zone(z: dict, cam_id: str) -> dict:
    zid = str(z.get("id") or "")
    if not ID_RE.match(zid):
        raise ValueError("камера %s: id зоны %r — только латиница, цифры, _ и -" % (cam_id, zid))
    where = "зона %s/%s" % (cam_id, zid)
    ztype = z.get("type", "motion")
    if ztype not in ZONE_TYPES:
        raise ValueError("%s: тип %r не из %s" % (where, ztype, list(ZONE_TYPES)))
    if ztype == "cycle":
        return _norm_cycle(z, zid, where)
    metric = z.get("metric", "flow")
    if metric not in METRICS:
        raise ValueError("%s: метрика %r не из %s" % (where, metric, list(METRICS)))
    pts = z.get("points") or []
    if len(pts) < 3:
        raise ValueError("%s: в полигоне меньше трёх точек" % where)
    try:
        pts = [[float(p[0]), float(p[1])] for p in pts]
    except Exception:
        raise ValueError("%s: точки полигона должны быть парами [x, y]" % where)
    d_on, d_off = THRESH_DEFAULT[metric]
    on = _num(z.get("on", d_on), where + ".on", 0)
    off = _num(z.get("off", d_off), where + ".off", 0)
    if off > on:
        raise ValueError("%s: порог выключения (off=%s) больше порога включения (on=%s)" % (where, off, on))
    return {
        "id": zid,
        "name": str(z.get("name") or zid),
        "enabled": bool(z.get("enabled", True)),
        "type": ztype,
        "metric": metric,
        "points": pts,
        "on": on,
        "off": off,
        "invert": bool(z.get("invert", ztype == "presence")),
        "track_flow": bool(z.get("track_flow", True)),
        "smooth": _num(z.get("smooth", 5), where + ".smooth", 1, 100, True),
        "frames_to_on": _num(z.get("frames_to_on", 3), where + ".frames_to_on", 1, 1000, True),
        "frames_to_off": _num(z.get("frames_to_off", 4), where + ".frames_to_off", 1, 1000, True),
        "blur": _num(z.get("blur", 5), where + ".blur", 0, 31, True),
        "flicker_comp": bool(z.get("flicker_comp", True)),
        "out": _out(z.get("out"), ZONE_OUT_KINDS, where + ".out", values=ZONE_OUT_VALUES),
        **_fill_cfg(z, where),
    }


def _hsv4(v, default, name, hue_pair_ok):
    """[h1, h2, s_min, v_min] — оттенок 0..179 (шкала OpenCV), насыщенность и яркость 0..255."""
    v = list(v) if v else list(default)
    if len(v) != 4:
        raise ValueError("%s: нужно четыре числа [H1, H2, S, V]" % name)
    h1, h2 = _num(v[0], name, 0, 179, True), _num(v[1], name, 0, 179, True)
    s, val = _num(v[2], name, 0, 255, True), _num(v[3], name, 0, 255, True)
    if not hue_pair_ok and h1 > h2:
        raise ValueError("%s: первый оттенок больше второго" % name)
    return [h1, h2, s, val]


def _norm_cycle(z: dict, zid: str, where: str) -> dict:
    """Зона-«цикл»: палка из красной и синей половин на оси; центр + радиус вместо полигона. Результат — угол красного конца
    от вертикали по часовой (0..360). Полигона нет (points = []), метрик потока/разности нет."""
    c = z.get("center")
    if not c or len(c) != 2:
        raise ValueError("%s: нужен центр оси [x, y]" % where)
    center = [_num(c[0], where + ".center", 0), _num(c[1], where + ".center", 0)]
    return {
        "id": zid,
        "name": str(z.get("name") or zid),
        "enabled": bool(z.get("enabled", True)),
        "type": "cycle",
        "metric": "flow",
        "points": [],
        "on": 0.0, "off": 0.0, "invert": False, "track_flow": False,
        "smooth": _num(z.get("smooth", 3), where + ".smooth", 1, 50, True),
        "frames_to_on": 1, "frames_to_off": 1, "blur": 0, "flicker_comp": False,
        "out": _out(z.get("out"), ZONE_OUT_KINDS, where + ".out"),
        "center": center,
        "radius": _num(z.get("radius", 330), where + ".radius", 20),
        "r_min": _num(z.get("r_min", 0.1), where + ".r_min", 0.0, 0.8),
        "red_hsv": _hsv4(None if tuple(z.get("red_hsv") or ()) == CYCLE_RED_HSV_OLD else z.get("red_hsv"), CYCLE_RED_HSV, where + ".red_hsv", True),
        "blue_hsv": _hsv4(z.get("blue_hsv"), CYCLE_BLUE_HSV, where + ".blue_hsv", False),
        "min_score": _num(z.get("min_score", 0.4), where + ".min_score", 0.01, 2),
        "min_conf": _num(z.get("min_conf", 0.1), where + ".min_conf", 0, 1),
        "jump_deg": _num(z.get("jump_deg", 25), where + ".jump_deg", 3, 180),
        "coast_s": _num(z.get("coast_s", 30), where + ".coast_s", 0, 3600),
        "red_min": _num(z.get("red_min", 0.12), where + ".red_min", 0, 1),
        "blue_alone": _num(z.get("blue_alone", 0.6), where + ".blue_alone", 0, 1),
        "blue_w": _num(z.get("blue_w", 0.5), where + ".blue_w", 0, 1),
        "thin": _num(z.get("thin", 9), where + ".thin", 0, 51, True),
        "rpm_window": _num(z.get("rpm_window", 20), where + ".rpm_window", 2, 300),
        "stop_s": _num(z.get("stop_s", 10), where + ".stop_s", 2, 300),
        "stop_deg": _num(z.get("stop_deg", 5), where + ".stop_deg", 1, 45),
        "rpm_out": _out(z.get("rpm_out"), ("holding",), where + ".rpm_out"),
    }


def _norm_group(g: dict, cam_id: str, zone_ids: list, cycle_ids=()) -> dict:
    gid = str(g.get("id") or "")
    if not ID_RE.match(gid):
        raise ValueError("камера %s: id группы %r — только латиница, цифры, _ и -" % (cam_id, gid))
    where = "группа %s/%s" % (cam_id, gid)
    gtype = g.get("type", "sum")
    if gtype not in GROUP_TYPES:
        raise ValueError("%s: тип %r не из %s" % (where, gtype, list(GROUP_TYPES)))
    zs = [str(x) for x in (g.get("zones") or [])]
    bad = [x for x in zs if x not in zone_ids]
    if bad:
        raise ValueError("%s: нет таких зон: %s" % (where, ", ".join(bad)))
    if not zs:
        raise ValueError("%s: не выбрано ни одной зоны" % where)
    out = {
        "id": gid,
        "name": str(g.get("name") or gid),
        "type": gtype,
        "zones": zs,
        "out": _out(g.get("out"), GROUP_OUT_KINDS, where + ".out"),
    }
    if gtype == "mirror":
        if len(zs) != 2 or any(x not in cycle_ids for x in zs):
            raise ValueError("%s: нужны ровно две зоны типа cycle" % where)
        mode = g.get("mode", "mirror")
        if mode not in MIRROR_MODES:
            raise ValueError("%s: режим %r не из %s" % (where, mode, list(MIRROR_MODES)))
        tbl = g.get("table")
        if tbl:
            if not isinstance(tbl, (list, tuple)) or len(tbl) != MIRROR_BINS:
                raise ValueError("%s: таблица поправки — ровно %d чисел" % (where, MIRROR_BINS))
            tbl = [_num(v, where + ".table", -90, 90) for v in tbl]
        out.update(mode=mode, table=tbl or None,
                   ref=_num(g.get("ref", 0), where + ".ref", -180, 180),
                   tol=_num(g.get("tol", 10), where + ".tol", 0.5, 180),
                   learn=bool(g.get("learn", True)),
                   learn_min=_num(g.get("learn_min", 30), where + ".learn_min", 5, 240, True),
                   frames_to_on=_num(g.get("frames_to_on", 3), where + ".frames_to_on", 1, 1000, True),
                   frames_to_off=_num(g.get("frames_to_off", 5), where + ".frames_to_off", 1, 1000, True))
    if gtype == "monotonic":
        order = [str(x) for x in (g.get("order") or zs)]
        if sorted(order) != sorted(zs):
            raise ValueError("%s: order (снизу вверх) должен содержать те же зоны, что и zones" % where)
        out["order"] = order
    if gtype == "worktimer":
        agg = g.get("agg", "any")
        if agg not in AGGS:
            raise ValueError("%s: agg %r не из %s" % (where, agg, list(AGGS)))
        out.update(window_s=_num(g.get("window_s", 60), where + ".window_s", 1, 86400, True),
                   min_active=_num(g.get("min_active", 1), where + ".min_active", 1, len(zs), True),
                   agg=agg,
                   scale=_num(g.get("scale", 100), where + ".scale", 1, 65535, True))
    return out


def _norm_scenario(sc: dict, cams: list) -> dict:
    sid = str(sc.get("id") or "")
    if not ID_RE.match(sid):
        raise ValueError("сценарий %r: id — только латиница, цифры, _ и -" % sid)
    where = "сценарий %s" % sid
    rule = sc.get("rule", "or")
    if rule not in ("or", "and", "sum"):
        raise ValueError("%s: правило %r не из or/and/sum" % (where, rule))
    signal = sc.get("signal", "move")
    if signal not in SCENARIO_SIGNALS:
        raise ValueError("%s: сигнал %r не из %s" % (where, signal, "/".join(SCENARIO_SIGNALS)))
    zones = {c["id"]: {z["id"] for z in c["zones"]} for c in cams}
    groups = {c["id"]: {g["id"] for g in c["groups"] if g["type"] != "mirror"} for c in cams}
    srcs = []
    for s in sc.get("sources") or []:     # источники удалённых камер/зон/групп тихо отбрасываем: сценарий не должен ломать весь конфиг
        cam = str(s.get("camera", ""))
        if s.get("group") is not None:    # источник — группа камеры («вычисление»): берётся её значение (сумма, уровень, % наработки)
            grp = str(s.get("group"))
            if grp in groups.get(cam, ()) and not any(x["camera"] == cam and x.get("group") == grp for x in srcs):
                srcs.append({"camera": cam, "group": grp})
            continue
        zone = str(s.get("zone", ""))
        if zone in zones.get(cam, ()) and not any(x["camera"] == cam and x.get("zone") == zone for x in srcs):
            srcs.append({"camera": cam, "zone": zone})
    mb = sc.get("modbus")
    return {"id": sid, "name": str(sc.get("name") or sid), "rule": rule, "signal": signal, "sources": srcs,
            "out": _out(sc.get("out"), ZONE_OUT_KINDS, where + ".out"),
            "modbus": normalize_modbus(mb, base=None) if mb else None}


def normalize_camera(c: dict) -> dict:
    cid = str(c.get("id") or "")
    if not ID_RE.match(cid):
        raise ValueError("id камеры %r — только латиница, цифры, _ и -" % cid)
    zones = [_norm_zone(z, cid) for z in (c.get("zones") or [])]
    ids = [z["id"] for z in zones]
    if len(set(ids)) != len(ids):
        raise ValueError("камера %s: id зон повторяются" % cid)
    cycle_ids = [z["id"] for z in zones if z["type"] == "cycle"]
    groups = [_norm_group(g, cid, ids, cycle_ids) for g in (c.get("groups") or [])]
    gids = [g["id"] for g in groups]
    if len(set(gids)) != len(gids):
        raise ValueError("камера %s: id групп повторяются" % cid)
    transport = str(c.get("transport", "tcp")).lower()
    if transport not in ("tcp", "udp"):
        raise ValueError("камера %s: transport tcp или udp" % cid)
    hw = str(c.get("hw_accel", "auto")).lower()
    if hw not in HW_ACCEL_MODES:
        raise ValueError("камера %s: hw_accel — одно из %s" % (cid, ", ".join(HW_ACCEL_MODES)))
    fs = c.get("frame_size")
    if fs:
        fs = [_num(fs[0], "frame_size", 1, None, True), _num(fs[1], "frame_size", 1, None, True)]
    mb = c.get("modbus")
    if mb:
        mb = normalize_modbus(mb, base=None)
    return {
        "id": cid,
        "name": str(c.get("name") or cid),
        "enabled": bool(c.get("enabled", True)),
        "url": str(c.get("url") or ""),
        "transport": transport,
        "hw_accel": hw,
        "substream": bool(c.get("substream", False)),
        "proc_fps": _num(c.get("proc_fps", 3), "камера %s.proc_fps" % cid, 0.2, 30),
        "frame_gap": _num(c.get("frame_gap", 1), "камера %s.frame_gap" % cid, 1, 10, True),
        "reconnect_delay_s": _num(c.get("reconnect_delay_s", 3.0), "камера %s.reconnect_delay_s" % cid, 0.5, 300),
        "freeze_timeout_s": _num(c.get("freeze_timeout_s", 10), "камера %s.freeze_timeout_s" % cid, 1, 3600),
        "freeze_eps": _num(c.get("freeze_eps", 1.0), "камера %s.freeze_eps" % cid, 0),
        "corrupt_ratio": _num(c.get("corrupt_ratio", 0.3), "камера %s.corrupt_ratio" % cid, 0, 1),
        "corrupt_hold_s": _num(c.get("corrupt_hold_s", 2.0), "камера %s.corrupt_hold_s" % cid, 0, 60),
        "loop": bool(c.get("loop", True)),
        "frame_size": fs,
        "status_out": _out(c.get("status_out"), ZONE_OUT_KINDS, "камера %s.status_out" % cid),
        "modbus": mb,
        "zones": zones,
        "groups": groups,
    }


def normalize_modbus(m: Optional[dict], base: Optional[dict] = DEFAULT_MODBUS) -> dict:
    m = m or {}
    b = base or {}
    return {
        "enable": bool(m.get("enable", b.get("enable", False))),
        "host": str(m.get("host", b.get("host", "192.168.1.20"))).strip(),
        "port": _num(m.get("port", b.get("port", 502)), "modbus.port", 1, 65535, True),
        "device_id": _num(m.get("device_id", b.get("device_id", 1)), "modbus.device_id", 0, 255, True),
        "write_period_s": _num(m.get("write_period_s", b.get("write_period_s", 1.0)), "modbus.write_period_s", 0.1, 3600),
    }


def normalize_config(cfg: dict) -> dict:
    """Проверить конфиг и добавить значения по умолчанию. Ошибка → ValueError с понятным текстом (по-русски)."""
    if not isinstance(cfg, dict):
        raise ValueError("конфиг должен быть объектом")
    cams = [normalize_camera(c) for c in (cfg.get("cameras") or [])]
    ids = [c["id"] for c in cams]
    if len(set(ids)) != len(ids):
        raise ValueError("id камер повторяются")
    scs = [_norm_scenario(s, cams) for s in (cfg.get("scenarios") or [])]
    sids = [s["id"] for s in scs]
    if len(set(sids)) != len(sids):
        raise ValueError("id сценариев повторяются")
    return {"version": 1, "modbus": normalize_modbus(cfg.get("modbus")), "cameras": cams, "scenarios": scs}


# ───────────────────────────── метрики зоны ─────────────────────────────
def scale_points(points, frame_size, cur_wh):
    """Точки полигона в пикселях разметки → пиксели текущего кадра (если камера отдаёт другое разрешение)."""
    if not frame_size:
        return [[float(x), float(y)] for x, y in points]
    sx, sy = cur_wh[0] / float(frame_size[0]), cur_wh[1] / float(frame_size[1])
    return [[x * sx, y * sy] for x, y in points]


class ZoneRT:
    """Зона на конкретном кадре: полигон, bbox, маска, сглаживание и гистерезис."""

    def __init__(self, zcfg: dict, frame_size, cur_wh):
        self.cfg = zcfg
        self.id, self.name = zcfg["id"], zcfg["name"]
        W, H = cur_wh
        pts = np.round(np.array(scale_points(zcfg["points"], frame_size, cur_wh))).astype(np.int32)
        pts[:, 0] = np.clip(pts[:, 0], 0, W - 1)
        pts[:, 1] = np.clip(pts[:, 1], 0, H - 1)
        x, y, w, h = cv2.boundingRect(pts)
        w, h = max(1, w), max(1, h)
        m = np.zeros((h, w), np.uint8)
        cv2.fillPoly(m, [pts - [x, y]], 255)
        self.bbox, self.mask, self.poly = (x, y, w, h), m > 0, pts
        if not self.mask.any():
            self.mask[:] = True
        self.hist_flow = deque(maxlen=zcfg["smooth"])
        self.hist_diff = deque(maxlen=zcfg["smooth"])
        self.hys = Hysteresis(zcfg["on"], zcfg["off"], zcfg["invert"], zcfg["frames_to_on"], zcfg["frames_to_off"])
        self.flow = self.diff = self.value = 0.0
        self.fill_metric = zcfg.get("fill_metric", "off")
        self.hist_fill = deque(maxlen=zcfg.get("fill_smooth", 15))
        # решает «жом есть» только при заданных порогах (fill_on > 0), иначе признак лишь измеряется
        self.fill_hys = (Hysteresis(zcfg["fill_on"], zcfg["fill_off"], zcfg["fill_invert"], zcfg["fill_frames"], zcfg["fill_frames"])
                         if self.fill_metric != "off" and zcfg["fill_on"] > 0 else None)
        self.tex = self.lum = self.rel = 0.0    # сглаженные зернистость, яркость и яркость относительно металла (считаются, пока признак вида окна включён)
        self.ring = self._build_ring(pts, W, H) if self.fill_metric == "rel" else None
        self.fill: Optional[int] = None     # 1 — жом есть, 0 — нет, None — решения нет (признак выключен или пороги не заданы)

    def _build_ring(self, pts, W, H):
        """Металл вокруг окна: (x0, y0, x1, y1, маска) — полоса вокруг bbox без самого окна и его рамки. Яркость окна делим на медиану
        этой полосы: свет, солнце и автоэкспозиция меняют обе одинаково, а «жом / пусто» — только окно."""
        x, y, w, h = self.bbox
        m = max(8, int(round(h * FILL_RING_MARGIN)))
        x0, y0, x1, y1 = max(0, x - m), max(0, y - m), min(W, x + w + m), min(H, y + h + m)
        hole = np.zeros((y1 - y0, x1 - x0), np.uint8)
        cv2.fillPoly(hole, [pts - [x0, y0]], 255)
        g = max(3, int(round(h * FILL_RING_GAP))) * 2 + 1
        hole = cv2.dilate(hole, np.ones((g, g), np.uint8))
        ring = hole == 0
        return (x0, y0, x1, y1, ring) if ring.sum() >= 50 else None

    def reset(self):
        self.hist_flow.clear()
        self.hist_diff.clear()
        self.hist_fill.clear()
        self.hys.reset()
        if self.fill_hys:
            self.fill_hys.reset()
        self.flow = self.diff = self.value = 0.0
        self.tex = self.lum = self.rel = 0.0
        self.fill = None

    def step(self, prev: np.ndarray, cur: np.ndarray, dt: float) -> int:
        x, y, w, h = self.bbox
        a, b = prev[y:y + h, x:x + w], cur[y:y + h, x:x + w]
        raw_diff = diff_metric(a, b, self.mask, self.cfg["blur"], self.cfg["flicker_comp"])
        self.hist_diff.append(raw_diff)
        # оптический поток — самое дорогое в анализе (на крупной зоне больше половины процессора): если зона решает по diff и flow
        # для неё не нужен (track_flow выключен), не считаем; в графиках и записях flow такой зоны равен 0
        if self.cfg["metric"] == "flow" or self.cfg.get("track_flow", True):
            self.hist_flow.append(flow_metric(a, b, self.mask, dt))
            self.flow = sum(self.hist_flow) / len(self.hist_flow)
        else:
            self.flow = 0.0
        self.diff = sum(self.hist_diff) / len(self.hist_diff)
        self.value = self.flow if self.cfg["metric"] == "flow" else self.diff
        if self.fill_metric != "off":
            self._fill_step(b, cur)
        return self.hys.update(self.value)

    def _fill_step(self, cur: np.ndarray, frame: np.ndarray):
        """Вид окна: зернистость, яркость и яркость относительно металла вокруг (rel), сглаженные за fill_smooth кадров.
        Жом в окне светлый волокнистый, пустое окно тёмное зернистое: rel у жома выше (~0,9 против ~0,65)."""
        m = self.mask
        k = FILL_TEX_BLUR if FILL_TEX_BLUR % 2 else FILL_TEX_BLUR + 1
        tex = float(np.abs(cur.astype(np.float32) - cv2.GaussianBlur(cur, (k, k), 0).astype(np.float32))[m].mean())
        lum = float(cur[m].mean())
        rel = 0.0
        if self.ring is not None:
            x0, y0, x1, y1, rm = self.ring
            rel = lum / max(1.0, float(np.median(frame[y0:y1, x0:x1][rm])))
        self.hist_fill.append((tex, lum, rel))
        n = len(self.hist_fill)
        self.tex = sum(v[0] for v in self.hist_fill) / n
        self.lum = sum(v[1] for v in self.hist_fill) / n
        self.rel = sum(v[2] for v in self.hist_fill) / n
        if self.fill_hys:
            self.fill = self.fill_hys.update({"tex": self.tex, "lum": self.lum, "rel": self.rel}[self.fill_metric])


MIRROR_BINS = 36         # таблица поправки зеркальности: 36 ячеек по 10° по углу первой палки


def mirror_corr(table, theta: float) -> float:
    """Поправка (°) для угла первой палки theta: линейная интерполяция по кругу между ячейками таблицы (центры 0, 10, … 350°). Нет таблицы — 0."""
    if not table:
        return 0.0
    x = (theta % 360.0) / (360.0 / MIRROR_BINS)
    i = int(x) % MIRROR_BINS
    f = x - int(x)
    return float(table[i] + (table[(i + 1) % MIRROR_BINS] - table[i]) * f)


def build_mirror_table(samples, min_bins: int = 28, min_n: int = 60):
    """samples [(θ1, сумма углов θ1±θ2)] по кадрам при ровных валах → (таблица из 36 чисел, сведения).
    Сумма углов приводится к −180..180 (норма ≈ 0); в каждой ячейке медиана, пустые заполняются по кругу линейно. Мало точек, не весь круг
    пройден или разброс слишком велик (валы, похоже, не синхронны) — ValueError с понятным текстом: плохая таблица запомнит чужой перекос как норму."""
    pts = [(float(a) % 360.0, ang_diff(float(r), 0.0)) for a, r in samples]
    if len(pts) < min_n:
        raise ValueError("мало данных: %d кадров, нужно не меньше %d. Дайте валам сделать полный оборот" % (len(pts), min_n))
    bins: list = [[] for _ in range(MIRROR_BINS)]
    for a, r in pts:
        bins[int(round(a / (360.0 / MIRROR_BINS))) % MIRROR_BINS].append(r)
    have = [i for i, b in enumerate(bins) if b]
    if len(have) < min_bins:
        raise ValueError("палка прошла не весь круг: заполнено %d ячеек из %d, нужно не меньше %d. Дождитесь полного оборота" % (len(have), MIRROR_BINS, min_bins))
    xs = np.array(have, dtype=float)
    ys = np.array([float(np.median(bins[i])) for i in have])
    table = np.interp(np.arange(MIRROR_BINS), xs, ys, period=MIRROR_BINS)
    spread = float(np.ptp(table))
    if spread > 60.0:
        raise ValueError("разброс %.0f° слишком велик — валы, похоже, не синхронны; таблицу в таком состоянии снимать нельзя" % spread)
    return [round(float(v), 1) for v in table], {"n": len(pts), "bins": len(have), "spread": round(spread, 1)}


LEARN_STEP_S = 10.0       # как часто пересчитывать выученную форму
LEARN_ALPHA = 0.25        # какую долю пути к новой форме проходим за один пересчёт: форма плывёт плавно, поправка не прыгает
LEARN_AMP_MAX = 25.0      # размах выученной формы по модулю, °: больше — это уже не перекос камеры
LEARN_GATE = 60.0         # кадр дальше стольких градусов от ожидаемого в обучение не берём (ошибочный захват палки)
LEARN_MIN_BINS = 28       # столько ячеек из 36 должно быть заполнено, чтобы форма считалась
LEARN_MIN_PER_BIN = 3     # ... и по стольку кадров в каждой
LEARN_PER_BIN_MAX = 600   # потолок кадров в ячейке (память)


class MirrorLearn:
    """Самоподстройка поправки группы mirror: выучивает, как Δ зависит от угла левой палки (перекос камеры), по скользящему окну.

    В ячейку по углу кладётся остаток r = Δ до выученной формы (после ручной таблицы и эталона). Форма = медианы ячеек минус их среднее:
    постоянный сдвиг валов и медленный дрейф в форму НЕ уходят, остаются в Δ и дают тревогу; учится только то, что повторяется каждый оборот.
    Окно скользит: свет и положение камеры меняются — форма подстраивается. shape — 36 чисел по 10°, как у table."""

    def __init__(self, window_s: float, shape=None):
        self.window_s = float(window_s)
        self.bins = [deque(maxlen=LEARN_PER_BIN_MAX) for _ in range(MIRROR_BINS)]   # ячейка → (время, остаток)
        ok = isinstance(shape, (list, tuple)) and len(shape) == MIRROR_BINS
        self.shape = [float(v) for v in shape] if ok else [0.0] * MIRROR_BINS
        self.fresh = not ok                 # формы ещё не было: первую считаем целиком, без плавного подхода
        self.level = 0.0                    # где сейчас среднее остатка (≈ постоянный сдвиг валов), °
        self.filled = 0                     # заполненных ячеек в окне
        self.ready = False
        self.dirty = False                  # форма изменилась с последнего сохранения
        self._t_step = 0.0

    def corr(self, theta: float) -> float:
        return mirror_corr(self.shape, theta)

    def add(self, now: float, theta: float, r: float) -> None:
        """r — отклонение θ1 ± θ2 от нормы ДО выученной формы (после ручной таблицы и эталона), °, −180..180. Форму учим целиком, не добавку к ней."""
        gate = LEARN_GATE if (self.ready or not self.fresh) else 90.0
        if abs(ang_diff(r, self.level + self.corr(theta))) > gate:
            return                                          # ошибочный захват палки: в форму не берём
        self.bins[int(round((theta % 360.0) / (360.0 / MIRROR_BINS))) % MIRROR_BINS].append((float(now), float(r)))

    def step(self, now: float) -> None:
        """Раз в LEARN_STEP_S: выкинуть старое, пересчитать форму."""
        if now - self._t_step < LEARN_STEP_S:
            return
        self._t_step = now
        edge = now - self.window_s
        meds = []
        for i, d in enumerate(self.bins):
            while d and d[0][0] < edge:
                d.popleft()
            meds.append(float(np.median([v for _, v in d])) if len(d) >= LEARN_MIN_PER_BIN else None)
        have = [i for i, m in enumerate(meds) if m is not None]
        self.filled = len(have)
        self.ready = self.filled >= LEARN_MIN_BINS
        if not self.ready:
            return
        ys = np.array([meds[i] for i in have])
        self.level = float(ys.mean())
        target = np.interp(np.arange(MIRROR_BINS), np.array(have, dtype=float), ys - self.level, period=MIRROR_BINS)
        target = np.clip(target, -LEARN_AMP_MAX, LEARN_AMP_MAX)
        old = np.array(self.shape)
        new = target if self.fresh else old + LEARN_ALPHA * (target - old)
        self.fresh = False
        self.shape = [float(v) for v in new]
        self.dirty = True

    def amp(self) -> float:
        return float(max(self.shape) - min(self.shape))

    def info(self) -> dict:
        return {"ready": bool(self.ready), "bins": int(self.filled), "amp": round(self.amp(), 1), "window_min": round(self.window_s / 60.0)}


PHIS = np.arange(0, 360, 1.0)
_PHI_SIN, _PHI_COS = np.sin(np.radians(PHIS))[:, None], np.cos(np.radians(PHIS))[:, None]
_K3 = np.ones((3, 3), np.uint8)


def circ_mean(deg) -> float:
    """Среднее углов в градусах по кругу (359 и 1 → 0, а не 180)."""
    r = np.radians(np.asarray(deg, dtype=np.float64))
    return float(np.degrees(np.arctan2(np.sin(r).mean(), np.cos(r).mean())) % 360.0)


def ang_diff(a: float, b: float) -> float:
    """a − b, приведённое к −180..180."""
    return (a - b + 180.0) % 360.0 - 180.0


class CycleRT:
    """Палка на оси: красная половина в одну сторону, синяя в противоположную. Угол красного конца — от вертикали по часовой, 0..360.

    Ищем только по цвету вдоль лучей от центра оси (фон по медиане не годится: колёса за палкой тоже вращаются): для каждого угла φ
    считаем долю «красных» точек на луче φ плюс blue_w · долю «синих» на луче φ+180, берём лучший φ. Синий в цехе почти везде
    (тени, фон), поэтому он только подспорье: палку принимаем, если красного на луче не меньше red_min либо синий тонкой полосой
    почти на всю длину (blue_alone — красный конец бывает блеклым). Цветные пятна (кожух, тени) отсекаем:
    считаем только тонкие цветные полоски (thin — ширина «окна открытия» в пикселях, 0 — не отсекать).
    Уверенность — насколько лучший φ лучше самой сильной альтернативы дальше 20° от него. Найдя палку, дальше ищем рядом с
    ожидаемым углом (прошлый + скорость·время, окно ±jump_deg, при пропаже расширяется на 1°/с): одиночные ложные пики не уводят угол.
    Палку не видно (переход по тёмному/зелёному, блик) — до coast_s секунд держим ожидаемый угол и скорость (coast), а не прыгаем
    на ближайшую кромку; позже coast_s — палка потеряна, снова ищем по всему кругу."""

    def __init__(self, zcfg: dict, frame_size, cur_wh):
        self.cfg = zcfg
        self.id, self.name = zcfg["id"], zcfg["name"]
        W, H = cur_wh
        sx = W / float(frame_size[0]) if frame_size else 1.0
        sy = H / float(frame_size[1]) if frame_size else 1.0
        cx, cy = zcfg["center"][0] * sx, zcfg["center"][1] * sy
        R = max(8.0, zcfg["radius"] * (sx + sy) / 2.0)
        self.x0, self.y0 = max(0, int(cx - R)), max(0, int(cy - R))
        self.x1, self.y1 = min(W, int(cx + R) + 1), min(H, int(cy + R) + 1)
        self.k = min(1.0, CYCLE_WORK_R / R)                          # сжатие рабочей области
        self.ox, self.oy = (cx - self.x0) * self.k, (cy - self.y0) * self.k
        r1 = R * self.k
        self.rs = np.arange(max(3.0, r1 * zcfg["r_min"]), r1, 1.0)
        self.xs = np.clip(np.rint(self.ox + self.rs[None, :] * _PHI_SIN).astype(int), 0, max(0, int((self.x1 - self.x0) * self.k) - 1))
        self.ys = np.clip(np.rint(self.oy - self.rs[None, :] * _PHI_COS).astype(int), 0, max(0, int((self.y1 - self.y0) * self.k) - 1))
        self.ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (zcfg["thin"], zcfg["thin"])) if zcfg["thin"] >= 3 else None
        self.raw: deque = deque(maxlen=zcfg["smooth"])
        self.trk: deque = deque()               # (время, «развёрнутый» угол без скачков через 360°) за последние rpm_window секунд — для оборотов
        self.reset()

    def reset(self):
        self.raw.clear()
        self.trk.clear()
        self.unw: Optional[float] = None
        self.last: Optional[float] = None       # последний принятый угол (до сглаживания) — вокруг него ищем дальше
        self.miss = 0                           # сколько кадров подряд палку не нашли
        self.last_t = 0.0                       # время последнего принятого угла
        self.mv: deque = deque()                # (время, развёрнутый угол) за последние stop_s секунд — для «крутится / стоит»
        self.moving: Optional[bool] = None      # вал крутится (True) / стоит (False); None — пока неизвестно
        self.hint_stop = False                  # палки не видно, а парная стоит: ожидаемый угол не двигаем
        self.coast = False                      # палки не видно, угол — ожидаемый по скорости (valid при этом сохраняется до coast_s)
        self.angle = 0.0
        self.conf = 0.0
        self.score = 0.0
        self.rpm = 0.0                          # обороты, об/мин: «+» по часовой стрелке, «−» против; 0 — стоит или пока мало данных
        self.valid = False

    def _track(self, now: float):
        """Угол принят: наращиваем непрерывный угол и пересчитываем обороты наклоном прямой по точкам последних rpm_window секунд.
        Наклон по многим точкам, а не разность соседних кадров: шум угла в ±1° не превращается в ложные обороты."""
        self.unw = self.angle if self.unw is None else self.unw + ang_diff(self.angle, self.unw % 360.0)
        self.trk.append((now, self.unw))
        win = self.cfg["rpm_window"]
        while self.trk and now - self.trk[0][0] > win:
            self.trk.popleft()
        if len(self.trk) < 5 or self.trk[-1][0] - self.trk[0][0] < 0.4 * win:
            self.rpm = 0.0
            return
        t = np.array([p[0] for p in self.trk])
        a = np.array([p[1] for p in self.trk])
        self.rpm = float(np.polyfit(t - t[0], a, 1)[0]) / 6.0              # град/с → об/мин

    def _lost(self):
        self.trk.clear()
        self.unw = None
        self.rpm = 0.0
        self.mv.clear()
        self.moving = None
        self.hint_stop = False

    def _motion(self, now: float):
        """Крутится или стоит: за stop_s секунд угол сдвинулся меньше stop_deg — стоит; больше 1,6·stop_deg — крутится; между — как было.
        Не по оборотам (там окно 20 с и они отстают), а по самому сдвигу угла: ход вала ≈ 4,7°/с, за 10 с это ~47°, а шум угла — градус."""
        self.mv.append((now, self.unw))
        while self.mv and now - self.mv[0][0] > self.cfg["stop_s"]:
            self.mv.popleft()
        if now - self.mv[0][0] < 0.8 * self.cfg["stop_s"]:
            return
        a = [p[1] for p in self.mv]
        rng = max(a) - min(a)
        if rng < self.cfg["stop_deg"]:
            self.moving = False
        elif rng >= 1.6 * self.cfg["stop_deg"]:
            self.moving = True

    def _liks(self, bgr: np.ndarray):
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        h, s, v = [c.astype(np.int32) for c in cv2.split(hsv)]
        hm, hn, rs_, rv = self.cfg["red_hsv"]
        red = (((h <= hm) | (h >= hn)) & (s >= rs_) & (v >= rv)).astype(np.uint8)
        b1, b2, bs, bv = self.cfg["blue_hsv"]
        blue = ((h >= b1) & (h <= b2) & (s >= bs) & (v >= bv)).astype(np.uint8)
        if self.ker is not None:                                           # оставляем только тонкое: то, что не пережило «открытие»
            red &= 1 - cv2.morphologyEx(red, cv2.MORPH_OPEN, self.ker)
            blue &= 1 - cv2.morphologyEx(blue, cv2.MORPH_OPEN, self.ker)
        return cv2.dilate(red.astype(np.float32), _K3), cv2.dilate(blue.astype(np.float32), _K3)

    def _expected(self, now: float) -> float:
        """Где палка должна быть сейчас: прошлый угол + скорость × время (скорость — по последним оборотам, не больше 30°/с)."""
        rate = 0.0 if self.hint_stop else max(-30.0, min(30.0, self.rpm * 6.0))
        return (self.last + rate * max(0.0, now - self.last_t)) % 360.0

    def step(self, frame: Optional[np.ndarray], now: float = 0.0) -> bool:
        """Новый цветной кадр (now — его время, с) → угол и обороты. True, если палка найдена на этом кадре."""
        c = self.cfg
        found = False
        dt = now - self.last_t
        locked = self.last is not None and dt <= c["coast_s"]
        if frame is not None and frame.ndim == 3:
            crop = frame[self.y0:self.y1, self.x0:self.x1]
            if self.k < 1.0:
                crop = cv2.resize(crop, None, fx=self.k, fy=self.k, interpolation=cv2.INTER_AREA)
            red, blue = self._liks(crop)
            sr, sb = red[self.ys, self.xs].mean(axis=1), blue[self.ys, self.xs].mean(axis=1)
            comb = sr + c["blue_w"] * np.roll(sb, -180)                    # красный на φ, синий на φ+180
            cand = comb
            if locked:
                hw = min(90.0, c["jump_deg"] + max(0.0, dt - 2.0))         # при пропаже окно поиска понемногу растёт: 1°/с
                near = np.abs(((PHIS - self._expected(now) + 180.0) % 360.0) - 180.0) <= hw
                cand = np.where(near, comb, -1.0)
            i = int(np.argmax(cand))
            best = float(comb[i])
            far = np.abs(((PHIS - PHIS[i] + 180.0) % 360.0) - 180.0) > 20.0
            second = float(comb[far].max()) if far.any() else 0.0
            conf = 1.0 - second / best if best > 1e-6 else 0.0
            self.score, self.conf = best, max(0.0, conf)
            if cand[i] >= 0 and best >= c["min_score"] and (sr[i] >= c["red_min"] or sb[(i + 180) % 360] >= c["blue_alone"]) and (locked or conf >= c["min_conf"]):
                idx = (i + np.arange(-4, 5)) % 360                          # уточнение до долей градуса: центр тяжести вокруг пика
                w = np.clip(comb[idx] - 0.5 * best, 0.0, None)
                r = np.radians(PHIS[idx])
                ang = float(np.degrees(np.arctan2((w * np.sin(r)).sum(), (w * np.cos(r)).sum())) % 360.0) if w.sum() > 0 else float(PHIS[i])
                if self.coast:
                    self.mv.clear()                                          # скачок при возврате палки — не ход вала
                    self.raw.clear()                                         # сглаживание не тянет к ожидаемому углу из пропажи
                self.last, self.last_t = ang, now
                self.raw.append(ang)
                self.angle = circ_mean(list(self.raw))
                self.miss = 0
                self.valid, self.coast = True, False
                found = True
                self._track(now)
                self._motion(now)
        if not found:
            self.miss += 1
            if locked:                                                       # не видно, но недавно была: держим ожидаемый угол и скорость
                self.angle = self._expected(now)
                self.coast, self.valid = True, True
                self.conf = 0.0
            else:
                self.valid, self.coast = False, False
                self.last = None
                self.raw.clear()
                self._lost()
        return found


def flow_metric(a: np.ndarray, b: np.ndarray, mask: np.ndarray, dt: float) -> float:
    """Средняя величина оптического потока в полигоне, приведённая к калибровочному dt."""
    if a.shape[0] < MIN_FLOW_SIDE or a.shape[1] < MIN_FLOW_SIDE:
        return 0.0
    fl = cv2.calcOpticalFlowFarneback(a, b, None, **FB)
    mag = np.sqrt(fl[..., 0] ** 2 + fl[..., 1] ** 2)
    raw = float(mag[mask].mean())
    return raw * (CALIB_DT / dt) if dt > 1e-3 else raw


def diff_metric(a: np.ndarray, b: np.ndarray, mask: np.ndarray, blur: int, comp: bool) -> float:
    """Средняя |b − a| в полигоне (уровни серого). comp: вычесть общий сдвиг яркости зоны — мерцание ламп и
    автоэкспозиция меняют ВСЕ пиксели одинаково и «движением» не считаются."""
    if blur and blur > 1:
        k = blur if blur % 2 else blur + 1
        a, b = cv2.GaussianBlur(a, (k, k), 0), cv2.GaussianBlur(b, (k, k), 0)
    d = b.astype(np.float32) - a.astype(np.float32)
    if comp:
        d -= float(d[mask].mean())
    return float(np.abs(d[mask]).mean())


class Hysteresis:
    """Порог с гистерезисом + debounce. invert=False: активна при ВЫСОКОЙ метрике; invert=True: при НИЗКОЙ
    (включение при value <= off, выключение при value >= on — как flow_invert в belt_detector)."""

    def __init__(self, on, off, invert=False, frames_to_on=3, frames_to_off=4):
        self.on, self.off, self.invert = on, off, invert
        self.n_on, self.n_off = frames_to_on, frames_to_off
        self.state = 0
        self.cnt = 0

    def reset(self):
        self.state = 0
        self.cnt = 0

    def update(self, v: float) -> int:
        if not self.invert:
            act, deact = v >= self.on, v <= self.off
        else:
            act, deact = v <= self.off, v >= self.on
        if self.state == 0:
            self.cnt = self.cnt + 1 if act else 0
            if self.cnt >= self.n_on:
                self.state, self.cnt = 1, 0
        else:
            self.cnt = self.cnt + 1 if deact else 0
            if self.cnt >= self.n_off:
                self.state, self.cnt = 0, 0
        return self.state


class WorkTimer:
    """Наработка: сколько секунд из последнего окна зона была активна (посекундная сетка). agg решает, считать ли
    секунду рабочей по кадрам внутри неё: any — хватает одного кадра, majority — не меньше половины, all — все."""

    def __init__(self, window: int, agg: str = "any"):
        self.window = max(1, int(window))
        self.slots = deque(maxlen=self.window)
        self.cur_sec = None
        self.act = self.tot = 0
        self.agg = agg if agg in AGGS else "any"

    def _worked(self, act, tot):
        if tot == 0:
            return False
        if self.agg == "all":
            return act == tot
        if self.agg == "majority":
            return act * 2 >= tot
        return act > 0

    def reset(self):
        self.slots.clear()
        self.cur_sec = None
        self.act = self.tot = 0

    def update(self, now: float, active: bool) -> int:
        sec = int(now)
        if self.cur_sec is None:
            self.cur_sec = sec
            self.act = self.tot = 0
        elif sec != self.cur_sec:
            self.slots.append(self._worked(self.act, self.tot))
            for _ in range(min(sec - self.cur_sec - 1, self.window)):
                self.slots.append(False)
            self.cur_sec = sec
            self.act = self.tot = 0
        self.tot += 1
        if active:
            self.act += 1
        total = sum(1 for s in self.slots if s) + (1 if self._worked(self.act, self.tot) else 0)
        return min(self.window, total)


def monotonic_level(bits_by_id: dict, order: list) -> tuple[dict, int]:
    """Заполнение снизу вверх: уровень = самая верхняя сработавшая зона из order (снизу вверх); всё ниже
    считается заполненным (устойчиво к слабому сигналу нижних окон). Возвращает (биты после коррекции, уровень)."""
    level = 0
    for pos, zid in enumerate(order, 1):
        if bits_by_id.get(zid):
            level = max(level, pos)
    fixed = dict(bits_by_id)
    for pos, zid in enumerate(order, 1):
        fixed[zid] = 1 if pos <= level else 0
    return fixed, level


# ───────────────────────────── движок камеры ─────────────────────────────
class CameraEngine:
    """Один поток кадров → состояния зон/групп. Потокобезопасность — на вызывающем (один поток на камеру)."""

    def __init__(self, cam_cfg: dict, learn_path=None):
        self.cfg = cam_cfg
        self.gap = cam_cfg["frame_gap"]
        self.proc_interval = 1.0 / cam_cfg["proc_fps"]
        self.buf: deque = deque(maxlen=self.gap + 1)
        self.zones: Optional[list[ZoneRT]] = None
        self.frame_wh: Optional[tuple] = None
        self.timers = {g["id"]: WorkTimer(g["window_s"], g["agg"]) for g in cam_cfg["groups"] if g["type"] == "worktimer"}
        self.mirrors = {g["id"]: Hysteresis(g["tol"], g["tol"] * 0.7, False, g["frames_to_on"], g["frames_to_off"])
                        for g in cam_cfg["groups"] if g["type"] == "mirror"}
        self.cycles: dict = {}                  # id зоны-цикла → CycleRT (строятся вместе с зонами, на первом кадре)
        self.last_state: Optional[dict] = None
        # самоподстройка поправки синхронности: выученная форма переживает перезапуск (камера пересоздаётся при любой правке настроек)
        self.learn_path = learn_path
        self._learn_saved = 0.0
        saved = self._learn_load()
        # выученная форма — добавка к ручной таблице; сменили таблицу — прежняя форма к ней уже не подходит, учим заново
        self.learners = {g["id"]: MirrorLearn(g["learn_min"] * 60.0,
                                              saved.get(g["id"], {}).get("shape") if saved.get(g["id"], {}).get("table") == g["table"] else None)
                         for g in cam_cfg["groups"] if g["type"] == "mirror" and g["learn"]}

    def _learn_load(self) -> dict:
        if not self.learn_path:
            return {}
        try:
            with open(self.learn_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _learn_save(self, now: float, force: bool = False) -> None:
        """Раз в минуту, если форма менялась: data\\mirror_learn_<камера>.json. Ошибка записи не должна мешать анализу кадров."""
        if not self.learn_path or not self.learners or (not force and now - self._learn_saved < 60.0):
            return
        self._learn_saved = now
        if not any(m.dirty for m in self.learners.values()):
            return
        try:
            tables = {g["id"]: g["table"] for g in self.cfg["groups"]}
            data = {gid: {"shape": [round(v, 2) for v in m.shape], "table": tables.get(gid)} for gid, m in self.learners.items()}
            tmp = str(self.learn_path) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.learn_path)
            for m in self.learners.values():
                m.dirty = False
        except OSError:
            pass

    def reset(self):
        """После потери камеры/переподключения: буфер кадров и состояния — с нуля."""
        self.buf.clear()
        for z in self.zones or []:
            z.reset()
        for c in self.cycles.values():
            c.reset()
        for t in self.timers.values():
            t.reset()
        for m in self.mirrors.values():
            m.reset()

    def _build(self, gray):
        h, w = gray.shape[:2]
        self.frame_wh = (w, h)
        self.zones = [ZoneRT(z, self.cfg["frame_size"], (w, h)) for z in self.cfg["zones"] if z["enabled"] and z["type"] != "cycle"]
        self.cycles = {z["id"]: CycleRT(z, self.cfg["frame_size"], (w, h)) for z in self.cfg["zones"] if z["enabled"] and z["type"] == "cycle"}
        for m in self.mirrors.values():
            m.reset()

    def process(self, gray: np.ndarray, now: float, frame: Optional[np.ndarray] = None) -> Optional[dict]:
        """Следующий (уже отобранный по proc_fps) серый кадр → состояние. None, пока копится буфер для FRAME_GAP.
        frame — тот же кадр в цвете: нужен зонам-циклам (цвет палки); без него (серый поток) они «палку не видят»."""
        if self.zones is None or self.frame_wh != (gray.shape[1], gray.shape[0]):
            self._build(gray)
            self.buf.clear()
        self.buf.append(gray)
        if len(self.buf) <= self.gap:
            return None
        prev = self.buf[0]
        dt = self.gap * self.proc_interval
        bits = {z.id: z.step(prev, gray, dt) for z in self.zones}
        for c in self.cycles.values():
            bits[c.id] = int(c.step(frame, now))
        self._link_partners()
        self.last_state = self._state(bits, now, alive=True)
        self._learn_save(now)
        return self.last_state

    def _link_partners(self) -> None:
        """Палки не видно, а парная в кадре: валы зеркальны, значит «крутится / стоит» у них общее. Парная стоит — ожидаемый угол не двигаем
        (иначе при остановке в слепом секторе угол «уезжал» бы по прежней скорости)."""
        for g in self.cfg["groups"]:
            if g["type"] != "mirror":
                continue
            a, b = (self.cycles.get(zid) for zid in g["zones"])
            if not a or not b:
                continue
            for x, y in ((a, b), (b, a)):
                if x.coast and y.valid and not y.coast and y.moving is not None:
                    x.moving = y.moving
                    x.hint_stop = y.moving is False
                else:
                    x.hint_stop = False

    def dead_state(self, now: float, reason: str = "") -> dict:
        """Камеры нет/кадр застыл: биты и уровни = 0, status = 0 (в ПЛК уходят нули и «камера не жива»)."""
        if self.zones is None:
            self.zones = []
        self.last_state = self._state({z.id: 0 for z in self.zones}, now, alive=False, reason=reason, update_timers=False)
        return self.last_state

    def _state(self, bits: dict, now: float, alive: bool, reason: str = "", update_timers: bool = True) -> dict:
        zs = {z.id: z for z in self.zones}
        zones = []
        for zc in self.cfg["zones"]:
            if zc["type"] == "cycle":
                zones.append(self._cycle_zone(zc, alive))
                continue
            z = zs.get(zc["id"])
            zones.append({"id": zc["id"], "name": zc["name"], "type": zc["type"], "metric": zc["metric"],
                          "enabled": zc["enabled"], "bit": int(bits.get(zc["id"], 0)) if alive else 0,
                          "flow": round(z.flow, 4) if z else 0.0, "diff": round(z.diff, 4) if z else 0.0,
                          "value": round(z.value, 4) if z else 0.0, "on": zc["on"], "off": zc["off"],
                          **self._fill_state(zc, z, alive, bool(bits.get(zc["id"], 0)))})
        groups = []
        for g in self.cfg["groups"]:
            gb = {zid: (bits.get(zid, 0) if alive else 0) for zid in g["zones"]}
            if g["type"] == "mirror":
                groups.append(self._mirror_group(g, alive, now))
                continue
            if g["type"] == "monotonic":
                gb, value = monotonic_level(gb, g["order"])
            elif g["type"] == "sum":
                value = sum(gb.values())
            else:
                active = sum(gb.values()) >= g["min_active"]
                if alive and update_timers:
                    sec = self.timers[g["id"]].update(now, active)
                else:
                    sec = 0
                value = max(0, min(g["scale"], round(sec / g["window_s"] * g["scale"])))
            groups.append({"id": g["id"], "name": g["name"], "type": g["type"], "value": int(value),
                           "bits": [int(gb[zid]) for zid in g["zones"]]})
        return {"ts": now, "alive": alive, "reason": reason, "frame_wh": list(self.frame_wh) if self.frame_wh else None,
                "zones": zones, "groups": groups}

    def _cycle_zone(self, zc: dict, alive: bool) -> dict:
        """Зона-цикл → запись состояния. angle — градусы 0..360 красного конца от вертикали по часовой; bit — палка найдена.
        flow = angle, diff = conf: так угол и уверенность попадают в истории, тренды и CSV, которые знают только flow/diff/bit."""
        c = self.cycles.get(zc["id"])
        ok = bool(alive and c and c.valid)
        coast = bool(ok and c.coast)
        angle = round(c.angle, 2) if ok else 0.0
        conf = round(c.conf, 3) if ok else 0.0
        moving = c.moving if ok else None
        rpm = round(c.rpm, 3) if ok and moving is not False else 0.0         # стоит — обороты 0 сразу, не ждём окно 20 с
        return {"id": zc["id"], "name": zc["name"], "type": "cycle", "metric": "flow", "enabled": zc["enabled"],
                "bit": int(ok and not coast), "flow": angle, "diff": conf, "value": angle, "on": 0.0, "off": 0.0,
                "angle": angle, "conf": conf, "rpm": rpm, "score": round(c.score, 3) if c else 0.0, "valid": ok, "coast": coast, "moving": moving}

    def _mirror_group(self, g: dict, alive: bool, now: float = 0.0) -> dict:
        """Две палки → рассинхрон. mirror: углы зеркальны (θ1 + θ2 = 360 + ref), same: одинаковы (θ1 − θ2 = ref). delta — отклонение
        в градусах −180..180; alarm — |delta| ≥ tol (гистерезис и debounce). Не видно хоть одной палки или камеры нет — delta не считается,
        alarm = 0 (нечего судить), value = 0. Пока хоть одна палка в удержании (её не видно, угол достроен по скорости — hold), delta
        показывается, но тревогу не меняем: ни включить, ни выключить, ни накопить счёт. Достроенный угол уходит от настоящего на
        единицы градусов — на таких отрезках на заводе набирались ложные тревоги."""
        ca, cb = (self.cycles.get(zid) for zid in g["zones"])
        ok = bool(alive and ca and cb and ca.valid and cb.valid)
        hys = self.mirrors[g["id"]]
        delta = None
        corr = 0.0
        lrn = self.learners.get(g["id"])
        if ok:
            raw = ca.angle + cb.angle if g["mode"] == "mirror" else ca.angle - cb.angle
            hold = ca.coast or cb.coast
            corr_man = mirror_corr(g["table"], ca.angle)     # перекос от угла камеры/объектива в этом положении палки, снятый при калибровке
            if lrn:
                # самоподстройка: учим только по честным кадрам (обе палки видны, вал крутится), а поправка — ручная таблица + выученная форма
                if not hold and ca.moving is not False and cb.moving is not False:
                    lrn.add(now, ca.angle, ang_diff(raw, corr_man + g["ref"]))
                lrn.step(now)
            corr = corr_man + (lrn.corr(ca.angle) if lrn else 0.0)
            delta = round(ang_diff(raw, corr + g["ref"]), 2)
            alarm = hys.state if hold else hys.update(abs(delta))
        else:
            hys.reset()
            alarm = 0
            hold = False
        return {"id": g["id"], "name": g["name"], "type": "mirror", "value": int(round(delta * 10)) if ok else 0,
                "bits": [int(alarm)], "delta": delta, "valid": ok, "hold": bool(hold), "alarm": int(alarm), "tol": g["tol"], "ref": g["ref"], "mode": g["mode"],
                "corr": round(corr, 2), "table": bool(g["table"]),
                "learn": lrn.info() if lrn else None, "shape": [round(v, 2) for v in lrn.shape] if lrn else None,
                "angles": [round(ca.angle, 2) if ca and ca.valid else None, round(cb.angle, 2) if cb and cb.valid else None]}

    @staticmethod
    def _fill_state(zc: dict, z: Optional[ZoneRT], alive: bool, moving: bool) -> dict:
        """Поля вида окна для состояния зоны. state: 2 — движется, 1 — жом есть и стоит, 0 — пусто и стоит; None — решения нет."""
        if zc.get("fill_metric", "off") == "off":
            return {}
        fill = z.fill if (z and alive) else None
        state = None if fill is None else (2 if moving else fill)
        if not alive:
            state = 0
        return {"fill_metric": zc["fill_metric"], "tex": round(z.tex, 3) if z else 0.0, "lum": round(z.lum, 2) if z else 0.0,
                "rel": round(z.rel, 3) if z else 0.0,
                "fill": fill, "state": state}

    def compute_writes(self, state: dict) -> list:
        """Состояние → записи в ПЛК: [(kind, address, value)], kind: holding | coil. Порядок стабильный.
        Зона-цикл в holding пишет угол в 0,1° (0..3599), в coil — «палка найдена»; группа mirror в holding — рассинхрон в 0,1°
        знаковым int16 (дополнительный код: −5,0° → 65486)."""
        out = []
        zb = {z["id"]: z["bit"] for z in state["zones"]}
        za = {z["id"]: z for z in state["zones"]}
        zs = {z["id"]: z.get("state") for z in state["zones"]}
        for zc in self.cfg["zones"]:
            o = zc["out"]
            if o and zc["enabled"]:
                if zc["type"] == "cycle" and o["kind"] == "holding":
                    val = int(round(za[zc["id"]]["angle"] * 10)) % 3600 if za[zc["id"]]["valid"] else 0
                else:
                    sv = zs.get(zc["id"]) if o.get("value") == "state" else None    # состояния нет (пороги не заданы) — пишем бит движения
                    val = int(zb.get(zc["id"], 0) if sv is None else sv)
                out.append((o["kind"], o["address"], val))
            ro = zc.get("rpm_out")
            if ro and zc["enabled"] and zc["type"] == "cycle":      # обороты в 0,01 об/мин знаковым int16 (против часовой — дополнительный код)
                out.append(("holding", ro["address"], int(round(za[zc["id"]]["rpm"] * 100)) & 0xFFFF))
        gv = {g["id"]: g for g in state["groups"]}
        for gc in self.cfg["groups"]:
            o = gc["out"]
            if not o:
                continue
            g = gv[gc["id"]]
            if o["kind"] == "holding":
                out.append(("holding", o["address"], int(g["value"]) & 0xFFFF if g["type"] == "mirror" else int(g["value"])))
            elif o["kind"] == "bits":
                packed = 0
                for i, b in enumerate(g["bits"][:16]):
                    packed |= (1 if b else 0) << i
                out.append(("holding", o["address"], packed))
            else:
                for i, b in enumerate(g["bits"]):
                    out.append(("coil", o["address"] + i, 1 if b else 0))
        so = self.cfg["status_out"]
        if so:
            out.append((so["kind"], so["address"], 1 if state["alive"] else 0))
        return out


def clone(cfg):
    return copy.deepcopy(cfg)
