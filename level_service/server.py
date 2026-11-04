"""Сервис уровня (сайдкар): держит YOLO-модель и отдаёт контуры найденного на кадре. Порт по умолчанию 8769 (LEVEL_PORT).

Отдельный процесс рядом с web_MVS (как cv_service / motion_service), со своим venv (torch + ultralytics, см. README).
Кадры берёт у камеры motion_service и шлёт сюда по localhost; уровень по зонам и запись в ПЛК считает уже он.
Сервис про «жидкость» ничего не знает: отдаёт объекты ВСЕХ классов модели, так что подойдёт любая seg/detect-модель.

Эндпоинты:
  GET  /health                   — жив ли, загружена ли модель, устройство, версия.
  GET  /model                    — метаданные модели (имя, классы, размер входа, устройство).
  GET  /models                   — файлы в model/ и какой из них загружен.
  POST /model/upload?name=x.pt   — залить .pt/.onnx (сырые байты), сохранить в model/ и сразу загрузить (горячо).
  POST /model/load  {"path":…}   — загрузить модель с диска (относительный путь — от папки сервиса).
  POST /infer?conf=&iou=&imgsz=&max_det=&geometry=  — тело: кадр (jpg/png) → {objects:[{cls,name,conf,bbox,polygons}], …}.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from seg_model import GEOMETRIES, MODEL_EXTS

BASE_DIR = Path(__file__).resolve().parent


def _read_version() -> str:
    """Версия сервиса = номер релиза, с которым собран архив (build.bat кладёт файл VERSION). Из исходников файла нет — «dev»."""
    try:
        return (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip() or "dev"
    except Exception:
        return "dev"


def default_factory(path: str, device=None, imgsz=None):
    from seg_model import SegModel
    return SegModel(path, device=device, imgsz=imgsz)


def create_app(model_dir: Optional[Path] = None, model_factory: Callable = default_factory, boot_path: Optional[str] = None) -> FastAPI:
    """Приложение. model_factory(path) → объект с .info() и .infer(img, …) — в тестах подменяется заглушкой без torch."""
    mdir = Path(model_dir or (BASE_DIR / "model"))
    mdir.mkdir(parents=True, exist_ok=True)
    last_file = mdir / "last_model.json"          # какую модель грузили последней — её же берём при старте
    version = _read_version()
    app = FastAPI(title="web_MVS level service", version=version)
    started = time.time()
    state = {"model": None, "error": ""}
    swap = threading.Lock()                       # замена модели
    run = threading.Lock()                        # модель считает по одному кадру: два разбора разом удвоили бы память и время

    def resolve(p: str) -> str:
        return p if os.path.isabs(p) else str(BASE_DIR / p)

    def remember(p: Optional[str]):
        try:
            last_file.write_text(json.dumps({"path": p}, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            print("не удалось запомнить модель:", e)

    def recall() -> Optional[str]:
        try:
            p = (json.loads(last_file.read_text(encoding="utf-8")) or {}).get("path")
        except Exception:
            return None
        return p if p and os.path.exists(resolve(p)) else None

    def load(path: str) -> dict:
        """Загрузить модель и подменить текущую. Бросает при ошибке — старая модель остаётся."""
        m = model_factory(resolve(path))
        with swap:
            state["model"], state["error"] = m, ""
        remember(path)
        return m.info()

    def boot():
        """Старт: последняя загруженная > LEVEL_MODEL (run.bat) > без модели. Битая запомненная не мешает старту."""
        for cand in (recall(), boot_path if boot_path is not None else (os.environ.get("LEVEL_MODEL", "").strip() or None)):
            if not cand:
                continue
            try:
                load(cand)
                return
            except Exception as e:
                state["error"] = "модель %s не загрузилась: %s" % (cand, e)
                print(state["error"])

    boot()

    def cur():
        with swap:
            return state["model"]

    @app.get("/health")
    def health():
        m = cur()
        return {"ok": True, "version": version, "uptime_s": round(time.time() - started, 1),
                "model": m.info() if m else None, "error": state["error"]}

    @app.get("/model")
    def model():
        m = cur()
        return m.info() if m else {"error": state["error"] or "модель не загружена"}

    @app.get("/models")
    def models():
        m = cur()
        files = sorted(p.name for p in mdir.iterdir() if p.is_file() and p.suffix.lower() in MODEL_EXTS)
        return {"files": files, "current": m.name if m else None}

    @app.post("/model/load")
    async def model_load(request: Request):
        path = ((await request.json()) or {}).get("path")
        if not path:
            return JSONResponse({"error": "не указан путь к модели"}, status_code=400)
        try:
            return {"status": "ok", "model": await run_in_threadpool(load, path)}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.post("/model/upload")
    async def model_upload(request: Request, name: str = "model.pt"):
        raw = await request.body()
        if not raw or len(raw) < 1000:
            return JSONResponse({"error": "файл пустой или слишком маленький"}, status_code=400)
        safe = "".join(c for c in os.path.basename(name) if c.isalnum() or c in "-_.") or "model.pt"
        if not safe.lower().endswith(MODEL_EXTS):
            safe += ".pt"
        dst = mdir / safe
        try:
            dst.write_bytes(raw)
            rel = os.path.join("model", safe) if mdir == BASE_DIR / "model" else str(dst)
            return {"status": "ok", "saved": str(dst), "size": len(raw), "model": await run_in_threadpool(load, rel)}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    def _infer(m, img, conf, iou, imgsz, max_det, geometry):
        t = time.time()
        with run:
            objs = m.infer(img, conf=conf, iou=iou, imgsz=imgsz, max_det=max_det, geometry=geometry)
        return objs, round((time.time() - t) * 1000.0)

    @app.post("/infer")
    async def infer(request: Request, conf: float = 0.35, iou: float = 0.7, imgsz: int = 0, max_det: int = 300,
                    geometry: str = "lvldec"):
        raw = await request.body()               # тело читаем ДО любых отказов: иначе клиент, ещё шлющий кадр, получает обрыв соединения вместо ответа
        m = cur()
        if m is None:
            return JSONResponse({"error": "модель не загружена"}, status_code=409)
        if geometry not in GEOMETRIES:
            return JSONResponse({"error": "geometry — одно из %s" % ", ".join(GEOMETRIES)}, status_code=400)
        img = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR) if raw else None
        if img is None:
            return JSONResponse({"error": "кадр не разобрался"}, status_code=400)
        # модель — в потоке: пока кадр считается (секунды на процессоре), /health отвечает и web_MVS не считает сервис упавшим
        objs, ms = await run_in_threadpool(_infer, m, img, conf, iou, imgsz or None, max_det, geometry)
        h, w = img.shape[:2]
        return {"image": {"w": w, "h": h}, "geometry": geometry, "count": len(objs), "objects": objs,
                "timing": {"ms": ms}, "model": m.info()["name"]}

    return app


def main():
    import uvicorn
    port = int(os.environ.get("LEVEL_PORT", "8769"))
    host = os.environ.get("LEVEL_HOST", "127.0.0.1")
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
