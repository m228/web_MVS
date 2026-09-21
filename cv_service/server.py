"""CV-сайдкар: отдельный процесс с детектором кристаллов (Фаза 1 — заглушка).

Запускается РЯДОМ с основным web_MVS (не внутри его exe): обычный Python-venv, где штатно
ставится torch-cuda/onnxruntime-gpu (см. README). Основное приложение шлёт сюда кадры пробы
по localhost и получает детекции; измерения/группировку делает уже основной app (cv_analyzer).

Эндпоинты:
  GET  /health          — жив ли, загружен ли детектор, устройство.
  GET  /model           — метаданные детектора (имя, seg, устройство).
  POST /infer           — тело: image (файл) + query: tiles, conf, iou, overlap.
                          Ответ: объекты в координатах кадра + тайминги.

Порт по умолчанию 8765 (правится переменной окружения CV_PORT).
"""
from __future__ import annotations

import os
import time

import cv2
import numpy as np
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import JSONResponse

from detector import build_detector
from sahi_tiler import run_tiled

MODEL_PATH = os.environ.get("CV_MODEL", "").strip() or None
AVG_COUNT = int(os.environ.get("CV_STUB_COUNT", "40"))

app = FastAPI(title="web_MVS CV service", version="0.1.0")
_detector = build_detector(MODEL_PATH, avg_count=AVG_COUNT)
_started = time.time()


@app.get("/health")
def health():
    return {
        "ok": True,
        "uptime_s": round(time.time() - _started, 1),
        "detector": _detector.info(),
        "model_path": MODEL_PATH,
        "stub": MODEL_PATH is None,
    }


@app.get("/model")
def model():
    return _detector.info()


@app.post("/infer")
async def infer(image: UploadFile = File(...), tiles: int = 6, conf: float = 0.25,
                iou: float = 0.45, overlap: float = 0.15):
    raw = await image.read()
    buf = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img is None:
        return JSONResponse({"error": "decode_failed"}, status_code=400)

    dets, timing = run_tiled(_detector, img, tiles=tiles, conf=conf,
                             iou_thr=iou, overlap=overlap)
    h, w = img.shape[:2]
    return {
        "image": {"w": w, "h": h},
        "count": len(dets),
        "objects": [d.as_dict() for d in dets],
        "timing": timing,
        "detector": _detector.info(),
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("CV_PORT", "8765"))
    host = os.environ.get("CV_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port, log_level="info")
