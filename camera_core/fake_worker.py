"""camera_core.fake_worker — фейковые камеры для WEB_MVS_FAKE=1 (см. fake_mode.py).

FakeGigeWorker наследует настоящий CameraWorker: сохранение фото/видео, цветокоррекция, метрики, сохранённые параметры
камеры (camera_params) работают как есть; подменены только то, что лезло в Harvester/MVS SDK — чтение IP/инфо/сети, предел
параметров и сам цикл стрима (кадры берём из fake_frames).
FakeRtspWorker наследует RtspCameraWorker и подменяет только открытие захвата (вместо cv2.VideoCapture — FakeCapture);
цикл захвата, зум, качество кадра, автосохранение — настоящие. Dahua CGI подменён в fake_mode.fake_cgi.
"""
import time

import cv2

import camera_params
import fake_mode
from logger import log_event

import fake_frames
from .gige_worker import CameraWorker
from .imaging import _apply_color
from .rtsp_worker import RtspCameraWorker

PIXEL_FORMATS = ["BayerRG8", "RGB8Packed", "Mono8"]
MAX_FPS = 15.0


class FakeGigeWorker(CameraWorker):
    """GigE-камера без железа: кадры синтетические, настройки принимает и «запоминает» как настоящая."""

    def __init__(self, serial_number, manager):
        super().__init__(serial_number, manager)
        ip, model = fake_mode.GIGE_DEVICES.get(serial_number, ("192.168.1.250", "MV-CS050-10GC (fake)"))
        self._fake_ip, self._fake_model = ip, model
        self._fake_mask, self._fake_gw, self._fake_dhcp = "255.255.255.0", "192.168.1.1", False
        self.data_limit = {
            "width": {"value": fake_frames.SENSOR_W, "min": 64, "max": fake_frames.SENSOR_W, "step": 8},
            "height": {"value": fake_frames.SENSOR_H, "min": 64, "max": fake_frames.SENSOR_H, "step": 2},
            "offset_x": {"value": 0, "min": 0, "max": fake_frames.SENSOR_W - 64, "step": 8},
            "offset_y": {"value": 0, "min": 0, "max": fake_frames.SENSOR_H - 64, "step": 2},
            "exposure_time": {"value": 10000, "min": 20, "max": 1000000},
            "exposure_auto": {"value": "Off", "options": ["Off", "Once", "Continuous"]},
            "sensor": {"width_max": fake_frames.SENSOR_W, "height_max": fake_frames.SENSOR_H},
            "pixel_format": {"value": "BayerRG8", "options": list(PIXEL_FORMATS)},
        }
        # «состояние матрицы»: то, что камера приняла в последний раз (как при открытии потока)
        self._cfg = {"width": fake_frames.SENSOR_W, "height": fake_frames.SENSOR_H, "offset_x": 0, "offset_y": 0,
                     "exposure_auto": "Off", "exposure_time": 10000.0, "gain_auto": "Off", "gain": 0.0,
                     "pixel_format": "BayerRG8", "fps": 5.0}

    # ---------- control-канал: без Harvester ----------

    def get_ip(self, interface_id=None, device_handle=None):
        return {"ip": self._fake_ip}

    def get_info(self, interface_id=None, device_handle=None):
        c = self._cfg
        items = [("Модель", self._fake_model), ("Производитель", "web_MVS fake"), ("Серийный номер", self.serial_number),
                 ("Версия прошивки", "fake-1.0"), ("IP-адрес", self._fake_ip), ("Маска подсети", self._fake_mask),
                 ("Шлюз", self._fake_gw), ("MAC-адрес", "00:11:22:33:44:%02X" % (sum(map(ord, self.serial_number)) % 256)),
                 ("Макс. разрешение", "%d × %d" % (fake_frames.SENSOR_W, fake_frames.SENSOR_H)),
                 ("Текущее разрешение", "%d × %d" % (c["width"], c["height"])), ("Формат пикселей", c["pixel_format"]),
                 ("Частота кадров", "%.2f fps" % c["fps"]), ("Температура", "41.0 °C")]
        return {"items": [{"label": a, "value": str(b)} for a, b in items]}

    def get_network_settings(self, interface_id=None, device_handle=None):
        return self._fake_ip, self._fake_mask, self._fake_gw, self._fake_dhcp

    def sdk_read_data_limit(self):
        return self.data_limit

    def change_ip(self, ip, mask="", gateway=""):
        """Принимаем смену адреса (как после DeviceReset): запоминаем, камера остаётся доступной."""
        if ip == self._fake_ip and (not mask or mask == self._fake_mask) and (not gateway or gateway == self._fake_gw):
            return {"ip": "no_changes"}
        if ip == (gateway or self._fake_gw):
            return {"ip": "gateway==ip"}
        if self.running:
            return {"ip": "stream_not_closed"}
        self._fake_ip = ip
        if self.advanced_settings:
            self._fake_mask, self._fake_gw = mask or self._fake_mask, gateway or self._fake_gw
        self.advanced_settings = False
        log_event("camera_core.change_ip", "[fake] IP камеры изменён", "info", {"serial_number": self.serial_number, "ip": ip})
        return {"ip": "ip_changed"}

    # ---------- стрим ----------

    def _apply_fake(self, s):
        """Принять настройки как камера: в пределах матрицы, шаг 8/2; возвращает фактический конфиг."""
        c = self._cfg
        if s.get("pixel_format") in PIXEL_FORMATS:
            c["pixel_format"] = s["pixel_format"]
        W, H = fake_frames.SENSOR_W, fake_frames.SENSOR_H
        w = int(s.get("width") or c["width"]) // 8 * 8
        h = int(s.get("height") or c["height"]) // 2 * 2
        c["width"], c["height"] = max(64, min(w, W)), max(64, min(h, H))
        ox = int(s.get("offset_x") if s.get("offset_x") is not None else c["offset_x"]) // 8 * 8
        oy = int(s.get("offset_y") if s.get("offset_y") is not None else c["offset_y"]) // 2 * 2
        c["offset_x"], c["offset_y"] = max(0, min(ox, W - c["width"])), max(0, min(oy, H - c["height"]))
        if s.get("fps"):
            c["fps"] = max(0.1, min(float(s["fps"]), MAX_FPS))
        if s.get("exposure_auto") in ("Off", "Once", "Continuous"):
            c["exposure_auto"] = s["exposure_auto"]
        if s.get("exposure_time") and c["exposure_auto"] == "Off":
            c["exposure_time"] = max(20.0, min(float(s["exposure_time"]), 1000000.0))
        if s.get("gain_auto") in ("Off", "Once", "Continuous"):
            c["gain_auto"] = s["gain_auto"]
        if s.get("gain") is not None and c["gain_auto"] == "Off":
            c["gain"] = max(0.0, min(float(s["gain"]), 24.0))
        self.data_limit["width"]["value"], self.data_limit["height"]["value"] = c["width"], c["height"]
        return dict(c)

    def generate(self, width=None, height=None, offset_x=None, offset_y=None, fps=None, exposure_auto=None,
                 exposure_time=None, pixel_format=None, client_id=None, save=False):
        self._start_lock.acquire()
        try:
            self._session += 1
            token = self._session
            self.client_id = client_id
            settings = self._resolve_settings({"width": width, "height": height, "offset_x": offset_x, "offset_y": offset_y,
                                               "fps": fps, "exposure_auto": exposure_auto, "exposure_time": exposure_time,
                                               "pixel_format": pixel_format}, save)
            self.current_config = self._apply_fake(settings)
            if save:
                camera_params.store(self.serial_number, self.current_config)
            self.running = True
        finally:
            self._start_lock.release()
        log_event("camera_core.generate_stream", "[fake] Поток камеры запущен", "success", {"serial_number": self.serial_number})

        self.metrics.update(errors=0, image_number=0, fps=0.0, bandwidth_mbps=0.0)
        last_t = None
        n = 0
        try:
            while self.running and self._session == token:
                c = self._cfg
                t0 = time.time()
                n += 1
                img = fake_frames.render(self.serial_number, n, "micro", c["width"], c["height"], c["offset_x"], c["offset_y"],
                                         c["exposure_time"], c["gain"], mono=c["pixel_format"].startswith("Mono"))
                self._last_bgr = img
                self._black_feed(img)
                if self.color:
                    img = _apply_color(img, self.color)
                ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if not ok:
                    self.metrics["errors"] += 1
                    continue
                frame = enc.tobytes()
                now = time.time()
                self.metrics["image_number"] += 1
                self.metrics["width"], self.metrics["height"] = img.shape[1], img.shape[0]
                if last_t is not None and now > last_t:
                    self.metrics["fps"] = 1.0 / (now - last_t)
                    self.metrics["bandwidth_mbps"] = img.size * 8 * self.metrics["fps"] / 1e6 / 3
                last_t = now
                self._maybe_save(img, c["fps"])
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                wait = t0 + 1.0 / c["fps"] - time.time()
                if wait > 0:
                    time.sleep(wait)
        finally:
            if self._session == token:
                self.running = False
                self._reset_save_state()
            log_event("camera_core.generate_stream", "[fake] Поток камеры закрыт", "info", {"serial_number": self.serial_number})

    def force_close(self):
        self.running = False
        log_event("camera_core.close_stream_force", "[fake] Принудительная остановка потока", "warn")
        return {"status": "force_stopped"}


class FakeRtspWorker(RtspCameraWorker):
    """RTSP-камера без железа: захват отдаёт синтетику; цикл, зум, сохранение — настоящие."""

    def _open_capture(self):
        self.decoder = "cpu"
        return fake_frames.FakeCapture(self.serial_number, fps=8.0, kind="rtsp")
