"""render — ролик варки: фото пробы с контурами + рассев (9 групп, как на тренде web_MVS) + графики размера, муки и СВ.

Читает только папки проб web_MVS (cv_results\\<серийник>\\<ts>\\): result.json, frame_N.png|jpg, objects_N.json.
Из web_MVS ничего не импортирует — папку video_service можно переложить куда угодно.

Проверка из командной строки:
    python render.py frame <папка с пробами> [--probe N] [--out кадр.png]   — один кадр (N-я проба, по умолчанию последняя)
    python render.py video <папка с пробами> [--out ролик.mp4]              — весь ролик (по умолчанию в video\\)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

import cv2
import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "video"
W, H = 1920, 1080
FPS = 30
PER_PROBE_S = 0.5            # сколько секунд на пробу
INTRO_S, OUTRO_S = 2.0, 3.0  # заставка и итог
SMOOTH_S = 90.0              # сглаживание рассева, размера и муки: σ гауссова окна по времени, с (0 — сырые значения проб)

# --- цвета (тёмная тема, как в web_MVS); холст — RGB ---
BG = (13, 17, 23)
PANEL = (22, 27, 34)
BORDER = (48, 56, 68)
TEXT = (230, 237, 243)
MUTED = (139, 148, 158)
DIM = (70, 78, 90)
FINES_COL = "#c98a1e"

# контуры — те же цвета, что в окне «Комп. зрение» web_MVS (02-cv.js CV_GROUP_COLOR / CV_REASON_COLOR)
GROUP_COLOR = {"small": "#1d9e75", "medium": "#378add", "large": "#ba7517", "reject": "#e24b4a", "suspect": "#6ad1f5",
               "cut": "#9aa3ad", "bubble": "#e0b04a"}
REASON_COLOR = {"aggregate": "#ec5fa6", "needle": "#ff7a2f", "crooked": "#9a6cf0"}
LEGEND = [("малая", GROUP_COLOR["small"]), ("средняя", GROUP_COLOR["medium"]), ("большая", GROUP_COLOR["large"]),
          ("сросток", REASON_COLOR["aggregate"]), ("игла", REASON_COLOR["needle"]), ("кривой", REASON_COLOR["crooked"]),
          ("мелочь", GROUP_COLOR["reject"]), ("вытянутый", GROUP_COLOR["suspect"]), ("у края кадра", GROUP_COLOR["cut"])]

# рассев: 9 групп из 21 фракции по 0,05 мм — как серии fsv0–fsv8 тренда (cv_store.FSV_GROUPS)
SIEVE_GROUPS = [(0, 1), (1, 2), (2, 3), (3, 4), (4, 8), (8, 12), (12, 16), (16, 20), (20, 21)]
SIEVE_LABELS = ["0 – 0,05", "0,05 – 0,10", "0,10 – 0,15", "0,15 – 0,20", "0,20 – 0,40", "0,40 – 0,60", "0,60 – 0,80",
                "0,80 – 1,0", "> 1,0"]
SIEVE_COLORS = ["#fff0a8", "#f5d861", "#e8b93c", "#c98a1e", "#1d9e75", "#378add", "#7f77dd", "#d16fb8", "#ba7517"]
FINES_ROWS = 4                       # первые 4 группы (до 0,20 мм) — мука

SERIES = [  # (ключ, подпись, единица, знаков, цвет) — цвета как у серий тренда web_MVS
    ("mean", "Средний размер", "мкм", 0, "#7f77dd"),
    ("fines", "Мука — мелкие кристаллы", "% объёма", 1, "#ff5c5c"),
    ("sv", "СВ (сухие вещества)", "%", 1, "#f2c94c"),
]

STAGE_NAMES = {3: "сгущение", 4: "затравка", 5: "подкачка", 6: "стабилизация", 7: "рост", 8: "уваривание",
               9: "готовность", 10: "выгрузка", 20: "УНВ", 21: "пауза"}
PHASE_TITLES = {"p1": "подкачка 1", "g1": "рост 1", "p2": "подкачка 2", "g2": "рост 2"}
STAGE_COLORS = {"подкачка": "#3a6ea5", "рост": "#2f8f6b", "стабилизация": "#5a6b8c", "уваривание": "#b07a2a",
                "готовность": "#b8484a", "затравка": "#7a5aa8", "сгущение": "#6b7a8f"}

FONT_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"

# раскладка кадра
PH_X, PH_Y, PH_W, PH_H = 40, 112, 1000, 837            # фото: пропорции кадра камеры 2448×2048
RX0, RX1 = 1080, 1880                                  # правая колонка
SV_Y0, SV_Y1 = 112, 590                                # рассев
BX0, BX1 = RX0 + 190, RX1 - 110                        # полосы рассева
ROW_Y0, ROW_H = SV_Y0 + 92, 40
CH_Y0, CH_Y1 = 612, 949                                # графики
CH_H = (CH_Y1 - CH_Y0 - 20) // 3
GX0, GX1 = RX0 + 24, RX1 - 24
TL_Y, TX0, TX1 = 990, 40, 1880                         # полоса стадий


def _rgb(c) -> tuple:
    if not isinstance(c, str):
        return tuple(c)
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _mix(a, b, k: float) -> tuple:
    """Смесь цветов: k — доля b."""
    a, b = _rgb(a), _rgb(b)
    return tuple(round(x * (1 - k) + y * k) for x, y in zip(a, b))


@lru_cache(maxsize=64)
def _font(size: int, weight: str = "regular") -> ImageFont.FreeTypeFont:
    names = {"regular": ["segoeui.ttf", "arial.ttf"], "semi": ["seguisb.ttf", "segoeuib.ttf", "arialbd.ttf"],
             "bold": ["segoeuib.ttf", "arialbd.ttf"]}[weight]
    for n in names:
        p = FONT_DIR / n
        if p.exists():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default(size)


def _num(v: Optional[float], dec: int) -> str:
    if v is None:
        return "—"
    return (f"{v:.{dec}f}" if dec else str(int(round(v)))).replace(".", ",")


def _lerp(a: Optional[float], b: Optional[float], e: float) -> Optional[float]:
    if a is None:
        return b
    if b is None:
        return a
    return a + (b - a) * e


def _ease(x: float) -> float:          # плавный разгон и торможение
    x = max(0.0, min(1.0, x))
    return 1 - (1 - x) ** 3


def _dur(s: float) -> str:             # 5520 → «1 ч 32 мин»
    m = round(s / 60)
    return f"{m} мин" if m < 60 else f"{m // 60} ч" + (f" {m % 60} мин" if m % 60 else "")


# ================= данные проб =================

@dataclass
class Probe:
    ts: str
    t: float
    dir: Path
    sv: Optional[float]
    stage: Optional[int]
    phase: Optional[str]
    sieve: Optional[list]            # 9 групп, % объёма
    mean: Optional[float]
    fines: Optional[float]
    frame: Optional[Path] = None
    objects: Optional[Path] = None
    vals: dict = field(default_factory=dict)
    disp: dict = field(default_factory=dict)       # что показываем в ролике (после сглаживания): mean, fines, sv
    disp_sieve: Optional[list] = None

    @property
    def label(self) -> str:          # что сейчас идёт: фаза рецепта или стадия варки
        if self.phase in PHASE_TITLES:
            return PHASE_TITLES[self.phase]
        return STAGE_NAMES.get(self.stage, f"стадия {self.stage}" if self.stage is not None else "—")


def _avg3(d: dict) -> Optional[float]:
    v = [d.get(m) for m in ("m1", "m2", "m3") if d.get(m) is not None]
    return sum(v) / len(v) if v else None


def _sieve9(sf: Optional[dict]) -> Optional[list]:
    """21 фракция по моделям M1–M3 → 9 групп: каждая фракция — среднее моделей, группа — сумма своих фракций."""
    if not sf or not sf.get("m3"):
        return None
    bins = []
    for i in range(21):
        v = [sf[m][i] for m in ("m1", "m2", "m3") if sf.get(m) and len(sf[m]) > i and sf[m][i] is not None]
        bins.append(sum(v) / len(v) if v else None)
    out = []
    for a, b in SIEVE_GROUPS:
        v = [x for x in bins[a:b] if x is not None]
        out.append(sum(v) if v else None)
    return out


def _find_frame(d: Path) -> tuple[Optional[Path], Optional[Path]]:
    """Первый кадр пробы, у которого есть и картинка, и контуры."""
    for i in range(16):
        obj = d / f"objects_{i}.json"
        if not obj.exists():
            continue
        for ext in ("png", "jpg"):
            fr = d / f"frame_{i}.{ext}"
            if fr.exists():
                return fr, obj
    return None, None


def load_probe(d: Path) -> Optional[Probe]:
    try:
        r = json.loads((d / "result.json").read_text(encoding="utf-8"))
        t = time.mktime(time.strptime(r.get("ts") or d.name, "%Y-%m-%d_%H_%M_%S"))
    except Exception:
        return None
    s = r.get("summary") or {}
    vp = s.get("volume_pct") or {}
    fr, obj = _find_frame(d)
    p = Probe(ts=r.get("ts") or d.name, t=t, dir=d, sv=r.get("sv"), stage=r.get("stage"), phase=r.get("phase"),
              sieve=_sieve9(vp.get("sieve_fine")), mean=(s.get("size_um") or {}).get("mean"),
              fines=_avg3(vp.get("flat") or {}), frame=fr, objects=obj)
    p.vals = {"mean": p.mean, "fines": p.fines, "sv": p.sv}
    return p


def load_probes(dirs) -> list[Probe]:
    """Пробы по времени; без кадра с контурами — пропускаем (ролик без фото не нужен)."""
    out = [p for p in (load_probe(Path(d)) for d in dirs) if p and p.frame]
    out.sort(key=lambda p: p.t)
    return out


def probe_dirs(folder: Path) -> list[Path]:
    return sorted(p for p in Path(folder).iterdir() if p.is_dir() and (p / "result.json").exists())


def smooth(probes: list[Probe], sigma_s: float = SMOOTH_S) -> None:
    """Значения для ролика: среднее соседних проб с гауссовым весом по времени (σ = sigma_s). В начале варки пробы частые
    (раз в ~25 с) и шумные — кристаллов мало, доли прыгают между соседними фракциями; окно захватывает ~8 проб. Позже пробы
    реже (раз в ~70 с) — 2–3 пробы, настоящие изменения на подкачке не размываются. СВ — показание прибора, не трогаем."""
    ts = np.array([p.t for p in probes])
    for p in probes:
        p.disp = dict(p.vals)
        p.disp_sieve = p.sieve
        if sigma_s <= 0:
            continue
        w = np.exp(-0.5 * ((ts - p.t) / sigma_s) ** 2)
        near = [(wk, q) for wk, q in zip(w, probes) if wk > 0.01]
        for key in ("mean", "fines"):
            s = [(wk, q.vals[key]) for wk, q in near if q.vals.get(key) is not None]
            if s:
                p.disp[key] = sum(wk * v for wk, v in s) / sum(wk for wk, _ in s)
        sv = [(wk, q.sieve) for wk, q in near if q.sieve]
        if sv:
            tot = sum(wk for wk, _ in sv)
            p.disp_sieve = [sum(wk * (s[k] or 0.0) for wk, s in sv) / tot for k in range(len(SIEVE_GROUPS))]


# ================= рисование =================

def _rrect(cv, x0, y0, x1, y1, r, color, fill=True, th=1):
    """Скруглённый прямоугольник на холсте."""
    color = _rgb(color)
    if fill:
        cv2.rectangle(cv, (x0 + r, y0), (x1 - r, y1), color, -1)
        cv2.rectangle(cv, (x0, y0 + r), (x1, y1 - r), color, -1)
        for cx, cy in ((x0 + r, y0 + r), (x1 - r, y0 + r), (x0 + r, y1 - r), (x1 - r, y1 - r)):
            cv2.circle(cv, (cx, cy), r, color, -1, cv2.LINE_AA)
    else:
        cv2.line(cv, (x0 + r, y0), (x1 - r, y0), color, th, cv2.LINE_AA)
        cv2.line(cv, (x0 + r, y1), (x1 - r, y1), color, th, cv2.LINE_AA)
        cv2.line(cv, (x0, y0 + r), (x0, y1 - r), color, th, cv2.LINE_AA)
        cv2.line(cv, (x1, y0 + r), (x1, y1 - r), color, th, cv2.LINE_AA)
        for (cx, cy), a in (((x0 + r, y0 + r), 180), ((x1 - r, y0 + r), 270), ((x0 + r, y1 - r), 90), ((x1 - r, y1 - r), 0)):
            cv2.ellipse(cv, (cx, cy), (r, r), a, 0, 90, color, th, cv2.LINE_AA)


def photo(p: Probe, w: int = PH_W, h: int = PH_H) -> np.ndarray:
    """Кадр пробы, вписанный в w×h, с контурами кристаллов (RGB)."""
    img = cv2.imdecode(np.fromfile(str(p.frame), np.uint8), cv2.IMREAD_COLOR)
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    h0, w0 = img.shape[:2]
    sc = min(w / w0, h / h0)
    nw, nh = round(w0 * sc), round(h0 * sc)
    img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    try:
        objs = json.loads(p.objects.read_text(encoding="utf-8"))
    except Exception:
        objs = []
    SH = 4                                              # субпиксельные координаты для сглаженных линий
    for o in objs:
        poly = o.get("poly") or []
        if len(poly) < 3:
            continue
        g = o.get("group")
        if g in ("cut", "bubble"):
            col = GROUP_COLOR[g]
        elif o.get("members", 1) > 1:
            col = REASON_COLOR["aggregate"]
        elif o.get("defect"):
            col = REASON_COLOR.get(o["defect"], GROUP_COLOR["reject"])
        elif o.get("suspect"):
            col = GROUP_COLOR["suspect"]
        else:
            col = GROUP_COLOR.get(g, "#cccccc")
        pts = np.round(np.array(poly, np.float64) * sc * (1 << SH)).astype(np.int32)
        cv2.polylines(img, [pts], True, _rgb(col), 2, cv2.LINE_AA, SH)
    out = np.empty((h, w, 3), np.uint8)
    out[:] = BG
    y, x = (h - nh) // 2, (w - nw) // 2
    out[y:y + nh, x:x + nw] = img
    return out


class Boil:
    """Варка целиком: шкалы графиков и рассева общие на весь ролик — так видно рост, а не прыгающий масштаб."""

    def __init__(self, probes: list[Probe], smooth_s: float = SMOOTH_S):
        if not probes:
            raise ValueError("нет проб с кадрами")
        self.probes = probes
        self.smooth_s = smooth_s
        smooth(probes, smooth_s)
        self.t0, self.t1 = probes[0].t, max(probes[-1].t, probes[0].t + 60)
        self.span = self.t1 - self.t0
        smax = max((v for p in probes if p.disp_sieve for v in p.disp_sieve if v is not None), default=50.0)
        self.sieve_max = max(10.0, math.ceil(smax / 10.0) * 10.0)
        self.ranges = {}
        for key, *_ in SERIES:
            v = [p.disp[key] for p in probes if p.disp.get(key) is not None]
            lo, hi = (min(v), max(v)) if v else (0.0, 1.0)
            pad = max((hi - lo) * 0.1, 0.5 if key == "sv" else 1.0)
            self.ranges[key] = (lo - pad, hi + pad)
        self.segments = self._segments()
        self._static = None

    def _segments(self) -> list[tuple[str, float, float]]:
        """Полоса стадий: подряд идущие пробы с одной подписью — один отрезок (до первой пробы следующего)."""
        segs = []
        for p in self.probes:
            if segs and segs[-1][0] == p.label:
                continue
            if segs:
                segs[-1][2] = p.t
            segs.append([p.label, p.t, self.t1])
        return [tuple(s) for s in segs]

    def title(self) -> str:
        a, b = time.localtime(self.t0), time.localtime(self.t1)
        return f"Варка {time.strftime('%d.%m.%Y', a)} · {time.strftime('%H:%M', a)} – {time.strftime('%H:%M', b)}"

    def file_name(self) -> str:
        a, b = time.localtime(self.t0), time.localtime(self.t1)
        return f"варка_{time.strftime('%Y-%m-%d_%H-%M', a)}__{time.strftime('%H-%M', b)}.mp4"

    def tx(self, t: float) -> float:
        return TX0 + (TX1 - TX0) * (t - self.t0) / self.span

    def gx(self, t: float) -> float:
        return GX0 + (GX1 - GX0) * (t - self.t0) / self.span

    def static(self) -> np.ndarray:
        """Всё, что не меняется по ходу ролика: фон, панели, логотип, заголовки, подписи. Рисуется один раз."""
        if self._static is not None:
            return self._static
        cv = np.empty((H, W, 3), np.uint8)
        cv[:] = BG
        _rrect(cv, PH_X - 1, PH_Y - 1, PH_X + PH_W, PH_Y + PH_H, 6, BORDER, fill=False)
        _rrect(cv, RX0, SV_Y0, RX1, SV_Y1, 14, PANEL)
        for k in range(9):
            y = ROW_Y0 + k * ROW_H
            cv2.rectangle(cv, (BX0, y + 8), (BX1, y + ROW_H - 8), BG, -1)
        fy0, fy1 = ROW_Y0 + 10, ROW_Y0 + FINES_ROWS * ROW_H - 10
        cv2.line(cv, (RX0 + 30, fy0), (RX0 + 30, fy1), _rgb(FINES_COL), 2, cv2.LINE_AA)
        _rrect(cv, RX0, CH_Y0, RX1, CH_Y1, 14, PANEL)
        for n in range(3):
            y0 = CH_Y0 + 10 + n * CH_H
            cv2.line(cv, (GX0, y0 + CH_H - 12), (GX1, y0 + CH_H - 12), BORDER, 1, cv2.LINE_AA)
            if n:
                cv2.line(cv, (GX0, y0 - 4), (GX1, y0 - 4), BORDER, 1, cv2.LINE_AA)

        im = Image.fromarray(cv)
        d = ImageDraw.Draw(im)
        logo = _logo(52)
        im.paste(logo, (40, 30), logo)
        lx = 40 + logo.width + 36
        d.text((lx, 26), self.title(), font=_font(32, "semi"), fill=TEXT)
        d.text((lx, 68), "Микроскоп в вакуум-аппарате · распознавание кристаллов", font=_font(20), fill=MUTED)
        lx, ly = PH_X + 4, PH_Y + PH_H + 14
        for name, col in LEGEND:
            d.ellipse((lx, ly + 6, lx + 12, ly + 18), fill=_rgb(col))
            d.text((lx + 18, ly), name, font=_font(18), fill=MUTED)
            lx += 18 + d.textlength(name, font=_font(18)) + 20
        d.text((RX0 + 24, SV_Y0 + 18), "Рассев по объёму", font=_font(28, "semi"), fill=TEXT)
        d.text((RX0 + 24, SV_Y0 + 56), "доля кристаллов каждого размера в общем объёме, %" +
               (" · сглажено по соседним пробам" if self.smooth_s > 0 else ""), font=_font(18), fill=MUTED)
        for k in range(9):
            y = ROW_Y0 + k * ROW_H + ROW_H // 2
            d.text((BX0 - 16, y), SIEVE_LABELS[k], font=_font(20), fill=_rgb("#e8b93c") if k < FINES_ROWS else MUTED, anchor="rm")
        d.text((RX0 + 40, (fy0 + fy1) // 2), "мука", font=_font(16), fill=_rgb(FINES_COL), anchor="lm")
        d.text((BX0 - 16, ROW_Y0 + 9 * ROW_H + 6), "мм", font=_font(16), fill=DIM, anchor="ra")
        for n, (_k, lbl, _u, _d, col) in enumerate(SERIES):
            y0 = CH_Y0 + 10 + n * CH_H
            d.ellipse((GX0, y0 + 12, GX0 + 12, y0 + 24), fill=_rgb(col))
            d.text((GX0 + 20, y0 + 6), lbl, font=_font(20), fill=MUTED)
        d.text((TX0, TL_Y + 40), time.strftime("%H:%M", time.localtime(self.t0)), font=_font(17), fill=MUTED)
        d.text((TX1, TL_Y + 40), time.strftime("%H:%M", time.localtime(self.t1)), font=_font(17), fill=MUTED, anchor="ra")
        self._static = np.asarray(im).copy()
        return self._static


@lru_cache(maxsize=4)
def _logo(h: int) -> Image.Image:
    im = Image.open(HERE / "assets" / "logo_white.png").convert("RGBA")
    return im.resize((round(im.width * h / im.height), h), Image.LANCZOS)


def render_frame(boil: Boil, i: int, e: float = 1.0, ph: Optional[np.ndarray] = None) -> np.ndarray:
    """Кадр ролика (RGB) на пробе i; e — сколько пройдено от пробы i-1 к пробе i (0…1): столбики, числа и линии доезжают плавно."""
    p = boil.probes[i]
    q = boil.probes[i - 1] if i > 0 else p
    t_now = q.t + (p.t - q.t) * e
    cv = boil.static().copy()
    cv[PH_Y:PH_Y + PH_H, PH_X:PH_X + PH_W] = photo(p) if ph is None else ph

    # рассев
    sieve = [_lerp(a, b, e) for a, b in zip(q.disp_sieve or [None] * 9, p.disp_sieve or [None] * 9)]
    for k, v in enumerate(sieve):
        if v:
            y = ROW_Y0 + k * ROW_H
            x = BX0 + round((BX1 - BX0) * min(v, boil.sieve_max) / boil.sieve_max)
            if x > BX0:
                cv2.rectangle(cv, (BX0, y + 8), (x, y + ROW_H - 8), _rgb(SIEVE_COLORS[k]), -1)

    # графики: линия по прошлым пробам + доезжающий конец
    now_vals = {}
    for n, (key, *_r, col) in enumerate(SERIES):
        y0 = CH_Y0 + 10 + n * CH_H
        py0, py1 = y0 + 44, y0 + CH_H - 12
        lo, hi = boil.ranges[key]
        ys = lambda v: py1 - (py1 - py0) * (v - lo) / (hi - lo)
        cur = _lerp(q.disp.get(key), p.disp.get(key), e)
        now_vals[key] = cur
        pts = [(boil.gx(r.t), ys(r.disp[key])) for r in boil.probes[:i] if r.disp.get(key) is not None]
        if cur is not None:
            pts.append((boil.gx(t_now), ys(cur)))
        if pts:
            arr = np.round(np.array(pts) * 16).astype(np.int32)
            cv2.polylines(cv, [arr], False, _rgb(col), 3, cv2.LINE_AA, 4)
            cx, cy = round(pts[-1][0] * 16), round(pts[-1][1] * 16)
            cv2.circle(cv, (cx, cy), 7 * 16, _rgb(col), -1, cv2.LINE_AA, 4)
            cv2.circle(cv, (cx, cy), 7 * 16, BG, 2, cv2.LINE_AA, 4)

    # полоса стадий: пройденное — ярко, впереди — приглушённо
    xn = round(boil.tx(t_now))
    for lbl, a, b in boil.segments:
        x0, x1 = round(boil.tx(a)), round(boil.tx(b))
        base = STAGE_COLORS.get(lbl.split(" ")[0], "#5a6270")
        cv2.rectangle(cv, (x0, TL_Y), (max(x0, x1 - 2), TL_Y + 30), _mix(base, BG, 0.65), -1)
        if xn > x0:
            cv2.rectangle(cv, (x0, TL_Y), (max(x0, min(xn, x1 - 2)), TL_Y + 30), _rgb(base), -1)
    cv2.line(cv, (xn, TL_Y - 8), (xn, TL_Y + 38), TEXT, 2, cv2.LINE_AA)

    # текст, который меняется
    im = Image.fromarray(cv)
    d = ImageDraw.Draw(im)
    d.text((1880, 22), time.strftime("%H:%M", time.localtime(t_now)), font=_font(40, "semi"), fill=TEXT, anchor="ra")
    d.text((1880, 74), f"{p.label} · СВ {_num(now_vals.get('sv'), 1)}", font=_font(22), fill=MUTED, anchor="ra")
    for k, v in enumerate(sieve):
        d.text((RX1 - 22, ROW_Y0 + k * ROW_H + ROW_H // 2), _num(v, 1), font=_font(22, "semi"), fill=TEXT, anchor="rm")
    for n, (key, _l, unit, dec, _c) in enumerate(SERIES):
        y0 = CH_Y0 + 10 + n * CH_H
        d.text((GX1, y0 + 2), f"{_num(now_vals.get(key), dec)} {unit}", font=_font(24, "semi"), fill=TEXT, anchor="ra")
    for lbl, a, b in boil.segments:
        x0, x1 = boil.tx(a), boil.tx(b)
        if d.textlength(lbl, font=_font(17)) + 12 < x1 - x0:
            d.text(((x0 + x1) / 2, TL_Y + 15), lbl, font=_font(17), fill=TEXT, anchor="mm")
    return np.asarray(im)


def intro_frame(boil: Boil) -> np.ndarray:
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    logo = _logo(110)
    im.paste(logo, ((W - logo.width) // 2, 300), logo)
    d.text((W // 2, 520), "Как растут кристаллы сахара", font=_font(64, "semi"), fill=TEXT, anchor="mm")
    d.text((W // 2, 600), boil.title(), font=_font(36), fill=TEXT, anchor="mm")
    d.text((W // 2, 660), "Микроскоп в вакуум-аппарате · распознавание кристаллов", font=_font(26), fill=MUTED, anchor="mm")
    return np.asarray(im)


def outro_frame(boil: Boil, last: np.ndarray, k: float) -> np.ndarray:
    """Последний кадр, притушенный на k, и поверх — карточка «Итог варки» (появляется вместе с k)."""
    base = (last.astype(np.float32) * (1 - 0.55 * k)).astype(np.uint8)
    if k <= 0:
        return base
    p = boil.probes[-1]
    card = np.array(base)
    x0, y0, x1, y1 = 460, 330, 1460, 750
    _rrect(card, x0, y0, x1, y1, 20, PANEL)
    im = Image.fromarray(card)
    d = ImageDraw.Draw(im)
    d.text((W // 2, y0 + 60), "Итог варки", font=_font(44, "semi"), fill=TEXT, anchor="mm")
    v = p.disp                                         # те же числа, что были на экране перед итогом
    items = [("длительность", _dur(boil.t1 - boil.t0), TEXT), ("средний размер", f"{_num(v.get('mean'), 0)} мкм", _rgb(SERIES[0][4])),
             ("мука", f"{_num(v.get('fines'), 1)} %", _rgb(SERIES[1][4])), ("СВ на финише", _num(v.get('sv'), 1), _rgb(SERIES[2][4]))]
    cw = (x1 - x0) / len(items)
    for n, (lbl, val, col) in enumerate(items):
        cx = x0 + cw * (n + 0.5)
        d.text((cx, y0 + 200), val, font=_font(46, "semi"), fill=col, anchor="mm")
        d.text((cx, y0 + 262), lbl, font=_font(22), fill=MUTED, anchor="mm")
    d.text((W // 2, y1 - 50), boil.title(), font=_font(22), fill=MUTED, anchor="mm")
    card = np.asarray(im)
    return cv2.addWeighted(base, 1 - k, card, k, 0)


def render_video(probes: list[Probe], out: Path, per_probe: float = PER_PROBE_S, fps: int = FPS, smooth_s: float = SMOOTH_S,
                 progress: Optional[Callable[[int, int], None]] = None) -> Path:
    """Весь ролик в MP4 (H.264, yuv420p — играет везде). progress(сделано_кадров, всего)."""
    boil = Boil(probes, smooth_s)
    n = max(1, round(per_probe * fps))
    n_in, n_out, fade = round(INTRO_S * fps), round(OUTRO_S * fps), max(1, round(0.4 * fps))
    total = n_in + len(probes) * n + n_out
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".part.mp4")              # пишем во временный файл: недоделанный ролик не выглядит готовым
    w = imageio_ffmpeg.write_frames(str(tmp), (W, H), fps=fps, codec="libx264", quality=None, macro_block_size=8,
                                    output_params=["-crf", "20", "-preset", "medium", "-movflags", "+faststart"])
    w.send(None)
    done = 0

    def put(fr: np.ndarray):
        nonlocal done
        w.send(np.ascontiguousarray(fr))
        done += 1
        if progress:
            progress(done, total)

    try:
        # заставка: проявляется из черноты, в конце перетекает в первую пробу
        intro, first_ph = intro_frame(boil), photo(probes[0])
        first = render_frame(boil, 0, 1.0, first_ph)
        black = np.zeros_like(intro)
        for f in range(n_in):
            fr = cv2.addWeighted(black, 1 - min(1.0, (f + 1) / fade), intro, min(1.0, (f + 1) / fade), 0)
            k = (f - (n_in - fade)) / fade
            if k > 0:
                fr = cv2.addWeighted(fr, 1 - k, first, k, 0)
            put(fr)
        # пробы: фото перетекает за 3 кадра (дольше — контуры двух проб двоятся), столбики/линии доезжают за 70 % пробы
        prev_ph, last = None, first
        for i, p in enumerate(probes):
            ph = first_ph if i == 0 else photo(p)
            for f in range(n):
                pe = min(1.0, (f + 1) / 3)
                img = ph if prev_ph is None or pe >= 1 else cv2.addWeighted(prev_ph, 1 - pe, ph, pe, 0)
                last = render_frame(boil, i, _ease((f + 1) / (n * 0.7)), img)
                put(last)
            prev_ph = ph
        # итог: кадр притухает, появляется карточка
        for f in range(n_out):
            put(outro_frame(boil, last, min(1.0, (f + 1) / fade)))
        w.close()
    except BaseException:
        w.close()
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(out)
    return out


def main():
    ap = argparse.ArgumentParser(description="Ролик варки: проверка из командной строки")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("frame", help="один кадр PNG")
    f.add_argument("folder")
    f.add_argument("--probe", type=int, default=-1)
    f.add_argument("--out", default="frame.png")
    v = sub.add_parser("video", help="весь ролик MP4")
    v.add_argument("folder")
    v.add_argument("--out", default="")
    for p in (f, v):
        p.add_argument("--smooth", type=float, default=SMOOTH_S, help="сглаживание, σ в секундах (0 — сырые значения проб)")
    a = ap.parse_args()
    probes = load_probes(probe_dirs(Path(a.folder)))
    if not probes:
        raise SystemExit("в папке нет проб с кадрами и контурами")
    t = time.time()
    if a.cmd == "frame":
        boil = Boil(probes, a.smooth)
        i = a.probe if a.probe >= 0 else len(probes) + a.probe
        Image.fromarray(render_frame(boil, i)).save(a.out)
        print(f"проб {len(probes)} · кадр {i} ({probes[i].ts}) → {a.out} · {time.time() - t:.2f} с")
        return
    out = Path(a.out) if a.out else OUT_DIR / Boil(probes).file_name()
    last = [0.0]

    def prog(done, total):
        if time.time() - last[0] > 5 or done == total:
            last[0] = time.time()
            print(f"  {done}/{total} кадров ({100 * done // total} %)", flush=True)

    render_video(probes, out, smooth_s=a.smooth, progress=prog)
    print(f"проб {len(probes)} → {out} · {time.time() - t:.0f} с")


if __name__ == "__main__":
    main()
