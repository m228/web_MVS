"""Веб-слой (app) через ASGI без сети и без lifespan: ключевые GET-эндпоинты отвечают,
несуществующий путь даёт 404, схема RTSP-URL валидируется (SSRF-guard)."""
import asyncio

import pytest


async def _asgi_get(app, path, query=""):
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": query.encode(),
             "headers": [(b"host", b"test")], "client": ("127.0.0.1", 1), "server": ("test", 80), "root_path": ""}
    out = {"status": None, "body": bytearray()}
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            await asyncio.sleep(3600)
        sent["done"] = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")

    await asyncio.wait_for(app(scope, receive, send), timeout=30)
    return out["status"], bytes(out["body"])


def get(path, query=""):
    import app as web
    return asyncio.run(_asgi_get(web.app, path, query))


@pytest.mark.parametrize("path", [
    "/api/version", "/api/debug/logs", "/api/micro/enabled",
    "/api/cv/settings", "/api/rtsp/saved", "/api/autostart/status",
])
def test_endpoints_200(data_dir, path):
    status, _ = get(path)
    assert status == 200, "%s → HTTP %s" % (path, status)


def test_pages_render(data_dir):
    for p in ("/", "/camera", "/rtsp", "/multi"):
        status, body = get(p)
        assert status == 200 and b"<" in body


def test_unknown_route_404(data_dir):
    status, _ = get("/api/nope/not-here")
    assert status == 404


def test_rtsp_save_rejects_non_rtsp_scheme(data_dir):
    # SSRF-guard: file:// / http:// в базу не попадают (иначе cv2.VideoCapture открыл бы их)
    import json
    status, body = get("/api/rtsp/save", "url=file:///etc/passwd")
    assert status == 200
    assert json.loads(body).get("error") == "invalid_rtsp_scheme"


def test_version_payload(data_dir):
    import json
    status, body = get("/api/version")
    assert status == 200 and "app" in json.loads(body)
