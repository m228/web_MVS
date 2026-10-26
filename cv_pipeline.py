"""cv_pipeline — кадр → сайдкар (детекции) → доводка швов. Один путь и для живой пробы (microscope_service),
и для проверки модели по эталону (model_check): проверка меряет ровно то, что считается на варке.
Измерения (cv_analyzer.analyze) остаются на вызывающем: у пробы они зависят от СВ, фазы и возраста затравки."""
from __future__ import annotations

import time
from typing import Optional


def infer_params(cv: dict) -> dict:
    return {"tiles": int(cv.get("tiles", 6)), "conf": float(cv.get("conf", 0.25)),
            "iou": float(cv.get("iou", 0.45)), "overlap": float(cv.get("overlap", 0.15))}


def infer_meta(cv: dict, inf: dict) -> dict:
    """Чем и как снята проба: параметры, что реально ушли в infer (+ защита крупных) — идут в result.json и метку модели."""
    meta = {**inf, "seam_refine": bool(cv.get("seam_refine", True)) and inf["tiles"] > 1}
    if float(cv.get("conf_big") or 0) > 0:
        meta.update(conf_big=float(cv["conf_big"]), conf_big_k=float(cv.get("conf_big_k") or 4.0))
    return meta


def detect(url: str, img, cv: dict) -> Optional[dict]:
    """Кадр (BGR) → сайдкар → (если включено) повторный проход по окнам вокруг обрубков у шва.
    None — сайдкар не ответил. Иначе {resp, inf, meta, objs, timing}: timing — от сайдкара плюс время и счётчики доводки."""
    import cv2
    import cv_analyzer
    import cv_client
    ok, enc = cv2.imencode(".png", img)
    inf = infer_params(cv)
    resp = cv_client.infer(url, enc.tobytes(), **inf) if ok else None
    if not resp:
        return None
    meta = infer_meta(cv, inf)
    objs = resp.get("objects", [])
    timing = resp.get("timing", {})
    if meta["seam_refine"]:
        # обрубки у шва нарезки — повторный проход модели по окну вокруг них (целый кристалл
        # идёт в рассев). Сбой запроса = обрубок остаётся как был.
        def _infer_window(crop, _url=url, _inf=inf):
            okw, encw = cv2.imencode(".png", crop)
            rw = cv_client.infer(_url, encw.tobytes(), **{**_inf, "tiles": 1}, timeout=30.0) if okw else None
            return rw.get("objects") if rw else None
        t_ref = time.time()
        ref = {}
        objs = cv_analyzer.refine_seam_stubs(img, objs, cv, _infer_window, ref)
        timing = {**timing, "refine_ms": round((time.time() - t_ref) * 1000),
                  "refine_stubs": ref.get("stubs", 0), "refine_fixed": ref.get("fixed", 0)}
    return {"resp": resp, "inf": inf, "meta": meta, "objs": objs, "timing": timing}
