"""Сборка микроскопа в один singleton для веб-эндпоинтов (по образцу камерного manager).

Связывает PlateClient (драйвер платы) и MicroscopeFSM (автомат) и даёт приложению
простой фасад: запустить/остановить, отдать телеметрию/статус/состояние автомата,
принять команды. app.py дёргает только `micro`.
"""
import threading

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

    def start(self):
        with self._lock:
            if self._started:
                return
            self.cfg = plate_config.load()
            self.plate = PlateClient(self.cfg)
            self.fsm = MicroscopeFSM(self.plate, self.cfg)
            self.fsm.on_photo = self._auto_photo   # серия скринов в выдержке (см. _auto_photo)
            self.fsm.on_video = self._auto_video   # запись видео пробы на выдержку (см. _auto_video)
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

    def _auto_photo(self):
        """Колбэк автомата: в выдержке пробы -> снять кадр камерой (soft-триггер).
        Скрин пробы делается ВСЕГДА, когда цикл дошёл до выдержки (не зависит от camera_mode:
        camera_mode рулит лишь авто-СТАРТОМ цикла по стадии). Нужен серийник и живой стрим —
        snap сохранит следующий кадр. Все причины пропуска пишем в лог, чтобы было видно почему."""
        cfg = self.cfg or {}
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

    def set_trigger_mode(self, mode):
        """Сменить триггер пробы (time/sv) сразу, без перезапуска платы, и запомнить в конфиг."""
        mode = "sv" if str(mode) == "sv" else "time"
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
        if self.fsm:
            self.fsm.set_manual(bool(on))
            if not on:
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
