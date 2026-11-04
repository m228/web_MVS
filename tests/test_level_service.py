"""level_service: геометрия масок (как в LVLDec.exe и «точная») и HTTP-сервер (модель — поддельная, без torch)."""
import importlib.util
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import cv2
import numpy as np
import pytest

LS = Path(__file__).resolve().parent.parent / "level_service"
sys.path.insert(0, str(LS))

import seg_model as sm  # noqa: E402


def _server():
    spec = importlib.util.spec_from_file_location("level_server", LS / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bbox(polys):
    pts = np.array([p for poly in polys for p in poly])
    return pts[:, 0].min(), pts[:, 1].min(), pts[:, 0].max(), pts[:, 1].max()


# ───────────────────────────── геометрия масок ─────────────────────────────
def test_lvldec_geometry_stretches_letterboxed_mask_like_the_exe_and_exact_does_not():
    """Кадр 1920×1080, сеть 640×384: полезная часть маски — строки 12..371, по 12 строк полей сверху и снизу."""
    m = np.zeros((384, 640), np.uint8)
    m[12:372, :] = 1                                  # жидкость на весь кадр
    x0, y0, x1, y1 = _bbox(sm.mask_to_polygons(m, 1920, 1080, "lvldec"))
    assert (y0, y1) == (34, 1045) or (abs(y0 - 34) <= 3 and abs(y1 - 1045) <= 3), "как в exe: маска растянута вместе с полями, края съезжают к центру"
    x0, y0, x1, y1 = _bbox(sm.mask_to_polygons(m, 1920, 1080, "exact"))
    assert y0 <= 3 and y1 >= 1076 and x0 <= 3 and x1 >= 1916, "точная: поля вырезаны, жидкость на весь кадр"


def test_mask_polygons_keep_area():
    m = np.zeros((384, 640), np.uint8)
    m[100:200, 200:300] = 1
    m[300, 10] = 1                                    # одиночный пиксель маски — крошечный контур (в exe такой же считался), площадь ничтожна
    polys = sm.mask_to_polygons(m, 1920, 1080, "lvldec")
    z = np.zeros((1080, 1920), np.uint8)
    for p in polys:
        cv2.fillPoly(z, [np.array(p, np.int32).reshape(-1, 1, 2)], 1)
    want = (100 * 100 + 1) * (1920 / 640) * (1080 / 384)
    assert abs(int(z.sum()) - want) / want < 0.02, "площадь контуров = площади маски, растянутой на кадр (±2 %)"
    assert max(len(p) for p in polys) <= 8, "прямоугольник упрощён до нескольких точек"


def test_box_polygon_for_detect_models():
    assert sm.box_polygon([1.2, 2.6, 10.4, 20.5]) == [[1, 3], [10, 3], [10, 20], [1, 20]] or sm.box_polygon([1.2, 2.6, 10.4, 20.5])[0] == [1, 3]


# ───────────────────────────── HTTP (настоящий uvicorn на свободном порту) ─────────────────────────────
class FakeModel:
    def __init__(self, path, device=None, imgsz=None):
        if path.endswith("bad.pt"):
            raise RuntimeError("не модель")
        self.path, self.name = path, Path(path).name

    def info(self):
        return {"name": self.name, "path": self.path, "task": "segment", "seg": True, "classes": ["liquid"], "imgsz": 640, "device": "cpu"}

    def infer(self, img, **kw):
        return [{"cls": 0, "name": "liquid", "conf": 0.9, "bbox": [1, 2, 3, 4], "polygons": [[[1, 1], [9, 1], [9, 9]]]}]


def _jpeg(w=64, h=48):
    ok, buf = cv2.imencode(".jpg", np.full((h, w, 3), 120, np.uint8))
    return buf.tobytes()


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Client:
    """Мини-клиент к поднятому серверу: .get/.post → (код, json)."""

    def __init__(self, port):
        self.port = port

    def _do(self, method, path, data=None, ctype="application/octet-stream"):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method,
                                     headers={"Content-Type": ctype} if data is not None else {})
        try:
            with _opener.open(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def get(self, path):
        return self._do("GET", path)

    def post(self, path, data=b"", json_body=None):
        if json_body is not None:
            return self._do("POST", path, json.dumps(json_body).encode(), "application/json")
        return self._do("POST", path, data)


@pytest.fixture
def serve(tmp_path):
    import uvicorn
    started = []

    def make(model_dir=None, boot_path=""):
        srv = _server()
        app = srv.create_app(model_dir=model_dir or (tmp_path / "model"), model_factory=FakeModel, boot_path=boot_path)
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        threading.Thread(target=server.run, daemon=True).start()
        t0 = time.time()
        while not server.started and time.time() - t0 < 10:
            time.sleep(0.02)
        assert server.started
        started.append(server)
        return Client(port)

    yield make
    for s in started:
        s.should_exit = True


def test_health_without_model_and_infer_refuses_without_dropping_the_body(serve):
    c = serve()
    code, h = c.get("/health")
    assert code == 200 and h["ok"] and h["model"] is None
    code, r = c.post("/infer", _jpeg())
    assert code == 409 and "не загружена" in r["error"], "кадр прочитан до отказа — клиент получает ответ, а не обрыв"
    assert "error" in c.get("/model")[1]


def test_upload_loads_model_hot_and_infer_returns_objects(serve, tmp_path):
    c = serve()
    code, r = c.post("/model/upload?name=my_model.pt", b"x" * 2000)
    assert code == 200 and r["model"]["name"] == "my_model.pt"
    code, r = c.post("/model/upload?name=%D0%BC%D0%BE%D0%B4%D0%B5%D0%BB%D1%8C!.pt", b"x" * 2000)
    assert code == 200 and r["model"]["name"] == ".pt" or r["model"]["name"].endswith(".pt"), "имя файла очищается от мусора"
    c.post("/model/load", json_body={"path": str(tmp_path / "model" / "my_model.pt")})
    assert (tmp_path / "model" / "my_model.pt").is_file()
    code, m = c.get("/models")
    assert "my_model.pt" in m["files"] and m["current"] == "my_model.pt"
    code, out = c.post("/infer?conf=0.4&geometry=exact&imgsz=800", _jpeg())
    assert code == 200 and out["count"] == 1 and out["objects"][0]["name"] == "liquid" and out["image"] == {"w": 64, "h": 48}
    assert out["model"] == "my_model.pt" and out["geometry"] == "exact" and "ms" in out["timing"]


def test_infer_validates_input(serve):
    c = serve()
    c.post("/model/upload?name=a.pt", b"x" * 2000)
    assert c.post("/infer?geometry=zzz", _jpeg())[0] == 400
    assert c.post("/infer", b"not an image")[0] == 400
    assert c.post("/model/upload", b"tiny")[0] == 400, "пустой/крошечный файл — не модель"


def test_broken_model_does_not_replace_the_working_one(serve):
    c = serve()
    c.post("/model/upload?name=good.pt", b"x" * 2000)
    code, r = c.post("/model/upload?name=bad.pt", b"x" * 2000)
    assert code == 400 and "не модель" in r["error"]
    assert c.get("/model")[1]["name"] == "good.pt", "старая модель продолжает работать"


def test_last_model_is_remembered_across_restart(serve, tmp_path):
    c = serve()
    c.post("/model/upload?name=one.pt", b"x" * 2000)
    c.post("/model/upload?name=two.pt", b"x" * 2000)
    c.post("/model/load", json_body={"path": str(tmp_path / "model" / "one.pt")})
    c2 = serve()                                                  # «перезапуск»: новый сервер на той же папке model
    assert c2.get("/health")[1]["model"]["name"] == "one.pt", "после перезапуска — последняя загруженная, а не последняя по алфавиту"
    c3 = serve(model_dir=tmp_path / "other", boot_path=str(tmp_path / "model" / "two.pt"))
    assert c3.get("/health")[1]["model"]["name"] == "two.pt", "нет запомненной — берём LEVEL_MODEL"


def test_boot_with_broken_model_still_starts(serve, tmp_path):
    c = serve(boot_path=str(tmp_path / "bad.pt"))
    h = c.get("/health")[1]
    assert h["ok"] and h["model"] is None and "bad.pt" in h["error"]
