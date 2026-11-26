"""engine — ядро анализа движения по зонам-полигонам (без сети и без потоков: только кадры → состояния → записи в ПЛК).

Из детекторов шахты (shnek_fill_detector) и ленты (belt_detector) вынесено общее: по каждой зоне считаем две метрики —
  flow — оптический поток Farneback (устойчив к мерцанию света и сжатию видео; так работают шахта и лента жома),
  diff — разность текущего и предыдущего кадра в зоне (простой «датчик вращения»; с вычитанием общей смены яркости,
         чтобы мерцание ламп и автоэкспозиция не давали ложного «движения»).
Какая метрика решает — поле zone.metric; вторая всё равно считается и показывается (для калибровки порогов).

Дальше как в старых детекторах: сглаживание → гистерезис on/off + debounce (frames_to_on / frames_to_off) → бит зоны.
Зона type=motion активна при ВЫСОКОЙ метрике (движется), type=presence — при НИЗКОЙ (густой ровный жом гасит поток).

Группы зон (groups): level — уровень жидкости по маске YOLO-модели и зонам-стёклам (motion_level.py, декантатор); sum — сколько зон активно; monotonic — уровень снизу вверх (верхнее окно полное → нижние тоже, шахта);
worktimer — сколько секунд (в % окна) за последнее окно были активны зоны (лента жома).

Выход в ПЛК описывается полями out у зоны / группы / камеры (status_out), compute_writes() превращает состояние в список записей.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import threading
import time
from collections import deque
from typing import Optional

import cv2
import numpy as np

from perf_stats import PERF
from motion_kiln import DEFAULT_CODES as KILN_CODES, STAGES as KILN_STAGES, KilnRT
from motion_level import DEFAULT_URL as LEVEL_URL, GEOMETRIES as LEVEL_GEOMETRIES, LevelRT
from motion_sound import DEFAULT_CODES as SOUND_CODES

CALIB_DT = 0.33          # интервал между кадрами, при котором калибровались пороги потока (камеры ~3 fps)
FB = dict(pyr_scale=0.5, levels=3, winsize=15, iterations=3, poly_n=5, poly_sigma=1.2, flags=0)
MIN_FLOW_SIDE = 16       # зона меньше этого (px) для Farneback не годится — поток = 0
ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")

ZONE_TYPES = ("motion", "presence", "cycle", "level", "clarity", "gate", "tube")
# Уровнемеры — значение 0..100 % по картинке. gate («стрелка», бывший шибер): цветной указатель ездит рядом с линейкой. tube («столбик»): уровень виден
# цветом — от начала шкалы «заполнено», дальше «пусто» (магнитный указатель, стекло). Удержание, «нет данных», сценарии и тренды у них общие.
GAUGE_TYPES = ("gate", "tube")
GAUGE_HOLD_S = 60.0                     # не видно (закрыли, нет кадров, картинка стоит) — держим последнее значение столько секунд, потом «нет данных»
GAUGE_STALE_S = 15.0                    # значение в состоянии камеры старее этого (и «держать») сценарий не берёт: поток камеры мог зависнуть на переоткрытии
GAUGE_SMOOTH = 5                        # значение — медиана стольких последних удачных кадров
GAUGE_GAP_S = 10.0                      # перерыв в удачных кадрах дольше этого — прежние в медиану не идут
GAUGE_STILL_S = 3.0                     # неизменившийся кадр (дубль потока) даёт значение ещё столько секунд и при малом «держать»; дальше — по «держать»
TUBE_DIRECTIONS = ("up", "down", "right", "left")   # куда растёт уровень: снизу вверх / сверху вниз / слева направо / справа налево
TUBE_TOL = 12.0                         # допуск цвета: расстояние в Lab до образца (яркость с весом TUBE_L_WEIGHT)
TUBE_L_WEIGHT = 0.5                     # тень и свет меняют в основном яркость — она в расстоянии весит вдвое меньше оттенка
TUBE_QUALITY = 80                       # столько % строк должно сходиться с «до уровня заполнено, дальше пусто», иначе колонку не видно
TUBE_ROW_PIX = 3                        # меньше стольких точек цвета строку не заполняют (узкая колонка в широком прямоугольнике)
TUBE_ROW_SHARES = (0.0, 0.35, 0.5)      # …и не меньше такой доли от типичной заполненной строки (тень сбоку даёт лишь несколько точек): берётся доля, при которой картина сходится лучше
TUBE_MAX_H, TUBE_MAX_W = 600, 160       # полоска вдоль шкалы не крупнее этого
TUBE_MAX_COLORS = 6
TUBE_MAX_GAP = 0.07                      # границу уровня «размазало» шире этой доли шкалы (часть колонки у границы не видна) — значения нет
GATE_RED_HSV = (10, 165, 100, 70)       # лента указателя [h_max, h_min, S_min, V_min]: строже, чем красный палки, — ржавчину рядом не берём
GATE_MIN_AREA = 200                     # пятно меньше этого (точек кадра разметки) за указатель не принимаем
GATE_CLOSE_DIV = 128                    # разрывы ленты по горизонтали склеиваем ядром «ширина кадра / 128» (15 точек на 1920)
CLARITY_DIRTY_MODES = ("shared", "own")   # «грязный» цвет зоны: общий сдвиг камеры от «чистого» этой зоны / свой собственный
CLARITY_REFS = ("shared", "own")            # эталоны камеры: общие на все стаканы / у каждого свои
CLARITY_NO_DATA = 65535                   # что уходит в ПЛК вместо мутности, когда её нет (нет эталонов, кадра, цвета): 0 — это «чистый»
CLARITY_MIN_SPAN = 3.0                    # «чистый» и «грязный» ближе этого (ΔE по Lab) — шкалы нет, мутность не считаем
CLARITY_LO = 10.0                         # где на шкале 0..100 стоит эталон «чистый»: чище него — ниже 10 (запас вниз)
CLARITY_HI = 90.0                         # где стоит эталон «грязный»: грязнее него — выше 90 (запас вверх)
CLARITY_SCALE = 2                         # версия шкалы зоны: нет поля — старая (чистый 0, грязный 100), пороги при чтении пересчитываются
CLARITY_T_TURB = 20.0                     # пороги по умолчанию на шкале 10/90 (ролик со светом 19.11: чистые ≈10, мутные 1 и 5 ≈ 26–46, грязный 6 ≈ 90)
CLARITY_T_DIRTY = 66.0
CLARITY_HYST = 2.0
CLARITY_SMOOTH_S = 30.0                   # инерция цвета, с: сначала цвет подтягивается к измеренному медленно (постоянная времени S), при устойчивом отклонении разгоняется до S/10.
                                          # Пар, блик, рука не успевают сдвинуть значение; настоящее изменение подхватывается за ~S секунд. 0 — старое среднее за smooth кадров
CLARITY_HOLD_S = 20.0                     # новое состояние должно продержаться столько секунд, прежде чем сменится (0 — сразу)
CLARITY_DEAD_DE = 2.0                     # отклонение цвета от сглаженного меньше этого (ΔE по Lab) — шум, на разгон не работает
CLARITY_LEVELS = ("clear", "turbid", "dirty")   # прозрачный / мутный / грязный
CLARITY_KEEP_S = 600                    # сколько секунд сырых цветов храним для калибровки («чистый»/«грязный» по последним N секундам)
# «Есть ли сок» по движению (22.11): в кружке сок течёт — картинка шевелится; стакан пуст — стоит. Подобрано на роликах пустых стаканов (22.11) и полных (19.11):
# у пустых шум после размытия и усреднения ≈ 0,6–1,0, у стаканов с соком 1,1–4 и выше. Пороги у каждого стакана свои (кнопка «пусто» снимает шум).
CLARITY_MV_HI = 1.1                     # движение от этого — сок есть
CLARITY_MV_LO = 0.9                     # ниже этого — стакан пуст (между порогами состояние не меняется — гистерезис)
CLARITY_MV_WIN_S = 10.0                 # окно кадров, с: делится пополам по числу кадров, сравниваются средние половин
CLARITY_MV_SMOOTH_S = 20.0              # медиана значения движения за столько секунд (против одиночных всплесков)
CLARITY_MV_HOLD_S = 30.0                # новое состояние «сок / пусто» должно продержаться столько секунд
CLARITY_MV_N = 48                       # круг приводится к N×N, чтобы метрика не зависела от разрешения камеры
CLARITY_MV_SIGMA = 2.5                  # размытие (в точках N×N): шум сжатия мелкий, течение сока — крупнее
CLARITY_MV_MIN_FRAMES = 4               # меньше стольких разных кадров в окне — движение не считаем (по два в каждой половине)
CLARITY_MV_DUP_EPS = 0.05               # кадр, отличающийся от прежнего меньше этого (средний |Δ| серого), — дубль потока, в окно не берём
METRICS = ("flow", "diff")
GROUP_TYPES = ("sum", "monotonic", "worktimer", "mirror", "level", "kiln", "kiln_sound")
MIRROR_MODES = ("mirror", "same")
BAN_DEG = 15.0           # полуширина запрета вокруг ложной неподвижной цели палки, градусы
MAX_BANS = 3             # сколько ложных целей помним на палку
CYCLE_WORK_R = 170       # радиус (px), до которого сжимается область вокруг оси палки перед поиском: быстро и не зависит от разрешения камеры
CYCLE_RED_HSV = (16, 165, 50, 55)     # красный: H ≤ h_max или H ≥ h_min (круг оттенков), S ≥ s_min, V ≥ v_min; мягче прежнего: красный конец в сумерках бледнеет и желтеет
CYCLE_RED_HSV_OLD = (12, 163, 70, 80)  # прежний умолчательный красный: зоны, сохранённые с ним нетронутыми, переходят на новый (свои значения не трогаем)
CYCLE_BLUE_HSV = (82, 135, 55, 60)    # синий/голубой: h_lo ≤ H ≤ h_hi, S ≥ s_min, V ≥ v_min (голубая половина тоже сюда)
TIP_DIR_MIN = 0.05       # светоотражатели на обоих концах одинаковы: где красный конец, при поиске по всему кругу подсказывает цвет — его сила на луче не меньше этой
TIP_PREC = 2.0           # вес угла по светоотражателям против угла по цвету при слиянии: кончики на полном радиусе, угол по ним точнее
TIP_FLIP = 0.12          # отражатели не знают, где красный конец: голос цвета «за» противоположный луч (больше этого) или «против» (меньше минус этого)
TIP_FLIP_DEG = 90.0      # стрелку, ведомую отражателями, разворачиваем на 180°, если вал прошёл столько градусов при голосе «за» без единого «против»
TIP_BG_TAU = 30.0        # память неподвижных бликов, с: что светится на одном месте, пока вал крутится, — не кончик (люк, рамка, лампа)
TIP_END_HW = 6          # видимость отражателя каждого конца: сила кончика в ±столько градусов от стрелки (и от обратного луча)
TIP_BG_MAX = 0.35       # точка горит дольше этой доли времени — неподвижный блик, из маски отражателей её убираем
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
CAMERA_ROWS = ("", "lower", "upper")           # ряд валов камеры: не задан / нижний (Н) / верхний (В) — для разностей валов (motion_shafts.py)
SHAFT_IDS = ("nl", "np", "vl", "vp")           # НЛВ, НПВ, ВЛВ, ВПВ
PAIR_IDS = ("nlr", "vlr", "lnv", "rnv")        # НЛВ–НПВ, ВЛВ–ВПВ, НЛВ–ВЛВ, НПВ–ВПВ

# Пороги по умолчанию зависят от метрики: поток — безразмерный (0.3..2), diff — градации серого (после размытия).
THRESH_DEFAULT = {"flow": (0.9, 0.6), "diff": (4.0, 2.0)}

DEFAULT_MODBUS = {"enable": False, "host": "192.168.1.20", "port": 502, "device_id": 1, "write_period_s": 1.0}


def default_config() -> dict:
    return {"version": 1, "modbus": dict(DEFAULT_MODBUS), "cameras": [], "scenarios": [], "shafts": normalize_shafts(None)}


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
    if ztype == "level":
        return _norm_level_zone(z, zid, where)
    if ztype == "clarity":
        return _norm_clarity(z, zid, where)
    if ztype == "gate":
        return _norm_gate(z, zid, where)
    if ztype == "tube":
        return _norm_tube(z, zid, where)
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
    motion = bool(z.get("motion", True))      # False — зона «по яркости»: движение не считается, бит — «есть / нет» по виду окна
    fill = _fill_cfg(z, where)
    if not motion and fill["fill_metric"] == "off":
        raise ValueError("%s: движение выключено — выберите признак яркости (fill_metric)" % where)
    return {
        "id": zid,
        "name": str(z.get("name") or zid),
        "enabled": bool(z.get("enabled", True)),
        "type": ztype,
        "motion": motion,
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
        **fill,
    }


def _norm_level_zone(z: dict, zid: str, where: str) -> dict:
    """Зона-«стекло» для уровня жидкости (декантатор): только полигон; метрик потока/разности и порогов нет. Уровень считает группа type=level
    по маске модели. flow зоны = уровень стекла 0..100 %, diff = сырой уровень до отсечки и сглаживания — оба попадают в тренды и CSV."""
    pts = z.get("points") or []
    if len(pts) < 3:
        raise ValueError("%s: в полигоне меньше трёх точек" % where)
    try:
        pts = [[float(p[0]), float(p[1])] for p in pts]
    except Exception:
        raise ValueError("%s: точки полигона должны быть парами [x, y]" % where)
    return {
        "id": zid,
        "name": str(z.get("name") or zid),
        "enabled": bool(z.get("enabled", True)),
        "type": "level",
        "metric": "flow",
        "points": pts,
        "on": 0.0, "off": 0.0, "invert": False, "track_flow": False,
        "smooth": 1, "frames_to_on": 1, "frames_to_off": 1, "blur": 0, "flicker_comp": False,
        "out": None,
    }


def order_ruler(pts) -> list:
    """Четыре угла линейки в любом порядке → [низ слева, низ справа, верх справа, верх слева] (линейка стоит вертикально)."""
    p = sorted(pts, key=lambda q: q[1])
    top, bot = sorted(p[:2], key=lambda q: q[0]), sorted(p[2:], key=lambda q: q[0])
    return [bot[0], bot[1], top[1], top[0]]


def _norm_gate(z: dict, zid: str, where: str) -> dict:
    """Уровнемер «стрелка» (бывший шибер): ruler — четыре угла линейки (нижняя сторона = 0 %, верхняя = 100 %; flip — наоборот), points — многоугольник, где ходит
    указатель, цвет указателя — теми же полями, что красный конец палки (red_hsv / red_more / red_not). Значение — заполнение линейки 0..100 %
    (flow = %, diff = площадь найденного пятна, bit = значение есть). В ПЛК — только сценарием: % × 10 или целые %, нет данных — −1."""
    def pairs(v, name):
        try:
            return [[float(p[0]), float(p[1])] for p in v]
        except Exception:
            raise ValueError("%s: точки %s должны быть парами [x, y]" % (where, name))
    ruler, pts = pairs(z.get("ruler") or [], "линейки"), pairs(z.get("points") or [], "зоны указателя")
    if len(ruler) != 4:
        raise ValueError("%s: у линейки нужны ровно четыре угла" % where)
    if len(pts) < 3:
        raise ValueError("%s: в зоне указателя меньше трёх точек" % where)
    ruler = order_ruler(ruler)
    bl, br, tr, tl = ruler
    if min(bl[1], br[1]) - max(tl[1], tr[1]) < 4 or br[0] - bl[0] < 1 or tr[0] - tl[0] < 1:
        raise ValueError("%s: линейка должна стоять вертикально: два угла внизу, два вверху" % where)
    cut = pairs(z.get("cut") or [], "выреза линейки")
    if cut and len(cut) != 4:
        raise ValueError("%s: у выреза линейки нужны четыре угла или ни одного" % where)
    return {
        "id": zid,
        "name": str(z.get("name") or zid),
        "enabled": bool(z.get("enabled", True)),
        "type": "gate",
        "metric": "flow",
        "points": pts,
        "on": 0.0, "off": 0.0, "invert": False, "track_flow": False,
        "smooth": _num(z.get("smooth", GAUGE_SMOOTH), where + ".smooth", 1, 100, True),
        "frames_to_on": 1, "frames_to_off": 1, "blur": 0, "flicker_comp": False,
        "out": None,
        "ruler": ruler,
        # линейка, как она висит на кадре (её метки вырезаются из маски); ruler после подгонки «это 0 % / 100 %» — только шкала. None — совпадают.
        # Ключ отдаётся всегда: по нему страница понимает, что сервис вырез не потеряет
        "cut": order_ruler(cut) if cut else None,
        "flip": bool(z.get("flip", False)),
        "hold_s": _num(z.get("hold_s", GAUGE_HOLD_S), where + ".hold_s", 0, 3600),
        "min_area": _num(z.get("min_area", GATE_MIN_AREA), where + ".min_area", 1, 10 ** 7, True),
        "red_hsv": _hsv4(z.get("red_hsv"), GATE_RED_HSV, where + ".red_hsv", True),
        "red_more": _more(z.get("red_more"), where + ".red_more"),
        "red_not": _nots(z.get("red_not"), where + ".red_not"),
    }


def _colors(v, name) -> list:
    """Образцы цвета уровнемера-столбика: [[L, a, b], …] (L 0..100, a и b −128..127), не больше TUBE_MAX_COLORS; пусто — цвета ещё не взяты."""
    if v in (None, ""):
        return []
    if not isinstance(v, (list, tuple)) or len(v) > TUBE_MAX_COLORS:
        raise ValueError("%s: список не длиннее %d цветов" % (name, TUBE_MAX_COLORS))
    out = []
    for c in v:
        x = _lab3(c, name)
        if x is None:
            raise ValueError("%s: нужно три числа [L, a, b]" % name)
        out.append(x)
    return out


def _uv(pts, direction: str) -> np.ndarray:
    """Точки кадра → (u поперёк шкалы, v вдоль неё: растёт от 0 % к 100 %)."""
    p = np.asarray(pts, float).reshape(-1, 2)
    x, y = p[:, 0], p[:, 1]
    u, v = {"up": (x, -y), "down": (x, y), "right": (y, x), "left": (y, -x)}[direction]
    return np.stack([u, v], axis=1)


def _norm_tube(z: dict, zid: str, where: str) -> dict:
    """Уровнемер «столбик»: points — разметка шкалы (4 точки — линии уровня соединяют боковые стороны, учитывается наклон; 3 или 5+ — многоугольник,
    уровень по срезам поперёк направления), direction — куда растёт уровень, fill_colors / empty_colors — образцы цвета «заполнено» и «пусто» (Lab),
    tol — допуск цвета, min_quality — сколько % строк должно сходиться. Значение — 0..100 % разметки (flow = %, diff = качество, bit = значение есть)."""
    try:
        pts = [[float(p[0]), float(p[1])] for p in z.get("points") or []]
    except Exception:
        raise ValueError("%s: точки разметки должны быть парами [x, y]" % where)
    if len(pts) < 3:
        raise ValueError("%s: в разметке меньше трёх точек" % where)
    direction = z.get("direction") or "up"
    if direction not in TUBE_DIRECTIONS:
        raise ValueError("%s: направление %r не из %s" % (where, direction, list(TUBE_DIRECTIONS)))
    uv = _uv(pts, direction)
    if np.ptp(uv[:, 1]) < 4 or np.ptp(uv[:, 0]) < 1:
        raise ValueError("%s: разметка слишком мала вдоль или поперёк шкалы" % where)
    return {
        "id": zid,
        "name": str(z.get("name") or zid),
        "enabled": bool(z.get("enabled", True)),
        "type": "tube",
        "metric": "flow",
        "points": pts,
        "on": 0.0, "off": 0.0, "invert": False, "track_flow": False,
        "smooth": _num(z.get("smooth", GAUGE_SMOOTH), where + ".smooth", 1, 100, True),
        "frames_to_on": 1, "frames_to_off": 1, "blur": 0, "flicker_comp": False,
        "out": None,
        "direction": direction,
        "fill_colors": _colors(z.get("fill_colors"), where + ".fill_colors"),
        "empty_colors": _colors(z.get("empty_colors"), where + ".empty_colors"),
        "tol": _num(z.get("tol", TUBE_TOL), where + ".tol", 1, 60),
        "min_quality": _num(z.get("min_quality", TUBE_QUALITY), where + ".min_quality", 50, 100, True),
        "hold_s": _num(z.get("hold_s", GAUGE_HOLD_S), where + ".hold_s", 0, 3600),
    }


def _lab3(v, name, shift=False):
    """[L, a, b] цвета в пространстве Lab (OpenCV: L 0..100, a и b −127..127) или None. shift — это сдвиг цвета, а не сам цвет (пределы шире)."""
    if v is None or v == "" or v == []:
        return None
    if not isinstance(v, (list, tuple)) or len(v) != 3:
        raise ValueError("%s: нужно три числа [L, a, b]" % name)
    lo, hi = (-200.0, 200.0) if shift else (0.0, 100.0)
    return [round(_num(v[0], name, lo, hi), 2), round(_num(v[1], name, -256 if shift else -128, 256 if shift else 128), 2),
            round(_num(v[2], name, -256 if shift else -128, 256 if shift else 128), 2)]


def _clarity_refs(v) -> str:
    if v in (None, ""):
        return "shared"
    if v not in CLARITY_REFS:
        raise ValueError("clarity_refs %r не из %s" % (v, list(CLARITY_REFS)))
    return v


def _norm_clarity(z: dict, zid: str, where: str) -> dict:
    """Зона-«прозрачность»: круг на стакане с соком (центр + радиус, как у палки). Результат — мутность turb 0..100: эталон «чистый» стоит на 10,
    «грязный» на 90 (оба задаёт оператор; чище — ниже 10, грязнее — выше 90), между ними — по линейке в цветовом пространстве Lab. Полигона и метрик потока нет (points = [],
    flow = turb, diff = движение в кружке (mv_*, «сок есть / пусто»; до 22.11 тут была яркость L) — так они попадают в истории, тренды и CSV). Вывод в ПЛК — только holding."""
    c = z.get("center")
    if not c or len(c) != 2:
        raise ValueError("%s: нужен центр круга [x, y]" % where)
    mode = z.get("dirty_mode", "shared")
    if mode not in CLARITY_DIRTY_MODES:
        raise ValueError("%s: dirty_mode %r не из %s" % (where, mode, list(CLARITY_DIRTY_MODES)))
    t_turb = _num(z.get("t_turb", CLARITY_T_TURB), where + ".t_turb", 0, 100)
    t_dirty = _num(z.get("t_dirty", CLARITY_T_DIRTY), where + ".t_dirty", 0, 100)
    hyst = _num(z.get("hyst", CLARITY_HYST), where + ".hyst", 0, 50)
    if z.get("scale") != CLARITY_SCALE:                  # зона сохранена на старой шкале (чистый 0, грязный 100): пороги переводим на 10/90 один раз
        k = (CLARITY_HI - CLARITY_LO) / 100.0
        if "t_turb" in z:
            t_turb = float(round(CLARITY_LO + k * t_turb))
        if "t_dirty" in z:
            t_dirty = float(round(CLARITY_LO + k * t_dirty))
        if "hyst" in z:
            hyst = round(hyst * k, 1)
    if t_turb >= t_dirty:
        raise ValueError("%s: порог «мутный» должен быть меньше порога «грязный»" % where)
    mv_hi = _num(z.get("mv_hi", CLARITY_MV_HI), where + ".mv_hi", 0.01, 1000)
    mv_lo = _num(z.get("mv_lo", CLARITY_MV_LO), where + ".mv_lo", 0, 1000)
    if mv_lo >= mv_hi:
        raise ValueError("%s: порог «нет сока ниже» должен быть меньше порога «сок есть от»" % where)
    return {
        "id": zid,
        "name": str(z.get("name") or zid),
        "enabled": bool(z.get("enabled", True)),
        "type": "clarity",
        "metric": "flow",
        "points": [],
        "on": 0.0, "off": 0.0, "invert": False, "track_flow": False,
        "smooth": _num(z.get("smooth", 15), where + ".smooth", 1, 600, True),
        "frames_to_on": 1, "frames_to_off": 1, "blur": 0, "flicker_comp": False,
        "out": _out(z.get("out"), ("holding",), where + ".out"),
        "center": [_num(c[0], where + ".center", 0), _num(c[1], where + ".center", 0)],
        "radius": _num(z.get("radius", 40), where + ".radius", 3),
        "core": _num(z.get("core", 0.7), where + ".core", 0.2, 1.0),             # какую часть радиуса берём (без стенки стакана и бликов на краю)
        "glare_pct": _num(z.get("glare_pct", 10), where + ".glare_pct", 0, 50),   # сколько % самых ярких точек круга отбросить (блики, пена)
        "off_max": _num(z.get("off_max", 1.0), where + ".off_max", 0, 100),       # цвет дальше от шкалы (в длинах шкалы) — «не уверен»; 0 — не проверять
        "clean": _lab3(z.get("clean"), where + ".clean"),
        "dirty": _lab3(z.get("dirty"), where + ".dirty"),
        "dirty_mode": mode,
        "scale": CLARITY_SCALE,
        "t_turb": t_turb,                                                                 # с какой мутности сок «мутный»
        "t_dirty": t_dirty,                                                               # с какой — «грязный»
        "smooth_s": _num(z.get("smooth_s", CLARITY_SMOOTH_S), where + ".smooth_s", 0, 600),   # сглаживание цвета (медиана за N секунд)
        "hold_s": _num(z.get("hold_s", CLARITY_HOLD_S), where + ".hold_s", 0, 3600),          # выдержка смены состояния, с
        "hyst": hyst,                                                                     # гистерезис порогов, пункты мутности: против дрожания границы
        "label": _label(z.get("label"), where + ".label"),                                # ручная метка оператора для базы: "" / clear / turbid / dirty
        "mv_on": bool(z.get("mv_on", False)),                                             # определять «сок есть / пусто» по движению (нет движения → мутность 0, кружок серый)
        "mv_hi": mv_hi,                                                                   # движение от этого — сок есть
        "mv_lo": mv_lo,                                                                   # ниже этого — пусто
        "mv_win_s": _num(z.get("mv_win_s", CLARITY_MV_WIN_S), where + ".mv_win_s", 2, 120),
        "mv_smooth_s": _num(z.get("mv_smooth_s", CLARITY_MV_SMOOTH_S), where + ".mv_smooth_s", 0, 600),
        "mv_hold_s": _num(z.get("mv_hold_s", CLARITY_MV_HOLD_S), where + ".mv_hold_s", 0, 3600),
    }


def _label(v, where: str) -> str:
    v = str(v or "")
    if v not in ("", "clear", "turbid", "dirty"):
        raise ValueError("%s: метка %r не из '', clear, turbid, dirty" % (where, v))
    return v


def _juice_base(v, where: str) -> dict:
    """Сбор базы «Сок»: {on, every_min}. Шаг 1..60 минут."""
    v = v or {}
    return {"on": bool(v.get("on", False)), "every_min": _num(v.get("every_min", 2), where + ".every_min", 1, 60, True)}


def _autocal_watch(v, where: str) -> dict:
    """Сторож подгонки валов: {on, min_pct, check_min, cooldown_h}. Видимость ниже min_pct % за check_min минут → сама подгонка (motion_autocal_watch)."""
    v = v or {}
    return {"on": bool(v.get("on", False)),
            "min_pct": _num(v.get("min_pct", 95.0), where + ".min_pct", 50, 100),
            "check_min": _num(v.get("check_min", 30.0), where + ".check_min", 10, 180),
            "cooldown_h": _num(v.get("cooldown_h", 2.0), where + ".cooldown_h", 0.5, 48)}


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


MAX_MORE = 6                                       # сколько дополнительных цветов может быть у каждого конца палки (red_more / blue_more)


def _more(v, name: str) -> list:
    """Дополнительные цвета конца палки: [[H1, H2, S_min, V_min], …] — к основному диапазону (red_hsv / blue_hsv) добавляются «ИЛИ».
    Оттенков красного несколько (светлый, тёмный, розоватый), одним диапазоном их без лишнего не покрыть."""
    if not v:
        return []
    if not isinstance(v, (list, tuple)) or len(v) > MAX_MORE:
        raise ValueError("%s: не больше %d цветов" % (name, MAX_MORE))
    return [_hsv4(x, None, name, False) for x in v]


def _box6(v, name: str) -> list:
    """[H1, H2, S1, S2, V1, V2] — «ящик» цвета: оттенок 0..179 (H1 > H2 — через ноль круга, так лежит красный), насыщенность и яркость 0..255 «от и до»."""
    if not isinstance(v, (list, tuple)) or len(v) != 6:
        raise ValueError("%s: нужно шесть чисел [H1, H2, S1, S2, V1, V2]" % name)
    h1, h2 = _num(v[0], name, 0, 179, True), _num(v[1], name, 0, 179, True)
    s1, s2, v1, v2 = [_num(x, name, 0, 255, True) for x in v[2:]]
    if s1 > s2 or v1 > v2:
        raise ValueError("%s: нижняя граница насыщенности или яркости выше верхней" % name)
    return [h1, h2, s1, s2, v1, v2]


def _nots(v, name: str) -> list:
    """Исключённые цвета конца палки (red_not / blue_not): [[H1, H2, S1, S2, V1, V2], …] — вырезаются из маски этого конца, что бы ни ловили
    основной диапазон и «ещё цвет». Так убирают цвет корпуса, блик или пластину рядом с палкой."""
    if not v:
        return []
    if not isinstance(v, (list, tuple)) or len(v) > MAX_MORE:
        raise ValueError("%s: не больше %d цветов" % (name, MAX_MORE))
    return [_box6(x, name) for x in v]


def hsv_box_hit(h, s, v, box):
    """Булева маска пикселей, попавших в «ящик» [H1, H2, S1, S2, V1, V2]; H1 > H2 — оттенок по кругу через ноль."""
    h1, h2, s1, s2, v1, v2 = box
    hue = ((h >= h1) & (h <= h2)) if h1 <= h2 else ((h >= h1) | (h <= h2))
    return hue & (s >= s1) & (s <= s2) & (v >= v1) & (v <= v2)


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
        "red_more": _more(z.get("red_more"), where + ".red_more"),
        "blue_more": _more(z.get("blue_more"), where + ".blue_more"),
        "red_not": _nots(z.get("red_not"), where + ".red_not"),
        "blue_not": _nots(z.get("blue_not"), where + ".blue_not"),
        "tip_hsv": _nots(z.get("tip_hsv"), where + ".tip_hsv"),          # цвета светоотражателей на концах палки — те же «ящики»; пусто — отражатели не ищем
        "tip_w": _num(z.get("tip_w", 1.0), where + ".tip_w", 0, 3),      # вес отражателей в поиске угла (0 — не использовать)
        "tip_lead": bool(z.get("tip_lead", False)),                      # вести по отражателям: видны — угол по ним, цвет только говорит, где красный конец
        "tip_min": _num(z.get("tip_min", 0.3), where + ".tip_min", 0.05, 1),   # отражатель считается видимым от этой силы (1 — кончик целиком, 0,5 — один из двух)
        "tip_r0": _num(z.get("tip_r0", 0.55), where + ".tip_r0", 0.2, 1.0),    # кольцо, где ищем кончики: от tip_r0 до tip_r1 радиуса
        "tip_r1": _num(z.get("tip_r1", 1.15), where + ".tip_r1", 1.0, 1.5),    # за радиус: кончик бывает чуть дальше окружности поиска
        "tip_len": _num(z.get("tip_len", 0.06), where + ".tip_len", 0.01, 0.5),  # длина кончика в долях радиуса: столько точек на луче = «кончик целиком»
        "tip_thin": _num(z.get("tip_thin", 0), where + ".tip_thin", 0, 51, True),   # «тонкое» для маски отражателя (рабочий масштаб); 0 — как у палки (thin). Широкую полоску thin вырезает как пятно
        "min_score": _num(z.get("min_score", 0.4), where + ".min_score", 0.01, 2),
        "min_conf": _num(z.get("min_conf", 0.1), where + ".min_conf", 0, 1),
        "jump_deg": _num(z.get("jump_deg", 25), where + ".jump_deg", 3, 180),
        "coast_s": _num(z.get("coast_s", 30), where + ".coast_s", 0, 3600),
        "red_min": _num(z.get("red_min", 0.12), where + ".red_min", 0, 1),
        "blue_alone": _num(z.get("blue_alone", 0.6), where + ".blue_alone", 0, 1),
        "blue_w": _num(z.get("blue_w", 0.5), where + ".blue_w", 0, 1),
        "blue_min": _num(z.get("blue_min", 0.3), where + ".blue_min", 0, 1),
        "agree_deg": _num(z.get("agree_deg", 20), where + ".agree_deg", 2, 90),
        "thin": _num(z.get("thin", 9), where + ".thin", 0, 51, True),
        "rpm_window": _num(z.get("rpm_window", 20), where + ".rpm_window", 2, 300),
        "stop_s": _num(z.get("stop_s", 10), where + ".stop_s", 2, 300),
        "stop_deg": _num(z.get("stop_deg", 5), where + ".stop_deg", 1, 45),
        "rpm_out": _out(z.get("rpm_out"), ("holding",), where + ".rpm_out"),
    }


def _norm_group(g: dict, cam_id: str, zone_ids: list, cycle_ids=(), level_ids=(), kiln_zones=None) -> dict:
    gid = str(g.get("id") or "")
    if not ID_RE.match(gid):
        raise ValueError("камера %s: id группы %r — только латиница, цифры, _ и -" % (cam_id, gid))
    where = "группа %s/%s" % (cam_id, gid)
    gtype = g.get("type", "sum")
    if gtype not in GROUP_TYPES:
        raise ValueError("%s: тип %r не из %s" % (where, gtype, list(GROUP_TYPES)))
    zs = [] if gtype == "kiln_sound" else [str(x) for x in (g.get("zones") or [])]      # по звуку зоны не нужны
    bad = [x for x in zs if x not in zone_ids]
    if bad:
        raise ValueError("%s: нет таких зон: %s" % (where, ", ".join(bad)))
    if not zs and gtype != "kiln_sound":
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
    if gtype == "level":
        if not 1 <= len(zs) <= 2 or any(x not in level_ids for x in zs):
            raise ValueError("%s: нужны одна-две зоны типа level — нижнее стекло, потом верхнее" % where)
        geometry = g.get("geometry", "lvldec")
        if geometry not in LEVEL_GEOMETRIES:
            raise ValueError("%s: геометрия %r не из %s" % (where, geometry, list(LEVEL_GEOMETRIES)))
        out.update(
            service_url=str(g.get("service_url") or LEVEL_URL).strip().rstrip("/"),
            liquid_class=str(g.get("liquid_class") or "liquid").strip(),
            conf=_num(g.get("conf", 0.35), where + ".conf", 0.01, 0.99),
            imgsz=_num(g.get("imgsz", 0), where + ".imgsz", 0, 4096, True),
            geometry=geometry,
            infer_interval_s=_num(g.get("infer_interval_s", 2), where + ".infer_interval_s", 0.2, 600),
            alpha=_num(g.get("alpha", 0.93), where + ".alpha", 0, 0.999),
            warmup_s=_num(g.get("warmup_s", 10), where + ".warmup_s", 0, 600),
            glass1_min=_num(g.get("glass1_min", 15), where + ".glass1_min", 0, 100),
            glass2_min=_num(g.get("glass2_min", 20), where + ".glass2_min", 0, 100),
            glass1_full=_num(g.get("glass1_full", 100), where + ".glass1_full", 0, 100),
            glass2_full=_num(g.get("glass2_full", 100), where + ".glass2_full", 0, 100),
            glass2_gate=_num(g.get("glass2_gate", 90), where + ".glass2_gate", 0, 100),
            shots_every_s=_num(g.get("shots_every_s", 60), where + ".shots_every_s", 0, 3600),
            hold_s=_num(g.get("hold_s", 30), where + ".hold_s", 1, 3600),
            contour=bool(g.get("contour", False)),
            frames_on=bool(g.get("frames_on", False)),
            frames_run=_num(g.get("frames_run", 0), where + ".frames_run", 0, 2 ** 31 - 1, True),
            frames_every_s=_num(g.get("frames_every_s", 60), where + ".frames_every_s", 1, 86400),
            frames_hours=_num(g.get("frames_hours", 24), where + ".frames_hours", 0.01, 720),
            frames_keep=_num(g.get("frames_keep", 1000), where + ".frames_keep", 10, 100000, True))
    if gtype == "kiln":
        known = kiln_zones or {}      # id → зона: этапы печи берут у зон и движение, и вид окна
        if any(x not in known for x in zs):
            raise ValueError("%s: этапам печи нужны зоны с движением (тип motion, движение не выключено)" % where)
        if not any(known[x].get("fill_metric", "off") != "off" and known[x].get("enabled", True) for x in zs):
            raise ValueError("%s: включите у зоны признак «вид окна» (лучше «яркость к металлу») — по нему виден сброс" % where)
        codes = g.get("codes") or {}
        if not isinstance(codes, dict):
            raise ValueError("%s: коды этапов — объект «этап: число»" % where)
        out.update(drop_pct=_num(g.get("drop_pct", 12), where + ".drop_pct", 1, 60),
                   load_max_s=_num(g.get("load_max_s", 10), where + ".load_max_s", 3, 120),
                   rot_wait_s=_num(g.get("rot_wait_s", 60), where + ".rot_wait_s", 5, 600),
                   codes={s: _num(codes.get(s, KILN_CODES[s]), "%s.codes.%s" % (where, s), 0, 65535, True) for s in KILN_STAGES})
    if gtype == "kiln_sound":
        codes = g.get("codes") or {}
        if not isinstance(codes, dict):
            raise ValueError("%s: коды этапов — объект «этап: число»" % where)
        out.update(out=None, threshold_db=_num(g.get("threshold_db", 30), where + ".threshold_db", 10, 60),
                   codes={s: _num(codes.get(s, SOUND_CODES[s]), "%s.codes.%s" % (where, s), 0, 65535, True) for s in SOUND_CODES})
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
    if rule not in ("or", "and", "sum", "value"):
        raise ValueError("%s: правило %r не из or/and/sum/value" % (where, rule))
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
    # уровнемер — число, а не бит: складывать его с окнами нельзя. Сценарий с уровнемером берёт его одного (rule value) и пишет проценты:
    # scale 10 — с десятыми (54,3 % → 543, так и без поля), 1 — целые (54,3 % → 54).
    gauges = {c["id"]: {z["id"] for z in c["zones"] if z["type"] in GAUGE_TYPES} for c in cams}
    analog = [s for s in srcs if s.get("zone") in gauges.get(s["camera"], ())]
    if analog:
        rule, srcs = "value", analog[:1]
    elif rule == "value" and srcs:
        rule = "or"                 # уровнемер из источников убрали, остались обычные окна
    # этапы печи — тоже число: «или» превратило бы код этапа в 1. Берём такой источник один и пишем его значение как есть
    kilns = {c["id"]: {g["id"] for g in c["groups"] if g["type"] in ("kiln", "kiln_sound")} for c in cams}
    stages = [s for s in srcs if s.get("group") in kilns.get(s["camera"], ())]
    if stages and not analog:
        rule, srcs = "sum", stages[:1]
    out = _out(sc.get("out"), ZONE_OUT_KINDS, where + ".out")
    if rule == "value" and out and out["kind"] != "holding":
        raise ValueError("%s: значение уровнемера пишется только в регистр" % where)
    if stages and not analog and out and out["kind"] != "holding":
        raise ValueError("%s: код этапа печи пишется только в регистр" % where)       # в coil любой этап стал бы единицей
    res = {"id": sid, "name": str(sc.get("name") or sid), "rule": rule, "signal": signal, "sources": srcs,
           "out": out,
           "modbus": normalize_modbus(mb, base=None) if mb else None}
    if rule == "value":                 # формат значения есть только у уровнемера
        scale = 10 if sc.get("scale") is None else sc["scale"]
        if scale not in (1, 10):
            raise ValueError("%s: формат значения %r не из 10/1" % (where, scale))
        res["scale"] = int(scale)
    return res


def normalize_shafts(raw: Optional[dict]) -> dict:
    """Блок «валы»: куда писать скорость каждого вала и что считать/писать по четырём парам (motion_shafts.py). Всегда полный:
    все 4 вала и 4 пары, пустое — по умолчанию (допуски 0 = тревога выключена, адреса нет = в ПЛК не пишем)."""
    raw = raw if isinstance(raw, dict) else {}
    rs, rp = raw.get("shafts") or {}, raw.get("pairs") or {}
    shafts = {sid: {"out_rpm": _out((rs.get(sid) or {}).get("out_rpm"), ("holding",), "вал %s.out_rpm" % sid)} for sid in SHAFT_IDS}
    pairs = {}
    for pid in PAIR_IDS:
        p, where = rp.get(pid) or {}, "пара %s" % pid
        pairs[pid] = {"ref": _num(p.get("ref", 0), where + ".ref", -180, 180),
                      "tol_angle": _num(p.get("tol_angle", 0), where + ".tol_angle", 0, 180),
                      "tol_rpm": _num(p.get("tol_rpm", 0), where + ".tol_rpm", 0, 10000),
                      "out_angle": _out(p.get("out_angle"), ("holding",), where + ".out_angle"),
                      "out_rpm": _out(p.get("out_rpm"), ("holding",), where + ".out_rpm"),
                      # самоподстройка волны Δ угла за оборот (как learn у группы mirror): окно в минутах, 5..240
                      "learn": bool(p.get("learn", True)),
                      "learn_min": _num(p.get("learn_min", 30), where + ".learn_min", 5, 240, True)}
    # align — сравнивать углы разных камер на ОДНОМ моменте времени съёмки (интерполяция), а не «последний кадр каждой»;
    # avg_s — окно усреднения Δ угла пары для графика и тревоги, с (0 — без усреднения);
    # stop_s — тревога «вал стоит»: вал не крутится дольше стольких секунд (0 — выключена, как и допуски)
    return {"shafts": shafts, "pairs": pairs, "align": bool(raw.get("align", True)),
            "avg_s": _num(raw.get("avg_s", 4.0), "валы.avg_s", 0, 60),
            "stop_s": _num(raw.get("stop_s", 0), "валы.stop_s", 0, 3600)}


WEBVIEW_DEFAULT = {"enabled": False, "host": "0.0.0.0", "port": 5000, "update_sec": 5.0, "jpeg_quality": 85, "annotate": False,
                   "crop_x1": 0, "crop_y1": 0, "crop_x2": 0, "crop_y2": 0, "output_w": 0, "output_h": 0}


def _norm_webview(w: dict, cid: str) -> dict:
    """Картинка камеры для SCADA и браузера (motion_webview.py). Ключи и значения по умолчанию — как в [webview] у LVLDec."""
    where = "камера %s.webview" % cid
    d = WEBVIEW_DEFAULT
    host = str(w.get("host", d["host"])).strip()
    if not host:
        raise ValueError("%s.host: пусто (0.0.0.0 — вся сеть, 127.0.0.1 — только этот ПК)" % where)
    out = {"enabled": bool(w.get("enabled", d["enabled"])), "host": host,
           "port": _num(w.get("port", d["port"]), where + ".port", 1, 65535, True),
           "update_sec": _num(w.get("update_sec", d["update_sec"]), where + ".update_sec", 0.5, 3600),
           "jpeg_quality": _num(w.get("jpeg_quality", d["jpeg_quality"]), where + ".jpeg_quality", 1, 100, True),
           "annotate": bool(w.get("annotate", d["annotate"]))}
    for k in ("crop_x1", "crop_y1", "crop_x2", "crop_y2"):
        out[k] = _num(w.get(k, 0), where + "." + k, 0, 100000, True)
    for k in ("output_w", "output_h"):
        out[k] = _num(w.get(k, 0), where + "." + k, 0, 16384, True)
    if any(out[k] for k in ("crop_x1", "crop_y1", "crop_x2", "crop_y2")) and not (out["crop_x2"] > out["crop_x1"] and out["crop_y2"] > out["crop_y1"]):
        raise ValueError("%s: обрезка — x2 больше x1 и y2 больше y1 (или все четыре 0 — без обрезки)" % where)
    if bool(out["output_w"]) != bool(out["output_h"]):
        raise ValueError("%s: размер — ширина и высота обе больше 0 (или обе 0 — не менять размер)" % where)
    return out


def normalize_camera(c: dict) -> dict:
    cid = str(c.get("id") or "")
    if not ID_RE.match(cid):
        raise ValueError("id камеры %r — только латиница, цифры, _ и -" % cid)
    zones = [_norm_zone(z, cid) for z in (c.get("zones") or [])]
    ids = [z["id"] for z in zones]
    if len(set(ids)) != len(ids):
        raise ValueError("камера %s: id зон повторяются" % cid)
    cycle_ids = [z["id"] for z in zones if z["type"] == "cycle"]
    level_ids = [z["id"] for z in zones if z["type"] == "level"]
    kiln_zones = {z["id"]: z for z in zones if z["type"] == "motion" and z.get("motion", True)}
    groups = [_norm_group(g, cid, ids, cycle_ids, level_ids, kiln_zones) for g in (c.get("groups") or [])]
    gids = [g["id"] for g in groups]
    if len(set(gids)) != len(gids):
        raise ValueError("камера %s: id групп повторяются" % cid)
    if sum(g["type"] == "kiln_sound" for g in groups) > 1:
        raise ValueError("камера %s: вычисление «этапы по звуку» может быть только одно — звук у камеры один" % cid)
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
    wv = _norm_webview(c["webview"], cid) if c.get("webview") else None
    row = str(c.get("row") or "").lower()
    if row not in CAMERA_ROWS:
        raise ValueError("камера %s: ряд валов — одно из %s" % (cid, ", ".join(r or "(не задан)" for r in CAMERA_ROWS)))
    return {
        "id": cid,
        "name": str(c.get("name") or cid),
        "enabled": bool(c.get("enabled", True)),
        "url": str(c.get("url") or ""),
        "transport": transport,
        "hw_accel": hw,
        "substream": bool(c.get("substream", False)),
        "row": row,
        "clarity_refs": _clarity_refs(c.get("clarity_refs")),    # shared — один «чистый» и один «грязный» на все стаканы камеры; own — у каждого стакана свои
        "clarity_clean": _lab3(c.get("clarity_clean"), "камера %s.clarity_clean" % cid),    # общий «чистый» камеры: для стаканов, у которых своего «чистого» нет
        "clarity_shift": _lab3(c.get("clarity_shift"), "камера %s.clarity_shift" % cid, shift=True),    # общий сдвиг «грязного» от «чистого» (зоны прозрачности с dirty_mode=shared)
        "juice_base": _juice_base(c.get("juice_base"), "камера %s.juice_base" % cid),
        "autocal_watch": _autocal_watch(c.get("autocal_watch"), "камера %s.autocal_watch" % cid),
        "proc_fps": _num(c.get("proc_fps", 3), "камера %s.proc_fps" % cid, 0.2, 30),
        # задержка кадра камеры, с (кадр приходит позже, чем снят: очередь кодера/декодера); меряется tools/cam_latency_check.py.
        # Углы валов разных камер сравниваются по времени СЪЁМКИ = приход − latency_s
        "latency_s": _num(c.get("latency_s", 0.0), "камера %s.latency_s" % cid, 0, 15),
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
        "webview": wv,
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
    ports = [c["webview"]["port"] for c in cams if c["webview"] and c["webview"]["enabled"]]
    if len(set(ports)) != len(ports):
        raise ValueError("картинка для SCADA: у двух камер один и тот же порт — у каждой камеры свой")
    scs = [_norm_scenario(s, cams) for s in (cfg.get("scenarios") or [])]
    sids = [s["id"] for s in scs]
    if len(set(sids)) != len(sids):
        raise ValueError("id сценариев повторяются")
    return {"version": 1, "modbus": normalize_modbus(cfg.get("modbus")), "cameras": cams, "scenarios": scs,
            "shafts": normalize_shafts(cfg.get("shafts"))}


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
        self.motion = zcfg.get("motion", True)
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
        """Следующий кадр → бит зоны: «движется», а у зоны без движения — «есть» по виду окна (пороги не заданы — 0)."""
        x, y, w, h = self.bbox
        a, b = prev[y:y + h, x:x + w], cur[y:y + h, x:x + w]
        if not self.motion:
            self._fill_step(b, cur)
            self.value = {"tex": self.tex, "lum": self.lum, "rel": self.rel}[self.fill_metric]
            return self.fill or 0
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


class ClarityRT:
    """Круг на стакане: цвет сока → мутность 0..100 между двумя эталонами.

    По кругу (внутренние `core` радиуса) берётся медиана цвета в Lab, без самых ярких `glare_pct` % точек (блики, пена). Цвет сглаживается
    за `smooth` кадров. Эталоны: `clean` — цвет чистого сока (мутность 0), `dirty` — самого мутного (100). Мутность — проекция цвета
    на отрезок clean → dirty: t = (c − clean)·(dirty − clean) / |dirty − clean|², turb = 10 + 80·t, зажатая в 0..100. Цвет далеко от этого отрезка
    (дальше `off_max` его длин в сторону: рука, чужой предмет, лампа) — valid = False, turb держит последнее хорошее значение.
    «Грязный» бывает общий (dirty_mode = shared: clean этой зоны + сдвиг камеры clarity_shift) или свой (own)."""

    def __init__(self, zcfg: dict, frame_size, cur_wh, shift=None, base_clean=None, shared=False):
        self.cfg = zcfg
        self.id, self.name = zcfg["id"], zcfg["name"]
        W, H = cur_wh
        sx = W / float(frame_size[0]) if frame_size else 1.0
        sy = H / float(frame_size[1]) if frame_size else 1.0
        cx, cy = zcfg["center"][0] * sx, zcfg["center"][1] * sy
        rc = max(2.0, zcfg["radius"] * (sx + sy) / 2.0 * zcfg["core"])
        x0, y0 = max(0, int(cx - rc)), max(0, int(cy - rc))
        x1, y1 = min(W, int(cx + rc) + 1), min(H, int(cy + rc) + 1)
        self.bbox = (x0, y0, x1, y1)
        yy, xx = np.ogrid[y0:y1, x0:x1]
        self.mask = ((xx - cx) ** 2 + (yy - cy) ** 2) <= rc * rc if (x1 > x0 and y1 > y0) else np.zeros((0, 0), bool)
        self.geom_ok = bool(self.mask.sum() >= 9)           # круг целиком вне кадра — считать нечего
        # shared — один «чистый» (камеры) и один «грязный» (его сдвиг) на все стаканы; иначе — свой «чистый», а нет его — общий камеры
        base = base_clean if shared else (zcfg["clean"] or base_clean)
        self.clean = np.array(base, np.float64) if base else None
        self.clean_default = bool(base_clean and (shared or not zcfg["clean"]))
        if shared or zcfg["dirty_mode"] == "shared":
            self.dirty = (self.clean + np.array(shift, np.float64)) if (self.clean is not None and shift) else None
        else:
            self.dirty = np.array(zcfg["dirty"], np.float64) if zcfg["dirty"] else None
        self._hist: deque = deque(maxlen=zcfg["smooth"])
        self._th: deque = deque()                           # (время, цвет) за smooth_s / 3 секунд — для медианы-фильтра выбросов
        self._y: Optional[np.ndarray] = None                # сглаженный цвет (подтягивается к медиане с разгоном)
        self._samples: deque = deque()                      # (время, L, a, b) сырые, за последние CLARITY_KEEP_S секунд — для калибровки
        self._lock = threading.Lock()
        # движение: квадрат по полному радиусу (не по «ядру») → серый N×N; маска круга «ядра» на этой сетке
        N = CLARITY_MV_N
        rf = max(2.0, zcfg["radius"] * (sx + sy) / 2.0)
        self.mbox = (max(0, int(cx - rf)), max(0, int(cy - rf)), min(W, int(cx + rf) + 1), min(H, int(cy + rf) + 1))
        self.mv_geom_ok = bool(self.mbox[2] - self.mbox[0] >= 6 and self.mbox[3] - self.mbox[1] >= 6)
        yy2, xx2 = np.ogrid[:N, :N]
        self._mmask = ((xx2 - (N - 1) / 2.0) ** 2 + (yy2 - (N - 1) / 2.0) ** 2) <= (N / 2.0 * zcfg["core"]) ** 2
        self.reset()

    def reset(self):
        self._hist.clear()
        self._th.clear()
        self._y = None
        self._last_t = 0.0
        self._persist = 0.0                                 # сколько секунд подряд цвет уходит в одну сторону — чем дольше, тем быстрее подгонка
        self._dprev = None
        self._cand = None                                   # состояние-кандидат и с какого момента оно держится (выдержка hold_s)
        self._cand_t = 0.0
        self._fresh = True                                  # первого достоверного значения ещё не было — состояние берём сразу, без выдержки
        self.lab: Optional[np.ndarray] = None
        self.turb = 0.0                                     # последнее хорошее значение (держим, пока цвет «не на шкале»)
        self.raw = 0.0                                      # то же без зажима в 0..100 (видно, насколько вышли за эталоны)
        self.off = 0.0                                      # насколько цвет в стороне от шкалы, в её длинах
        self.valid = False
        self.reason = "нет кадров"
        self.level = 0                                      # 0 прозрачный, 1 мутный, 2 грязный (по порогам t_turb / t_dirty с гистерезисом)
        self._mf: deque = deque()                           # (время, размытый серый круг) разных кадров за mv_win_s
        self._mlast: Optional[np.ndarray] = None            # прежний кадр круга — чтобы отличить дубль потока
        self._mraw: deque = deque()                         # (время, движение) за mv_smooth_s — для медианы
        self._mhist: deque = deque()                        # (время, сглаженное движение) за CLARITY_KEEP_S — для кнопки «пусто»
        self.move: Optional[float] = None                   # сглаженное движение; None — ещё не посчитано (мало кадров) или выключено
        self.juice: Optional[bool] = None                   # True — сок есть, False — пусто, None — не знаем
        self._jcand: Optional[bool] = None
        self._jcand_t = 0.0

    @property
    def empty(self) -> bool:
        """Стакан пуст: определение по движению включено, движения нет (с выдержкой). Тогда мутность — 0, кружок серый."""
        return bool(self.cfg["mv_on"] and self.juice is False)

    def _reset_color(self):
        """Цвет «забываем», пока стакан пуст: когда сок вернётся, мутность считается с нуля, а не подтягивается от цвета осадка."""
        self._hist.clear()
        self._th.clear()
        self._y = None
        self._persist = 0.0
        self._dprev = None
        self._cand = None
        self._fresh = True
        self.turb = self.raw = self.off = 0.0
        self.level = 0

    def step_move(self, frame: Optional[np.ndarray], now: float) -> None:
        """Следующий кадр → движение в кружке и состояние «сок / пусто». Движение = насколько отличаются средние старой и новой половин окна кадров
        (размытие гасит шум сжатия, усреднение — шум матрицы; общая яркость снята, блики отброшены). Дубли кадров потока в окно не берём."""
        cfg = self.cfg
        if not cfg["mv_on"]:
            if self.juice is not None or self.move is not None:
                self._mf.clear(); self._mraw.clear(); self._mhist.clear(); self._mlast = None
                self.move = self.juice = None
            return
        if frame is None or not self.mv_geom_ok:
            return
        x0, y0, x1, y1 = self.mbox
        sq = frame[y0:y1, x0:x1]
        g = cv2.cvtColor(sq, cv2.COLOR_BGR2GRAY) if sq.ndim == 3 else sq
        N = CLARITY_MV_N
        g = cv2.resize(g, (N, N), interpolation=cv2.INTER_AREA).astype(np.float32)
        dup = self._mlast is not None and float(np.abs(g - self._mlast).mean()) < CLARITY_MV_DUP_EPS
        if not dup:
            self._mlast = g
            self._mf.append((now, cv2.GaussianBlur(g, (0, 0), CLARITY_MV_SIGMA)[self._mmask]))
            while self._mf and now - self._mf[0][0] > cfg["mv_win_s"]:
                self._mf.popleft()
            if len(self._mf) >= CLARITY_MV_MIN_FRAMES:
                arr = np.stack([v for _, v in self._mf])
                k = len(arr) // 2
                ma, mb = arr[:k].mean(0), arr[k:].mean(0)
                keep = ma <= np.percentile(ma, 100.0 - cfg["glare_pct"]) if cfg["glare_pct"] > 0 else np.ones(len(ma), bool)
                raw = float(np.abs((ma - ma.mean()) - (mb - mb.mean()))[keep].mean())
                self._mraw.append((now, raw))
                S = cfg["mv_smooth_s"]
                while len(self._mraw) > 1 and now - self._mraw[0][0] > S:
                    self._mraw.popleft()
                if S <= 0 or now - self._mraw[0][0] >= 0.5 * S:       # набрали хотя бы полокна сглаживания — иначе первое же значение решало бы всё
                    self.move = float(np.median([v for _, v in self._mraw]))
                    with self._lock:
                        self._mhist.append((now, self.move))
                        while self._mhist and now - self._mhist[0][0] > CLARITY_KEEP_S:
                            self._mhist.popleft()
        self._set_juice(now)

    def _set_juice(self, now: float) -> None:
        """Состояние по порогам с гистерезисом и выдержкой mv_hold_s (первое значение берём сразу)."""
        if self.move is None:
            return
        cfg = self.cfg
        cur = self.juice
        new = self.move >= cfg["mv_hi"] or (cur is True and self.move >= cfg["mv_lo"])
        if cur is None or cfg["mv_hold_s"] <= 0 or new == cur:
            self.juice, self._jcand = new, None
        else:
            if self._jcand != new:
                self._jcand, self._jcand_t = new, now
            if now - self._jcand_t >= cfg["mv_hold_s"]:
                self.juice, self._jcand = new, None
        if self.empty:
            self._reset_color()

    def move_sample(self, seconds: float) -> dict:
        """Шум пустого стакана по последним `seconds` секундам: floor — почти максимум сглаженного движения (p95), median. Для кнопки «пусто»."""
        with self._lock:
            rows = list(self._mhist)
        if not rows:
            raise ValueError("зона %s: движение ещё не посчитано (нужно %.0f с кадров или определение выключено)" % (self.name, self.cfg["mv_win_s"] + 0.5 * self.cfg["mv_smooth_s"]))
        edge = rows[-1][0] - float(seconds)
        a = np.array([v for t, v in rows if t >= edge], np.float64)
        if len(a) < 3:
            raise ValueError("зона %s: за последние %.0f с набрано %d значений движения, мало" % (self.name, seconds, len(a)))
        return {"floor": round(float(np.percentile(a, 95)), 3), "median": round(float(np.median(a)), 3), "n": int(len(a))}

    def _smooth(self, med: np.ndarray, now: float) -> np.ndarray:
        """Цвет кадра → сглаженный. smooth_s = S > 0: медиана за S/3 секунд (отсекает короткие выбросы), затем цвет подтягивается к ней
        с постоянной времени S; если отклонение держится в одну сторону, постоянная времени падает до S/10 (разгон за ~S секунд).
        Пар на 5–10 с почти не двигает значение, а настоящее изменение проходит за ~S секунд. S = 0 — прежнее среднее за smooth кадров."""
        S = self.cfg["smooth_s"]
        if S <= 0:
            self._hist.append(med)
            return np.mean(self._hist, axis=0)
        self._th.append((now, med))
        while len(self._th) > 1 and now - self._th[0][0] > S / 3.0:
            self._th.popleft()
        target = np.median(np.array([c for _, c in self._th]), axis=0)
        if self._y is None or now - self._last_t > S:        # первый кадр или долгий перерыв — стартуем с измеренного
            self._y, self._last_t, self._persist, self._dprev = target.copy(), now, 0.0, None
            return self._y
        dt = max(0.0, now - self._last_t)
        self._last_t = now
        d = target - self._y
        if float(np.linalg.norm(d)) > CLARITY_DEAD_DE:
            same = self._dprev is not None and float(d @ self._dprev) > 0
            self._persist = self._persist + dt if same else 0.0
            self._dprev = d
        else:
            self._persist = max(0.0, self._persist - 2.0 * dt)
        tau = S - (S - S / 10.0) * min(1.0, self._persist / S)
        self._y = self._y + (1.0 - math.exp(-dt / tau)) * d
        return self._y

    def _set_level(self, now: float = 0.0):
        """Состояние по порогам. Гистерезис: вверх — сразу при пороге, вниз — только когда мутность ушла ниже порога на hyst пунктов.
        Выдержка hold_s: смена состояния происходит, только когда новое держится непрерывно hold_s секунд (пар, блик, рука не дёргают кружок)."""
        t, td, h = self.cfg["t_turb"], self.cfg["t_dirty"], self.cfg["hyst"]
        lv = self.level
        if self.turb >= td or (lv == 2 and self.turb >= td - h):
            new = 2
        elif self.turb >= t or (lv >= 1 and self.turb >= t - h):
            new = 1
        else:
            new = 0
        hold = self.cfg["hold_s"]
        if new == lv or hold <= 0 or self._fresh:
            self.level, self._cand = new, None
            return
        if self._cand != new:
            self._cand, self._cand_t = new, now
        if now - self._cand_t >= hold:
            self.level, self._cand = new, None

    def _fail(self, reason: str) -> bool:
        self.valid = False
        self.reason = reason
        return False

    def step(self, frame: Optional[np.ndarray], now: float) -> bool:
        """Следующий цветной кадр → обновить цвет и мутность. True — значение достоверно."""
        if frame is None or frame.ndim != 3:
            return self._fail("в кадре нет цвета")
        if not self.geom_ok:
            return self._fail("круг вне кадра")
        x0, y0, x1, y1 = self.bbox
        lab = cv2.cvtColor(frame[y0:y1, x0:x1].astype(np.float32) / 255.0, cv2.COLOR_BGR2Lab)[self.mask]
        gp = self.cfg["glare_pct"]
        if gp > 0 and len(lab) >= 20:
            lab = lab[lab[:, 0] <= np.percentile(lab[:, 0], 100.0 - gp)]
        med = np.median(lab, axis=0).astype(np.float64)
        with self._lock:
            self._samples.append((now, float(med[0]), float(med[1]), float(med[2])))
            while self._samples and now - self._samples[0][0] > CLARITY_KEEP_S:
                self._samples.popleft()
        if self.empty:                                      # стакан пуст (нет движения): мутности нет, цвет осадка в неё не идёт — показываем только сырой цвет
            self.lab = med
            return self._fail("нет сока: нет движения в кружке")
        if self.cfg["mv_on"] and self.juice is None:        # сок есть или пусто — ещё не знаем (после старта нужно набрать кадры): мутность по цвету осадка выдавать нельзя
            self.lab = med
            return self._fail("считаю движение: нужно %.0f с разных кадров" % (self.cfg["mv_win_s"] + 0.5 * self.cfg["mv_smooth_s"]))
        proj = self._project(med)
        if isinstance(proj, str):                           # эталонов нет — цвет всё равно копим и показываем (для калибровки)
            self.lab = self._smooth(med, now)
            return self._fail(proj)
        if self.cfg["off_max"] > 0 and proj[1] > self.cfg["off_max"]:
            # цвет сейчас не на шкале (рука, чужой предмет): в сглаживание его не берём — иначе переход к нему попадёт в «последнее хорошее» значение
            self.off = proj[1]
            return self._fail("цвет не на шкале «чистый — грязный»")
        self.lab = self._smooth(med, now)
        t, off = self._project(self.lab)
        self.raw, self.off = CLARITY_LO + (CLARITY_HI - CLARITY_LO) * t, off
        self.turb = max(0.0, min(100.0, self.raw))
        self._set_level(now)
        self._fresh = False
        self.valid = True
        self.reason = ""
        return True

    def _project(self, c: np.ndarray):
        """Цвет → (положение на шкале clean→dirty, 0 — чистый, 1 — грязный; отклонение в сторону от шкалы, в её длинах) или текст, почему шкалы нет."""
        if self.clean is None:
            return "нет эталона «чистый»"
        if self.dirty is None:
            return "нет эталона «грязный»"
        v = self.dirty - self.clean
        vv = float(v @ v)
        if vv < CLARITY_MIN_SPAN ** 2:
            return "«чистый» и «грязный» почти совпадают"
        d = c - self.clean
        t = float(d @ v) / vv
        return t, float(np.linalg.norm(d - t * v) / np.sqrt(vv))

    def sample(self, seconds: float) -> dict:
        """Медиана сырого цвета за последние `seconds` секунд → эталон. spread — разброс цвета (p90 − p10, наибольший по каналам): велик —
        цвет плыл, эталон шаткий. Мало кадров — ValueError с понятным текстом."""
        with self._lock:
            rows = list(self._samples)
        if not rows:
            raise ValueError("зона %s: кадров нет — камера не отдаёт цвет" % self.name)
        edge = rows[-1][0] - float(seconds)
        a = np.array([r[1:] for r in rows if r[0] >= edge], np.float64)
        if len(a) < 3:
            raise ValueError("зона %s: за последние %.0f с набрано %d кадров, мало" % (self.name, seconds, len(a)))
        res = {"lab": [round(float(x), 2) for x in np.median(a, axis=0)], "n": int(len(a)),
               "spread": round(float(np.max(np.percentile(a, 90, axis=0) - np.percentile(a, 10, axis=0))), 2)}
        try:                                                # шум движения для кнопки «пусто»; нет его (определение выключено, рано) — просто без ключа
            res["move"] = self.move_sample(seconds)
        except ValueError:
            res["move"] = None
        return res


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
        n = max(3, int(round(zcfg["thin"] / self.k))) | 1                  # «тонкое» задано в рабочем масштабе, а маски считаем в родном: ядро растёт в 1/k раз
        self.ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (n, n)) if zcfg["thin"] >= 3 else None
        # светоотражатели на концах палки: ищем в кольце tip_r0..tip_r1 радиуса — оно выходит за окружность поиска, поэтому своя (расширенная) область;
        # в ней же картинка превью и кадр для пипетки (кончик за окружностью тоже можно взять). Масштаб k общий с цветом.
        Rt = R * float(zcfg.get("tip_r1", 1.15))
        self.tx0, self.ty0 = max(0, int(cx - Rt)), max(0, int(cy - Rt))
        self.tx1, self.ty1 = min(W, int(cx + Rt) + 1), min(H, int(cy + Rt) + 1)
        self.tox, self.toy = (cx - self.tx0) * self.k, (cy - self.ty0) * self.k
        self.tip_on = bool(zcfg.get("tip_hsv")) and float(zcfg.get("tip_w", 1.0)) > 0
        rt = np.arange(max(3.0, r1 * float(zcfg.get("tip_r0", 0.55))), r1 * float(zcfg.get("tip_r1", 1.15)), 1.0)
        tw, th = max(1, int((self.tx1 - self.tx0) * self.k)), max(1, int((self.ty1 - self.ty0) * self.k))
        self.txs = np.clip(np.rint(self.tox + rt[None, :] * _PHI_SIN).astype(int), 0, tw - 1)
        self.tys = np.clip(np.rint(self.toy - rt[None, :] * _PHI_COS).astype(int), 0, th - 1)
        self.tip_n = max(1.0, float(zcfg.get("tip_len", 0.06)) * r1)       # столько «отражающих» точек на луче — кончик виден целиком (сила 1)
        self.r_tip = (float(rt[0]) if len(rt) else 0.0, float(rt[-1]) if len(rt) else 0.0)
        self.raw: deque = deque(maxlen=zcfg["smooth"])
        self.trk: deque = deque()               # (время, «развёрнутый» угол без скачков через 360°) за последние rpm_window секунд — для оборотов
        self.off_b = 0.0                        # на сколько синяя половина «довёрнута» от красной (град), учится, пока видны обе; reset не трогает
        self.bans: list = []                    # углы ложных неподвижных целей: при поиске по всему кругу вокруг них не смотрим (reset их не трогает)
        self.tip_bg: Optional[np.ndarray] = None   # доля времени, когда точка «отражает», пока вал крутится (неподвижные блики); reset не трогает
        self.tip_bg_t = 0.0
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
        self.src = "none"                       # чем держим угол: both (обе половины) / red / blue / hold (не видно, ожидаемый) / none
        self.lvl_r = self.lvl_b = 0.0           # насколько видна красная / синяя половина на найденном луче, 0..1
        self.lvl_t = 0.0                        # насколько видны светоотражатели на концах (среднее двух кончиков), 0..1; 0 — не видны или не настроены
        self.lvl_tr = self.lvl_tb = 0.0         # то же по каждому концу: отражатель красного конца (на угле) и синего (напротив), 0..1
        self.lead = "color"                     # откуда угол на этом кадре: color — по цвету (или среднее с отражателями), tip — по отражателям
        self.flip_v = 0.0                       # сколько градусов вал прошёл, пока цвет «за» разворот (≥ TIP_FLIP_DEG — стрелку разворачиваем на 180°)
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

    def _liks(self, bgr: np.ndarray, red_hsv=None, blue_hsv=None, red_more=None, blue_more=None, red_not=None, blue_not=None):
        """Маски красной и синей половин (доли 0..1 в рабочем масштабе). red_hsv/blue_hsv/red_more/blue_more/red_not/blue_not — свои пороги вместо настроенных (для превью на панели).
        Дополнительные цвета (red_more / blue_more) добавляются к основному диапазону «ИЛИ», исключённые (red_not / blue_not) вычитаются из итога — всё до фильтра «тонкого»."""
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        h, s, v = [c.astype(np.int32) for c in cv2.split(hsv)]
        hm, hn, rs_, rv = red_hsv or self.cfg["red_hsv"]
        red = (((h <= hm) | (h >= hn)) & (s >= rs_) & (v >= rv)).astype(np.uint8)
        b1, b2, bs, bv = blue_hsv or self.cfg["blue_hsv"]
        blue = ((h >= b1) & (h <= b2) & (s >= bs) & (v >= bv)).astype(np.uint8)
        for h1, h2, s_, v_ in (self.cfg.get("red_more") or []) if red_more is None else red_more:
            red |= ((h >= h1) & (h <= h2) & (s >= s_) & (v >= v_)).astype(np.uint8)
        for h1, h2, s_, v_ in (self.cfg.get("blue_more") or []) if blue_more is None else blue_more:
            blue |= ((h >= h1) & (h <= h2) & (s >= s_) & (v >= v_)).astype(np.uint8)
        for box in (self.cfg.get("red_not") or []) if red_not is None else red_not:      # исключённые цвета: вычитаем из маски своего конца
            red[hsv_box_hit(h, s, v, box)] = 0
        for box in (self.cfg.get("blue_not") or []) if blue_not is None else blue_not:
            blue[hsv_box_hit(h, s, v, box)] = 0
        if self.ker is not None:                                           # оставляем только тонкое: то, что не пережило «открытие»
            red &= 1 - cv2.morphologyEx(red, cv2.MORPH_OPEN, self.ker)
            blue &= 1 - cv2.morphologyEx(blue, cv2.MORPH_OPEN, self.ker)
        if self.k < 1.0:                                                   # сжимаем уже маски, а не картинку: палка в ~8 px при сжатии кадра
            red = cv2.resize(red.astype(np.float32), None, fx=self.k, fy=self.k, interpolation=cv2.INTER_AREA)   # смешивалась с корпусом
            blue = cv2.resize(blue.astype(np.float32), None, fx=self.k, fy=self.k, interpolation=cv2.INTER_AREA) # и теряла цвет
            red, blue = np.minimum(1.0, red * 3.0), np.minimum(1.0, blue * 3.0)   # доля покрытия пикселя → «есть» (тонкая линия закрывает 1/3 пикселя)
        return cv2.dilate(red.astype(np.float32), _K3), cv2.dilate(blue.astype(np.float32), _K3)

    def _tipmask(self, bgr: np.ndarray, tip_hsv=None) -> np.ndarray:
        """Маска светоотражателей (доли 0..1 в рабочем масштабе) по расширенной области вокруг оси. Отражатель от прожектора даёт пересвет:
        белое (V под 255, насыщенность почти 0) или яркий жёлтый — цвета задаются «ящиками» tip_hsv. Большие яркие пятна (лампа, белая стена,
        блик на корпусе) срезает тот же фильтр «тонкого», что и у цветных половин: кончик — короткая полоска, а не пятно."""
        boxes = (self.cfg.get("tip_hsv") or []) if tip_hsv is None else tip_hsv
        s2 = min(1.0, 2.0 * self.k)                                        # кончик толще палки: цвет читаем с кадра вдвое крупнее рабочего, а не с родного —
        if s2 < 1.0:                                                       # область отражателей большая, в родном разрешении это десятки мс на вал
            bgr = cv2.resize(bgr, None, fx=s2, fy=s2, interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        m = np.zeros(hsv.shape[:2], np.uint8)
        for h1, h2, s1, s2_, v1, v2 in boxes:                              # inRange по uint8 — быстрее сравнений в int32
            if h1 <= h2:
                m |= cv2.inRange(hsv, (h1, s1, v1), (h2, s2_, v2))
            else:                                                          # оттенок через ноль круга (красноватый)
                m |= cv2.inRange(hsv, (h1, s1, v1), (179, s2_, v2)) | cv2.inRange(hsv, (0, s1, v1), (h2, s2_, v2))
        m = (m > 0).astype(np.float32)
        if s2 > self.k:                                                    # в рабочий масштаб: доля покрытия → «есть» (как у цветных масок)
            m = np.minimum(1.0, cv2.resize(m, None, fx=self.k / s2, fy=self.k / s2, interpolation=cv2.INTER_AREA) * 2.0)
        thin = self.cfg.get("tip_thin") or self.cfg["thin"]                # свой «тонкий» у отражателя: полоска шире палки, а палочный thin её вырезал бы
        if thin >= 3:                                                      # «тонкое» задано в рабочем масштабе — здесь и фильтруем
            b = (m >= 0.5).astype(np.uint8)
            kt = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (int(thin) | 1,) * 2)
            m = m * (1 - cv2.morphologyEx(b, cv2.MORPH_OPEN, kt))
        return cv2.dilate(m.astype(np.float32), _K3)

    def _tip_static(self, tm: np.ndarray, now: float) -> np.ndarray:
        """Убрать из маски отражателей неподвижные блики: доля времени, когда точка светится, копится, пока вал крутится (или ещё неизвестно);
        кончик пробегает точку за доли секунды раз в оборот, а люк или лампа светятся всё время. Вал стоит — не копим: иначе его же кончики
        записались бы в блики."""
        if self.tip_bg is None or self.tip_bg.shape != tm.shape:
            self.tip_bg = np.zeros_like(tm)
            self.tip_bg_t = now
        eff = np.where(self.tip_bg < TIP_BG_MAX, tm, 0.0)
        if self.moving is not False:
            a = min(0.2, max(0.0, now - self.tip_bg_t) / TIP_BG_TAU)
            self.tip_bg += a * (tm - self.tip_bg)
        self.tip_bg_t = now
        return eff

    def _tip_profile(self, tmask: np.ndarray) -> np.ndarray:
        """Сила отражателя на луче φ (0..1): сколько отражающих точек в кольце у конца палки, к длине кончика (tip_n точек — кончик целиком)."""
        h, w = tmask.shape[:2]
        return np.minimum(1.0, tmask[np.minimum(self.tys, h - 1), np.minimum(self.txs, w - 1)].sum(axis=1) / self.tip_n)

    @staticmethod
    def _peak(s: np.ndarray, centre: float, hw: int = 15):
        """Лучший угол профиля s в окне ±hw° вокруг centre, уточнённый до долей градуса: (угол, сила 0..1) или None."""
        idx = (int(round(centre)) + np.arange(-hw, hw + 1)) % 360
        j = int(idx[np.argmax(s[idx])])
        v = float(s[j])
        if v <= 1e-6:
            return None
        i2 = (j + np.arange(-4, 5)) % 360
        w = np.clip(s[i2] - 0.5 * v, 0.0, None)
        r = np.radians(PHIS[i2])
        a = float(np.degrees(np.arctan2((w * np.sin(r)).sum(), (w * np.cos(r)).sum())) % 360.0) if w.sum() > 0 else float(PHIS[j])
        return a, v

    def _fuse(self, sr: np.ndarray, sb: np.ndarray, i: int, fallback: float, now: float, locked: bool, ts: Optional[np.ndarray] = None):
        """Одна стрелка из двух половин и светоотражателей. Угол по цвету — _fuse_color; ts — сила пары отражателей по лучам (None — не ищем).
        Отражатели видны и согласны с цветом (≤ agree_deg) → среднее с весом TIP_PREC·tip_w·сила (кончики на полном радиусе — угол точнее);
        цвета нет → угол по отражателям (src «tip»); не согласны → при слежении то, что ближе к ожидаемому углу, при поиске по кругу — цвет.
        «Вести по отражателям» (tip_lead): отражатели видны → угол только по ним, цвет не усредняется (src — по-прежнему какие половины видны).
        self.lead — откуда угол на этом кадре: «tip» — по отражателям, «color» — по цвету или среднее."""
        a, src = self._fuse_color(sr, sb, i, fallback, now, locked)
        self.lead = "color"
        pt = self._peak(ts, i) if ts is not None else None
        self.lvl_t = pt[1] if pt else 0.0
        if pt is None or pt[1] < self.cfg.get("tip_min", 0.3):
            return a, ("red" if src == "none" else src)                    # как раньше: обе половины слабые — угол по общему пику, src «red»
        if src == "none":                                                  # цвета на луче нет — держимся за отражатели
            self.lead = "tip"
            return pt[0], "tip"
        if self.cfg.get("tip_lead"):                                       # ведём по отражателям: кончики видны — угол по ним, цвет нужен только для направления
            self.lead = "tip"
            return pt[0], src
        if abs(ang_diff(pt[0], a)) <= self.cfg["agree_deg"]:
            wc = max(self.lvl_r, self.lvl_b, 1e-3)
            wt = TIP_PREC * float(self.cfg.get("tip_w", 1.0)) * pt[1]
            r = np.radians([a, pt[0]])
            return float(np.degrees(np.arctan2(wc * np.sin(r[0]) + wt * np.sin(r[1]), wc * np.cos(r[0]) + wt * np.cos(r[1]))) % 360.0), src
        if locked:
            e = self._expected(now)
            if abs(ang_diff(pt[0], e)) < abs(ang_diff(a, e)):
                self.lead = "tip"
                return pt[0], "tip"
            return a, src
        return a, src

    def _fuse_color(self, sr: np.ndarray, sb: np.ndarray, i: int, fallback: float, now: float, locked: bool):
        """Угол по двум цветным половинам. Красный пик около i и синий около i+180 ищем по отдельности.
        Видны обе и согласны (разница с учётом off_b ≤ agree_deg) → среднее по силе; видна одна → по ней;
        не согласны → та, что ближе к ожидаемому углу (при поиске по кругу — сильнее). Возвращает (угол, src); ни одной — (fallback, «none»)."""
        c = self.cfg
        pr = self._peak(sr, i)
        pb = self._peak(sb, (i + 180) % 360)
        red_ok = pr is not None and pr[1] >= c["red_min"]
        blue_ok = pb is not None and pb[1] >= min(c["blue_min"], c["blue_alone"])
        self.lvl_r = pr[1] if pr else 0.0
        self.lvl_b = pb[1] if pb else 0.0
        if not red_ok and not blue_ok:
            return fallback, "none"
        ab = None if pb is None else (pb[0] + 180.0) % 360.0                # синий пик, пересчитанный в «красный» угол
        if red_ok and blue_ok:
            d = ang_diff(ab, pr[0])
            if abs(d - self.off_b) <= c["agree_deg"]:
                self.off_b += 0.05 * (d - self.off_b)                       # учим смещение, пока половины согласны
                ab_c = (ab - self.off_b) % 360.0
                w1, w2 = pr[1], pb[1]
                r = np.radians([pr[0], ab_c])
                a = float(np.degrees(np.arctan2(w1 * np.sin(r[0]) + w2 * np.sin(r[1]), w1 * np.cos(r[0]) + w2 * np.cos(r[1]))) % 360.0)
                return a, "both"
            ab_c = (ab - self.off_b) % 360.0
            if locked:
                e = self._expected(now)
                return (pr[0], "red") if abs(ang_diff(pr[0], e)) <= abs(ang_diff(ab_c, e)) else (ab_c, "blue")
            return (pr[0], "red") if pr[1] >= pb[1] else (ab_c, "blue")
        if red_ok:
            return pr[0], "red"
        return (ab - self.off_b) % 360.0, "blue"

    def mask_preview(self, frame: np.ndarray, red_hsv=None, blue_hsv=None, max_w: int = 640, raw: bool = False, red_more=None, blue_more=None, red_not=None, blue_not=None,
                     tip_hsv=None) -> np.ndarray:
        """Что видит детектор: область вокруг оси (в рабочем масштабе), красные точки — маска красной половины, голубые — синей, жёлтые — светоотражатели,
        окружность поиска, кольцо, где ищем кончики (пунктир), и ось. Пороги можно подставить свои — подбирать цвета, не меняя настройки. raw — сам кадр без масок
        (искать палку глазами, брать цвет пипеткой): в родном разрешении (хоть 900 px и больше), без рисунка. Картинка — расширенная область (с кольцом отражателей), целиком."""
        crop_src = frame[self.ty0:self.ty1, self.tx0:self.tx1]
        if raw:                                                            # сам кадр области в родном разрешении и без рисунка: окно пипетки читает цвета прямо с картинки
            w0, h0 = crop_src.shape[1], crop_src.shape[0]
            if max_w == w0:
                return crop_src.copy()
            return cv2.resize(crop_src, (max_w, max(2, int(round(h0 * max_w / float(w0))))), interpolation=cv2.INTER_AREA if max_w < w0 else cv2.INTER_CUBIC)
        red, blue = self._liks(crop_src, red_hsv, blue_hsv, red_more, blue_more, red_not, blue_not)
        tips = (self.cfg.get("tip_hsv") or []) if tip_hsv is None else tip_hsv
        crop = cv2.resize(crop_src, (red.shape[1], red.shape[0]), interpolation=cv2.INTER_AREA)
        img = (crop.astype(np.float32) * 0.55).astype(np.uint8)
        img[red > 0.5] = (40, 40, 255)
        img[blue > 0.5] = (255, 200, 40)
        if tips:                                                           # отражатели — жёлтым; неподвижные блики, которые разбор уже отсеял, — тускло-оливковым
            tm = self._tipmask(crop_src, tips)[:img.shape[0], :img.shape[1]] > 0.5
            if self.tip_bg is not None and self.tip_bg.shape[0] >= tm.shape[0] and self.tip_bg.shape[1] >= tm.shape[1]:
                stat = self.tip_bg[:tm.shape[0], :tm.shape[1]] >= TIP_BG_MAX
                img[tm & stat] = (0, 110, 130)
                tm &= ~stat
            img[tm] = (0, 230, 255)
        sc = max_w / float(max(1, img.shape[1]))                           # и увеличить, и уменьшить до нужной ширины
        if sc > 1.0:
            img = cv2.resize(img, None, fx=sc, fy=sc, interpolation=cv2.INTER_NEAREST)
        elif sc < 1.0:
            img = cv2.resize(img, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
        o = (int(round(self.tox * sc)), int(round(self.toy * sc)))
        cv2.circle(img, o, int(round(float(self.rs[-1] if len(self.rs) else 3) * sc)), (200, 200, 200), 1, cv2.LINE_AA)
        if tips:                                                           # кольцо, где ищем кончики: две тонкие окружности жёлтым пунктиром
            for rr in self.r_tip:
                rp = int(round(rr * sc))
                for a0 in range(0, 360, 12):
                    cv2.ellipse(img, o, (rp, rp), 0, a0, a0 + 6, (0, 200, 230), 1, cv2.LINE_AA)
        cv2.circle(img, o, 3, (255, 255, 255), -1)
        return img

    def pick_color(self, frame: np.ndarray, u: float, v: float) -> Optional[dict]:
        """Пипетка: u, v — доли (0..1) картинки превью по ширине и высоте (картинка — вся область вокруг оси). Тонкая палка при клике по маленькой
        картинке попадает неточно, поэтому в окне вокруг точки берём пиксели, сильнее всего отличающиеся от фона окна (медианы), и усредняем их цвет:
        это и есть палка. -> {"h": 0..179, "s": 0..255, "v": 0..255, "x", "y" — точка в кадре, "n" — сколько пикселей усреднено} или None."""
        if frame is None or frame.ndim != 3:
            return None
        u, v = min(1.0, max(0.0, float(u))), min(1.0, max(0.0, float(v)))
        x = int(round(self.tx0 + u * (self.tx1 - self.tx0 - 1)))          # картинка превью — расширенная область (с кольцом отражателей)
        y = int(round(self.ty0 + v * (self.ty1 - self.ty0 - 1)))
        r = int(max(6, min(24, 0.03 * (self.x1 - self.x0))))
        patch = frame[max(0, y - r):y + r + 1, max(0, x - r):x + r + 1]
        if patch.size == 0:
            return None
        px = patch.reshape(-1, 3).astype(np.float32)
        d = np.linalg.norm(px - np.median(px, axis=0), axis=1)
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV).reshape(-1, 3).astype(np.int32)
        live = (hsv[:, 1] >= 40) & (hsv[:, 2] >= 40)                        # тени и блёклое — не палка
        d = np.where(live, d, -1.0) if live.sum() >= 3 else d
        k = max(3, int(0.04 * len(d)))
        top = np.argsort(-d)[:k]
        ang = np.radians(hsv[top, 0] * 2.0)                                  # оттенок — по кругу: 176 и 2 — соседи
        h = int(round((np.degrees(np.arctan2(np.sin(ang).mean(), np.cos(ang).mean())) % 360.0) / 2.0)) % 180
        return {"h": h, "s": int(np.median(hsv[top, 1])), "v": int(np.median(hsv[top, 2])), "x": x, "y": y, "n": int(k)}

    def _expected(self, now: float) -> float:
        """Где палка должна быть сейчас: прошлый угол + скорость × время (скорость — по последним оборотам, не больше 30°/с)."""
        rate = 0.0 if self.hint_stop else max(-30.0, min(30.0, self.rpm * 6.0))
        return (self.last + rate * max(0.0, now - self.last_t)) % 360.0

    def step(self, frame: Optional[np.ndarray], now: float = 0.0) -> bool:
        """Новый цветной кадр (now — его время, с) → угол и обороты. True, если палка найдена на этом кадре."""
        if frame is not None and frame.ndim == 3:
            red, blue = self._liks(frame[self.y0:self.y1, self.x0:self.x1])   # маски — в родном разрешении, сжатие внутри
            st = self._tip_profile(self._tip_static(self._tipmask(frame[self.ty0:self.ty1, self.tx0:self.tx1]), now)) if self.tip_on else None
            return self.step_profiles(red[self.ys, self.xs].mean(axis=1), blue[self.ys, self.xs].mean(axis=1), now, st)
        return self.step_profiles(None, None, now)

    def step_profiles(self, sr: Optional[np.ndarray], sb: Optional[np.ndarray], now: float = 0.0, st: Optional[np.ndarray] = None) -> bool:
        """То же по готовым профилям лучей: sr/sb — доля красных/синих точек на луче φ (360 чисел, φ = 0..359); None — кадра нет.
        st — сила светоотражателя на луче φ (None — отражатели не ищем). Вынесено из step: автоподгонка (motion_autocal) гоняет этот же
        боевой разбор по профилям, посчитанным для разных цветов и осей.

        Отражатели на обоих концах одинаковые, поэтому дают ось палки, но не направление: пара кончиков ts(φ) = (st(φ) + st(φ+180)) / 2.
        При слежении направление держит окно вокруг ожидаемого угла — пара добавляется к профилю целиком, и палку ведут даже одни отражатели.
        При поиске по кругу пара идёт только в ту сторону, где цвет сильнее (красный на φ против φ+180), — уверенность от этого не падает."""
        c = self.cfg
        found = False
        dt = now - self.last_t
        locked = self.last is not None and dt <= c["coast_s"]
        if sr is not None and sb is not None:
            comb = sr + c["blue_w"] * np.roll(sb, -180)                    # красный на φ, синий на φ+180
            ts = None
            if st is not None and c.get("tip_w", 1.0) > 0:
                ts = 0.5 * (st + np.roll(st, -180))
                colr = comb
                own = colr >= np.roll(colr, -180)                          # отражатели — в сторону, где цвет сильнее (ничья — в обе)
                tw = c.get("tip_w", 1.0) * ts     # и при «вести по отражателям» вес тот же: удвоенный при сбитом центре разворачивал стрелку (ролик 23.11)
                comb = colr + tw * own                                     # профиль для уверенности и для поиска по кругу
                comb_lk = colr + tw                                        # при слежении: направление держит окно
            cand = comb
            if not locked and self.bans:                                   # ложная неподвижная цель (рамка, лист): при поиске по кругу её как будто нет
                for b in self.bans:                                        # (и в уверенности тоже: иначе она «вторая по силе» и режет conf настоящей палки)
                    comb = np.where(np.abs(((PHIS - b + 180.0) % 360.0) - 180.0) <= BAN_DEG, 0.0, comb)
                cand = comb
            prof = comb_lk if (locked and ts is not None) else comb
            if locked:
                hw = min(90.0, c["jump_deg"] + max(0.0, dt - 2.0))         # при пропаже окно поиска понемногу растёт: 1°/с
                near = np.abs(((PHIS - self._expected(now) + 180.0) % 360.0) - 180.0) <= hw
                cand = np.where(near, prof, -1.0)
            i = int(np.argmax(cand))
            best = float(prof[i])
            far = np.abs(((PHIS - PHIS[i] + 180.0) % 360.0) - 180.0) > 20.0
            second = float(comb[far].max()) if far.any() else 0.0
            conf = 1.0 - second / float(comb[i]) if comb[i] > 1e-6 else 0.0
            self.score, self.conf = best, max(0.0, conf)
            anti = (i + 180) % 360
            color_ok = sr[i] >= c["red_min"] or sb[anti] >= c["blue_alone"]
            tip_ok = ts is not None and ts[i] >= c.get("tip_min", 0.3) and (locked or (colr[i] >= TIP_DIR_MIN and colr[i] > colr[anti]))
            if cand[i] >= 0 and best >= c["min_score"] and (color_ok or tip_ok) and (locked or conf >= c["min_conf"]):
                idx = (i + np.arange(-4, 5)) % 360                          # уточнение до долей градуса: центр тяжести вокруг пика
                w = np.clip(prof[idx] - 0.5 * best, 0.0, None)
                r = np.radians(PHIS[idx])
                ang0 = float(np.degrees(np.arctan2((w * np.sin(r)).sum(), (w * np.cos(r)).sum())) % 360.0) if w.sum() > 0 else float(PHIS[i])
                ang, self.src = self._fuse(sr, sb, i, ang0, now, locked, ts)   # одна стрелка из красной и синей половин и отражателей
                if ts is not None and locked:                                # отражатели ведут без направления: обе половины устойчиво «за» обратный луч — разворот
                    # голосуют видимые половины: красный сильнее на обратном луче, синий — на прямом. Видны обе — голос меньший из двух: неподвижный
                    # цветной предмет (коричневые спицы колеса при мягком красном) «за» разворот только у одной половины, вторая против — стрелка стоит
                    rv, bv = float(sr[anti] - sr[i]), float(sb[i] - sb[anti])
                    see_r, see_b = max(sr[i], sr[anti]) >= c["red_min"], max(sb[i], sb[anti]) >= 0.5 * c["blue_min"]
                    vote = min(rv, bv) if (see_r and see_b) else rv if see_r else bv if see_b else 0.0
                    # копим не кадры, а пройденный угол: перевёрнутая стрелка ошибается во всех секторах оборота, а косой центр или пятно цвета
                    # «голосуют» только в одном секторе — дальше голос «против» обнуляет счёт
                    if vote > TIP_FLIP:
                        self.flip_v += abs(ang_diff(ang, self.last)) if self.last is not None else 0.0
                    elif vote < -TIP_FLIP:
                        self.flip_v = 0.0
                    if self.flip_v >= TIP_FLIP_DEG:
                        ang = (ang + 180.0) % 360.0
                        self.flip_v = 0.0
                        self.raw.clear()
                        self.trk.clear()
                        self.unw = None
                        self.mv.clear()
                        self.src = "tip"                                     # на этом кадре угол — по отражателям, направление — по цвету
                else:
                    self.flip_v = 0.0
                if st is not None:                                           # отражатель каждого конца по отдельности: на стрелке — красный конец, напротив — синий
                    ia = int(round(ang)) % 360
                    self.lvl_tr = float(st[(ia + np.arange(-TIP_END_HW, TIP_END_HW + 1)) % 360].max())
                    self.lvl_tb = float(st[(ia + 180 + np.arange(-TIP_END_HW, TIP_END_HW + 1)) % 360].max())
                else:
                    self.lvl_tr = self.lvl_tb = 0.0
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
                self.src, self.lvl_r, self.lvl_b, self.lvl_t = "hold", 0.0, 0.0, 0.0
                self.lvl_tr = self.lvl_tb = 0.0
                self.lead = "color"
            else:
                self.valid, self.coast = False, False
                self.last = None
                self.raw.clear()
                self.src, self.lvl_r, self.lvl_b, self.lvl_t = "none", 0.0, 0.0, 0.0
                self.lvl_tr = self.lvl_tb = 0.0
                self.lead = "color"
                self._lost()
        return found


def ruler_level(ruler, pt) -> float:
    """На какой высоте линейки точка pt: 0 — на нижней стороне, 1 — на верхней (за краями зажато). Линия уровня t соединяет точки на левой и
    правой сторонах линейки (доля t от низа) и продолжается вбок — туда, где ходит указатель. Так наклон «горизонтали» плавно меняется
    от нижней стороны к верхней, как его видит камера; поэтому нижнюю и верхнюю стороны при разметке ведут вдоль меток линейки."""
    bl, br, tr, tl = [np.asarray(q, float) for q in ruler]

    def side(t: float) -> float:                # меньше нуля — точка выше линии уровня t (ось y смотрит вниз)
        a, b = bl + t * (tl - bl), br + t * (tr - br)
        return float((b[0] - a[0]) * (pt[1] - a[1]) - (b[1] - a[1]) * (pt[0] - a[0]))

    if side(0.0) >= 0:
        return 0.0
    if side(1.0) <= 0:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(30):
        mid = 0.5 * (lo + hi)
        lo, hi = (mid, hi) if side(mid) < 0 else (lo, mid)
    return 0.5 * (lo + hi)


class GaugeRT:
    """Общее у уровнемеров: доля шкалы t (0..1) → медиана последних smooth удачных кадров. Не видно — hold_s секунд держим последнее значение
    (held), потом «нет данных» (valid = False). Память о последнем удачном кадре переживает reset(): удержание действует и после обрыва потока.
    Настройки читаются из cfg на каждом кадре: «мягкие» (цвета, допуск, удержание…) меняются без перезапуска камеры."""

    def __init__(self, zcfg: dict, frame_size, cur_wh):
        self.cfg = zcfg
        self.id, self.name = zcfg["id"], zcfg["name"]
        W, H = cur_wh
        self.sx = W / float(frame_size[0]) if frame_size else 1.0
        self.sy = H / float(frame_size[1]) if frame_size else 1.0
        self.hist: deque = deque(maxlen=zcfg["smooth"])
        self.good: Optional[tuple] = None       # (время, t, подробности) последнего удачного кадра
        self.fresh = True                       # False — картинка не изменилась с прошлого кадра: такой кадр удержание не продлевает
        self._sig: Optional[str] = None         # настройки детектора, которыми получено значение
        self.reset()

    def reset(self):
        self.hist.clear()
        self.valid = self.held = False
        self.t = 0.0                    # доля шкалы 0..1 от начала разметки (у стрелки — от нижней стороны линейки, без учёта flip)
        self.why = ""                   # почему на последнем кадре значения не получилось
        self.seen: Optional[float] = None   # время последнего удачного кадра с изменившейся картинкой

    def pct_of(self, t: float) -> float:
        return 100.0 * t

    @property
    def pct(self) -> float:
        return self.pct_of(self.t)

    def _extra(self):
        """Что запомнить вместе с удачным значением (для рисования во время удержания)."""
        return None

    detector: tuple = ()                # поля настроек, от которых зависит прочитанное значение

    def _check_settings(self):
        """Сменили цвета, допуск и т. п. (без перезапуска камеры) — значение, полученное прежними настройками, не держим и в медиану не берём."""
        sig = repr([self.cfg.get(k) for k in self.detector])
        if self._sig is not None and sig != self._sig:
            self.hist.clear()
            self.good = None
            self.valid = self.held = False
        self._sig = sig

    def _found(self, t: float, now: float):
        if self.hist.maxlen != self.cfg["smooth"]:
            self.hist = deque(self.hist, maxlen=self.cfg["smooth"])
        if self.seen is not None and now - self.seen > GAUGE_GAP_S:
            self.hist.clear()
        self.hist.append(t)
        self.t = float(np.median(self.hist))
        self.valid, self.held, self.why = True, False, ""
        if self.fresh or self.good is None:                # застывшая картинка читается, но «держать» считается от последней живой
            self.seen = now
            self.good = (now, self.t, self._extra())

    def _fresh(self, now: float) -> bool:
        return self.good is not None and now - self.good[0] <= self.cfg["hold_s"]

    def _lost(self, now: float, why: str):
        self.why = why
        fresh = self._fresh(now)
        self.valid = self.held = fresh
        if fresh:
            self.t = self.good[1]
        else:
            self.hist.clear()                              # после долгой пропажи старые значения в медиану не идут

    def snapshot(self, now: float, alive: bool) -> Optional[dict]:
        """Что отдавать сейчас: None — «нет данных», иначе {t, held, hold_left, until, extra}. Камера не жива (обрыв, картинка стоит, каша) —
        последнее удачное значение, пока не вышло hold_s. until — после этого момента значение из состояния брать нельзя (поток мог зависнуть)."""
        hold = self.cfg["hold_s"]
        live = (alive and self.valid and not self.held and self.good is not None
                and (self.fresh or now - self.good[0] <= max(hold, GAUGE_STILL_S)))      # застывшая картинка читается, но не вечно
        if not live and not self._fresh(now):
            return None
        at, t, extra = self.good
        return {"t": self.t if live else t, "held": not live, "hold_left": 0 if live else max(0, int(round(at + hold - now))),
                "until": at + max(hold, GAUGE_STALE_S), "extra": extra}


class GateRT(GaugeRT):
    """«Стрелка»: самое крупное пятно цвета указателя внутри его зоны (саму линейку с её красными метками вырезаем) → высота центра пятна по
    линейке → 0..100 %."""

    detector = ("red_hsv", "red_more", "red_not", "min_area")

    def __init__(self, zcfg: dict, frame_size, cur_wh):
        super().__init__(zcfg, frame_size, cur_wh)
        W, H = cur_wh
        trk = np.round(np.array(scale_points(zcfg["points"], frame_size, cur_wh))).astype(np.int32)
        trk[:, 0] = np.clip(trk[:, 0], 0, W - 1)
        trk[:, 1] = np.clip(trk[:, 1], 0, H - 1)
        self.ruler = scale_points(zcfg["ruler"], frame_size, cur_wh)
        x, y, w, h = cv2.boundingRect(trk)
        self.bbox = (x, y, max(1, w), max(1, h))
        m = np.zeros((self.bbox[3], self.bbox[2]), np.uint8)
        cv2.fillPoly(m, [trk - [x, y]], 255)
        cut = scale_points(zcfg.get("cut") or zcfg["ruler"], frame_size, cur_wh)
        cv2.fillPoly(m, [np.round(np.array(cut)).astype(np.int32) - [x, y]], 0)          # метки линейки — того же цвета, их не берём
        self.mask, self.track = m, trk
        pad = max(8, int(round(0.08 * max(w, h))))                                       # превью и пипетка: зона указателя с полями
        self.view = (max(0, x - pad), max(0, y - pad), min(W, x + w + pad), min(H, y + h + pad))
        self.ker = cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, int(round(W / float(GATE_CLOSE_DIV)))) | 1, 3))

    def reset(self):
        super().reset()
        self.area = 0                   # площадь самого крупного пятна на последнем кадре, точек кадра разметки (и когда оно мало)
        self.ptr = None                 # центр указателя, точки текущего кадра
        self.box = None                 # его прямоугольник (x0, y0, x1, y1)

    def pct_of(self, t: float) -> float:
        return 100.0 * (1.0 - t if self.cfg["flip"] else t)

    def _extra(self):
        return self.ptr, self.box

    def _color(self, bgr: np.ndarray, red_hsv=None, red_more=None, red_not=None) -> np.ndarray:
        """Маска цвета указателя 0/255. Параметры — пробные значения вместо настроенных (превью на панели). Смысл полей — как у палки."""
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        hm, hn, s, v = red_hsv or self.cfg["red_hsv"]
        m = cv2.inRange(hsv, (0, s, v), (hm, 255, 255)) | cv2.inRange(hsv, (hn, s, v), (179, 255, 255))
        for h1, h2, s_, v_ in self.cfg["red_more"] if red_more is None else red_more:
            m |= cv2.inRange(hsv, (h1, s_, v_), (h2, 255, 255))
        nots = self.cfg["red_not"] if red_not is None else red_not
        if nots:
            h, sat, val = cv2.split(hsv)
            for box in nots:
                m[hsv_box_hit(h, sat, val, box)] = 0
        return m

    def step(self, frame: Optional[np.ndarray], now: float = 0.0) -> bool:
        """Новый цветной кадр (now — его время, с). True — указатель найден на этом кадре."""
        self._check_settings()
        x, y, w, h = self.bbox
        best = None
        if frame is not None and frame.ndim == 3:
            m = self._color(frame[y:y + h, x:x + w]) & self.mask
            m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, self.ker) & self.mask
            n, _, st, cen = cv2.connectedComponentsWithStats(m)
            if n > 1:
                i = 1 + int(np.argmax(st[1:, cv2.CC_STAT_AREA]))
                best = (int(st[i, cv2.CC_STAT_AREA]), float(cen[i][0]) + x, float(cen[i][1]) + y,
                        (int(st[i, 0]) + x, int(st[i, 1]) + y, int(st[i, 0] + st[i, 2]) + x, int(st[i, 1] + st[i, 3]) + y))
        self.area = int(round(best[0] / (self.sx * self.sy))) if best else 0
        if best and best[0] >= self.cfg["min_area"] * self.sx * self.sy:
            self.ptr, self.box = (best[1], best[2]), best[3]
            self._found(ruler_level(self.ruler, self.ptr), now)
            return True
        self._lost(now, "указатель не найден")
        return False

    def mask_preview(self, frame: np.ndarray, red_hsv=None, blue_hsv=None, max_w: int = 640, raw: bool = False, red_more=None, blue_more=None, red_not=None,
                     blue_not=None, tip_hsv=None) -> np.ndarray:
        """Что видит детектор: зона указателя с полями. Красным — то, что принято за цвет указателя (только внутри зоны и вне линейки),
        жёлтым — границы зоны, голубым — линейка. raw — сам кадр (для пипетки). Вызов — как у палки (CycleRT.mask_preview), синего у стрелки нет."""
        vx0, vy0, vx1, vy1 = self.view
        crop = frame[vy0:vy1, vx0:vx1]
        sc = max_w / float(max(1, crop.shape[1]))
        if raw:
            img = crop.copy()
        else:
            x, y, w, h = self.bbox
            m = self._color(frame[y:y + h, x:x + w], red_hsv, red_more, red_not) & self.mask
            img = (crop.astype(np.float32) * 0.55).astype(np.uint8)
            part = img[y - vy0:y - vy0 + m.shape[0], x - vx0:x - vx0 + m.shape[1]]
            part[m > 0] = (40, 40, 255)
            cv2.polylines(img, [self.track - [vx0, vy0]], True, (0, 220, 255), 1, cv2.LINE_AA)
            cv2.polylines(img, [np.round(np.array(self.ruler)).astype(np.int32) - [vx0, vy0]], True, (255, 200, 0), 1, cv2.LINE_AA)
        if max_w == img.shape[1]:
            return img
        return cv2.resize(img, (max_w, max(2, int(round(img.shape[0] * sc)))), interpolation=cv2.INTER_AREA if sc < 1 else cv2.INTER_NEAREST if not raw else cv2.INTER_CUBIC)


def tube_grid(points, direction: str) -> tuple:
    """Разметка столбика (точки текущего кадра) → (map_x, map_y, inside): откуда брать точки полоски H × W, строка 0 — начало шкалы (0 %).
    Четыре точки: пара с меньшим «вдоль шкалы» — начало, пара с большим — конец; строка соединяет точки на боковых сторонах (наклон учтён,
    высота линейна вдоль сторон, без перспективы). Иначе — многоугольник: строки поперёк направления, вне многоугольника точки не считаются."""
    uv = _uv(points, direction)
    if len(uv) == 4:
        by_v = uv[np.argsort(uv[:, 1], kind="stable")]
        s0, s1 = sorted(by_v[:2].tolist(), key=lambda q: q[0])
        e0, e1 = sorted(by_v[2:].tolist(), key=lambda q: q[0])
        s0, s1, e0, e1 = (np.array(q) for q in (s0, s1, e0, e1))
        H = int(np.clip(round(max(np.linalg.norm(e0 - s0), np.linalg.norm(e1 - s1))), 8, TUBE_MAX_H))
        W = int(np.clip(round(max(np.linalg.norm(s1 - s0), np.linalg.norm(e1 - e0))) + 1, 3, TUBE_MAX_W))
        t = ((np.arange(H) + 0.5) / H)[:, None, None]
        s = ((np.arange(W) + 0.5) / W)[None, :, None]
        left, right = s0 + t * (e0 - s0), s1 + t * (e1 - s1)
        g = left + s * (right - left)
        inside = np.ones((H, W), bool)
    else:
        lo, hi = uv.min(axis=0), uv.max(axis=0)
        du, dv = max(1e-6, hi[0] - lo[0]), max(1e-6, hi[1] - lo[1])
        H = int(np.clip(round(dv), 8, TUBE_MAX_H))
        W = int(np.clip(round(du) + 1, 3, TUBE_MAX_W))
        u = lo[0] + (np.arange(W) + 0.5) / W * du
        v = lo[1] + (np.arange(H) + 0.5) / H * dv
        g = np.stack(np.meshgrid(u, v), axis=-1)
        poly = np.stack([(uv[:, 0] - lo[0]) / du * W - 0.5, (uv[:, 1] - lo[1]) / dv * H - 0.5], axis=1)
        m = np.zeros((H, W), np.uint8)
        cv2.fillPoly(m, [np.round(poly * 16).astype(np.int32)], 255, cv2.LINE_8, 4)
        inside = m > 0
    gu, gv = g[..., 0], g[..., 1]
    x, y = {"up": (gu, -gv), "down": (gu, gv), "right": (gv, gu), "left": (-gv, gu)}[direction]
    return x.astype(np.float32), y.astype(np.float32), inside


def tube_step(filled: np.ndarray, empty: np.ndarray) -> tuple:
    """Строки полоски от начала шкалы → (t, quality, ragged): граница, при которой «до неё заполнено, дальше пусто» сходится у наибольшего
    числа строк. ragged — почти так же хорошо подходят границы на широком участке шкалы: часть колонки у уровня не видна (рука, труба),
    и где он на самом деле, неизвестно."""
    n = len(filled)
    below = np.concatenate([[0], np.cumsum(filled)])
    above = np.concatenate([[0], np.cumsum(empty)])
    score = below + (above[-1] - above)
    k = int(np.argmax(score))
    slack = max(1, int(round(0.01 * n)))
    near = np.nonzero(score >= score[k] - slack)[0]
    ragged = bool(near[-1] - near[0] > max(2 * slack + 1, int(round(TUBE_MAX_GAP * n))))
    return k / float(n), float(score[k]) / n, ragged


class TubeRT(GaugeRT):
    """«Столбик»: каждая точка внутри разметки похожа на образец «заполнено», на образец «пусто» или ни на что; строка поперёк шкалы — заполнена,
    пуста или чужая. Уровень — граница между заполненными и пустыми строками. Строк, согласных с такой картиной, мало (закрыли, пар, блик,
    не те цвета) — значения нет. Один залипший флажок или блик границу не сдвигает."""

    detector = ("fill_colors", "empty_colors", "tol", "min_quality")

    def __init__(self, zcfg: dict, frame_size, cur_wh):
        super().__init__(zcfg, frame_size, cur_wh)
        W, H = cur_wh
        pts = np.array(scale_points(zcfg["points"], frame_size, cur_wh), float)
        self.pts = pts
        self.map_x, self.map_y, self.inside = tube_grid(pts, zcfg["direction"])
        x0, y0 = pts.min(axis=0)
        x1, y1 = pts.max(axis=0)
        pad = max(8, int(round(0.06 * max(x1 - x0, y1 - y0))))
        self.view = (max(0, int(x0) - pad), max(0, int(y0) - pad), min(W, int(math.ceil(x1)) + pad + 1), min(H, int(math.ceil(y1)) + pad + 1))

    def reset(self):
        super().reset()
        self.quality = 0                # сколько % строк последнего кадра согласны с найденным уровнем

    def classify(self, bgr: np.ndarray, fill=None, empty=None, tol=None) -> tuple:
        """Маски (заполнено, пусто) по точкам картинки BGR. Параметры — пробные значения вместо настроенных (превью на панели)."""
        fill = self.cfg["fill_colors"] if fill is None else fill
        empty = self.cfg["empty_colors"] if empty is None else empty
        tol = self.cfg["tol"] if tol is None else tol
        lab = cv2.cvtColor(bgr.astype(np.float32) / 255.0, cv2.COLOR_BGR2Lab)
        w = np.array([TUBE_L_WEIGHT, 1.0, 1.0], np.float32)

        def dist(colors):
            d = np.full(lab.shape[:2], np.inf, np.float32)
            for c in colors:
                d = np.minimum(d, np.linalg.norm((lab - np.array(c, np.float32)) * w, axis=2))
            return d

        df, de = dist(fill), dist(empty)
        return (df < tol) & (df <= de), (de < tol) & (de < df)

    def read(self, frame: np.ndarray, fill=None, empty=None, tol=None) -> tuple:
        """(t, quality, ragged) по одному кадру без сглаживания и порога качества. Цвета и допуск — пробные вместо настроенных (превью)."""
        strip = cv2.remap(frame, self.map_x, self.map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        f, e = self.classify(strip, fill, empty, tol)
        base = max(1, min(TUBE_ROW_PIX, int(round(0.3 * strip.shape[1]))))
        nf, ne = (f & self.inside).sum(axis=1), (e & self.inside).sum(axis=1)
        typical = nf[nf >= base]
        best = None
        for share in TUBE_ROW_SHARES:                              # при равенстве — порог строже
            need = max(base, int(share * np.percentile(typical, 80))) if share and len(typical) else base
            filled = nf >= need
            found = tube_step(filled, ~filled & (ne >= base))
            if best is None or found[1] >= best[1]:
                best = found
        return best

    def step(self, frame: Optional[np.ndarray], now: float = 0.0) -> bool:
        """Новый цветной кадр (now — его время, с). True — уровень прочитан на этом кадре."""
        self._check_settings()
        if not self.cfg["fill_colors"] or not self.cfg["empty_colors"]:
            self.quality = 0
            self.valid = self.held = False
            self.good = None                               # цвета убрали — старое значение держать нечем
            self.hist.clear()
            self.why = "не заданы цвета «заполнено» и «пусто»"
            return False
        if frame is None or frame.ndim != 3:
            self.quality = 0
            self._lost(now, "нет цветного кадра")
            return False
        t, q, ragged = self.read(frame)
        self.quality = int(math.floor(q * 100 + 1e-9))
        if self.quality < self.cfg["min_quality"]:
            self._lost(now, "колонка не читается: сходится %d %% строк из нужных %d %%" % (self.quality, self.cfg["min_quality"]))
            return False
        if ragged:
            self._lost(now, "колонка не читается: часть шкалы у границы уровня не видна")
            return False
        self._found(t, now)
        return True

    def preview(self, frame: np.ndarray, max_w: int = 640, raw: bool = False, fill=None, empty=None, tol=None) -> np.ndarray:
        """Что видит детектор: разметка с полями, вписанная в квадрат max_w. Красным — «заполнено», голубым — «пусто» (внутри разметки), остальное
        притемнено; жёлтым — разметка, белым — уровень на этом кадре (когда он читается). raw — сам кадр без рисунка (для пипетки)."""
        vx0, vy0, vx1, vy1 = self.view
        crop = frame[vy0:vy1, vx0:vx1]
        sc = max_w / float(max(1, crop.shape[0], crop.shape[1]))
        size = (max(2, int(round(crop.shape[1] * sc))), max(2, int(round(crop.shape[0] * sc))))
        interp = cv2.INTER_AREA if sc < 1 else cv2.INTER_NEAREST               # увеличенное — без смешивания цветов: пипетка берёт настоящие
        if raw:
            return cv2.resize(crop, size, interpolation=interp)
        poly = np.round(self.pts - [vx0, vy0]).astype(np.int32)
        if len(poly) == 4:
            poly = cv2.convexHull(poly).reshape(-1, 2)
        area = np.zeros(crop.shape[:2], np.uint8)
        cv2.fillPoly(area, [poly], 255)
        f, e = self.classify(crop, fill, empty, tol)
        img = (crop.astype(np.float32) * 0.5).astype(np.uint8)
        img[f & (area > 0)] = (40, 40, 255)
        img[e & (area > 0)] = (235, 200, 40)
        img = cv2.resize(img, size, interpolation=interp)
        k = np.array([size[0] / float(crop.shape[1]), size[1] / float(crop.shape[0])])
        cv2.polylines(img, [np.round(poly * k).astype(np.int32)], True, (0, 220, 255), 1, cv2.LINE_AA)
        t, q, ragged = self.read(frame, fill, empty, tol)
        if q * 100 >= self.cfg["min_quality"] and not ragged:
            row = min(len(self.map_x) - 1, int(t * len(self.map_x)))
            ends = np.array([[self.map_x[row, 0], self.map_y[row, 0]], [self.map_x[row, -1], self.map_y[row, -1]]])
            a, b = np.round((ends - [vx0, vy0]) * k).astype(int)
            cv2.line(img, tuple(a), tuple(b), (255, 255, 255), 2, cv2.LINE_AA)
        return img


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
        self.kilns = {g["id"]: KilnRT(g["drop_pct"], g["load_max_s"], g["rot_wait_s"], g["codes"], "%s/%s" % (cam_cfg["id"], g["id"]))
                      for g in cam_cfg["groups"] if g["type"] == "kiln"}
        self.cycles: dict = {}                  # id зоны-цикла → CycleRT (строятся вместе с зонами, на первом кадре)
        self.clarity: dict = {}                 # id зоны-прозрачности → ClarityRT (там же)
        self.gauges: dict = {}                  # id уровнемера → GateRT / TubeRT (там же)
        zl ={z["id"]: z for z in cam_cfg["zones"] if z["type"] == "level"}
        self.levels = {g["id"]: LevelRT(g, [zl[x] for x in g["zones"] if x in zl], cam_cfg["frame_size"])
                       for g in cam_cfg["groups"] if g["type"] == "level"}     # уровни живут от потока модели (LevelInfer), не от кадров камеры
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
        for c in self.clarity.values():
            c.reset()
        for c in self.gauges.values():
            c.reset()
        for t in self.timers.values():
            t.reset()
        for k in self.kilns.values():
            k.reset()
        for m in self.mirrors.values():
            m.reset()
        for lv in self.levels.values():
            lv.reset()

    def _build(self, gray):
        h, w = gray.shape[:2]
        self.frame_wh = (w, h)
        self.zones = [ZoneRT(z, self.cfg["frame_size"], (w, h)) for z in self.cfg["zones"] if z["enabled"] and z["type"] not in ("cycle", "level", "clarity") + GAUGE_TYPES]
        self.cycles = {z["id"]: CycleRT(z, self.cfg["frame_size"], (w, h)) for z in self.cfg["zones"] if z["enabled"] and z["type"] == "cycle"}
        old = self.gauges
        self.gauges = {z["id"]: (GateRT if z["type"] == "gate" else TubeRT)(z, self.cfg["frame_size"], (w, h))
                       for z in self.cfg["zones"] if z["enabled"] and z["type"] in GAUGE_TYPES}
        for zid, c in self.gauges.items():                  # сменилось разрешение потока — последнее значение и удержание не теряем
            if zid in old and old[zid].good is not None:
                c.good = (old[zid].good[0], old[zid].good[1], None)
        cz = [z for z in self.cfg["zones"] if z["enabled"] and z["type"] == "clarity"]
        base = self.cfg.get("clarity_clean")
        if base is None:                                    # общего нет — медиана «чистых» других стаканов (снятых хотя бы у одного)
            own = [z["clean"] for z in cz if z["clean"]]
            base = [round(float(np.median([c[k] for c in own])), 2) for k in range(3)] if own else None
        self.clarity = {z["id"]: ClarityRT(z, self.cfg["frame_size"], (w, h), self.cfg.get("clarity_shift"), base, self.cfg.get("clarity_refs") == "shared") for z in cz}
        for m in self.mirrors.values():
            m.reset()

    def process(self, gray: np.ndarray, now: float, frame: Optional[np.ndarray] = None, fresh: bool = True) -> Optional[dict]:
        """Следующий (уже отобранный по proc_fps) серый кадр → состояние. None, пока копится буфер для FRAME_GAP.
        frame — тот же кадр в цвете: нужен зонам-циклам (цвет палки); без него (серый поток) они «палку не видят».
        fresh = False — картинка не изменилась с прошлого кадра: уровнемеры по такому кадру удержание не продлевают."""
        if self.zones is None or self.frame_wh != (gray.shape[1], gray.shape[0]):
            self._build(gray)
            self.buf.clear()
        self.buf.append(gray)
        if len(self.buf) <= self.gap:
            return None
        prev = self.buf[0]
        dt = self.gap * self.proc_interval
        cid, budget = self.cfg["id"], self.proc_interval * 1000.0
        t0 = time.perf_counter()
        bits = {z.id: z.step(prev, gray, dt) for z in self.zones}
        t1 = time.perf_counter()
        for c in self.cycles.values():
            bits[c.id] = int(c.step(frame, now))
        t2 = time.perf_counter()
        for c in self.clarity.values():
            c.step_move(frame, now)                         # сначала движение: пустой стакан цвет в мутность не пускает
            bits[c.id] = int(c.step(frame, now))
        t3 = time.perf_counter()
        for c in self.gauges.values():
            c.fresh = fresh
            bits[c.id] = int(c.step(frame, now))
        if self.gauges:
            PERF.record(cid + ":gate", (time.perf_counter() - t3) * 1000.0, got=sum(1 for c in self.gauges.values() if c.valid and not c.held),
                        of=len(self.gauges), budget_ms=budget)
        if self.zones:
            PERF.record(cid + ":zones", (t1 - t0) * 1000.0, got=sum(1 for z in self.zones if bits[z.id]), of=len(self.zones), budget_ms=budget)
        if self.cycles:
            PERF.record(cid + ":shafts", (t2 - t1) * 1000.0, got=sum(1 for c in self.cycles.values() if c.valid and not c.coast),
                        of=len(self.cycles), budget_ms=budget)
        if self.clarity:
            PERF.record(cid + ":juice", (t3 - t2) * 1000.0, got=sum(1 for c in self.clarity.values() if c.valid), of=len(self.clarity), budget_ms=budget)
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
                if x.moving is True:
                    x.bans.clear()                                           # снова крутится — прежние запреты не нужны
                elif x.valid and not x.coast and x.moving is False and y.valid and not y.coast and y.moving is True:
                    # «нашлась», но стоит, пока парная крутится: это ложная цель — запоминаем угол и ищем палку заново
                    x.bans = (x.bans + [x.angle])[-MAX_BANS:]
                    x.reset()
                    continue
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
            if zc["type"] == "level":
                zones.append(self._level_zone(zc, now, alive))
                continue
            if zc["type"] == "clarity":
                zones.append(self._clarity_zone(zc, alive))
                continue
            if zc["type"] in GAUGE_TYPES:
                zones.append(self._gauge_zone(zc, now, alive))
                continue
            z = zs.get(zc["id"])
            zones.append({"id": zc["id"], "name": zc["name"], "type": zc["type"], "metric": zc["metric"],
                          "enabled": zc["enabled"], "bit": int(bits.get(zc["id"], 0)) if alive else 0,
                          "flow": round(z.flow, 4) if z else 0.0, "diff": round(z.diff, 4) if z else 0.0,
                          "value": round(z.value, 4) if z else 0.0, "on": zc["on"], "off": zc["off"],
                          **({} if zc.get("motion", True) else {"motion": False}),      # ключ есть только у зон без движения
                          **self._fill_state(zc, z, alive, bool(bits.get(zc["id"], 0)))})
        groups = []
        for g in self.cfg["groups"]:
            if g["type"] == "kiln_sound":
                continue                   # от картинки не зависит: его добавляет поток камеры из слушателя звука (motion_sound)
            gb = {zid: (bits.get(zid, 0) if alive else 0) for zid in g["zones"]}
            if g["type"] == "mirror":
                groups.append(self._mirror_group(g, alive, now))
                continue
            if g["type"] == "level":
                groups.append(self._level_group(g, now, alive))
                continue
            if g["type"] == "kiln":
                groups.append(self._kiln_group(g, gb, zs, now, alive and update_timers))
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

    def _kiln_group(self, g: dict, bits: dict, zones: dict, now: float, alive: bool) -> dict:
        """Этапы печи: «движется» — хоть одна зона группы, вид окна — у зон с этим признаком. value — код этапа для ПЛК,
        bits — идут сейчас сброс / засыпка / прокрутка. Камеры нет — value 0 и этапа нет; место в цикле не сбрасываем
        (короткое замирание кадра), это делают пределы этапов и переоткрытие потока."""
        k = self.kilns[g["id"]]
        look = {zid: getattr(zones[zid], zones[zid].fill_metric) for zid in g["zones"] if zid in zones and zones[zid].fill_metric != "off"}
        if alive:
            k.step(now, any(bits.values()), look)
        s = k.snapshot(now)
        if not alive:
            s.update(stage=None, stage_name="нет данных", value=0, since_s=0)
        return {"id": g["id"], "name": g["name"], "type": "kiln", "bits": [int(s["stage"] == x) for x in ("drop", "load", "rot")], **s}

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
                "angle": angle, "conf": conf, "rpm": rpm, "score": round(c.score, 3) if c else 0.0, "valid": ok, "coast": coast, "moving": moving,
                "src": c.src if ok else "none", "lvl_r": round(c.lvl_r, 2) if ok else 0.0, "lvl_b": round(c.lvl_b, 2) if ok else 0.0,
                "lvl_t": round(c.lvl_t, 2) if ok else 0.0, "off_b": round(c.off_b, 1) if c else 0.0,
                "tip_on": bool(c and c.tip_on),                                  # отражатели настроены: на плитке и в виджете концы рисуются всегда (видны — ярко, нет — тускло)
                "lvl_tr": round(c.lvl_tr, 2) if ok else 0.0, "lvl_tb": round(c.lvl_tb, 2) if ok else 0.0,   # отражатель красного / синего конца, 0..1
                "lead": c.lead if ok else "color"}                               # угол на этом кадре: по отражателям (tip) или по цвету (color)

    def _clarity_zone(self, zc: dict, alive: bool) -> dict:
        """Зона-прозрачность → запись состояния. turb — мутность 0..100 (flow = turb, diff = движение в кружке — для историй, трендов и CSV; раньше тут
        была яркость L, она осталась в lab), bit/valid — значение достоверно (есть кадр и оба эталона, цвет на шкале, стакан не пуст). lab — сглаженный
        цвет, calibrated — оба эталона заданы (для интерфейса). empty — нет движения, стакан пуст: turb = 0, valid = False; move / juice — движение и «сок есть»."""
        c = self.clarity.get(zc["id"])
        ok = bool(alive and c and c.valid)
        empty = bool(alive and c and c.empty)
        turb = round(c.turb, 1) if (c and alive and not empty) else 0.0
        lab = [round(float(x), 2) for x in c.lab] if (c is not None and c.lab is not None) else None
        move = round(c.move, 3) if (c and alive and c.move is not None) else 0.0
        reason = "" if ok else ("нет кадров" if not alive else (c.reason if c else "зона выключена"))
        return {"id": zc["id"], "name": zc["name"], "type": "clarity", "metric": "flow", "enabled": zc["enabled"],
                "bit": int(ok), "flow": turb, "diff": move, "value": turb, "on": 0.0, "off": 0.0,
                "turb": turb, "level": (c.level if c else 0), "level_name": ("empty" if empty else CLARITY_LEVELS[c.level] if c else "clear"),
                "raw": round(c.raw, 1) if c else 0.0, "off_axis": round(c.off, 2) if c else 0.0, "lab": lab,
                "valid": ok, "reason": reason, "calibrated": bool(c and c.clean is not None and c.dirty is not None),
                "ref_default": bool(c and c.clean_default),
                "move": move, "juice": (None if (c is None or c.juice is None or not c.cfg["mv_on"]) else int(c.juice)), "empty": empty}

    def _gauge_zone(self, zc: dict, now: float, alive: bool) -> dict:
        """Уровнемер → запись состояния. pct — 0..100 % (flow = pct — для историй, трендов и CSV), bit/valid — значение есть (в том числе
        удержанное, held: осталось hold_left с), until — после этого времени значение брать нельзя. t — доля шкалы от начала разметки.
        Стрелка: diff = площадь пятна указателя, ptr и box — где он, в точках кадра разметки. Столбик: diff = quality, % сходящихся строк."""
        c = self.gauges.get(zc["id"])
        s = c.snapshot(now, alive) if c else None
        ok = s is not None
        pct = round(c.pct_of(s["t"]), 1) if ok else 0.0
        reason = "" if ok and not s["held"] else ("нет кадров" if not alive else (c.why if c else "зона выключена"))
        out = {"id": zc["id"], "name": zc["name"], "type": zc["type"], "metric": "flow", "enabled": zc["enabled"],
               "bit": int(ok), "flow": pct, "value": pct, "on": 0.0, "off": 0.0,
               "pct": pct, "valid": ok, "held": bool(ok and s["held"]), "hold_left": s["hold_left"] if ok else 0,
               "until": round(s["until"], 1) if ok else None, "reason": reason, "t": round(s["t"], 4) if ok else 0.0}
        if zc["type"] == "tube":
            quality = c.quality if (c and alive) else 0
            out.update(diff=quality, quality=quality)
            return out
        area = c.area if (c and alive) else 0
        ptr, box = s["extra"] if ok and s["extra"] else (None, None)
        out.update(diff=area, area=area,
                   ptr=[round(ptr[0] / c.sx, 1), round(ptr[1] / c.sy, 1)] if ptr else None,
                   box=[round(box[0] / c.sx), round(box[1] / c.sy), round(box[2] / c.sx), round(box[3] / c.sy)] if box else None)
        return out

    def held_zones(self, zones: list, now: float) -> list:
        """Битые кадры: прошлое состояние повторяется как есть, а уровнемеры отдают удержанное по своим «держать» (или «нет данных»)."""
        own = {z["id"]: z for z in self.cfg["zones"]}
        return [self._gauge_zone(own[z["id"]], now, False) if z.get("type") in GAUGE_TYPES and z["id"] in own else z for z in zones]

    def clarity_sample(self, zone_ids, seconds: float) -> dict:
        """Эталон цвета по последним `seconds` секундам для зон-прозрачности: {id: {lab, n, spread}} или {id: {error}}. zone_ids пуст — все."""
        res = {}
        for zid, c in self.clarity.items():
            if zone_ids and zid not in zone_ids:
                continue
            try:
                res[zid] = c.sample(seconds)
            except ValueError as e:
                res[zid] = {"error": str(e)}
        return res

    def _level_zone(self, zc: dict, now: float, alive: bool) -> dict:
        """Зона-стекло → запись состояния. flow = уровень стекла 0..100 %, diff = сырой уровень (до отсечки и сглаживания), bit — в стекле есть жидкость."""
        gc = next((g for g in self.cfg["groups"] if g["type"] == "level" and zc["id"] in g["zones"]), None)
        snap = self.levels[gc["id"]].snapshot(now, alive) if gc else None
        idx = gc["zones"].index(zc["id"]) if gc else 0
        valid = bool(snap and snap["valid"])
        pct = round((snap["s1"] if idx == 0 else snap["s2"]) * 100, 1) if valid else 0.0
        raw = snap["raw"][idx] if snap else None
        rawp = round(raw * 100, 1) if (valid and raw is not None) else 0.0
        return {"id": zc["id"], "name": zc["name"], "type": "level", "metric": "flow", "enabled": zc["enabled"],
                "bit": int(valid and pct > 0), "flow": pct, "diff": rawp, "value": pct, "on": 0.0, "off": 0.0,
                "level": pct, "raw": rawp, "valid": valid}

    def _level_group(self, g: dict, now: float, alive: bool) -> dict:
        """Группа-уровень → запись состояния. value = Total × 10 (0..1000) — это и уходит в ПЛК; total/g1/g2 — в процентах (g1, g2: 0..50)."""
        lv = self.levels[g["id"]]
        s = lv.snapshot(now, alive)
        ok = s["valid"]
        total = max(0.0, min(100.0, s["total"]))
        out = {"id": g["id"], "name": g["name"], "type": "level", "value": int(round(total * 10)) if ok else 0,
               "bits": [int(ok and s["g1"] > 0), int(ok and s["g2"] > 0)],
               "valid": ok, "total": round(total, 1), "g1": round(s["g1"], 1), "g2": round(s["g2"], 1),
               "raw": [None if r is None else round(r * 100, 1) for r in s["raw"]], "conf": round(s["conf"], 2),
               "ms": s["ms"], "model": s["model"], "err": s["err"], "age_s": s["age_s"], "warmup": bool(now - lv.t0 < g["warmup_s"])}
        if s.get("frames"):
            out["frames"] = s["frames"]
        if g.get("contour"):
            out["contour"], out["contour_wh"] = s["contour"], s["contour_wh"]
        return out

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
        """Поля вида окна для состояния зоны. state: 2 — движется, 1 — жом есть и стоит, 0 — пусто и стоит; None — решения нет.
        У зоны без движения (motion=False) состояния только 0 нет / 1 есть: там moving — это и есть бит наличия."""
        if zc.get("fill_metric", "off") == "off":
            return {}
        fill = z.fill if (z and alive) else None
        state = None if fill is None else (2 if moving and zc.get("motion", True) else fill)
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
                if zc["type"] == "clarity":
                    a = za[zc["id"]]
                    # пусто (нет движения) — 0 (решение Макса 22.11: «прозрачно и всё»); нет значения иначе — 65535, а не 0: ПЛК не должен принять это за «чистый»
                    val = 0 if a.get("empty") else (int(round(a["turb"])) if a["valid"] else CLARITY_NO_DATA)
                elif zc["type"] == "cycle" and o["kind"] == "holding":
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
