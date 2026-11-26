"""Картинка камеры для SCADA и браузера (перенос [webview] из LVLDec): подготовка JPEG, адреса, настройки камеры, порт, разметка, воркер.

Без настоящей камеры и сервиса уровня: кадры и ответ модели подменены. Сеть — только 127.0.0.1 на свободных портах.
Ожидаемое поведение сверено с байт-кодом LVLDec.exe (модули httpstream и debug_view).
"""
import http.client
import json
import socket
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

MS = Path(__file__).resolve().parent.parent / "motion_service"
sys.path.insert(0, str(MS))

import motion_camera as mcam  # noqa: E402
import motion_engine as me  # noqa: E402
import motion_modbus as mm  # noqa: E402
import motion_webview as mw  # noqa: E402
from motion_camera import CameraWorker  # noqa: E402

W, H = 320, 240


@pytest.fixture(autouse=True)
def _no_open_gap(monkeypatch):
    monkeypatch.setattr(mcam, "OPEN_GAP_S", 0.0)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def get(port: int, path: str):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        c.request("GET", path)
        r = c.getresponse()
        return r.status, dict((k.lower(), v) for k, v in r.getheaders()), r.read()
    finally:
        c.close()


def decode(data: bytes) -> np.ndarray:
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)


def gradient(w=W, h=H) -> np.ndarray:
    """Кадр, где каждый пиксель различим: по x растёт синий, по y — зелёный."""
    f = np.zeros((h, w, 3), np.uint8)
    f[:, :, 0] = np.linspace(0, 255, w, dtype=np.uint8)[None, :]
    f[:, :, 1] = np.linspace(0, 255, h, dtype=np.uint8)[:, None]
    return f


def cfg(**kw) -> dict:
    return me._norm_webview(dict(enabled=True, **kw), "cam")


# ───────────────────────────── подготовка картинки (FrameStore._prepare) ─────────────────────────────
def test_prepare_without_crop_and_size_keeps_frame_size_and_stamps_time():
    st = mw.FrameStore(90)
    jpeg = st._prepare(gradient(), time.time())
    img = decode(jpeg)
    assert img.shape == (H, W, 3)
    # метка времени снизу слева: белые пиксели есть там, а сверху слева кадр не тронут
    assert (img[H - 30:H - 5, 10:200].min(axis=2) > 200).any()
    assert abs(int(img[2, 2, 0]) - int(gradient()[2, 2, 0])) < 12


def test_prepare_crop_then_resize_gives_exact_output_size():
    st = mw.FrameStore(85, crop=(40, 20, 240, 220), output_size=(100, 80))
    img = decode(st._prepare(gradient(), 1.0))
    assert img.shape == (80, 100, 3)


def test_prepare_resize_without_crop_resizes_whole_frame():
    img = decode(mw.FrameStore(85, output_size=(600, 500))._prepare(gradient(), 1.0))
    assert img.shape == (500, 600, 3)


def test_prepare_crop_is_clamped_to_frame_and_empty_crop_means_whole_frame():
    assert decode(mw.FrameStore(85, crop=(-50, -50, 100, 100))._prepare(gradient(), 1.0)).shape == (100, 100, 3)
    assert decode(mw.FrameStore(85, crop=(200, 100, 9999, 9999))._prepare(gradient(), 1.0)).shape == (H - 100, W - 200, 3)
    assert decode(mw.FrameStore(85, crop=(500, 500, 900, 900))._prepare(gradient(), 1.0)).shape == (H, W, 3), "за пределами кадра — кадр как есть"


def test_prepare_picks_interpolation_by_area(monkeypatch):
    used = []
    real = cv2.resize
    monkeypatch.setattr(cv2, "resize", lambda img, size, interpolation=None: used.append(interpolation) or real(img, size, interpolation=interpolation))
    mw.FrameStore(85, output_size=(100, 100))._prepare(gradient(), 1.0)
    mw.FrameStore(85, output_size=(600, 500))._prepare(gradient(), 1.0)
    assert used == [cv2.INTER_AREA, cv2.INTER_LINEAR], "уменьшаем по площади, увеличиваем линейно"


def test_prepare_does_not_touch_the_original_frame():
    f = gradient()
    keep = f.copy()
    mw.FrameStore(85, crop=(0, 0, 100, 100), output_size=(50, 50))._prepare(f, 1.0)
    assert np.array_equal(f, keep)


def test_jpeg_quality_changes_size():
    f = np.random.default_rng(1).integers(0, 255, (H, W, 3)).astype(np.uint8)
    assert len(mw.FrameStore(95)._prepare(f, 1.0)) > len(mw.FrameStore(20)._prepare(f, 1.0))


# ───────────────────────────── настройки камеры ─────────────────────────────
def test_webview_defaults_match_lvldec_config():
    assert me._norm_webview({"enabled": True}, "c") == {
        "enabled": True, "host": "0.0.0.0", "port": 5000, "update_sec": 5.0, "jpeg_quality": 85, "annotate": False,
        "crop_x1": 0, "crop_y1": 0, "crop_x2": 0, "crop_y2": 0, "output_w": 0, "output_h": 0}


def test_webview_accepts_production_config():
    c = cfg(host="0.0.0.0", port=5000, update_sec=3, jpeg_quality=85, annotate=True,
            crop_x1=540, crop_y1=30, crop_x2=1735, crop_y2=1040, output_w=600, output_h=500)
    assert (c["port"], c["update_sec"], c["annotate"], c["crop_x2"], c["output_h"]) == (5000, 3.0, True, 1735, 500)


@pytest.mark.parametrize("raw,part", [
    ({"port": 0}, "port"), ({"port": 70000}, "port"), ({"update_sec": 0.1}, "update_sec"), ({"jpeg_quality": 0}, "jpeg_quality"),
    ({"jpeg_quality": 101}, "jpeg_quality"), ({"host": " "}, "host"), ({"crop_x1": -1}, "crop_x1"), ({"port": "abc"}, "port"),
    ({"crop_x1": 100, "crop_x2": 50, "crop_y2": 10}, "обрезка"), ({"crop_x2": 50}, "обрезка"),
    ({"output_w": 600}, "размер"), ({"output_h": 500}, "размер"),
])
def test_webview_rejects_bad_input(raw, part):
    with pytest.raises(ValueError, match=part):
        me._norm_webview(raw, "cam")


def test_camera_without_webview_stays_without_it_and_ports_must_differ():
    base = {"id": "a", "url": "x"}
    assert me.normalize_camera(base)["webview"] is None
    ok = me.normalize_config({"cameras": [dict(base, webview={"enabled": True, "port": 5000}), dict(base, id="b", webview={"enabled": True, "port": 5001})]})
    assert ok["cameras"][1]["webview"]["port"] == 5001
    with pytest.raises(ValueError, match="один и тот же порт"):
        me.normalize_config({"cameras": [dict(base, webview={"enabled": True, "port": 5000}), dict(base, id="b", webview={"enabled": True, "port": 5000})]})
    me.normalize_config({"cameras": [dict(base, webview={"enabled": False, "port": 5000}), dict(base, id="b", webview={"enabled": True, "port": 5000})]})


# ───────────────────────────── HTTP-адреса ─────────────────────────────
@pytest.fixture
def server():
    store = mw.FrameStore(85)
    made = []

    def make(annotate=False, refresh=3.0):
        srv, t = mw.start_server("127.0.0.1", 0, store, refresh, annotate)
        made.append((srv, t))
        return srv.server_address[1]

    yield store, make
    for srv, t in made:
        mw.stop_server(srv, t)


def test_frame_is_503_until_first_picture_then_jpeg_with_no_cache(server):
    store, make = server
    port = make()
    code, _, body = get(port, "/frame.jpg")
    assert code == 503 and body.decode("utf-8") == "нет кадра — камера ещё не готова"
    store.set_raw(gradient(), time.time())
    code, h, body = get(port, "/frame.jpg?ts=123")
    assert code == 200 and h["content-type"] == "image/jpeg" and h["cache-control"] == "no-store, no-cache, must-revalidate"
    assert int(h["content-length"]) == len(body) and decode(body).shape == (H, W, 3)


def test_annotated_routes_are_503_until_annotation_exists(server):
    store, make = server
    port = make(annotate=True)
    for p in ("/frame_annotated.jpg", "/scada.jpg"):
        code, _, body = get(port, p)
        assert code == 503 and body.decode("utf-8") == "аннотированный кадр ещё не готов"
    store.set_annotated(gradient(), time.time())
    for p in ("/frame_annotated.jpg", "/scada.jpg"):
        assert get(port, p)[0] == 200


def test_scada_jpg_serves_plain_picture_when_annotation_is_off(server):
    """SCADA стоит на /scada.jpg (Navigate2 в её скрипте): без разметки там должна быть обычная картинка, а не 503, как в LVLDec без annotate."""
    store, make = server
    port = make(annotate=False)
    code, _, body = get(port, "/scada.jpg")
    assert code == 503 and body.decode("utf-8") == "нет кадра — камера ещё не готова"
    store.set_raw(gradient(), time.time())
    code, h, scada = get(port, "/scada.jpg?t=1")
    assert code == 200 and h["content-type"] == "image/jpeg" and scada == get(port, "/frame.jpg")[2], "тот же кадр, что и /frame.jpg"
    assert get(port, "/frame_annotated.jpg")[0] == 503, "размеченного кадра без annotate нет, как в LVLDec"


def test_scada_jpg_serves_annotated_picture_when_annotation_is_on(server):
    store, make = server
    port = make(annotate=True)
    store.set_raw(gradient(), time.time())
    assert get(port, "/scada.jpg")[0] == 503, "размеченный ещё не готов — чистый не подсовываем: SCADA покажет старую картинку, а не подмену"
    other = gradient()[::-1].copy()
    store.set_annotated(other, time.time())
    assert get(port, "/scada.jpg")[2] == get(port, "/frame_annotated.jpg")[2] != get(port, "/frame.jpg")[2]


def test_scada_page_points_at_raw_or_annotated_frame_and_refreshes(server):
    _, make = server
    raw_port, ann_port = make(annotate=False, refresh=3.0), make(annotate=True, refresh=2.6)
    for p in ("/scada.html", "/scada.htm"):
        code, h, body = get(raw_port, p)
        html = body.decode("utf-8")
        assert code == 200 and h["content-type"] == "text/html; charset=utf-8"
        assert "saved from url=(0014)about:internet" in html and "'/frame.jpg?t='" in html and '<meta http-equiv="refresh" content="3">' in html
        assert "3000" in html
    html = get(ann_port, "/scada.html")[2].decode("utf-8")
    assert "/scada.jpg?t=" in html and 'content="3"' in html and "2600" in html, "период 2,6 с → refresh округляется до 3, таймер 2600 мс"


def test_index_page_defaults_to_raw_or_annotated(server):
    _, make = server
    a, b = make(annotate=False, refresh=5.0), make(annotate=True, refresh=5.0)
    page_raw, page_ann = get(a, "/")[2].decode("utf-8"), get(b, "/index.html")[2].decode("utf-8")
    assert "обновление раз в 5 с" in page_raw and "|| 'raw'" in page_raw and "|| 'ann'" in page_ann and "setInterval(refresh, 5000)" in page_raw


def test_healthz_reports_ages_and_unknown_path_is_404(server):
    store, make = server
    port = make(annotate=False)
    d = json.loads(get(port, "/healthz")[2])
    assert d == {"ok": True, "raw_age_sec": -1.0, "annotated_age_sec": -1.0, "annotate": False}
    store.set_raw(gradient(), time.time() - 7)
    d = json.loads(get(port, "/healthz")[2])
    assert 6.5 < d["raw_age_sec"] < 9 and d["annotated_age_sec"] == -1
    code, _, body = get(port, "/nope")
    assert code == 404 and body == b"not found"


def test_many_readers_get_the_same_prepared_jpeg_without_reencoding(server, monkeypatch):
    store, make = server
    port = make()
    store.set_raw(gradient(), time.time())
    calls = []
    monkeypatch.setattr(cv2, "imencode", lambda *a, **k: calls.append(1) or (False, None))
    bodies = [get(port, "/frame.jpg")[2] for _ in range(5)]
    assert len(set(bodies)) == 1 and not calls, "запрос только читает готовый JPEG, кодирует раз в update_sec"


def test_a_busy_port_is_refused_instead_of_shared():
    busy = socket.socket()
    busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)       # как у старого LVLDec.exe: обычный HTTPServer ставит его сам
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    try:
        with pytest.raises(OSError):
            mw.start_server("127.0.0.1", busy.getsockname()[1], mw.FrameStore(85), 3, False)
    finally:
        busy.close()


def test_port_is_free_again_right_after_stop():
    port = free_port()
    srv, t = mw.start_server("127.0.0.1", port, mw.FrameStore(85), 3, False)
    assert get(port, "/healthz")[0] == 200
    mw.stop_server(srv, t)
    srv2, t2 = mw.start_server("127.0.0.1", port, mw.FrameStore(85), 3, False)      # без OSError
    mw.stop_server(srv2, t2)


def test_stop_right_after_start_does_not_hang():
    for _ in range(5):
        srv, t = mw.start_server("127.0.0.1", 0, mw.FrameStore(85), 3, False)
        t0 = time.time()
        mw.stop_server(srv, t)
        assert time.time() - t0 < 2.5


# ───────────────────────────── разметка ─────────────────────────────
def liquid_obj(poly, bbox=(20, 20, 120, 200), name="liquid", conf=0.93):
    return {"cls": 0, "name": name, "conf": conf, "bbox": list(bbox), "polygons": [poly]}


def test_overlay_tints_liquid_inside_polygon_only_and_writes_levels():
    f = np.full((H, W, 3), 100, np.uint8)
    poly = [[100, 100], [200, 100], [200, 200], [100, 200]]
    out = mw.draw_overlay(f, [liquid_obj(poly, bbox=(100, 100, 200, 200))], [W, H], "liquid", {"total": 51.25, "glass1": 60.0, "glass2": 42.5})
    inside, outside = out[150, 150].astype(int), out[220, 280].astype(int)
    assert abs(inside - np.array([0.45 * mw.COL_LIQUID[i] + 0.55 * 100 for i in range(3)])).max() < 2, "маска 45 % поверх кадра"
    assert (outside == 100).all(), "вне жидкости кадр не тронут"
    assert (out[10:60, 10:300] != 100).any(), "две строки уровня сверху"
    assert (f == 100).all(), "исходный кадр не меняется"


def test_overlay_without_level_or_objects_still_draws_status_text():
    f = np.full((H, W, 3), 100, np.uint8)
    out = mw.draw_overlay(f, [], None, "liquid", None)
    assert (out[10:60, 10:300] != 100).any() and (out[120:, :] == 100).all()


def test_overlay_hides_glass_class_and_scales_objects_to_frame():
    f = np.full((H, W, 3), 100, np.uint8)
    glass = liquid_obj([[100, 100], [200, 100], [200, 200], [100, 200]], name="glass")
    assert (mw.draw_overlay(f, [glass], [W, H], "liquid", None)[150, 150] == 100).all(), "стекло по умолчанию скрыто, как у LVLDec"
    half = liquid_obj([[50, 50], [100, 50], [100, 100], [50, 100]], bbox=(50, 50, 100, 100))
    out = mw.draw_overlay(f, [half], [W // 2, H // 2], "liquid", None)          # ответ модели был на кадре вдвое меньше
    assert (out[150, 150] != 100).any() and (out[220, 280] == 100).all()


# ───────────────────────────── WebView (поток обновления) ─────────────────────────────
def _view(port, source, **kw):
    c = cfg(host="127.0.0.1", port=port, update_sec=0.5, **kw)
    return mw.WebView("cam", c, source, threading.Event())


def test_step_stores_picture_and_annotation_only_with_a_fresh_frame():
    frame = gradient()
    state = {"src": None}
    v = _view(free_port(), lambda: state["src"], annotate=True)
    try:
        assert v.step(1000.0) is False and v.store.get_raw()[0] == b""
        state["src"] = (frame, {"objs": [liquid_obj([[10, 10], [60, 10], [60, 60], [10, 60]])], "wh": [W, H], "liquid_class": "liquid",
                                "lv": {"total": 50.0, "glass1": 100.0, "glass2": 0.0}})
        assert v.step(1001.0) is True
        raw, ts = v.store.get_raw()
        ann, ats = v.store.get_annotated()
        assert raw and ann and ts == ats == 1001.0 and raw != ann
        assert v.status()["listening"] is True
    finally:
        v.halt.set()
        v.close()


def test_step_without_annotate_keeps_annotated_picture_empty():
    v = _view(free_port(), lambda: (gradient(), None))
    try:
        v.step(5.0)
        assert v.store.get_raw()[0] and v.store.get_annotated()[0] == b""
    finally:
        v.halt.set()
        v.close()


def test_busy_port_waits_and_binds_when_freed():
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    port = busy.getsockname()[1]
    v = _view(port, lambda: (gradient(), None))
    try:
        v.step(100.0)
        assert v.status()["listening"] is False and v.status()["error"] == "", "первая неудача тихая: так выглядит пересоздание камеры при правке настроек"
        assert v.store.get_raw()[0], "кадр готовится и без порта — как только порт освободится, картинка уже есть"
        v.step(100.2)
        assert v.status()["error"] == "", "повтор не раньше паузы 0,5 с"
        v.step(100.6)
        st = v.status()
        assert st["listening"] is False and "не открылся" in st["error"], "вторая подряд — настоящая занятость, ошибка видна в состоянии"
        busy.close()
        v.step(101.0)
        assert v.status()["listening"] is False, "следующая пауза 2 с"
        v.step(103.0)
        assert v.status()["listening"] is True and v.status()["error"] == ""
        assert get(port, "/frame.jpg")[0] == 200
    finally:
        busy.close()
        v.halt.set()
        v.close()


def test_loop_updates_picture_and_stops_with_halt():
    v = _view(free_port(), lambda: (gradient(), None))
    v.start()
    try:
        t0 = time.time()
        while not v.store.get_raw()[0] and time.time() - t0 < 5:
            time.sleep(0.05)
        assert v.store.get_raw()[0]
        first = v.store.get_raw()[1]
        t0 = time.time()
        while v.store.get_raw()[1] == first and time.time() - t0 < 3:
            time.sleep(0.05)
        assert v.store.get_raw()[1] > first, "кадр обновляется раз в update_sec"
    finally:
        v.halt.set()
        v.join(5)
    assert not v.is_alive() and v.srv is None


# ───────────────────────────── камера целиком ─────────────────────────────
class FakeCap:
    def isOpened(self):
        return True

    def grab(self):
        time.sleep(0.02)
        return True

    def retrieve(self):
        rng = np.random.default_rng(int(time.time() * 1000) % 1000)
        return True, cv2.cvtColor(rng.integers(60, 120, (H, W)).astype(np.uint8), cv2.COLOR_GRAY2BGR)

    def release(self):
        pass

    def set(self, *_):
        return True

    def get(self, *_):
        return 25.0


def _wait(cond, timeout=8.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


def level_cam_with_web(port, **web):
    low = [[100, 120], [140, 120], [140, 230], [100, 230]]
    up = [[200, 10], [240, 10], [240, 110], [200, 110]]
    return me.normalize_camera({
        "id": "dec", "url": "fake", "hw_accel": "off", "proc_fps": 20, "freeze_timeout_s": 5, "frame_size": [W, H],
        "zones": [{"id": "low", "type": "level", "points": low}, {"id": "up", "type": "level", "points": up}],
        "groups": [{"id": "lvl", "type": "level", "zones": ["low", "up"], "warmup_s": 0, "alpha": 0, "glass1_min": 0, "glass2_min": 0,
                    "infer_interval_s": 0.2, "hold_s": 5}],
        "webview": dict(enabled=True, host="127.0.0.1", port=port, update_sec=0.5, **web)})


def answer():
    return {"image": {"w": W, "h": H}, "geometry": "lvldec", "count": 1, "timing": {"ms": 5}, "model": "fake.pt",
            "objects": [{"cls": 0, "name": "liquid", "conf": 0.9, "bbox": [90, 120, 150, 235],
                         "polygons": [[[90, 120], [150, 120], [150, 235], [90, 235]]]}]}


def test_worker_serves_raw_and_annotated_picture_and_frees_port_on_stop(tmp_path):
    port = free_port()
    cam = level_cam_with_web(port, annotate=True, crop_x1=10, crop_y1=10, crop_x2=310, crop_y2=230, output_w=150, output_h=100)
    w = CameraWorker(cam, me.normalize_modbus({}), mm.LinkRegistry(lambda h, p: None), tmp_path, lambda url, tr: FakeCap(), lambda *a: answer())
    w.start()
    try:
        assert _wait(lambda: (w.state()["webview"] or {}).get("age_s") is not None and w.state()["result"]["groups"][0]["valid"])
        st = w.state()["webview"]
        assert st["enabled"] and st["port"] == port and st["listening"] and st["error"] == ""
        assert _wait(lambda: get(port, "/frame_annotated.jpg")[0] == 200)
        raw, ann = decode(get(port, "/frame.jpg")[2]), decode(get(port, "/scada.jpg")[2])
        assert raw.shape == ann.shape == (100, 150, 3), "обрезка 300×220 и размер 150×100"
        assert np.abs(raw.astype(int) - ann.astype(int)).max() > 20, "на размеченной картинке видна маска жидкости"
        assert json.loads(get(port, "/healthz")[2])["annotate"] is True
    finally:
        w.stop()
    assert _wait(lambda: _can_bind(port), 5), "порт освобождается вместе с остановкой камеры"


def test_recreating_the_camera_on_the_same_port_shows_no_error(tmp_path):
    """Правка любых настроек пересоздаёт камеру: новая сразу просит порт, который прежняя ещё закрывает. Это не ошибка и в состоянии её нет."""
    port = free_port()

    def make():
        cam = level_cam_with_web(port)
        return CameraWorker(cam, me.normalize_modbus({}), mm.LinkRegistry(lambda h, p: None), tmp_path, lambda url, tr: FakeCap(), lambda *a: answer())

    a = make()
    a.start()
    assert _wait(lambda: (a.state()["webview"] or {}).get("listening"))
    a.stop()
    b = make()
    b.start()
    try:
        errors = []
        t0 = time.time()
        while time.time() - t0 < 6:
            st = b.state()["webview"]
            if st:
                errors.append(st["error"])
                if st["listening"]:
                    break
            time.sleep(0.02)
        assert (b.state()["webview"] or {}).get("listening"), "новая камера заняла порт сама"
        assert not any(errors), "за это время ошибка порта не показывалась: %s" % [e for e in errors if e][:1]
    finally:
        b.stop()


def _can_bind(port):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def test_worker_without_webview_has_no_listener_and_no_state(tmp_path):
    cam = me.normalize_camera({"id": "dec", "url": "fake", "hw_accel": "off", "proc_fps": 20, "frame_size": [W, H]})
    w = CameraWorker(cam, me.normalize_modbus({}), mm.LinkRegistry(lambda h, p: None), tmp_path, lambda url, tr: FakeCap(), lambda *a: answer())
    w.start()
    try:
        assert _wait(lambda: w.state()["connected"])
        assert w.state()["webview"] is None
    finally:
        w.stop()
