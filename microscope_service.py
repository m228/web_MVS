"""Сборка микроскопа в один singleton для веб-эндпоинтов (по образцу камерного manager).

Связывает PlateClient (драйвер платы) и MicroscopeFSM (автомат) и даёт приложению
простой фасад: запустить/остановить, отдать телеметрию/статус/состояние автомата,
принять команды. app.py дёргает только `micro`.
"""
import threading
import time

import plate_config
from microscope_plc import PlateClient
from microscope_fsm import MicroscopeFSM
from sv_source import SvSource
from logger import log_event


class MicroscopeService:
    def __init__(self):
        self.cfg = None
        self.plate = None
        self.fsm = None
        self.sv_source = None
        self._started = False
        self._lock = threading.Lock()
        # DEBUG (убрать после отладки цикла): перехват ПЛК — при True данные СВ/стадии
        # из sv_source игнорируются, работает ручной ввод со страницы (см. sv_override).
        self._sv_override = False
        # CV: сессия пробы — фоновый поток «кадр → разбор → следующий» на время выдержки
        self._probe_collecting = False            # сессия сейчас набирает кадры у стекла
        self._cv_run_lock = threading.Lock()      # один разбор за раз
        # статус последнего разбора — показывается в UI (вместо тихих отказов)
        self._cv_status = {"state": "idle", "message": "", "ts": None}
        # автофокус М2 (поиск фокуса по резкости) — фоновый поток + статус/результат
        self._af_thread = None
        self._af_running = False
        self._af_status = {"running": False, "phase": "idle", "table": [], "best": None, "message": ""}

    def start(self):
        with self._lock:
            if self._started:
                return
            self.cfg = plate_config.load()
            self._migrate_cv_thresholds()
            self.plate = PlateClient(self.cfg)
            self.fsm = MicroscopeFSM(self.plate, self.cfg)
            self.fsm.on_photo = self._auto_photo   # кадры пробы в выдержке (см. _auto_photo)
            self.fsm.on_video = self._auto_video   # запись видео пробы на выдержку (см. _auto_video)
            self.fsm.on_varka_count = self._persist_varka   # persist счётчика варок (автокалибровка)
            self.fsm.on_approach_fail = self._log_approach_fail   # «довод не сошёлся» -> отдельный лог
            # довод по абсолютнику ушёл в прошивку — гасим в рантайме (конфиг не перезаписываем),
            # доезд цикла идёт по энкодеру. Автокалибровка нуля работает в ПК (см. set_autocal).
            self.fsm.set_fine_approach(enabled=False)
            self._sync_cv_fsm()                    # выдержка «по CV» (N кадров), если CV включён
            self.plate.start()
            self.fsm.start()
            # режим «Автомат» (галочка камеры) = мастер авто-цикла: включаем циклический режим
            if self.cfg.get("camera_mode") in ("auto", "trigger"):
                self.fsm.set_cyclic(True)
            # этап A: источник СВ/стадии из ПЛК (если включён в конфиге)
            svc = self.cfg.get("sv_source", {}) or {}
            if svc.get("enabled"):
                self.sv_source = SvSource(svc, on_update=self._on_sv,
                                          fallback_host=self.cfg.get("host", ""))
                self.sv_source.start()
            self._started = True
            log_event("microscope_service", "Микроскоп запущен",
                      "info", {"host": self.cfg.get("host"), "port": self.cfg.get("port"),
                               "sv_source": bool(svc.get("enabled"))})

    def _photo_suffix(self):
        """Суффикс в конец имени сырого скрина: стадия варки + СВ (напр. «st7_SV82_2» = стадия 7,
        СВ 82.2), чтобы по имени было видно, на какой стадии и при каком СВ снят кадр.
        Точку в СВ меняем на «_» (в имени файла точка нежелательна)."""
        stage = self.fsm.stage if self.fsm else None
        sv = self.fsm.sv if self.fsm else None
        parts = []
        if stage is not None:
            parts.append("st%d" % int(stage))
        if sv is not None:
            parts.append("SV" + ("%.1f" % float(sv)).replace(".", "_"))
        return "_".join(parts)

    def _migrate_cv_thresholds(self):
        """Один раз: старые сохранённые пороги групп/формы заменяем стартовыми из «Памятки оператора»
        (группы 500/900 мкм, игла 3,0, выпуклость 0,90, вытянутые от 1,6). Округлость и уверенность
        модели не трогаем. Метка th_ver в plate_config.json — чтобы не затирать правки Макса снова."""
        cv = (self.cfg or {}).get("cv") or {}
        if int(cv.get("th_ver", 0)) >= 2:
            return
        patch = {"th_ver": 2,
                 "groups": {"small_max_um": 500.0, "medium_max_um": 900.0},
                 "shape": {"max_aspect": 3.0, "min_solidity": 0.90, "suspect_aspect": 1.6}}
        self.cfg = plate_config.save({"cv": patch})
        log_event("microscope_service", "Пороги CV обновлены на стартовые из памятки (th_ver=2)", "info", patch)

    def _auto_photo(self):
        """Колбэк автомата: дёргается на входе в выдержку пробы и далее каждые shot_interval_sec
        (не зависит от camera_mode: camera_mode рулит лишь авто-СТАРТОМ цикла по стадии).
          * CV включён → на первом вызове стартует СЕССИЯ ПРОБЫ (см. _cv_probe_session): кадры
            берутся подряд по готовности обработки, а не по этому таймеру; повторные вызовы — no-op;
          * CV выключен, тумблер «Сырые фото» (вкладка «Цикл») включён → по таймеру пишем сырой
            файл в датасет (png/jpg), как раньше.
        Нужен серийник и живой стрим. Все причины пропуска пишем в лог, чтобы было видно почему."""
        cfg = self.cfg or {}
        if (cfg.get("cv") or {}).get("enabled"):
            self._start_probe_session()
            return
        if not (cfg.get("probe_cycle") or {}).get("photo_enabled", False):
            return
        serial = (cfg.get("camera_serial") or "").strip()
        if not serial:
            log_event("microscope_service", "Скрин пробы пропущен: не задан camera_serial", "warn")
            return
        try:
            from camera_core import manager as cam_manager
            worker = cam_manager.get(serial)
            if not worker.running:
                log_event("microscope_service",
                          "Скрин пробы пропущен: камера не стримит (открой страницу/поток камеры)",
                          "warn", {"serial": serial})
                return
            fmt = (cfg.get("probe_cycle") or {}).get("photo_format", "png")
            worker.photo_suffix = self._photo_suffix()
            worker.snap("microscope", fmt)
            log_event("microscope_service", "Скрин пробы: запрошен снимок", "info",
                      {"serial": serial, "format": fmt})
        except Exception as e:
            log_event("microscope_service", "Ошибка скрина пробы", "warn", {"error": str(e)})

    def _auto_video(self, duration):
        """Колбэк автомата: на входе в выдержку — писать видео пробы на duration сек (авто-финиш).
        Как и скрин, делается всегда при пробе (не зависит от camera_mode); нужен серийник и стрим."""
        cfg = self.cfg or {}
        serial = (cfg.get("camera_serial") or "").strip()
        if not serial:
            return
        try:
            from camera_core import manager as cam_manager
            worker = cam_manager.get(serial)
            if not worker.running:
                log_event("microscope_service",
                          "Видео пробы пропущено: камера не стримит", "warn", {"serial": serial})
                return
            worker.on_video(int(duration), "microscope")
            log_event("microscope_service", "Видео пробы: старт записи", "info",
                      {"serial": serial, "duration": int(duration)})
        except Exception as e:
            log_event("microscope_service", "Ошибка видео пробы", "warn", {"error": str(e)})

    # ---------- CV: анализ пробы (компьютерное зрение) ----------
    # Кадры пробы живут в ПАМЯТИ: камера → модель. На диск идёт только распознанный overlay
    # (JPEG) + result.json. Сырые файлы пишутся лишь при тумблере «Сырые фото».
    # Темп кадров задаёт сама обработка: кадр разобран → берём следующий. У стекла стоим не по
    # секундам, а до frames_per_probe разобранных кадров — потом сервис даёт автомату «отводи»
    # (fsm.cv_release). Триггер "cv" запускает следующую пробу, когда эта сохранена (fsm.cv_end).

    PROBE_MAX_FRAMES = 12      # потолок кадров на пробу (overlay каждого ~15 МБ в памяти)
    PROBE_MODE_DWELL = 23      # режим автомата «выдержка у стекла» (см. microscope_fsm)

    def _set_cv_status(self, state, message=""):
        """Статус разбора для UI: idle / running / ok / no_camera / no_frames /
        sidecar_offline / error. Любой отказ виден на странице, а не только в логе."""
        self._cv_status = {"state": state, "message": message, "ts": time.strftime("%H:%M:%S")}

    def cv_status(self):
        return dict(self._cv_status)

    def _cv_frames_per_probe(self, cv):
        return max(1, min(self.PROBE_MAX_FRAMES, int(cv.get("frames_per_probe", 3))))

    def _sync_cv_fsm(self):
        """Передать автомату настройки выдержки «по CV» (вкл / кадров / защитная пауза /
        страховка) — на старте и при каждой правке настроек CV."""
        if not self.fsm:
            return
        cv = (self.cfg or {}).get("cv") or {}
        self.fsm.set_cv_dwell(bool(cv.get("enabled")),
                              frames=self._cv_frames_per_probe(cv),
                              gap_sec=cv.get("gap_sec", 10),
                              timeout_sec=cv.get("dwell_timeout_sec", 60))

    def _probe_at_glass(self):
        """Автомат стоит в выдержке у стекла — кадры пробы можно брать."""
        return self.fsm is not None and self.fsm.mode == self.PROBE_MODE_DWELL

    def _start_probe_session(self):
        """Запустить сессию пробы в фоне (не блокируем FSM); уже набирает кадры — ничего не делаем."""
        if self._probe_collecting:
            return
        self._probe_collecting = True
        threading.Thread(target=self._cv_probe_session, daemon=True).start()

    def _grab_frame(self, worker, save_raw=False, fmt=None, timeout=5.0):
        """Следующий кадр стрима → в память (при save_raw — ещё и сырым файлом в датасет).
        None — камера не отдала кадр за timeout."""
        got = threading.Event()
        box = []

        def sink(img):
            box.append(img)
            got.set()

        if save_raw:
            worker.photo_suffix = self._photo_suffix()
        worker.snap("microscope" if save_raw else None, fmt, save=save_raw, sink=sink)
        if got.wait(timeout):
            return box[0]
        worker.snap_once = False     # кадра нет — снять запрос, чтобы он не сработал позже
        return None

    def _cv_probe_session(self):
        """Сессия пробы (фон): у стекла «снять кадр → разобрать → следующий», пока не разобрано
        frames_per_probe кадров. Затем автомату — «отводи» (cv_release), а проба досохраняется
        (разломы по серии + overlay JPEG). В конце cv_end: CV готов к следующей пробе.
        Любой отказ (нет камеры/кадров) тоже отпускает автомат — у стекла он не зависает."""
        cfg = self.cfg or {}
        cv = cfg.get("cv") or {}
        serial = (cfg.get("camera_serial") or "").strip()
        fsm = self.fsm
        released = False
        locked = False
        if fsm:
            fsm.cv_begin()
        try:
            if not serial:
                self._set_cv_status("no_camera", "проба не разобрана: камера не выбрана")
                log_event("microscope_service", "CV: проба пропущена — не задан camera_serial", "warn")
                return
            from camera_core import manager as cam_manager
            worker = cam_manager.get(serial)
            if not worker.running:
                self._set_cv_status("no_camera", "проба не разобрана: камера не стримит")
                log_event("microscope_service",
                          "CV: проба пропущена — камера не стримит (открой страницу/поток камеры)",
                          "warn", {"serial": serial})
                return
            # прошлая проба ещё досохраняется / идёт ручной разбор — ждём, но недолго
            locked = self._cv_run_lock.acquire(timeout=10)
            if not locked:
                self._set_cv_status("error", "проба не разобрана: CV занят прошлым разбором")
                log_event("microscope_service", "CV: проба пропущена — CV занят более 10 с", "warn")
                return
            pc = cfg.get("probe_cycle") or {}
            save_raw = bool(pc.get("photo_enabled", False))
            fmt = pc.get("photo_format", "png")
            stage = fsm.stage if fsm else None
            need = self._cv_frames_per_probe(cv)
            deadline = time.time() + max(10, int(cv.get("dwell_timeout_sec", 60)))   # страховка
            run = self._cv_begin(cfg)
            while (self._probe_at_glass() and len(run["overlays"]) < need
                   and time.time() < deadline):
                img = self._grab_frame(worker, save_raw, fmt)
                if img is None or not self._probe_at_glass():
                    break      # камера молчит / кадр пришёл уже после начала отвода (в движении)
                self._cv_analyze_one(run, img)
                done = len(run["overlays"])
                if fsm:
                    fsm.cv_progress(done)
                self._set_cv_status("running", "проба: разобрано кадров %d из %d" % (done, need))
            self._probe_collecting = False
            done = len(run["overlays"])
            if fsm:
                fsm.cv_release(ok=done > 0)     # кадры набраны → отвод; сохранение идёт параллельно
            released = True
            if done:
                self._cv_finish(run, stage, serial)
            else:
                self._set_cv_status("no_frames", "нет кадров пробы: камера не отдала кадр")
                log_event("microscope_service", "CV: кадры пробы не получены", "warn",
                          {"serial": serial})
        except Exception as e:
            self._set_cv_status("error", "ошибка разбора: %s" % e)
            log_event("microscope_service", "Ошибка CV-анализа пробы", "error", {"error": str(e)})
        finally:
            self._probe_collecting = False
            if locked:
                self._cv_run_lock.release()
            if fsm:
                if not released:
                    fsm.cv_release(ok=False)
                fsm.cv_end()

    def analyze_last_probe(self):
        """Ручной разбор (кнопка «Разобрать пробу», /api/cv/analyze): взять ЖИВОЙ кадр с камеры
        и разобрать его. Сырых файлов на диске нет, поэтому разбираем то, что камера видит сейчас."""
        cfg = self.cfg or {}
        serial = (cfg.get("camera_serial") or "").strip()
        if not serial:
            self._set_cv_status("no_camera", "камера не выбрана")
            return {"status": "no_camera"}
        from camera_core import manager as cam_manager
        worker = cam_manager.get(serial)
        if not worker.running:
            self._set_cv_status("no_camera", "камера не стримит — подключи поток")
            return {"status": "no_camera"}
        if self._cv_run_lock.locked():
            return {"status": "busy"}
        stage = self.fsm.stage if self.fsm else None
        self._set_cv_status("running", "разбор…")
        threading.Thread(target=self._cv_live_worker, args=(stage, worker, serial), daemon=True).start()
        return {"status": "started"}

    def _cv_live_worker(self, stage, worker, serial):
        """Фон: снять один живой кадр в память и разобрать."""
        if not self._cv_run_lock.acquire(blocking=False):
            return
        try:
            img = self._grab_frame(worker)
            if img is None:
                self._set_cv_status("no_frames", "камера не отдала кадр за 5 с")
                log_event("microscope_service", "CV: живой кадр не получен за 5 с", "warn")
                return
            run = self._cv_begin(self.cfg or {})
            self._cv_analyze_one(run, img)
            self._cv_finish(run, stage, serial)
        except Exception as e:
            self._set_cv_status("error", "ошибка разбора: %s" % e)
            log_event("microscope_service", "Ошибка CV-анализа кадра", "error", {"error": str(e)})
        finally:
            self._cv_run_lock.release()

    def _cv_begin(self, cfg):
        """Начать разбор: настройки + проверка сайдкара. Возвращает накопитель кадров серии."""
        import cv_client
        cv = cfg.get("cv") or {}
        fr_cfg = cfg.get("fracture") or {}
        url = cv.get("service_url", "http://127.0.0.1:8765")
        self._set_cv_status("running", "разбор…")
        return {
            "cv": cv, "url": url,
            "sidecar_ok": cv_client.health(url) is not None,    # кристаллы (YOLO) — опционально
            "fr_cfg": fr_cfg, "fr_on": fr_cfg.get("enabled", True),
            # overlays — ЧИСТЫЕ кадры серии (контуры в картинку не впекаем — их рисует браузер)
            "frame_recs": [], "overlays": [], "zones": [], "timing": {}, "answered": 0, "shape": None,
            "svs": [],       # СВ на момент каждого кадра: от него зависит, идёт ли брак в рассев
        }

    def _cv_analyze_one(self, run, img):
        """Один кадр (BGR, из памяти) → сайдкар (детекции) → cv_analyzer (измерения) + зоны
        разломов. Результат копится в run; сырой кадр нигде не сохраняется."""
        import cv2
        import cv_analyzer
        import cv_client
        import cv_fracture
        cv = run["cv"]
        # --- кристаллы (сайдкар YOLO), если он поднят ---
        summary, objects = None, []
        if run["sidecar_ok"]:
            ok, enc = cv2.imencode(".png", img)
            resp = cv_client.infer(
                run["url"], enc.tobytes(), tiles=int(cv.get("tiles", 6)),
                conf=float(cv.get("conf", 0.25)), iou=float(cv.get("iou", 0.45)),
                overlap=float(cv.get("overlap", 0.15))) if ok else None
            if resp:
                run["answered"] += 1
                run["timing"] = resp.get("timing", {})
                sv = self.fsm.sv if self.fsm else None
                run["svs"].append(sv)
                res = cv_analyzer.analyze(img, resp.get("objects", []), cv_cfg=cv, with_overlay=False, sv=sv)
                summary, objects = res["summary"], res["objects"]
        # --- разломы (чистый OpenCV, всегда) ---
        run["zones"].append(cv_fracture.detect_zones(img, run["fr_cfg"]) if run["fr_on"] else [])
        run["frame_recs"].append({"file": "frame_%d" % len(run["overlays"]),
                                  "summary": summary, "objects": objects})
        run["overlays"].append(img)
        run["shape"] = img.shape

    def _cv_finish(self, run, stage, serial):
        """Закрыть разбор: подтвердить разломы по серии кадров, сохранить пробу (чистые кадры в
        JPEG + объекты с контурами + result.json), выставить итоговый статус."""
        import cv_analyzer
        import cv_fracture
        import cv_store
        cv = run["cv"]
        overlays = run["overlays"]
        # миниатюра пробы — кадр 0 с контурами (в ленте проб видно, что распознано)
        thumb_img = cv_analyzer.draw_objects(overlays[0], run["frame_recs"][0]["objects"])
        # подтверждение разломов по серии кадров; зоны уходят в result.json — их рисует браузер
        fracture = None
        if run["fr_on"]:
            confirmed, fr_summary = cv_fracture.confirm(run["zones"], run["fr_cfg"])
            fr_summary["area_pct"] = cv_fracture.area_pct(confirmed, run["shape"])
            cv_fracture.draw(thumb_img, confirmed, confirmed=True)
            fracture = {"summary": fr_summary, "zones": confirmed}
            if fr_summary["has_fracture"]:
                log_event("microscope_service",
                          "Разлом обнаружен: %d зон (%.1f%% кадра)" % (
                              fr_summary["zones"], fr_summary["area_pct"]),
                          "warn", {"serial": serial, "zones": fr_summary["zones"]})

        saved = cv_store.save_sample(serial, stage, run["frame_recs"], overlays, run["timing"],
                                     keep_last=int(cv.get("keep_last", 50)), fracture=fracture,
                                     jpeg_quality=int(cv.get("overlay_jpeg_quality", 85)),
                                     thumb_img=thumb_img,
                                     sv=(sum(x for x in run["svs"] if x is not None) / max(1, len([x for x in run["svs"] if x is not None]))
                                         if any(x is not None for x in run["svs"]) else None))
        if not saved:
            self._set_cv_status("error", "не удалось сохранить пробу (см. лог)")
        elif not run["sidecar_ok"]:
            self._set_cv_status("sidecar_offline", "сайдкар не отвечает — кристаллы не посчитаны")
            log_event("microscope_service", "CV: сайдкар не отвечает, кристаллы не посчитаны",
                      "warn", {"url": run["url"]})
        elif run["answered"] == 0:
            self._set_cv_status("sidecar_offline", "сайдкар не вернул детекции (см. лог)")
        else:
            self._set_cv_status("ok", "разбор готов: %d крист., кадров %d" % (
                round(saved["summary"].get("count") or 0), len(overlays)))

    def _on_sv(self, sv, stage):
        # СВ/стадия из ПЛК -> в автомат (заменяет ручной ввод, пока источник жив)
        # DEBUG: при перехвате ПЛК не затираем ручной ввод со страницы
        if self._sv_override:
            return
        if self.fsm:
            if sv is not None:
                self.fsm.set_sv(sv)
            if stage is not None:
                self.fsm.set_stage(stage)

    def stop(self):
        with self._lock:
            if not self._started:
                return
            for obj in (self.sv_source, self.fsm, self.plate):
                try:
                    if obj is not None:
                        obj.stop()
                except Exception:
                    pass
            self.sv_source = None
            self._started = False
            log_event("microscope_service", "Микроскоп остановлен", "info")

    def reload(self):
        """Перечитать plate_config.json и перезапустить плату+автомат с новыми настройками
        (чтобы правки SP/SVSP/периодов/адресов применялись без перезапуска приложения)."""
        was = self._started
        self.stop()
        if was:
            self.start()   # start() сам вызывает plate_config.load()
        return {"status": "reloaded", "host": self.cfg.get("host") if self.cfg else None}

    # ---------- чтение для эндпоинтов ----------

    def telemetry(self):
        return self.plate.telemetry if self.plate else {}

    def ext(self):
        return self.plate.ext if self.plate else {}

    def status(self):
        return self.plate.status if self.plate else {"connected": False, "reconnecting": False}

    def state(self):
        return self.fsm.state if self.fsm else {}

    def config(self):
        return self.cfg or {}

    def sv_status(self):
        if self.sv_source:
            return {"enabled": True, **self.sv_source.status()}
        return {"enabled": False, "connected": False, "values": {}}

    def apply_settings(self, patch):
        """Сохранить правки (IP камеры/платы и т.п.) в plate_config.json и перезапуститься."""
        plate_config.save(patch)
        return self.reload()

    def cv_config(self):
        """Текущий блок CV из конфига (вкладка «CV»)."""
        return (self.cfg or {}).get("cv", {}) if self.cfg else {}

    def set_cv(self, patch):
        """Живое обновление настроек CV (тумблер/пороги/масштаб) — БЕЗ reload платы,
        чтобы не прерывать цикл. Пороги читаются на каждый анализ, так что применяются сразу."""
        plate_config.save({"cv": patch})
        if self.cfg is not None:
            cur = self.cfg.setdefault("cv", {})
            for k, v in patch.items():
                if isinstance(v, dict) and isinstance(cur.get(k), dict):
                    cur[k].update(v)
                else:
                    cur[k] = v
        self._sync_cv_fsm()     # вкл/кадров/пауза — сразу в автомат (выдержка «по CV»)
        log_event("microscope_service", "Настройки CV обновлены", "info", {"patch": patch})
        return self.cv_config()

    def _live_patch(self, key, patch):
        """Живое обновление вложенного блока конфига (без reload платы)."""
        plate_config.save({key: patch})
        if self.cfg is not None:
            cur = self.cfg.setdefault(key, {})
            for k, v in patch.items():
                if isinstance(v, dict) and isinstance(cur.get(k), dict):
                    cur[k].update(v)
                else:
                    cur[k] = v
        return (self.cfg or {}).get(key, {})

    def fracture_config(self):
        """Блок настроек разломов (вкладка «Разломы», Часть B)."""
        return (self.cfg or {}).get("fracture", {}) if self.cfg else {}

    def set_fracture(self, patch):
        """Живое обновление порогов разломов — применяется к следующей пробе."""
        res = self._live_patch("fracture", patch)
        log_event("microscope_service", "Настройки разломов обновлены", "info", {"patch": patch})
        return res

    def approach_config(self):
        """Блок автоподвода по разломам (Часть C, по умолчанию выкл)."""
        return (self.cfg or {}).get("approach_correction", {}) if self.cfg else {}

    def set_approach(self, patch):
        """Живое обновление настроек автоподвода (галочка/шаг мкм/пороги)."""
        res = self._live_patch("approach_correction", patch)
        log_event("microscope_service", "Настройки автоподвода обновлены", "info", {"patch": patch})
        return res

    def set_photo_enabled(self, on):
        """Тумблер «Сырые фото» (вкладка «Цикл»): вкл — кадры пробы дополнительно пишутся в датасет
        (для дообучения модели). CV от него не зависит: кадр в модель идёт из памяти. Без reload платы."""
        on = bool(on)
        plate_config.save({"probe_cycle": {"photo_enabled": on}})
        if self.cfg is not None:
            self.cfg.setdefault("probe_cycle", {})["photo_enabled"] = on
        log_event("microscope_service", "Сырые фото пробы: " + ("вкл" if on else "выкл"),
                  "info", {"photo_enabled": on})
        return {"photo_enabled": on}

    def set_trigger_mode(self, mode):
        """Сменить триггер пробы (time/sv/cv) сразу, без перезапуска платы, и запомнить в конфиг."""
        mode = str(mode) if str(mode) in ("time", "sv", "cv") else "time"
        res = self.fsm.set_trigger_mode(mode) if self.fsm else {"trigger_mode": mode}
        plate_config.save({"probe_cycle": {"trigger_mode": mode}})
        if self.cfg is not None:
            self.cfg.setdefault("probe_cycle", {})["trigger_mode"] = mode
        log_event("microscope_service", "Триггер пробы: " + mode, "info", {"trigger_mode": mode})
        return res

    def set_ignore_stage(self, on):
        """«Варить без стадии» — авто-цикл без проверки стадии 3..9. Сразу, без reload."""
        on = bool(on)
        res = self.fsm.set_ignore_stage(on) if self.fsm else {"ignore_stage": on}
        plate_config.save({"probe_cycle": {"ignore_stage": on}})
        if self.cfg is not None:
            self.cfg.setdefault("probe_cycle", {})["ignore_stage"] = on
        log_event("microscope_service", "Варить без стадии: " + ("вкл" if on else "выкл"),
                  "info", {"ignore_stage": on})
        return res

    # ---------- автофокус М2 (поиск фокуса по резкости) ----------

    def autofocus(self, start, end, coarse, fine):
        """Запустить двухпроходный автофокус М2 в фоне. Только в РУЧНОМ режиме (автомат не
        должен перетирать фокус). Грубый свип шагом coarse -> пик -> точный шагом fine вокруг."""
        if not self.fsm or not self.plate:
            return {"status": "error", "error": "микроскоп не запущен"}
        if not self.fsm.manual:
            return {"status": "error", "error": "включите Ручной режим"}
        if self._af_running:
            return {"status": "error", "error": "автофокус уже идёт"}
        serial = (self.cfg or {}).get("camera_serial", "")
        try:
            from camera_core import manager as cam_manager
            cam = cam_manager.get(serial) if serial else None
        except Exception:
            cam = None
        if not cam or not getattr(cam, "running", False):
            return {"status": "error", "error": "камера не стримит (подключите видео)"}
        start = max(0, int(start)); end = max(start, int(end))
        coarse = max(10, int(coarse)); fine = max(1, int(fine))
        self._af_running = True
        self._af_status = {"running": True, "phase": "coarse", "table": [], "best": None, "message": "старт"}
        self._af_thread = threading.Thread(target=self._autofocus_run, name="autofocus",
                                           args=(cam, start, end, coarse, fine), daemon=True)
        self._af_thread.start()
        log_event("microscope_service", "Автофокус запущен", "info",
                  {"start": start, "end": end, "coarse": coarse, "fine": fine})
        return {"status": "started"}

    def autofocus_stop(self):
        self._af_running = False
        return {"status": "stopping"}

    def autofocus_status(self):
        return dict(self._af_status)

    def _goto_m2_settle(self, pos, tol=50, stuck_s=5.0, max_s=25.0):
        """Довести М2 до pos повтором goto (плата за импульс делает лишь шаг). Возврат:
        'arrived' — доехал (|pos2-pos|<=tol); 'stuck' — 5 с без движения (упор/приехал);
        'timeout'/'abort'. Упор безопасен (спец. проход), детектим по отсутствию движения."""
        p = self.plate
        last = None
        no_move_since = time.time()
        t0 = time.time()
        while self._af_running and (time.time() - t0) < max_s:
            p.motor_goto(2, pos)                        # импульс к цели (redrive)
            time.sleep(0.35)
            cur = (p.telemetry or {}).get("pos2")
            if cur is None:
                continue
            if abs(cur - pos) <= tol:
                return "arrived"
            if last is not None and abs(cur - last) < 5:   # позиция не меняется
                if (time.time() - no_move_since) >= stuck_s:
                    return "stuck"
            else:
                no_move_since = time.time()               # было движение — сбрасываем таймер упора
            last = cur
        return "abort" if not self._af_running else "timeout"

    def _measure(self, cam):
        """Замер резкости ПОСЛЕ перемещения. Камера медленная (~1 fps), поэтому сначала ждём
        СВЕЖИЙ кадр в новой позиции (по счётчику image_number), иначе замерим старый кадр (до
        движения). Затем небольшой запас и усредняем пару замеров."""
        def frame_n():
            try:
                return cam.metrics.get("image_number")
            except Exception:
                return None
        start_n = frame_n()
        t0 = time.time()
        # ждём новый кадр (до 3 с — с запасом на 1 fps); если счётчика нет — просто пауза 1.2 с
        while self._af_running and (time.time() - t0) < 3.0:
            n = frame_n()
            if start_n is None:
                if (time.time() - t0) >= 1.2:
                    break
            elif n is not None and n != start_n:
                break
            time.sleep(0.1)
        time.sleep(0.4)   # дать свежему кадру осесть
        vals = []
        for _ in range(2):
            s = cam.sharpness()
            if s is not None:
                vals.append(s)
            time.sleep(0.15)
        return round(sum(vals) / len(vals), 1) if vals else None

    def _autofocus_run(self, cam, start, end, coarse, fine):
        try:
            table = []
            # 1) грубый свип
            pos = start
            while self._af_running and pos <= end:
                st = self._goto_m2_settle(pos)
                sh = self._measure(cam)
                if sh is not None:
                    table.append({"pos": pos, "sharp": sh})
                self._af_status = {"running": True, "phase": "coarse", "table": list(table),
                                   "best": None, "message": "грубо: %d мкм" % pos}
                if st == "stuck":
                    break                                 # упор — дальше некуда
                pos += coarse
            if not self._af_running:
                self._finish_af(table, None, "остановлено")
                return
            if not table:
                self._finish_af(table, None, "нет данных (камера не даёт кадры)")
                return
            # 2) пик грубого прохода -> точный свип вокруг ±coarse
            best = max(table, key=lambda r: r["sharp"])["pos"]
            lo = max(start, best - coarse)
            hi = min(end, best + coarse)
            pos = lo
            while self._af_running and pos <= hi:
                if not any(r["pos"] == pos for r in table):
                    st = self._goto_m2_settle(pos)
                    sh = self._measure(cam)
                    if sh is not None:
                        table.append({"pos": pos, "sharp": sh})
                    self._af_status = {"running": True, "phase": "fine", "table": sorted(table, key=lambda r: r["pos"]),
                                       "best": None, "message": "точно: %d мкм" % pos}
                    if st == "stuck" and pos > best:
                        break
                pos += fine
            # 3) лучшая точка -> переезжаем туда
            best_row = max(table, key=lambda r: r["sharp"])
            if self._af_running:
                self._goto_m2_settle(best_row["pos"])
            self._finish_af(table, best_row, "готово")
        except Exception as e:
            log_event("microscope_service", "Ошибка автофокуса", "error", {"error": str(e)})
            self._finish_af([], None, "ошибка: %s" % e)

    def _finish_af(self, table, best_row, message):
        self._af_running = False
        self._af_status = {
            "running": False, "phase": "done",
            "table": sorted(table, key=lambda r: r["pos"]),
            "best": best_row, "message": message,
        }
        log_event("microscope_service", "Автофокус завершён", "info",
                  {"best": best_row, "points": len(table), "message": message})

    def set_ignore_focus(self, on):
        """Галочка «Не использовать фокус» (вкладка СВ/МКМ) — сразу, persist в конфиг."""
        on = bool(on)
        res = self.fsm.set_ignore_focus(on) if self.fsm else {"ignore_focus": on}
        plate_config.save({"probe_cycle": {"ignore_focus": on}})
        if self.cfg is not None:
            self.cfg.setdefault("probe_cycle", {})["ignore_focus"] = on
        log_event("microscope_service", "Фокус по таблице: " + ("выкл" if on else "вкл"),
                  "info", {"ignore_focus": on})
        return res

    def set_sensor_filter(self, enabled=None, avg_sec=None):
        """Фильтр датчика перемещения (1271): вкл/выкл + окно усреднения (сек). Сразу, без reload.
        Отфильтрованное значение идёт и в показ, и в логику доезда/watchdog. Persist в конфиг."""
        patch = {}
        if enabled is not None:
            patch["enabled"] = bool(enabled)
        if avg_sec is not None:
            patch["avg_sec"] = max(0.1, float(avg_sec))
        if self.plate:
            self.plate.set_sensor_filter(patch.get("enabled"), patch.get("avg_sec"))
        if patch:
            plate_config.save({"sensor_filter": patch})
            if self.cfg is not None:
                self.cfg.setdefault("sensor_filter", {}).update(patch)
        log_event("microscope_service", "Фильтр датчика 1271", "info", patch)
        return {"status": "ok", **patch}

    def set_fine_approach(self, enabled=None, coarse_tol_um=None, fine_tol_um=None,
                          max_retry=None, pause_sec=None):
        """Гибридный доезд подвода (грубо по 1274 → точно по датчику 1271): вкл/выкл + допуски/
        повторы/пауза. Сразу, без reload; persist в конфиг."""
        patch = {}
        if enabled is not None:
            patch["enabled"] = bool(enabled)
        if coarse_tol_um is not None:
            patch["coarse_tol_um"] = int(coarse_tol_um)
        if fine_tol_um is not None:
            patch["fine_tol_um"] = int(fine_tol_um)
        if max_retry is not None:
            patch["max_retry"] = int(max_retry)
        if pause_sec is not None:
            patch["pause_sec"] = max(0.5, float(pause_sec))
        res = self.fsm.set_fine_approach(**patch) if self.fsm else patch
        if patch:
            plate_config.save({"fine_approach": patch})
            if self.cfg is not None:
                self.cfg.setdefault("fine_approach", {}).update(patch)
        log_event("microscope_service", "Довод по абсолютнику", "info", patch)
        return {"status": "ok", **res}

    def clear_fault(self):
        """Сброс аварии оператором (подгон не сошёлся и т.п.): снять запрет, погасить флаг."""
        if self.fsm:
            return self.fsm.clear_fault()
        return {"status": "no_fsm"}

    def _persist_varka(self, count):
        """Колбэк FSM: сохранить счётчик варок в конфиг (переживает перезапуск)."""
        plate_config.save({"autocal": {"count": int(count)}})
        if self.cfg is not None:
            self.cfg.setdefault("autocal", {})["count"] = int(count)

    def _log_approach_fail(self, info):
        """Колбэк FSM: подгон по датчику не сошёлся — пошли на след. этап. Пишем момент в
        ОТДЕЛЬНЫЙ файл (approach_fails.log в каталоге данных), чтобы разобрать/починить потом."""
        from datetime import datetime
        from paths import DATA_DIR
        try:
            line = "%s\ttarget=%s\tsensor=%s\tdelta=%s\tretry=%s\tsv=%s\n" % (
                datetime.now().isoformat(timespec="seconds"),
                info.get("target"), info.get("sensor"), info.get("delta"),
                info.get("retry"), info.get("sv"))
            path = DATA_DIR / "approach_fails.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception as e:
            log_event("microscope_service", "Не удалось записать лог довода", "warn", {"error": str(e)})

    def set_autocal(self, enabled=None, every_n=None, sensor_lo=None, sensor_hi=None, timeout_sec=None):
        """Автокалибровка нуля М1: вкл/выкл + через сколько варок + пороги датчика + таймаут.
        Сразу, без reload; persist в конфиг."""
        patch = {}
        if enabled is not None:
            patch["enabled"] = bool(enabled)
        if every_n is not None:
            patch["every_n"] = max(1, int(every_n))
        if sensor_lo is not None:
            patch["sensor_lo"] = int(sensor_lo)
        if sensor_hi is not None:
            patch["sensor_hi"] = int(sensor_hi)
        if timeout_sec is not None:
            patch["timeout_sec"] = max(1, int(timeout_sec))
        res = self.fsm.set_autocal(**patch) if self.fsm else patch
        if patch:
            plate_config.save({"autocal": patch})
            if self.cfg is not None:
                self.cfg.setdefault("autocal", {}).update(patch)
        log_event("microscope_service", "Автокалибровка нуля", "info", patch)
        return {"status": "ok", **(res or patch)}

    def start_autocal(self):
        """Ручной запуск автокалибровки (кнопка)."""
        if self.fsm:
            return self.fsm.start_autocal()
        return {"status": "no_fsm"}

    def reset_varka_count(self):
        """Сбросить счётчик варок в 0 (кнопка)."""
        if self.fsm:
            self.fsm.set_autocal(count=0)
        self._persist_varka(0)
        return {"status": "ok", "count": 0}

    def set_sensor_display_scale(self, value):
        """Масштаб датчика 1271: и показ на странице, И логика доезда (FSM), чтобы датчик был
        в масштабе задания — иначе дельта «задание−датчик» огромная и подгон не сходится."""
        v = float(value)
        plate_config.save({"sensor_display_scale": v})
        if self.cfg is not None:
            self.cfg["sensor_display_scale"] = v
        if self.fsm:
            self.fsm.set_sensor_scale(v)   # тот же коэффициент в логику доезда
        log_event("microscope_service", "Масштаб датчика 1271 = %s (показ+доезд)" % v, "info",
                  {"sensor_display_scale": v})
        return {"status": "ok", "sensor_display_scale": v}

    def set_m1_stop_sensor(self, value):
        """Записать порог аппаратной блокировки «Стоп М1 при положении аналог. датчика» в
        регистр прошивки (1234, m1_stop_sensor, мкм). Прошивка сама стопит М1 у предела."""
        if not self.plate:
            return {"error": "not_started"}
        reg = int(((self.cfg or {}).get("ext_map", {}).get("m1_stop_sensor", {})).get("reg", 1234))
        val = int(round(float(value)))
        self.plate.write_reg(reg, val)
        log_event("microscope_service", "Стоп М1 по датчику (рег. %d) = %d мкм" % (reg, val),
                  "info", {"reg": reg, "value": val})
        return {"status": "ok", "reg": reg, "value": val}

    def set_lock_bit(self, bit, disabled):
        """Блокировки прошивки (рег. 1535): бит=1 → блокировка ОТКЛЮЧЕНА. Меняем один бит по
        последнему прочитанному значению; не прочитали (нет связи) — не пишем вслепую."""
        if not self.plate:
            return {"error": "not_started"}
        bit = int(bit)
        if not 0 <= bit <= 15:
            return {"error": "bad_bit"}
        spec = (self.cfg or {}).get("ext_map", {}).get("locks") or {}
        reg = int(spec.get("reg", 1535))
        cur = (self.plate.ext or {}).get("locks")
        if cur is None:
            return {"error": "no_value", "hint": "регистр блокировок ещё не прочитан"}
        cur = int(cur) & 0xFFFF
        new = (cur | (1 << bit)) if disabled else (cur & ~(1 << bit) & 0xFFFF)
        if new == cur:
            return {"status": "unchanged", "reg": reg, "value": cur}
        self.plate.write_reg(reg, new)
        log_event("microscope_service",
                  "Блокировка бит %d (рег. %d): %s, маска %s → %s" % (
                      bit, reg, "ОТКЛЮЧЕНА" if disabled else "включена", format(cur, "016b"), format(new, "016b")),
                  "warn", {"reg": reg, "bit": bit, "disabled": bool(disabled), "old": cur, "new": new})
        return {"status": "ok", "reg": reg, "value": new}

    def set_cycle_autostart(self, on):
        """Запомнить галочку «Автостарт цикла» в конфиг БЕЗ перезапуска платы."""
        on = bool(on)
        plate_config.save({"cycle_autostart": on})
        if self.cfg is not None:
            self.cfg["cycle_autostart"] = on
        log_event("microscope_service", "Автостарт цикла после перезапуска: " + ("вкл" if on else "выкл"),
                  "info", {"cycle_autostart": on})
        return {"status": "ok", "cycle_autostart": on}

    def set_camera_serial(self, serial):
        """Запомнить серийник камеры микроскопа в конфиг БЕЗ перезапуска платы/автомата.
        Нужен для скринов/видео пробы (_auto_photo читает cfg['camera_serial']). Камера
        находится автоматически в UI, но серверу её серийник надо знать явно. reload не
        делаем — просто пишем в JSON и обновляем кэш cfg на лету, чтобы не рвать цикл."""
        serial = (serial or "").strip()
        if not serial:
            return {"status": "empty"}
        if self.cfg is not None and self.cfg.get("camera_serial") == serial:
            return {"status": "unchanged", "camera_serial": serial}
        plate_config.save({"camera_serial": serial})
        if self.cfg is not None:
            self.cfg["camera_serial"] = serial   # чтобы _auto_photo увидел сразу, без reload
        log_event("microscope_service", "Серийник камеры сохранён в конфиг", "info",
                  {"camera_serial": serial})
        return {"status": "ok", "camera_serial": serial}

    # ---------- команды от эндпоинтов ----------

    def command(self, cmd):
        if self.fsm:
            self.fsm.send_command(cmd)

    def set_led(self, bright=None, on=None):
        if self.fsm:
            self.fsm.set_led(bright, on)

    def set_sv(self, value):
        if self.fsm:
            self.fsm.set_sv(value)

    def set_stage(self, stage):
        if self.fsm:
            self.fsm.set_stage(stage)

    def set_cyclic(self, on):
        if self.fsm:
            self.fsm.set_cyclic(on)

    def take_sample(self):
        """Кнопка «Взять пробу»: запустить цикл пробы (шаги 20→24)."""
        if self.fsm:
            return self.fsm.start_sample()
        return {"status": "no_fsm"}

    def cycle_reset(self):
        """Кнопка «Сброс»: прервать цикл, в Ожидание."""
        if self.fsm:
            return self.fsm.reset_cycle()
        return {"status": "no_fsm"}

    def confirm_auto(self):
        """Подтверждение перехода в Автомат при старте варки (из диалога UI)."""
        if self.fsm:
            return self.fsm.confirm_auto()
        return {"status": "no_fsm"}

    def decline_auto(self):
        """Отклонение перехода — остаёмся в ручном."""
        if self.fsm:
            return self.fsm.decline_auto()
        return {"status": "no_fsm"}

    def cycle_skip(self):
        """Кнопка «Вперёд»: перепрыгнуть на следующий шаг цикла."""
        if self.fsm:
            return self.fsm.skip_step()
        return {"status": "no_fsm"}

    def sv_override(self, on):
        """DEBUG (убрать после отладки): перехват ПЛК. При True sv_source перестаёт
        затирать ручной СВ/стадию со страницы — можно вбивать значения и смотреть цикл."""
        self._sv_override = bool(on)
        return {"sv_override": self._sv_override}

    def stop_movement(self, on):
        if self.fsm:
            self.fsm.set_movement_inhibit(on)

    # ---------- РУЧНОЙ ПУЛЬТ платы (прямое управление, как родной конфигуратор) ----------

    def manual_mode(self, on):
        """Верхний тумблер «Автомат/Ручной». Ручной — автомат не трогает плату (рулит пульт).
        Автомат (on=False) — сразу включаем авто-цикл (sw0), чтобы верхний тумблер был
        единственным выключателем автоматики (раньше sw0 включался отдельно на вкладке камеры)."""
        on = bool(on)
        if self.fsm:
            self.fsm.set_manual(on)
            if on:
                # При входе в РУЧНОЙ физически гасим все DQ (клапаны) на плате. FSM в ручном
                # плату не трогает (ранний return в tick), поэтому cw0/cw1 закрываются только
                # ВНУТРИ объекта, а слово выходов 1250 висело бы в последнем состоянии — из-за
                # этого промывка трубки оставалась открытой. Обнуляем слово клапанов явно:
                # буфер OUT[valves] станет 0, поток опроса платы запишет 0 следующим тактом.
                if self.plate:
                    idx = int((self.cfg or {}).get("out", {}).get("valves", 0))
                    self.plate.set_out(idx, 0)
            else:
                self.fsm.set_cyclic(True)   # «Автомат» = авто-цикл включён
        return {"manual": bool(self.fsm.manual) if self.fsm else False}

    def is_manual(self):
        return bool(self.fsm.manual) if self.fsm else False

    def motor_op(self, m, op, value=None):
        """Ручная команда мотору m (1/2). Работает ТОЛЬКО в ручном режиме (иначе автомат
        затрёт следующим тактом). op: goto/steps/shift/stop/find_zero/set_zero/
        home_start/home_end/dir_fwd/dir_back/enable/disable."""
        if not self.plate or not self.fsm:
            return {"error": "not_started"}
        if not self.fsm.manual:
            return {"error": "not_manual"}
        p, m = self.plate, int(m)
        ops = {
            "stop": lambda: p.motor_stop(m),
            "find_zero": lambda: p.motor_find_zero(m),
            "set_zero": lambda: p.motor_set_zero(m),
            "home_start": lambda: p.motor_home(m, "start"),
            "home_end": lambda: p.motor_home(m, "end"),
            "dir_fwd": lambda: p.motor_direction(m, True),
            "dir_back": lambda: p.motor_direction(m, False),
            "enable": lambda: p.motor_enable(m, True),
            "disable": lambda: p.motor_enable(m, False),
            "goto": lambda: p.motor_goto(m, float(value)),
            "steps": lambda: p.motor_steps(m, int(float(value))),
            "shift": lambda: p.motor_shift(m, float(value)),
        }
        fn = ops.get(op)
        if fn is None:
            return {"error": "bad_op"}
        if op in ("goto", "steps", "shift") and value is None:
            return {"error": "no_value"}
        # перед любым движением разрешаем мотор (best-effort: если оператор забыл «разрешён»)
        if op in ("goto", "steps", "shift", "home_start", "home_end", "find_zero"):
            p.motor_enable(m, True)
        fn()
        return {"status": "ok", "m": m, "op": op, "value": value}

    def led_native(self, bright=None, freq=None, on=None):
        """LED-фара: яркость (%)/частота (Гц)/вкл. Работает и в авто (через FSM), и в ручном
        (немедленной нативной записью). Частоту FSM не трогает — пишем всегда.
        Значения persist в plate_config (без reload), чтобы после перезапуска LED был тем же."""
        if self.fsm:
            self.fsm.set_led(bright, on)
        if self.plate:
            if self.is_manual():
                self.plate.set_led_native(bright, freq, on)
            elif freq is not None:
                self.plate.set_led_native(freq=freq)
        # запомнить в конфиг: яркость (+ led_on по яркости) и частоту
        patch = {}
        if bright is not None:
            patch["led_bright"] = int(bright)
            patch["led_on"] = bool(on) if on is not None else int(bright) > 0
        elif on is not None:
            patch["led_on"] = bool(on)
        if freq is not None:
            patch["led_freq"] = int(freq)
        if patch:
            plate_config.save(patch)
            if self.cfg is not None:
                self.cfg.update(patch)

    def dq_bit(self, bit, on):
        """Дискретный выход DQ (клапан и пр.). Только в ручном режиме (в авто клапаны у автомата)."""
        if not self.plate or not self.is_manual():
            return {"error": "not_manual"}
        self.plate.set_dq_bit(int(bit), bool(on))
        return {"status": "ok", "bit": int(bit), "on": bool(on)}

    # ---------- ручное движение моторов (нативные команды платы) ----------

    def move_motor(self, m, um):
        """Идти мотором m (1/2) в позицию um (мкм): снять стоп, разрешить мотор, задать цель.
        Дальше формирование команды в автомате доведёт мотор до позиции (нативная 0x1006/0x2006)."""
        if not self.fsm or not self.plate:
            return
        self.fsm.set_movement_inhibit(False)       # иначе автомат не пишет моторы
        en = (self.cfg or {}).get("motor_enable", {}).get(str(m))
        if en:
            self.plate.write_reg(en, 1)            # разрешить мотор
        if int(m) == 2:
            self.fsm.set_m2_target(um)
        else:
            self.fsm.set_m1_target(um)

    def enable_motor(self, m, on):
        en = (self.cfg or {}).get("motor_enable", {}).get(str(m))
        if en and self.plate:
            self.plate.write_reg(en, 1 if on else 0)

    def estop(self):
        """Аварийный стоп (в любом режиме): нативная команда СТОП обоим моторам + запрет
        движения автомата + обесточить оба мотора."""
        if self.fsm:
            self.fsm.set_movement_inhibit(True)
        if self.plate:
            for m in (1, 2):
                self.plate.motor_stop(m)          # нативная команда СТОП (0x1007/0x2007)
                self.plate.motor_enable(m, False)  # обесточить мотор


# singleton, с которым работает app.py
micro = MicroscopeService()
