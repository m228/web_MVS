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

STEP_NAMES = {0: "ожидание", 10: "отвод от стекла", 11: "промывка трубки",
              12: "подвод к стеклу", 13: "резерв"}


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

        self._period = max(0.02, int(config["poll_interval_ms"]) / 1000.0)

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
        self._halt = False    # запрос аварийной остановки (цель = текущая позиция)
        self.cw0 = False      # клапан промывки трубки
        self.cw1 = False      # клапан промывки стекла
        self.led_bright = int(config.get("led_bright", 0))
        self.led_on = False

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
            return {
                "mode": self.mode,
                "step": STEP_NAMES.get(self.mode, str(self.mode)),
                "cyclic": self.sw0,
                "inhibit": self.sw3,
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
                        self.mode = 10
                        self.t = 0
                        self.cycle_t = 0
            else:
                self.cycle_t = 0

            # 2) обратный отсчёт промывки стекла (как ST)
            if self.u > 0:
                self.u -= 1
            if self.u == 1:
                self.cw1 = False

            # 3) почасовая промывка от засахаривания (временный приём из ST, отключаемый)
            if self._hw_enabled:
                now = datetime.now()
                if now.minute == self._hw_minute and now.second >= self._hw_on:
                    self.cw0 = True
                    self.cw1 = True
                if now.minute == self._hw_minute and now.second >= self._hw_off:
                    self.cw0 = False
                    self.cw1 = False

            # 4) команда с ВУ (по фронту cmd<>cmd_old)
            if self.cmd != self.cmd_old:
                c = self.cmd // 100
                if c == 1:
                    self.mode = 10                     # произвести цикл отвода М1
                elif c == 2:
                    self.m1_sp = self.SP[0]            # отвести на 40 мм
                elif c == 3:
                    self.cw1 = True                    # промыть стекло
                    self.u = int(self.SP[50]) // 100
                elif c == 4:
                    self.sw0 = not self.sw0            # переключить циклический режим
                self.cmd_old = self.cmd

            # 5) движение (case mode, 1:1 с ST)
            if self.mode == 0:
                pass
            elif self.mode == 10:
                # отвод на позицию SP[0]
                self.m1_sp = self.SP[0]
                self.t += 1
                if self.t > 100:                       # застрял -> след. шаг
                    self.t = 5
                    self.mode += 1
                if pos1 is not None and pos1 == self.SP[0]:  # приехал -> след. шаг
                    self.t = 5
                    self.mode += 1
            elif self.mode == 11:
                # пауза 500 мс + вкл клапан промывки трубки
                self.t -= 1
                self.cw0 = True
                self.sw2 = False
                if self.t < 1:
                    self.mode += 1
            elif self.mode == 12:
                # подвод в SP[i] по СВ, затем выкл клапана
                self.m1_sp = self.SP[1]                # по умолчанию SP[1]
                self.t += 1
                for i in range(1, 50):
                    if self.sv >= self.SVSP[i] and self.SVSP[i] > 0.0:
                        self.m1_sp = self.SP[i]
                if self.t > 100:                       # застрял -> стоп
                    self.cw0 = False
                    self.sw1 = False
                    self.mode = 0
                    self.t = 0
                arrived = pos1 is not None and pos1 == self.m1_sp
                saw_glass = pos1_ai is not None and pos1_ai < 20
                if arrived or saw_glass:
                    self.cw0 = False
                    self.sw1 = False
                    self.sw2 = False
                    self.mode = 0
            elif self.mode == 13:
                pass

            # 6) формирование команды мотору М1 (тайминги как ST: 200мс -> 3с)
            if self.m1_sp != self.m1_sp_old and self.cmd1 == 0 and not self.sw1 and not self.sw2:
                if self.m1_sp < self.m1_sp_old:        # движение назад -> вкл клапан трубки
                    self.cw0 = True
                self.t1 += 1
                if self.t1 > 2:                        # ждём 200мс, потом команда
                    self.cmd1 = CMD_SET_SP1
                    self.t1 = 0
                    self.m1_sp_old = self.m1_sp
            if self.cmd1 > 0:
                self.t1 += 1
                if self.t1 > 30:                       # 3с -> сброс cmd и выкл клапан
                    if self.cmd1 == CMD_SET_SP1:
                        self.cw0 = False
                    self.cmd1 = 0
                    self.t1 = 0

            # 6b) формирование команды мотору М2 (фокус)
            if self.m2_sp != self.m2_sp_old:
                self.t2 += 1
                if self.t2 > 2:
                    self.cmd2 = CMD_SET_SP2
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

        pos2 = telem.get("pos2")
        connected = self.plate.status.get("connected", False)

        # плату дёргаем ВНЕ лока (её методы потокобезопасны)
        if halt:
            # аварийный стоп: цель = текущая позиция, мотор встаёт где есть (плата
            # держит последнюю уставку, поэтому «просто не слать» её не остановит)
            if pos1 is not None:
                self.plate.write_m1_sp(pos1)
                self.plate.cmd1(CMD_SET_SP1)
            if pos2 is not None:
                self.plate.write_m2_sp(pos2)
                self.plate.cmd2(CMD_SET_SP2)
            with self._lock:
                if pos1 is not None:
                    self.m1_sp = pos1
                    self.m1_sp_old = pos1
                if pos2 is not None:
                    self.m2_sp = pos2
                    self.m2_sp_old = pos2
        elif connected and not out["inhibit"]:
            # движение обоих моторов — только при связи и снятом запрете
            self.plate.cmd1(out["cmd1"])
            self.plate.write_m1_sp(out["m1_sp"])
            self.plate.cmd2(out["cmd2"])
            self.plate.write_m2_sp(out["m2_sp"])

        # подсветка и клапаны — не движение, пишем всегда
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
