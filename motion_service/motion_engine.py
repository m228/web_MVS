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
import re
from collections import deque
from typing import Optional

import cv2
import numpy as np

CALIB_DT = 0.33          # интервал между кадрами, при котором калибровались пороги потока (камеры ~3 fps)
FB = dict(pyr_scale=0.5, levels=3, winsize=15, iterations=3, poly_n=5, poly_sigma=1.2, flags=0)
MIN_FLOW_SIDE = 16       # зона меньше этого (px) для Farneback не годится — поток = 0
ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")

ZONE_TYPES = ("motion", "presence")
METRICS = ("flow", "diff")
GROUP_TYPES = ("sum", "monotonic", "worktimer")
HW_ACCEL_MODES = ("off", "auto", "d3d11")    # аппаратное декодирование видео: выкл / любое доступное / Direct3D 11 (видеоядро Intel, AMD, NVIDIA)
ZONE_OUT_KINDS = ("holding", "coil")
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


def _out(o, kinds, name, allow_none=True):
    if not o or not o.get("kind"):
        return None
    kind = o.get("kind")
    if kind not in kinds:
        raise ValueError("%s: вид вывода %r не из %s" % (name, kind, list(kinds)))
    return {"kind": kind, "address": _num(o.get("address", 0), name + ".address", 0, 65535, True)}


def _norm_zone(z: dict, cam_id: str) -> dict:
    zid = str(z.get("id") or "")
    if not ID_RE.match(zid):
        raise ValueError("камера %s: id зоны %r — только латиница, цифры, _ и -" % (cam_id, zid))
    where = "зона %s/%s" % (cam_id, zid)
    ztype = z.get("type", "motion")
    if ztype not in ZONE_TYPES:
        raise ValueError("%s: тип %r не из %s" % (where, ztype, list(ZONE_TYPES)))
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
        "out": _out(z.get("out"), ZONE_OUT_KINDS, where + ".out"),
    }


def _norm_group(g: dict, cam_id: str, zone_ids: list) -> dict:
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
    zones = {c["id"]: {z["id"] for z in c["zones"]} for c in cams}
    srcs = []
    for s in sc.get("sources") or []:     # источники удалённых камер/зон тихо отбрасываем: сценарий не должен ломать весь конфиг
        cam, zone = str(s.get("camera", "")), str(s.get("zone", ""))
        if zone in zones.get(cam, ()) and not any(x["camera"] == cam and x["zone"] == zone for x in srcs):
            srcs.append({"camera": cam, "zone": zone})
    mb = sc.get("modbus")
    return {"id": sid, "name": str(sc.get("name") or sid), "rule": rule, "sources": srcs,
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
    groups = [_norm_group(g, cid, ids) for g in (c.get("groups") or [])]
    gids = [g["id"] for g in groups]
    if len(set(gids)) != len(gids):
        raise ValueError("камера %s: id групп повторяются" % cid)
    transport = str(c.get("transport", "tcp")).lower()
    if transport not in ("tcp", "udp"):
        raise ValueError("камера %s: transport tcp или udp" % cid)
    hw = str(c.get("hw_accel", "off")).lower()
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

    def reset(self):
        self.hist_flow.clear()
        self.hist_diff.clear()
        self.hys.reset()
        self.flow = self.diff = self.value = 0.0

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
        return self.hys.update(self.value)


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

    def __init__(self, cam_cfg: dict):
        self.cfg = cam_cfg
        self.gap = cam_cfg["frame_gap"]
        self.proc_interval = 1.0 / cam_cfg["proc_fps"]
        self.buf: deque = deque(maxlen=self.gap + 1)
        self.zones: Optional[list[ZoneRT]] = None
        self.frame_wh: Optional[tuple] = None
        self.timers = {g["id"]: WorkTimer(g["window_s"], g["agg"]) for g in cam_cfg["groups"] if g["type"] == "worktimer"}
        self.last_state: Optional[dict] = None

    def reset(self):
        """После потери камеры/переподключения: буфер кадров и состояния — с нуля."""
        self.buf.clear()
        for z in self.zones or []:
            z.reset()
        for t in self.timers.values():
            t.reset()

    def _build(self, gray):
        h, w = gray.shape[:2]
        self.frame_wh = (w, h)
        self.zones = [ZoneRT(z, self.cfg["frame_size"], (w, h)) for z in self.cfg["zones"] if z["enabled"]]

    def process(self, gray: np.ndarray, now: float) -> Optional[dict]:
        """Следующий (уже отобранный по proc_fps) серый кадр → состояние. None, пока копится буфер для FRAME_GAP."""
        if self.zones is None or self.frame_wh != (gray.shape[1], gray.shape[0]):
            self._build(gray)
            self.buf.clear()
        self.buf.append(gray)
        if len(self.buf) <= self.gap:
            return None
        prev = self.buf[0]
        dt = self.gap * self.proc_interval
        bits = {z.id: z.step(prev, gray, dt) for z in self.zones}
        self.last_state = self._state(bits, now, alive=True)
        return self.last_state

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
            z = zs.get(zc["id"])
            zones.append({"id": zc["id"], "name": zc["name"], "type": zc["type"], "metric": zc["metric"],
                          "enabled": zc["enabled"], "bit": int(bits.get(zc["id"], 0)) if alive else 0,
                          "flow": round(z.flow, 4) if z else 0.0, "diff": round(z.diff, 4) if z else 0.0,
                          "value": round(z.value, 4) if z else 0.0, "on": zc["on"], "off": zc["off"]})
        groups = []
        for g in self.cfg["groups"]:
            gb = {zid: (bits.get(zid, 0) if alive else 0) for zid in g["zones"]}
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

    def compute_writes(self, state: dict) -> list:
        """Состояние → записи в ПЛК: [(kind, address, value)], kind: holding | coil. Порядок стабильный."""
        out = []
        zb = {z["id"]: z["bit"] for z in state["zones"]}
        for zc in self.cfg["zones"]:
            o = zc["out"]
            if o and zc["enabled"]:
                out.append((o["kind"], o["address"], int(zb.get(zc["id"], 0))))
        gv = {g["id"]: g for g in state["groups"]}
        for gc in self.cfg["groups"]:
            o = gc["out"]
            if not o:
                continue
            g = gv[gc["id"]]
            if o["kind"] == "holding":
                out.append(("holding", o["address"], int(g["value"])))
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
