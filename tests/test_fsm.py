"""Автомат пробы (microscope_fsm) без платы и камеры: такт за тактом через fsm.tick().

Плата — фейк: telemetry/status как атрибуты, команды складываются в списки. Камеры и
Modbus нет (см. память про локальную проверку CV/FSM)."""
import copy

import pytest

import plate_config
from microscope_fsm import MicroscopeFSM


class FakePlate:
    """Минимальная плата для FSM: отдаёт телеметрию, принимает команды/позицию в списки."""

    def __init__(self):
        self.telemetry = {"pos1": 0, "pos1_ai": 0, "pos1_enc": 0, "pos2": 0}
        self.status = {"connected": True}
        self.m1_sp = None
        self.m2_sp = None
        self.cmd1_calls = []
        self.stop_calls = []
        self.leds = []
        self.valves = []

    def __post_init__(self):
        pass

    def write_m1_sp(self, v): self.m1_sp = v
    def write_m2_sp(self, v): self.m2_sp = v
    def cmd1(self, v): self.cmd1_calls.append(v)
    def cmd2(self, v): pass
    def motor_goto(self, m, v): pass
    def motor_enable(self, m, on): pass
    def motor_stop(self, m): self.stop_calls.append(m)
    def motor_find_zero(self, m): pass
    def motor_set_zero(self, m): pass
    def motor_steps(self, m, n): pass       # снятие флага «Поиск 0» после автокалибровки (1.9.98)
    def set_led(self, b, on): self.leds.append((b, on))
    def set_valve_tube(self, on): self.valves.append(("tube", on))
    def set_valve_glass(self, on): self.valves.append(("glass", on))


@pytest.fixture
def fsm():
    cfg = copy.deepcopy(plate_config.DEFAULTS)
    # короткие тайминги, чтобы цикл проходил за разумное число тиков
    cfg["probe_cycle"].update({"pause_sec": 1, "trigger_mode": "time", "pre_wash_sec": 0,
                               "post_wash_pause_sec": 0, "dwell_sec": 0, "settle_sec": 0, "glass_settle_sec": 0})
    cfg["probe_cycle"]["fast"]["enabled"] = False
    return MicroscopeFSM(FakePlate(), cfg)


def _run_until(fsm, pred, max_ticks=600, track_arrival=True):
    """Крутить tick() до pred() или лимита. track_arrival — телеметрия «доезжает» до текущей
    цели мотора, иначе цикл встанет на доезде. Возвращает множество пройденных mode."""
    seen = set()
    for _ in range(max_ticks):
        if track_arrival and fsm.plate.m1_sp is not None:
            v = fsm.plate.m1_sp
            fsm.plate.telemetry.update({"pos1": v, "pos1_enc": v, "pos1_ai": v})
        fsm.tick()
        seen.add(fsm.mode)
        if pred():
            break
    return seen


def test_auto_mode_writes_motor_setpoint_to_plate(fsm):
    # позитивный контроль: в обычном авто-режиме уставка М1 реально уходит на плату
    fsm.set_cyclic(True)
    fsm.set_stage(7)
    fsm.set_sv(88.0)
    _run_until(fsm, lambda: fsm.plate.m1_sp is not None)
    assert fsm.plate.m1_sp is not None, "write_m1_sp не вызывался — плата не управляется"


def test_full_probe_cycle_runs_to_completion(fsm):
    # цикл проходит 20→21→22→23→24→0, а не застревает на входе
    fsm.set_cyclic(True)
    fsm.set_stage(7)
    fsm.set_sv(88.0)
    # дошли до выдержки (23), затем вернулись в ожидание уже ПОСЛЕ неё
    seen = _run_until(fsm, lambda: fsm.mode == 23)
    assert 20 in seen and 21 in seen and 22 in seen and 23 in seen, "цикл застрял: %s" % sorted(seen)
    seen |= _run_until(fsm, lambda: fsm.mode == 0, max_ticks=400)
    assert 24 in seen, "не было шага возврата (24): %s" % sorted(seen)
    assert fsm.mode == 0, "цикл не вернулся в ожидание, mode=%d" % fsm.mode


def _start_cycle(fsm):
    fsm.set_cyclic(True)
    fsm.set_stage(7)
    fsm.set_sv(88.0)


def test_glass_settle_waits_after_approach_only():
    # осадка у стекла: после подвода (22) ждём glass_settle_sec до выдержки (23); отвод/возврат ждут settle_sec (тут 0)
    cfg = copy.deepcopy(plate_config.DEFAULTS)
    cfg["probe_cycle"].update({"pause_sec": 1, "trigger_mode": "time", "pre_wash_sec": 0,
                               "post_wash_pause_sec": 0, "dwell_sec": 0, "settle_sec": 0, "glass_settle_sec": 5})
    cfg["probe_cycle"]["fast"]["enabled"] = False
    f = MicroscopeFSM(FakePlate(), cfg)
    _start_cycle(f)
    _run_until(f, lambda: f.mode == 22)
    ticks = 0
    for _ in range(200):
        v = f.plate.m1_sp
        f.plate.telemetry.update({"pos1": v, "pos1_enc": v, "pos1_ai": v})
        f.tick()
        if f.mode == 23:
            break
        ticks += 1
    assert 50 <= ticks <= 70, "осадка у стекла должна длиться ~5 с (50 тиков), было %d" % ticks
    _run_until(f, lambda: f.mode == 0, max_ticks=400)
    assert f.mode == 0


def _approach_record(arrive_tol, ai_offset):
    """Цикл с датчиком прихода «ai», который стоит на ai_offset мкм дальше цели (как на Усп); возвращает запись прихода подвода."""
    cfg = copy.deepcopy(plate_config.DEFAULTS)
    cfg["probe_cycle"].update({"pause_sec": 1, "trigger_mode": "time", "pre_wash_sec": 0, "arrive_sensor": "ai",
                               "post_wash_pause_sec": 0, "dwell_sec": 0, "settle_sec": 0, "glass_settle_sec": 0})
    if arrive_tol is not None:
        cfg["probe_cycle"]["arrive_tol_um"] = arrive_tol
    cfg["probe_cycle"]["fast"]["enabled"] = False
    f = MicroscopeFSM(FakePlate(), cfg)
    f._step_timeout_ticks = 200                 # таймаут шага (в бою 300 с) — короче, чтобы «не доехал» не крутить 3000 тактов
    recs = []
    f.on_probe_positions = recs.append
    _start_cycle(f)
    for _ in range(800):
        v = f.plate.m1_sp
        if v is not None:
            # сырой датчик приходит в своих единицах, автомат домножает его на sensor_display_scale (0,1 → мкм)
            f.plate.telemetry.update({"pos1": v, "pos1_enc": v, "pos1_ai": (v + ai_offset) / f._sensor_scale})
        f.tick()
        if any(r["step"] == "подвод" for r in recs):
            break
    return [r for r in recs if r["step"] == "подвод"][0]


def test_approach_tolerance_default_is_50():
    assert MicroscopeFSM(FakePlate(), copy.deepcopy(plate_config.DEFAULTS))._arrive_tol_um == 50
    assert _approach_record(None, 40)["timeout"] is False          # в допуске 50 — доехал
    assert _approach_record(None, 70)["timeout"] is True           # датчик на 70 дальше цели — «не доехал» (как было на Усп)


def test_approach_tolerance_setting_lets_far_sensor_arrive():
    assert _approach_record(80, 70)["timeout"] is False            # допуск поднят до 80 — тот же сдвиг датчика уже «доехал»
    assert _approach_record(60, 70)["timeout"] is True             # допуск 60 меньше сдвига — по-прежнему нет


def test_approach_tolerance_is_clamped():
    mk = lambda v: MicroscopeFSM(FakePlate(), {**copy.deepcopy(plate_config.DEFAULTS),
                                               "probe_cycle": {**plate_config.DEFAULTS["probe_cycle"], "arrive_tol_um": v}})._arrive_tol_um
    assert mk(1) == 10 and mk(5000) == 300 and mk(120) == 120


def test_probe_positions_reported_for_retract_and_approach(fsm):
    # мониторинг: на доезде отвода и подвода уходит запись с тремя датчиками — ровно по одной на шаг
    recs = []
    fsm.on_probe_positions = recs.append
    _start_cycle(fsm)
    _run_until(fsm, lambda: fsm.mode == 23)
    _run_until(fsm, lambda: fsm.mode == 0, max_ticks=400)
    assert [r["step"] for r in recs] == ["отвод", "подвод"], recs
    for r in recs:
        assert r["timeout"] is False
        assert r["calc"] == r["enc"] == r["target"], r
        assert isinstance(r["ai"], float) and r["sv"] == 88.0 and r["stage"] == 7 and r["t"] > 0
    assert recs[0]["target"] == fsm._retract_pos


def test_approach_record_carries_sv_table_row(fsm):
    # советы по разломам: запись прихода подвода знает строку СВ-таблицы и её SP (СВ 88 → строка с порогом 88)
    recs = []
    fsm.on_probe_positions = recs.append
    _start_cycle(fsm)
    _run_until(fsm, lambda: fsm.mode == 23)
    ap = [r for r in recs if r["step"] == "подвод"][0]
    row = max(i for i in range(1, 50) if fsm.SVSP[i] > 0 and 88.0 >= fsm.SVSP[i])
    assert ap["row"] == row and ap["sp_row"] == float(fsm.SP[row]) == ap["target"], ap


def test_approach_ai_is_mean_of_three_readings(fsm):
    # аналог подвода «болтается» (-6/0/+6 от цели в замерах раз в 0,5 с) — в запись идёт среднее по 3 замерам, а не один
    recs = []
    fsm.on_probe_positions = recs.append
    _start_cycle(fsm)
    seen_ai = set()
    for _ in range(600):
        v = fsm.plate.m1_sp
        if v is not None:
            k = (fsm.t + 1) // 5
            ai = v + 6 * (k % 3 - 1) if fsm.mode == 22 else v
            seen_ai.add(round(ai, 1))
            fsm.plate.telemetry.update({"pos1": v, "pos1_enc": v, "pos1_ai": ai / fsm._sensor_scale})
        fsm.tick()
        if fsm.mode == 23:
            break
    ap = [r for r in recs if r["step"] == "подвод"][0]
    assert len(seen_ai) >= 3, "датчик в тесте не болтался"
    assert ap["ai"] == round(float(ap["target"]), 1), ap


def test_probe_positions_flag_timeout_when_motor_does_not_arrive(fsm):
    # мотор «не едет» (телеметрия стоит на нуле): шаг уходит по таймауту, в записи timeout=True
    recs = []
    fsm.on_probe_positions = recs.append
    fsm._step_timeout_ticks = 5
    _start_cycle(fsm)
    _run_until(fsm, lambda: len(recs) >= 2, max_ticks=300, track_arrival=False)
    assert [r["step"] for r in recs[:2]] == ["отвод", "подвод"], recs
    assert all(r["timeout"] is True for r in recs[:2]), recs
    assert recs[0]["enc"] == 0 and recs[0]["target"] == fsm._retract_pos


def test_probe_positions_callback_error_does_not_break_cycle(fsm):
    def boom(_):
        raise RuntimeError("упал приёмник записей")
    fsm.on_probe_positions = boom
    _start_cycle(fsm)
    _run_until(fsm, lambda: fsm.mode == 23)
    _run_until(fsm, lambda: fsm.mode == 0, max_ticks=400)
    assert fsm.mode == 0, "цикл встал из-за ошибки в колбэке положений"


def test_estop_stops_both_motors(fsm):
    fsm.set_cyclic(True)
    fsm.set_stage(7)
    fsm.set_movement_inhibit(True)     # SW3 + halt
    fsm.tick()
    assert fsm.plate.stop_calls[:2] == [1, 2], "аварийный стоп не послал motor_stop обоим: %s" % fsm.plate.stop_calls


def test_manual_mode_does_not_write_plate(fsm):
    fsm.set_manual(True)
    fsm.set_stage(7)
    for _ in range(5):
        fsm.tick()
    # в ручном режиме автомат вообще не трогает плату (ранний return)
    assert fsm.plate.m1_sp is None and not fsm.plate.leds


def test_auto_cycle_starts_probe_by_time_trigger(fsm):
    fsm.set_cyclic(True)
    fsm.set_stage(7)              # стадия в рабочей зоне 3..9
    fsm.set_sv(88.0)
    # pause_sec=1 → ~10 тиков в простое, потом mode 20 (отвод)
    started = False
    for _ in range(30):
        fsm.tick()
        if fsm.mode != 0:
            started = True
            break
    assert started, "авто-цикл не стартовал пробу при стадии 7 и включённом цикле"
    assert fsm.mode == 20


def test_no_cycle_outside_stage_range(fsm):
    fsm.set_cyclic(True)
    fsm.set_stage(1)             # вне 3..9 — цикл не стартует
    fsm.set_sv(88.0)
    for _ in range(30):
        fsm.tick()
    assert fsm.mode == 0


def test_stop_movement_inhibits(fsm):
    fsm.set_cyclic(True)
    fsm.set_stage(7)
    fsm.set_movement_inhibit(True)
    for _ in range(30):
        fsm.tick()
    # SW3 (стоп движения) — моторная уставка на плату не пишется
    assert fsm.plate.m1_sp is None


def test_take_sample_from_idle(fsm):
    res = fsm.start_sample()
    assert res["status"] == "started"
    assert fsm.mode == 20


def test_reset_cycle_returns_to_idle(fsm):
    fsm.start_sample()
    fsm.reset_cycle()
    assert fsm.mode == 0


# ---------- окно перезапуска для автообновления (restart_window) ----------

def test_restart_window_needs_known_stage(fsm):
    ok, why = fsm.restart_window()
    assert not ok and "стадия" in why                   # от ПЛК ещё ничего не пришло


@pytest.mark.parametrize("stage", [1, 11, 12, 13, 14])
def test_restart_window_open_after_boil(fsm, stage):
    fsm.set_stage(stage)
    assert fsm.restart_window() == (True, "")


@pytest.mark.parametrize("stage", [0, 2, 3, 5, 7, 9, 10, 20, 21])
def test_restart_window_closed_on_boil_and_unknown_stages(fsm, stage):
    fsm.set_stage(stage)
    ok, why = fsm.restart_window()
    assert not ok and "стадия %d" % stage in why


def test_restart_window_closed_while_probe_runs(fsm):
    fsm.set_stage(12)
    fsm.start_sample()
    ok, why = fsm.restart_window()
    assert not ok and "проба" in why


def test_restart_window_closed_in_manual_fault_and_no_link(fsm):
    fsm.set_stage(12)
    fsm.manual = True
    assert fsm.restart_window()[1] == "ручной режим"
    fsm.manual = False
    fsm.set_movement_inhibit(True)
    assert "авария" in fsm.restart_window()[1]
    fsm.set_movement_inhibit(False)
    fsm._halt = False
    fsm.plate.status["connected"] = False
    assert fsm.restart_window()[1] == "нет связи с платой"


def test_restart_window_closed_while_cv_busy_and_ignore_stage(fsm):
    fsm.set_stage(12)
    fsm.cv_begin()
    assert not fsm.restart_window()[0]
    fsm.cv_end()
    assert fsm.restart_window()[0]
    fsm.set_ignore_stage(True)
    assert "без стадии" in fsm.restart_window()[1]


def test_restart_window_waits_for_zero_calibration(fsm):
    fsm.set_autocal(enabled=True, every_n=1)
    fsm.set_stage(10)
    fsm.tick()
    fsm.set_stage(11)                                   # фронт пропарки: варок 1/1 — калибровка стартует сразу
    fsm.tick()
    ok, why = fsm.restart_window()
    assert not ok and "калибровк" in why
    fsm._cal_active = False                             # калибровка кончилась, но своя очередь ещё не снята
    fsm.mode = 0
    fsm._ac_armed = True
    assert "калибровк" in fsm.restart_window()[1]
    fsm._ac_armed = False
    fsm._unflag = {1: 0, 2: 0}
    assert fsm.restart_window() == (True, "")


def test_restart_window_open_when_calibration_not_due(fsm):
    fsm.set_autocal(enabled=True, every_n=3)            # варка 1 из 3: калибровки на этой пропарке не будет
    fsm.set_stage(10)
    fsm.tick()
    fsm.set_stage(11)
    fsm.tick()
    assert fsm.restart_window() == (True, "")


def test_take_sample_from_manual_enables_cycle(fsm):
    # «Взять пробу» из Ручного снимает ручной (тумблер «Автомат» в UI) — авто-цикл (sw0) должен быть включён,
    # иначе после пробы «Ожидание — «Автомат» выключен» при зелёном тумблере
    fsm.manual = True
    assert fsm.start_sample()["status"] == "started"
    assert not fsm.manual and fsm.sw0
    assert fsm.state["cyclic"]


def test_autocal_from_manual_returns_manual_on_abort(fsm):
    # калибровка кнопкой из ручного; оборвалась по «стопу движения» — ручной возвращается, а не «Автомат» без цикла
    fsm.manual = True
    assert fsm.start_autocal()["status"] == "started"
    assert not fsm.manual and fsm._cal_return_manual
    fsm.set_movement_inhibit(True)
    fsm.tick()
    assert not fsm._cal_active
    assert fsm.manual and not fsm.sw0
    assert fsm.state["manual"]


def test_autocal_abort_by_operator_manual_keeps_manual(fsm):
    # человек сам нажал «Ручной» во время калибровки — ручной остаётся, повторного возврата нет
    fsm.manual = True
    fsm.start_autocal()
    fsm.set_manual(True)
    fsm.tick()
    assert not fsm._cal_active and fsm.manual and not fsm._cal_return_manual


def test_autocal_trace_is_collected_and_given_to_log_on_finish(fsm):
    # диагностика отвода: раз в секунду пишем фазу/цель/датчики, по окончании отдаём в колбэк журнала
    got = []
    fsm.on_autocal_done = got.append
    fsm.manual = True
    fsm.start_autocal()
    for _ in range(25):
        fsm.tick()
    assert len(fsm._cal_trace) >= 2 and "pre" in fsm._cal_trace[0]
    fsm.set_movement_inhibit(True)
    fsm.tick()
    assert got and got[0]["trace"] and got[0]["retract_pos"] == fsm._retract_pos


def test_slip_detected_needs_sensor_at_glass_swing_and_reversals():
    from microscope_fsm import slip_detected
    wiggle = [0, 150, 0, 120, 0, 150, 10, 140, 0, 150] * 2          # 0–150 туда-сюда
    assert slip_detected(wiggle, 5)
    assert not slip_detected(wiggle, 900)                           # датчик далеко от стекла — это не буксование
    assert not slip_detected(wiggle[:10], 5)                        # окно ещё не набралось
    ride = [i * 20 for i in range(20)]                              # честный ход к нулю: одна сторона
    assert not slip_detected(ride, 5)
    assert not slip_detected([0, 20, 0, 20] * 5, 5)                 # дрожь меньше размаха
    assert not slip_detected(wiggle, None)


def _slip_run(fsm, slips_wanted, max_ticks=2500):
    """Автокалибровка; в «Поиске 0» энкодер долбит 0–150 (датчик у нуля) slips_wanted раз подряд, потом честно встаёт."""
    calls = []
    p = fsm.plate
    p.motor_stop = lambda m: calls.append("stop")
    p.motor_find_zero = lambda m: calls.append("find_zero")
    p.motor_steps = lambda m, n: calls.append("steps0")
    fsm.manual = True
    fsm.start_autocal()
    n = 0
    for _ in range(max_ticks):
        ph = fsm._cal_phase
        if ph == "move":
            n += 1
            v = 150 if n % 2 else 0
            if calls.count("stop") >= slips_wanted:
                v = 0                                               # дальше без буксования — мотор встаёт у нуля
            p.telemetry.update({"pos1": v, "pos1_enc": v, "pos1_ai": 5})
        elif p.m1_sp is not None and ph not in ("pre", "find_zero"):
            v = p.m1_sp
            p.telemetry.update({"pos1": v, "pos1_enc": v, "pos1_ai": v / fsm._sensor_scale})   # сырой датчик: FSM умножает на масштаб
        fsm.tick()
        if not fsm._cal_active:
            break
    return calls


def test_autocal_slip_stop_retract_and_retry_succeeds(fsm):
    # буксование один раз: Стоп М1, «шаги 0» (снять флаг поиска), отвод на 3000, «Поиск 0» ещё раз — и калибровка сошлась
    calls = _slip_run(fsm, 1)
    assert calls[:4] == ["find_zero", "stop", "steps0", "find_zero"], calls
    assert calls.count("stop") == 1 and fsm._cal_slips == 1
    assert not fsm._cal_failed, fsm._cal_fail_msg


def test_autocal_slip_twice_gives_red_alarm(fsm):
    # повтор тоже забуксовал: больше не пробуем — красная плашка (она уходит в мониторинг), голова отведена
    calls = _slip_run(fsm, 2)
    assert calls.count("stop") == 2 and calls.count("find_zero") == 2, calls
    assert fsm._cal_failed and "буксует" in fsm._cal_fail_msg, fsm._cal_fail_msg
    assert not fsm._cal_active


def test_autocal_without_slip_never_sends_stop(fsm):
    calls = _slip_run(fsm, 0)
    assert "stop" not in calls and fsm._cal_slips == 0


def _glass_washes(every, probes):
    # сколько проб из `probes` промывали стекло (клапан glass открывался на шаге 21); трубка — в каждой пробе
    cfg = copy.deepcopy(plate_config.DEFAULTS)
    cfg["probe_cycle"].update({"pause_sec": 1, "trigger_mode": "time", "pre_wash_sec": 1,
                               "post_wash_pause_sec": 0, "dwell_sec": 0, "settle_sec": 0, "glass_settle_sec": 0,
                               "glass_wash_every": every})
    cfg["probe_cycle"]["fast"]["enabled"] = False
    f = MicroscopeFSM(FakePlate(), cfg)
    res = []
    for _ in range(probes):
        f.plate.valves.clear()
        _start_cycle(f)
        _run_until(f, lambda: f.mode == 23)
        on = {k for k, v in f.plate.valves if v}
        assert "tube" in on                       # трубка промывается в каждой пробе
        res.append("glass" in on)
        _run_until(f, lambda: f.mode == 0, max_ticks=400)
    return res


def test_glass_wash_every_n_probes():
    assert _glass_washes(1, 3) == [True, True, True]               # как раньше: каждую пробу
    assert _glass_washes(3, 7) == [True, False, False, True, False, False, True]
    assert _glass_washes(0, 2) == [False, False]                   # 0 — стекло не промывать


# ---------- Авария: М1 N раз подряд не доехал за таймаут шага (fault_guard.m1_timeouts) ----------

def _stuck(fsm, recs, n, max_ticks=600):
    """Мотор «стоит» (телеметрия не едет): крутим, пока не наберётся n записей доезда (отвод/подвод) или Аварии."""
    _run_until(fsm, lambda: len(recs) >= n or fsm._fault, max_ticks=max_ticks, track_arrival=False)


def test_three_m1_timeouts_in_row_raise_fault(fsm):
    # отвод, подвод, возврат — все три не доехали: Авария, цикл стоп, «Автомат» выкл., в режиме — fault
    recs = []
    fsm.on_probe_positions = recs.append
    fsm._step_timeout_ticks = 5
    _start_cycle(fsm)
    _stuck(fsm, recs, 99)
    assert fsm._fault, "Авария не поднялась после трёх таймаутов"
    assert [r["step"] for r in recs] == ["отвод", "подвод"]      # возврат записей не пишет — третий таймаут на нём
    assert fsm.mode == 0 and fsm.sw0 is False and fsm.sw3 is True
    assert fsm.mode_state() == "fault"
    st = fsm.state
    assert st["fault"] and "подряд: 3" in st["fault_msg"] and "возврат" in st["fault_msg"]


def test_fault_after_approach_timeout_skips_dwell(fsm):
    # порог 2: отвод и подвод не доехали — Авария на подводе, выдержки (кадры мимо стекла) нет
    fsm.set_fault_guard(m1_timeouts=2)
    fsm._step_timeout_ticks = 5
    photos = []
    fsm.on_photo = lambda: photos.append(1)
    _start_cycle(fsm)
    seen = _run_until(fsm, lambda: fsm._fault, max_ticks=600, track_arrival=False)
    assert fsm._fault and "подвод" in fsm._fault_msg
    assert 23 not in seen and not photos
    for _ in range(50):                       # Авария держится: новых проб нет
        fsm.tick()
    assert fsm.mode == 0


def test_normal_arrival_resets_m1_timeout_count(fsm):
    # два таймаута, потом нормальный доезд — счёт с нуля, Аварии нет
    recs = []
    fsm.on_probe_positions = recs.append
    fsm._step_timeout_ticks = 5
    _start_cycle(fsm)
    _stuck(fsm, recs, 2)
    assert fsm._m1_timeouts == 2 and not fsm._fault
    _run_until(fsm, lambda: fsm.mode == 0, max_ticks=400)      # возврат доехал
    assert fsm._m1_timeouts == 0 and not fsm._fault


def test_m1_timeout_guard_off_with_zero(fsm):
    assert fsm.set_fault_guard(m1_timeouts=0) == {"m1_timeouts": 0}
    recs = []
    fsm.on_probe_positions = recs.append
    fsm._step_timeout_ticks = 5
    _start_cycle(fsm)
    _stuck(fsm, recs, 6, max_ticks=2000)       # три цикла подряд мимо — защита выключена
    assert len(recs) >= 6 and not fsm._fault


def test_clear_fault_keeps_auto_off(fsm):
    fsm.set_fault_guard(m1_timeouts=1)
    fsm._step_timeout_ticks = 5
    _start_cycle(fsm)
    _run_until(fsm, lambda: fsm._fault, max_ticks=300, track_arrival=False)
    assert fsm._fault
    assert fsm.clear_fault() == {"status": "cleared"}
    assert not fsm._fault and fsm.sw3 is False and fsm._m1_timeouts == 0
    assert fsm.sw0 is False and fsm.mode_state() == "off"       # «Автомат» оператор включает сам
    for _ in range(30):
        fsm.tick()
    assert fsm.mode == 0


def test_fault_guard_default_from_config():
    cfg = copy.deepcopy(plate_config.DEFAULTS)
    assert cfg["fault_guard"]["m1_timeouts"] == 3
    cfg["fault_guard"]["m1_timeouts"] = 5
    assert MicroscopeFSM(FakePlate(), cfg).state["m1_timeouts_limit"] == 5
    del cfg["fault_guard"]                     # старый конфиг без раздела — умолчание 3
    assert MicroscopeFSM(FakePlate(), cfg)._m1_timeout_limit == 3
