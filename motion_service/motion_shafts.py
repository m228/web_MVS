"""Четыре вала на двух камерах: скорости и разности между валами (НЛВ, НПВ, ВЛВ, ВПВ).

Н/В — нижний/верхний ряд (поле камеры «ряд»), Л/П — левый/правый вал в ряду (по положению оси в кадре). Шесть букв не придумка:
НЛВ = Нижний Левый Вал, ВПВ = Верхний Правый Вал и так далее.

Четыре пары «рамкой»:
  nlr  НЛВ–НПВ — левый и правый нижнего ряда (крутятся навстречу: углы зеркальны, как у группы mirror)
  vlr  ВЛВ–ВПВ — то же у верхнего ряда
  lnv  НЛВ–ВЛВ — левые валы двух рядов (углы совпадают с точностью до эталона)
  rnv  НПВ–ВПВ — правые валы двух рядов

Для каждой пары считаем две разности:
  rpm_diff   — |скорость первого| − |скорость второго|, об/мин (направление вращения у левого и правого разное, поэтому по модулю);
  angle_diff — отклонение углов от эталона ref, градусы −180..180. Камеры смотрят под разными ракурсами, поэтому «ноль» разности
               углов снимается вручную (calibrate), когда валы стоят в нужном положении: ref = текущее значение,
               или по среднему за период (calibrate с window): ref = среднее сырого угла за окно, где палки видны без удержания.

Расчёт compute() — чистая функция «конфиг + состояния камер → снимок», без потоков и сети (проверяется тестами).
ShaftRunner раз в секунду зовёт её, ведёт историю для графика и пишет разности в ПЛК тем же каналом, что и сценарии.
"""
from __future__ import annotations

import bisect
import json
import logging
import math
import os
import threading
import time
from collections import deque
from typing import Optional

from motion_engine import PAIR_IDS, SHAFT_IDS, Hysteresis, MirrorLearn, ang_diff, normalize_shafts

log = logging.getLogger("motion.shafts")

ROWS = ("lower", "upper")
ROW_LETTER = {"lower": "Н", "upper": "В"}
# id вала → (ряд, сторона); подпись = буква ряда + буква стороны + «В»
SHAFTS = {"nl": ("lower", "L"), "np": ("lower", "R"), "vl": ("upper", "L"), "vp": ("upper", "R")}
SHAFT_ORDER = SHAFT_IDS
SIDE_LETTER = {"L": "Л", "R": "П"}
# id пары → (вал A, вал B, режим угла): mirror — углы зеркальны (сумма), same — углы одинаковы (разность)
PAIRS = {"nlr": ("nl", "np", "mirror"), "vlr": ("vl", "vp", "mirror"),
         "lnv": ("nl", "vl", "same"), "rnv": ("np", "vp", "same")}
PAIR_ORDER = PAIR_IDS
HISTORY_LEN = 20000           # точек на пару (раз в секунду ≈ 5,5 часа)
STATS_MAX_S = 18000.0         # окно статистики и авто-эталона: 5 ч, чтобы не выходить за историю
AUTO_REF_MIN_POINTS = 30      # меньше точек (секунд с видимыми палками) — эталон по среднему не снимаем
ALIGN_MARGIN_S = 0.6          # запас назад от «сейчас»: к моменту сравнения обе камеры должны успеть прислать кадр ПОСЛЕ него
MAX_GAP_S = 1.5               # соседние кадры ряда дальше друг от друга — интерполировать нельзя
SKEW_LEN = 300                # секунд истории «разъезда кадров» пары для панели
DEFAULT_RPM = 0.758           # когда скорость вала неизвестна (для перевода секунд в градусы)
OFFSET_WIN_S = 300.0          # оценка расхождения камер по Δ углов: окно, с (оборот вала ≈ 85 с — волна перекоса за оборот усредняется)
OFFSET_MIN_N = 60             # меньше секундных точек в окне — оценку не показываем
OFFSET_MIN_RPM = 0.2          # вал медленнее — угол от времени почти не зависит, секунды из градусов не получить
OFFSET_AGREE_S = 0.5          # две пары (левые и правые валы) дают одно расхождение с точностью до этого (или 30 %) — считаем, что это задержка, а не эталон
STALE_LAG_S = 1.0             # очередь кадров камеры длиннее (и камеры разные) — Δ углов пары недостоверна: тревогу не двигаем, самоподстройку не учим
FLIP_MIN_DEG = 150.0          # |Δ| не меньше — похоже, что одна палка держит противоположный конец (красный и синий перепутались), а не вал убежал на пол-оборота
FLIP_GRACE_S = 300.0          # так долго такую Δ не считаем тревогой (на пуске это 1–3 мин); держится дольше — это уже настоящий разъезд, тревога как обычно
STAND_RESUME_S = 5.0          # вал считается поехавшим, только если «крутится» держится столько секунд подряд: дрожание стоящего вала счёт остановки не сбрасывает


class StandWatch:
    """Тревога «вал стоит»: вал не крутится дольше stop_s секунд подряд. moving: True крутится / False стоит / None неизвестно
    (палку не видно) — неизвестно ничего не меняет: ни не запускает счёт, ни не сбрасывает его и не гасит тревогу. stop_s 0 — выключена."""

    def __init__(self):
        self.since: Optional[float] = None          # с какого момента стоит
        self._run: Optional[float] = None           # с какого момента подряд «крутится»

    def update(self, now: float, moving: Optional[bool], stop_s: float) -> int:
        if not stop_s:
            self.since = self._run = None
            return 0
        if moving is False:
            self._run = None
            if self.since is None:
                self.since = now
        elif moving is True:
            if self._run is None:
                self._run = now
            if now - self._run >= STAND_RESUME_S:
                self.since = None
        return int(self.since is not None and now - self.since >= stop_s)


def shaft_short(sid: str) -> str:
    row, side = SHAFTS[sid]
    return ROW_LETTER[row] + SIDE_LETTER[side] + "В"


def pair_short(pid: str) -> str:
    a, b, _ = PAIRS[pid]
    return shaft_short(a) + "–" + shaft_short(b)


def default_shafts_cfg() -> dict:
    return normalize_shafts(None)


def assign_shafts(cams: list) -> dict:
    """Какая зона-цикл какому валу соответствует: {id вала: (id камеры, id зоны)}.

    Камера с заданным рядом отдаёт свои зоны-циклы (включённые) по порядку слева направо: первая — левый вал, вторая — правый.
    Одна зона на камере — левая, если ось левее середины кадра, иначе правая. Если у двух камер один ряд — берётся первая в конфиге."""
    out: dict = {}
    for c in cams:
        row = c.get("row") or ""
        if row not in ROWS or not c.get("enabled", True):
            continue
        zs = sorted((z for z in c.get("zones", []) if z.get("type") == "cycle" and z.get("enabled", True) is not False),
                    key=lambda z: z["center"][0])
        if not zs:
            continue
        if len(zs) == 1:
            fw = (c.get("frame_size") or [0])[0]
            left = fw <= 0 or zs[0]["center"][0] < fw / 2.0
            picks = [("L" if left else "R", zs[0])]
        else:
            picks = [("L", zs[0]), ("R", zs[1])]
        for side, z in picks:
            sid = next(k for k, v in SHAFTS.items() if v == (row, side))
            out.setdefault(sid, (c["id"], z["id"]))
    return out


def _zone_state(states: dict, cam: str, zone: str) -> Optional[dict]:
    res = (states.get(cam) or {}).get("result") or {}
    if not res.get("alive"):
        return None
    for z in res.get("zones") or []:
        if z.get("id") == zone:
            return z
    return None


def track_at(series: list, t: float, max_gap: float = MAX_GAP_S) -> Optional[tuple]:
    """Угол палки на момент t по ряду [(время съёмки, угол, valid, coast)]: линейно между соседними кадрами по кратчайшей дуге.
    -> (угол 0..360, valid, coast) или None: t вне ряда или соседние кадры дальше max_gap. valid — оба соседа valid, coast — любой в удержании."""
    n = len(series)
    if n < 2 or t < series[0][0] or t > series[-1][0]:
        return None
    i = bisect.bisect_right([p[0] for p in series], t) - 1
    if i >= n - 1:
        a = series[-1]
        return (a[1] % 360.0, bool(a[2]), bool(a[3]))
    a, b = series[i], series[i + 1]
    if b[0] - a[0] > max_gap:
        return None
    f = (t - a[0]) / (b[0] - a[0]) if b[0] > a[0] else 0.0
    return ((a[1] + f * ang_diff(b[1], a[1])) % 360.0, bool(a[2] and b[2]), bool(a[3] or b[3]))


def raw_angle(mode: str, a: float, b: float) -> float:
    """Сырое значение угла пары до вычета эталона: сумма (зеркально) или разность (одинаково)."""
    return a + b if mode == "mirror" else a - b


def compute(cfg: dict, states: dict, at: Optional[dict] = None) -> dict:
    """Конфиг сервиса + {id камеры: state()} → снимок валов и пар. Валы без данных — valid False, значения None.
    at — {id вала: {"angle", "valid", "coast"}}: угол на общий момент времени съёмки (интерполяция по ряду) вместо «последнего кадра»."""
    sc = cfg.get("shafts") or default_shafts_cfg()
    assign = assign_shafts(cfg.get("cameras", []))
    cam_names = {c["id"]: c.get("name") or c["id"] for c in cfg.get("cameras", [])}
    shafts: dict = {}
    for sid in SHAFT_ORDER:
        cam, zone = assign.get(sid, (None, None))
        zs = _zone_state(states, cam, zone) if cam else None
        ok = bool(zs and zs.get("valid"))
        ov = (at or {}).get(sid)
        angle = zs.get("angle") if zs else None
        coast = bool(zs.get("coast")) if zs else False
        if ov is not None and ok:
            ok, angle, coast = bool(ov["valid"] and ov["angle"] is not None), ov["angle"], bool(ov["coast"])
        shafts[sid] = {"id": sid, "short": shaft_short(sid), "camera": cam, "camera_name": cam_names.get(cam), "zone": zone,
                       "assigned": cam is not None, "valid": ok,
                       "angle": round(float(angle), 2) if ok else None,
                       "rpm": round(float(zs.get("rpm", 0.0)), 3) if ok else None,
                       "coast": coast,
                       "src": (zs.get("src") or "none") if zs else "none",       # чем виден вал: both / red / blue / hold / none — для подсветки красной и синей половин
                       # отражатели концов [красный, синий] 0..1 — в виджете жёлтые точки, видно — ярко, нет — тускло; None — отражатели не настроены
                       "tip": [float(zs.get("lvl_tr") or 0.0), float(zs.get("lvl_tb") or 0.0)] if zs and zs.get("tip_on") else None,
                       "moving": zs.get("moving") if zs else None}
    pairs: dict = {}
    for pid in PAIR_ORDER:
        a_id, b_id, mode = PAIRS[pid]
        a, b = shafts[a_id], shafts[b_id]
        pc = sc["pairs"][pid]
        ok = a["valid"] and b["valid"]
        raw = round(raw_angle(mode, a["angle"], b["angle"]), 2) if ok else None
        pairs[pid] = {"id": pid, "short": pair_short(pid), "a": a_id, "b": b_id, "mode": mode, "valid": ok,
                      "assigned": a["assigned"] and b["assigned"],
                      "raw": raw,
                      "angle_diff": round(ang_diff(raw, pc["ref"]), 2) if ok else None,
                      "rpm_diff": round(abs(a["rpm"]) - abs(b["rpm"]), 3) if ok else None,
                      "ref": pc["ref"], "tol_angle": pc["tol_angle"], "tol_rpm": pc["tol_rpm"]}
    return {"shafts": shafts, "pairs": pairs}


def camera_offset(buf, now: float) -> Optional[float]:
    """Расхождение кадров двух камер по Δ углов пары «одинаково», с: τ(A) − τ(B), где τ — на сколько кадр камеры старше «приход − latency_s».
    Кадр камеры A показывает угол θ(t − τA), Δ = θA − θB − ref = −rate·(τA − τB) (rate, °/с — со знаком: «+» по часовой), отсюда
    τA − τB = −Δ/rate. «+» — кадры первой камеры старее; Δ мерится от эталона, поэтому это изменение расхождения с момента эталона.
    buf — [(время, Δ, rate)]; нужно ≥ OFFSET_MIN_N точек за OFFSET_WIN_S. None — данных мало."""
    rows = [r for r in buf if now - r[0] <= OFFSET_WIN_S]
    if len(rows) < OFFSET_MIN_N:
        return None
    base = rows[0][1]
    d = base + sum(ang_diff(r[1], base) for r in rows) / len(rows)
    rate = sum(r[2] for r in rows) / len(rows)
    if abs(rate) < OFFSET_MIN_RPM * 6.0:
        return None
    return -ang_diff(d, 0.0) / rate


class ShaftRunner(threading.Thread):
    """Раз в секунду: снимок валов → история (для графика) → тревоги → запись в ПЛК (если задан адрес и ПЛК включён)."""

    def __init__(self, mgr):
        super().__init__(daemon=True, name="shafts")
        self.mgr = mgr
        self._halt = threading.Event()
        self.snap: dict = {"shafts": {}, "pairs": {}}
        self.hist: dict = {p: deque(maxlen=HISTORY_LEN) for p in PAIR_ORDER}
        self._hys: dict = {}              # (пара, "angle"/"rpm") → (допуск, Hysteresis)
        self._stand: dict = {sid: StandWatch() for sid in SHAFT_ORDER}      # вал → тревога «стоит»
        self._flip: dict = {}             # пара → с какого момента |Δ| ≥ FLIP_MIN_DEG
        self._sent: dict = {}
        self.status: dict = {}            # ключ вывода → {ok, error}
        self.learners: dict = {}          # пара → (окно в минутах, MirrorLearn): самоподстройка волны Δ угла за оборот
        self._avg: dict = {}              # пара → deque (время, Δ угла): усреднение за avg_s
        self._avg_live: dict = {}         # то же для живого показа (виджет): окно по времени, частота опроса не важна
        self._live_lock = threading.Lock()
        self._skew: dict = {p: deque(maxlen=SKEW_LEN) for p in PAIR_ORDER}   # пара → (время, разъезд кадров двух камер, с)
        self._off: dict = {p: deque() for p in PAIR_ORDER if PAIRS[p][2] == "same"}   # пара → (время, Δ угла до автокоррекции, °, град/с первого вала)
        d = getattr(mgr, "dir", None)
        self.learn_path = (d / "shafts_learn.json") if d else None
        self._saved = self._learn_load()
        self._learn_saved_at = 0.0

    def _learn_load(self) -> dict:
        if not self.learn_path:
            return {}
        try:
            with open(self.learn_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _learn_save(self, now: float) -> None:
        """Раз в минуту, если форма менялась: data\\shafts_learn.json. Ошибка записи не мешает расчёту."""
        if not self.learn_path or now - self._learn_saved_at < 60.0:
            return
        self._learn_saved_at = now
        if not any(m.dirty for _, m in self.learners.values()):
            return
        try:
            data = {pid: {"shape": [round(v, 2) for v in m.shape]} for pid, (_, m) in self.learners.items()}
            tmp = str(self.learn_path) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.learn_path)
            for _, m in self.learners.values():
                m.dirty = False
            self._saved = data
        except OSError:
            pass

    def _learner(self, pid: str, pc: dict) -> Optional[MirrorLearn]:
        """Учитель пары по её настройкам: выключили — убираем, сменили окно — создаём заново (форму берём ту, что уже выучена)."""
        if not pc.get("learn", True):
            self.learners.pop(pid, None)
            return None
        win = int(pc.get("learn_min", 30))
        cur = self.learners.get(pid)
        if cur is None or cur[0] != win:
            shape = cur[1].shape if cur else (self._saved.get(pid) or {}).get("shape")
            cur = (win, MirrorLearn(win * 60.0, shape))
            self.learners[pid] = cur
        return cur[1]

    def stop(self):
        self._halt.set()

    def snapshot(self) -> dict:
        return self.snap

    def live(self, now: Optional[float] = None) -> dict:
        """Лёгкий снимок для виджета, зовётся часто (6–8 Гц): те же углы на общий момент съёмки и разности пар, но БЕЗ побочных
        эффектов — история, обучение, аварии и запись в ПЛК остаются на секундном тике. Поправка зеркальности и аварии берутся
        из последнего тика; среднее за avg_s считается отдельным буфером по времени."""
        now = time.time() if now is None else now
        cfg = self.mgr.cfg
        sc = cfg.get("shafts") or default_shafts_cfg()
        states = {cid: w.state() for cid, w in list(self.mgr.workers.items())}
        at, _t = self._aligned(cfg, sc, now)
        snap = compute(cfg, states, at)
        last = (self.snap.get("pairs") or {})
        with self._live_lock:
            for pid, p in snap["pairs"].items():
                lp = last.get(pid) or {}
                p["angle_diff_raw"] = p["angle_diff"]
                p["stale"] = bool(p["valid"] and self._stale(p, snap, states))
                if p["valid"]:
                    p["angle_diff"] = round(ang_diff(p["angle_diff"], float(lp.get("corr") or 0.0)), 2)
                if p["stale"]:
                    p["angle_diff_inst"] = p["angle_diff"]
                else:
                    self._average(pid, p, sc.get("avg_s", 4.0), now, self._avg_live)
                p["alarm_angle"] = lp.get("alarm_angle", 0)
                p["alarm_rpm"] = lp.get("alarm_rpm", 0)
        lsh = self.snap.get("shafts") or {}
        return {"ts": now,
                "shafts": {sid: dict({k: v[k] for k in ("id", "assigned", "valid", "angle", "coast", "src", "tip")},
                                     alarm_stop=int((lsh.get(sid) or {}).get("alarm_stop", 0)))      # тревога «стоит» — из последнего тика, как и аварии пар
                           for sid, v in snap["shafts"].items()},
                "pairs": {pid: {k: v.get(k) for k in ("id", "valid", "angle_diff", "angle_diff_raw", "alarm_angle", "alarm_rpm", "stale")}
                          for pid, v in snap["pairs"].items()}}

    def history(self, pair: str, window_s: float = 3600.0, points: int = 600) -> dict:
        rows = list(self.hist.get(pair, ()))
        if window_s > 0:
            t0 = time.time() - window_s
            rows = [r for r in rows if r[0] >= t0]
        if points > 0 and len(rows) > points:
            step = len(rows) / float(points)
            rows = [rows[int(i * step)] for i in range(points)]
        return {"pair": pair, "t": [round(r[0], 1) for r in rows], "angle": [r[1] for r in rows], "rpm": [r[2] for r in rows],
                "angle_raw": [r[5] for r in rows]}      # angle — после автокоррекции, angle_raw — до неё

    def stats(self, pair: str, window_s: float = 3600.0, now: Optional[float] = None) -> dict:
        """Среднее / мин / макс / среднее по модулю разности углов и скоростей пары за window_s секунд.

        Берутся секундные точки, где видны оба вала и ни у одного нет удержания (coast). Угол считается от ТЕКУЩЕГО эталона по сырому значению,
        поэтому после смены эталона цифры сразу честные. ref_suggest — эталон, при котором среднее Δ угла было бы нулём (круговое среднее сырого угла).
        angle — Δ угла с автокоррекцией, angle_raw — без неё (оба от текущего эталона). span_s — сколько секунд реально накоплено (не больше окна)."""
        now = time.time() if now is None else now
        window_s = max(1.0, min(float(window_s), STATS_MAX_S))
        t0 = now - window_s
        rows = [r for r in list(self.hist.get(pair, ())) if r[0] >= t0 and not r[3]]
        out = {"pair": pair, "window_s": window_s, "n": len(rows), "span_s": 0.0, "angle": None, "angle_raw": None, "rpm": None, "ref": None,
               "ref_suggest": None}
        pc = ((self.mgr.cfg.get("shafts") or {}).get("pairs") or {}).get(pair) or {}
        ref = float(pc.get("ref", 0.0))
        out["ref"] = ref
        if not rows:
            return out
        out["span_s"] = round(min(window_s, now - rows[0][0]), 1)
        raw = [ang_diff(r[4], ref) for r in rows]                       # от ТЕКУЩЕГО эталона, без автокоррекции
        ang = [ang_diff(v, r[6]) for v, r in zip(raw, rows)]           # то же за вычетом выученной поправки того момента
        rpm = [r[2] for r in rows]
        agg = lambda v: {"avg": round(sum(v) / len(v), 2), "min": round(min(v), 2), "max": round(max(v), 2),
                         "avg_abs": round(sum(abs(x) for x in v) / len(v), 2)}
        out["angle"], out["angle_raw"], out["rpm"] = agg(ang), agg(raw), agg(rpm)
        sx = sum(math.cos(math.radians(r[4])) for r in rows)
        sy = sum(math.sin(math.radians(r[4])) for r in rows)
        out["ref_suggest"] = round(ang_diff(math.degrees(math.atan2(sy, sx)), 0.0), 2)
        return out

    @staticmethod
    def _stale(p: dict, snap: dict, states: dict) -> bool:
        """Углы пары сняты с разных камер, и у какой-то кадры стоят в очереди дольше STALE_LAG_S: разность — это задержка, а не вал.
        У пары на одной камере (оба вала из одного кадра) очередь ничего не искажает."""
        cams = {snap["shafts"][p["a"]]["camera"], snap["shafts"][p["b"]]["camera"]} - {None}
        if len(cams) < 2:
            return False
        return any(((states.get(c) or {}).get("lag_s") or 0.0) > STALE_LAG_S for c in cams)

    def _alarm_keep(self, pid: str, kind: str) -> int:
        """Тревога как есть: ни включить, ни выключить, ни накопить счёт (как в удержании)."""
        cur = self._hys.get((pid, kind))
        return int(cur[1].state) if cur else 0

    def _flipping(self, pid: str, p: dict, now: float) -> bool:
        """Δ около ±180° первые FLIP_GRACE_S секунд: палка схватила не тот конец (на пуске после стоянки так бывает 1–3 мин). Тревогу по углу
        на это время не двигаем (как при очереди кадров); не прошло само за FLIP_GRACE_S — настоящий разъезд, дальше тревога как обычно."""
        d = p["angle_diff"]
        if not p["valid"] or d is None or abs(d) < FLIP_MIN_DEG:
            self._flip.pop(pid, None)
            return False
        return now - self._flip.setdefault(pid, now) < FLIP_GRACE_S

    def _alarm(self, pid: str, kind: str, tol: float, value: Optional[float]) -> int:
        """Тревога по допуску: tol 0 — выкл; нет данных — сброс (нечего судить)."""
        key = (pid, kind)
        if not tol or value is None:
            self._hys.pop(key, None)
            return 0
        cur = self._hys.get(key)
        if cur is None or cur[0] != tol:
            cur = (tol, Hysteresis(tol, tol * 0.7, False, 3, 5))
            self._hys[key] = cur
        return int(cur[1].update(abs(value)))

    def _aligned(self, cfg: dict, sc: dict, now: float) -> tuple:
        """Углы всех назначенных валов на ОДИН момент времени съёмки T = сейчас − (макс. задержка камер) − запас.
        Время съёмки кадра = время прихода − latency_s камеры. -> ({id вала: {angle, valid, coast}} или None, T или None).
        Нет ряда у какого-то вала (камера только запустилась, нет кадров) — вся пара тиков считается по-старому (последний кадр)."""
        if not sc.get("align", True):
            return None, None
        assign = assign_shafts(cfg.get("cameras", []))
        lat = {c["id"]: float(c.get("latency_s", 0.0) or 0.0) for c in cfg.get("cameras", [])}
        cams = {cam for cam, _ in assign.values()}
        if not cams:
            return None, None
        t_cmp = now - max(lat.get(c, 0.0) for c in cams) - ALIGN_MARGIN_S
        out: dict = {}
        for sid, (cam, zone) in assign.items():
            w = self.mgr.workers.get(cam)
            track = getattr(w, "rod_track", None)
            if track is None:
                return None, None
            d = lat.get(cam, 0.0)
            r = track_at([(t - d, a, v, c) for t, a, v, c in track(zone)], t_cmp)
            if r is None:
                return None, None
            out[sid] = {"angle": r[0], "valid": r[1], "coast": r[2]}
        return out, t_cmp

    def _average(self, pid: str, p: dict, avg_s: float, now: float, store: Optional[dict] = None) -> None:
        """Δ угла пары → среднее за avg_s секунд (по кругу: от первой точки окна). Мгновенное значение остаётся в angle_diff_inst.
        store — свой словарь буферов (живой показ); по умолчанию буферы секундного тика."""
        buf = (self._avg if store is None else store).setdefault(pid, deque())
        p["angle_diff_inst"] = p["angle_diff"]
        if p["valid"] and p["angle_diff"] is not None:
            buf.append((now, p["angle_diff"]))
        while buf and now - buf[0][0] > max(avg_s, 0.0):
            buf.popleft()
        if avg_s > 0 and p["valid"] and buf:
            base = buf[0][1]
            p["angle_diff"] = round(ang_diff(base + sum(ang_diff(v, base) for _, v in buf) / len(buf), 0.0), 2)

    def _timing(self, cfg: dict, states: dict, snap: dict, aligned: bool, now: float, t_cmp: Optional[float]) -> dict:
        """Панель «Время кадров»: по камерам — частота, шаг кадров, поправка задержки; по парам — разъезд кадров двух камер
        (на сколько секунд разнесены последние кадры) и сколько это градусов при текущей скорости вала."""
        cams: dict = {}
        for c in cfg.get("cameras", []):
            w = self.mgr.workers.get(c["id"])
            ft = getattr(w, "frame_timing", None)
            if ft is not None and any(z.get("type") == "cycle" for z in c.get("zones", [])):
                cams[c["id"]] = dict(ft(), name=c.get("name") or c["id"], row=c.get("row") or "")
        lat = {cid: v["latency_s"] for cid, v in cams.items()}
        pairs: dict = {}
        offs: dict = {}               # пара → (оценка τA − τB, камера A, камера B)
        for pid in PAIR_ORDER:
            p = snap["pairs"][pid]
            sa, sb = snap["shafts"][p["a"]], snap["shafts"][p["b"]]
            ca, cb = sa["camera"], sb["camera"]
            ta = ((states.get(ca) or {}).get("result") or {}).get("ts") if ca else None
            tb = ((states.get(cb) or {}).get("result") or {}).get("ts") if cb else None
            same = bool(ca and ca == cb)
            if ta is not None and tb is not None and not same:
                self._skew[pid].append((now, abs((ta - lat.get(ca, 0.0)) - (tb - lat.get(cb, 0.0)))))
            rows = [v for t, v in self._skew[pid] if now - t <= SKEW_LEN]
            rpm = abs(sa["rpm"]) if sa.get("rpm") else DEFAULT_RPM
            deg = rpm * 6.0
            pairs[pid] = {"same_camera": same, "skew_avg_s": round(sum(rows) / len(rows), 3) if rows else None,
                          "skew_max_s": round(max(rows), 3) if rows else None, "deg_per_s": round(deg, 2),
                          "skew_avg_deg": round(deg * sum(rows) / len(rows), 2) if rows else None,
                          "skew_max_deg": round(deg * max(rows), 2) if rows else None, "offset_s": None}
            if pid in self._off:
                off = self._offset(pid, p, sa, sb, now) if not same else None
                if off is not None:
                    pairs[pid]["offset_s"] = round(off, 2)
                    offs[pid] = (off, ca, cb)
        return {"aligned": aligned, "compare_lag_s": round(now - t_cmp, 2) if t_cmp is not None else None, "cameras": cams, "pairs": pairs,
                "offset": self._offset_verdict(offs, cams)}

    def _offset(self, pid: str, p: dict, sa: dict, sb: dict, now: float) -> Optional[float]:
        """Копим Δ пары (до автокоррекции) в секунды, когда она честная: оба вала видны и крутятся, кадры не в очереди. -> camera_offset или None."""
        buf = self._off[pid]
        while buf and now - buf[0][0] > OFFSET_WIN_S:
            buf.popleft()
        rpm = sa.get("rpm") or 0.0
        if (p["valid"] and not p.get("stale") and not (sa["coast"] or sb["coast"]) and sa["moving"] is not False and sb["moving"] is not False
                and abs(rpm) >= OFFSET_MIN_RPM and p.get("angle_diff_raw") is not None):
            buf.append((now, p["angle_diff_raw"], rpm * 6.0))
        return camera_offset(buf, now)

    @staticmethod
    def _offset_verdict(offs: dict, cams: dict) -> Optional[dict]:
        """Итог по парам: чья камера отстаёт и на сколько. Одна пара — оценка без проверки; две (левые и правые валы) должны сойтись на одной
        камере и одной величине — иначе Δ вызвана не задержкой камеры (эталон, ракурс). None — оценок нет."""
        if not offs:
            return None
        old = {}                                 # пара → (камера, которая старше, на сколько секунд)
        for pid, (s, ca, cb) in offs.items():
            old[pid] = (ca if s > 0 else cb, abs(s))
        vals = list(old.values())
        if len(vals) == 1:
            cam, s = vals[0]
            agree = None
        else:
            cam, s = vals[0]
            agree = all(c == cam for c, _ in vals) and max(v for _, v in vals) - min(v for _, v in vals) <= max(OFFSET_AGREE_S, 0.3 * max(v for _, v in vals))
            s = sum(v for _, v in vals) / len(vals)
        return {"cam": cam, "name": (cams.get(cam) or {}).get("name") or cam, "s": round(s, 2), "agree": agree,
                "latency_s": (cams.get(cam) or {}).get("latency_s"), "pairs": sorted(offs)}

    def tick(self, now: Optional[float] = None):
        now = time.time() if now is None else now
        cfg = self.mgr.cfg
        states = {cid: w.state() for cid, w in list(self.mgr.workers.items())}
        sc = cfg.get("shafts") or default_shafts_cfg()
        at, t_cmp = self._aligned(cfg, sc, now)
        snap = compute(cfg, states, at)
        stop_s = float(sc.get("stop_s") or 0.0)
        for sid, sh in snap["shafts"].items():                    # «вал стоит» — своя тревога, на Δ пар не влияет (и Δ при остановке тревожит как обычно)
            sh["alarm_stop"] = self._stand[sid].update(now, sh["moving"] if sh["assigned"] else None, stop_s)
        for pid, p in snap["pairs"].items():
            sa, sb = snap["shafts"][p["a"]], snap["shafts"][p["b"]]
            lrn = self._learner(pid, sc["pairs"][pid])
            stale = bool(p["valid"] and self._stale(p, snap, states))
            p["stale"] = stale
            p["angle_diff_raw"] = p["angle_diff"]                  # до самоподстройки
            p["angle_diff_inst"] = None
            corr = 0.0
            if lrn:
                if p["valid"] and not (sa["coast"] or sb["coast"] or stale) and sa["moving"] is not False and sb["moving"] is not False:
                    lrn.add(now, sa["angle"], p["angle_diff"])     # учим только когда оба вала видны и крутятся (и кадры камер не в очереди)
                lrn.step(now)
                if p["valid"]:
                    corr = lrn.corr(sa["angle"])
                    p["angle_diff"] = round(ang_diff(p["angle_diff"], corr), 2)
            p["corr"] = round(corr, 2)
            if stale:
                p["angle_diff_inst"] = p["angle_diff"]             # в среднее очередь не кладём: оно потом держало бы задержку ещё avg_s секунд
            else:
                self._average(pid, p, sc.get("avg_s", 4.0), now)
            p["learn"] = lrn.info() if lrn else None
            p["flip"] = self._flipping(pid, p, now)
            p["alarm_angle"] = self._alarm_keep(pid, "angle") if stale or p["flip"] else self._alarm(pid, "angle", p["tol_angle"], p["angle_diff"])
            p["alarm_rpm"] = self._alarm(pid, "rpm", p["tol_rpm"], p["rpm_diff"])
            if p["valid"]:
                coast = bool(sa["coast"] or sb["coast"] or stale)
                self.hist[pid].append((now, p["angle_diff"], p["rpm_diff"], coast, p["raw"], p["angle_diff_raw"], p["corr"]))
        self._learn_save(now)
        snap["timing"] = self._timing(cfg, states, snap, at is not None, now, t_cmp)
        self.snap = snap
        mb = cfg["modbus"]
        if not mb.get("enable"):
            return
        writes = []             # (ключ, kind, адрес, значение)
        for sid in SHAFT_ORDER:                                  # скорость вала ×100, 0 — нет данных (как у rpm_out зоны)
            out = sc["shafts"][sid].get("out_rpm")
            if out:
                v = snap["shafts"][sid]["rpm"]
                writes.append(("s:" + sid, out["kind"], out["address"], int(round((v or 0.0) * 100))))
        for pid in PAIR_ORDER:
            pc, p = sc["pairs"][pid], snap["pairs"][pid]
            if pc.get("out_angle"):                              # разность углов ×10 (0,1°)
                writes.append(("a:" + pid, pc["out_angle"]["kind"], pc["out_angle"]["address"],
                               int(round((p["angle_diff"] or 0.0) * 10))))
            if pc.get("out_rpm"):                                # разность скоростей ×100
                writes.append(("r:" + pid, pc["out_rpm"]["kind"], pc["out_rpm"]["address"],
                               int(round((p["rpm_diff"] or 0.0) * 100))))
        todo = []
        for key, kind, addr, val in writes:
            val &= 0xFFFF
            prev = self._sent.get(key)
            if prev and prev[0] == (kind, addr, val) and now - prev[1] < mb["write_period_s"]:
                continue
            todo.append((key, (kind, addr, val)))
        if todo:
            link = self.mgr.links.get(mb["host"], mb["port"], mb["device_id"])
            ok = link.write([it for _, it in todo])
            for key, it in todo:
                if ok:
                    self._sent[key] = (it, now)
                self.status[key] = {"ok": ok, "error": "" if ok else link.last_error}
        snap["plc"] = {"enable": True, "outputs": len(writes), "ok": all(v.get("ok") for v in self.status.values()) if self.status else None}

    def run(self):
        while not self._halt.wait(1.0):
            try:
                self.tick()
            except Exception:
                log.exception("валы: сбой расчёта")
