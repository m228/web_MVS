"""monitor_app — HTTP-слой приёмника мониторинга (FastAPI).

  POST /ingest                  — пульс от завода (sysmon.py): Authorization: Bearer <токен завода>. 200 — принято (события не потеряются
                                  и не задвоятся), 401 — токен не тот, 400 — формат не тот, 413 — слишком большой.
  GET  /                        — панель (один общий пароль, HTTP Basic: имя любое, пароль — из data\\server.json).
  GET  /api/plants              — карточки заводов: статус, снимок, открытые тревоги, последние события.
  POST /api/plants              — {"name"}: завести завод, в ответе токен (показывается один раз).
  GET  /api/plants/{id}/events  — журнал завода (?limit=100&before=<id>&errors=1 — только ошибки).
  POST /api/plants/{id}/alerts/ack — {"code"?}: сбросить тревогу (без code — все) в панели; в журнале остаётся «Сброшено: …».
  POST /api/plants/{id}/token   — выдать новый токен (старый перестаёт работать).
  POST /api/plants/{id}/rename  — {"name"}: переименовать завод (токен, журнал и датчики остаются).
  POST /api/plants/{id}/view    — {"hidden": ["pc", "camera", …]}: какие блоки карточки скрыть («Вид» в панели; хранится в приёмнике).
  DELETE /api/plants/{id}       — удалить завод вместе с журналом.
  POST /enroll                  — заявка нового клиента с ключом регистрации (без пароля панели): {key, machine, name, version, secret}.
                                  Повторяется раз в минуту, пока Макс не решит; «принято» приходит с токеном один раз. 403 — ключ не тот.
  GET  /api/enroll              — заявки, ждущие решения; POST /api/enroll/{id}/approve ({"name"}) и /reject — принять / отклонить.
  GET  /api/telegram            — состояние уведомлений в Telegram (без токена и chat id); POST /api/telegram/test — тестовое сообщение.
  GET  /health                  — жив ли (без пароля, без данных).
  GET  /manifest.webmanifest, /icon-192.png, /icon-512.png, /favicon.ico — иконка и описание приложения (без пароля, секретов нет):
                                  по ним браузер ставит панель отдельным окном («Установить как приложение»).

Изменяющие запросы панели требуют заголовок X-Panel: 1 и не принимаются со стороннего сайта (Sec-Fetch-Site): браузер сам подставляет
пароль Basic в любой запрос на этот адрес, и без такой защиты чужая страница могла бы удалить завод.
Пароль перебирать нельзя: 8 неверных за 5 минут с одного адреса — дальше 429.
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import Callable, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

import monitor_store

log = logging.getLogger("monitor")

INGEST_MAX_BYTES = 1_000_000
AUTH_FAILS_MAX = 8
AUTH_FAILS_WINDOW_S = 300
WATCH_EVERY_S = 30
PURGE_EVERY_S = 6 * 3600
APP_NAME = "web_MVS панель"
APP_SHORT_NAME = "MVS панель"


def manifest() -> dict:
    """Описание приложения для браузера: свой значок и окно без адресной строки."""
    return {"name": APP_NAME, "short_name": APP_SHORT_NAME, "description": "Мониторинг заводских ПК", "lang": "ru",
            "start_url": "/", "scope": "/", "display": "standalone", "background_color": "#0f1623", "theme_color": "#2563eb",
            "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                      {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"}]}


def create_app(store: monitor_store.Store, password: str, version: str, panel_html: str, watchdog: bool = True,
               clock: Callable[[], float] = time.time, assets: Optional[dict] = None, notifier=None, bot=None, releases=None,
               enroll_key: str = "") -> FastAPI:
    if not password:
        raise ValueError("пароль панели не задан")
    fails: dict = {}
    enroll_fails: dict = {}
    pw = password.encode("utf-8")
    ekey = (enroll_key or "").encode("utf-8")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = threading.Event()

        def loop():
            last_purge = 0.0
            while not stop.wait(WATCH_EVERY_S):
                try:
                    store.touch_alive()                  # отметка «приёмник жив»: по ней при запуске видно, сколько он не работал
                    for ev in store.check_offline():
                        log.warning("%s: %s", ev["plant"], ev["text"])
                        if notifier is not None:
                            notifier.kick()
                    if clock() - last_purge > PURGE_EVERY_S:
                        store.purge()
                        last_purge = clock()
                except Exception:
                    log.exception("сторож «нет пульса»")

        t = threading.Thread(target=loop, name="monitor-watch", daemon=True)
        if watchdog:
            t.start()
        if notifier is not None and watchdog:
            notifier.start()
        if bot is not None and watchdog:
            bot.start()
        if releases is not None and watchdog:
            releases.start()
        yield
        stop.set()
        if releases is not None:
            releases.stop()
        if notifier is not None:
            notifier.stop()
        if bot is not None:
            bot.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("Cache-Control", "no-store")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    def client_ip(request: Request) -> str:
        peer = request.client.host if request.client else ""
        if peer in ("127.0.0.1", "::1"):                 # за туннелем/прокси настоящий адрес — в заголовке Cloudflare
            return (request.headers.get("cf-connecting-ip") or peer)[:64]
        return peer

    def panel_denied(request: Request):
        """None — пароль верный; иначе готовый ответ (401 с запросом пароля или 429)."""
        ip = client_ip(request)
        now = clock()
        recent = [t for t in fails.get(ip, []) if now - t < AUTH_FAILS_WINDOW_S]
        if len(recent) >= AUTH_FAILS_MAX:
            fails[ip] = recent
            return JSONResponse({"error": "слишком много неверных паролей, подожди несколько минут"}, status_code=429,
                                headers={"Retry-After": "60"})
        head = request.headers.get("authorization", "")
        if head.lower().startswith("basic "):
            try:
                given = base64.b64decode(head[6:]).decode("utf-8").partition(":")[2].encode("utf-8")
            except Exception:
                given = b""
            if hmac.compare_digest(given, pw):
                return None
            recent.append(now)
        fails[ip] = recent
        if len(fails) > 1000:                            # словарь не должен расти бесконечно
            for k in [k for k, v in fails.items() if not v or now - v[-1] > AUTH_FAILS_WINDOW_S]:
                fails.pop(k, None)
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="monitor", charset="UTF-8"'})

    def write_denied(request: Request):
        bad = panel_denied(request)
        if bad is not None:
            return bad
        site = request.headers.get("sec-fetch-site")
        if request.headers.get("x-panel") != "1" or (site and site not in ("same-origin", "none")):
            return JSONResponse({"error": "запрос не из панели"}, status_code=403)
        return None

    def static_route(path: str, body: bytes, ctype: str):
        app.add_api_route(path, lambda: Response(body, media_type=ctype, headers={"Cache-Control": "public, max-age=3600"}),
                          methods=["GET"], include_in_schema=False)

    for path, (body, ctype) in (assets or {}).items():
        static_route(path, body, ctype)
    manifest_body = json.dumps(manifest(), ensure_ascii=False).encode("utf-8")
    static_route("/manifest.webmanifest", manifest_body, "application/manifest+json")

    @app.get("/health")
    def health():
        return {"ok": True, "service": "monitor", "version": version}

    @app.post("/ingest")
    async def ingest(request: Request):
        auth = request.headers.get("authorization", "")
        plant = store.plant_by_token(auth[7:].strip()) if auth.lower().startswith("bearer ") else None
        if plant is None:
            return JSONResponse({"ok": False, "error": "токен не принят"}, status_code=401)
        try:
            if int(request.headers.get("content-length") or 0) > INGEST_MAX_BYTES:
                raise OverflowError
            body = await request.body()
            if len(body) > INGEST_MAX_BYTES:
                raise OverflowError
        except OverflowError:
            return JSONResponse({"ok": False, "error": "пульс слишком большой"}, status_code=413)
        except ValueError:
            return JSONResponse({"ok": False, "error": "неверный Content-Length"}, status_code=400)
        try:
            payload = json.loads(body.decode("utf-8"))
            res = await run_in_threadpool(store.ingest, plant["id"], payload)
        except (ValueError, UnicodeDecodeError) as e:    # json.JSONDecodeError — тоже ValueError
            return JSONResponse({"ok": False, "error": "пульс не разобран: %s" % e}, status_code=400)
        except LookupError:
            return JSONResponse({"ok": False, "error": "токен не принят"}, status_code=401)
        if notifier is not None:
            notifier.kick()
        return {"ok": True, **res}

    @app.post("/enroll")
    async def enroll(request: Request):
        """Заявка нового клиента (завод сам просит токен). Пускаем только с ключом регистрации; решение — за Максом (панель, Telegram)."""
        if not ekey:
            return JSONResponse({"ok": False, "error": "приём новых клиентов выключен"}, status_code=503)
        ip = client_ip(request)
        now = clock()
        recent = [t for t in enroll_fails.get(ip, []) if now - t < AUTH_FAILS_WINDOW_S]
        enroll_fails[ip] = recent
        if len(recent) >= AUTH_FAILS_MAX:
            return JSONResponse({"ok": False, "error": "слишком много неверных ключей"}, status_code=429, headers={"Retry-After": "60"})
        try:
            body = await request.body()
            if len(body) > 4096:
                raise ValueError("слишком большая заявка")
            data = json.loads(body.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("заявка не разобрана")
        except (ValueError, UnicodeDecodeError) as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        if not hmac.compare_digest(str(data.get("key") or "").encode("utf-8"), ekey):
            recent.append(now)
            if len(enroll_fails) > 1000:
                for k in [k for k, v in enroll_fails.items() if not v or now - v[-1] > AUTH_FAILS_WINDOW_S]:
                    enroll_fails.pop(k, None)
            return JSONResponse({"ok": False, "error": "ключ регистрации не принят"}, status_code=403)
        try:
            res = await run_in_threadpool(store.enroll_request, data.get("machine"), data.get("name"), data.get("version"), ip, data.get("secret"))
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except OverflowError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=429, headers={"Retry-After": "300"})
        if res.pop("new", False) and bot is not None:                  # новая заявка: Макс увидит её и в Telegram (с кнопками)
            await run_in_threadpool(bot.announce_enroll, res["id"], res["name"], str(data.get("version") or "")[:20], ip)
        return {"ok": True, **res}

    @app.get("/api/enroll")
    def enroll_list(request: Request):
        bad = panel_denied(request)
        if bad is not None:
            return bad
        return {"requests": store.enroll_pending()}

    @app.post("/api/enroll/{req_id}/approve")
    async def enroll_approve(req_id: int, request: Request):
        bad = write_denied(request)
        if bad is not None:
            return bad
        try:
            body = await request.json()
        except ValueError:
            body = {}
        try:
            return store.enroll_approve(req_id, str(body.get("name") or "") if isinstance(body, dict) else "")
        except LookupError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.post("/api/enroll/{req_id}/reject")
    def enroll_reject(req_id: int, request: Request):
        bad = write_denied(request)
        if bad is not None:
            return bad
        return {"rejected": store.enroll_reject(req_id)}

    @app.get("/api/telegram")
    def telegram_state(request: Request):
        bad = panel_denied(request)
        if bad is not None:
            return bad
        return notifier.public_state() if notifier is not None else {"configured": False}

    @app.post("/api/telegram/test")
    async def telegram_test(request: Request):
        bad = write_denied(request)
        if bad is not None:
            return bad
        if notifier is None:
            return JSONResponse({"ok": False, "error": "Telegram не настроен: запусти telegram_setup.bat"}, status_code=400)
        ok, err = await run_in_threadpool(notifier.send_test)
        return {"ok": ok, "error": err}

    @app.get("/", response_class=HTMLResponse)
    def panel(request: Request):
        bad = panel_denied(request)
        return bad if bad is not None else HTMLResponse(panel_html)

    @app.get("/api/plants")
    def plants(request: Request):
        bad = panel_denied(request)
        if bad is not None:
            return bad
        return {"plants": store.plants_view(), "server_t": clock(), "version": version, "latest_version": releases.latest if releases else None}

    @app.post("/api/plants")
    async def plants_add(request: Request):
        bad = write_denied(request)
        if bad is not None:
            return bad
        try:
            body = await request.json()
            pid, token = store.create_plant(str(body.get("name", "")) if isinstance(body, dict) else "")
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"id": pid, "token": token}

    @app.get("/api/plants/{plant_id}/events")
    def plant_events(plant_id: int, request: Request, limit: int = 100, before: int = 0, errors: int = 0):
        bad = panel_denied(request)
        if bad is not None:
            return bad
        return {"events": store.events(plant_id, limit, before or None, bool(errors))}

    @app.get("/api/plants/{plant_id}/positions")
    def plant_positions(plant_id: int, request: Request, limit: int = 60):
        bad = panel_denied(request)
        if bad is not None:
            return bad
        return {"positions": store.positions(plant_id, limit)}

    @app.post("/api/plants/{plant_id}/token")
    def plant_token(plant_id: int, request: Request):
        bad = write_denied(request)
        if bad is not None:
            return bad
        try:
            return {"token": store.rotate_token(plant_id)}
        except LookupError:
            return JSONResponse({"error": "завода нет"}, status_code=404)

    @app.post("/api/plants/{plant_id}/rename")
    async def plant_rename(plant_id: int, request: Request):
        bad = write_denied(request)
        if bad is not None:
            return bad
        try:
            body = await request.json()
            return {"name": store.rename_plant(plant_id, str(body.get("name", "")) if isinstance(body, dict) else "")}
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except LookupError:
            return JSONResponse({"error": "завода нет"}, status_code=404)

    @app.post("/api/plants/{plant_id}/alerts/ack")
    async def plant_alerts_ack(plant_id: int, request: Request):
        bad = write_denied(request)
        if bad is not None:
            return bad
        try:
            body = await request.json()
        except ValueError:
            body = {}
        code = body.get("code") if isinstance(body, dict) else None      # без кода — сбросить все открытые тревоги завода
        return {"acked": store.ack_alerts(plant_id, str(code)[:40] if code else None)}

    @app.post("/api/plants/{plant_id}/view")
    async def plant_view(plant_id: int, request: Request):
        bad = write_denied(request)
        if bad is not None:
            return bad
        try:
            body = await request.json()
            return {"hidden": store.set_hidden_blocks(plant_id, body.get("hidden") if isinstance(body, dict) else None)}
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except LookupError:
            return JSONResponse({"error": "завода нет"}, status_code=404)

    @app.delete("/api/plants/{plant_id}")
    def plant_delete(plant_id: int, request: Request):
        bad = write_denied(request)
        if bad is not None:
            return bad
        return {"deleted": store.delete_plant(plant_id)}

    return app
