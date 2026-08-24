"""Сборка микроскопа в один singleton для веб-эндпоинтов (по образцу камерного manager).

Связывает PlateClient (драйвер платы) и MicroscopeFSM (автомат) и даёт приложению
простой фасад: запустить/остановить, отдать телеметрию/статус/состояние автомата,
принять команды. app.py дёргает только `micro`.
"""
import threading

import plate_config
from microscope_plc import PlateClient
from microscope_fsm import MicroscopeFSM
from logger import log_event


class MicroscopeService:
    def __init__(self):
        self.cfg = None
        self.plate = None
        self.fsm = None
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
            self._started = True
            log_event("microscope_service", "Микроскоп запущен",
                      "info", {"host": self.cfg.get("host"), "port": self.cfg.get("port")})

    def stop(self):
        with self._lock:
            if not self._started:
                return
            for obj in (self.fsm, self.plate):
                try:
                    if obj is not None:
                        obj.stop()
                except Exception:
                    pass
            self._started = False
            log_event("microscope_service", "Микроскоп остановлен", "info")

    # ---------- чтение для эндпоинтов ----------

    def telemetry(self):
        return self.plate.telemetry if self.plate else {}

    def status(self):
        return self.plate.status if self.plate else {"connected": False, "reconnecting": False}

    def state(self):
        return self.fsm.state if self.fsm else {}

    def config(self):
        return self.cfg or {}

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


# singleton, с которым работает app.py
micro = MicroscopeService()
