"""motion_level — уровень жидкости по маске модели и зонам-стёклам (перенос ядра LVLDec: декантатор, два мерных стекла).

Откуда цифра: сервис уровня (level_service/, YOLO) отдаёт контуры найденной жидкости; зоны-стёкла рисует человек на кадре.
Уровень стекла = площадь(маска жидкости ∩ зона) / площадь(зона), 0..1. Дальше — те же шаги, что в LVLDec.exe (core.LevelProcessor):
  1. нет жидкости в кадре → берём прошлое значение (память);
  2. отсечка осадка: ниже glass1_min / glass2_min (% заполнения) — считаем 0;
  3. калибровка «полного»: glassX_full (%) — сырой уровень, который модель даёт при РЕАЛЬНО полном стекле, тянем в 100 %;
  3а. (нет в LVLDec) верхнее стекло считаем, только когда нижнее заполнено не меньше glass2_gate (%): пока нижнее не полное, в
      верхнем жидкости быть не может — там корка осадка на дне, и её дрожь у порога glass2_min бросала нижнее в 100 % (пики на тренде);
  4. верхнее стекло > 0 → нижнее заведомо полное (100 %);
  5. гистерезис у потолка: выше 96 % фиксируем 100 %, сбрасываем ниже 96 %;
  6. сглаживание EMA: smooth = smooth·alpha + level·(1 − alpha); первые warmup_s секунд — без сглаживания;
  7. Total = (нижнее + верхнее) · 50: каждое стекло 0..50 %, вместе 0..100 %. В ПЛК — ×10 (54,3 % → 543).
Обработка идёт один раз на каждый ответ модели (раз в infer_interval_s), а не на каждый кадр камеры — как в LVLDec.

Ничего не знает про камеру: LevelInfer берёт кадр у вызывающего, шлёт сервису уровня и скармливает ответ в LevelRT.
"""
from __future__ import annotations

import json
import logging
import math
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np

log = logging.getLogger("motion.level")

DEFAULT_URL = "http://127.0.0.1:8770"
GEOMETRIES = ("lvldec", "exact")
FRAMES_MIN_FREE = 2 * 1024 ** 3     # съёмка кадров для обучения встаёт, если на диске свободно меньше
CONTOUR_MAX_PTS = 60               # контур жидкости для плитки: не больше стольких точек на контур
TREND_KEEP_S = 7200                # снимки для графика уровня живут столько же, сколько самое длинное окно графика (2 ч)
TREND_SHOT_W, TREND_SHOT_Q = 640, 70   # снимок графика: ширина, качество JPEG (≈ 40 КБ — разглядеть стёкла хватает)
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))     # localhost без системного прокси (иначе виснет)


class LevelProcessor:
    """Шаги 1–7 из шапки. update(raw1, raw2): сырые уровни 0..1 или None (нет данных). Возвращает {'total','glass1','glass2'} в %."""

    def __init__(self, alpha: float = 0.93, glass1_min: float = 15.0, glass2_min: float = 20.0,
                 glass1_full: float = 100.0, glass2_full: float = 100.0, glass2_gate: float = 0.0):
        self.alpha = alpha
        self.glass1_min, self.glass2_min = glass1_min, glass2_min
        self.glass1_full, self.glass2_full = glass1_full, glass2_full
        self.glass2_gate = glass2_gate          # 0 — как в LVLDec (верхнее считается всегда)
        self.last_g1 = self.last_g2 = 0.0
        self.fix_g1_at_100 = self.fix_g2_at_100 = False
        self.smooth_g1 = self.smooth_g2 = 0.0

    def update(self, raw1: Optional[float], raw2: Optional[float]) -> dict:
        g1, g2 = raw1, raw2
        if g1 is None:
            g1 = self.last_g1
        else:
            self.last_g1 = g1
        if g2 is None:
            g2 = self.last_g2
        else:
            self.last_g2 = g2
        if g1 * 100 < self.glass1_min:
            g1 = 0.0
        if g2 * 100 < self.glass2_min:
            g2 = 0.0
        if 0 < self.glass1_full < 100 and g1 > 0:
            g1 = min(1.0, g1 / (self.glass1_full / 100.0))
        if 0 < self.glass2_full < 100 and g2 > 0:
            g2 = min(1.0, g2 / (self.glass2_full / 100.0))
        if g1 * 100 < self.glass2_gate:  # нижнее не полное ⇒ в верхнем только осадок
            g2 = 0.0
            self.fix_g2_at_100 = False   # и «полное верхнее» не держим ещё тик
        if g2 > 0:                       # жидкость в верхнем стекле ⇒ нижнее полное
            g1 = 1.0
            self.last_g1 = 1.0
        g2p = g2 * 100
        if not self.fix_g2_at_100 and g2p > 96:
            self.fix_g2_at_100 = True
        if self.fix_g2_at_100:
            g2 = 1.0
            if g2p < 96:
                self.fix_g2_at_100 = False
        g1p = g1 * 100
        if not self.fix_g1_at_100 and g1p > 96:
            self.fix_g1_at_100 = True
        if self.fix_g1_at_100:
            g1 = 1.0
            if g1p < 96:
                self.fix_g1_at_100 = False
        a = self.alpha
        self.smooth_g1 = self.smooth_g1 * a + g1 * (1 - a)
        self.smooth_g2 = self.smooth_g2 * a + g2 * (1 - a)
        p1, p2 = self.smooth_g1 * 50, self.smooth_g2 * 50
        return {"total": p1 + p2, "glass1": p1, "glass2": p2}


def liquid_mask(objs: list, cls_name: str, W: int, H: int) -> tuple:
    """Контуры всех объектов класса cls_name → одна бинарная маска H×W и флаг «жидкость в кадре есть» (хоть один объект)."""
    mask = np.zeros((H, W), np.uint8)
    found = False
    for o in objs:
        if o.get("name") != cls_name:
            continue
        found = True
        for poly in o.get("polygons") or []:
            if len(poly) >= 3:
                cv2.fillPoly(mask, [np.array(poly, np.int32).reshape(-1, 1, 2)], 1)
    return mask, found


def scale_pts(points: list, frame_size, cur_wh) -> np.ndarray:
    """Точки зоны (пиксели кадра разметки) → пиксели кадра W×H (если камера отдаёт другое разрешение), округлённые и обрезанные по кадру."""
    W, H = cur_wh
    pts = np.array(points, float).reshape(-1, 2)
    if frame_size:
        pts = pts * [W / float(frame_size[0]), H / float(frame_size[1])]
    pts = np.round(pts).astype(np.int32)
    pts[:, 0] = np.clip(pts[:, 0], 0, W - 1)
    pts[:, 1] = np.clip(pts[:, 1], 0, H - 1)
    return pts


def contour_points(objs: list, cls_name: str, max_pts: int = CONTOUR_MAX_PTS) -> list:
    """Упрощённые контуры жидкости для плитки: [[[x, y], …], …] в пикселях кадра."""
    out = []
    for o in objs:
        if o.get("name") != cls_name:
            continue
        for poly in o.get("polygons") or []:
            c = np.array(poly, np.int32).reshape(-1, 1, 2)
            eps = 1.5
            while len(c) > max_pts and eps < 60:
                c = cv2.approxPolyDP(c, eps, True)
                eps *= 1.6
            if len(c) >= 3:
                out.append([[int(x), int(y)] for x, y in c.reshape(-1, 2)])
    return out


class LevelRT:
    """Уровень одной «группы-уровня»: 1–2 зоны-стекла (нижняя, потом верхняя) + процессор. Потокобезопасен: ingest() зовёт поток
    модели, snapshot() — поток кадров камеры."""

    def __init__(self, gcfg: dict, zcfgs: list, frame_size=None, clock: Callable[[], float] = time.time):
        self.g = gcfg
        self.zones = zcfgs[:2]
        self.frame_size = frame_size
        self.clock = clock
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        """С нуля: как свежий запуск LVLDec (память, сглаживание и гистерезис обнулены, прогрев заново)."""
        g = self.g
        with self._lock:
            self.proc = LevelProcessor(g["alpha"], g["glass1_min"], g["glass2_min"], g["glass1_full"], g["glass2_full"], g.get("glass2_gate", 0.0))
            self.t0 = self.clock()
            self.last_ok = 0.0
            self.res = {"total": 0.0, "glass1": 0.0, "glass2": 0.0}
            self.raw = [None, None]
            self.conf = 0.0
            self.ms = 0
            self.model = ""
            self.err = ""
            self.contour: list = []
            self.frames: Optional[dict] = None     # состояние съёмки кадров для обучения (LevelInfer) — в панель
            self.contour_wh: Optional[list] = None
            self.objs: list = []                  # объекты последнего ответа модели (все классы) — для размеченной картинки SCADA
            self._zmasks: dict = {}

    def _zone(self, i: int, W: int, H: int):
        """(маска зоны H×W, площадь) — считается один раз на размер кадра."""
        key = (i, W, H)
        z = self._zmasks.get(key)
        if z is None:
            m = np.zeros((H, W), np.uint8)
            cv2.fillPoly(m, [scale_pts(self.zones[i]["points"], self.frame_size, (W, H)).reshape(-1, 1, 2)], 1)
            z = self._zmasks[key] = (m, int(m.sum()))
        return z

    def ingest(self, result: dict, now: Optional[float] = None):
        """Ответ сервиса уровня на один кадр → новый уровень. Бросает при кривом ответе (вызывающий запишет ошибку)."""
        now = self.clock() if now is None else now
        W, H = int(result["image"]["w"]), int(result["image"]["h"])
        objs = result.get("objects") or []
        cls = self.g["liquid_class"]
        mask, found = liquid_mask(objs, cls, W, H)
        raws = [None, None]
        if found:
            for i in range(len(self.zones)):
                zmask, area = self._zone(i, W, H)
                if area:
                    raws[i] = max(0.0, min(1.0, int(np.count_nonzero(mask & zmask)) / area))
        confs = [float(o.get("conf", 0)) for o in objs if o.get("name") == cls]
        contour = contour_points(objs, cls) if self.g.get("contour") else []
        with self._lock:
            self.proc.alpha = 0.0 if now - self.t0 < self.g["warmup_s"] else self.g["alpha"]     # прогрев: без сглаживания, значение быстро набирает реальное
            self.res = self.proc.update(raws[0], raws[1])
            self.raw = raws
            self.conf = max(confs) if confs else 0.0
            self.ms = int((result.get("timing") or {}).get("ms") or 0)
            self.model = str(result.get("model") or "")
            self.last_ok, self.err = now, ""
            self.contour, self.contour_wh = contour, [W, H]
            self.objs = objs

    def overlay(self, now: Optional[float] = None) -> dict:
        """Всё для размеченной картинки SCADA: объекты последнего ответа модели и уровень. Ответ старше hold_s — объектов и уровня нет (как у snapshot)."""
        snap = self.snapshot(now, True)
        with self._lock:
            objs, wh = (list(self.objs), list(self.contour_wh) if self.contour_wh else None) if snap["valid"] else ([], None)
        lv = {"total": snap["total"], "glass1": snap["g1"], "glass2": snap["g2"]} if snap["valid"] else None
        return {"objs": objs, "wh": wh, "liquid_class": self.g["liquid_class"], "lv": lv}

    def set_frames(self, info: dict):
        with self._lock:
            self.frames = info

    def note_error(self, msg: str, quiet: bool = False):
        """Причина, по которой нового уровня нет (видна в панели). quiet — в журнал не писать (о мёртвой камере уже сказал её поток)."""
        with self._lock:
            changed = msg != self.err
            self.err = msg
        if changed and not quiet:
            log.warning("[уровень %s] %s", self.g["id"], msg)

    def snapshot(self, now: Optional[float], alive: bool) -> dict:
        """Состояние для движка. valid — свежий ответ модели не старше hold_s и камера жива; иначе нули (как у мёртвой камеры)."""
        now = self.clock() if now is None else now
        with self._lock:
            age = (now - self.last_ok) if self.last_ok else None
            valid = bool(alive and self.last_ok and age <= self.g["hold_s"])
            r = self.res if valid else {"total": 0.0, "glass1": 0.0, "glass2": 0.0}
            return {"valid": valid, "total": r["total"], "g1": r["glass1"], "g2": r["glass2"],
                    "s1": self.proc.smooth_g1 if valid else 0.0, "s2": self.proc.smooth_g2 if valid else 0.0,
                    "raw": list(self.raw), "conf": self.conf, "ms": self.ms, "model": self.model,
                    "err": self.err, "age_s": None if age is None else round(age, 1),
                    "contour": list(self.contour) if valid else [], "contour_wh": self.contour_wh, "frames": self.frames}


def post_infer(url: str, jpeg: bytes, params: dict, timeout: float = 30.0) -> dict:
    """Кадр (JPEG) → /infer сервиса уровня. Ошибка (нет связи, нет модели, плохой кадр) → {"error": текст}."""
    full = (url or DEFAULT_URL).rstrip("/") + "/infer?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, data=jpeg, method="POST", headers={"Content-Type": "application/octet-stream"})
    try:
        with _opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return {"error": json.loads(e.read().decode("utf-8")).get("error") or "HTTP %d" % e.code}
        except Exception:
            return {"error": "HTTP %d" % e.code}
    except Exception:
        return {"error": "сервис уровня не отвечает (%s)" % (url or DEFAULT_URL)}


def yolo_label(objs: list, cls_name: str, W: int, H: int) -> str:
    """Контуры жидкости → разметка YOLO-seg (класс 0, точки 0..1, по строке на контур). Пусто — жидкости модель не нашла."""
    lines = []
    for o in objs:
        if o.get("name") != cls_name:
            continue
        for poly in o.get("polygons") or []:
            if len(poly) >= 3:
                pts = " ".join("%.6f %.6f" % (min(1.0, max(0.0, x / W)), min(1.0, max(0.0, y / H))) for x, y in poly)
                lines.append("0 " + pts)
    return "\n".join(lines) + ("\n" if lines else "")


def frames_stats(folder: Path) -> tuple:
    """(сколько кадров лежит в folder\\images, сколько байт)."""
    n = size = 0
    try:
        for p in (folder / "images").glob("*.jpg"):
            n += 1
            size += p.stat().st_size
    except OSError:
        pass
    return n, size


def prune_frames(folder: Path, keep: int) -> tuple:
    """Оставить в folder\\images не больше keep самых новых кадров; разметку старых (labels\\<имя>.txt) убрать вместе с ними.
    Возвращает (сколько кадров стёрто, сколько байт освободили)."""
    imgs = sorted((folder / "images").glob("*.jpg"))
    gone = freed = 0
    for p in imgs[:max(0, len(imgs) - keep)]:
        try:
            freed += p.stat().st_size
        except OSError:
            pass
        for f in (p, folder / "labels" / (p.stem + ".txt")):
            try:
                f.unlink()
            except OSError:
                pass
        gone += 1
    return gone, freed


def save_trend_shot(folder: Path, frame: np.ndarray, now: float, width: int = TREND_SHOT_W, quality: int = TREND_SHOT_Q,
                    keep_s: float = TREND_KEEP_S) -> Path:
    """Снимок для графика уровня: кадр, ужатый до width по ширине, JPEG quality → folder\\<unix-секунды>.jpg; старше keep_s — стираем."""
    h, w = frame.shape[:2]
    if w > width:
        frame = cv2.resize(frame, (width, max(1, round(h * width / w))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ValueError("кадр не закодировался")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ("%d.jpg" % int(now))
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(buf.tobytes())
    tmp.replace(path)                                  # читатель не увидит недописанный файл
    for p, ts in trend_shots(folder):
        if ts < now - keep_s:
            try:
                p.unlink()
            except OSError:
                pass
    return path


def trend_shots(folder: Path) -> list:
    """[(файл, unix-секунды)] снимков графика в folder (имя файла — время)."""
    out = []
    try:
        for p in folder.glob("*.jpg"):
            if p.stem.isdigit():
                out.append((p, int(p.stem)))
    except OSError:
        pass
    return out


def nearest_trend_shot(folder: Path, ts: float, max_dt: float, now: Optional[float] = None, keep_s: float = TREND_KEEP_S,
                       direction: int = 0):
    """Снимок, ближайший к моменту ts (не дальше max_dt секунд и не старше keep_s): (файл, его время) или None.
    direction −1 / +1 — соседний снимок строго раньше / позже ts на любом расстоянии (стрелки в окне; между снимками бывают дыры)."""
    now = time.time() if now is None else now
    if not math.isfinite(ts):
        return None
    best = None
    for p, t in trend_shots(folder):
        if t < now - keep_s:
            continue
        if direction:
            if (t - ts) * direction <= 0:
                continue
        elif abs(t - ts) > max_dt:
            continue
        if best is None or abs(t - ts) < abs(best[1] - ts):
            best = (p, t)
    return best


class LevelInfer(threading.Thread):
    """Раз в infer_interval_s: свежий кадр камеры → сервис уровня → LevelRT. Отдельный поток: модель на процессоре думает
    секунду, и разбор кадров камеры, и запись в ПЛК от этого не тормозят.

    Съёмка кадров для обучения (save_dir + frames_* в группе). Включает её кнопка в панели (frames_on + новый frames_run), дальше сервер
    сам: раз в frames_every_s секунд (не чаще опроса модели) тот же кадр, что ушёл в модель (полный размер, без разметки), кладётся в
    save_dir\\images, а в save_dir\\labels — то, что нашла модель, в формате YOLO-seg (заготовка для разметки и дообучения). Идёт
    frames_hours часов от нажатия и сама заканчивается (метка начала — в save_dir\\run.json, переживает перезапуск); хранится не больше
    frames_keep кадров; свободного места на диске меньше FRAMES_MIN_FREE — съёмка встаёт, чтобы не забить диск.

    Снимки для графика (shots_dir + shots_every_s в группе, 0 — выкл.): раз в shots_every_s секунд маленький JPEG того же кадра в
    shots_dir; живут TREND_KEEP_S (2 ч, как окно графика). Клик по графику уровня в панели открывает ближайший к моменту снимок."""

    def __init__(self, rt: LevelRT, get_frame: Callable[[], Optional[np.ndarray]], halt: threading.Event,
                 post: Callable = post_infer, jpeg_quality: int = 95, save_dir: Optional[Path] = None,
                 shots_dir: Optional[Path] = None):
        super().__init__(daemon=True, name="level-" + rt.g["id"])
        self.rt, self.get_frame, self.halt, self.post, self.q = rt, get_frame, halt, post, jpeg_quality
        self.save_dir = Path(save_dir) if save_dir else None
        self.shots_dir = Path(shots_dir) if shots_dir else None
        self._last_save = 0.0
        self._last_shot = 0.0
        self._run: Optional[tuple] = None          # (номер запуска из конфига, время начала по часам сервера)
        self._stats: Optional[list] = None         # [кадров на диске, байт] — считаем один раз, дальше по ходу дела

    def run(self):
        g = self.rt.g
        while not self.halt.is_set():
            t0 = time.time()
            try:
                self.step()
            except Exception as e:                       # поток не должен умирать от случайной ошибки
                self.rt.note_error("сбой разбора кадра: %s" % e)
            self.halt.wait(max(0.2, g["infer_interval_s"] - (time.time() - t0)))

    def step(self):
        """Один круг. Отдельно от run() — чтобы тесты гоняли его без потока и времени."""
        g = self.rt.g
        frame = self.get_frame()
        if frame is None:
            self.rt.note_error("нет свежего кадра камеры", quiet=True)
            return
        self._maybe_shot(frame)
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.q])
        if not ok:
            self.rt.note_error("кадр не закодировался")
            return
        params = {"conf": g["conf"], "geometry": g["geometry"]}
        if g["imgsz"]:
            params["imgsz"] = g["imgsz"]
        res = self.post(g["service_url"], buf.tobytes(), params, max(10.0, g["infer_interval_s"] * 5))
        if "error" in res:
            self.rt.note_error(res["error"])
        else:
            self.rt.ingest(res)
            try:
                self._maybe_save(buf.tobytes(), res)
            except Exception as e:                       # диск полон / нет прав: уровень от этого не страдает
                log.warning("[уровень %s] кадр для обучения не сохранён: %s", g["id"], e)

    def _maybe_shot(self, frame: np.ndarray):
        """Снимок для графика, если пора. Ошибка диска уровню не мешает — только строка в журнал."""
        every = float(self.rt.g.get("shots_every_s") or 0)
        now = time.time()
        if not self.shots_dir or every <= 0 or now - self._last_shot < every:
            return
        self._last_shot = now
        try:
            save_trend_shot(self.shots_dir, frame, now)
        except Exception as e:
            log.warning("[уровень %s] снимок для графика не сохранён: %s", self.rt.g["id"], e)

    def _window(self, now: float) -> tuple:
        """(состояние съёмки: off | run | done, сколько секунд осталось). Начало окна — первое включение с новым frames_run."""
        g = self.rt.g
        if not g.get("frames_on"):
            return "off", 0.0
        rid = int(g.get("frames_run") or 0)
        if self._run is None or self._run[0] != rid:
            f = self.save_dir / "run.json"
            t0 = None
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                if int(d.get("run", -1)) == rid:
                    t0 = float(d["t0"])
            except (OSError, ValueError, KeyError, TypeError):
                pass
            if t0 is None:
                t0 = now
                self.save_dir.mkdir(parents=True, exist_ok=True)
                f.write_text(json.dumps({"run": rid, "t0": t0}), encoding="utf-8")
            self._run = (rid, t0)
        left = self._run[1] + float(g["frames_hours"]) * 3600.0 - now
        return ("run", left) if left > 0 else ("done", 0.0)

    def _maybe_save(self, jpeg: bytes, res: dict):
        if not self.save_dir:
            return
        g = self.rt.g
        now = time.time()
        state, left = self._window(now)
        if state == "run":
            try:
                if shutil.disk_usage(self.save_dir.parent).free < FRAMES_MIN_FREE:
                    state = "disk"
            except OSError:
                pass
        if self._stats is None:
            self._stats = list(frames_stats(self.save_dir))
        if state == "run" and now - self._last_save >= float(g["frames_every_s"]):
            self._last_save = now
            self._save(jpeg, res, now)
        self.rt.set_frames({"state": state, "left_s": int(left), "saved": self._stats[0], "mb": round(self._stats[1] / 1048576, 1)})

    def _save(self, jpeg: bytes, res: dict, now: float):
        g = self.rt.g
        W, H = int(res["image"]["w"]), int(res["image"]["h"])
        objs = res.get("objects") or []
        if g["geometry"] != "exact":                     # в разметку — честные контуры, а не «как в LVLDec» (те съезжают к центру)
            params = {"conf": g["conf"], "geometry": "exact"}
            if g["imgsz"]:
                params["imgsz"] = g["imgsz"]
            exact = self.post(g["service_url"], jpeg, params, max(10.0, g["infer_interval_s"] * 5))
            objs = (exact.get("objects") or []) if "error" not in exact else None
        with self.rt._lock:
            raw = list(self.rt.raw)
        tag = "_".join("%s%s" % (k, "x" if v is None else int(round(v * 100))) for k, v in zip("nv", raw))   # n — нижнее, v — верхнее, сырой %
        name = "%s_%s" % (time.strftime("%Y%m%d_%H%M%S", time.localtime(now)), tag)
        (self.save_dir / "images").mkdir(parents=True, exist_ok=True)
        (self.save_dir / "images" / (name + ".jpg")).write_bytes(jpeg)
        self._stats[0] += 1
        self._stats[1] += len(jpeg)
        if objs is not None:
            (self.save_dir / "labels").mkdir(parents=True, exist_ok=True)
            (self.save_dir / "labels" / (name + ".txt")).write_text(yolo_label(objs, g["liquid_class"], W, H), encoding="utf-8")
        gone, freed = prune_frames(self.save_dir, int(g.get("frames_keep") or 1000))
        self._stats[0] -= gone
        self._stats[1] -= freed
