"""video_client — HTTP-клиент к видео-сервису (video_service/, ролик варки) из основного приложения.

Как cv_client: сервис — отдельный процесс рядом, тут только тонкий клиент. Через ProxyHandler({}) — БЕЗ системного
прокси (иначе запрос к localhost может виснуть, см. память server-proxy-urllib). Ошибки глушим: видео необязательно,
его недоступность не должна ронять основное приложение.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

DEFAULT_URL = "http://127.0.0.1:8767"
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _base(service_url: str) -> str:
    return (service_url or DEFAULT_URL).rstrip("/")


def _call(service_url: str, path: str, body: Optional[dict] = None, timeout: float = 5.0) -> dict:
    """GET (или POST с JSON) → ответ сервиса. Ошибка сервиса — {"error": его текст}, нет связи — {"error": "…не отвечает"}."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(_base(service_url) + path, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with _opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return {"error": json.loads(e.read().decode("utf-8")).get("detail") or "HTTP %d" % e.code}
        except Exception:
            return {"error": "HTTP %d" % e.code}
    except Exception:
        return {"error": "видео-сервис не отвечает (%s)" % _base(service_url)}


def health(service_url: str, timeout: float = 1.0) -> Optional[dict]:
    """Статус сервиса или None, если не запущен."""
    r = _call(service_url, "/health", timeout=timeout)
    return None if "error" in r else r


def build(service_url: str, dirs: list[str], name: str) -> dict:
    """Запустить сборку ролика по папкам проб → {"id"} или {"error"}."""
    return _call(service_url, "/build", {"dirs": dirs, "name": name}, timeout=10.0)


def job(service_url: str, jid: str) -> dict:
    return _call(service_url, "/jobs/" + urllib.parse.quote(jid, safe=""))


def files(service_url: str) -> dict:
    return _call(service_url, "/files")


def open_file(service_url: str, name: str, timeout: float = 30.0):
    """Открытый ответ сервиса с роликом (читать кусками и закрыть) или None."""
    try:
        return _opener.open(_base(service_url) + "/file/" + urllib.parse.quote(name, safe=""), timeout=timeout)
    except Exception:
        return None
