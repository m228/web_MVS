"""motion_sync — остановки лент и сбои камер движения за последние дни для удалённого мониторинга (sysmon -> monitor_server).

Читает базу сервиса движения (motion_service\\data\\motion.db) ТОЛЬКО НА ЧТЕНИЕ и конфиг зон (motion_config.json) и считает из корзин
по 10 с (таблица stat — хранится «навсегда», вес малый) эпизоды трёх видов:
  * stop   — лента СТОИТ: метрика зоны ниже порога off дольше STOP_MIN_S при нормальной картинке (камера в порядке);
  * near   — «близко к порогу»: зона крутится, но метрика упала до off·NEAR_K и ниже (дольше NEAR_MIN_S) — до остановки оставалось чуть-чуть;
  * camera — поломка камеры: в базе нет кадров «в норме» дольше CAM_GAP_S (нет кадров, картинка застыла, битый поток, сервис не работал).
Остановка и поломка камеры — разные вещи: во время дыры данных «лента стоит» не судим.

Что уходит на сервер — только эпизоды (время, длительность, камера, зона, мин / ср значения, пороги), не кадры и не база.
Эпизод получает постоянный eid по камере, зоне и началу: сервер при повторе обновляет запись, а не дублирует. Уже отправленное
без изменений заново не шлём (журнал отправленного — в sysmon.db), так что первая отправка — полная за DAYS суток, дальше только новое
и то, что изменилось (длившаяся остановка закончилась). Не ушло — лежит в очереди sysmon и уйдёт в следующее окно связи.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Iterable, Optional

BIN_S = 10                  # длина корзины таблицы stat
CAM_GAP_S = 120             # дыра в данных дольше — камера не в порядке
STOP_MIN_S = 120            # лента стоит дольше — это остановка
NEAR_MIN_S = 60             # «близко к порогу» дольше этого
NEAR_K = 1.15               # близко: значение <= off * NEAR_K (но ещё выше off)
DAYS = 30                   # потолок окна, сут: берём всё, что есть в базе движения, но не глубже (глубина базы — в «from» итога)
EP_MAX = 300                # эпизодов за один прогон (защита от лавины при неисправном пороге)
REASON_NONE = "нет кадров в норме (сервис не работал, запись выключена или камера молчит)"


# ------------------------------------------------------------------------------------------------------------------ чтение

def load_zones(cfg_path: Path) -> dict:
    """{id камеры: {"name", "zones": {id зоны: {"name", "metric", "on", "off"}}}} — только включённые зоны-«движение» (не уровень, не палки, не «жом есть»)."""
    try:
        cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict = {}
    for c in cfg.get("cameras") or []:
        if not isinstance(c, dict) or not c.get("id") or c.get("enabled") is False:
            continue
        zones = {}
        for z in c.get("zones") or []:
            if not isinstance(z, dict) or not z.get("id") or z.get("enabled") is False:
                continue
            if (z.get("type") or "motion") != "motion" or z.get("invert"):
                continue
            metric = z.get("metric") or "flow"
            if metric not in ("flow", "diff"):
                continue
            try:
                on, off = float(z.get("on", 0)), float(z.get("off", 0))
            except (TypeError, ValueError):
                continue
            if on <= 0:                                   # порогов нет — судить не по чему
                continue
            zones[str(z["id"])] = {"name": str(z.get("name") or z["id"]), "metric": metric, "on": on, "off": off}
        if zones:
            out[str(c["id"])] = {"name": str(c.get("name") or c["id"]), "zones": zones}
    return out


def _open_ro(db_path: Path) -> sqlite3.Connection:
    c = sqlite3.connect("file:%s?mode=ro" % Path(db_path).resolve().as_posix(), uri=True, timeout=10)
    return c


def read_bins(db: sqlite3.Connection, cam: str, zones: Iterable[str], t0: float, t1: float) -> dict:
    """{зона: [(ключ корзины, число кадров, среднее flow, среднее diff, мин flow, мин diff)]} по возрастанию; значения в базе целые ×10000."""
    ids = {n: i for i, n in db.execute("SELECT id, name FROM names") if n == cam or n in set(zones)}
    if cam not in ids:
        return {}
    out: dict = {}
    for z in zones:
        if z not in ids:
            continue
        rows = db.execute("SELECT key, n, sum_flow, sum_diff, min_flow, min_diff FROM stat WHERE cam=? AND zone=? AND key>=? AND key<? ORDER BY key",
                          (ids[cam], ids[z], int(t0 // BIN_S), int(t1 // BIN_S) + 1)).fetchall()
        out[z] = [(k, n, sf / 10000.0 / n, sd / 10000.0 / n, mf / 10000.0, md / 10000.0) for k, n, sf, sd, mf, md in rows if n > 0]
    return out


def gap_reason(db: sqlite3.Connection, cam: str, t0: float, t1: float) -> str:
    """Что было в записи метрик (rec) в дыру: самая частая причина не-ok; нет строк — общая фраза."""
    try:
        r = db.execute("SELECT id FROM names WHERE name=?", (cam,)).fetchone()
        if not r:
            return REASON_NONE
        row = db.execute("SELECT n.name, COUNT(*) c FROM rec r JOIN names n ON n.id=r.status WHERE r.cam=? AND r.ts>=? AND r.ts<? AND n.name!='ok' "
                         "AND n.name NOT LIKE 'событие:%' GROUP BY n.name ORDER BY c DESC LIMIT 1", (r[0], int(t0 * 1000), int(t1 * 1000))).fetchone()
        return row[0] if row else REASON_NONE
    except sqlite3.Error:
        return REASON_NONE


# ------------------------------------------------------------------------------------------------------------------ эпизоды

def _value(b: tuple, metric: str) -> float:
    return b[2] if metric == "flow" else b[3]


def _low(b: tuple, metric: str) -> float:
    return b[4] if metric == "flow" else b[5]


def camera_gaps(keys: list, now: float, t0: float) -> list:
    """Дыры между корзинами (любой зоны камеры) дольше CAM_GAP_S: [(начало, конец)]. Хвост до «сейчас» не берём — его видно по живому пульсу."""
    gaps = []
    prev = None
    for k in keys:
        if prev is not None and (k - prev) * BIN_S - BIN_S > CAM_GAP_S:
            gaps.append(((prev + 1) * BIN_S, k * BIN_S))
        prev = k
    return gaps


def zone_episodes(bins: list, zc: dict, now: float) -> list:
    """Остановки и «близко» одной зоны по её корзинам. Дыры в данных (камера не в порядке) разрывают эпизод — в дыру «стоит» не судим.
    -> [{"kind": "stop"|"near", "t0", "t1", "open", "min", "mean"}]"""
    metric, on, off = zc["metric"], zc["on"], zc["off"]
    near_hi = off * NEAR_K if off > 0 else 0.0
    eps: list = []
    state = None                                          # None — неизвестно, "run" — крутится, "stop" — стоит
    cur = None                                            # открытый эпизод: [kind, t0, t_end, sum, n, min]
    prev_k = None

    def close(open_: bool):
        nonlocal cur
        if cur is not None:
            kind, a, b, s, n, mn = cur
            need = STOP_MIN_S if kind == "stop" else NEAR_MIN_S
            if b - a >= need:
                eps.append({"kind": kind, "t0": a, "t1": b, "open": open_, "min": round(mn, 4), "mean": round(s / n, 4)})
        cur = None

    for b in bins:
        k = b[0]
        if prev_k is not None and (k - prev_k) * BIN_S - BIN_S > CAM_GAP_S:      # дыра: эпизод закрываем на последней корзине, состояние забываем
            close(False)
            state = None
        prev_k = k
        v = _value(b, metric)
        ts, te = k * BIN_S, k * BIN_S + BIN_S
        if v >= on:
            new = "run"
        elif v <= off:
            new = "stop"
        else:
            new = state or "run"                          # гистерезис: между порогами — как было
        if new != state:
            close(False)
            state = new
        want = None
        if state == "stop":
            want = "stop"
        elif near_hi and state == "run" and v <= near_hi:
            want = "near"
        if want is None:
            if cur is not None:
                close(False)
            continue
        if cur is not None and cur[0] != want:
            close(False)
        if cur is None:
            cur = [want, ts, te, 0.0, 0, v]
        cur[2] = te
        cur[3] += v
        cur[4] += 1
        cur[5] = min(cur[5], v if want == "stop" else _low(b, metric))
    if cur is not None:
        close(open_=(now - cur[2]) < CAM_GAP_S)           # последняя корзина совсем свежая — эпизод ещё идёт
    return eps


def collect(db_path: Path, cfg_path: Path, now: Optional[float] = None, days: float = DAYS) -> dict:
    """Все эпизоды за days суток: {"from", "to", "cams": [...], "episodes": [{eid, kind, cam, cam_name, zone, zone_name, t0, t1, open, ...}]}.
    Нет базы или зон — пустой результат (ничего не шлём)."""
    now = time.time() if now is None else now
    t0 = now - days * 86400
    zones = load_zones(cfg_path)
    res = {"from": t0, "to": now, "cams": [], "episodes": []}
    if not zones or not Path(db_path).is_file():
        return res
    try:
        db = _open_ro(db_path)
    except sqlite3.Error:
        return res
    first = now
    try:
        for cam, cc in sorted(zones.items()):
            try:
                bins = read_bins(db, cam, cc["zones"], t0, now)
            except sqlite3.Error:
                continue
            keys = sorted({b[0] for zb in bins.values() for b in zb})
            res["cams"].append(cam)
            if keys:
                first = min(first, keys[0] * BIN_S)
            for a, b in camera_gaps(keys, now, t0):
                res["episodes"].append({"kind": "camera", "cam": cam, "cam_name": cc["name"], "zone": "", "zone_name": "", "t0": a, "t1": b, "open": False,
                                        "reason": gap_reason(db, cam, a, b)})
            for z, zb in bins.items():
                zc = cc["zones"][z]
                for e in zone_episodes(zb, zc, now):
                    e.update(cam=cam, cam_name=cc["name"], zone=z, zone_name=zc["name"], metric=zc["metric"], on=zc["on"], off=zc["off"])
                    res["episodes"].append(e)
    finally:
        db.close()
    res["from"] = max(t0, first)            # «просмотрено» — с первой записи базы, а не с границы окна: база моложе окна — так и скажем (за 12 сут, а не за 30)
    for e in res["episodes"]:
        e["eid"] = "mo-%s-%s-%s-%d" % (e["kind"], e["cam"], e["zone"] or "-", int(e["t0"]))[:64]
    res["episodes"] = sorted(res["episodes"], key=lambda e: e["t0"])[-EP_MAX:]
    return res


def text(e: dict) -> str:
    """Короткая строка эпизода для журнала."""
    dur = int(e["t1"] - e["t0"])
    d = "%d мин" % round(dur / 60) if dur >= 90 else "%d с" % dur
    where = e["cam_name"] + ((" · " + e["zone_name"]) if e.get("zone_name") else "")
    if e["kind"] == "stop":
        return "Лента стоит %s: %s%s" % (d, where, " (ещё идёт)" if e.get("open") else "")
    if e["kind"] == "near":
        return "Близко к остановке (%s): %s, мин %.3g при пороге %.3g" % (d, where, e.get("min", 0), e.get("off", 0))
    return "Камера не в порядке %s: %s — %s" % (d, where, e.get("reason") or "")
