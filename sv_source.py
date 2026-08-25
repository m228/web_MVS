"""Источник СВ (Brix) и стадии варки из ПЛК/аппарата по Modbus TCP — этап A.

ПЛК тут в роли «почтового ящика»: Python читает у него два числа, которые сам не
измеряет — СВ (густота утфеля) и стадию варки. Они нужны автомату, чтобы выбирать зазор
подвода SP[i] по СВ и период цикла по стадии.

Отдельное Modbus-соединение (это ДРУГОЕ устройство, не плата микроскопа). Читает медленно
(СВ меняется небыстро). При успешном чтении зовёт callback(sv, stage) — сервис пушит их в
автомат. Пока в конфиге `sv_source.enabled=false` — не запускается, работает ручной ввод.

Адреса/масштаб задаёт Макс в plate_config.json → sv_source:
  host/port/unit, sv_register (+ sv_scale), stage_register.
"""
import threading
import time

from pymodbus.client import ModbusTcpClient

from logger import log_event

_RECONNECT_BACKOFF = [1, 2, 5, 10, 30]


class SvSource:
    def __init__(self, cfg, on_update=None, fallback_host=""):
        # cfg — раздел sv_source из общего конфига
        self.on_update = on_update
        self.host = (cfg.get("host") or fallback_host or "").strip()
        self.port = int(cfg.get("port", 502))
        self.unit = int(cfg.get("unit", 255))
        self.sv_register = int(cfg.get("sv_register", 0))
        self.sv_scale = float(cfg.get("sv_scale", 100)) or 1.0
        self.stage_register = int(cfg.get("stage_register", 0))
        self.period = float(cfg.get("period_s", 1.0))

        self._client = None
        self._thread = None
        self._running = False

        self._connected = False
        self._last_error = None
        self._sv = None
        self._stage = None

    # ---------- жизненный цикл ----------

    def start(self):
        if self._running or not self.host:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="sv-source", daemon=True)
        self._thread.start()
        log_event("sv_source", "Источник СВ запущен", "info",
                  {"host": self.host, "port": self.port, "sv_register": self.sv_register,
                   "stage_register": self.stage_register})

    def stop(self):
        self._running = False
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._close()

    def status(self):
        return {
            "connected": self._connected,
            "sv": self._sv,
            "stage": self._stage,
            "host": self.host,
            "error": self._last_error,
        }

    # ---------- внутреннее ----------

    def _close(self):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        self._connected = False

    def _connect(self):
        if self._client is not None and self._connected:
            return True
        self._close()
        client = ModbusTcpClient(self.host, port=self.port, timeout=1.0)
        if client.connect():
            self._client = client
            self._connected = True
            self._last_error = None
            log_event("sv_source", "Связь с источником СВ установлена", "success", {"host": self.host})
            return True
        try:
            client.close()
        except Exception:
            pass
        return False

    def _read_reg(self, addr):
        rr = self._client.read_holding_registers(addr, count=1, slave=self.unit)
        if rr.isError():
            raise IOError("read reg %d error: %r" % (addr, rr))
        return rr.registers[0]

    def _loop(self):
        backoff_i = 0
        while self._running:
            if not self._connect():
                delay = _RECONNECT_BACKOFF[min(backoff_i, len(_RECONNECT_BACKOFF) - 1)]
                backoff_i += 1
                waited = 0.0
                while self._running and waited < delay:
                    time.sleep(0.2)
                    waited += 0.2
                continue
            try:
                sv = self._read_reg(self.sv_register) / self.sv_scale if self.sv_register else None
                stage = self._read_reg(self.stage_register) if self.stage_register else None
                self._sv = sv
                self._stage = stage
                if self.on_update is not None:
                    self.on_update(sv, stage)
                backoff_i = 0
            except Exception as e:
                self._last_error = str(e)
                log_event("sv_source", "Ошибка чтения СВ — переподключение", "warn", {"error": str(e)})
                self._close()
                continue
            time.sleep(self.period)
