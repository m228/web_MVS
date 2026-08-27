"""Клиент платы микроскопа `micro` по Modbus TCP (Python = мастер).

Заменяет DIO-сканер M580: сам пишет выходной блок OUT[0..9] (регистры 1250..1259) и
читает входной блок IN[0..9] (1270..1279). Весь Modbus-IO живёт в ОДНОМ фоновом потоке
`_loop` (pymodbus-клиент не потокобезопасен) — внешние потоки трогают только выходной
буфер `_out` и снимок телеметрии `_telemetry`, оба под локом. Такт = poll_interval_ms
(по умолчанию 100 мс, как в ПЛК).

Здесь только транспорт (читать/писать регистры, здоровье связи, реконнект). Логика
автомата (перенос ST) — отдельно, в microscope_fsm.py (этап B1), поверх этого клиента.
"""
import threading
import time

from pymodbus.client import ModbusTcpClient

from logger import log_event

# бэкофф переподключения (сек), как у RTSP-воркера — не долбим плату на обрыве
_RECONNECT_BACKOFF = [1, 2, 5, 10, 30]


class PlateClient:
    def __init__(self, config):
        self.cfg = config
        self._unit = int(config["unit"])
        self._read_base = int(config["read_base"])
        self._write_base = int(config["write_base"])
        self._block_len = int(config["block_len"])
        self._period = max(0.02, int(config["poll_interval_ms"]) / 1000.0)

        self._client = None
        self._thread = None
        self._running = False

        # выходной буфер (OUT[0..9]) — пишется целиком каждый такт, как делал ПЛК
        self._out = [0] * self._block_len
        self._out_lock = threading.Lock()

        # снимок телеметрии (инженерные величины) + сырой блок IN
        self._telemetry = {}
        self._telemetry_lock = threading.Lock()

        # расширенная телеметрия (родные регистры платы) — читаем реже основного блока
        self._ext_reads = config.get("ext_reads", [])
        self._ext_map = config.get("ext_map", {})
        self._ext = {}
        self._ext_lock = threading.Lock()
        self._ext_counter = 0

        # здоровье связи
        self._connected = False
        self._reconnecting = False
        self._last_ok = None
        self._last_error = None
        self._poll_count = 0

    # ---------- жизненный цикл ----------

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="plate-modbus", daemon=True)
        self._thread.start()
        log_event("microscope_plc", "Клиент платы запущен", "info",
                  {"host": self.cfg["host"], "port": self.cfg["port"], "unit": self._unit})

    def stop(self):
        self._running = False
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._close_client()
        log_event("microscope_plc", "Клиент платы остановлен", "info")

    # ---------- выходы (вызывают внешние потоки / автомат) ----------

    def set_out(self, index, value):
        """Записать целое слово в OUT[index] (0..block_len-1)."""
        with self._out_lock:
            if 0 <= index < self._block_len:
                self._out[index] = int(value) & 0xFFFF

    def set_out_bit(self, index, bit, on):
        """Установить/снять бит в слове OUT[index]."""
        with self._out_lock:
            if 0 <= index < self._block_len:
                if on:
                    self._out[index] |= (1 << bit)
                else:
                    self._out[index] &= ~(1 << bit) & 0xFFFF

    def get_out(self):
        with self._out_lock:
            return list(self._out)

    # --- удобные обёртки под раскладку OUT из конфига ---

    def _out_idx(self, name):
        return int(self.cfg["out"][name])

    def cmd1(self, value):
        self.set_out(self._out_idx("cmd1"), value)

    def cmd2(self, value):
        self.set_out(self._out_idx("cmd2"), value)

    def write_m1_sp(self, micrometers):
        # плата хранит позицию в единицах 10 мкм: OUT[3] = m1_SP/10 (как в ПЛК)
        self.set_out(self._out_idx("m1_sp"), int(micrometers) // 10)

    def write_m2_sp(self, micrometers):
        self.set_out(self._out_idx("m2_sp"), int(micrometers) // 10)

    def set_led(self, bright=None, on=None):
        if bright is not None:
            self.set_out(self._out_idx("led_bright"), bright)
        if on is not None:
            self.set_out_bit(self._out_idx("led_on"), 0, bool(on))

    def set_valve_tube(self, on):
        self.set_out_bit(self._out_idx("valves"), 0, bool(on))

    def set_valve_glass(self, on):
        self.set_out_bit(self._out_idx("valves"), 1, bool(on))

    # ---------- телеметрия / статус (снимки) ----------

    @property
    def telemetry(self):
        with self._telemetry_lock:
            return dict(self._telemetry)

    @property
    def ext(self):
        with self._ext_lock:
            return dict(self._ext)

    @property
    def status(self):
        return {
            "connected": self._connected,
            "reconnecting": self._reconnecting,
            "last_ok": self._last_ok,
            "error": self._last_error,
            "poll_count": self._poll_count,
            "host": self.cfg["host"],
            "port": self.cfg["port"],
            "unit": self._unit,
        }

    # ---------- внутреннее ----------

    def _close_client(self):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        self._connected = False

    def _ensure_connected(self):
        if self._client is not None and self._connected:
            return True
        self._close_client()
        client = ModbusTcpClient(
            self.cfg["host"], port=int(self.cfg["port"]), timeout=float(self.cfg["timeout_s"])
        )
        if client.connect():
            self._client = client
            self._connected = True
            self._reconnecting = False
            self._last_error = None
            log_event("microscope_plc", "Связь с платой установлена", "success",
                      {"host": self.cfg["host"], "port": self.cfg["port"]})
            return True
        try:
            client.close()
        except Exception:
            pass
        self._connected = False
        return False

    def _parse_telemetry(self, regs):
        """Сырой блок IN -> инженерные величины по карте in{off,scale} из конфига."""
        result = {"_raw": list(regs)}
        for name, spec in self.cfg["in"].items():
            off = int(spec["off"])
            scale = spec.get("scale", 1)
            if 0 <= off < len(regs):
                result[name] = regs[off] * scale
        return result

    def _read_ext(self):
        """Прочитать доп. регистры платы (блоками ext_reads) и разложить по ext_map."""
        values = {}
        for base, count in self._ext_reads:
            try:
                rr = self._client.read_holding_registers(int(base), count=int(count), slave=self._unit)
                if not rr.isError():
                    for i, v in enumerate(rr.registers):
                        values[int(base) + i] = v
            except Exception:
                pass
        ext = {}
        for name, spec in self._ext_map.items():
            reg = int(spec["reg"])
            if reg in values:
                raw = values[reg]
                ext[name] = raw * spec.get("scale", 1) if spec.get("kind") in ("um", "num") else raw
        return ext

    def _loop(self):
        backoff_i = 0
        while self._running:
            if not self._ensure_connected():
                # не смогли подключиться — бэкофф, нарезанный по 0.2 с для быстрого стопа
                self._reconnecting = True
                delay = _RECONNECT_BACKOFF[min(backoff_i, len(_RECONNECT_BACKOFF) - 1)]
                backoff_i += 1
                waited = 0.0
                while self._running and waited < delay:
                    time.sleep(0.2)
                    waited += 0.2
                continue

            cycle_start = time.time()
            try:
                # 1) пишем весь выходной блок (как ПЛК-сканер каждый скан)
                with self._out_lock:
                    out = list(self._out)
                wr = self._client.write_registers(self._write_base, out, slave=self._unit)
                if wr.isError():
                    raise IOError("write_registers error: %r" % wr)

                # 2) читаем входной блок
                rr = self._client.read_holding_registers(self._read_base, count=self._block_len, slave=self._unit)
                if rr.isError():
                    raise IOError("read_holding_registers error: %r" % rr)

                telem = self._parse_telemetry(rr.registers)
                with self._telemetry_lock:
                    self._telemetry = telem
                self._last_ok = time.time()
                self._poll_count += 1
                backoff_i = 0
                self._reconnecting = False

                # расширенная телеметрия — реже основного такта (раз в ~5 циклов)
                self._ext_counter += 1
                if self._ext_reads and self._ext_counter >= 5:
                    self._ext_counter = 0
                    ext = self._read_ext()
                    with self._ext_lock:
                        self._ext = ext

            except Exception as e:
                self._last_error = str(e)
                log_event("microscope_plc", "Ошибка обмена с платой — переподключение", "warn",
                          {"error": str(e)})
                self._close_client()
                continue

            # выдержать такт (учитывая потраченное на обмен)
            elapsed = time.time() - cycle_start
            if elapsed < self._period:
                time.sleep(self._period - elapsed)


# ручная проверка B0: python microscope_plc.py  (нужен запущенный эмулятор plate_sim.py)
if __name__ == "__main__":
    import plate_config

    cfg = plate_config.load()
    cfg["host"] = "127.0.0.1"
    cfg["port"] = 15020
    client = PlateClient(cfg)
    client.start()
    try:
        time.sleep(1.5)
        print("Отвод М1 в 40000 мкм...")
        client.write_m1_sp(40000)
        client.cmd1(0x1006)
        for _ in range(10):
            time.sleep(0.5)
            print("status:", client.status["connected"], "telemetry:", client.telemetry)
    finally:
        client.stop()
