"""motion_client — HTTP-клиент к сервису движения (motion_service/: датчик вращения, шахта, лента) из основного приложения.

Как video_client: сервис — отдельный процесс рядом, тут только тонкий клиент. Через ProxyHandler({}) — БЕЗ системного
прокси (иначе запрос к localhost может виснуть). Ошибки глушим: сервис необязателен, его недоступность не должна
ронять основное приложение — вернётся {"error": текст}.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

DEFAULT_URL = "http://127.0.0.1:8768"
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _base(service_url: str) -> str:
    return (service_url or DEFAULT_URL).rstrip("/")


def _call(service_url: str, path: str, body: Optional[dict] = None, method: Optional[str] = None, timeout: float = 5.0) -> dict:
    """GET (или POST/PUT с JSON) → ответ сервиса. Ошибка сервиса — {"error": его текст}, нет связи — {"error": "…не отвечает"}."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(_base(service_url) + path, data=data, method=method or ("POST" if data is not None else "GET"),
                                 headers={"Content-Type": "application/json"} if data is not None else {})
    try:
        with _opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return {"error": json.loads(e.read().decode("utf-8")).get("detail") or "HTTP %d" % e.code}
        except Exception:
            return {"error": "HTTP %d" % e.code}
    except Exception:
        return {"error": "сервис движения не отвечает (%s)" % _base(service_url)}


def health(service_url: str, timeout: float = 1.0) -> Optional[dict]:
    """Статус сервиса или None, если не запущен."""
    r = _call(service_url, "/health", timeout=timeout)
    return None if "error" in r else r


def get_config(service_url: str) -> dict:
    return _call(service_url, "/config")


def put_config(service_url: str, cfg: dict) -> dict:
    return _call(service_url, "/config", cfg, method="PUT", timeout=15.0)


def state(service_url: str, timeout: float = 5.0) -> dict:
    return _call(service_url, "/state", timeout=timeout)


def history(service_url: str, cam: str, n: int = 300, window: float = 0, points: int = 600) -> dict:
    return _call(service_url, "/cameras/%s/history?n=%d&window=%s&points=%d" % (urllib.parse.quote(cam, safe=""), int(n), float(window), int(points)))


def stats(service_url: str, cam: str, window: float = 10800.0) -> dict:
    return _call(service_url, "/cameras/%s/stats?window=%s" % (urllib.parse.quote(cam, safe=""), float(window)))


def visibility(service_url: str, cam: str, window: float = 86400.0) -> dict:
    return _call(service_url, "/cameras/%s/visibility?window=%s" % (urllib.parse.quote(cam, safe=""), float(window)), timeout=15.0)


def mirror_table(service_url: str, cam: str, group: str) -> dict:
    """Таблица поправки зеркальности валов по последним минутам работы (снимать, пока валы ровные)."""
    return _call(service_url, "/cameras/%s/mirror_table" % urllib.parse.quote(cam, safe=""), {"group": str(group)}, timeout=10.0)


def record(service_url: str, cam: str, on: bool) -> dict:
    return _call(service_url, "/cameras/%s/record" % urllib.parse.quote(cam, safe=""), {"on": bool(on)})


def record_all(service_url: str, on: bool) -> dict:
    return _call(service_url, "/record", {"on": bool(on)})


def records_zip(service_url: str, days: float = 7.0, timeout: float = 120.0):
    """(байты zip, None) или (None, текст ошибки): архив записей метрик (CSV + summary.txt)."""
    try:
        with _opener.open(_base(service_url) + "/records.zip?days=%s" % float(days), timeout=timeout) as r:
            return r.read(), None
    except urllib.error.HTTPError as e:
        try:
            return None, json.loads(e.read().decode("utf-8")).get("detail") or "HTTP %d" % e.code
        except Exception:
            return None, "HTTP %d" % e.code
    except Exception:
        return None, "сервис движения не отвечает (%s)" % _base(service_url)


def modbus_ping(service_url: str, body: dict) -> dict:
    return _call(service_url, "/modbus/ping", body, timeout=8.0)


def logs(service_url: str, n: int = 100) -> dict:
    return _call(service_url, "/logs?n=%d" % int(n))


def level_import(service_url: str, ini_text: str) -> dict:
    """config.ini от LVLDec → фрагмент настроек камеры (зоны, пороги, ПЛК); ничего не сохраняет."""
    return _call(service_url, "/level/import", {"ini": ini_text}, timeout=8.0)


def level_shot(service_url: str, cam: str, group: str, ts: float, direction: int = 0, timeout: float = 5.0):
    """(jpeg-байты, время снимка) — снимок графика уровня, ближайший к ts (direction −1/+1 — соседний раньше/позже),
    или None (нет снимка / сервис молчит)."""
    q = urllib.parse.urlencode({"group": group, "ts": float(ts), "dir": int(direction)})
    url = _base(service_url) + "/cameras/%s/level_shot?%s" % (urllib.parse.quote(cam, safe=""), q)
    try:
        with _opener.open(url, timeout=timeout) as r:
            return r.read(), int(r.headers.get("X-Shot-Ts") or 0)
    except Exception:
        return None


def level_shots(service_url: str, cam: str, group: str, since: float = 0.0) -> dict:
    """Снимки уровня с разметкой кадра новее since: {"shots": [{ts, meta}]} или {"error"}."""
    q = urllib.parse.urlencode({"group": group, "since": float(since)})
    return _call(service_url, "/cameras/%s/level_shots?%s" % (urllib.parse.quote(cam, safe=""), q), timeout=8.0)


def snapshot(service_url: str, cam: str, width: int = 0, timeout: float = 5.0):
    """(jpeg-байты, ширина оригинала, высота оригинала) или None, если кадра нет/сервис молчит."""
    url = _base(service_url) + "/cameras/%s/snapshot?w=%d" % (urllib.parse.quote(cam, safe=""), int(width))
    try:
        with _opener.open(url, timeout=timeout) as r:
            return r.read(), int(r.headers.get("X-Frame-W") or 0), int(r.headers.get("X-Frame-H") or 0)
    except Exception:
        return None
