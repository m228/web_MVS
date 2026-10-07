"""camera_core.base_worker — BaseCameraWorker: общее состояние и сохранение фото/видео для всех камер.

Часть бывшего camera_core.py (механический перенос, тела определений не менялись).
"""
from datetime import datetime
import os
import re
import shutil
import time
from pathlib import Path
import cv2
from logger import log_event
from paths import BUNDLE_DIR, DATA_DIR
import save_settings
import plate_config

from .utils import DEFAULT_VIDEO_FPS, DISK_CHECK_PERIOD, DISK_FREE_WARN_MB, PHOTO_FORMATS, PNG_COMPRESSION


class BaseCameraWorker:
    """Общее состояние и механизмы сохранения (фото/видео) для всех типов камер."""

    def __init__(self, serial_number, manager):
        self.serial_number = serial_number
        self.manager = manager

        self.running = False

        self.save_photo = False
        self.photo_interval = None
        self.last_photo = None
        # snap_once — «одиночный снимок по триггеру»: сохранить СЛЕДУЮЩИЙ кадр как фото и сбросить
        # флаг (не трогая интервальное автосохранение). Это soft-триггер для авто-цикла микроскопа.
        self.snap_once = False
        # куда уходит снимок snap_once: на диск (_snap_save) и/или в колбэк _snap_sink(img) —
        # кадр в память без файла (проба CV: кадр идёт в модель, сырой файл не пишем)
        self._snap_save = True
        self._snap_sink = None
        # имя проекта для фото: задаёт папку dataset/<проект>/<камера> и префикс имени файла
        self.photo_project = None
        # суффикс в конец имени скрина (напр. стадия варки «_st11»); ставит microscope_service
        self.photo_suffix = ""
        # формат файлов фото: всегда PNG без сжатия (см. _photo_ext / PNG_COMPRESSION)
        self.photo_format = "png"
        # сколько фото сохранено за текущую сессию автосохранения
        self.photo_saved_count = 0

        # --- здоровье автосохранения (см. photo_status): «включено, но не пишется» ---
        # когда включили автосохранение и когда последний раз реально записали файл
        self.photo_enabled_at = None
        self.last_photo_saved_at = None
        # текст последней ошибки записи (None — ошибок не было)
        self.last_save_error = None
        # предупреждение о свободном месте на диске (None — места достаточно)
        self.disk_warning = None
        self._disk_checked_at = 0.0
        # последнее состояние здоровья — чтобы писать событие ОДИН раз на переход
        self._photo_health_state = None

        # хостовая цветокоррекция (гамма/насыщ/оттенок/контраст/яркость/резкость/чёткость/
        # шум/CCM/палитра). пусто = без изменений; применяется в get_frame после _to_bgr.
        # Хранится в plate_config.camera_color (входит в «Дамп»), применяется к микроскопной
        # камере (serial == camera_serial). После перезапуска картинка сразу та же.
        self.color = {}
        # последний СЫРОЙ BGR-кадр (до цветокоррекции) — для метрики резкости автофокуса
        self._last_bgr = None
        try:
            pcfg = plate_config.load()
            if serial_number and serial_number == pcfg.get("camera_serial"):
                cc = pcfg.get("camera_color")
                if isinstance(cc, dict):
                    self.color = dict(cc)
        except Exception:
            self.color = {}

        # 0 нет автосохранения видео / 1 идёт / 2 завершение
        self.save_video = 0
        self.video_duration = None
        # имя проекта для видео (аналогично photo_project)
        self.video_project = None
        self.video_start = None
        self.video_writer = None

        self.metrics = {
            "fps": 0.0,
            "image_number": 0,
            "bandwidth_mbps": 0.0,
            "width": 0,
            "height": 0,
            "errors": 0,
        }

    # ---------- состояние потока ----------

    def stream_state(self):
        return {
            "serial_number": self.serial_number,
            "running": self.running,
            "closed": not self.running,
        }

    def close(self):
        self.running = False
        log_event("camera_core.close_stream", "Запрошена мягкая остановка потока")
        return {"status": "stopping"}

    # ---------- пути сохранения ----------

    # безопасное имя камеры для имён файлов (серийник без спецсимволов)
    def _camera_tag(self):
        tag = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(self.serial_number or "camera")).strip("_")
        return tag or "camera"

    # безопасное имя проекта для путей/файлов (кириллица допустима, режем только опасное).
    # Пустое имя -> None: тогда откат к прежнему поведению (без подпапки проекта).
    @staticmethod
    def _project_tag(name):
        if not name:
            return None
        # убираем разделители путей и служебные символы, пробелы оставляем как есть
        tag = re.sub(r'[\\/:*?"<>|]+', "_", str(name)).strip().strip(".")
        return tag or None

    # папка сохранения фото: <данные>/dataset/<проект>/<серийник> (проект задаёт группу).
    # Без проекта — прежний путь <данные>/dataset/<серийник>.
    # DATA_DIR — каталог пользовательских данных, переживающий обновление (см. paths.py)
    def photo_dir(self):
        project = self._project_tag(self.photo_project)
        base = DATA_DIR / "dataset"
        return base / project / self._camera_tag() if project else base / self._camera_tag()

    # папка сохранения видео: <данные>/Videos/<проект>/<серийник>
    def video_dir(self):
        project = self._project_tag(self.video_project)
        base = DATA_DIR / "Videos"
        return base / project / self._camera_tag() if project else base / self._camera_tag()

    # префикс имени файла: имя проекта, иначе прежнее значение по умолчанию
    def _photo_prefix(self):
        return self._project_tag(self.photo_project) or "frame"

    def _video_prefix(self):
        return self._project_tag(self.video_project) or "video"

    # расширение файла фото по выбранному формату (png по умолчанию, всегда одно из PHOTO_FORMATS)
    def _photo_ext(self):
        return "jpg" if self.photo_format == "jpg" else "png"

    # папка и шаблон имени файлов фото — показываем во фронтенде
    def photo_save_info(self):
        return {"dir": str(self.photo_dir().resolve()),
                "pattern": f"{self._photo_prefix()}_<дата>.{self._photo_ext()}"}

    # папка и шаблон имени файлов видео — показываем во фронтенде
    def video_save_info(self):
        return {"dir": str(self.video_dir().resolve()), "pattern": f"{self._video_prefix()}_<дата>.avi"}

    # ---------- фото ----------

    # длительность текущей записи видео в секундах (0 если не пишем)
    def video_elapsed(self):
        if self.save_video == 1 and self.video_start:
            return int(time.time() - self.video_start)
        return 0

    def on_photo(self, interval, project=None, photo_format=None):
        self.save_photo = True
        self.photo_interval = interval
        # имя проекта задаёт папку/имя файла; пустое -> прежнее поведение
        self.photo_project = project or None
        # формат кадра: неизвестное значение игнорируем (остаётся прежний)
        if photo_format in PHOTO_FORMATS:
            self.photo_format = photo_format
        # новая сессия автосохранения — обнуляем счётчик и состояние здоровья
        self.photo_saved_count = 0
        self.photo_enabled_at = time.time()
        self.last_photo_saved_at = None
        self.last_save_error = None
        self._photo_health_state = None

        # запоминаем выбор, чтобы подтянуть при следующем открытии.
        # photo_autostart=True — чтобы после перезапуска приложения запись возобновилась сама
        save_settings.update(self.serial_number, photo_project=self.photo_project,
                             photo_interval=interval, photo_format=self.photo_format,
                             photo_autostart=True)

        # свободное место проверяем СРАЗУ при включении: бессмысленно узнавать о нехватке
        # через час, когда датасет уже не пишется
        disk = self.check_disk_space(force=True)

        info = self.photo_save_info()
        log_event("camera_core.on_save", "Вкл. автосохранение фото c интервалом", "info",
                  {"interval": interval, "project": self.photo_project,
                   "photo_format": self.photo_format,
                   "save_dir": info["dir"], "file_pattern": info["pattern"],
                   "disk_free_mb": disk.get("free_mb"), "disk_warning": disk.get("warning")})
        return {"status": "ok", "photo_enabled": True, "interval": self.photo_interval,
                "project": self.photo_project, "photo_format": self.photo_format,
                "save_dir": info["dir"], "file_pattern": info["pattern"],
                "disk_free_mb": disk.get("free_mb"), "disk_warning": disk.get("warning")}

    def off_photo(self):
        self.save_photo = False
        self.photo_interval = None
        self.last_photo = None
        self.photo_project = None
        self.photo_enabled_at = None
        self._photo_health_state = None

        # снимаем флаг возобновления: пользователь выключил запись осознанно,
        # после перезапуска её поднимать не нужно
        save_settings.update(self.serial_number, photo_autostart=False)

        log_event("camera_core.off_save", "Выкл. автосохранение фото")
        return {"status": "ok", "photo_enabled": False}

    # ---------- здоровье автосохранения ----------

    # свободное место в каталоге данных. Дёргаем не чаще DISK_CHECK_PERIOD (диск опрашивать
    # на каждый кадр незачем), force — при включении автосохранения.
    def check_disk_space(self, force=False):
        now = time.time()
        if not force and (now - self._disk_checked_at) < DISK_CHECK_PERIOD:
            return {"free_mb": None, "warning": self.disk_warning}

        self._disk_checked_at = now
        try:
            free_mb = int(shutil.disk_usage(DATA_DIR).free / (1024 * 1024))
        except Exception as e:
            self.disk_warning = None
            return {"free_mb": None, "warning": None, "error": str(e)}

        if free_mb < DISK_FREE_WARN_MB:
            self.disk_warning = f"мало места на диске: свободно {free_mb} МБ"
        else:
            self.disk_warning = None
        return {"free_mb": free_mb, "warning": self.disk_warning}

    # статус автосохранения фото: off | ok | stalled (+ причина).
    # stalled — «включено, но не пишется»: нового файла нет дольше двух интервалов.
    def photo_status(self):
        if not self.save_photo:
            self._photo_health_state = None
            return {"photo_health": "off", "photo_error": None}

        self.check_disk_space()

        interval = self.photo_interval or 0
        # порог: два интервала, но не меньше интервал+5 c (короткие интервалы дают
        # ложные срабатывания на старте: камера ещё подключается)
        limit = max(2 * interval, interval + 5) if interval else 30
        reference = self.last_photo_saved_at or self.photo_enabled_at or time.time()
        overdue = (time.time() - reference) > limit

        if not overdue:
            health, reason = "ok", None
        elif self.last_save_error:
            health, reason = "stalled", f"ошибка записи: {self.last_save_error}"
        elif self.disk_warning:
            health, reason = "stalled", self.disk_warning
        elif not self.running:
            health, reason = "stalled", "поток не запущен — нет кадров"
        else:
            health, reason = "stalled", "нет кадров с камеры"

        # событие пишем один раз на переход состояния, а не каждую секунду опроса
        if health != self._photo_health_state:
            self._photo_health_state = health
            if health == "stalled":
                log_event("camera_core.save_health", "Автосохранение фото включено, но файлы не пишутся",
                          "error", {"serial_number": self.serial_number, "reason": reason,
                                    "interval": interval, "photo_count": self.photo_saved_count})
            else:
                log_event("camera_core.save_health", "Автосохранение фото пишет нормально", "success",
                          {"serial_number": self.serial_number, "photo_count": self.photo_saved_count})

        return {"photo_health": health, "photo_error": reason}

    def _should_save_photo(self, interval):
        current_time = time.time()

        if interval is None:
            return False

        if self.last_photo is None:
            self.last_photo = current_time
            return True

        if current_time - self.last_photo >= interval:
            self.last_photo = current_time
            return True

        return False

    # путь: dataset/<проект>/<серийник>/<проект>_<дата-время>.<jpg|png>
    # Ошибку записи (нет прав/нет места/битый путь) не роняем в цикл стрима, а
    # запоминаем в last_save_error — её показывает индикатор здоровья (photo_status).
    #
    # ВАЖНО: пишем через cv2.imencode + Path.write_bytes, а НЕ cv2.imwrite.
    # cv2.imwrite отдаёт путь в ОС байтами UTF-8, а Windows читает их в ANSI-кодировке
    # (cp1251) -> на любом пути с кириллицей (а имя проекта у нас русское:
    # dataset/завод один/...) файл молча НЕ создаётся, imwrite возвращает False.
    # Именно из-за этого автосохранение «работало», но датасет оставался пустым.
    # Заодно так удобно передать параметры кодека (сжатие PNG).
    # log_name=True — записать в лог имя сохранённого файла («скрин сохранён: …»).
    # Для одиночных/триггерных скринов (snap) True; для потокового автосохранения
    # датасета — False (иначе лог спамится каждым интервалом; у него свой health-мониторинг).
    def write_photo(self, img, log_name=False):
        folder = self.photo_dir()
        ext = self._photo_ext()
        suffix = ("_" + str(self.photo_suffix)) if self.photo_suffix else ""
        filename = f"{self._photo_prefix()}_{datetime.now().strftime('%d_%m_%Y_%H_%M_%S')}{suffix}.{ext}"
        path = os.path.join(folder, filename)
        params = [cv2.IMWRITE_PNG_COMPRESSION, PNG_COMPRESSION] if ext == "png" else []

        try:
            folder.mkdir(parents=True, exist_ok=True)
            ok, encoded = cv2.imencode(f".{ext}", img, params)
            if not ok:
                raise OSError(f"не удалось закодировать кадр в {ext.upper()}")
            Path(path).write_bytes(encoded.tobytes())
        except Exception as e:
            self.last_save_error = str(e)
            log_event("camera_core.write_photo", "Не удалось сохранить фото", "error",
                      {"serial_number": self.serial_number, "path": path, "error": str(e)})
            return None

        self.photo_saved_count += 1
        self.last_photo_saved_at = time.time()
        self.last_save_error = None
        if log_name:
            log_event("camera_core.write_photo", f"Скрин сохранён: {filename}", "success",
                      {"serial_number": self.serial_number, "file": filename, "path": path})
        return path

    # ---------- видео ----------

    def on_video(self, duration, project=None):
        if duration is None:
            self.video_duration = None
        elif self.save_video == 0:
            self.video_duration = duration

        # имя проекта фиксируем только при старте новой записи (не посреди идущей)
        if self.save_video == 0:
            self.video_project = project or None
            self.save_video = 1
            self.video_start = time.time()

        # запоминаем выбор, чтобы подтянуть при следующем открытии
        save_settings.update(self.serial_number, video_project=self.video_project, video_duration=duration)

        info = self.video_save_info()
        log_event("camera_core.on_video", "Вкл. автосохранение видео с длительностью: ", "info",
                  {"video_duration": self.video_duration, "project": self.video_project,
                   "save_dir": info["dir"], "file_pattern": info["pattern"]})
        return {"status": "ok", "video_enabled": True, "project": self.video_project,
                "save_dir": info["dir"], "file_pattern": info["pattern"]}

    def off_video(self):
        if self.save_video == 1:
            self.save_video = 2
        log_event("camera_core.off_video", "Выкл. автосохранение видео")
        return {"status": "ok", "video_enabled": False}

    def _check_video_finished(self):
        if self.save_video == 1 and self.video_duration is not None:
            if time.time() - self.video_start >= self.video_duration:
                self.save_video = 2

    def _write_video(self, img, fps):
        if self.video_writer is None:
            folder = self.video_dir()
            filename = f"{self._video_prefix()}_{datetime.now().strftime('%d_%m_%Y_%H_%M_%S')}.avi"
            path = os.path.join(folder, filename)
            try:
                folder.mkdir(parents=True, exist_ok=True)
                fourcc = cv2.VideoWriter_fourcc(*"MJPG")
                writer_fps = fps if fps and fps > 0 else DEFAULT_VIDEO_FPS
                writer = cv2.VideoWriter(path, fourcc, writer_fps, (img.shape[1], img.shape[0]))
                if not writer.isOpened():
                    raise OSError("VideoWriter не открылся (кодек/путь/права)")
            except Exception as e:
                # запись видео не поднялась — гасим режим, иначе на каждый кадр будет
                # новая попытка создать файл (и поток встанет на ошибках)
                self.last_save_error = str(e)
                self.save_video = 0
                self.video_start = None
                log_event("camera_core.write_video", "Не удалось начать запись видео", "error",
                          {"serial_number": self.serial_number, "path": path, "error": str(e)})
                return

            self.video_writer = writer

        try:
            self.video_writer.write(img)
        except Exception as e:
            self.last_save_error = str(e)
            log_event("camera_core.write_video", "Ошибка записи кадра видео", "error",
                      {"serial_number": self.serial_number, "error": str(e)})

    # ---------- сохранение в цикле стрима ----------

    # одиночный снимок по триггеру (soft-trigger): сохранить следующий кадр как фото.
    # project — необязательная папка (по умолчанию — текущий photo_project или «trigger»).
    # save=False — файл не пишем (проект/формат фото не трогаем); sink — колбэк(img), получит
    # копию следующего кадра в память (тот же кадр, что пошёл бы в файл).
    def snap(self, project=None, photo_format=None, save=True, sink=None):
        if save:
            if project:
                self.photo_project = project
            elif not self.photo_project:
                self.photo_project = "trigger"
            # формат одиночного снимка (напр. скрина пробы); неизвестное значение игнорируем
            if photo_format in PHOTO_FORMATS:
                self.photo_format = photo_format
        self._snap_save = bool(save)
        self._snap_sink = sink
        self.snap_once = True
        return {"status": "ok", "streaming": bool(self.running)}

    # вызывается на каждый кадр: автофото + запись видео
    def _maybe_save(self, img, fps):
        if self.snap_once:                 # одиночный снимок по триггеру (soft-trigger)
            self.snap_once = False
            sink, self._snap_sink = self._snap_sink, None
            if sink is not None:
                try:
                    sink(img.copy())
                except Exception as e:
                    log_event("camera_core.snap", "Ошибка колбэка снимка", "warn",
                              {"serial_number": self.serial_number, "error": str(e)})
            if self._snap_save:
                self.write_photo(img, log_name=True)   # имя скрина — в лог программы
        if self.save_photo and self._should_save_photo(self.photo_interval):
            self.write_photo(img)              # потоковый датасет: без лога имени (анти-спам)

        self._check_video_finished()
        if self.save_video == 1:
            self._write_video(img, fps)

        if self.save_video == 2 and self.video_writer is not None:
            self.video_writer.release()
            self.video_writer = None
            self.save_video = 0
            self.video_duration = None
            self.video_start = None
            self.video_project = None

    # метрика резкости последнего кадра (дисперсия лапласиана) — для автофокуса.
    # Чем выше — тем резче. None, если кадра ещё нет (камера не стримит).
    def sharpness(self):
        img = self._last_bgr
        if img is None:
            return None
        try:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            return float(cv2.Laplacian(gray, cv2.CV_64F).var())
        except Exception:
            return None

    # сброс состояния сохранения при закрытии потока
    def _reset_save_state(self):
        self.save_photo = False
        self.photo_interval = None
        self.last_photo = None
        self.photo_project = None

        if self.video_writer is not None:
            self.video_writer.release()
            self.video_writer = None
            self.save_video = 0
            self.video_duration = None
            self.video_start = None
            self.video_project = None
