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

    def start(self):
        with self._lock:
            if self._started:
                return
            self.cfg = plate_config.load()
            self.plate = PlateClient(self.cfg)
            self.fsm = MicroscopeFSM(self.plate, self.cfg)
            self.plate.start()
            self.fsm.start()
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

    def _on_sv(self, sv, stage):
        # СВ/стадия из ПЛК -> в автомат (заменяет ручной ввод, пока источник жив)
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

    def stop_movement(self, on):
        if self.fsm:
            self.fsm.set_movement_inhibit(on)

    # ---------- РУЧНОЙ ПУЛЬТ платы (прямое управление, как родной конфигуратор) ----------

    def manual_mode(self, on):
        """Ручной режим: автомат перестаёт писать плату, пультом рулит человек (и наоборот)."""
        if self.fsm:
            self.fsm.set_manual(bool(on))
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
        (немедленной нативной записью). Частоту FSM не трогает — пишем всегда."""
        if self.fsm:
            self.fsm.set_led(bright, on)
        if self.plate:
            if self.is_manual():
                self.plate.set_led_native(bright, freq, on)
            elif freq is not None:
                self.plate.set_led_native(freq=freq)

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
