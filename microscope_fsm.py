"""Автомат микроскопа — перенос ST-программы M580 на Python (этап B1).

Это «мозг»: та же логика, что крутил ПЛК, но поверх PlateClient (Python = мастер).
Такт `tick()` = 100 мс (как скан ПЛК). Имена переменных намеренно близки к ST
(`mode`, `t`, `t1`, `t2`, `u`, `m1_sp`, `cmd1`, `cw0/cw1`, `sw0..sw3`) — чтобы легко
сверять с оригиналом.

Цикл пробы (case mode), 1:1 с ST:
  0  — ожидание
  10 — отвод LED от стекла в SP[0] (~40 мм)
  11 — пауза 500 мс + включить промывку трубки (CW.0)
  12 — подвод в SP[i] (зазор по СВ), выключить клапан по приходу / pos1_ai<20
  13 — резерв

Отличие от ST (осознанное, см. Docs/microscope_code_guide.md): в ST таймер периода `t`
в простое не увеличивался, из-за чего циклический режим (SW.0) фактически не запускался
(автор пометил «пока не работает в цикле»). Здесь период считает отдельный `cycle_t`,
поэтому циклический режим реально работает. `t` остаётся таймером ШАГА внутри цикла.

Источник СВ/стадии на этапе B — ручной (set_sv/set_stage) или из конфига; на этапе A
заменится чтением из ПЛК. Без СВ подвод идёт по SP[1].
"""
import threading
import time
from datetime import datetime

from logger import log_event

CMD_SET_SP1 = 0x1006   # команда мотору М1 «установить SP»
CMD_SET_SP2 = 0x2006   # команда мотору М2 «установить SP»

# допуск «мотор доехал» (мкм). Реальная плата почти никогда не встаёт РОВНО в цель
# (энкодер даёт 39990 вместо 40000) — точное pos==цель почти не срабатывает, и шаг
# цикла ждал полный таймаут 10 с. Считаем «доехал», если в пределах допуска.
ARRIVE_TOL_UM = 50   # доезд: энкодер в пределах ±50 мкм от цели
GLASS_AI = 20        # pos1_ai < GLASS_AI = аналог «у стекла» (страховка на подводе)
ARRIVE_HOLD_TICKS = 20   # держать в диапазоне ±50 перед переходом (2 с при такте 100 мс)

# Повтор команды «идти» в цикле: плата за ОДИН goto делает лишь шаг к цели (как ручная
# кнопка «Идти»), поэтому в шагах движения цикла повторяем импульс каждые REDRIVE_TICKS
# тиков (≈0.7 с) до доезда — иначе мотор делает один шаг и стоит («не идёт по заданию»).
REDRIVE_TICKS = 7

# Новый цикл пробы «Взять пробу»: отвод -> промывка -> подвод -> выдержка(скрины+видео) -> возврат.
STEP_NAMES = {0: "ожидание", 20: "отвод", 21: "промывка перед пробой",
              22: "подвод к стеклу", 23: "проба · выдержка", 24: "возврат"}

# Диапазон стадий варки (M.mode), в котором авто-цикл пробы разрешён:
# 3 Сгущение … 9 Готовность. Вне диапазона (1 Остановлен, 2 Набор, 10+ Выгрузка и т.д.)
# новый цикл не стартует. Уже идущий цикл доводится до конца (проб не рвём — безопасность).
CYCLE_STAGE_MIN = 3
CYCLE_STAGE_MAX = 9


class MicroscopeFSM:
    def __init__(self, plate, config):
        self.plate = plate
        self.cfg = config
        self.SP = list(config["SP"])
        self.SVSP = list(config["SVSP"])
        # FOCUS[i] — позиция фокуса М2 (мкм) по тому же индексу СВ, что и SP/SVSP.
        # 0 = не двигать фокус на этом пороге. Длину выравниваем под SP (старые конфиги без FOCUS).
        focus = config.get("FOCUS") or []
        self.FOCUS = [int(focus[i]) if i < len(focus) else 0 for i in range(len(self.SP))]
        # использовать ли фокус М2 по таблице СВ. По умолчанию НЕ используем (безопасно для
        # старых конфигов). Галочка «Не использовать фокус» на вкладке «СВ/МКМ».
        self._ignore_focus = bool(config.get("probe_cycle", {}).get("ignore_focus", True))
        hw = config.get("hourly_wash", {})
        self._hw_enabled = bool(hw.get("enabled", True))
        self._hw_minute = int(hw.get("minute", 3))
        self._hw_on = int(hw.get("sec_on", 30))
        self._hw_off = int(hw.get("sec_off", 59))

        # параметры нового цикла пробы (правятся на вкладке «Цикл», persist в plate_config)
        pc = config.get("probe_cycle", {})
        self._retract_pos = int(pc.get("retract_pos", 20000))       # отвод/возврат, мкм
        self._pre_wash_sec = int(pc.get("pre_wash_sec", 4))         # промывка перед подводом, с
        self._dwell_sec = int(pc.get("dwell_sec", 15))             # выдержка пробы, с
        self._shot_interval_sec = max(1, int(pc.get("shot_interval_sec", 3)))  # период скринов, с
        self._pause_sec = max(0, int(pc.get("pause_sec", 60)))     # пауза между пробами (режим "time"), с
        # режим повтора пробы внутри цикла: "time" (по паузе) или "sv" (по целому СВ в [from..to])
        self._trigger_mode = "sv" if str(pc.get("trigger_mode", "time")) == "sv" else "time"
        self._sv_from = float(pc.get("sv_from", 84))
        self._sv_to = float(pc.get("sv_to", 92))
        # «варить без стадии»: при ручной варке ПЛК не двигает стадию (стоит), а СВ растёт —
        # тогда авто-цикл гоняем БЕЗ проверки стадии 3..9 (только по триггеру время/СВ).
        self._ignore_stage = bool(pc.get("ignore_stage", False))
        self._last_sv_shot = None    # последнее целое СВ, на котором взяли пробу (режим "sv")
        # подтверждение выхода из ручного при старте варки: если стадия ВХОДИТ в рабочую зону
        # (3..9), а мы в ручном — поднимаем флаг, UI спрашивает оператора «перейти в Автомат?».
        self._manual_confirm = False
        self._stage_prev_in = False   # была ли стадия в рабочей зоне на прошлом такте (для фронта)

        # довод подвода по абсолютнику (гибридный доезд mode 22): грубо по расчётной абс.позиции
        # 1274, точно по датчику 1271. Параметры правятся на вкладке «Цикл».
        fa = config.get("fine_approach", {}) or {}
        self._fa_enabled = bool(fa.get("enabled", False))
        self._fa_coarse_tol = int(fa.get("coarse_tol_um", 100))
        self._fa_fine_tol = int(fa.get("fine_tol_um", 20))
        self._fa_max_retry = int(fa.get("max_retry", 3))
        self._fa_pause_sec = max(0.5, float(fa.get("pause_sec", 3)))
        # масштаб датчика 1271 для ЛОГИКИ доезда (тот же коэффициент, что и показ на странице):
        # датчик отдаёт значение в ~10 раз крупнее реальных мкм — приводим к масштабу задания/
        # расчётной позиции, иначе дельта «задание−датчик» огромная (тысячи) и подгон не сходится.
        self._sensor_scale = float(config.get("sensor_display_scale", 1.0) or 1.0)

        # автокалибровка нуля М1 (раз в every_n варок на пропарке — стадия 11)
        ac = config.get("autocal", {}) or {}
        self._ac_enabled = bool(ac.get("enabled", False))
        self._ac_every_n = max(1, int(ac.get("every_n", 3)))
        self._ac_sensor_lo = int(ac.get("sensor_lo", 0))
        self._ac_sensor_hi = int(ac.get("sensor_hi", 30))
        self._ac_step_um = int(ac.get("step_um", 1000))              # шаг назад к нулю, мкм
        self._ac_step_pause_ticks = max(1, int(ac.get("step_pause_sec", 3)) * 10)
        self._ac_timeout_ticks = max(1, int(ac.get("timeout_sec", 180)) * 10)
        self._varka_count = int(ac.get("count", 0))   # счётчик варок (persist через колбэк)
        self._stage11_prev = False   # был ли на прошлом такте на стадии 11 (для фронта)
        # состояние процедуры калибровки
        self._cal_active = False
        self._cal_phase = None       # None / 'find_zero' / 'drive'
        self._cal_retry = 0
        self._cal_wait = 0           # тики паузы между шагами назад
        self._cal_timeout = 0        # общий предел процедуры (тики)
        self._cal_emit_findzero = False
        self._cal_emit_goto0 = False
        self._cal_emit_shift = 0     # шаг назад к нулю (мкм), 0 = нет
        self._cal_emit_setzero = False
        # колбэк persist счётчика варок (ставит microscope_service): on_varka_count(count)
        self.on_varka_count = None

        self._period = max(0.02, int(config["poll_interval_ms"]) / 1000.0)
        # предохранитель шага цикла (тики по 100мс): нормальный выход — «доехал», а это
        # защита от зависания. Большой, т.к. реальная плата едет медленно (см. step_timeout_s).
        self._step_timeout_ticks = max(1, int(config.get("step_timeout_s", 300)) * 10)

        # входы (задаются извне; на этапе A заменятся чтением из ПЛК)
        self.sv = float(config.get("manual_sv", 0.0))
        self.stage = int(config.get("manual_stage", 0))

        # --- состояние (как в ST) ---
        self.mode = 0
        self.t = 0            # таймер ШАГА внутри цикла (как ST)
        self.cycle_t = 0      # таймер ПЕРИОДА в простое (наше добавление)
        self.t1 = 0           # таймер формирования команды М1
        self.t2 = 0           # таймер формирования команды М2
        self.u = 0            # обратный отсчёт промывки стекла
        self.m1_sp = 0
        self.m1_sp_old = 0
        self.m2_sp = 0
        self.m2_sp_old = 0
        self.cmd1 = 0
        self.cmd2 = 0
        self.cmd = 0          # команда с ВУ (100/200/300/400)
        self.cmd_old = 0
        self.sw0 = False      # циклический режим
        self.sw1 = False      # движение LED назад к стеклу (флаг-защёлка)
        self.sw2 = False      # движение LED вперёд
        self.sw3 = False      # ЗАПРЕТ движения (наш «Стоп движения»)
        self.manual = False   # РУЧНОЙ РЕЖИМ: автомат не пишет плату вообще (пультом рулит человек)
        self._halt = False    # запрос аварийной остановки (цель = текущая позиция)
        self._emit_cmd1 = False  # выставить команду М1 на плату один раз (по фронту формирования)
        self._emit_cmd2 = False
        self.cw0 = False      # клапан промывки трубки
        self.cw1 = False      # клапан промывки стекла
        self.led_bright = int(config.get("led_bright", 0))
        # восстановить состояние LED из конфига: если сохранена яркость > 0 — включён
        self.led_on = bool(config.get("led_on", self.led_bright > 0))

        # колбэк «снять фото» — дёргается в выдержке (mode 23) КАЖДЫЕ shot_interval_sec (серия).
        # Ставит его microscope_service; он сам решает снимать ли (по режиму камеры auto).
        self.on_photo = None
        self._photo_request = False
        # колбэк «писать видео пробы» — дёргается один раз на входе в выдержку (dwell_sec).
        self.on_video = None
        self._video_request = 0        # длительность видео (сек) при запросе, иначе 0
        # состояние выдержки пробы (mode 23)
        self._dwell_left = 0           # осталось тиков выдержки
        self._shot_t = 0              # тики с прошлого скрина
        self._redrive = 0             # тики с прошлого повтора goto в шаге движения
        self._in_range = 0            # тиков подряд в диапазоне ±50 у цели (выдержка перед переходом)
        # состояние гибридного доезда подвода (mode 22)
        self._fa_phase = None         # None / 'coarse' (грубо по 1274) / 'fine' (подгон по 1271)
        self._fa_retry = 0            # сделано подгонов по датчику
        self._fa_wait = 0             # тиков паузы между сдвигами (устаканиться)
        self._fa_shift_req = None     # знаковый сдвиг (мкм) на плату в этом тике (иначе None)
        # авария (подгон не сошёлся / watchdog): цикл стоп, видно в UI
        self._fault = False
        self._fault_msg = ""

        self._lock = threading.Lock()
        self._thread = None
        self._running = False

    # ---------- команды снаружи (из API) ----------

    def send_command(self, cmd):
        """Команда с ВУ: 100 цикл / 200 отвод 40мм / 300 промыть стекло / 400 перекл. режим."""
        with self._lock:
            self.cmd = int(cmd)

    def set_sv(self, value):
        with self._lock:
            self.sv = float(value)

    def set_stage(self, stage):
        with self._lock:
            self.stage = int(stage)

    def set_led(self, bright=None, on=None):
        with self._lock:
            if bright is not None:
                self.led_bright = int(bright)
            if on is not None:
                self.led_on = bool(on)

    def set_cyclic(self, on):
        with self._lock:
            self.sw0 = bool(on)

    def set_trigger_mode(self, mode):
        """Сменить триггер пробы на лету: "time" или "sv". Сбрасываем счётчики отсчёта
        (пауза и точку СВ), чтобы новый режим начал считать заново, а не «держал» старое."""
        with self._lock:
            self._trigger_mode = "sv" if str(mode) == "sv" else "time"
            self.cycle_t = 0
            self._last_sv_shot = None
            return {"trigger_mode": self._trigger_mode}

    def set_ignore_focus(self, on):
        """Галочка «Не использовать фокус»: при True М2 не двигается по таблице СВ."""
        with self._lock:
            self._ignore_focus = bool(on)
            return {"ignore_focus": self._ignore_focus}

    def set_ignore_stage(self, on):
        """«Варить без стадии»: при True авто-цикл не проверяет стадию варки 3..9
        (ручная варка — стадия стоит, двигается только СВ). Сбрасываем точку СВ."""
        with self._lock:
            self._ignore_stage = bool(on)
            self._last_sv_shot = None
            return {"ignore_stage": self._ignore_stage}

    def start_sample(self):
        """Кнопка «Взять пробу»: запустить последовательность (отвод→промывка→подвод→
        выдержка→возврат) с шага 20. Если стоит Ручной режим — снимаем его безопасно и
        запускаем (переводим в Автомат). «Стоп движения» (sw3) остаётся защитой — при нём игнор."""
        with self._lock:
            if self.sw3:
                return {"status": "blocked", "hint": "снимите «Стоп движения»"}
            if self.mode != 0:
                return {"status": "busy", "mode": self.mode}
            was_manual = self.manual
            if self.manual:
                self._drop_manual_locked()   # снять ручной безопасно (зеркало set_manual(True))
            self.mode = 20
            self.t = 0
            self.cycle_t = 0
            return {"status": "started", "was_manual": was_manual}

    def confirm_auto(self):
        """Оператор подтвердил переход в Автомат при старте варки: снять ручной, включить
        авто-цикл (sw0). Дальше авто-цикл сам пойдёт по стадии/режиму."""
        with self._lock:
            self._manual_confirm = False
            if self.manual:
                self._drop_manual_locked()
            self.sw0 = True
            return {"status": "auto_on"}

    def decline_auto(self):
        """Оператор отклонил переход: остаёмся в ручном. Не переспрашиваем, пока стадия не
        выйдет из рабочей зоны и не вернётся (фронт)."""
        with self._lock:
            self._manual_confirm = False
            return {"status": "stay_manual"}

    def _drop_manual_locked(self):
        """Безопасно выйти из ручного режима (вызывается ПОД локом). Зеркало очистки из
        set_manual(True): гасим залипшие команды ВУ/моторов и клапаны, фиксируем уставки на
        текущих, чтобы автомат не рванул к старой цели до задания новой (mode 20)."""
        self.manual = False
        self.cmd = self.cmd_old       # погасить возможный фронт команды ВУ
        self.cmd1 = 0
        self.cmd2 = 0
        self._emit_cmd1 = False
        self._emit_cmd2 = False
        self.cw0 = False
        self.cw1 = False
        self.m1_sp_old = self.m1_sp
        self.m2_sp_old = self.m2_sp

    def _arrived(self, pos_ai, pos_enc, target, prefer_ai=False):
        # ДОЕЗД. prefer_ai=True (подвод) — ориентир по АНАЛОГОВОМУ датчику pos1_ai (рег. 1271,
        # «абсолютная позиция по датчику»): он точнее энкодера. Сравниваем |pos1_ai - target|.
        # Иначе (отвод/возврат) — по энкодеру pos1 ±ARRIVE_TOL_UM, аналог лишь страховка «у стекла».
        if prefer_ai and pos_ai is not None:
            return abs(pos_ai - target) <= ARRIVE_TOL_UM
        near = pos_enc is not None and abs(pos_enc - target) <= ARRIVE_TOL_UM
        at_glass = pos_ai is not None and pos_ai < GLASS_AI
        return near or at_glass

    def _reached_hold(self, pos_ai, pos_enc, target, prefer_ai=False):
        # доезд с выдержкой: в диапазоне держим ARRIVE_HOLD_TICKS (2 с), потом переход.
        # Считаем, что попал (пауза перед следующим шагом); вышел из диапазона — счётчик сброс.
        if self._arrived(pos_ai, pos_enc, target, prefer_ai):
            self._in_range += 1
        else:
            self._in_range = 0
        return self._in_range >= ARRIVE_HOLD_TICKS

    def _redrive_goto(self):
        # повтор импульса «идти» к текущей m1_sp каждые REDRIVE_TICKS тиков (вызывается под локом).
        # Первый импульс даёт блок 6 по смене m1_sp; дальше держим темп, как ручная авто-доводка.
        # Позицию блок 7 пишет каждый тик, здесь только просим повторную команду goto.
        self._redrive += 1
        if self._redrive >= REDRIVE_TICKS:
            self._redrive = 0
            self._emit_cmd1 = True

    def _approach_step(self, pos_enc, pos_ai):
        """Гибридный доезд подвода (mode 22), вызывается ПОД локом. Возврат True = доехали.
        Фаза 'coarse': ведём мотор к заданию по РАСЧЁТНОЙ абс.позиции 1274 (pos_enc) — НЕ по
        энкодеру 1285 (врёт) — до ±coarse_tol, чтобы не перелететь (кристалл). Фаза 'fine':
        подгон по ДАТЧИКУ 1271 (pos_ai): diff=Задание−Датчик; сдвиг мотора=(Датчик−Задание)
        [Задание>Датчик → назад]; пауза pause_sec; замер; повтор, пока |diff|≤fine_tol.
        Не сошлось за max_retry → авария."""
        if pos_enc is None:
            return False
        if self._fa_phase is None:
            self._fa_phase = "coarse"
            self._fa_retry = 0
            self._fa_wait = 0
        if self._fa_phase == "coarse":
            self._redrive_goto()                         # ведём к заданию (m1_sp) по расчётной 1274
            if abs(pos_enc - self.m1_sp) <= self._fa_coarse_tol:
                self._fa_phase = "fine"
                self._fa_wait = 0
                self._redrive = 0
            return False
        # fine — подгон по датчику
        if pos_ai is None:
            return False
        if self._fa_wait > 0:                            # ждём, пока сдвиг доедет и датчик осядет
            self._fa_wait -= 1
            return False
        diff = self.m1_sp - pos_ai                       # Задание − Датчик
        if abs(diff) <= self._fa_fine_tol:
            return True                                  # датчик у задания — доехали
        if self._fa_retry >= self._fa_max_retry:
            self._raise_fault("подгон по датчику не сошёлся за %d попыток (Δ=%d мкм)"
                              % (self._fa_max_retry, diff))
            return False
        self._fa_shift_req = self.m1_sp - pos_ai         # сдвиг = Задание−Датчик (знак по факту железа: гнало от цели)
        self._fa_retry += 1
        self._fa_wait = max(1, int(round(self._fa_pause_sec / self._period)))
        return False

    def _raise_fault(self, msg):
        """Авария (вызывается ПОД локом): стоп цикла + авто-цикла + аварийный стоп моторов.
        Держится, пока оператор не снимет (clear_fault). Видно в UI (label)."""
        self._fault = True
        self._fault_msg = msg
        self.mode = 0
        self.sw0 = False
        self._fa_phase = None
        self._halt = True
        self.sw3 = True
        log_event("microscope_fsm", "АВАРИЯ: " + msg, "error", {"msg": msg})

    def clear_fault(self):
        """Сброс аварии оператором: снять запрет движения, погасить флаг."""
        with self._lock:
            self._fault = False
            self._fault_msg = ""
            self.sw3 = False
        return {"status": "cleared"}

    def set_sensor_scale(self, value):
        """Масштаб датчика 1271 для ЛОГИКИ доезда (на лету). Тот же коэффициент, что показ."""
        with self._lock:
            self._sensor_scale = float(value or 1.0)
            return {"sensor_scale": self._sensor_scale}

    def set_autocal(self, enabled=None, every_n=None, sensor_lo=None, sensor_hi=None,
                    timeout_sec=None, count=None):
        """Настройка автокалибровки нуля М1 на лету (без reload)."""
        with self._lock:
            if enabled is not None:
                self._ac_enabled = bool(enabled)
            if every_n is not None:
                self._ac_every_n = max(1, int(every_n))
            if sensor_lo is not None:
                self._ac_sensor_lo = int(sensor_lo)
            if sensor_hi is not None:
                self._ac_sensor_hi = int(sensor_hi)
            if timeout_sec is not None:
                self._ac_timeout_ticks = max(1, int(timeout_sec) * 10)
            if count is not None:
                self._varka_count = max(0, int(count))
            return {"enabled": self._ac_enabled, "every_n": self._ac_every_n,
                    "sensor_lo": self._ac_sensor_lo, "sensor_hi": self._ac_sensor_hi,
                    "timeout_sec": self._ac_timeout_ticks // 10, "count": self._varka_count}

    def start_autocal(self):
        """Ручной запуск автокалибровки (кнопка). Только когда цикл в простое."""
        with self._lock:
            if self._cal_active:
                return {"status": "busy"}
            if self.mode != 0:
                return {"status": "busy_cycle", "mode": self.mode}
            self._cal_active = True
            self._cal_phase = "find_zero"
            self._cal_retry = 0
            self._cal_wait = 0
            return {"status": "started"}

    def set_fine_approach(self, enabled=None, coarse_tol_um=None, fine_tol_um=None,
                          max_retry=None, pause_sec=None):
        """Настройка гибридного доезда на лету (без reload)."""
        with self._lock:
            if enabled is not None:
                self._fa_enabled = bool(enabled)
            if coarse_tol_um is not None:
                self._fa_coarse_tol = int(coarse_tol_um)
            if fine_tol_um is not None:
                self._fa_fine_tol = int(fine_tol_um)
            if max_retry is not None:
                self._fa_max_retry = int(max_retry)
            if pause_sec is not None:
                self._fa_pause_sec = max(0.5, float(pause_sec))
            return {"enabled": self._fa_enabled, "coarse_tol_um": self._fa_coarse_tol,
                    "fine_tol_um": self._fa_fine_tol, "max_retry": self._fa_max_retry,
                    "pause_sec": self._fa_pause_sec}

    def reset_cycle(self):
        """Кнопка «Сброс»: прервать цикл — в Ожидание, закрыть клапаны, погасить повтор
        (мотор перестаёт получать goto и останавливается). Ручной режим/запрет не трогаем."""
        with self._lock:
            self.mode = 0
            self.t = 0
            self.cycle_t = 0
            self._redrive = 0
            self._dwell_left = 0
            self._fa_phase = None
            self._fa_retry = 0
            self._fa_wait = 0
            self.cw0 = False
            self.cw1 = False
        return {"status": "reset"}

    def skip_step(self):
        """Кнопка «Вперёд»: перепрыгнуть на следующий шаг цикла (отладка).
        В ручном режиме/запрете — игнор."""
        with self._lock:
            if self.manual or self.sw3:
                return {"status": "blocked"}
            nxt = {0: 20, 20: 21, 21: 22, 22: 23, 23: 24, 24: 0}.get(self.mode, 20)
            self.mode = nxt
            self.t = 0
            self._redrive = 0
            self._shot_t = 0
            if nxt == 23:
                # вход в выдержку через «Вперёд» — как обычный вход mode 22->23:
                # взводим выдержку, первый скрин сразу и запрос видео на всю выдержку
                self._dwell_left = self._dwell_sec * 10
                self._photo_request = True
                self._video_request = self._dwell_sec
            return {"status": "skipped", "mode": nxt}

    def set_manual(self, on):
        """Ручной режим: при True автомат перестаёт писать плату (пультом управляет человек).
        Снимает циклический режим и сбрасывает состояние цикла в БЕЗОПАСНОЕ — чтобы после выхода
        из ручного мотор не «доехал» к старой уставке и не сработала подвисшая команда ВУ."""
        with self._lock:
            self.manual = bool(on)
            if on:
                self.sw0 = False
                self.mode = 0
                self.cmd = self.cmd_old       # погасить возможный фронт команды ВУ
                self.cmd1 = 0
                self.cmd2 = 0
                self._emit_cmd1 = False
                self._emit_cmd2 = False
                self.cw0 = False
                self.cw1 = False
                # зафиксировать уставки на «уже выполнено», чтобы авто не двигало моторы при выходе
                self.m1_sp_old = self.m1_sp
                self.m2_sp_old = self.m2_sp

    def set_m1_target(self, um):
        """Ручная цель позиции М1 (мкм). Формирование команды в автомате доедет мотор туда."""
        with self._lock:
            self.m1_sp = int(um)

    def set_m2_target(self, um):
        with self._lock:
            self.m2_sp = int(um)

    def set_movement_inhibit(self, on):
        """Наш «Стоп движения» = SW.3. При True запрещаем движение и требуем немедленной
        остановки (цель моторов = текущая позиция), т.к. плата движется к последней уставке."""
        with self._lock:
            self.sw3 = bool(on)
            if on:
                self._halt = True

    # ---------- снимок состояния (для UI) ----------

    @property
    def state(self):
        with self._lock:
            m = self.mode
            # сколько секунд до таймаута текущего шага движения (чтобы было видно: идёт отсчёт,
            # а не зависло). Только для шагов, где крутится таймер шага self.t.
            step_left = (max(0, self._step_timeout_ticks - self.t) // 10) if m in (20, 22, 24) else None
            # человекочитаемая подпись текущего действия (для вкладки «Цикл»)
            if self._fault:
                label = "⚠ АВАРИЯ: %s — снимите на вкладке «Настр»" % self._fault_msg
            elif m == 20:
                label = "Отвожу в %d мкм · таймаут %d с" % (self._retract_pos, step_left)
            elif m == 21:
                left = max(0, self._pre_wash_sec * 10 - self.t) // 10
                label = "Промывка стекла+трубки: осталось %d с" % left
            elif m == 22:
                if self._fa_enabled and self._fa_phase == "fine":
                    label = "Подгон по датчику к %d мкм (попытка %d/%d) · таймаут %d с" % (
                        self.m1_sp, self._fa_retry, self._fa_max_retry, step_left)
                else:
                    label = "Подвожу к %d мкм (по СВ %.1f) · таймаут %d с" % (self.m1_sp, self.sv, step_left)
            elif m == 23:
                label = "Проба · выдержка: осталось %d с (скрин каждые %d с)" % (
                    max(0, self._dwell_left) // 10, self._shot_interval_sec)
            elif m == 24:
                label = "Возврат в %d мкм" % self._retract_pos
            else:
                # Ожидание — поясняем ЧЕГО ждём (чтобы было видно, продолжится ли авто-цикл)
                stage_in = self._ignore_stage or (CYCLE_STAGE_MIN <= self.stage <= CYCLE_STAGE_MAX)
                if self.manual:
                    label = "Ручной режим — авто-цикл не идёт"
                elif not self.sw0:
                    label = "Ожидание — «Автомат» выключен"
                elif not stage_in:
                    label = "Ожидание — варка не идёт (стадия %d, нужно 3–9)" % self.stage
                elif self._trigger_mode == "sv":
                    if self._last_sv_shot is None:
                        label = "Ожидание — жду СВ в диапазоне %g–%g (сейчас %.1f)" % (
                            self._sv_from, self._sv_to, self.sv)
                    else:
                        label = "Ожидание — жду ±1 от %.1f (сейчас %.1f)" % (
                            self._last_sv_shot, self.sv)
                else:
                    left = max(0, self._pause_sec * 10 - self.cycle_t) // 10
                    label = "Ожидание — след. проба через %d с" % left
            return {
                "mode": m,
                "step": STEP_NAMES.get(m, str(m)),
                "label": label,
                "target": self.m1_sp if m in (20, 22, 24) else None,
                "pre_wash_left_s": max(0, self._pre_wash_sec * 10 - self.t) // 10 if m == 21 else None,
                "dwell_left_s": max(0, self._dwell_left) // 10 if m == 23 else None,
                "step_timeout_left_s": step_left,   # тикающий отсчёт до таймаута шага движения
                "fault": self._fault,
                "fault_msg": self._fault_msg,
                "fa_phase": self._fa_phase,   # None/'coarse'/'fine' — фаза гибридного доезда
                "fine_approach": {
                    "enabled": self._fa_enabled,
                    "coarse_tol_um": self._fa_coarse_tol,
                    "fine_tol_um": self._fa_fine_tol,
                    "max_retry": self._fa_max_retry,
                    "pause_sec": self._fa_pause_sec,
                },
                "autocal": {
                    "enabled": self._ac_enabled,
                    "every_n": self._ac_every_n,
                    "sensor_lo": self._ac_sensor_lo,
                    "sensor_hi": self._ac_sensor_hi,
                    "timeout_sec": self._ac_timeout_ticks // 10,
                    "count": self._varka_count,
                    "active": self._cal_active,
                    "phase": self._cal_phase,
                },
                "cyclic": self.sw0,
                "inhibit": self.sw3,
                "manual": self.manual,
                "valve_tube": self.cw0,
                "valve_glass": self.cw1,
                "cmd1": self.cmd1,
                "cmd2": self.cmd2,
                "m1_sp": self.m1_sp,
                "m2_sp": self.m2_sp,
                "led_bright": self.led_bright,
                "led_on": self.led_on,
                "sv": self.sv,
                "stage": self.stage,
                "u": self.u,
                # авто-цикл разрешён по стадии (3..9) ИЛИ включено «варить без стадии».
                "stage_ok": self._ignore_stage or (CYCLE_STAGE_MIN <= self.stage <= CYCLE_STAGE_MAX),
                "last_sv_shot": self._last_sv_shot,   # на каком целом СВ взяли последнюю пробу (режим sv)
                "manual_confirm": self._manual_confirm,   # варка началась, а мы в ручном — спросить оператора
                "cycle_params": {
                    "retract_pos": self._retract_pos,
                    "pre_wash_sec": self._pre_wash_sec,
                    "dwell_sec": self._dwell_sec,
                    "shot_interval_sec": self._shot_interval_sec,
                    "pause_sec": self._pause_sec,
                    "trigger_mode": self._trigger_mode,
                    "sv_from": self._sv_from,
                    "sv_to": self._sv_to,
                    "ignore_stage": self._ignore_stage,
                },
            }

    # ---------- жизненный цикл ----------

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="micro-fsm", daemon=True)
        self._thread.start()
        log_event("microscope_fsm", "Автомат микроскопа запущен", "info")

    def stop(self):
        self._running = False
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        log_event("microscope_fsm", "Автомат микроскопа остановлен", "info")

    def _loop(self):
        while self._running:
            start = time.time()
            try:
                self.tick()
            except Exception as e:
                log_event("microscope_fsm", "Ошибка такта автомата", "error", {"error": str(e)})
            elapsed = time.time() - start
            if elapsed < self._period:
                time.sleep(self._period - elapsed)

    # ---------- один такт автомата (перенос ST) ----------

    def tick(self):
        telem = self.plate.telemetry
        pos1 = telem.get("pos1")
        pos1_ai = telem.get("pos1_ai")
        # датчик для ЛОГИКИ доезда — в масштабе задания/расчётной позиции (телеметрию для показа
        # не трогаем, JS сам умножает). Одна точка: дальше по tick pos1_ai уже приведён.
        if pos1_ai is not None:
            pos1_ai = pos1_ai * self._sensor_scale

        with self._lock:
            # 0) подтверждение перехода из ручного: фронт входа стадии в рабочую зону (3..9),
            # пока стоит Ручной режим -> просим оператора подтвердить переход в Автомат.
            # Флаг держится, пока не подтвердят/отклонят или стадия не выйдет из зоны.
            stage_in = CYCLE_STAGE_MIN <= self.stage <= CYCLE_STAGE_MAX
            if stage_in and not self._stage_prev_in and self.manual:
                self._manual_confirm = True
            if not stage_in:
                self._manual_confirm = False
            self._stage_prev_in = stage_in

            # 0.5) АВТОКАЛИБРОВКА нуля М1: фронт входа в стадию 11 (пропарка) -> счётчик варок +1;
            # дошёл до every_n -> на этой пропарке Поиск 0 -> ждём датчик в [lo..hi] -> set_zero
            # (обнулить датчик+энкодер). Не вошёл за таймаут -> повтор 1 раз -> авария.
            varka_persist = None   # не None -> сохранить счётчик варок (вне лока, через колбэк)
            stage11 = (self.stage == 11)
            if stage11 and not self._stage11_prev:
                self._varka_count += 1
                varka_persist = self._varka_count
                if self._ac_enabled and not self._cal_active and self._varka_count >= self._ac_every_n:
                    self._cal_active = True
                    self._cal_phase = "find_zero"
                    self._cal_retry = 0
                    self._cal_wait = 0
                    log_event("microscope_fsm", "Автокалибровка: старт (варок %d/%d)" %
                              (self._varka_count, self._ac_every_n), "info")
            self._stage11_prev = stage11

            if self._cal_active:
                if self._cal_phase == "find_zero":
                    # прожать всё: Поиск 0 + идти в позицию 0 (дальше дожимаем шагами назад)
                    self._cal_emit_findzero = True
                    self._cal_emit_goto0 = True
                    self._cal_phase = "drive"
                    self._cal_wait = self._ac_step_pause_ticks
                    self._cal_timeout = self._ac_timeout_ticks
                elif self._cal_phase == "drive":
                    if pos1_ai is not None and self._ac_sensor_lo <= pos1_ai <= self._ac_sensor_hi:
                        self._cal_emit_setzero = True     # датчик в зоне нуля -> обнулить (set_zero)
                        self._cal_active = False
                        self._cal_phase = None
                        self._varka_count = 0
                        varka_persist = 0
                        log_event("microscope_fsm", "Автокалибровка: ноль установлен", "success",
                                  {"sensor": round(pos1_ai, 1)})
                    else:
                        # шагаем НАЗАД к нулю по step_um каждые step_pause (пока датчик не в зоне)
                        self._cal_wait -= 1
                        if self._cal_wait <= 0:
                            self._cal_emit_shift = self._ac_step_um
                            self._cal_wait = self._ac_step_pause_ticks
                        self._cal_timeout -= 1
                        if self._cal_timeout <= 0:
                            if self._cal_retry < 1:
                                self._cal_retry += 1
                                self._cal_phase = "find_zero"   # повтор один раз
                                log_event("microscope_fsm", "Автокалибровка: повтор (Поиск 0 заново)", "warn")
                            else:
                                self._cal_active = False
                                self._cal_phase = None
                                self._raise_fault("автокалибровка: датчик не вошёл в %d..%d за таймаут"
                                                  % (self._ac_sensor_lo, self._ac_sensor_hi))

            # 1) авто-цикл: гоняем пробу, пока включён «Автомат» (sw0) И стадия варки в рабочем
            # диапазоне 3..9 (общее разрешение). ВНУТРИ — повторяемость по trigger_mode:
            #   "time" — новая проба через _pause_sec (cycle_t капает только в простое);
            #   "sv"   — новая проба на каждом ЦЕЛОМ СВ в [sv_from..sv_to] по мере роста СВ.
            # Стадия вне диапазона -> ничего не капает, новый цикл не стартует (уже идущий доводим),
            # счётчик СВ сбрасываем (новая варка снимет заново с sv_from).
            stage_ok = self._ignore_stage or (CYCLE_STAGE_MIN <= self.stage <= CYCLE_STAGE_MAX)
            if self.sw0 and stage_ok and not self._cal_active:   # идёт калибровка — цикл не стартуем
                if self.mode == 0:
                    if self._trigger_mode == "sv":
                        # запоминаем ТОЧНОЕ СВ на старте цикла и снимаем следующую пробу, когда
                        # СВ отклонилось от него на >=1 в ЛЮБУЮ сторону (рост или падение —
                        # напр. 87.0 -> 86.0). Не по целым: разница считается от точки запуска.
                        if (self._sv_from <= self.sv <= self._sv_to
                                and (self._last_sv_shot is None
                                     or abs(self.sv - self._last_sv_shot) >= 1.0)):
                            self._last_sv_shot = self.sv   # точка отсчёта = СВ на старте пробы
                            self.mode = 20
                            self.t = 0
                            self.cycle_t = 0
                    else:
                        self.cycle_t += 1
                        if self.cycle_t > self._pause_sec * 10:
                            self.mode = 20               # авто-цикл гонит последовательность пробы
                            self.t = 0
                            self.cycle_t = 0
            else:
                self.cycle_t = 0
                self._last_sv_shot = None

            # 2) обратный отсчёт промывки стекла (как ST)
            if self.u > 0:
                self.u -= 1
            if self.u == 1:
                self.cw1 = False

            # 3) ЕЖЕЧАСНАЯ промывка от засахаривания (всегда активна; только в простое mode 0,
            # чтобы не мешать пробе). Раз в час на минуте hw_minute: открыть ОБА клапана на ~5 с.
            if self._hw_enabled and self.mode == 0:
                now = datetime.now()
                if now.minute == self._hw_minute and self._hw_on <= now.second < self._hw_off:
                    self.cw0 = True
                    self.cw1 = True
                elif now.minute == self._hw_minute and now.second >= self._hw_off:
                    self.cw0 = False
                    self.cw1 = False

            # 4) команда с ВУ (по фронту cmd<>cmd_old). ИМПУЛЬСНАЯ: после обработки
            # сбрасываем cmd/cmd_old в 0, иначе повторная та же команда (напр. cmd=100
            # «запустить цикл») не даёт фронта и не срабатывает второй раз.
            if self.cmd != self.cmd_old:
                c = self.cmd // 100
                if c == 1:
                    self.mode = 20                     # «Взять пробу» / произвести цикл
                elif c == 2:
                    self.m1_sp = self.SP[0]            # отвести на 40 мм
                elif c == 3:
                    self.cw1 = True                    # промыть стекло
                    self.u = int(self.SP[50]) // 100
                elif c == 4:
                    self.sw0 = not self.sw0            # переключить циклический режим
                self.cmd = 0                           # команда потреблена
                self.cmd_old = 0

            # 5) НОВЫЙ цикл пробы (шаги 20→21→22→23→24). Доезд = энкодер у цели + мотор остановился.
            if self.mode == 0:
                self._redrive = 0; self._in_range = 0
                self._fa_phase = None; self._fa_retry = 0; self._fa_wait = 0
            elif self.mode == 20:
                # отвод в retract_pos (напр. 20000 мкм)
                self.m1_sp = self._retract_pos
                self._redrive_goto()               # повторяем goto до доезда (как ручная «Идти»)
                self.t += 1
                if self._reached_hold(pos1_ai, pos1, self._retract_pos) or self.t > self._step_timeout_ticks:
                    self.t = 0
                    self.mode = 21
            elif self.mode == 21:
                # промывка стекла + трубки перед пробой (pre_wash_sec)
                self._redrive = 0; self._in_range = 0
                self.cw0 = True
                self.cw1 = True
                self.t += 1
                if self.t > self._pre_wash_sec * 10:
                    self.cw0 = False
                    self.cw1 = False
                    self.t = 0
                    self.mode = 22
            elif self.mode == 22:
                # подвод к зазору по СВ (таблица SVSP); клапан трубки ОТКРЫТ на подводе.
                # По той же строке выбираем фокус М2 (FOCUS[i]) — на разных СВ фокус сбит.
                self.m1_sp = self.SP[1]                # по умолчанию SP[1]
                focus = 0
                for i in range(1, 50):
                    if self.sv >= self.SVSP[i] and self.SVSP[i] > 0.0:
                        self.m1_sp = self.SP[i]
                        focus = self.FOCUS[i] if i < len(self.FOCUS) else 0
                # фокус применяем, если он ВКЛЮЧЁН галочкой. Значение 0 — валидная позиция
                # (новый ноль после set_zero): М2 притянется к 0, а не «не трогать».
                if not self._ignore_focus:
                    self.m2_sp = focus
                self.cw0 = True                        # промывка трубки открыта на подводе
                self.t += 1
                # доезд подвода. Галочка «Довод по абсолютнику»: грубо по расчётной 1274,
                # потом точный подгон по датчику 1271 (см. _approach_step). Иначе — старое
                # поведение: доезд по датчику с выдержкой (_reached_hold prefer_ai).
                if self._fa_enabled:
                    arrived = self._approach_step(pos1, pos1_ai)
                else:
                    self._redrive_goto()
                    arrived = self._reached_hold(pos1_ai, pos1, self.m1_sp, prefer_ai=True)
                if arrived or self.t > self._step_timeout_ticks:
                    self.cw0 = False                   # по приходу к стеклу — закрыть трубку
                    self.t = 0
                    self._fa_phase = None; self._fa_retry = 0; self._fa_wait = 0
                    self._dwell_left = self._dwell_sec * 10
                    self._shot_t = 0
                    self._photo_request = True         # первый скрин сразу у стекла
                    self._video_request = self._dwell_sec   # писать видео пробы всю выдержку
                    self.mode = 23
            elif self.mode == 23:
                # выдержка пробы: серия скринов каждые shot_interval_sec + пишется видео
                self._redrive = 0; self._in_range = 0
                self.t += 1
                self._shot_t += 1
                if self._shot_t >= self._shot_interval_sec * 10:
                    self._shot_t = 0
                    self._photo_request = True
                self._dwell_left -= 1
                if self._dwell_left <= 0:
                    self.t = 0
                    self.mode = 24
            elif self.mode == 24:
                # возврат в retract_pos
                self.m1_sp = self._retract_pos
                self._redrive_goto()                   # повторяем goto до доезда
                self.t += 1
                if self._reached_hold(pos1_ai, pos1, self._retract_pos) or self.t > self._step_timeout_ticks:
                    self.t = 0
                    self.mode = 0

            # 6) формирование команды мотору М1 (тайминги как ST: 200мс -> 3с)
            if self.m1_sp != self.m1_sp_old and self.cmd1 == 0 and not self.sw1 and not self.sw2:
                if self.m1_sp < self.m1_sp_old:        # движение назад -> вкл клапан трубки
                    self.cw0 = True
                self.t1 += 1
                if self.t1 > 2:                        # ждём 200мс, потом команда
                    self.cmd1 = CMD_SET_SP1
                    self._emit_cmd1 = True             # послать команду на плату один раз
                    self.t1 = 0
                    self.m1_sp_old = self.m1_sp
            if self.cmd1 > 0:
                self.t1 += 1
                if self.t1 > 30:                       # 3с -> сброс cmd и выкл клапан
                    # НО в подводе (mode 22) трубка должна оставаться открытой до приезда —
                    # её закрывает сам шаг 22 по доезду; здесь не гасим, иначе мигает.
                    if self.cmd1 == CMD_SET_SP1 and self.mode != 22:
                        self.cw0 = False
                    self.cmd1 = 0
                    self.t1 = 0

            # 6b) формирование команды мотору М2 (фокус)
            if self.m2_sp != self.m2_sp_old:
                self.t2 += 1
                if self.t2 > 2:
                    self.cmd2 = CMD_SET_SP2
                    self._emit_cmd2 = True
                    self.t2 = 0
                    self.m2_sp_old = self.m2_sp
            if self.cmd2 > 0:
                self.t2 += 1
                if self.t2 > 2:
                    self.cmd2 = 0
                    self.t2 = 0

            # 7) сбор выходов; аварийный стоп срабатывает один раз по фронту SW.3
            out = self._collect_outputs()
            halt = self._halt and self.sw3
            if halt:
                self._halt = False
            manual = self.manual
            emit_cmd1 = self._emit_cmd1; self._emit_cmd1 = False
            emit_cmd2 = self._emit_cmd2; self._emit_cmd2 = False
            photo_request = self._photo_request; self._photo_request = False
            video_request = self._video_request; self._video_request = 0
            shift_req = self._fa_shift_req; self._fa_shift_req = None   # знаковый сдвиг подгона (мкм)
            cal_active = self._cal_active
            cal_findzero = self._cal_emit_findzero; self._cal_emit_findzero = False
            cal_goto0 = self._cal_emit_goto0; self._cal_emit_goto0 = False
            cal_shift = self._cal_emit_shift; self._cal_emit_shift = 0
            cal_setzero = self._cal_emit_setzero; self._cal_emit_setzero = False

        pos2 = telem.get("pos2")
        connected = self.plate.status.get("connected", False)

        # РУЧНОЙ РЕЖИМ: автомат не трогает плату — всем (моторы/LED/клапаны) рулит пульт.
        # (защита хода М1 у стекла — аппаратная, в прошивке по регистру 1234, см. m1_stop_sensor)
        if manual:
            return

        # видео пробы по триггеру: на входе в выдержку — начать запись на dwell сек (авто-финиш)
        if video_request and self.on_video:
            try:
                self.on_video(video_request)
            except Exception as e:
                log_event("microscope_fsm", "Ошибка колбэка видео", "warn", {"error": str(e)})

        # фото по триггеру: серия скринов в выдержке (колбэк сам решает по режиму auto)
        if photo_request and self.on_photo:
            try:
                self.on_photo()
            except Exception as e:
                log_event("microscope_fsm", "Ошибка колбэка фото", "warn", {"error": str(e)})

        # persist счётчика варок (изменился на этом такте) — через колбэк сервиса
        if varka_persist is not None and self.on_varka_count:
            try:
                self.on_varka_count(varka_persist)
            except Exception as e:
                log_event("microscope_fsm", "Ошибка persist счётчика варок", "warn", {"error": str(e)})

        # плату дёргаем ВНЕ лока (её методы потокобезопасны)
        if halt:
            # аварийный стоп: нативная команда СТОП + фиксируем цель на текущей позиции,
            # чтобы после снятия запрета мотор не «доезжал» к старой уставке
            self.plate.motor_stop(1)
            self.plate.motor_stop(2)
            with self._lock:
                if pos1 is not None:
                    self.m1_sp = pos1
                    self.m1_sp_old = pos1
                if pos2 is not None:
                    self.m2_sp = pos2
                    self.m2_sp_old = pos2
        elif connected and cal_active:
            # автокалибровка рулит М1 сама — обычное движение не пишем. Прожимаем всё, чтобы поехал в 0:
            # Поиск 0 + идти в позицию 0, затем шаги НАЗАД по step_um до входа датчика в зону -> set_zero.
            if cal_findzero:
                self.plate.motor_enable(1, True)
                self.plate.motor_find_zero(1)
            if cal_goto0:
                self.plate.motor_enable(1, True)
                self.plate.motor_goto(1, 0)               # идти в позицию 0
            if cal_shift:
                self.plate.motor_enable(1, True)
                self.plate.motor_direction(1, False)      # назад к нулю
                self.plate.motor_shift(1, int(cal_shift))
            if cal_setzero:
                self.plate.motor_set_zero(1)
        elif connected and not out["inhibit"]:
            # движение обоих моторов — только при связи и снятом запрете.
            if shift_req is not None:
                # точный подгон по датчику: нативный сдвиг на разницу (Задание−Датчик).
                # знак -> направление (>=0 вперёд), модуль -> величина shift. goto к m1_sp в этом
                # тике НЕ шлём, чтобы не конфликтовать со сдвигом.
                self.plate.motor_enable(1, True)
                self.plate.motor_direction(1, shift_req >= 0)
                self.plate.motor_shift(1, abs(int(shift_req)))
            else:
                # позицию пишем всегда (OUT-блок дедуплицирует по изменению), команду — по фронту.
                self.plate.write_m1_sp(out["m1_sp"])
                if emit_cmd1:
                    self.plate.cmd1(CMD_SET_SP1)
            self.plate.write_m2_sp(out["m2_sp"])
            if emit_cmd2:
                self.plate.cmd2(CMD_SET_SP2)

        # подсветка и клапаны — не движение, пишем всегда (кроме ручного режима — там return выше)
        self.plate.set_led(out["led_bright"], out["led_on"])
        self.plate.set_valve_tube(out["cw0"])
        self.plate.set_valve_glass(out["cw1"])

    def _collect_outputs(self):
        return {
            "inhibit": self.sw3,
            "cmd1": self.cmd1, "cmd2": self.cmd2,
            "m1_sp": self.m1_sp, "m2_sp": self.m2_sp,
            "led_bright": self.led_bright, "led_on": self.led_on,
            "cw0": self.cw0, "cw1": self.cw1,
        }
