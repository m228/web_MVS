"""cv_client — HTTP-клиент к CV-сайдкару из основного приложения.

Сайдкар — отдельный процесс (см. cv_service/). Тут только тонкий клиент: health + infer.
Через ProxyHandler({}) — БЕЗ системного прокси, иначе запрос к localhost может виснуть
(та же беда, что с CGI к платам, см. память server-proxy-urllib). Все ошибки глушим и
возвращаем None/флаг — CV необязателен, его недоступность не должна ронять основное приложение.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Optional

from logger import log_event

# opener без прокси — localhost не должен ходить через системный прокси
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _base(service_url: str) -> str:
    return (service_url or "http://127.0.0.1:8765").rstrip("/")


def health(service_url: str, timeout: float = 1.5) -> Optional[dict]:
    """Статус сайдкара или None, если недоступен."""
    try:
        req = urllib.request.Request(_base(service_url) + "/health", method="GET")
        with _opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def infer(service_url: str, image_bytes: bytes, tiles: int = 6, conf: float = 0.25,
          iou: float = 0.45, overlap: float = 0.15, timeout: float = 20.0) -> Optional[dict]:
    """Отправить кадр (сырые байты png/jpg) в сайдкар. Вернуть детекции+тайминги или None.

    Ответ: {image:{w,h}, count, objects:[{bbox,conf,polygon}], timing, detector}.
    """
    q = f"?tiles={tiles}&conf={conf}&iou={iou}&overlap={overlap}"
    url = _base(service_url) + "/infer" + q
    try:
        req = urllib.request.Request(url, data=image_bytes, method="POST",
                                     headers={"Content-Type": "application/octet-stream"})
        with _opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log_event("cv_client", "Ошибка запроса к CV-сайдкару", "warn",
                  {"url": _base(service_url), "error": str(e)})
        return None


def model_info(service_url: str, timeout: float = 2.0) -> Optional[dict]:
    """Метаданные текущей модели сайдкара (имя/seg/устройство/классы/путь)."""
    try:
        req = urllib.request.Request(_base(service_url) + "/model", method="GET")
        with _opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def model_upload(service_url: str, data: bytes, name: str = "best.pt",
                 timeout: float = 120.0) -> Optional[dict]:
    """Залить .pt (сырые байты) на сайдкар — сохранит в model/ и горячо загрузит."""
    url = _base(service_url) + "/model/upload?name=" + urllib.parse.quote(name)
    try:
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/octet-stream"})
        with _opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log_event("cv_client", "Ошибка загрузки модели в сайдкар", "warn", {"error": str(e)})
        return None


def model_load(service_url: str, path: str, timeout: float = 60.0) -> Optional[dict]:
    """Загрузить модель на сайдкаре по пути на его диске (горячо)."""
    try:
        body = json.dumps({"path": path}).encode("utf-8")
        req = urllib.request.Request(_base(service_url) + "/model/load", data=body,
                                     method="POST", headers={"Content-Type": "application/json"})
        with _opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log_event("cv_client", "Ошибка загрузки модели по пути", "warn", {"error": str(e)})
        return None
