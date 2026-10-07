"""camera_core.rtsp_worker — RtspCameraWorker: RTSP-камера (фоновый захват, цифровой зум, управление Dahua).

Часть бывшего camera_core.py (механический перенос, тела определений не менялись).
"""
import threading
import time
import cv2
from logger import log_event
import dahua_control

from .utils import DEFAULT_VIDEO_FPS, RTSP_RECONNECT_BACKOFF, RTSP_RECONNECT_LOG_EVERY, RTSP_STALL_SECONDS
from .base_worker import BaseCameraWorker


class RtspCameraWorker(BaseCameraWorker):
    """IP-камера по RTSP (например, Dahua). Просмотр, запись видео и снимки.

    Захват РАЗВЯЗАН с отдачей MJPEG: кадры читает фоновый поток `_capture_loop`,
    а `generate()` только раздаёт последний готовый JPEG подключённым зрителям.
    Так автосохранение продолжает писать при закрытой вкладке браузера и может
    подниматься само при старте приложения (autostart), а несколько зрителей одной
    камеры используют ОДНО подключение к ней вместо своего на каждого.
    """

    def __init__(self, serial_number, manager, rtsp_url):
        super().__init__(serial_number, manager)

        self.rtsp_url = rtsp_url
        self.capture = None
        # последний полученный кадр — для снимка без повторного подключения
        self.last_frame = None
        # цифровой зум (кроп + растяжение): 1.0 = без зума, до 4.0
        self.zoom_factor = 1.0
        # положение окна кропа (панорамирование): 0.5,0.5 = центр; 0,0 = левый верх; 1,1 = правый низ
        self.zoom_pan_x = 0.5
        self.zoom_pan_y = 0.5
        # кэш возможностей камеры (белый свет / оптический зум), заполняется по запросу
        self._caps = None

        # --- фоновый захват ---
        self._capture_thread = None
        # сигнал «остановиться немедленно» (force_close / выход приложения)
        self._loop_stop = threading.Event()
        # сериализует запуск/остановку фонового потока
        self._loop_lock = threading.Lock()
        # последний готовый JPEG + счётчик кадров: зрители ждут смены seq
        self._frame_cond = threading.Condition()
        self._latest_jpeg = None
        self._latest_seq = 0
        # сколько браузеров сейчас смотрит поток
        self._viewers = 0
        # пользователь нажал «Остановить» — просмотр больше не держит цикл
        self._stop_requested = False
        # поднимать камеру при старте приложения (галочка в списке сохранённых)
        self.autostart = False
        # идёт переподключение к камере (показываем бейджем в UI)
        self.reconnecting = False
        self.reconnect_attempts = 0
        # сторож простоя: поток, время последнего кадра и флаг «о тишине уже сообщили»
        self._watchdog_thread = None
        self._last_frame_at = time.time()
        self._stall_reported = False
        # параметры отдачи: масштаб кадра (%) и целевой fps — задаёт подключающийся зритель
        self.stream_scale = 100
        self.target_fps = None

    # ---------- фоновый захват ----------

    # сколько браузеров смотрит поток прямо сейчас
    def viewer_count(self):
        return self._viewers

    # цикл нужен, пока есть зритель ИЛИ идёт автосохранение ИЛИ включён автостарт
    def _should_capture(self):
        if self._loop_stop.is_set():
            return False
        if self.save_photo or self.save_video == 1 or self.autostart:
            return True
        return self._viewers > 0 and not self._stop_requested

    # поднять фоновый захват (если он ещё не идёт) и запомнить параметры отдачи
    def ensure_capture(self, scale=None, target_fps=None):
        if scale is not None:
            try:
                self.stream_scale = max(10, min(100, int(scale)))
            except (TypeError, ValueError):
                self.stream_scale = 100
        if target_fps is not None:
            try:
                value = float(target_fps)
                self.target_fps = value if value > 0 else None
            except (TypeError, ValueError):
                self.target_fps = None

        with self._loop_lock:
            self._stop_requested = False
            self._loop_stop.clear()
            thread = self._capture_thread
            if thread is not None and thread.is_alive():
                return False

            self._capture_thread = threading.Thread(
                target=self._capture_loop,
                name=f"rtsp-capture-{self.serial_number}",
                daemon=True,
            )
            self._capture_thread.start()
            log_event("camera_core.rtsp_capture", "Запущен фоновый захват RTSP", "info",
                      {"serial_number": self.serial_number, "rtsp_url": self.rtsp_url,
                       "scale": self.stream_scale, "target_fps": self.target_fps,
                       "autostart": self.autostart})
            return True

    # Сторож простоя. Нужен отдельным потоком, потому что `capture.read()` в цикле
    # захвата БЛОКИРУЕТСЯ внутри FFmpeg (на мёртвой камере — десятки секунд) и цикл
    # физически не может ни заметить тишину, ни сообщить о ней. Сторож не читает
    # кадры, поэтому видит обрыв ровно через RTSP_STALL_SECONDS и сразу поднимает
    # флаг «переподключение» и событие в журнал — не дожидаясь возврата read().
    def _start_watchdog(self):
        thread = self._watchdog_thread
        if thread is not None and thread.is_alive():
            return
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            name=f"rtsp-watchdog-{self.serial_number}",
            daemon=True,
        )
        self._watchdog_thread.start()

    def _watchdog_loop(self):
        while self._should_capture():
            time.sleep(0.5)
            if not self.running:
                continue  # цикл сам знает, что переподключается (пауза бэкоффа)

            silence = time.time() - self._last_frame_at
            if silence < RTSP_STALL_SECONDS or self._stall_reported:
                continue

            self._stall_reported = True
            self.reconnecting = True
            log_event("camera_core.rtsp_stream", "Нет кадров с RTSP — камера не отвечает", "warn",
                      {"serial_number": self.serial_number, "rtsp_url": self.rtsp_url,
                       "silence_seconds": round(silence, 1),
                       "hint": "ждём таймаут чтения FFmpeg, затем переподключение"})

        with self._loop_lock:
            if self._watchdog_thread is threading.current_thread():
                self._watchdog_thread = None

    # пауза «нарезанная»: реагируем на стоп/уход зрителей почти мгновенно
    def _sleep_sliced(self, seconds):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if not self._should_capture():
                return False
            time.sleep(0.2)
        return True

    # выложить готовый кадр зрителям
    def _publish(self, frame):
        with self._frame_cond:
            self._latest_jpeg = frame
            self._latest_seq += 1
            self._frame_cond.notify_all()

    def _backoff_delay(self, attempt):
        index = min(attempt, len(RTSP_RECONNECT_BACKOFF)) - 1
        return RTSP_RECONNECT_BACKOFF[max(0, index)]

    # основной цикл: подключение -> чтение кадров -> автосохранение -> публикация.
    # Обрыв связи (нет кадров дольше RTSP_STALL_SECONDS) не убивает цикл: камера
    # переоткрывается с нарастающей паузой, состояние автосохранения сохраняется.
    def _capture_loop(self):
        capture = None
        attempt = 0
        last_frame_time = None
        last_emit = 0.0
        source_fps = DEFAULT_VIDEO_FPS
        self._last_frame_at = time.time()
        self._start_watchdog()

        try:
            while self._should_capture():
                # --- (пере)подключение ---
                if capture is None:
                    self.reconnecting = attempt > 0
                    capture = self._open_capture()
                    if not capture.isOpened():
                        try:
                            capture.release()
                        except Exception:
                            pass
                        capture = None
                        attempt += 1
                        self.reconnect_attempts = attempt
                        if attempt == 1 or attempt % RTSP_RECONNECT_LOG_EVERY == 0:
                            log_event("camera_core.rtsp_stream", "Не удалось подключиться к RTSP, пробуем снова",
                                      "warn", {"serial_number": self.serial_number,
                                               "rtsp_url": self.rtsp_url, "attempt": attempt,
                                               "retry_in": self._backoff_delay(attempt)})
                        self._sleep_sliced(self._backoff_delay(attempt))
                        continue

                    self.capture = capture
                    self.running = True
                    source_fps = self._capture_fps(capture)
                    self._last_frame_at = time.time()
                    self._stall_reported = False
                    if attempt:
                        log_event("camera_core.rtsp_stream", "RTSP-поток восстановлен после обрыва", "success",
                                  {"serial_number": self.serial_number, "attempts": attempt})
                    else:
                        log_event("camera_core.rtsp_stream", "RTSP-поток запущен", "success",
                                  {"serial_number": self.serial_number, "fps": source_fps})
                    attempt = 0
                    self.reconnect_attempts = 0
                    self.reconnecting = False

                # --- чтение кадра ---
                ok, raw = capture.read()

                if not ok or raw is None:
                    self.metrics["errors"] += 1
                    # тишина дольше порога = реальный обрыв -> переоткрываем камеру
                    if time.time() - self._last_frame_at >= RTSP_STALL_SECONDS:
                        attempt += 1
                        self.reconnect_attempts = attempt
                        self.reconnecting = True
                        if attempt == 1 or attempt % RTSP_RECONNECT_LOG_EVERY == 0:
                            log_event("camera_core.rtsp_stream", "Обрыв RTSP-потока, переподключение", "warn",
                                      {"serial_number": self.serial_number, "attempt": attempt,
                                       "retry_in": self._backoff_delay(attempt),
                                       "stall_seconds": round(time.time() - self._last_frame_at, 1)})
                        try:
                            capture.release()
                        except Exception:
                            pass
                        if self.capture is capture:
                            self.capture = None
                        capture = None
                        self.running = False
                        self._sleep_sliced(self._backoff_delay(attempt))
                    else:
                        time.sleep(0.05)
                    continue

                self._last_frame_at = time.time()
                # кадры снова пошли после «тишины» — гасим тревогу сторожа
                if self._stall_reported:
                    self._stall_reported = False
                    self.reconnecting = False
                    log_event("camera_core.rtsp_stream", "Кадры RTSP снова идут", "success",
                              {"serial_number": self.serial_number})
                # полноразмерный кадр держим для снимка
                self.last_frame = raw

                now = time.time()
                # троттлинг отдачи/записи: читаем всё (иначе растёт буфер), но
                # кодируем и сохраняем не чаще целевого fps
                min_interval = (1.0 / self.target_fps) if self.target_fps else 0.0
                if min_interval and (now - last_emit) < min_interval:
                    continue
                last_emit = now

                # цифровой зум (кроп + растяжение), затем сетевой масштаб
                zoomed = self._apply_digital_zoom(raw)

                scale_factor = self.stream_scale / 100.0
                if scale_factor < 1.0:
                    new_w = max(2, int(zoomed.shape[1] * scale_factor))
                    new_h = max(2, int(zoomed.shape[0] * scale_factor))
                    img = cv2.resize(zoomed, (new_w, new_h), interpolation=cv2.INTER_AREA)
                else:
                    img = zoomed

                ok_jpeg, encoded = cv2.imencode(".jpg", img)
                if not ok_jpeg:
                    self.metrics["errors"] += 1
                    continue
                frame = encoded.tobytes()

                self.metrics["image_number"] += 1
                self.metrics["width"] = img.shape[1]
                self.metrics["height"] = img.shape[0]

                if last_frame_time is not None:
                    dt = now - last_frame_time
                    if dt > 0:
                        self.metrics["fps"] = 1.0 / dt

                if self.metrics["fps"] > 0:
                    self.metrics["bandwidth_mbps"] = (len(frame) * 8 * self.metrics["fps"]) / 1_000_000

                last_frame_time = now

                # автофото + запись видео (тот же механизм, что и у GigE)
                self._maybe_save(img, self.target_fps or source_fps)

                self._publish(frame)

        except Exception as e:
            log_event("camera_core.rtsp_stream", "Ошибка фонового захвата RTSP", "error",
                      {"serial_number": self.serial_number, "error": repr(e)})
            self.metrics["errors"] += 1

        finally:
            self.running = False
            self.reconnecting = False
            self._stall_reported = False

            if capture is not None:
                try:
                    capture.release()
                except Exception:
                    pass
            if self.capture is capture:
                self.capture = None

            with self._loop_lock:
                if self._capture_thread is threading.current_thread():
                    self._capture_thread = None

            # будим зрителей, чтобы их генераторы вышли, а не висели на ожидании кадра
            self._publish(None)

            log_event("camera_core.rtsp_stream", "Фоновый захват RTSP остановлен", "info",
                      {"serial_number": self.serial_number, "photo": self.save_photo,
                       "video": self.save_video, "autostart": self.autostart})

    def _open_capture(self):
        capture = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
        try:
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        return capture

    def _capture_fps(self, capture):
        try:
            fps = capture.get(cv2.CAP_PROP_FPS)
        except Exception:
            fps = 0
        return fps if fps and fps > 0 else DEFAULT_VIDEO_FPS

    # ---------- стрим ----------

    # Отдача MJPEG. Камеру НЕ открывает: поднимает фоновый захват (если не идёт) и
    # раздаёт последний готовый кадр, пока браузер подключён. Первый кадр ждём
    # FIRST_FRAME_TIMEOUT — за это время камера успевает подключиться.
    FIRST_FRAME_TIMEOUT = 15.0

    def generate(self, scale=100, target_fps=None):
        log_event("camera_core.rtsp_stream", "Запрошена отдача RTSP-потока", "info",
                  {"serial_number": self.serial_number, "rtsp_url": self.rtsp_url,
                   "scale": scale, "target_fps": target_fps})

        with self._frame_cond:
            self._viewers += 1
            viewers = self._viewers
        self.ensure_capture(scale=scale, target_fps=target_fps)

        seen_seq = 0
        got_first = False
        deadline = time.time() + self.FIRST_FRAME_TIMEOUT

        try:
            while True:
                with self._frame_cond:
                    # ждём кадр НОВЕЕ отданного (иначе гоняли бы один и тот же по кругу)
                    while self._latest_seq == seen_seq and self._latest_jpeg is not None:
                        if not self._frame_cond.wait(timeout=1.0):
                            break
                    frame = self._latest_jpeg
                    seen_seq = self._latest_seq

                if frame is None:
                    # захват ещё поднимается (или уже остановлен) — ждём первый кадр,
                    # после потери связи держим соединение, пока цикл переподключается
                    if not got_first and time.time() > deadline:
                        log_event("camera_core.rtsp_stream", "Первый кадр RTSP не получен за таймаут", "error",
                                  {"serial_number": self.serial_number, "rtsp_url": self.rtsp_url})
                        return
                    if got_first and not self._should_capture():
                        return
                    time.sleep(0.1)
                    continue

                got_first = True

                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                )
        finally:
            with self._frame_cond:
                self._viewers = max(0, self._viewers - 1)
                left = self._viewers
            log_event("camera_core.rtsp_stream", "Зритель RTSP отключился", "info",
                      {"serial_number": self.serial_number, "viewers": left,
                       "photo": self.save_photo, "video": self.save_video})
            # состояние автосохранения НЕ сбрасываем: запись продолжается без зрителей
            # (её глушит только off_photo/off_video или force_close)

    # мягкая остановка: просмотр больше не держит захват. Если идёт автосохранение
    # или включён автостарт — цикл продолжает работать (в этом весь смысл развязки).
    def close(self):
        self._stop_requested = True
        keep = self.save_photo or self.save_video == 1 or self.autostart
        log_event("camera_core.rtsp_stream", "Запрошена мягкая остановка RTSP-потока", "info",
                  {"serial_number": self.serial_number, "keep_capture": bool(keep)})
        return {"status": "kept_alive" if keep else "stopping", "capture_kept": bool(keep)}

    # ---------- снимок ----------

    # сохранить отдельный снимок и вернуть его как jpeg
    def snapshot(self):
        # берём живой кадр и СРАЗУ копируем: generate() в другом потоке параллельно
        # перезаписывает self.last_frame, а cv2 может переиспользовать буфер —
        # без копии imencode получил бы кадр, меняющийся под ним.
        img = self.last_frame
        if img is not None:
            img = img.copy()
        opened = None
        try:
            if img is None:
                opened = self._open_capture()
                if not opened.isOpened():
                    log_event("camera_core.rtsp_snapshot", "Не удалось подключиться к RTSP для снимка", "error", {"rtsp_url": self.rtsp_url})
                    return None
                ok, img = opened.read()
                if not ok or img is None:
                    log_event("camera_core.rtsp_snapshot", "Не удалось получить кадр для снимка", "error")
                    return None

            # снимок отражает то же, что видно в потоке (с учётом цифрового зума)
            img = self._apply_digital_zoom(img)
            self.write_photo(img)

            ok_jpeg, encoded = cv2.imencode(".jpg", img)
            if not ok_jpeg:
                log_event("camera_core.rtsp_snapshot", "Ошибка кодирования снимка", "error")
                return None

            log_event("camera_core.rtsp_snapshot", "Снимок сохранён", "success", {"serial_number": self.serial_number})
            return encoded.tobytes()
        finally:
            if opened is not None:
                opened.release()

    # принудительная остановка: гасим фоновый захват целиком и снимаем автосохранение —
    # это осознанное «выключить всё», в отличие от close() (просто ушёл зритель)
    def force_close(self):
        # _loop_stop сильнее всех признаков в _should_capture (включая autostart);
        # сам флаг autostart не трогаем — он живёт в базе сохранённых камер
        self._loop_stop.set()
        self._stop_requested = True
        self.running = False

        if self.capture is not None:
            try:
                self.capture.release()
            except Exception as e:
                log_event("camera_core.rtsp_force", "Ошибка release RTSP", "warn", {"error": str(e)})

            self.capture = None

        # будим зрителей: их генераторы должны выйти, а не висеть на ожидании кадра
        self._publish(None)
        self._reset_save_state()

        log_event("camera_core.rtsp_force", "Запрошена принудительная остановка RTSP-потока", "warn",
                  {"serial_number": self.serial_number})
        return {"status": "force_stopped"}

    # состояние потока + признаки фонового захвата (для бейджа «переподключение…» в UI)
    def stream_state(self):
        return {
            "serial_number": self.serial_number,
            "running": self.running,
            "closed": not self.running,
            "capture_active": self._capture_thread is not None and self._capture_thread.is_alive(),
            "reconnecting": self.reconnecting,
            "viewers": self._viewers,
            "autostart": self.autostart,
        }

    # ---------- цифровой зум ----------

    def _apply_digital_zoom(self, frame):
        """Кроп области 1/factor (с учётом панорамирования) и растяжение к размеру кадра."""
        factor = self.zoom_factor
        if not factor or factor <= 1.0:
            return frame
        h, w = frame.shape[:2]
        crop_w = max(2, int(w / factor))
        crop_h = max(2, int(h / factor))
        # положение окна: pan 0..1 отображается в диапазон [0 .. (размер - кроп)]
        x0 = int(round((w - crop_w) * self.zoom_pan_x))
        y0 = int(round((h - crop_h) * self.zoom_pan_y))
        x0 = max(0, min(w - crop_w, x0))
        y0 = max(0, min(h - crop_h, y0))
        crop = frame[y0:y0 + crop_h, x0:x0 + crop_w]
        return cv2.resize(crop, (w, h), interpolation=cv2.INTER_LINEAR)

    def set_zoom(self, factor=None, pan_x=None, pan_y=None):
        """Задать кратность цифрового зума (1.0..4.0) и/или положение окна (pan 0..1).

        При смене кратности окно возвращается в центр. Панорамирование (pan_x/pan_y)
        можно менять отдельно, не трогая кратность. Читается потоком на лету.
        """
        if factor is not None:
            try:
                factor = float(factor)
            except (TypeError, ValueError):
                return {"error": "bad_factor"}
            self.zoom_factor = max(1.0, min(4.0, factor))
            # новая кратность — сбрасываем вид в центр
            self.zoom_pan_x = 0.5
            self.zoom_pan_y = 0.5
        if pan_x is not None:
            try:
                self.zoom_pan_x = max(0.0, min(1.0, float(pan_x)))
            except (TypeError, ValueError):
                pass
        if pan_y is not None:
            try:
                self.zoom_pan_y = max(0.0, min(1.0, float(pan_y)))
            except (TypeError, ValueError):
                pass
        log_event("camera_core.rtsp_zoom", "Цифровой зум", "info",
                  {"serial_number": self.serial_number, "factor": self.zoom_factor,
                   "pan_x": self.zoom_pan_x, "pan_y": self.zoom_pan_y})
        return {"status": "ok", "mode": "digital", "factor": self.zoom_factor,
                "pan_x": self.zoom_pan_x, "pan_y": self.zoom_pan_y}

    # ---------- управление камерой (Dahua CGI) ----------

    def _dahua_creds(self):
        return dahua_control.parse_rtsp_credentials(self.rtsp_url)

    def get_capabilities(self, refresh=False):
        """Возможности камеры (белый свет / оптический зум).

        Статичная часть (свет/оптика/модель) кэшируется; динамическая (текущий зум и
        положение окна) добавляется всегда свежей — иначе UI при переоткрытии видит
        устаревшую кратность из кэша.
        """
        if self._caps is None or refresh:
            host, user, password = self._dahua_creds()
            caps = dahua_control.get_capabilities(host, user, password)
            caps["digital_zoom"] = True  # цифровой зум делаем у себя — доступен всегда
            self._caps = caps
        # динамические поля — всегда актуальные, не из кэша
        result = dict(self._caps)
        result["zoom_factor"] = self.zoom_factor
        result["zoom_pan_x"] = self.zoom_pan_x
        result["zoom_pan_y"] = self.zoom_pan_y
        return result

    def set_light(self, on, brightness=100):
        """Включить/выключить белый прожектор камеры."""
        host, user, password = self._dahua_creds()
        if not host:
            return {"error": "no_host"}
        ok = dahua_control.set_white_light(host, user, password, on, brightness)
        return {"status": "ok" if ok else "failed", "on": bool(on)}

    def get_light(self):
        """Текущее состояние белого прожектора: on | off | auto | None."""
        host, user, password = self._dahua_creds()
        if not host:
            return {"error": "no_host"}
        return {"state": dahua_control.get_white_light(host, user, password)}

    def optical_zoom(self, direction, speed=1):
        """Оптический зум камеры (если поддерживается). direction: tele|wide|stop; speed 1..8."""
        host, user, password = self._dahua_creds()
        if not host:
            return {"error": "no_host"}
        ok = dahua_control.optical_zoom(host, user, password, direction, speed)
        return {"status": "ok" if ok else "failed", "direction": direction, "mode": "optical"}

    def focus(self, direction):
        """Ручной фокус камеры (относительно). direction: near|far|stop."""
        host, user, password = self._dahua_creds()
        if not host:
            return {"error": "no_host"}
        ok = dahua_control.focus(host, user, password, direction)
        return {"status": "ok" if ok else "failed", "direction": direction}

    def set_focus_abs(self, value):
        """Абсолютный фокус (0.0..1.0) — для ползунка."""
        host, user, password = self._dahua_creds()
        if not host:
            return {"error": "no_host"}
        ok = dahua_control.set_focus_abs(host, user, password, value)
        return {"status": "ok" if ok else "failed", "focus": value}

    def lens_status(self):
        """Текущие позиции зума/фокуса (0..1) — для инициализации ползунков."""
        host, user, password = self._dahua_creds()
        if not host:
            return {"error": "no_host"}
        return dahua_control.get_lens_status(host, user, password)

    def auto_focus(self):
        """Разовый автофокус (наведение резкости)."""
        host, user, password = self._dahua_creds()
        if not host:
            return {"error": "no_host"}
        ok = dahua_control.auto_focus(host, user, password)
        return {"status": "ok" if ok else "failed"}

    # ---------- настройки изображения (экспозиция / баланс белого / день-ночь) ----------

    def get_image_settings(self):
        """Текущие настройки изображения камеры (экспозиция/ББ/день-ночь)."""
        host, user, password = self._dahua_creds()
        if not host:
            return {"error": "no_host"}
        return dahua_control.get_image_settings(host, user, password)

    def set_exposure(self, compensation=None, gain_min=None, gain_max=None):
        """Экспозиция (авто): компенсация и пределы усиления. Пишется во все профили."""
        host, user, password = self._dahua_creds()
        if not host:
            return {"error": "no_host"}
        return dahua_control.set_exposure(host, user, password,
                                          compensation, gain_min, gain_max)

    def set_white_balance(self, mode):
        """Баланс белого: пресет (Auto/Sunny/…). Пишется во все профили."""
        host, user, password = self._dahua_creds()
        if not host:
            return {"error": "no_host"}
        return dahua_control.set_white_balance(host, user, password, mode)

    def set_day_night(self, mode):
        """Режим день/ночь: Color | BlackWhite. Пишется во все профили."""
        host, user, password = self._dahua_creds()
        if not host:
            return {"error": "no_host"}
        return dahua_control.set_day_night(host, user, password, mode)

    # ---------- сетевые настройки (смена IP камеры) ----------

    def get_network(self):
        """Текущие сетевые настройки камеры (IP/маска/шлюз/DHCP)."""
        host, user, password = self._dahua_creds()
        if not host:
            return {"error": "no_host"}
        return dahua_control.get_network(host, user, password)

    def set_network(self, ip=None, mask=None, gateway=None, dhcp=None):
        """Сменить сетевые настройки камеры. См. dahua_control.set_network."""
        host, user, password = self._dahua_creds()
        if not host:
            return {"ok": False, "error": "no_host"}
        return dahua_control.set_network(host, user, password,
                                         ip=ip, mask=mask, gateway=gateway, dhcp=dhcp)
