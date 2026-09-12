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

# Повтор команды «идти» в цикле: плата за ОДИН goto делает лишь шаг к цели (как ручная
# кнопка «Идти»), поэтому в шагах движения цикла повторяем импульс каждые REDRIVE_TICKS
# тиков (≈0.7 с) до доезда — иначе мотор делает один шаг и стоит («не идёт по заданию»).
REDRIVE_TICKS = 7

# Новый цикл пробы «Взять пробу»: отвод -> промывка -> подвод -> выдержка(скрины+видео) -> возврат.
STEP_NAMES = {0: "ожидание", 20: "отвод", 21: "промывка перед пробой",
              22: "подвод к стеклу", 23: "проба · выдержка", 24: "возврат"}


class MicroscopeFSM:
    def __init__(self, plate, config):
        self.plate = plate
        self.cfg = config
        self.SP = list(config["SP"])
        self.SVSP = list(config["SVSP"])
        self.cycle_period = config.get("cycle_period_s", {"default": 120})
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
        self.led_on = False

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

    def start_sample(self):
        """Кнопка «Взять пробу»: запустить последовательность (отвод→промывка→подвод→
        выдержка→возврат) с шага 20. В ручном режиме и при аварийном запрете — игнор."""
        with self._lock:
            if self.manual or self.sw3:
                return {"status": "blocked",
                        "hint": "снимите Ручной режим / Стоп движения"}
            if self.mode != 0:
                return {"status": "busy", "mode": self.mode}
            self.mode = 20
            self.t = 0
            self.cycle_t = 0
            return {"status": "started"}

    def _arrived(self, pos_ai, pos_enc, target):
        # ДОЕЗД: энкодер pos1 в диапазоне ±ARRIVE_TOL_UM (±50 мкм) от цели — та же шкала мкм.
        # pos1_ai (аналог) НЕ в шкале зазора (мал только у стекла), с целью НЕ сравниваем —
        # берём лишь как страховку «у стекла» на подводе. step_timeout — общий предохранитель.
        near = pos_enc is not None and abs(pos_enc - target) <= ARRIVE_TOL_UM
        at_glass = pos_ai is not None and pos_ai < GLASS_AI
        return near or at_glass

    def _redrive_goto(self):
        # повтор импульса «идти» к текущей m1_sp каждые REDRIVE_TICKS тиков (вызывается под локом).
        # Первый импульс даёт блок 6 по смене m1_sp; дальше держим темп, как ручная авто-доводка.
        # Позицию блок 7 пишет каждый тик, здесь только просим повторную команду goto.
        self._redrive += 1
        if self._redrive >= REDRIVE_TICKS:
            self._redrive = 0
            self._emit_cmd1 = True

    def reset_cycle(self):
        """Кнопка «Сброс»: прервать цикл — в Ожидание, закрыть клапаны, погасить повтор
        (мотор перестаёт получать goto и останавливается). Ручной режим/запрет не трогаем."""
        with self._lock:
            self.mode = 0
            self.t = 0
            self.cycle_t = 0
            self._redrive = 0
            self._dwell_left = 0
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
                self._dwell_left = self._dwell_sec * 10
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
            # человекочитаемая подпись текущего действия (для вкладки «Цикл»)
            if m == 20:
                label = "Отвожу в %d мкм" % self._retract_pos
            elif m == 21:
                left = max(0, self._pre_wash_sec * 10 - self.t) // 10
                label = "Промывка стекла+трубки: осталось %d с" % left
            elif m == 22:
                label = "Подвожу к %d мкм (по СВ %.1f)" % (self.m1_sp, self.sv)
            elif m == 23:
                label = "Проба · выдержка: осталось %d с (скрин каждые %d с)" % (
                    max(0, self._dwell_left) // 10, self._shot_interval_sec)
            elif m == 24:
                label = "Возврат в %d мкм" % self._retract_pos
            else:
                label = "Ожидание"
            return {
                "mode": m,
                "step": STEP_NAMES.get(m, str(m)),
                "label": label,
                "target": self.m1_sp if m in (20, 22, 24) else None,
                "pre_wash_left_s": max(0, self._pre_wash_sec * 10 - self.t) // 10 if m == 21 else None,
                "dwell_left_s": max(0, self._dwell_left) // 10 if m == 23 else None,
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
                "cycle_params": {
                    "retract_pos": self._retract_pos,
                    "pre_wash_sec": self._pre_wash_sec,
                    "dwell_sec": self._dwell_sec,
                    "shot_interval_sec": self._shot_interval_sec,
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

        with self._lock:
            # 1) циклический режим: период по стадии варки (наш cycle_t в простое)
            if self.sw0:
                if self.mode == 0:
                    self.cycle_t += 1
                    if self.cycle_t > self._cycle_threshold_ticks():
                        self.mode = 20               # авто-цикл гонит ту же последовательность пробы
                        self.t = 0
                        self.cycle_t = 0
            else:
                self.cycle_t = 0

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
                self._redrive = 0
            elif self.mode == 20:
                # отвод в retract_pos (напр. 20000 мкм)
                self.m1_sp = self._retract_pos
                self._redrive_goto()               # повторяем goto до доезда (как ручная «Идти»)
                self.t += 1
                if self._arrived(pos1_ai, pos1, self._retract_pos) or self.t > self._step_timeout_ticks:
                    self.t = 0
                    self.mode = 21
            elif self.mode == 21:
                # промывка стекла + трубки перед пробой (pre_wash_sec)
                self._redrive = 0
                self.cw0 = True
                self.cw1 = True
                self.t += 1
                if self.t > self._pre_wash_sec * 10:
                    self.cw0 = False
                    self.cw1 = False
                    self.t = 0
                    self.mode = 22
            elif self.mode == 22:
                # подвод к зазору по СВ (таблица SVSP); клапан трубки ОТКРЫТ на подводе
                self.m1_sp = self.SP[1]                # по умолчанию SP[1]
                for i in range(1, 50):
                    if self.sv >= self.SVSP[i] and self.SVSP[i] > 0.0:
                        self.m1_sp = self.SP[i]
                self.cw0 = True                        # промывка трубки открыта на подводе
                self._redrive_goto()                   # повторяем goto до доезда
                self.t += 1
                if self._arrived(pos1_ai, pos1, self.m1_sp) or self.t > self._step_timeout_ticks:
                    self.cw0 = False                   # по приходу к стеклу — закрыть трубку
                    self.t = 0
                    self._dwell_left = self._dwell_sec * 10
                    self._shot_t = 0
                    self._photo_request = True         # первый скрин сразу у стекла
                    self._video_request = self._dwell_sec   # писать видео пробы всю выдержку
                    self.mode = 23
            elif self.mode == 23:
                # выдержка пробы: серия скринов каждые shot_interval_sec + пишется видео
                self._redrive = 0
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
                if self._arrived(pos1_ai, pos1, self._retract_pos) or self.t > self._step_timeout_ticks:
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

        pos2 = telem.get("pos2")
        connected = self.plate.status.get("connected", False)

        # РУЧНОЙ РЕЖИМ: автомат не трогает плату — всем (моторы/LED/клапаны) рулит пульт.
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
        elif connected and not out["inhibit"]:
            # движение обоих моторов — только при связи и снятом запрете.
            # позицию пишем всегда (OUT-блок дедуплицирует по изменению), команду — по фронту.
            self.plate.write_m1_sp(out["m1_sp"])
            self.plate.write_m2_sp(out["m2_sp"])
            if emit_cmd1:
                self.plate.cmd1(CMD_SET_SP1)
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

    def _cycle_threshold_ticks(self):
        sec = self.cycle_period.get(str(self.stage), self.cycle_period.get("default", 120))
        return int(sec) * 10   # секунды -> такты по 100 мс
