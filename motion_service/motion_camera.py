"""camera — рабочий поток одной камеры: RTSP/файл → кадры → CameraEngine → состояние + запись в ПЛК.

Что делает сверх engine:
  • переподключение к потоку (пауза reconnect_delay_s), RTSP по tcp (убирает «bad cseq» и потери пакетов);
  • детектор зависшей камеры: картинка не меняется дольше freeze_timeout_s (или кадров нет столько же) → все биты и
    уровни = 0, status = 0 — в ПЛК уходят нули и «камера не жива», детекция сама оживает, когда кадры пойдут;
  • отдельный поток записи в ПЛК: недоступный контроллер не тормозит разбор кадров (запись — по изменению и раз в
    write_period_s);
  • последний кадр для просмотра (snapshot), история метрик для графиков калибровки и запись в CSV.

Источник — rtsp://… или путь к видеофайлу (для проверки без камеры; файл крутится по кругу, темп — по FPS файла).
"""
from __future__ import annotations

import csv
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np

from motion_engine import CameraEngine
from motion_modbus import LinkRegistry

log = logging.getLogger("motion.camera")

HISTORY_LEN = 900                # точек истории на камеру (~5 мин при 3 fps)
RECORD_MAX_BYTES = 100 * 1024 * 1024
_OPEN_LOCK = threading.Lock()    # OPENCV_FFMPEG_CAPTURE_OPTIONS — переменная окружения процесса, выставляем под замком


def is_stream_url(url: str) -> bool:
    return str(url).lower().startswith(("rtsp://", "rtsps://", "rtmp://", "http://", "https://"))


def garbage_ratio(gray: np.ndarray, bs: int = 16) -> float:
    """Доля блоков bs×bs, залитых ровным серым ~128: так декодер H.264 «стирает» кадр при потере пакетов (серая каша).
    Живой кадр (даже тёмный/засвеченный) такого не даёт: у него есть шум матрицы или яркость далеко от 128."""
    h, w = gray.shape[:2]
    h -= h % bs
    w -= w % bs
    if h < bs or w < bs:
        return 0.0
    b = gray[:h, :w].reshape(h // bs, bs, w // bs, bs).astype(np.float32).transpose(0, 2, 1, 3).reshape(-1, bs * bs)
    flat = (b.std(axis=1) < 1.5) & (np.abs(b.mean(axis=1) - 128.0) <= 10.0)
    return float(flat.mean())


def open_capture(url: str, transport: str = "tcp"):
    """cv2.VideoCapture с таймаутами открытия/чтения (без них мёртвая камера вешает поток надолго)."""
    with _OPEN_LOCK:
        if str(url).lower().startswith("rtsp") and transport in ("tcp", "udp"):
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;%s" % transport
        try:
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000,
                                                         cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000])
        except Exception:
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass
    return cap


class CameraWorker(threading.Thread):
    def __init__(self, cam_cfg: dict, modbus_cfg: dict, links: LinkRegistry, data_dir: Path,
                 capture_factory: Callable = open_capture):
        super().__init__(daemon=True, name="cam-" + cam_cfg["id"])
        self.cfg = cam_cfg
        self.id = cam_cfg["id"]
        self.mb = cam_cfg["modbus"] or modbus_cfg
        self.links = links
        self.data_dir = Path(data_dir)
        self.capture_factory = capture_factory
        self.engine = CameraEngine(cam_cfg)
        self._halt = threading.Event()
        self._lock = threading.Lock()
        self._frame: Optional[np.ndarray] = None
        self._state: Optional[dict] = None
        self._connected = False
        self._conn_logged: Optional[bool] = None
        self._error = ""
        self._last_good = 0.0                 # когда последний раз пришёл кадр
        self._stamps: deque = deque(maxlen=12)
        self._history: deque = deque(maxlen=HISTORY_LEN)
        self._writes: Optional[list] = None   # что должно лежать в ПЛК (последнее)
        self._writes_evt = threading.Event()
        self._rec_fh = self._rec_csv = None
        self._rec_path: Optional[Path] = None
        self._rec_ids: list = []
        self._writer: Optional[threading.Thread] = None
        self.plc = {"enabled": bool(self.mb.get("enable")), "ok": None, "error": ""}

    # ─────────── внешний API ───────────
    def stop(self):
        self._halt.set()
        self._writes_evt.set()
        self.stop_record()

    def state(self) -> dict:
        with self._lock:
            st = dict(self._state) if self._state else None
            fps = 0.0
            if len(self._stamps) >= 2 and self._stamps[-1] > self._stamps[0]:
                fps = (len(self._stamps) - 1) / (self._stamps[-1] - self._stamps[0])
            return {"id": self.id, "name": self.cfg["name"], "enabled": self.cfg["enabled"], "connected": self._connected,
                    "error": self._error, "fps": round(fps, 2), "age_s": round(time.time() - self._last_good, 1) if self._last_good else None,
                    "recording": self._rec_path.name if self._rec_path else None, "plc": dict(self.plc), "result": st}

    def snapshot_jpeg(self, max_w: int = 0) -> Optional[tuple]:
        """(jpeg-байты, (ширина, высота) оригинала) последнего кадра или None."""
        with self._lock:
            f = self._frame
        if f is None:
            return None
        h, w = f.shape[:2]
        if max_w and w > max_w:
            f = cv2.resize(f, (max_w, int(round(h * max_w / w))), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return (buf.tobytes(), (w, h)) if ok else None

    def history(self, n: int = 300) -> dict:
        with self._lock:
            pts = list(self._history)[-max(1, n):]
        zones: dict = {}
        for _, z, _ok in pts:
            for zid, (fl, df, bit) in z.items():
                d = zones.setdefault(zid, {"flow": [], "diff": [], "bit": []})
                d["flow"].append(fl)
                d["diff"].append(df)
                d["bit"].append(bit)
        return {"t": [round(t, 2) for t, _, _ in pts], "ok": [ok for _, _, ok in pts], "zones": zones}

    def start_record(self) -> str:
        """Писать метрики каждого кадра в CSV (data/records) — для калибровки порогов по реальной ленте."""
        with self._lock:
            if self._rec_path:
                return self._rec_path.name
            d = self.data_dir / "records"
            d.mkdir(parents=True, exist_ok=True)
            self._rec_path = d / ("%s_%s.csv" % (self.id, time.strftime("%Y%m%d_%H%M%S")))
            self._rec_ids = [z["id"] for z in self.cfg["zones"] if z["enabled"]]
            self._rec_fh = open(self._rec_path, "w", newline="", encoding="utf-8-sig")
            self._rec_csv = csv.writer(self._rec_fh, delimiter=";")
            head = ["time"]
            for zid in self._rec_ids:
                head += ["%s_flow" % zid, "%s_diff" % zid, "%s_bit" % zid]
            self._rec_csv.writerow(head + ["alive"])
            return self._rec_path.name

    def stop_record(self):
        with self._lock:
            if self._rec_fh:
                try:
                    self._rec_fh.close()
                except Exception:
                    pass
            self._rec_fh = self._rec_csv = None
            self._rec_path = None

    # ─────────── внутреннее ───────────
    def _publish(self, state: dict, frame: Optional[np.ndarray], count_frame: bool):
        writes = self.engine.compute_writes(state)
        with self._lock:
            self._state = state
            if frame is not None:
                self._frame = frame
            if count_frame:
                self._stamps.append(state["ts"])
                self._history.append((state["ts"], {z["id"]: (z["flow"], z["diff"], z["bit"]) for z in state["zones"]}, 1 if state["alive"] else 0))
            self._writes = writes
            if self._rec_csv:
                row = [time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(state["ts"])) + ".%03d" % int(state["ts"] % 1 * 1000)]
                byid = {z["id"]: z for z in state["zones"]}
                for zid in self._rec_ids:
                    z = byid.get(zid) or {}
                    row += [z.get("flow", 0), z.get("diff", 0), z.get("bit", 0)]
                self._rec_csv.writerow(row + [int(state["alive"])])
                if self._rec_fh.tell() > RECORD_MAX_BYTES:
                    self._rec_fh.close()
                    self._rec_fh = self._rec_csv = None
                    self._rec_path = None
        self._writes_evt.set()

    def _publish_held(self, now: float, frame: np.ndarray):
        """Каша: показываем последнее состояние с отметкой corrupt, историю и запись в ПЛК не двигаем."""
        with self._lock:
            st = dict(self._state) if self._state else None
            self._frame = frame
        if st is not None:
            st["corrupt"] = True
            st["ts"] = now
            with self._lock:
                self._state = st
                self._stamps.append(now)
                self._history.append((now, {z["id"]: (z["flow"], z["diff"], z["bit"]) for z in st["zones"]}, 0))

    def _plc_loop(self):
        """Поток записи: пишет, когда значения изменились или прошло write_period_s. Недоступный ПЛК тормозит только его."""
        link = None
        sent: Optional[list] = None
        last = 0.0
        period = float(self.mb.get("write_period_s", 1.0))
        while not self._halt.is_set():
            self._writes_evt.wait(timeout=period)
            self._writes_evt.clear()
            if self._halt.is_set():
                break
            if not self.mb.get("enable"):
                self.plc.update(enabled=False, ok=None, error="")
                continue
            with self._lock:
                writes = list(self._writes) if self._writes is not None else None
            if not writes:
                continue
            now = time.time()
            if writes == sent and now - last < period:
                continue
            if link is None:
                link = self.links.get(self.mb["host"], self.mb["port"], self.mb["device_id"])
            ok = link.write(writes)
            last = now
            if ok:
                sent = writes
            self.plc.update(enabled=True, ok=ok, error="" if ok else link.last_error)

    def run(self):
        cfg = self.cfg
        url = cfg["url"]
        if not cfg["enabled"] or not url:
            self._error = "камера выключена" if not cfg["enabled"] else "не задан адрес потока"
            return
        self._writer = threading.Thread(target=self._plc_loop, daemon=True, name="plc-" + self.id)
        self._writer.start()
        file_src = not is_stream_url(url)
        interval = self.engine.proc_interval
        timeout = cfg["freeze_timeout_s"]
        cap = None
        last_proc = 0.0
        last_gray = None
        freeze_time = 0.0
        frozen = False
        bad_since = 0.0                        # когда началась «серая каша» (0 — кадр нормальный)
        bad_until = 0.0                        # пока не истечёт — кадры после каши не считаем
        was_bad = False
        next_t = 0.0                           # темп чтения файла
        while not self._halt.is_set():
            now = time.time()
            if cap is None or not cap.isOpened():
                if cap is not None:
                    cap.release()
                cap = self.capture_factory(url, cfg["transport"])
                if not cap.isOpened():
                    self._note_conn(False, "нет связи с источником")
                    self._dead(now, timeout, "нет кадров")
                    self._halt.wait(cfg["reconnect_delay_s"])
                    continue
                self._note_conn(True, "")
                self.engine.reset()
                last_gray, last_proc, freeze_time, frozen = None, 0.0, 0.0, False
                if file_src:
                    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
                    file_dt = 1.0 / max(1.0, fps)
                    next_t = time.time()
            if not cap.grab():
                if file_src and cfg["loop"]:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    self.engine.reset()
                    last_gray = None
                    continue
                self._note_conn(False, "поток оборвался")
                cap.release()
                cap = None
                self._dead(time.time(), timeout, "поток оборвался")
                self._halt.wait(cfg["reconnect_delay_s"])
                continue
            if file_src:                       # без паузы файл читался бы со скоростью диска
                next_t += file_dt
                self._halt.wait(max(0.0, next_t - time.time()))
            now = time.time()
            if now - last_proc < interval:
                continue
            ok, frame = cap.retrieve()
            if not ok or frame is None:
                self._dead(now, timeout, "нет кадров")
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
            dt = now - last_proc if last_proc else interval
            last_proc = now
            self._last_good = now
            # детектор зависшей картинки: живой поток всегда чуть «дышит» (шум матрицы)
            if last_gray is not None and last_gray.shape == gray.shape:
                if float(cv2.absdiff(last_gray, gray).mean()) < cfg["freeze_eps"]:
                    freeze_time += dt
                else:
                    freeze_time = 0.0
            last_gray = gray
            # серая каша: результаты не берём, держим последнее состояние; если затянулось — как мёртвая камера
            cr = cfg["corrupt_ratio"]
            if cr > 0 and garbage_ratio(gray) >= cr:
                if not bad_since:
                    bad_since = now
                    log.warning("[%s] битый кадр (серая каша) — результаты не учитываются", self.id)
                bad_until = now + cfg["corrupt_hold_s"]
            elif bad_since and now >= bad_until:
                log.info("[%s] кадры в норме (каша %.1f с)", self.id, now - bad_since)
                bad_since = 0.0
            if bad_since:
                was_bad = True
                if now - bad_since >= timeout:
                    frozen = True              # после выхода из каши — состояния с нуля (как после фриза)
                    self._publish(self.engine.dead_state(now, "битый поток"), frame, True)
                else:
                    self._publish_held(now, frame)
                continue
            if was_bad:                        # после каши первый кадр сравнивать не с чем — буфер с нуля, гистерезис не трогаем
                was_bad = False
                self.engine.buf.clear()
            now_frozen = freeze_time >= timeout
            if now_frozen:
                frozen = True
                self._publish(self.engine.dead_state(now, "картинка застыла"), frame, True)
                continue
            if frozen:                         # камера ожила — состояния с нуля
                frozen = False
                self.engine.reset()
            st = self.engine.process(gray, now)
            if st is not None:
                self._publish(st, frame, True)
            else:
                with self._lock:
                    self._frame = frame
        if cap is not None:
            cap.release()

    def _note_conn(self, ok: bool, err: str):
        """Состояние подключения + запись в журнал ТОЛЬКО при смене (мёртвая камера не засоряет журнал каждую попытку)."""
        if ok != self._connected or (not ok and self._conn_logged is None):
            src = self.cfg["url"] if not is_stream_url(self.cfg["url"]) else self.cfg["url"].split("@")[-1]
            if ok:
                log.info("[%s] источник открыт: %s", self.id, src)
            else:
                log.warning("[%s] %s: %s", self.id, err, src)
        self._conn_logged = ok
        self._connected, self._error = ok, err

    def _dead(self, now: float, timeout: float, reason: str):
        """Источника нет: после freeze_timeout_s без кадров выдаём нули и status=0."""
        if self._last_good and now - self._last_good < timeout:
            return
        self._publish(self.engine.dead_state(now, reason), None, False)
