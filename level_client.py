"""level_client — HTTP-клиент к сервису уровня (level_service/: YOLO-модель → контуры) из основного приложения.

Как cv_client / motion_client: сервис — отдельный процесс рядом, тут только тонкий клиент. Через ProxyHandler({}) — БЕЗ системного
прокси (иначе запрос к localhost может виснуть). Ошибки глушим: сервис необязателен, его недоступность не должна ронять
основное приложение — вернётся {"error": текст}.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

DEFAULT_URL = "http://127.0.0.1:8770"
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _base(service_url: str) -> str:
    return (service_url or DEFAULT_URL).rstrip("/")


def _call(service_url: str, path: str, body=None, raw: Optional[bytes] = None, timeout: float = 5.0) -> dict:
    """GET (или POST: JSON-тело `body` либо сырые байты `raw`) → ответ сервиса. Ошибка сервиса — {"error": его текст},
    нет связи — {"error": "…не отвечает"}."""
    if raw is not None:
        data, headers = raw, {"Content-Type": "application/octet-stream"}
    elif body is not None:
        data, headers = json.dumps(body).encode("utf-8"), {"Content-Type": "application/json"}
    else:
        data, headers = None, {}
    req = urllib.request.Request(_base(service_url) + path, data=data, method="POST" if data is not None else "GET", headers=headers)
    try:
        with _opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return {"error": json.loads(e.read().decode("utf-8")).get("error") or "HTTP %d" % e.code}
        except Exception:
            return {"error": "HTTP %d" % e.code}
    except Exception:
        return {"error": "сервис уровня не отвечает (%s)" % _base(service_url)}


def health(service_url: str, timeout: float = 1.0) -> Optional[dict]:
    """Статус сервиса или None, если не запущен."""
    r = _call(service_url, "/health", timeout=timeout)
    return None if "error" in r else r


def model_info(service_url: str, timeout: float = 2.0) -> dict:
    return _call(service_url, "/model", timeout=timeout)


def models(service_url: str, timeout: float = 2.0) -> dict:
    return _call(service_url, "/models", timeout=timeout)


def model_upload(service_url: str, data: bytes, name: str = "model.pt", timeout: float = 180.0) -> dict:
    """Залить .pt/.onnx (сырые байты) — сервис сохранит в model/ и горячо загрузит."""
    return _call(service_url, "/model/upload?name=" + urllib.parse.quote(name), raw=data, timeout=timeout)


def model_load(service_url: str, path: str, timeout: float = 120.0) -> dict:
    """Загрузить модель по пути на диске сервиса (горячо)."""
    return _call(service_url, "/model/load", body={"path": path}, timeout=timeout)
