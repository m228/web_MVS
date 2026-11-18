"""Автоподгонка зоны-палки (CycleRT) по записи её вращения: цвета красной/синей половин, центр оси и радиус.

Зачем: после сдвига камеры или смены света цвета и ось подбирали руками; тут то же делает кнопка за пару минут.

Как: пишем кадры вращающейся палки (1-2 оборота), потом ОФЛАЙН перебираем настройки на ОДНИХ И ТЕХ ЖЕ кадрах — оценки сравнимы
(онлайн «минута на точку» нечестна: оборот ≈ 75 с, за минуту палка проходит не все углы). Оценка кандидата — боевой разбор
CycleRT.step_profiles по профилям лучей: найденность × охват углов × ровность хода (вал крутится равномерно, ложная неподвижная цель
или шум дают не прямую «развёрнутый угол — время»), обороты вне 0,25..2 об/мин отсекаются. Подбор: цвета (покоординатный спуск) →
центр и радиус (сетка, потом уточнение) → цвета ещё раз. Результат проверяется на отложенных кадрах, не участвовавших в подборе;
хуже прежнего — не предлагаем. Применяет результат человек (кнопка в панели), не сам подбор.

Координаты всюду — в системе кадров, которые подали (у сервиса это вырез вокруг оси: смещение и масштаб к кадру камеры — снаружи).
"""
from __future__ import annotations

import time
from typing import Callable, Optional, Sequence

import cv2
import numpy as np

import motion_engine as me

SECTOR_DEG = 30                          # охват углов считаем по секторам такой ширины
NSEC = 360 // SECTOR_DEG
RPM_MIN, RPM_MAX = 0.25, 2.0             # обороты вне диапазона — это не вал (залипание на неподвижной цели, шум)
RED_STEP, RED_LIM = (4, 5, 15, 15), ((4, 30), (140, 178), (10, 200), (10, 160))        # шаг и границы [H_max, H_min, S, V]
BLUE_STEP, BLUE_LIM = (6, 8, 15, 15), ((60, 110), (100, 150), (10, 200), (10, 160))    # [H_lo, H_hi, S, V]
THIN_LIM = (3, 15)
BLUE_ALONE_OPTS = (0.3, 0.45, 0.6, 0.8)
RADIUS_FACTORS = (0.6, 0.75, 0.9, 1.0, 1.2, 1.4, 1.6, 1.8)
RADIUS_MAX_K = 1.8                       # во сколько раз радиус может вырасти относительно заданного (вырез записи должен это вмещать)
VAL_BLOCK, VAL_EVERY = 10, 3             # отложенные кадры: каждый третий блок из 10 кадров (охватывают все углы)
PROD_DT = 0.43                           # шаг кадров боевого анализа (≈2,3 к/с): окно поиска «рядом с ожидаемым углом» рассчитано на него
JUMP_MAX = 60.0
MIN_GAIN = 0.02                          # на сколько оценка на отложенных кадрах должна вырасти, чтобы предлагать результат

_PHIS = np.arange(0, 360, 1.0)
_SIN, _COS = np.sin(np.radians(_PHIS))[:, None], np.cos(np.radians(_PHIS))[:, None]


class Cancelled(Exception):
    """Подгонку отменили (кнопка «Отмена» или остановка сервиса)."""


class JpegStore:
    """Кадры записи в памяти в виде JPEG: вырез 2688×1520 в сыром виде — мегабайты, а на 150 кадров и две оси это сотни МБ."""

    def __init__(self, quality: int = 95):
        self.q = int(quality)
        self.t: list = []
        self._b: list = []

    def add(self, t: float, bgr: np.ndarray) -> None:
        ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, self.q])
        if ok:
            self.t.append(float(t))
            self._b.append(buf.tobytes())

    def __len__(self) -> int:
        return len(self._b)

    def get(self, i: int) -> np.ndarray:
        return cv2.imdecode(np.frombuffer(self._b[i], np.uint8), cv2.IMREAD_COLOR)

    @property
    def nbytes(self) -> int:
        return sum(len(b) for b in self._b)


# ───────────── маски и профили ─────────────
def thin_kernel(thin: int, k: float):
    """Как в CycleRT: «тонкое» задано в рабочем масштабе, а маски считаем в родном — ядро растёт в 1/k раз."""
    if thin < 3:
        return None
    n = max(3, int(round(thin / k))) | 1
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (n, n))


def _compress(m: np.ndarray, k: float) -> np.ndarray:
    """Как в CycleRT._liks: сжимаем маску, а не картинку; доля покрытия пикселя → «есть»; лёгкое утолщение."""
    if k < 1.0:
        m = cv2.resize(m.astype(np.float32), None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
        m = np.minimum(1.0, m * 3.0)
    return cv2.dilate(m.astype(np.float32), me._K3)


def colour_mask(hsv: np.ndarray, kind: str, p: Sequence[int], ker, k: float, fast: bool = True) -> np.ndarray:
    """Маска одной половины палки в рабочем масштабе k (то же, что CycleRT._liks, но один цвет и без повторной конвертации в HSV).
    fast: «открытие» (самое дорогое) считаем на каждом втором пикселе — для перебора; итог всё равно проверяет боевой разбор."""
    if kind == "red":
        hm, hn, s, v = p
        m = cv2.inRange(hsv, (0, s, v), (hm, 255, 255)) | cv2.inRange(hsv, (hn, s, v), (179, 255, 255))
    else:
        b1, b2, s, v = p
        m = cv2.inRange(hsv, (b1, s, v), (b2, 255, 255))
    m = (m != 0).astype(np.uint8)
    if ker is not None:
        if fast and k <= 0.5 and ker.shape[0] >= 15:
            m2 = np.ascontiguousarray(m[::2, ::2])
            k2 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (max(3, ker.shape[0] // 2) | 1,) * 2)
            m2 &= 1 - cv2.morphologyEx(m2, cv2.MORPH_OPEN, k2)
            return _compress(m2, 2.0 * k)
        m &= 1 - cv2.morphologyEx(m, cv2.MORPH_OPEN, ker)
    return _compress(m, k)


def ray_profile(m: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Доля «есть» на каждом из 360 лучей (как в CycleRT.step)."""
    return m[ys, xs].mean(axis=1)


# ───────────── оценка по ходу палки ─────────────
def paced_cfg(cfg: dict, times: Sequence[float]) -> dict:
    """Запись идёт раз в секунду, боевой анализ — ≈2,3 раза в секунду: окно поиска палки (jump_deg) растим во столько же раз, иначе на
    разреженных кадрах разбор теряет палку, которую в бою держит."""
    if len(times) < 2:
        return cfg
    dt = float(np.median(np.diff(np.asarray(times, float))))
    return dict(cfg, jump_deg=float(min(JUMP_MAX, max(cfg["jump_deg"], cfg["jump_deg"] * dt / PROD_DT))))


def run_tracker(cfg: dict, sr: Sequence[np.ndarray], sb: Sequence[np.ndarray], times: Sequence[float]):
    """Боевой разбор CycleRT по готовым профилям: (найдена[bool], угол[°], src[str])."""
    z = me.CycleRT(paced_cfg(cfg, times), None, (4000, 4000))
    n = len(times)
    found, ang, src = np.zeros(n, bool), np.zeros(n), []
    for i in range(n):
        found[i] = z.step_profiles(sr[i], sb[i], float(times[i]))
        ang[i] = z.angle
        src.append(z.src)
    return found, ang, src


def score_track(found: np.ndarray, ang: np.ndarray, times: Sequence[float], sel: Optional[np.ndarray] = None) -> dict:
    """Найденность × охват углов × ровность хода. Всё 0..1; score = (0,5·найдено + 0,5·охват) · ровность · (обороты в диапазоне).
    Разбор палки шёл по ВСЕМ кадрам подряд (на разорванной ленте он теряет палку), а считаем только по кадрам sel (подбор / отложенные)."""
    t_all = np.asarray(times, float)
    n = len(found)
    sel = np.ones(n, bool) if sel is None else np.asarray(sel, bool)
    ns = max(1, int(sel.sum()))
    idx = np.flatnonzero(found)
    out = {"found": float(found[sel].sum()) / ns, "cov": 0.0, "worst": 0.0, "dev": 180.0, "rpm": 0.0, "revs": 0.0, "score": 0.0,
           "sectors": [0.0] * NSEC}
    if len(idx) < max(8, int(0.1 * n)):
        return out
    t = t_all[idx] - t_all[idx[0]]
    a = np.degrees(np.unwrap(np.radians(ang[idx])))
    b, a0 = np.polyfit(t, a, 1)
    res = a - (a0 + b * t)
    keep = np.abs(res) <= max(3.0 * float(np.median(np.abs(res))), 4.0)        # выбросы (ложные пики) подгонку прямой не тянут
    if keep.sum() >= 8:
        b, a0 = np.polyfit(t[keep], a[keep], 1)
        res = a - (a0 + b * t)
    in_sel = sel[idx]
    dev = float(np.median(np.abs(res[in_sel]))) if in_sel.any() else 180.0     # типичный сход с прямой, градусы (перспектива даёт 3-5°)
    rpm = float(b) / 6.0
    pred = (a0 + b * (t_all - t_all[idx[0]])) % 360.0                           # где палка «должна быть» и на потерянных кадрах
    sec = (pred // SECTOR_DEG).astype(int) % NSEC
    cov = []
    for s_ in range(NSEC):
        w = (sec == s_) & sel
        cov.append(float(found[w].mean()) if w.sum() >= 3 else float("nan"))
    cv_ = np.array(cov)
    ok = ~np.isnan(cv_)
    cover = float(cv_[ok].mean()) if ok.any() else 0.0
    worst = float(np.sort(cv_[ok])[:3].mean()) if ok.any() else 0.0
    q = 1.0 / (1.0 + (dev / 6.0) ** 2)
    gate = 1.0 if RPM_MIN <= abs(rpm) <= RPM_MAX else 0.05
    out.update(cov=cover, worst=worst, dev=dev, rpm=rpm, revs=abs(float(a[-1] - a[0])) / 360.0,
               score=(0.5 * out["found"] + 0.5 * cover) * q * gate,
               sectors=[0.0 if np.isnan(x) else x for x in cov])
    return out


def split_frames(n: int) -> tuple:
    """(подбор, отложенные): блоки по VAL_BLOCK кадров, каждый VAL_EVERY-й — отложенный. Отложенные охватывают все углы вала."""
    tr, va = [], []
    for i in range(n):
        (va if (i // VAL_BLOCK) % VAL_EVERY == VAL_EVERY - 1 else tr).append(i)
    return tr, va


def _clamp(v, lo, hi):
    return int(min(hi, max(lo, int(round(v)))))


# ───────────── подгонка ─────────────
class RodTuner:
    """Подгонка одной палки. store — JpegStore (или что угодно с t, __len__, get(i)); base — полный конфиг зоны-цикла
    (как в normalize_camera); center/radius — стартовые, в координатах кадров store; max_radius — потолок радиуса
    (напр. 0,49 расстояния между осями: чужая палка в луч не попадает)."""

    def __init__(self, store, base: dict, center: Sequence[float], radius: float, max_radius: Optional[float] = None,
                 progress: Optional[Callable] = None, cancelled: Optional[Callable] = None):
        self.store, self.base = store, dict(base)
        self.times = list(store.t)
        self.n = len(store)
        self.W = self.H = None
        self.cur = {"center": [float(center[0]), float(center[1])], "radius": float(radius),
                    "red_hsv": list(base["red_hsv"]), "blue_hsv": list(base["blue_hsv"]),
                    "thin": int(base["thin"]), "blue_alone": float(base["blue_alone"])}
        self.old = {k: (list(v) if isinstance(v, list) else v) for k, v in self.cur.items()}
        self.max_radius = float(max_radius) if max_radius else RADIUS_MAX_K * float(radius)
        self.train, self.val = split_frames(self.n)
        self._progress, self._cancelled = progress, cancelled
        self.log: list = []

    # — служебное —
    def _tick(self, frac: float, text: str):
        if self._cancelled and self._cancelled():
            raise Cancelled()
        if self._progress:
            self._progress(max(0.0, min(1.0, frac)), text)

    def _cfg(self, P: dict) -> dict:
        return dict(self.base, center=list(P["center"]), radius=float(P["radius"]), red_hsv=list(P["red_hsv"]), blue_hsv=list(P["blue_hsv"]),
                    thin=int(P["thin"]), blue_alone=float(P["blue_alone"]))

    def _sub(self, idx: Sequence[int], cap: int) -> list:
        step = max(1, int(np.ceil(len(idx) / cap)))
        return list(idx)[::step]

    def _score(self, cfg: dict, sr: list, sb: list, times: list, sel: np.ndarray) -> dict:
        found, ang, _ = run_tracker(cfg, sr, sb, times)
        return score_track(found, ang, times, sel)

    def _frames(self, cap: int) -> tuple:
        """Кадры для перебора: равномерно прореженная ВСЯ запись (лента без дыр) и отметка, какие из них «подбор» (остальные отложены)."""
        idx = self._sub(range(self.n), cap)
        tr = set(self.train)
        return idx, np.array([i in tr for i in idx], bool)

    # — боевая оценка (настоящий CycleRT.step на кадрах) —
    def evaluate(self, P: dict, tag: str = "") -> dict:
        """Боевой прогон по всем кадрам подряд: {"all": …, "val": …} — по всем и только по отложенным (их при подборе не использовали)."""
        idx = list(range(self.n))
        cfg = paced_cfg(self._cfg(P), self.times)
        frame0 = self.store.get(idx[0])
        h, w = frame0.shape[:2]
        z = me.CycleRT(cfg, None, (w, h))
        found, ang = np.zeros(len(idx), bool), np.zeros(len(idx))
        src = []
        for j, i in enumerate(idx):
            f = frame0 if j == 0 else self.store.get(i)
            found[j] = z.step(f, self.times[i])
            ang[j] = z.angle
            src.append(z.src)
            if j % 10 == 0:
                self._tick(getattr(self, "_p0", 0.0) + getattr(self, "_pw", 0.0) * j / max(1, len(idx)), tag)
        times = [self.times[i] for i in idx]
        isval = np.zeros(self.n, bool)
        isval[self.val] = True
        out = {"all": score_track(found, ang, times), "val": score_track(found, ang, times, isval) if self.val else None}
        out["all"]["both"] = sum(1 for s_ in src if s_ == "both") / max(1, len(src))
        if out["val"] is None:
            out["val"] = out["all"]
        return out

    # — этап «цвета» —
    def stage_colors(self, scale: float, p0: float, pw: float, label: str) -> dict:
        """Покоординатный шаг по цветам красного/синего, тонкости и blue_alone; профили всех вариантов — за один проход по кадрам."""
        P = self.cur
        idx, sel = self._frames(110)
        times = [self.times[i] for i in idx]
        cfg0 = self._cfg(P)
        f0 = self.store.get(idx[0])
        h, w = f0.shape[:2]
        z = me.CycleRT(cfg0, None, (w, h))
        ker = z.ker

        def vary(base, steps, lims):
            out = []
            for j in range(4):
                for sgn in (-1, 1):
                    q = list(base)
                    q[j] = _clamp(base[j] + sgn * max(1, round(steps[j] * scale)), *lims[j])
                    if q != list(base) and q not in out:
                        out.append(q)
            return out

        red_opts = [list(P["red_hsv"])] + [q for q in vary(P["red_hsv"], RED_STEP, RED_LIM) if q[0] < q[1]]
        blue_opts = [list(P["blue_hsv"])] + [q for q in vary(P["blue_hsv"], BLUE_STEP, BLUE_LIM) if q[0] < q[1]]
        thins = [t for t in (P["thin"] - 2, P["thin"] + 2) if THIN_LIM[0] <= t <= THIN_LIM[1]]
        SR = {tuple(q): [] for q in red_opts}
        SB = {tuple(q): [] for q in blue_opts}
        TH = {t: ([], [], thin_kernel(t, z.k)) for t in thins}
        for n_, i in enumerate(idx):
            bgr = f0 if n_ == 0 else self.store.get(i)
            hsv = cv2.cvtColor(bgr[z.y0:z.y1, z.x0:z.x1], cv2.COLOR_BGR2HSV)
            for q in red_opts:
                SR[tuple(q)].append(ray_profile(colour_mask(hsv, "red", q, ker, z.k), z.xs, z.ys))
            for q in blue_opts:
                SB[tuple(q)].append(ray_profile(colour_mask(hsv, "blue", q, ker, z.k), z.xs, z.ys))
            for t, (a, b, kk) in TH.items():
                a.append(ray_profile(colour_mask(hsv, "red", P["red_hsv"], kk, z.k), z.xs, z.ys))
                b.append(ray_profile(colour_mask(hsv, "blue", P["blue_hsv"], kk, z.k), z.xs, z.ys))
            self._tick(p0 + pw * (n_ + 1) / len(idx), label)
        base_sc = self._score(cfg0, SR[tuple(P["red_hsv"])], SB[tuple(P["blue_hsv"])], times, sel)
        best = base_sc
        best_red, best_blue = list(P["red_hsv"]), list(P["blue_hsv"])
        for q in red_opts[1:]:
            s = self._score(dict(cfg0, red_hsv=q), SR[tuple(q)], SB[tuple(P["blue_hsv"])], times, sel)
            if s["score"] > best["score"] + 1e-9:
                best, best_red = s, list(q)
        for q in blue_opts[1:]:
            s = self._score(dict(cfg0, red_hsv=best_red, blue_hsv=q), SR[tuple(best_red)], SB[tuple(q)], times, sel)
            if s["score"] > best["score"] + 1e-9:
                best, best_blue = s, list(q)
        new_thin = P["thin"]
        for t, (a, b, _kk) in TH.items():                       # тонкость проверяем на прежних цветах: заметно лучше — берём, цвета уточнит следующий круг
            s = self._score(dict(cfg0, thin=t), a, b, times, sel)
            if s["score"] > best["score"] + 0.01:
                best, new_thin = s, t
        if new_thin != P["thin"]:
            best_red, best_blue = list(P["red_hsv"]), list(P["blue_hsv"])       # профили при новой тонкости посчитаны на прежних цветах
        sr_best = SR[tuple(best_red)] if new_thin == P["thin"] else TH[new_thin][0]
        sb_best = SB[tuple(best_blue)] if new_thin == P["thin"] else TH[new_thin][1]
        ba = P["blue_alone"]
        for v in BLUE_ALONE_OPTS:                              # blue_alone — только в разборе, профили те же
            s = self._score(dict(cfg0, red_hsv=best_red, blue_hsv=best_blue, thin=new_thin, blue_alone=v), sr_best, sb_best, times, sel)
            if s["score"] > best["score"] + 0.005:
                best, ba = s, v
        P["red_hsv"], P["blue_hsv"], P["thin"], P["blue_alone"] = best_red, best_blue, int(new_thin), float(ba)
        self.log.append({"stage": label, "score": round(best["score"], 3), "found": round(best["found"], 3), "cov": round(best["cov"], 3),
                         "red": best_red, "blue": best_blue, "thin": new_thin, "blue_alone": ba, "from": round(base_sc["score"], 3)})
        return best

    # — этап «центр и радиус» —
    def stage_geometry(self, p0: float, pw: float, label: str) -> dict:
        P = self.cur
        R0, (cx0, cy0) = P["radius"], P["center"]
        win = max(40.0, 0.15 * R0)
        Rmax = max(R0, min(self.max_radius, RADIUS_MAX_K * R0))
        half = Rmax + win
        idx, sel = self._frames(90)
        times = [self.times[i] for i in idx]
        f0 = self.store.get(idx[0])
        h, w = f0.shape[:2]
        X0, Y0 = max(0, int(cx0 - half)), max(0, int(cy0 - half))
        X1, Y1 = min(w, int(cx0 + half) + 1), min(h, int(cy0 + half) + 1)
        kB = min(1.0, me.CYCLE_WORK_R / R0)
        ker = thin_kernel(P["thin"], kB)
        cache = []
        for n_, i in enumerate(idx):
            bgr = f0 if n_ == 0 else self.store.get(i)
            hsv = cv2.cvtColor(bgr[Y0:Y1, X0:X1], cv2.COLOR_BGR2HSV)
            cache.append((colour_mask(hsv, "red", P["red_hsv"], ker, kB).astype(np.float32), colour_mask(hsv, "blue", P["blue_hsv"], ker, kB).astype(np.float32)))
            self._tick(p0 + pw * 0.35 * (n_ + 1) / len(idx), label)
        r_min = self.base["r_min"]
        wB = self.base["blue_w"]
        rs = np.arange(max(3.0, R0 * kB * r_min), Rmax * kB, 1.0)
        nr0 = max(2, int((rs <= R0 * kB).sum()))
        sub = cache[::max(1, len(cache) // 40)]                 # перебор центров — по сорока кадрам, хватает

        def maps(cx, cy, nr):
            ox, oy = (cx - X0) * kB, (cy - Y0) * kB
            mx = (ox + rs[None, :nr] * _SIN).astype(np.float32)
            my = (oy - rs[None, :nr] * _COS).astype(np.float32)
            return mx, my

        def proxy(cx, cy):
            mx, my = maps(cx, cy, nr0)
            tot = 0.0
            for red, blue in sub:
                sr = cv2.remap(red, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0).mean(axis=1)
                sb = cv2.remap(blue, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0).mean(axis=1)
                tot += float((sr + wB * np.roll(sb, -180)).max())
            return tot / len(sub)

        best_c, best_v = (cx0, cy0), proxy(cx0, cy0)
        v_start = best_v
        rng, step = win, win / 6.0
        for rnd in range(3):
            g = np.arange(-rng, rng + 1e-6, step)
            cand = [(best_c[0] + dx, best_c[1] + dy) for dx in g for dy in g]
            vals = []
            for j, c in enumerate(cand):
                vals.append(proxy(*c))
                if j % 20 == 0:
                    self._tick(p0 + pw * (0.35 + 0.35 * (rnd + j / len(cand)) / 3.0), label)
            j = int(np.argmax(vals))
            if vals[j] > best_v:
                best_v, best_c = vals[j], cand[j]
            rng, step = step * 1.5, max(1.0, step / 3.0 if rnd == 0 else 1.0)
        # радиус — по оценке хода палки при найденном центре (профили всех радиусов за один проход: суммы вдоль луча)
        nrF = len(rs)
        mx, my = maps(best_c[0], best_c[1], nrF)
        cands = [R for R in (R0 * f for f in RADIUS_FACTORS) if 40.0 <= R <= Rmax + 1e-6]
        if R0 not in cands:
            cands.append(R0)
        cands.sort()
        SRr = {R: [] for R in cands}
        SBr = {R: [] for R in cands}
        lo = {R: int(np.searchsorted(rs, max(3.0, R * kB * r_min))) for R in cands}
        hi = {R: max(lo[R] + 2, int((rs <= R * kB).sum())) for R in cands}
        for red, blue in cache:
            cr = np.cumsum(cv2.remap(red, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0), axis=1)
            cb = np.cumsum(cv2.remap(blue, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0), axis=1)
            for R in cands:
                a, b = lo[R], min(hi[R], nrF)
                base_r = cr[:, a - 1] if a > 0 else 0.0
                base_b = cb[:, a - 1] if a > 0 else 0.0
                SRr[R].append((cr[:, b - 1] - base_r) / max(1, b - a))
                SBr[R].append((cb[:, b - 1] - base_b) / max(1, b - a))
        scored = {R: self._score(dict(self._cfg(P), center=[best_c[0], best_c[1]], radius=R), SRr[R], SBr[R], times, sel) for R in cands}
        top = max(v["score"] for v in scored.values())
        best_R = max(R for R, v in scored.items() if v["score"] >= 0.93 * top)      # из почти лучших — наибольший: больше точек на луче, меньше шума;
        best_s = (scored[best_R]["score"], scored[best_R])                           # слишком короткий радиус боевой разбор оценивает хуже, чем быстрая оценка
        self._tick(p0 + pw, label)
        P["center"], P["radius"] = [float(best_c[0]), float(best_c[1])], float(best_R)
        self.log.append({"stage": label, "score": round(best_s[1]["score"], 3), "found": round(best_s[1]["found"], 3), "cov": round(best_s[1]["cov"], 3),
                         "center": [round(best_c[0], 1), round(best_c[1], 1)], "radius": round(best_R, 1),
                         "shift_px": round(float(np.hypot(best_c[0] - cx0, best_c[1] - cy0)), 1), "proxy": [round(v_start, 3), round(best_v, 3)]})
        return best_s[1]

    def _colour_rounds(self, p0: float, pw: float, scales: Sequence[float]):
        for k, scale in enumerate(scales):
            was = (list(self.cur["red_hsv"]), list(self.cur["blue_hsv"]), self.cur["thin"], self.cur["blue_alone"])
            self.stage_colors(scale, p0 + pw * k / len(scales), pw / len(scales), "цвета, круг %d" % (len(self.log) + 1))
            if was == (self.cur["red_hsv"], self.cur["blue_hsv"], self.cur["thin"], self.cur["blue_alone"]):
                break

    # — весь подбор —
    def run(self) -> dict:
        t0 = time.time()
        self._tick(0.0, "до подгонки")
        self._p0, self._pw = 0.0, 0.15
        before = self.evaluate(self.old, "до подгонки")
        self.stage_colors(1.0, 0.15, 0.12, "цвета, круг 1")
        self.stage_geometry(0.27, 0.13, "центр и радиус")
        self._colour_rounds(0.40, 0.17, (1.0, 0.6))                 # после смены оси цвета уточняем, пока что-то улучшается
        self.cur["radius"] = self.old["radius"]                     # радиус заново от заданного: с нормальными цветами оценка радиуса надёжнее
        self.stage_geometry(0.57, 0.12, "центр и радиус, уточнение")
        self._colour_rounds(0.69, 0.04, (0.4,))
        self._p0, self._pw = 0.73, 0.27
        after = self.evaluate(self.cur, "после подгонки")
        before_all, before_val, after_all, after_val = before["all"], before["val"], after["all"], after["val"]
        gain = after_val["score"] - before_val["score"]
        # предлагаем, если на всей записи лучше, а на отложенных кадрах не хуже (отложенных мало: у уже неплохой палки они могут оказаться все лёгкими)
        improved = after_all["score"] - before_all["score"] >= MIN_GAIN and gain >= -0.02 and after_val["found"] >= before_val["found"] - 0.03
        self._tick(1.0, "готово")
        return {"old": self.old, "new": self.cur, "before": before_all, "after": after_all, "before_val": before_val, "after_val": after_val,
                "gain_val": round(gain, 3), "improved": bool(improved), "log": self.log, "seconds": round(time.time() - t0, 1), "frames": self.n}
