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
        self._motor = config.get("motor", {})   # нативная карта команд моторов (1248/1249 и т.д.)

        self._client = None
        self._thread = None
        self._running = False

        # выходной буфер (OUT[0..9]); пишем ТОЛЬКО изменившиеся регистры по одному (FC06),
        # чтобы в простое записей не было и одна плохая запись не рвала связь
        self._out = [0] * self._block_len
        self._out_last = [0] * self._block_len   # что реально записано в плату
        self._out_lock = threading.Lock()
        self._last_write_warn = 0.0

        # очередь одиночных записей вне OUT-блока (разрешение мотора #1219 и т.п.)
        self._pending = []
        self._pending_lock = threading.Lock()

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
        # команда мотору М1 идёт в НАТИВНЫЙ регистр (1248), одиночной записью, а не в OUT-блок
        self._write_motor_cmd_reg("1", value)

    def cmd2(self, value):
        self._write_motor_cmd_reg("2", value)

    def write_m1_sp(self, micrometers):
        # плата хранит позицию в единицах 10 мкм: OUT[3]=1253 = m1_SP/10 (как в ПЛК)
        self.set_out(self._out_idx("m1_sp"), int(micrometers) // 10)

    def write_m2_sp(self, micrometers):
        self.set_out(self._out_idx("m2_sp"), int(micrometers) // 10)

    # --- нативное управление моторами (как родной конфигуратор: значение -> команда в 1248/1249) ---

    def _write_motor_cmd_reg(self, m, value):
        reg = (self._motor.get("cmd_reg") or {}).get(str(m))
        if reg is not None:
            self.write_reg(int(reg), int(value) & 0xFFFF)

    def motor_code(self, m, op):
        """Код команды: cmd_base[мотор] + cmd_op[операция] (напр. М1 goto = 0x1006)."""
        base = (self._motor.get("cmd_base") or {}).get(str(m), 0)
        off = (self._motor.get("cmd_op") or {}).get(op)
        return None if off is None else (int(base) + int(off))

    def _write_native(self, key, m, value):
        reg = (self._motor.get(key) or {}).get(str(m))
        if reg is not None:
            self.write_reg(int(reg), int(value) & 0xFFFF)

    def motor_cmd(self, m, op):
        """Послать мотору m команду-операцию op (find_zero/steps/home_start/.../goto/stop/set_zero)."""
        code = self.motor_code(m, op)
        if code is not None:
            self._write_motor_cmd_reg(m, code)

    def motor_goto(self, m, micrometers):
        div = int(self._motor.get("um_div", 10)) or 1
        self._write_native("pos_set", m, int(micrometers) // div)   # значение позиции ...
        self.motor_cmd(m, "goto")                                    # ... затем команда «идти»

    def motor_steps(self, m, n):
        self._write_native("steps", m, int(n))
        self.motor_cmd(m, "steps")

    def motor_shift(self, m, micrometers):
        div = int(self._motor.get("um_div", 10)) or 1
        self._write_native("shift", m, int(micrometers) // div)
        self.motor_cmd(m, "shift")

    def motor_stop(self, m):
        self.motor_cmd(m, "stop")

    def motor_find_zero(self, m):
        self.motor_cmd(m, "find_zero")

    def motor_set_zero(self, m):
        self.motor_cmd(m, "set_zero")

    def motor_home(self, m, which):
        self.motor_cmd(m, "home_end" if which == "end" else "home_start")

    def motor_direction(self, m, forward):
        self._write_native("dir", m, 1 if forward else 0)

    def motor_enable(self, m, on):
        self._write_native("enable", m, 1 if on else 0)

    def set_led_native(self, bright=None, freq=None, on=None):
        led = self.cfg.get("led", {})
        if bright is not None and led.get("bright") is not None:
            self.write_reg(int(led["bright"]), int(bright))
        if freq is not None and led.get("freq") is not None:
            self.write_reg(int(led["freq"]), int(freq))
        if on is not None and led.get("on_bit") is not None:
            self.write_reg(int(led["on_bit"]), 1 if on else 0)

    def set_dq_bit(self, bit, on):
        """Установить/снять один бит DQ (регистр 1250) через OUT-блок (read-modify-write по буферу)."""
        self.set_out_bit(self._out_idx("valves"), int(bit), bool(on))

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

    def write_reg(self, addr, value):
        """Одиночная запись регистра ВНЕ OUT-блока (напр. разрешение мотора #1219).
        Ставится в очередь и выполняется в потоке опроса."""
        with self._pending_lock:
            self._pending.append((int(addr), int(value) & 0xFFFF))

    def _warn_write(self, msg, payload):
        now = time.time()
        if now - self._last_write_warn > 2.0:   # не чаще раза в 2 с — чтобы не флудить лог
            self._last_write_warn = now
            log_event("microscope_plc", msg, "warn", payload)

    def _write_changed_out(self):
        with self._out_lock:
            out = list(self._out)
        for i, v in enumerate(out):
            if v == self._out_last[i]:
                continue
            addr = self._write_base + i
            try:
                wr = self._client.write_register(addr, int(v) & 0xFFFF, slave=self._unit)
                if wr.isError():
                    raise IOError("isError")
                self._out_last[i] = v
            except Exception as e:
                # помечаем как записанное, чтобы не долбить каждый такт (иначе флуд/стоп цикла);
                # если запись реально нужна — команда изменит значение снова
                self._out_last[i] = v
                self._warn_write("Ошибка записи регистра платы (не критично)",
                                 {"reg": addr, "value": v, "error": str(e)})

    def _flush_pending(self):
        with self._pending_lock:
            pending = self._pending
            self._pending = []
        for addr, value in pending:
            try:
                wr = self._client.write_register(int(addr), value, slave=self._unit)
                if wr.isError():
                    raise IOError("isError")
            except Exception as e:
                self._warn_write("Ошибка одиночной записи регистра (не критично)",
                                 {"reg": addr, "value": value, "error": str(e)})

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

            # 1) ЧТЕНИЕ телеметрии — первым и НЕЗАВИСИМО от записи. Связь/телеметрия не должны
            #    падать из-за проблем с записью. Ошибка чтения = реальный обрыв -> реконнект.
            try:
                rr = self._client.read_holding_registers(self._read_base, count=self._block_len, slave=self._unit)
                if rr.isError():
                    raise IOError("read error: %r" % rr)
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
                log_event("microscope_plc", "Ошибка чтения платы — переподключение", "warn", {"error": str(e)})
                self._close_client()
                continue

            # 2) ЗАПИСЬ только изменившихся регистров OUT, по одному (FC06). В простое записей нет.
            #    Ошибка записи логируется (не чаще раза в 2 с) и НЕ рвёт связь.
            self._write_changed_out()
            # 3) отложенные одиночные записи вне OUT-блока (разрешение мотора и т.п.)
            self._flush_pending()

            # выдержать такт
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
