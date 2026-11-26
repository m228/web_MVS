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


def perf(service_url: str, timeout: float = 2.0) -> dict:
    """Замеры времени обработки кадров сервиса движения (валы, зоны, сок, уровень) — /perf."""
    return _call(service_url, "/perf", timeout=timeout)


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


def loss_map(service_url: str, cam: str, window: float = 7 * 86400.0) -> dict:
    """Карта потерь палок: доля «найдена» по секторам угла и часам суток за период."""
    return _call(service_url, "/cameras/%s/loss_map?window=%s" % (urllib.parse.quote(cam, safe=""), float(window)), timeout=30.0)


def autocal_start(service_url: str, cam: str, zones: list, seconds: float = 150.0) -> dict:
    """Запустить подгонку валов: запись вращения (75–300 с) и подбор цветов, оси и радиуса."""
    return _call(service_url, "/cameras/%s/autocal" % urllib.parse.quote(cam, safe=""), {"zones": list(zones or []), "seconds": float(seconds)}, timeout=10.0)


def autocal_status(service_url: str, cam: str) -> dict:
    return _call(service_url, "/cameras/%s/autocal" % urllib.parse.quote(cam, safe=""), timeout=10.0)


def autocal_cancel(service_url: str, cam: str) -> dict:
    return _call(service_url, "/cameras/%s/autocal/cancel" % urllib.parse.quote(cam, safe=""), {}, timeout=10.0)


def autocal_apply(service_url: str, cam: str, zones: list) -> dict:
    """Применить результат подгонки (сохраняет настройки зон камеры)."""
    return _call(service_url, "/cameras/%s/autocal/apply" % urllib.parse.quote(cam, safe=""), {"zones": list(zones or [])}, timeout=20.0)


def autocal_report(service_url: str, cam: str, days: int = 10, shots: int = 16) -> tuple:
    """HTML-отчёт «день / ночь» по видимости палок (собирается секунд 10–30): (страница, None) или (None, текст ошибки)."""
    url = _base(service_url) + "/cameras/%s/autocal/report?days=%d&shots=%d" % (urllib.parse.quote(cam, safe=""), int(days), int(shots))
    try:
        with _opener.open(urllib.request.Request(url), timeout=120.0) as r:
            return r.read().decode("utf-8"), None
    except urllib.error.HTTPError as e:
        try:
            return None, json.loads(e.read().decode("utf-8")).get("detail") or "HTTP %d" % e.code
        except Exception:
            return None, "HTTP %d" % e.code
    except Exception:
        return None, "сервис движения не отвечает (%s)" % _base(service_url)


def mirror_table(service_url: str, cam: str, group: str) -> dict:
    """Таблица поправки зеркальности валов по последним минутам работы (снимать, пока валы ровные)."""
    return _call(service_url, "/cameras/%s/mirror_table" % urllib.parse.quote(cam, safe=""), {"group": str(group)}, timeout=10.0)


def clarity_sample(service_url: str, cam: str, zones: list, seconds: float = 30.0) -> dict:
    """Цвет кругов-прозрачности за последние секунды (медиана Lab) — эталон «чистый» / «грязный»; сохраняет интерфейс."""
    return _call(service_url, "/cameras/%s/clarity_sample" % urllib.parse.quote(cam, safe=""), {"zones": list(zones or []), "seconds": float(seconds)}, timeout=10.0)


def camera_state(service_url: str, cam: str) -> dict:
    return _call(service_url, "/cameras/%s/state" % urllib.parse.quote(cam, safe=""), timeout=2.0)


def shafts_live(service_url: str) -> dict:
    return _call(service_url, "/shafts/live", timeout=2.0)


def shafts_history(service_url: str, pair: str, window: float = 3600.0, points: int = 600) -> dict:
    return _call(service_url, "/shafts/history?pair=%s&window=%s&points=%d" % (urllib.parse.quote(pair, safe=""), float(window), int(points)))


def shafts_stats(service_url: str, pair: str, window: float = 3600.0) -> dict:
    return _call(service_url, "/shafts/stats?pair=%s&window=%s" % (urllib.parse.quote(pair, safe=""), float(window)))


def shafts_calibrate(service_url: str, pair: str, window: Optional[float] = None) -> dict:
    """Эталон разности углов пары валов = текущее положение (валы должны стоять «в ноль») или, с window, среднее за последние window секунд."""
    body = {"pair": str(pair)}
    if window is not None:
        body["window"] = float(window)
    return _call(service_url, "/shafts/calibrate", body, timeout=10.0)


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


def video_rec(service_url: str) -> dict:
    """Запись видео со звуком на диск: настройки и ход по камерам."""
    return _call(service_url, "/video_rec")


def video_rec_limit(service_url: str, limit_gb) -> dict:
    return _call(service_url, "/video_rec", {"limit_gb": limit_gb}, method="PUT", timeout=30.0)


def video_rec_camera(service_url: str, cam: str, patch: dict) -> dict:
    """Включить/выключить запись камеры, поток, область. Сервис ждёт, пока ffmpeg закроет файл, — поэтому таймаут большой."""
    return _call(service_url, "/cameras/%s/video_rec" % urllib.parse.quote(cam, safe=""), patch, timeout=30.0)


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


def rod_mask(service_url: str, cam: str, zone: str, red: str = "", blue: str = "", width: int = 640, timeout: float = 8.0, raw: bool = False, red_more=None, blue_more=None, red_not=None, blue_not=None,
             tip=None, fill=None, empty=None, tol=None):
    """JPEG «что видит детектор» палки или уровнемера с пробными порогами цвета или None (кадра нет / сервис старый / зоны нет)."""
    args = {"zone": zone, "red": red, "blue": blue, "w": int(width), "raw": 1 if raw else 0}
    if red_more is not None:                                  # дополнительные цвета конца палки: «H1,H2,S,V;H1,H2,S,V» (пусто — без них); нет параметра — как в настройках
        args["red_more"] = red_more
    if blue_more is not None:
        args["blue_more"] = blue_more
    if red_not is not None:                                   # исключённые цвета: «H1,H2,S1,S2,V1,V2;…», тот же порядок «нет параметра — как в настройках»
        args["red_not"] = red_not
    if blue_not is not None:
        args["blue_not"] = blue_not
    if tip is not None:                                       # цвета светоотражателей на концах палки — тот же вид «H1,H2,S1,S2,V1,V2;…»
        args["tip"] = tip
    for name, value in (("fill", fill), ("empty", empty), ("tol", tol)):   # уровнемер-столбик: образцы «L,a,b;…» и допуск; нет параметра — как в настройках
        if value is not None:
            args[name] = value
    q = urllib.parse.urlencode(args)
    try:
        with _opener.open(_base(service_url) + "/cameras/%s/rod_mask?%s" % (urllib.parse.quote(cam, safe=""), q), timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def rod_pick(service_url: str, cam: str, zone: str, u: float, v: float) -> dict:
    """Пипетка: цвет палки в точке картинки rod_mask (u, v — доли 0..1) → {h, s, v, x, y, n} или {"error": …}."""
    q = urllib.parse.urlencode({"zone": zone, "u": float(u), "v": float(v)})
    return _call(service_url, "/cameras/%s/rod_pick?%s" % (urllib.parse.quote(cam, safe=""), q), timeout=8.0)


def snapshot(service_url: str, cam: str, width: int = 0, timeout: float = 5.0):
    """(jpeg-байты, ширина оригинала, высота оригинала) или None, если кадра нет/сервис молчит."""
    url = _base(service_url) + "/cameras/%s/snapshot?w=%d" % (urllib.parse.quote(cam, safe=""), int(width))
    try:
        with _opener.open(url, timeout=timeout) as r:
            return r.read(), int(r.headers.get("X-Frame-W") or 0), int(r.headers.get("X-Frame-H") or 0)
    except Exception:
        return None
