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
        return {"enabled": False, "connected": False}

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


# singleton, с которым работает app.py
micro = MicroscopeService()
