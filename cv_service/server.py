"""CV-сайдкар: отдельный процесс с детектором кристаллов (YOLO-seg / классика / заглушка).

Запускается РЯДОМ с основным web_MVS (не внутри его exe): обычный Python-venv, где штатно
ставится torch-cuda + ultralytics (см. README). Основное приложение шлёт сюда кадры пробы
по localhost и получает детекции; измерения/группировку делает уже основной app (cv_analyzer).

Эндпоинты:
  GET  /health          — жив ли, загружен ли детектор, устройство, путь модели.
  GET  /model           — метаданные детектора (имя, seg, устройство, классы).
  POST /model/load      — {"path": "model/best.pt"} загрузить модель с диска (горячо, без рестарта).
  POST /model/upload?name=best.pt — залить .pt (сырые байты) в model/, сохранить и загрузить.
  POST /infer           — тело: сырые байты кадра; query: tiles, conf, iou, overlap.

Порт по умолчанию 8765 (CV_PORT). Модель по умолчанию из CV_MODEL (путь к .pt) или 'classic'.
"""
from __future__ import annotations

import os
import threading
import time

import cv2
import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from detector import build_detector
from sahi_tiler import run_tiled

MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model")
os.makedirs(MODEL_DIR, exist_ok=True)

MODEL_PATH = os.environ.get("CV_MODEL", "").strip() or None
AVG_COUNT = int(os.environ.get("CV_STUB_COUNT", "40"))

app = FastAPI(title="web_MVS CV service", version="0.3.0")
_lock = threading.Lock()                       # защита на время горячей замены модели
_detector = build_detector(MODEL_PATH, avg_count=AVG_COUNT)
_model_path = MODEL_PATH
_started = time.time()


def _resolve(path: str) -> str:
    """Путь к модели: абсолютный как есть, относительный — от каталога сайдкара."""
    if os.path.isabs(path):
        return path
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), path)


def _load(path: str | None):
    """Горячо заменить детектор на модель по пути (или 'classic'/пусто). Бросает при ошибке."""
    global _detector, _model_path
    det = build_detector(path if (not path or path == "classic") else _resolve(path),
                         avg_count=AVG_COUNT)
    with _lock:
        _detector = det
        _model_path = path
    return _detector.info()


@app.get("/health")
def health():
    return {
        "ok": True,
        "uptime_s": round(time.time() - _started, 1),
        "detector": _detector.info(),
        "model_path": _model_path,
        "stub": _model_path is None,
    }


@app.get("/model")
def model():
    info = dict(_detector.info())
    info["model_path"] = _model_path
    return info


@app.post("/model/load")
async def model_load(request: Request):
    body = await request.json()
    path = (body or {}).get("path")
    if not path:
        return JSONResponse({"error": "no_path"}, status_code=400)
    try:
        info = _load(path)
        return {"status": "ok", "detector": info, "model_path": _model_path}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/model/upload")
async def model_upload(request: Request, name: str = "best.pt"):
    """Залить .pt (сырые байты тела) в model/<name>, сохранить и сразу загрузить."""
    raw = await request.body()
    if not raw or len(raw) < 1000:
        return JSONResponse({"error": "empty_or_too_small"}, status_code=400)
    safe = "".join(c for c in os.path.basename(name) if c.isalnum() or c in "-_.") or "best.pt"
    if not safe.lower().endswith((".pt", ".onnx", ".engine")):
        safe += ".pt"
    dst = os.path.join(MODEL_DIR, safe)
    try:
        with open(dst, "wb") as f:
            f.write(raw)
        info = _load(os.path.join("model", safe))
        return {"status": "ok", "saved": dst, "size": len(raw),
                "detector": info, "model_path": _model_path}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/infer")
async def infer(request: Request, tiles: int = 6, conf: float = 0.25,
                iou: float = 0.45, overlap: float = 0.15):
    raw = await request.body()
    if not raw:
        return JSONResponse({"error": "empty_body"}, status_code=400)
    buf = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img is None:
        return JSONResponse({"error": "decode_failed"}, status_code=400)

    with _lock:
        det = _detector
    dets, timing = run_tiled(det, img, tiles=tiles, conf=conf, iou_thr=iou, overlap=overlap)
    h, w = img.shape[:2]
    return {
        "image": {"w": w, "h": h},
        "count": len(dets),
        "objects": [d.as_dict() for d in dets],
        "timing": timing,
        "detector": det.info(),
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("CV_PORT", "8765"))
    host = os.environ.get("CV_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port, log_level="info")
