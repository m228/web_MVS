"""Источник СВ (Brix) и стадии варки из ПЛК/аппарата по Modbus TCP или OPC UA — этап A.

ПЛК тут в роли «почтового ящика»: Python читает у него два числа, которые сам не
измеряет — СВ (густота утфеля) и стадию варки. Они нужны автомату, чтобы выбирать зазор
подвода SP[i] по СВ и период цикла по стадии.

Отдельное Modbus-соединение (это ДРУГОЕ устройство, не плата микроскопа). Читает медленно
(СВ меняется небыстро). При успешном чтении зовёт callback(sv, stage) — сервис пушит их в
автомат. Пока в конфиге `sv_source.enabled=false` — не запускается, работает ручной ввод.

Адреса/масштаб задаёт Макс в plate_config.json → sv_source (или со страницы, вкладка «ПЛК»):
  protocol = "modbus": host/port/unit, fields.<имя>.reg (+ scale, signed);
  protocol = "opcua":  opcua.endpoint / security / mode / auth, opcua.fields.<имя>.node (+ scale).
Протокол — целиком один на весь источник. По OPC UA только ЧТЕНИЕ (opcua_client.py). Дальше всё общее: фильтр сбоев,
клапаны, on_update.
"""
import threading
import time

from pymodbus.client import ModbusTcpClient

from logger import log_event

_RECONNECT_BACKOFF = [1, 2, 5, 10, 30]

INT_FIELDS = ("stage", "substage", "cook_time")     # по OPC UA могут прийти Float (5.0) — автомату и журналу нужны целые

VALVE_SLACK = 5.0     # обратка чуть за 0–100 % — шум датчика (у закрытого клапана в знаковом регистре бывает −1): это 0 / 100 %


def valve_pct(v):
    """Обратка клапана, % (0–100). Дальше VALVE_SLACK за пределами — не положение клапана, а мусор регистра (Кир, клапан воды: 65535,
    похоже −1 в беззнаковом чтении): значения нет — None, в дозу и «открыт» не идёт. Нет значения — None."""
    if v is None:
        return None
    v = float(v)
    if not -VALVE_SLACK <= v <= 100.0 + VALVE_SLACK:      # NaN тоже сюда
        return None
    return max(0.0, min(100.0, v))


class SvSource:
    def __init__(self, cfg, on_update=None, fallback_host=""):
        # cfg — раздел sv_source из общего конфига
        self.on_update = on_update
        self.protocol = "opcua" if str(cfg.get("protocol") or "modbus").strip().lower() == "opcua" else "modbus"
        self._opc_cfg = dict(cfg.get("opcua") or {})
        self.host = (cfg.get("host") or fallback_host or "").strip()
        if self.protocol == "opcua":
            self.host = str(self._opc_cfg.get("endpoint") or "").strip()     # адрес источника для статуса/журнала
        self.port = int(cfg.get("port", 502))
        self.unit = int(cfg.get("unit", 255))
        self.period = float(cfg.get("period_s", 1.0))

        # карта полей аппарата: name -> (reg, scale, signed). Из fields{} + совместимость sv/stage.
        # signed=true -> регистр читается как знаковый int16 (для разрежения/давления, которые
        # после REAL_TO_INT в ПЛК могут быть отрицательными и приходят в доп. коде).
        self.fields = {}
        if self.protocol == "opcua":
            # OPC UA: name -> (NodeId, scale, False). Пустой node — поле не читается («(—)»). Знак уже в типе тега (Int16/Float).
            for name, spec in (self._opc_cfg.get("fields") or {}).items():
                node = str((spec or {}).get("node") or "").strip()
                if node:
                    self.fields[name] = (node, float((spec or {}).get("scale", 1) or 1) or 1.0, False)
        for name, spec in ((cfg.get("fields") or {}) if self.protocol == "modbus" else {}).items():
            reg = int((spec or {}).get("reg", 0) or 0)
            if reg > 0:
                scale = float((spec or {}).get("scale", 1)) or 1.0
                signed = bool((spec or {}).get("signed", False))
                self.fields[name] = (reg, scale, signed)
        if self.protocol == "modbus" and "sv" not in self.fields and int(cfg.get("sv_register", 0) or 0) > 0:
            self.fields["sv"] = (int(cfg["sv_register"]), float(cfg.get("sv_scale", 100)) or 1.0, False)
        if self.protocol == "modbus" and "stage" not in self.fields and int(cfg.get("stage_register", 0) or 0) > 0:
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
        # чтение одним запросом на весь блок регистров (согласованный снимок, 1 запрос вместо 9); не получилось — по одному
        self._block_ok = bool(cfg.get("block_read", True))
        self.requests = 0                     # счётчик запросов к ПЛК (для проверки)
        self._sv_good, self._sv_bad_n, self._sv_bad_raw = None, 0, None
        self._stage_cur, self._stage_cand, self._stage_n = None, None, 0
        self._last_glitch_log = 0.0
        self._last_conn_log = 0.0
        # клапаны (поля «v_*», 0–100 %): копим между пробами дозу (∫ открытие·dt, %·с) и время открытия — проба раз в 1–2 мин,
        # а клапан открывают на минуты; по мгновенному снимку на пробе дозу не увидеть. Забирает take_valves() при записи пробы.
        self._valves_lock = threading.Lock()
        self._valves_acc = {}                 # name -> [доза %·с, секунд открыт]
        self._valves_prev = None              # (время опроса, {name: %}) — прошлое значение держится до следующего опроса
        self._close_lock = threading.Lock()   # _close зовут и поток опроса, и stop() из другого потока — закрыть клиент ровно один раз

    # ---------- жизненный цикл ----------

    def start(self):
        # без адресов полей стартовать незачем (нечего читать) — на странице будут «(—)»
        if self._running or not self.host or not self.fields:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="sv-source", daemon=True)
        self._thread.start()
        log_event("sv_source", "Источник данных аппарата (ПЛК) запущен", "info",
                  {"protocol": self.protocol, "host": self.host, "port": self.port, "fields": list(self.fields.keys())})

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
            "protocol": self.protocol,
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
                if self._sv_bad_n and self._sv_bad_raw is not None:    # странное значение не удержалось — это был сбой
                    self._note_glitch("СВ", self._sv_bad_raw, sv)
                self._sv_good, self._sv_bad_n, self._sv_bad_raw = sv, 0, None   # нормальное значение
            else:
                self._sv_bad_n += 1
                if self._sv_bad_n >= self.SV_CONFIRM:                  # продержалось — это не сбой, а настоящий скачок
                    self._sv_good, self._sv_bad_n, self._sv_bad_raw = sv, 0, None
                else:
                    self._sv_bad_raw = sv
                    v["sv"] = self._sv_good                            # пока держим последнее хорошее
        st = v.get("stage")
        if st is not None:
            if self._stage_cur is None or st == self._stage_cur:
                if self._stage_cand is not None:                       # новая стадия не удержалась — это был сбой
                    self._note_glitch("стадия", self._stage_cand, st)
                self._stage_cur, self._stage_cand, self._stage_n = st, None, 0
            else:
                self._stage_n = self._stage_n + 1 if st == self._stage_cand else 1
                self._stage_cand = st
                if self._stage_n >= self.STAGE_CONFIRM:
                    self._stage_cur, self._stage_cand, self._stage_n = st, None, 0
                else:
                    v["stage"] = self._stage_cur                       # пока держим прежнюю; в журнал — только если не удержится
        return v

    def _note_glitch(self, what, raw, kept):
        now = time.time()
        if now - self._last_glitch_log > 60.0:                          # не чаще раза в минуту
            self._last_glitch_log = now
            log_event("sv_source", "Разовый сбой чтения ПЛК отброшен: %s %s вместо %s" % (what, raw, kept), "warn", {"raw": raw, "kept": kept})

    def _close(self):
        with self._close_lock:
            client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        self._connected = False
        self._sock_up = False

    def _connect(self):
        if self.protocol == "opcua":
            return self._connect_opcua()
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

    def _connect_opcua(self):
        """Сессия OPC UA (вход и шифрование — по opcua_client). Не вышло — причина в status().error и в журнал раз в минуту."""
        if self._client is not None:
            return True
        from opcua_client import OpcUaConn, explain
        try:
            conn = OpcUaConn(self._opc_cfg)
            conn.connect()
        except Exception as e:
            self._last_error = explain(e)
            self._connected = False
            now = time.time()
            if now - self._last_conn_log > 60.0:
                self._last_conn_log = now
                log_event("sv_source", "OPC UA: нет подключения к серверу", "warn", {"host": self.host, "error": self._last_error})
            return False
        self._client = conn
        self._sock_up = True
        self._last_error = None
        if not self._running:                 # пока ждали сервер, источник остановили (перезапуск настроек) — сессию не держим
            self._close()                     # после присваивания: stop() мог закрыть «пусто» раньше, чем клиент появился
            return False
        return True

    def _value(self, name, raw, scale):
        """Сырое значение -> инж. величина. Modbus: слово int16/uint16 / scale. OPC UA: число тега / scale; стадия и т.п. — целым."""
        if self.protocol == "opcua":
            v = raw if scale == 1 else raw / scale
            if name in INT_FIELDS:
                return int(round(v))
            return round(v, 4) if isinstance(v, float) else v
        return raw if scale == 1 else raw / scale   # scale=1 -> целое (стадия)

    VALVE_OPEN_PCT = 2.0  # клапан «открыт», если обратка больше этого (обратка закрытого клапана не всегда ровно 0)
    VALVE_GAP_S = 10.0    # опросов не было дольше (обрыв связи) — этот отрезок в дозу не идёт: что было с клапаном, не знаем

    def _accumulate_valves(self, values, now):
        """Доза и время открытия клапанов (поля v_*) за отрезок от прошлого опроса до этого — по прошлому значению.
        Сироп и вода идут через коллектор и головной клапан продукта: если он читается и закрыт, в аппарат они не попадают — в дозу не идут.
        Значение клапана неизвестно (мусор регистра, valve_pct → None) — отрезок для него пропускаем, как обрыв связи: не «закрыт» и не «открыт»;
        неизвестен клапан продукта — пропускаем и воду с сиропом (попали ли они в аппарат, не знаем)."""
        cur = {k: valve_pct(v) for k, v in values.items() if k.startswith("v_")}
        with self._valves_lock:
            prev = self._valves_prev
            self._valves_prev = (now, cur)
            if not prev or not cur:
                return
            dt = now - prev[0]
            if dt <= 0 or dt > self.VALVE_GAP_S:
                return
            pv = prev[1].get("v_product", 100.0)          # клапан продукта не настроен — как открыт
            for k, v in prev[1].items():
                feed = k in ("v_water", "v_syrup")
                if v is None or (feed and pv is None):
                    continue
                acc = self._valves_acc.setdefault(k, [0.0, 0.0])
                if feed and pv <= self.VALVE_OPEN_PCT:
                    continue
                acc[0] += v * dt
                if v > self.VALVE_OPEN_PCT:
                    acc[1] += dt

    def adopt_valves(self, other):
        """Перенять накопленную дозу клапанов у прежнего источника (сохранили настройки ПЛК посреди варки — доза до пробы не теряется)."""
        if other is None:
            return
        with other._valves_lock:
            acc, prev = dict(other._valves_acc), other._valves_prev
        with self._valves_lock:
            self._valves_acc, self._valves_prev = acc, prev

    def take_valves(self):
        """Накопленное с прошлого вызова: {"water_dose": %·с, "water_s": с, "syrup_dose": …} (имя без «v_») и обнуление.
        Клапаны не настроены — {}; клапан весь отрезок неизвестен — его ключей нет (в журнале None, а не «0 — закрыт»)."""
        with self._valves_lock:
            acc, self._valves_acc = self._valves_acc, {}
        out = {}
        for k, (dose, open_s) in acc.items():
            out[k[2:] + "_dose"] = round(dose)
            out[k[2:] + "_s"] = round(open_s)
        return out

    BLOCK_MAX = 100       # блок длиннее — читаем по одному

    def _read_all(self):
        """Все нужные регистры -> {reg: raw}. Один запрос на весь блок [мин..макс] — снимок согласован по времени, запросов в 9 раз
        меньше; ПЛК не дал блок (исключение Modbus) — читаем по одному регистру. Сетевые ошибки идут выше (переподключение)."""
        if self.protocol == "opcua":                           # OPC UA: все теги одним запросом Read; плохой статус -> None
            self.requests += 1
            return self._client.read_many([node for node, _, _ in self.fields.values()])
        regs = sorted({reg for reg, _, _ in self.fields.values()})
        lo, hi = regs[0], regs[-1]
        if self._block_ok and hi - lo + 1 <= self.BLOCK_MAX:
            self.requests += 1
            rr = self._client.read_holding_registers(lo, count=hi - lo + 1, slave=self.unit)
            if not rr.isError() and len(rr.registers) == hi - lo + 1:
                return {reg: rr.registers[reg - lo] for reg in regs}
            self._block_ok = False
            log_event("sv_source", "ПЛК не отдал блок регистров %d..%d — читаем по одному" % (lo, hi), "warn", {"resp": repr(rr)})
        return {reg: self._read_reg(reg) for reg in regs}

    def _read_reg(self, addr):
        self.requests += 1
        rr = self._client.read_holding_registers(addr, count=1, slave=self.unit)
        if rr.isError():
            raise IOError("read reg %d error: %r" % (addr, rr))
        return rr.registers[0]

    def _loop(self):
        try:
            self._poll()
        finally:
            self._close()                     # вышли из цикла (stop) — сессию/сокет не оставляем висеть

    def _poll(self):
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
                raws = self._read_all()
                if not self._running:                   # остановлен, пока читали: устаревшие значения в автомат не отдаём
                    break
                for name, (reg, scale, signed) in self.fields.items():
                    raw = raws.get(reg)
                    if raw is None:                     # OPC UA: тег с плохим статусом — значения нет («(—)»)
                        values[name] = None
                        continue
                    if signed and raw >= 0x8000:        # int16 в доп. коде -> отрицательное
                        raw -= 0x10000
                    values[name] = self._value(name, raw, scale)
                    if name.startswith("v_"):
                        values[name] = valve_pct(values[name])     # обратка вне 0–100 % (65535) — значения нет: на странице «(—)», в пробу не идёт
                values = self._sanitize(values)
                self._values = values
                self._accumulate_valves(values, time.time())
                if self.on_update is not None:
                    self.on_update(values.get("sv"), values.get("stage"))
                backoff_i = 0
                if self.protocol == "opcua":            # сервер на связи, но ни один тег не читается — скорее всего неверные NodeId
                    self._last_error = None if any(v is not None for v in values.values()) else \
                        "ни один тег не читается — проверь теги (NodeId) на вкладке «ПЛК»"
                if not self._connected:   # живой ПЛК подтверждён чтением (не connect())
                    self._connected = True
                    log_event("sv_source", "Связь с источником СВ установлена", "success", {"host": self.host})
            except Exception as e:
                if not self._running:                   # штатная остановка (клиент закрыли из stop) — это не ошибка
                    break
                self._last_error = str(e)
                log_event("sv_source", "Ошибка чтения контроллера — переподключение", "warn", {"error": str(e)})
                self._close()
                if self.protocol == "opcua":            # сессия OPC UA открывается дорого — не долбим сервер в цикле «вошёл/ошибка»
                    time.sleep(min(self.period, 1.0))
                continue
            time.sleep(self.period)
