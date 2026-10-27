"""model_check — единая проверка качества CV-модели («единая правда»).

Эталонный набор (папка ref_set рядом с mvs.db, собирается scripts/make_ref_set.py) — кадры + контуры кристаллов, как их отметил Макс.
Проверка гоняет кадры через ТЕКУЩИЕ настройки (cv_pipeline — тот же путь, что у живой пробы), меряет результат теми же формулами
и сравнивает с эталоном: сдвиг муки по объёму, сдвиг среднего размера, доля найденных кристаллов. Вне допуска — «не годится».
Результат пишется в mvs.db (таблица model_checks) вместе с «подписью» настроек: сменилась модель / порог / нарезка — проверка устарела.

Измерение эталонных и найденных контуров идёт ОДНИМ фиксированным набором параметров (manifest.measure_cfg), не зависящим от живых
настроек формы: так на результат влияет только то, что видит модель, а не то, как подкрутили пороги формы.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional

import numpy as np

import db
from logger import log_event

REF_DIRNAME = "ref_set"
PRIMARY_ZONE = "fin"                       # зона, по которой выносится вердикт: финиш варки (там мука решает качество)
ZONES = [("start", "начало"), ("mid84", "СВ 84,5"), ("mid86", "СВ 86"), ("end88", "СВ 88"), ("fin", "финиш")]
# допуск (решение Макса 26.10): мука финиша ±10 %, средний размер ±3 %, найдено ≥ 85 %
TOL = {"fines_rel_pct": 10.0, "size_rel_pct": 3.0, "found_min_pct": 85.0}
# настройки, от которых зависит результат проверки (подпись): сменилась любая — проверка устарела
SIG_CV_KEYS = ("tiles", "conf", "iou", "overlap", "conf_big", "conf_big_k", "conf_big_um", "seam_refine", "seam_merge", "seam_in_sieve",
               "refine_max", "cluster_gap_px", "nest_frac", "edge_margin_px", "mask_smooth", "mask_smooth_pct", "um_per_px")
MIN_FRAMES_FAIL = 3                        # столько кадров подряд без ответа сайдкара — прерываем проверку с ошибкой

_lock = threading.Lock()
STATE = {"running": False, "done": 0, "total": 0, "error": None, "cancel": False, "t0": None, "by": None}


# --- эталон ---
def ref_dir(cv: Optional[dict]) -> Path:
    custom = str((cv or {}).get("ref_set_dir") or "").strip()
    return Path(custom) if custom else db.DB_PATH.parent / REF_DIRNAME


_ref_cache: dict = {}


def load_ref(d: Path) -> Optional[dict]:
    """Манифест эталона + маска светлой зоны (кэш по папке, дате манифеста и числу файлов в папке — статус опрашивается каждые секунды;
    число файлов — чтобы недокопированный набор подхватывался, когда остальное докопировали, без перезапуска).
    None — набора нет / он битый; кадров, чьих файлов не хватает, — в ref['missing']."""
    f = Path(d) / "manifest.json"
    if not f.exists():
        _ref_cache.pop(str(d), None)
        return None
    with os.scandir(d) as it:
        n_files = sum(1 for _ in it)
    key = (f.stat().st_mtime, f.stat().st_size, n_files)
    hit = _ref_cache.get(str(d))
    if hit and hit[0] == key:
        return hit[1]
    ref = _load_ref(d, f)
    _ref_cache[str(d)] = (key, ref)
    return ref


def _load_ref(d: Path, f: Path) -> Optional[dict]:
    try:
        man = json.loads(f.read_text(encoding="utf-8"))
        frames = [fr for fr in man["frames"] if (Path(d) / fr["image"]).exists() and (Path(d) / fr["polys"]).exists()]
        if not frames:
            return None
        mask = None
        zf = Path(d) / "zone.png"
        if zf.exists():
            import cv2
            m = cv2.imdecode(np.fromfile(str(zf), np.uint8), cv2.IMREAD_GRAYSCALE)
            mask = m > 127 if m is not None else None
        return {"dir": Path(d), "man": man, "frames": frames, "mask": mask, "missing": len(man["frames"]) - len(frames)}
    except Exception as e:
        log_event("model_check", "Эталон не прочитан", "warn", {"dir": str(d), "error": repr(e)})
        return None


# --- измерения (формулы metrics.py из разбора 16–26.10) ---
class _Shape:
    """Полигон как маска в своей рамке: пересечение двух — в общей части рамок (быстрый IoU)."""
    def __init__(self, poly):
        import cv2
        pts = np.asarray(poly, np.int32).reshape(-1, 2)
        self.x0, self.y0 = pts.min(0)
        x1, y1 = pts.max(0) + 1
        self.m = np.zeros((y1 - self.y0, x1 - self.x0), np.uint8)
        cv2.fillPoly(self.m, [pts - [self.x0, self.y0]], 1)
        self.area = int(self.m.sum())
        self.x1, self.y1 = x1, y1

    def inter(self, o: "_Shape") -> int:
        x0, y0 = max(self.x0, o.x0), max(self.y0, o.y0)
        x1, y1 = min(self.x1, o.x1), min(self.y1, o.y1)
        if x1 <= x0 or y1 <= y0:
            return 0
        a = self.m[y0 - self.y0:y1 - self.y0, x0 - self.x0:x1 - self.x0]
        b = o.m[y0 - o.y0:y1 - o.y0, x0 - o.x0:x1 - o.x0]
        return int((a & b).sum())


def measure(polys: list, sv: Optional[float], mcfg: dict, shape: tuple) -> list:
    """Каждый контур меряем отдельно (чтобы ни склейка, ни мелкий фильтр не путали порядок): [{size_um, …} | None]."""
    import cv_analyzer
    h, w = shape[:2]
    out = []
    for p in polys:
        a = np.array(p, np.float32)
        if a.ndim != 2 or len(a) < 3:
            out.append(None)
            continue
        x1, y1 = a.min(0)
        x2, y2 = a.max(0)
        o = {"polygon": a.tolist(), "bbox": [float(x1), float(y1), float(x2), float(y2)], "conf": 1.0}
        if x1 <= 3 or y1 <= 3 or x2 >= w - 4 or y2 >= h - 4:
            o["edge"] = True
        ms = cv_analyzer.measure_objects([o], mcfg, sv=sv, img_shape=(h, w, 3))
        out.append(SimpleNamespace(size_um=ms[0].size_um, length_um=ms[0].length_um, width_um=ms[0].width_um,
                                   group=ms[0].group, defect=ms[0].defect, cx=ms[0].cx, cy=ms[0].cy, poly=p) if ms else None)
    return out


def _in_zone(m, mask) -> bool:
    if mask is None:
        return True
    h, w = mask.shape
    return bool(mask[int(min(h - 1, max(0, m.cy))), int(min(w - 1, max(0, m.cx)))])


def _zone_objs(polys, sv, mcfg, shape, mask) -> list:
    return [m for m in measure(polys, sv, mcfg, shape) if m is not None and _in_zone(m, mask) and m.group not in ("cut", "bubble")]


def _stats(ms: list, fines_um: float, k: float) -> dict:
    """Объём (M3, призма) хороших кристаллов и доля мельче порога муки; размеры хороших — для среднего."""
    import cv_volume
    good = [m for m in ms if m.defect not in cv_volume.REJECT_DEFECTS]
    vol = lambda m: cv_volume.crystal_volumes(m.size_um, m.length_um, m.width_um, k)["m3"]   # noqa: E731
    return {"vol_good": sum(vol(m) for m in good), "vol_fines": sum(vol(m) for m in good if m.size_um < fines_um),
            "sizes": [m.size_um for m in good]}


def _match(a: list, b: list) -> int:
    """Сколько объектов из a имеют пару в b (IoU ≥ 0,5)."""
    sb = [_Shape(m.poly) for m in b]
    n = 0
    for m in a:
        s = _Shape(m.poly)
        if any(s.inter(x) / max(1, s.area + x.area - s.inter(x)) >= 0.5 for x in sb):
            n += 1
    return n


def frame_compare(ref_ms: list, mod_ms: list, fines_um: float, k: float) -> dict:
    return {"ref": _stats(ref_ms, fines_um, k), "mod": _stats(mod_ms, fines_um, k),
            "n_ref": len(ref_ms), "n_mod": len(mod_ms), "found": _match(ref_ms, mod_ms), "junk": len(mod_ms) - _match(mod_ms, ref_ms)}


def aggregate(rows: list) -> dict:
    """Кадры одной зоны → суммарные цифры: мука по объёму, средний размер, найдено, мусор."""
    def pooled(key):
        vg = sum(r[key]["vol_good"] for r in rows)
        vf = sum(r[key]["vol_fines"] for r in rows)
        sz = [x for r in rows for x in r[key]["sizes"]]
        return (100 * vf / vg if vg else None), (float(np.mean(sz)) if sz else None)
    fr, sr = pooled("ref")
    fm, sm = pooled("mod")
    n_ref, n_mod = sum(r["n_ref"] for r in rows), sum(r["n_mod"] for r in rows)
    return {"n": len(rows), "fines_ref": fr, "fines_model": fm, "size_ref": sr, "size_model": sm,
            "fines_rel": 100 * (fm / fr - 1) if fr and fm is not None else None,
            "size_rel": 100 * (sm / sr - 1) if sr and sm is not None else None,
            "found": 100 * sum(r["found"] for r in rows) / n_ref if n_ref else None,
            "junk": 100 * sum(r["junk"] for r in rows) / n_mod if n_mod else None}


def evaluate(zones: dict, tol: Optional[dict] = None) -> dict:
    """Вердикт по финишной зоне (нет её в наборе — по всем кадрам): {ok, zone, reasons[]}. Нет цифр — не годится, а не «пропущено»."""
    tol = tol or TOL
    zone = PRIMARY_ZONE if PRIMARY_ZONE in zones else "all"
    z = zones.get(zone) or {}
    reasons = []
    if z.get("fines_rel") is None or z.get("size_rel") is None or z.get("found") is None:
        reasons.append("нет цифр для сравнения")
    else:
        if abs(z["fines_rel"]) > tol["fines_rel_pct"]:
            reasons.append("мука сдвинута на %+.0f %% (допуск ±%g %%)" % (z["fines_rel"], tol["fines_rel_pct"]))
        if abs(z["size_rel"]) > tol["size_rel_pct"]:
            reasons.append("размер сдвинут на %+.1f %% (допуск ±%g %%)" % (z["size_rel"], tol["size_rel_pct"]))
        if z["found"] < tol["found_min_pct"]:
            reasons.append("найдено %.0f %% кристаллов (нужно ≥ %g %%)" % (z["found"], tol["found_min_pct"]))
    return {"ok": not reasons, "zone": zone, "reasons": reasons}


# --- подпись настроек ---
def signature(cv: dict, detector: Optional[dict], model_path: Optional[str] = None) -> str:
    """Короткий отпечаток «чем снимаем»: паспорт модели от сайдкара (+ размер и дата файла) и настройки CV, влияющие на результат."""
    st = None
    try:
        if model_path and os.path.exists(model_path):
            s = os.stat(model_path)
            st = [s.st_size, int(s.st_mtime)]
    except OSError:
        pass
    blob = json.dumps({"det": detector or {}, "file": st, "cv": {k: (cv or {}).get(k) for k in SIG_CV_KEYS}},
                      sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def tag(cv: dict, detector: Optional[dict]) -> str:
    """Читаемая метка для строки статуса: «26x_1280.pt@1280 conf 0.25»."""
    import cv_store
    return cv_store._model_tag(detector, {"conf": (cv or {}).get("conf"), "conf_big": (cv or {}).get("conf_big"),
                                          "conf_big_k": (cv or {}).get("conf_big_k")}) or "модель не определена"


# --- прогон ---
def run_check(cv: dict, url: str, ref: dict, detector: Optional[dict], model_path: Optional[str],
              stop: Callable[[], bool] = lambda: False, progress: Callable[[int, int], None] = lambda d, t: None) -> Optional[dict]:
    """Кадры эталона → живой путь (сайдкар + доводка + analyze) → сравнение. None — остановили (stop())."""
    import cv2
    import cv_analyzer
    import cv_pipeline
    man, mask = ref["man"], ref["mask"]
    mcfg, k = man.get("measure_cfg") or {}, float(man.get("k_thick", 0.88))
    frames = ref["frames"]
    rows, by_zone, fails, t0 = [], {}, 0, time.time()
    for i, fr in enumerate(frames):
        if stop():
            return None
        img = cv2.imdecode(np.fromfile(str(ref["dir"] / fr["image"]), np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError("кадр эталона не читается: %s" % fr["image"])
        det = cv_pipeline.detect(url, img, cv)
        if det is None:
            fails += 1
            if fails >= MIN_FRAMES_FAIL:
                raise RuntimeError("сайдкар не отвечает")
            continue
        fails = 0
        sv = fr.get("sv")
        res = cv_analyzer.analyze(img, det["objs"], cv_cfg=cv, with_overlay=False, sv=sv, phase=None)
        ref_polys = json.loads((ref["dir"] / fr["polys"]).read_text(encoding="utf-8"))["polys"]
        shp = img.shape
        row = frame_compare(_zone_objs(ref_polys, sv, mcfg, shp, mask), _zone_objs([o["poly"] for o in res["objects"]], sv, mcfg, shp, mask),
                            float(fr["fines_um"]), k)
        rows.append(row)
        by_zone.setdefault(fr.get("zone") or "all", []).append(row)
        progress(i + 1, len(frames))
    if not rows:
        raise RuntimeError("ни один кадр эталона не разобран")
    zones = {z: aggregate(rs) for z, rs in by_zone.items()}
    zones["all"] = aggregate(rows)
    ev = evaluate(zones)
    return {"t": time.time(), "sig": signature(cv, detector, model_path), "tag": tag(cv, detector), "ok": ev["ok"], "verdict": ev,
            "zones": zones, "tol": dict(TOL), "n_frames": len(rows), "n_ref": len(frames), "sec": round(time.time() - t0),
            "infer": {k2: (cv or {}).get(k2) for k2 in ("tiles", "conf", "iou", "overlap", "conf_big", "conf_big_k")},
            "detector": detector, "ref_created": man.get("created"), "ref_n": len(man["frames"])}


# --- хранение ---
def save(res: dict) -> None:
    db.check_save(res["t"], res["sig"], res["tag"], bool(res["ok"]), json.dumps(res, ensure_ascii=False, default=str))


def last() -> Optional[dict]:
    rows = history(1)
    return rows[0] if rows else None


def history(limit: int = 10) -> list:
    out = []
    for r in db.checks(limit):
        try:
            out.append(json.loads(r["data"]))
        except Exception:
            continue
    return out


# --- предупреждение «модель проверялась с разным результатом» (отчёт и «Сравнение») ---
DRIFT_FINES_PP = 5.0        # между проверками мука финиша сдвинулась больше чем на столько п.п. — результат «разный» даже при одинаковом вердикте
DRIFT_SIZE_PP = 2.0         # то же для размера


def _brief(r: dict) -> dict:
    z = (r.get("zones") or {}).get((r.get("verdict") or {}).get("zone")) or {}
    return {"t": r.get("t"), "ok": bool(r.get("ok")), "tag": r.get("tag"), "fines_rel": z.get("fines_rel"), "size_rel": z.get("size_rel"), "found": z.get("found")}


def _differs(a: dict, b: dict) -> bool:
    if a["ok"] != b["ok"]:
        return True
    for k, lim in (("fines_rel", DRIFT_FINES_PP), ("size_rel", DRIFT_SIZE_PP)):
        if a.get(k) is not None and b.get(k) is not None and abs(a[k] - b[k]) > lim:
            return True
    return False


def drift(t_from: float, t_to: float, rows: Optional[list] = None) -> Optional[dict]:
    """Проверки модели, действующие в периоде [t_from, t_to]: последняя ДО него (так модель стояла в начале) и все внутри. Две соседние дали разный
    результат (вердикт или сдвиг муки / размера) — {text, checks}: цифры муки и размера до и после нельзя сравнивать напрямую. Иначе None."""
    import time
    allr = rows if rows is not None else history(200)                # новые первыми
    inside = [_brief(r) for r in reversed(allr) if t_from <= r["t"] <= t_to]
    before = next((_brief(r) for r in allr if r["t"] < t_from), None)
    seq = ([before] if before else []) + inside
    if len(seq) < 2 or not any(_differs(a, b) for a, b in zip(seq, seq[1:])):
        return None
    seq = seq[-5:]
    f = lambda t: time.strftime("%d.%m %H:%M", time.localtime(t))                      # noqa: E731
    n = lambda v, d: ("—" if v is None else ("%+." + str(d) + "f") % v).replace(".", ",").replace("-", "−")   # noqa: E731
    parts = ["%s%s — %s (мука %s %%, размер %s %%)" % (f(c["t"]), " (до периода)" if c is before else "", "в допуске" if c["ok"] else "вне допуска",
                                                       n(c["fines_rel"], 0), n(c["size_rel"], 1)) for c in seq]
    return {"checks": seq, "detail": "; ".join(parts), "text": "Проверка модели по эталону давала разный результат: " + "; ".join(parts) +
            ". Цифры муки и размера до и после нельзя сравнивать напрямую — часть разницы от настройки распознавания, а не от процесса."}


# --- состояние и запуск ---
def status(cv: dict, health: Optional[dict]) -> dict:
    """Всё для строки на вкладке «CV»: есть ли эталон, последняя проверка, устарела ли она, идёт ли сейчас."""
    ref = load_ref(ref_dir(cv))
    lst = last()
    det = (health or {}).get("detector")
    cur_sig = signature(cv, det, (health or {}).get("model_path")) if health else None
    with _lock:
        st = dict(STATE)
    out = {"ref": {"present": ref is not None, "dir": str(ref_dir(cv)), "n": len(ref["frames"]) if ref else 0,
                   "missing": ref["missing"] if ref else 0, "created": ref["man"].get("created") if ref else None},
           "state": st, "tol": dict(TOL), "current_tag": tag(cv, det) if health else None, "online": health is not None}
    if lst:
        out["last"] = {k: lst.get(k) for k in ("t", "tag", "ok", "verdict", "n_frames", "sec", "ref_n")}
        out["last"]["zone"] = (lst.get("zones") or {}).get((lst.get("verdict") or {}).get("zone")) or {}
        out["stale"] = (cur_sig is not None and lst.get("sig") != cur_sig)
    else:
        out["last"], out["stale"] = None, cur_sig is not None
    out["history"] = [{"t": r.get("t"), "tag": r.get("tag"), "ok": r.get("ok"),
                       "fines_rel": ((r.get("zones") or {}).get((r.get("verdict") or {}).get("zone")) or {}).get("fines_rel")} for r in history(8)]
    return out


def start(cv: dict, url: str, health: Optional[dict], stop: Callable[[], bool], by: str = "manual") -> dict:
    """Запустить проверку в фоне. {started: bool, reason}. Одна за раз; без эталона / без сайдкара — отказ с причиной."""
    ref = load_ref(ref_dir(cv))
    if ref is None:
        return {"started": False, "reason": "no_ref"}
    if health is None:
        return {"started": False, "reason": "sidecar_offline"}
    with _lock:
        if STATE["running"]:
            return {"started": False, "reason": "running"}
        STATE.update(running=True, done=0, total=len(ref["frames"]), error=None, cancel=False, t0=time.time(), by=by)
    cv = json.loads(json.dumps(cv or {}))          # снимок настроек на момент старта: правка во время прогона его не меняет

    def work():
        res = None
        try:
            def _stop():
                return STATE["cancel"] or stop()
            def _prog(d, t):
                STATE["done"] = d
            res = run_check(cv, url, ref, health.get("detector"), health.get("model_path"), stop=_stop, progress=_prog)
            if res is not None:
                save(res)
                log_event("model_check", "Модель проверена: %s" % ("в допуске" if res["ok"] else "ВНЕ допуска"), "info" if res["ok"] else "warn",
                          {"tag": res["tag"], "reasons": res["verdict"]["reasons"], "sec": res["sec"], "by": by})
            else:
                log_event("model_check", "Проверка модели прервана", "info", {"by": by})
        except Exception as e:
            STATE["error"] = str(e)
            log_event("model_check", "Проверка модели не удалась", "warn", {"error": repr(e), "by": by})
        finally:
            STATE["running"] = False
    threading.Thread(target=work, name="model-check", daemon=True).start()
    return {"started": True}


def cancel() -> None:
    STATE["cancel"] = True
