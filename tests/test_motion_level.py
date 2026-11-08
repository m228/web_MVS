"""Уровень жидкости (декантатор): ядро (перенос LVLDec), группа level в движке, поток модели в воркере.

Сервис уровня подменён поддельным ответом (контуры «жидкости» прямоугольниками) — без torch, камеры и ПЛК.
Ожидаемые числа получены руками по формулам из LVLDec.exe (core.LevelProcessor.update, байт-код разобран).
"""
import http.server
import json
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

MS = Path(__file__).resolve().parent.parent / "motion_service"
sys.path.insert(0, str(MS))

import motion_engine as me  # noqa: E402
import motion_level as ml  # noqa: E402
import motion_modbus as mm  # noqa: E402
import motion_camera as mcam  # noqa: E402
from motion_camera import CameraWorker  # noqa: E402

W, H = 320, 240
LOW = [[100, 120], [140, 120], [140, 230], [100, 230]]       # нижнее стекло: 41 × 111 px
UP = [[200, 10], [240, 10], [240, 110], [200, 110]]          # верхнее стекло: 41 × 101 px


@pytest.fixture(autouse=True)
def _no_open_gap(monkeypatch):
    monkeypatch.setattr(mcam, "OPEN_GAP_S", 0.0)


def rect(x1, y1, x2, y2):
    return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]


def answer(*rects, name="liquid", conf=0.9, ms=7):
    """Поддельный ответ сервиса уровня: по контуру на каждый прямоугольник."""
    return {"image": {"w": W, "h": H}, "geometry": "lvldec", "count": len(rects), "timing": {"ms": ms}, "model": "fake.pt",
            "objects": [{"cls": 0, "name": name, "conf": conf, "bbox": [0, 0, 1, 1], "polygons": [rect(*r)]} for r in rects]}


def level_cam(**g):
    grp = {"id": "lvl", "type": "level", "zones": ["low", "up"], "warmup_s": 0, "alpha": 0, "glass1_min": 0, "glass2_min": 0,
           "infer_interval_s": 0.2, "hold_s": 5, "out": {"kind": "holding", "address": 11}}
    grp.update(g)
    return me.normalize_camera({"id": "dec", "url": "fake", "hw_accel": "off", "proc_fps": 20, "freeze_timeout_s": 5, "frame_size": [W, H],
                                "zones": [{"id": "low", "type": "level", "points": LOW}, {"id": "up", "type": "level", "points": UP}],
                                "groups": [grp]})


def rt(**g):
    cam = level_cam(**g)
    zc = {z["id"]: z for z in cam["zones"]}
    clock = [1000.0]
    r = ml.LevelRT(cam["groups"][0], [zc["low"], zc["up"]], cam["frame_size"], clock=lambda: clock[0])
    return r, clock


# ───────────────────────────── процессор (формулы LVLDec) ─────────────────────────────
def test_processor_is_total_of_two_halves_and_scale_50_each():
    p = ml.LevelProcessor(alpha=0, glass1_min=0, glass2_min=0)
    r = p.update(0.5, None)
    assert (round(r["glass1"], 3), r["glass2"], round(r["total"], 3)) == (25.0, 0.0, 25.0), "полстекла = 25 %, верхнего нет"
    assert round(p.update(0.3, 0.4)["total"], 3) == 70.0, "верхнее > 0 ⇒ нижнее заведомо полное: 50 + 0,4·50"


def test_processor_memory_when_no_data():
    p = ml.LevelProcessor(alpha=0, glass1_min=0, glass2_min=0)
    p.update(0.6, 0.0)
    r = p.update(None, None)
    assert round(r["glass1"], 3) == 30.0 and r["glass2"] == 0.0, "нет жидкости в кадре — прежнее значение, а не провал в ноль"


def test_processor_cuts_sediment_below_min_and_does_not_force_lower():
    p = ml.LevelProcessor(alpha=0, glass1_min=15, glass2_min=20)
    r = p.update(0.10, 0.15)
    assert (r["glass1"], r["glass2"]) == (0.0, 0.0), "ниже glass1_min / glass2_min — осадок, считаем пусто"
    r = p.update(0.10, 0.25)
    assert round(r["glass1"], 3) == 50.0 and round(r["glass2"], 3) == 12.5, "в верхнем реально есть — нижнее 100 %"


def test_processor_full_calibration_stretches_model_underestimate_to_100():
    p = ml.LevelProcessor(alpha=0, glass1_min=0, glass2_min=0, glass2_full=84)
    r = p.update(1.0, 0.84)
    assert round(r["glass2"], 3) == 50.0, "84 % сырого при ПОЛНОМ стекле = 100 %"
    assert round(ml.LevelProcessor(alpha=0, glass1_min=0, glass2_min=0, glass2_full=84).update(1.0, 0.42)["glass2"], 3) == 25.0


def test_processor_hysteresis_holds_100_one_tick_after_dropping_below_96():
    p = ml.LevelProcessor(alpha=0, glass1_min=0, glass2_min=0)
    assert round(p.update(0.97, None)["glass1"], 3) == 50.0, "выше 96 % — фиксируем 100 %"
    assert round(p.update(0.95, None)["glass1"], 3) == 50.0, "первый тик ниже 96 % ещё держит 100 % (как в LVLDec)"
    assert round(p.update(0.95, None)["glass1"], 3) == 47.5, "дальше уровень снова живой"


def test_processor_ema_smoothing():
    p = ml.LevelProcessor(alpha=0.5, glass1_min=0, glass2_min=0)
    assert round(p.update(1.0, None)["glass1"], 3) == 25.0
    assert round(p.update(1.0, None)["glass1"], 3) == 37.5, "smooth = smooth·alpha + level·(1 − alpha)"


# ───────────────────────────── уровень по зонам ─────────────────────────────
def test_area_ratio_by_zone_and_forced_lower():
    r, _ = rt()
    r.ingest(answer((90, 175, 150, 235)))                     # жидкость в нижней половине нижнего стекла, верхнее пусто
    s = r.snapshot(1000.0, True)
    assert s["valid"] and s["raw"][0] == pytest.approx(0.5, abs=0.03) and s["raw"][1] == 0.0
    assert s["total"] == pytest.approx(25.0, abs=1.6)
    r.ingest(answer((90, 120, 150, 235), (190, 60, 250, 115)))   # нижнее полное, верхнее на ~половину
    s = r.snapshot(1000.0, True)
    assert s["g1"] == pytest.approx(50.0) and s["raw"][1] == pytest.approx(0.5, abs=0.06)
    assert s["total"] == pytest.approx(50.0 + s["raw"][1] * 50, abs=0.01)


def test_no_liquid_objects_keeps_previous_value_and_other_classes_are_ignored():
    r, _ = rt()
    r.ingest(answer((90, 175, 150, 235)))
    before = r.snapshot(1000.0, True)["total"]
    r.ingest(answer())                                        # модель «мигнула»: жидкости нет
    assert r.snapshot(1000.0, True)["total"] == before, "нет жидкости в кадре → память, а не обнуление"
    r.ingest(answer((90, 120, 150, 235), name="glass"))       # чужой класс (стекло) жидкостью не считается
    assert r.snapshot(1000.0, True)["total"] == before


def test_zone_is_scaled_when_camera_gives_other_resolution():
    cam = level_cam()
    cam["frame_size"] = [W // 2, H // 2]                      # размечали на кадре вдвое меньше — зона растёт вдвое
    zc = {z["id"]: z for z in cam["zones"]}
    r = ml.LevelRT(cam["groups"][0], [zc["low"], zc["up"]], cam["frame_size"])
    r.ingest(answer((0, 0, 320, 240)))
    s = r.snapshot(r.clock(), True)
    assert s["raw"][0] > 0.99, "полностью залитый кадр — зона целиком залита после пересчёта координат"


def test_warmup_has_no_smoothing_then_alpha_applies():
    r, clock = rt(alpha=0.9, warmup_s=10)
    r.ingest(answer((90, 120, 150, 235)), now=1005.0)         # 5 с после старта — ещё прогрев
    assert r.snapshot(1005.0, True)["g1"] == pytest.approx(50.0, abs=0.01), "в прогрев сглаживания нет — значение сразу реальное"
    r2, _ = rt(alpha=0.9, warmup_s=10)
    r2.ingest(answer((90, 120, 150, 235)), now=1015.0)        # прогрев кончился — alpha 0,9: за один тик набирается 10 %
    assert r2.snapshot(1015.0, True)["g1"] == pytest.approx(5.0, abs=0.01)


def test_hold_then_zero_and_dead_camera_gives_zero():
    r, clock = rt(hold_s=5)
    r.ingest(answer((90, 120, 150, 235)), now=1000.0)
    assert r.snapshot(1003.0, True)["valid"], "ответ свежий — держим значение"
    s = r.snapshot(1006.5, True)
    assert not s["valid"] and s["total"] == 0.0 and s["age_s"] == 6.5, "модель молчит дольше hold_s — нули"
    assert not r.snapshot(1000.5, False)["valid"], "камера мертва — нули, как у остальных КЗ"


def test_reset_starts_from_scratch():
    r, _ = rt()
    r.ingest(answer((90, 120, 150, 235)))
    r.reset()
    assert r.snapshot(r.clock(), True)["valid"] is False and r.proc.smooth_g1 == 0.0


# ───────────────────────────── конфиг ─────────────────────────────
def test_level_config_defaults_match_production_lvldec():
    g = level_cam()["groups"][0]
    d = me.normalize_camera({"id": "d", "zones": [{"id": "a", "type": "level", "points": LOW}], "groups": [
        {"id": "l", "type": "level", "zones": ["a"]}]})["groups"][0]
    assert (d["conf"], d["infer_interval_s"], d["alpha"], d["warmup_s"], d["glass1_min"], d["glass2_min"]) == (0.35, 2, 0.93, 10, 15, 20)
    assert (d["glass1_full"], d["glass2_full"], d["geometry"], d["liquid_class"], d["service_url"]) == (100, 100, "lvldec", "liquid", "http://127.0.0.1:8770")
    assert g["out"] == {"kind": "holding", "address": 11}


@pytest.mark.parametrize("raw,part", [
    ({"zones": [{"id": "a", "type": "level", "points": LOW[:2]}]}, "меньше трёх точек"),
    ({"zones": [{"id": "a", "type": "motion", "points": LOW}], "groups": [{"id": "l", "type": "level", "zones": ["a"]}]}, "типа level"),
    ({"zones": [{"id": "a", "type": "level", "points": LOW}, {"id": "b", "type": "level", "points": UP}, {"id": "c", "type": "level", "points": UP}],
      "groups": [{"id": "l", "type": "level", "zones": ["a", "b", "c"]}]}, "одна-две"),
    ({"zones": [{"id": "a", "type": "level", "points": LOW}], "groups": [{"id": "l", "type": "level", "zones": ["a"], "geometry": "zzz"}]}, "геометрия"),
    ({"zones": [{"id": "a", "type": "level", "points": LOW}], "groups": [{"id": "l", "type": "level", "zones": ["a"], "alpha": 2}]}, "не больше"),
])
def test_level_config_rejects_bad_input(raw, part):
    with pytest.raises(ValueError) as e:
        me.normalize_camera(dict({"id": "c"}, **raw))
    assert part in str(e.value)


# ───────────────────────────── движок и запись в ПЛК ─────────────────────────────
def _gray_frames(n=6):
    return [np.full((H, W), 90, np.uint8) for _ in range(n)]


def test_engine_state_and_plc_value_is_total_times_ten():
    cam = level_cam()
    eng = me.CameraEngine(cam)
    eng.levels["lvl"].ingest(answer((90, 120, 150, 235), (190, 60, 250, 115)))
    st = None
    for i, f in enumerate(_gray_frames()):
        st = eng.process(f, 2000.0 + i / 20.0) or st
    g = st["groups"][0]
    assert g["type"] == "level" and g["valid"] and g["bits"] == [1, 1]
    assert g["value"] == int(round(g["total"] * 10)) and 700 <= g["value"] <= 790
    zl = {z["id"]: z for z in st["zones"]}
    assert zl["low"]["type"] == "level" and zl["low"]["flow"] == pytest.approx(100.0, abs=0.1), "нижнее стекло полное"
    assert zl["up"]["flow"] == pytest.approx(g["raw"][1], abs=0.2) and zl["up"]["bit"] == 1
    assert ("holding", 11, g["value"]) in eng.compute_writes(st), "Total ×10 уходит в выбранный регистр"


def test_engine_dead_camera_zeroes_level():
    eng = me.CameraEngine(level_cam())
    eng.levels["lvl"].ingest(answer((90, 120, 150, 235)))
    st = eng.dead_state(3000.0, "нет кадров")
    assert st["groups"][0]["value"] == 0 and not st["groups"][0]["valid"] and st["zones"][0]["bit"] == 0


def test_contour_only_when_asked():
    for want in (True, False):
        eng = me.CameraEngine(level_cam(contour=want))
        eng.levels["lvl"].ingest(answer((90, 120, 150, 235)))
        st = None
        for i, f in enumerate(_gray_frames()):
            st = eng.process(f, 2000.0 + i / 20.0) or st
        g = st["groups"][0]
        assert ("contour" in g) is want
        if want:
            assert g["contour"] and g["contour_wh"] == [W, H]


# ───────────────────────────── воркер + поток модели ─────────────────────────────
class FakeCap:
    def __init__(self, delay=0.02):
        self.delay = delay

    def isOpened(self):
        return True

    def grab(self):
        time.sleep(self.delay)
        return True

    def retrieve(self):
        rng = np.random.default_rng(int(time.time() * 1000) % 1000)
        return True, cv2.cvtColor((rng.integers(60, 120, (H, W))).astype(np.uint8), cv2.COLOR_GRAY2BGR)

    def release(self):
        pass

    def set(self, *_):
        return True

    def get(self, *_):
        return 25.0


class FakeClient:
    connected = True

    def __init__(self, host, port):
        self.log = []

    def connect(self):
        return True

    def close(self):
        pass

    def write_register(self, address, value, device_id=1):
        self.log.append(("holding", address, value))
        return type("R", (), {"isError": lambda s: False})()

    def write_coil(self, address, value, device_id=1):
        self.log.append(("coil", address, value))
        return type("R", (), {"isError": lambda s: False})()


def _wait(cond, timeout=8.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _worker(tmp_path, post, plc=True, **g):
    cam = level_cam(**g)
    mb = me.normalize_modbus({"enable": plc, "host": "plc", "write_period_s": 0.2})
    clients = []

    def factory(host, port):
        clients.append(FakeClient(host, port))
        return clients[-1]

    w = CameraWorker(cam, mb, mm.LinkRegistry(factory), tmp_path, lambda url, tr: FakeCap(), post)
    w.start()
    return w, clients


def test_worker_asks_model_and_writes_total_x10_to_plc(tmp_path):
    calls = []

    def post(url, jpeg, params, timeout):
        calls.append((url, jpeg[:2], dict(params)))
        return answer((90, 120, 150, 235))                     # нижнее стекло полное, верхнее пусто → 50 %

    w, clients = _worker(tmp_path, post)
    try:
        assert _wait(lambda: (w.state()["result"] or {}).get("groups") and w.state()["result"]["groups"][0]["valid"])
        g = w.state()["result"]["groups"][0]
        assert g["total"] == pytest.approx(50.0, abs=0.01) and g["model"] == "fake.pt" and g["ms"] == 7
        assert _wait(lambda: clients and ("holding", 11, 500) in clients[0].log), "50,0 % → регистр 11 = 500"
        url, magic, params = calls[0]
        assert url == "http://127.0.0.1:8770" and magic == b"\xff\xd8", "на сервис уходит JPEG кадра"
        assert params == {"conf": 0.35, "geometry": "lvldec"}
        assert _wait(lambda: len(calls) >= 3), "модель опрашивается регулярно (infer_interval_s), а не один раз"
    finally:
        w.stop()


def test_worker_with_dead_level_service_reports_error_and_zero(tmp_path):
    w, clients = _worker(tmp_path, lambda *a: {"error": "модель не загружена"})
    try:
        assert _wait(lambda: (w.state()["result"] or {}).get("groups") and w.state()["result"]["groups"][0]["err"])
        g = w.state()["result"]["groups"][0]
        assert g["err"] == "модель не загружена" and not g["valid"] and g["value"] == 0
        assert _wait(lambda: clients and ("holding", 11, 0) in clients[0].log), "пока модели нет — в ПЛК ноль, не мусор"
    finally:
        w.stop()


def test_worker_plc_write_is_off_by_default(tmp_path):
    w, clients = _worker(tmp_path, lambda *a: answer((90, 120, 150, 235)), plc=False)
    try:
        assert _wait(lambda: (w.state()["result"] or {}).get("groups") and w.state()["result"]["groups"][0]["valid"])
        time.sleep(0.6)
        assert not clients, "запись в ПЛК выключена — соединения с контроллером нет вовсе"
        assert w.state()["plc"]["enabled"] is False
    finally:
        w.stop()


# ───────────────────────────── HTTP к сервису уровня ─────────────────────────────
class _H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        _H.seen = (self.path, len(body))
        code, data = _H.reply
        raw = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


def _serve():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


def test_post_infer_reads_answer_and_error_text():
    srv, url = _serve()
    try:
        _H.reply = (200, answer((1, 1, 5, 5)))
        r = ml.post_infer(url, b"\xff\xd8abc", {"conf": 0.35, "geometry": "exact"})
        assert r["count"] == 1 and _H.seen[0] == "/infer?conf=0.35&geometry=exact" and _H.seen[1] == 5
        _H.reply = (409, {"error": "модель не загружена"})
        assert ml.post_infer(url, b"x", {}) == {"error": "модель не загружена"}
    finally:
        srv.shutdown()
    assert "не отвечает" in ml.post_infer("http://127.0.0.1:1", b"x", {}, timeout=1)["error"]


# ───────────────────────────── перенос config.ini от LVLDec ─────────────────────────────
LVLDEC_INI = """\
[camera]
rtsp_url = rtsp://admin:secret@192.168.3.239:554/cam/realmonitor?channel=1&subtype=0

[plc]
ip = 192.168.20.14
port = 502
device_id = 1
mb_address = 11
heartbeat_register = -1

[detection]
model = 26x_detection.pt
conf = 0.35
imgsz = 640
resize_w = 0
resize_h = 0
alpha = 0.93
glass1_min = 15
glass2_min = 20
glass1_full = 100
glass2_full = 84
infer_interval_sec = 2

[runtime]
stale_timeout_sec = 20
value_stuck_sec = 60
restart_hours = 0
warmup_sec = 10
state_save_sec = 120

[zones]
; Полигоны стёкол
lower = 757,559 757,1050 834,1053 831,560
upper = 1400,23 1379,562 1454,563 1479,24
"""


def test_import_lvldec_ini_maps_production_config():
    import motion_lvldec as lv
    r = lv.import_ini(LVLDEC_INI)
    assert r["url"].startswith("rtsp://admin:secret@192.168.3.239:554/cam/realmonitor?channel=1&subtype=0") and r["model"] == "26x_detection.pt"
    assert r["zones"]["lower"] == [[757, 559], [757, 1050], [834, 1053], [831, 560]] and len(r["zones"]["upper"]) == 4
    g = r["group"]
    assert (g["conf"], g["imgsz"], g["infer_interval_s"], g["alpha"], g["warmup_s"]) == (0.35, 640, 2.0, 0.93, 10.0)
    assert (g["glass1_min"], g["glass2_min"], g["glass1_full"], g["glass2_full"]) == (15, 20, 100, 84)
    assert r["plc"] == {"host": "192.168.20.14", "port": 502, "device_id": 1, "address": 11}
    assert "out" not in g, "прямого вывода группы нет: в ПЛК пишет сценарий (как у остальных КЗ)"
    assert any("не переносятся" in n for n in r["notes"]) and any("26x_detection.pt" in n for n in r["notes"])


def test_imported_fragment_passes_config_validation_end_to_end():
    import motion_lvldec as lv
    r = lv.import_ini(LVLDEC_INI)
    cam = me.normalize_camera({"id": "dec", "url": r["url"], "frame_size": [1920, 1080],
                               "zones": [{"id": "low", "type": "level", "points": r["zones"]["lower"]},
                                         {"id": "up", "type": "level", "points": r["zones"]["upper"]}],
                               "groups": [dict(r["group"], id="lvl", type="level", zones=["low", "up"])]})
    assert cam["groups"][0]["glass2_full"] == 84 and cam["groups"][0]["out"] is None
    sc = me.normalize_config({"cameras": [cam], "scenarios": [{"id": "lvl_dec", "rule": "sum", "sources": [{"camera": "dec", "group": "lvl"}],
                                                               "out": {"kind": "holding", "address": r["plc"]["address"]},
                                                               "modbus": {"enable": False, "host": r["plc"]["host"], "port": r["plc"]["port"],
                                                                          "device_id": r["plc"]["device_id"]}}]})["scenarios"][0]
    assert sc["modbus"]["enable"] is False and sc["sources"] == [{"camera": "dec", "group": "lvl"}] and sc["out"]["address"] == 11


@pytest.mark.parametrize("text,part", [
    ("hello", "не config.ini"),
    ("[detection]\nconf = abc\n", "нужно число"),
    ("[zones]\nlower = 1,2 3\n", "нужно x,y"),
])
def test_import_lvldec_ini_rejects_garbage(text, part):
    import motion_lvldec as lv
    with pytest.raises(ValueError) as e:
        lv.import_ini(text)
    assert part in str(e.value)


def test_import_lvldec_ini_without_upper_zone_and_with_few_points():
    import motion_lvldec as lv
    r = lv.import_ini("[detection]\nconf = 0.4\n[zones]\nlower = 1,1 5,1 5,5\nupper = 1,1 2,2\n")
    assert r["zones"]["lower"] and r["zones"]["upper"] is None
    assert any("верхней зоны нет" in n for n in r["notes"])


# ── кадры для обучения: JPEG, ушедший в модель, + разметка YOLO-seg ───────────────────────────────────────────────

def test_yolo_label_is_normalized_and_only_liquid():
    objs = answer((10, 20, 110, 220))["objects"] + [{"cls": 1, "name": "glass", "conf": 0.9, "bbox": [0, 0, 1, 1], "polygons": [rect(0, 0, 5, 5)]}]
    lines = ml.yolo_label(objs, "liquid", W, H).strip().split("\n")
    assert len(lines) == 1 and lines[0].startswith("0 ")
    nums = [float(v) for v in lines[0].split()[1:]]
    assert len(nums) == 8 and nums[:2] == pytest.approx([10 / W, 20 / H], abs=1e-5) and max(nums) <= 1.0
    assert ml.yolo_label([], "liquid", W, H) == ""


def test_prune_frames_keeps_newest_and_removes_labels_together(tmp_path):
    (tmp_path / "images").mkdir()
    (tmp_path / "labels").mkdir()
    for i in range(5):
        (tmp_path / "images" / ("2026110%d.jpg" % i)).write_bytes(b"xxx")
        (tmp_path / "labels" / ("2026110%d.txt" % i)).write_text("0 0 0 1 1 1 0\n")
    assert ml.frames_stats(tmp_path) == (5, 15)
    assert ml.prune_frames(tmp_path, 2) == (3, 9), "стёрто 3 кадра, 9 байт"
    assert sorted(p.name for p in (tmp_path / "images").glob("*")) == ["20261103.jpg", "20261104.jpg"]
    assert sorted(p.name for p in (tmp_path / "labels").glob("*")) == ["20261103.txt", "20261104.txt"]


def _infer_step(tmp_path, posted, geometry="exact", **g):
    g.setdefault("frames_keep", 10)
    cam = level_cam(geometry=geometry, **g)
    zc = {z["id"]: z for z in cam["zones"]}
    rtt = ml.LevelRT(cam["groups"][0], [zc["low"], zc["up"]], cam["frame_size"])

    def post(url, jpeg, params, timeout):
        posted.append(dict(params))
        return answer((90, 120, 150, 235))

    return ml.LevelInfer(rtt, lambda: np.full((H, W, 3), 90, np.uint8), threading.Event(), post, save_dir=tmp_path / "lf")


def test_frames_are_off_by_default_and_status_says_so(tmp_path):
    li = _infer_step(tmp_path, [])
    li.step()
    assert not (tmp_path / "lf").exists()
    assert li.rt.snapshot(None, True)["frames"]["state"] == "off"


def test_saves_frame_and_label_every_frames_every_s_while_window_is_open(tmp_path):
    posted = []
    li = _infer_step(tmp_path, posted, frames_on=True, frames_run=7, frames_every_s=300, frames_hours=2)
    li.step()
    li.step()                                                  # второй раз сразу — ещё рано (интервал 300 с)
    jpgs = list((tmp_path / "lf" / "images").glob("*.jpg"))
    txts = list((tmp_path / "lf" / "labels").glob("*.txt"))
    assert len(jpgs) == 1 and len(txts) == 1 and jpgs[0].stem == txts[0].stem
    assert jpgs[0].stem.endswith("_n100_v0"), "в имени — сырой уровень нижнего и верхнего, %"
    assert cv2.imread(str(jpgs[0])).shape[:2] == (H, W), "кадр полного размера, без разметки"
    assert txts[0].read_text().startswith("0 ")
    fr = li.rt.snapshot(None, True)["frames"]
    assert fr["state"] == "run" and 7100 < fr["left_s"] <= 7200 and fr["saved"] == 1 and fr["mb"] >= 0
    assert (tmp_path / "lf" / "run.json").exists()
    assert len(posted) == 2 and all(p["geometry"] == "exact" for p in posted), "exact — второго запроса за честными контурами не нужно"


def test_window_ends_by_itself_and_new_run_id_starts_a_new_one(tmp_path):
    posted = []
    li = _infer_step(tmp_path, posted, frames_on=True, frames_run=7, frames_every_s=1, frames_hours=1)
    (tmp_path / "lf").mkdir()
    (tmp_path / "lf" / "run.json").write_text('{"run": 7, "t0": %f}' % (time.time() - 3700))      # час давно прошёл
    li.step()
    assert li.rt.snapshot(None, True)["frames"]["state"] == "done"
    assert not (tmp_path / "lf" / "images").exists(), "окно закрыто — кадров нет"
    li.rt.g["frames_run"] = 8                                  # кнопка «Начать» ещё раз
    li.step()
    assert li.rt.snapshot(None, True)["frames"]["state"] == "run"
    assert len(list((tmp_path / "lf" / "images").glob("*.jpg"))) == 1


def test_window_survives_restart(tmp_path):
    posted = []
    a = _infer_step(tmp_path, posted, frames_on=True, frames_run=7, frames_every_s=300, frames_hours=1)
    a.step()
    t0 = a._run[1]
    b = _infer_step(tmp_path, posted, frames_on=True, frames_run=7, frames_every_s=300, frames_hours=1)     # перезапуск камеры
    b.step()
    assert b._run == (7, t0), "тот же запуск продолжается с прежнего начала, а не начинается заново"


def test_stops_when_disk_is_nearly_full(tmp_path, monkeypatch):
    monkeypatch.setattr(ml, "FRAMES_MIN_FREE", 10 ** 18)
    li = _infer_step(tmp_path, [], frames_on=True, frames_run=1, frames_every_s=1)
    li.step()
    assert li.rt.snapshot(None, True)["frames"]["state"] == "disk"
    assert not (tmp_path / "lf" / "images").exists()


def test_label_uses_exact_geometry_even_if_level_is_lvldec(tmp_path):
    posted = []
    li = _infer_step(tmp_path, posted, geometry="lvldec", frames_on=True, frames_run=1, frames_every_s=300)
    li.step()
    assert [p["geometry"] for p in posted] == ["lvldec", "exact"], "для разметки — отдельный запрос с честной геометрией"
    assert len(list((tmp_path / "lf" / "labels").glob("*.txt"))) == 1


def test_saver_failure_does_not_break_level(tmp_path):
    li = _infer_step(tmp_path, [], frames_on=True, frames_run=1, frames_every_s=300)
    (tmp_path / "lf").write_text("файл вместо папки")          # mkdir упадёт
    li.step()
    assert li.rt.snapshot(None, True)["valid"], "уровень посчитан, хотя кадр не сохранился"


def test_level_config_frames_defaults_and_limits():
    g = level_cam()["groups"][0]
    assert (g["frames_on"], g["frames_every_s"], g["frames_hours"], g["frames_keep"]) == (False, 60, 24, 1000)
    for bad in ({"frames_keep": 1}, {"frames_every_s": 0}, {"frames_hours": 0}):
        with pytest.raises(ValueError):
            level_cam(**bad)
