"""Клиент платы микроскопа `micro` по Modbus TCP (Python = мастер).

Заменяет DIO-сканер M580: сам пишет выходной блок OUT[0..9] (регистры 1250..1259) и
читает входной блок IN[0..9] (1270..1279). Весь Modbus-IO живёт в ОДНОМ фоновом потоке
`_loop` (pymodbus-клиент не потокобезопасен) — внешние потоки трогают только выходной
буфер `_out` и снимок телеметрии `_telemetry`, оба под локом. Такт = poll_interval_ms
(по умолчанию 100 мс, как в ПЛК).

Здесь только транспорт (читать/писать регистры, здоровье связи, реконнект). Логика
автомата (перенос ST) — отдельно, в microscope_fsm.py (этап B1), поверх этого клиента.
"""
import collections
import threading
import time

from pymodbus.client import ModbusTcpClient

from logger import log_event

# бэкофф переподключения (сек), как у RTSP-воркера — не долбим плату на обрыве
_RECONNECT_BACKOFF = [1, 2, 5, 10, 30]

BOARD_CMD_REG = 1570      # регистр команд платы (рядом «Сохранить» 0x8888 — не трогаем)
BOARD_REBOOT = 0xAAAA     # чистый перезапуск без записи в EEPROM


class PlateClient:
    def __init__(self, config):
        self.cfg = config
        self._unit = int(config["unit"])
        self._read_base = int(config["read_base"])
        self._write_base = int(config["write_base"])
        self._block_len = int(config["block_len"])           # длина OUT-блока записи (1250..)
        self._read_len = int(config.get("read_len", config["block_len"]))   # длина блока чтения (может быть > OUT)
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
        # сокет, в который записан _out_last: новый сокет = плата могла перезагрузиться (см. _write_changed_out)
        self._out_sock = None
        # слова OUT из карты "out" — их ПК и переписывает после переподключения. Остальные
        # (1251/1252/1255/1256/1259) ПК не пишет: их смысл в прошивке не разобран, нули туда не льём.
        self._out_owned = sorted({int(i) for i in config.get("out", {}).values() if 0 <= int(i) < self._block_len})
        self._uptime_prev = None   # прошлый аптайм платы (1523): если стал меньше — плата перезапускалась
        self._last_write_warn = 0.0
        self._last_read_warn = 0.0

        # очередь одиночных записей вне OUT-блока (разрешение мотора #1219 и т.п.)
        self._pending = []
        self._pending_lock = threading.Lock()

        # снимок телеметрии (инженерные величины) + сырой блок IN
        self._telemetry = {}
        self._telemetry_lock = threading.Lock()

        # фильтр аналогового датчика перемещения (рег.1271): сглаживание дрожания (0/-50/70).
        # Фильтруем СЫРОЕ значение в ОДНОЙ точке; отфильтрованное инж.значение идёт и в
        # pos1_ai (телеметрия + доезд FSM + watchdog), и в ext.sensor (показ) — единый источник.
        sf = config.get("sensor_filter", {}) or {}
        self._sensor_filter_on = bool(sf.get("enabled", False))
        self._sensor_avg_sec = max(0.1, float(sf.get("avg_sec", 2)))
        self._sensor_eng = None   # последнее инж.значение датчика (signed*scale, при вкл — сглаженное)
        self._sensor_lock = threading.Lock()
        self._sensor_buf = collections.deque(maxlen=self._sensor_buf_len())

        # расширенная телеметрия (родные регистры платы) — читаем реже основного блока
        self._ext_reads = config.get("ext_reads", [])
        self._ext_map = config.get("ext_map", {})
        self._ext = {}
        self._ext_lock = threading.Lock()
        self._ext_counter = 0

        # здоровье связи
        # _sock_up  — TCP-сокет поднят (connect() ок). НЕ доказывает живое устройство:
        #             в pymodbus 3.x connect() к мёртвому IP возвращает True.
        # _connected — реальная связь: подтверждается ТОЛЬКО успешным чтением регистров.
        self._connected = False
        self._sock_up = False
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
        self._sock_up = False

    def _ensure_connected(self):
        # гвард — по факту наличия сокета (_sock_up), а НЕ по _connected: connect() в
        # pymodbus 3.x врёт (True к мёртвому IP), поэтому «связь» подтверждает только чтение.
        if self._client is not None and self._sock_up:
            return True
        self._close_client()
        client = ModbusTcpClient(
            self.cfg["host"], port=int(self.cfg["port"]), timeout=float(self.cfg["timeout_s"])
        )
        if client.connect():
            self._client = client
            self._sock_up = True   # сокет есть; _connected выставит успешное чтение в _loop
            self._last_error = None
            log_event("microscope_plc", "TCP-сокет платы открыт (ждём ответ на чтение)", "info",
                      {"host": self.cfg["host"], "port": self.cfg["port"]})
            return True
        try:
            client.close()
        except Exception:
            pass
        self._sock_up = False
        self._connected = False
        return False

    # ---------- фильтр датчика перемещения (1271) ----------

    def _sensor_buf_len(self):
        """Длина буфера скользящего среднего = окно avg_sec в тактах опроса (>=1)."""
        return max(1, int(round(self._sensor_avg_sec / self._period)))

    def set_sensor_filter(self, enabled=None, avg_sec=None):
        """Настройка фильтра датчика 1271 на лету (без reload). enabled — вкл/выкл сглаживания,
        avg_sec — окно усреднения (сек). Пересобирает буфер под новое окно."""
        with self._sensor_lock:
            if avg_sec is not None:
                self._sensor_avg_sec = max(0.1, float(avg_sec))
            if enabled is not None:
                self._sensor_filter_on = bool(enabled)
            self._sensor_buf = collections.deque(self._sensor_buf, maxlen=self._sensor_buf_len())
            if not self._sensor_filter_on:
                self._sensor_buf.clear()

    def _sensor_process(self, raw, scale):
        """Сырое слово 1271 -> инж.мкм: signed int16 + (опц.) скользящее среднее за avg_sec.
        ЕДИНАЯ точка: результат идёт и в pos1_ai (доезд/watchdog), и в ext.sensor (показ).
        signed: шум у нуля уходит в минус (а не в ~65500), иначе среднее и доезд ловят ложный скачок."""
        val = int(raw)
        if val >= 0x8000:
            val -= 0x10000
        eng = val * scale
        with self._sensor_lock:
            if self._sensor_filter_on:
                self._sensor_buf.append(eng)
                eng = sum(self._sensor_buf) / len(self._sensor_buf)
            self._sensor_eng = int(round(eng))
            return self._sensor_eng

    def _parse_telemetry(self, regs):
        """Сырой блок IN -> инженерные величины по карте in{off,scale} из конфига."""
        result = {"_raw": list(regs)}
        for name, spec in self.cfg["in"].items():
            off = int(spec["off"])
            scale = spec.get("scale", 1)
            if 0 <= off < len(regs):
                raw = regs[off]
                if spec.get("signed") and raw >= 0x8000:   # знаковый int16: у нуля позиция уходит в минус, а не в 655350
                    raw -= 0x10000
                result[name] = raw * scale
        # датчик перемещения 1271 (pos1_ai): signed + фильтр в одной точке (перекрывает сырой расчёт выше)
        ai_spec = self.cfg["in"].get("pos1_ai")
        if ai_spec is not None:
            off = int(ai_spec["off"])
            if 0 <= off < len(regs):
                result["pos1_ai"] = self._sensor_process(regs[off], ai_spec.get("scale", 1))
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
                if spec.get("signed") and raw >= 0x8000:   # знаковый int16 (напр. температура ниже 0)
                    raw -= 0x10000
                ext[name] = raw * spec.get("scale", 1) if spec.get("kind") in ("um", "num") else raw
        # датчик перемещения (1271) — из ЕДИНОЙ фильтрованной точки, согласовано с pos1_ai
        if self._sensor_eng is not None:
            ext["sensor"] = self._sensor_eng
        return ext

    def write_reg(self, addr, value):
        """Одиночная запись регистра ВНЕ OUT-блока (напр. разрешение мотора #1219).
        Ставится в очередь и выполняется в потоке опроса."""
        with self._pending_lock:
            self._pending.append((int(addr), int(value) & 0xFFFF))

    def reboot(self):
        """Перезапустить плату: команда 0xAAAA в регистр команд 1570 (так же шлёт кнопка «Перезагрузить» её страницы,
        проверено по HAR 03.11). Плата сама гасит регистр и через ~1 с перезагружается, возвращается за ~5 с; наши слова OUT
        после неё переписываются на новом сокете (_write_changed_out). Другие значения в 1570 здесь писать нельзя:
        0xBBBB, 0xCCCC, 0x9876, 0x4353 затирают EEPROM."""
        self.write_reg(BOARD_CMD_REG, BOARD_REBOOT)

    def _warn_write(self, msg, payload):
        now = time.time()
        if now - self._last_write_warn > 2.0:   # не чаще раза в 2 с — чтобы не флудить лог
            self._last_write_warn = now
            log_event("microscope_plc", msg, "warn", payload)

    def _warn_read(self, msg, payload):
        now = time.time()
        if now - self._last_read_warn > 3.0:    # не чаще раза в 3 с — иначе лог тонет во флуде
            self._last_read_warn = now
            log_event("microscope_plc", msg, "warn", payload)

    def _restore_after_restart(self):
        """Плата перезапустилась (или мы только подключились): все наши слова OUT пишем заново, подсветку и её частоту
        возвращаем такими, какими их оставили (см. _restore_led)."""
        for i in self._out_owned:
            self._out_last[i] = None
        self._restore_led()

    def _restore_led(self):
        """Последние яркость, «вкл» и частота LED — из конфига (их туда сохраняет led_native, он же держит автомат в том же
        состоянии). Яркость и «вкл» кладём в OUT-блок: в РУЧНОМ режиме автомат его не обновляет, и после перезапуска туда
        ушли бы старые слова. Частота (1202) лежит вне OUT-блока — одиночной записью. Не задано (0) — не трогаем."""
        c = self.cfg
        if "led_bright" in c.get("out", {}) and c.get("led_bright") is not None:
            self.set_out(self._out_idx("led_bright"), int(c["led_bright"]))
            self._out_last[self._out_idx("led_bright")] = None    # не «уже записано»: плата могла остаться с нулём
        if "led_on" in c.get("out", {}) and c.get("led_on") is not None:
            self.set_out_bit(self._out_idx("led_on"), 0, bool(c["led_on"]))
            self._out_last[self._out_idx("led_on")] = None
        freq = int(c.get("led_freq") or 0)
        reg = c.get("led", {}).get("freq")
        if freq > 0 and reg is not None:
            self.write_reg(int(reg), freq)

    def led_mismatch(self):
        """Что реально стоит в плате (читаем в ext: 1257 яркость, 1202 частота) против сохранённого в конфиге.
        {} — совпадает или плата это не отдала; иначе {"bright": (должно, стоит), "freq": (должно, стоит)}."""
        c, ext, diff = self.cfg, self.ext, {}
        want_b, got_b = c.get("led_bright"), ext.get("led_bright")
        if c.get("led_on") and want_b and got_b is not None and int(got_b) != int(want_b):
            diff["bright"] = (int(want_b), int(got_b))
        want_f, got_f = int(c.get("led_freq") or 0), ext.get("led_freq")
        if want_f > 0 and got_f is not None and int(got_f) != want_f:
            diff["freq"] = (want_f, int(got_f))
        return diff

    def reapply_led(self):
        """Если подсветка в плате разошлась с сохранённой (на странице 12 %, а по факту 0 и картинка чёрная) — записать
        сохранённые значения заново. Возвращает, что расходилось ({} — всё совпало, ничего не писали)."""
        diff = self.led_mismatch()
        if diff:
            self._restore_led()
        return diff

    def _check_restart(self, ext):
        """Аптайм платы (1523) стал меньше прежнего — она перезапустилась (кнопкой, нашим ребутом, питанием). Сокет при этом
        мог не смениться, а записи в момент загрузки терялись, поэтому восстанавливаем уже по живому ответу. Раз в 18 ч
        аптайм переходит через 65536 и тоже «уменьшается» — тогда мы лишь повторно пишем те же значения."""
        up = ext.get("uptime_s")
        if not isinstance(up, (int, float)):
            return
        if self._uptime_prev is not None and up + 10 < self._uptime_prev:
            log_event("microscope_plc", "Плата перезапустилась — возвращаю яркость, частоту и выходы", "info",
                      {"uptime_s": up, "was": self._uptime_prev})
            self._restore_after_restart()
        self._uptime_prev = up

    def _write_changed_out(self):
        # pymodbus 3.x после сбоя сам переоткрывает сокет внутри execute() — _connected и
        # _ensure_connected перезагрузку платы не видят. А плата после неё стоит с умолчаниями
        # (1253 = 0): повтор goto (cmd1) повёл бы М1 в 0, к стеклу. Поэтому на НОВЫЙ сокет
        # забываем, что записано, — наши слова OUT перепишутся целиком в этом же проходе.
        sock = getattr(self._client, "socket", None)
        if sock is not None and sock is not self._out_sock:
            self._out_sock = sock
            self._restore_after_restart()
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
                # если сокет переоткроется на самой команде, goto уйдёт в свежую плату раньше цели
                # в 1253 — поэтому подключаемся заранее и на новый сокет сперва пишем блок OUT
                if not self._client.connect():
                    raise IOError("нет соединения с платой")
                if self._client.socket is not self._out_sock:
                    self._write_changed_out()
                wr = self._client.write_register(int(addr), value, slave=self._unit)
                if wr.isError():
                    raise IOError("isError")
            except Exception as e:
                self._warn_write("Ошибка одиночной записи регистра (не критично)",
                                 {"reg": addr, "value": value, "error": str(e)})

    def _loop(self):
        backoff_i = 0
        read_fails = 0
        while self._running:
            if not self._ensure_connected():
                # не смогли ПОДКЛЮЧИТЬСЯ (TCP) — бэкофф, нарезанный по 0.2 с для быстрого стопа
                self._reconnecting = True
                delay = _RECONNECT_BACKOFF[min(backoff_i, len(_RECONNECT_BACKOFF) - 1)]
                backoff_i += 1
                waited = 0.0
                while self._running and waited < delay:
                    time.sleep(0.2)
                    waited += 0.2
                continue

            cycle_start = time.time()

            # 1) ЧТЕНИЕ телеметрии. ВАЖНО: сбой чтения НЕ рвёт связь сразу и НЕ блокирует запись
            #    команд. Раньше на ошибке чтения делался `continue` до записи — и если плата молчит
            #    на чтении (напр. занята вторым мастером/неверный unit), КОМАНДЫ ВООБЩЕ НЕ УХОДИЛИ.
            #    Теперь: читаем, при неудаче — троттлим лог; всё равно шлём команды; реконнект только
            #    после нескольких неудач подряд (сокет реально мёртв). Флуд «WARN/SUCCESS» устранён.
            try:
                rr = self._client.read_holding_registers(self._read_base, count=self._read_len, slave=self._unit)
                if rr.isError():
                    raise IOError("read error: %r" % rr)
                telem = self._parse_telemetry(rr.registers)
                with self._telemetry_lock:
                    self._telemetry = telem
                self._last_ok = time.time()
                self._poll_count += 1
                backoff_i = 0
                read_fails = 0
                self._reconnecting = False
                self._last_error = None
                if not self._connected:   # реальное подтверждение живого устройства (не connect())
                    self._connected = True
                    log_event("microscope_plc", "Связь с платой установлена", "success",
                              {"host": self.cfg["host"], "port": self.cfg["port"]})

                # расширенная телеметрия — реже основного такта (раз в ~5 циклов)
                self._ext_counter += 1
                if self._ext_reads and self._ext_counter >= 5:
                    self._ext_counter = 0
                    ext = self._read_ext()
                    with self._ext_lock:
                        self._ext = ext
                    self._check_restart(ext)
            except Exception as e:
                read_fails += 1
                self._last_error = str(e)
                self._warn_read("Ошибка чтения платы (команды всё равно шлём; проверь unit/второй мастер)",
                                {"error": str(e), "fails_in_row": read_fails, "unit": self._unit})

            # 2) ЗАПИСЬ команд/выходов — ВСЕГДА, даже если чтение не прошло (кнопки должны работать).
            self._write_changed_out()
            self._flush_pending()

            # 3) сокет мёртв (много неудач чтения подряд) -> пересоздать соединение
            if read_fails >= 5:
                self._reconnecting = True
                self._close_client()
                read_fails = 0

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
