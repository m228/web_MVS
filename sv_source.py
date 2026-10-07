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
        self.period = float(cfg.get("period_s", 1.0))

        # карта полей аппарата: name -> (reg, scale, signed). Из fields{} + совместимость sv/stage.
        # signed=true -> регистр читается как знаковый int16 (для разрежения/давления, которые
        # после REAL_TO_INT в ПЛК могут быть отрицательными и приходят в доп. коде).
        self.fields = {}
        for name, spec in (cfg.get("fields") or {}).items():
            reg = int((spec or {}).get("reg", 0) or 0)
            if reg > 0:
                scale = float((spec or {}).get("scale", 1)) or 1.0
                signed = bool((spec or {}).get("signed", False))
                self.fields[name] = (reg, scale, signed)
        if "sv" not in self.fields and int(cfg.get("sv_register", 0) or 0) > 0:
            self.fields["sv"] = (int(cfg["sv_register"]), float(cfg.get("sv_scale", 100)) or 1.0, False)
        if "stage" not in self.fields and int(cfg.get("stage_register", 0) or 0) > 0:
            self.fields["stage"] = (int(cfg["stage_register"]), 1.0, False)

        self._client = None
        self._thread = None
        self._running = False

        # _sock_up — TCP-сокет поднят; _connected — реальная связь (подтверждена чтением).
        # В pymodbus 3.x connect() к мёртвому IP возвращает True, поэтому одного connect() мало.
        self._connected = False
        self._sock_up = False
        self._last_error = None
        self._values = {}
        # защита от разовых сбоев чтения (СВ вдруг 0 или 58 при 87, стадия на один опрос «3»): новое значение принимаем,
        # только если оно продержалось несколько опросов подряд. Реальный скачок принимается с задержкой в пару секунд.
        self.glitch_filter = bool(cfg.get("glitch_filter", True))
        self._sv_good, self._sv_bad_n = None, 0
        self._stage_cur, self._stage_cand, self._stage_n = None, None, 0
        self._last_glitch_log = 0.0

    # ---------- жизненный цикл ----------

    def start(self):
        # без адресов полей стартовать незачем (нечего читать) — на странице будут «(—)»
        if self._running or not self.host or not self.fields:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="sv-source", daemon=True)
        self._thread.start()
        log_event("sv_source", "Источник данных аппарата (ПЛК) запущен", "info",
                  {"host": self.host, "port": self.port, "fields": list(self.fields.keys())})

    def stop(self):
        self._running = False
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._close()

    def status(self):
        return {
            "connected": self._connected,
            "values": dict(self._values),          # {sv, stage, temp_app, vacuum, ...}
            "sv": self._values.get("sv"),
            "stage": self._values.get("stage"),
            "host": self.host,
            "error": self._last_error,
        }

    # ---------- внутреннее ----------

    SV_JUMP = 10.0        # скачок СВ больше этого (или СВ < 5) за один опрос — подозрение на сбой чтения
    SV_CONFIRM = 5        # столько опросов подряд должно держаться новое СВ, чтобы его принять как настоящее
    STAGE_CONFIRM = 3     # столько опросов подряд должна держаться новая стадия

    def _sanitize(self, values):
        """Отсечь разовые сбои чтения ПЛК: СВ и стадию. Остальные поля не трогаем. Возвращает значения для публикации."""
        if not self.glitch_filter:
            return values
        v = dict(values)
        sv = v.get("sv")
        if sv is not None:
            if self._sv_good is None or not (sv < 5 or abs(sv - self._sv_good) > self.SV_JUMP):
                self._sv_good, self._sv_bad_n = sv, 0                 # нормальное значение
            else:
                self._sv_bad_n += 1
                if self._sv_bad_n >= self.SV_CONFIRM:                  # продержалось — это не сбой, а настоящий скачок
                    self._sv_good, self._sv_bad_n = sv, 0
                else:
                    v["sv"] = self._sv_good                            # пока держим последнее хорошее
                    self._note_glitch("СВ", sv, self._sv_good)
        st = v.get("stage")
        if st is not None:
            if self._stage_cur is None or st == self._stage_cur:
                self._stage_cur, self._stage_cand, self._stage_n = st, None, 0
            else:
                self._stage_n = self._stage_n + 1 if st == self._stage_cand else 1
                self._stage_cand = st
                if self._stage_n >= self.STAGE_CONFIRM:
                    self._stage_cur, self._stage_cand, self._stage_n = st, None, 0
                else:
                    v["stage"] = self._stage_cur
                    self._note_glitch("стадия", st, self._stage_cur)
        return v

    def _note_glitch(self, what, raw, kept):
        now = time.time()
        if now - self._last_glitch_log > 60.0:                          # не чаще раза в минуту
            self._last_glitch_log = now
            log_event("sv_source", "Разовый сбой чтения ПЛК отброшен: %s %s вместо %s" % (what, raw, kept), "warn", {"raw": raw, "kept": kept})

    def _close(self):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        self._connected = False
        self._sock_up = False

    def _connect(self):
        # гвард по сокету, а не по _connected: connect() врёт (True к мёртвому IP)
        if self._client is not None and self._sock_up:
            return True
        self._close()
        client = ModbusTcpClient(self.host, port=self.port, timeout=1.0)
        if client.connect():
            self._client = client
            self._sock_up = True   # сокет есть; _connected подтвердит успешное чтение в _loop
            self._last_error = None
            return True
        try:
            client.close()
        except Exception:
            pass
        self._sock_up = False
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
                values = {}
                for name, (reg, scale, signed) in self.fields.items():
                    raw = self._read_reg(reg)
                    if signed and raw >= 0x8000:        # int16 в доп. коде -> отрицательное
                        raw -= 0x10000
                    values[name] = raw if scale == 1 else raw / scale   # scale=1 -> целое (стадия)
                values = self._sanitize(values)
                self._values = values
                if self.on_update is not None:
                    self.on_update(values.get("sv"), values.get("stage"))
                backoff_i = 0
                if not self._connected:   # живой ПЛК подтверждён чтением (не connect())
                    self._connected = True
                    log_event("sv_source", "Связь с источником СВ установлена", "success", {"host": self.host})
            except Exception as e:
                self._last_error = str(e)
                log_event("sv_source", "Ошибка чтения контроллера — переподключение", "warn", {"error": str(e)})
                self._close()
                continue
            time.sleep(self.period)
