"""Эмулятор платы микроскопа `micro` (Modbus TCP server) для отладки без железа.

Имитирует то, что делает реальная плата:
  * принимает WRITE-блок OUT[0..9] (1250..1259): команды моторам, уставки, клапаны, LED;
  * по команде 0x1006/0x2006 «двигает» мотор — pos едет к заданному SP;
  * отдаёт READ-блок IN[0..9] (1270..1279): позиции, температуру, 12В и т.д.

Запуск (из корня проекта):  python scripts/plate_sim.py  [--host 0.0.0.0] [--port 15020]
Порт по умолчанию 15020 (локальная отладка). Реальная плата — 502.

Геометрию регистров берём из plate_config.DEFAULTS, чтобы эмулятор и клиент совпадали.
"""
import argparse
import os
import random
import sys
import threading
import time

# корень проекта в путь, чтобы импортировать plate_config/paths/logger из scripts/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pymodbus.datastore import ModbusSequentialDataBlock, ModbusSlaveContext, ModbusServerContext
from pymodbus.server import StartTcpServer

import plate_config

CFG = plate_config.DEFAULTS
HR = 3  # function code: holding registers

WRITE_BASE = CFG["write_base"]      # 1250
READ_BASE = CFG["read_base"]        # 1270
OUT = CFG["out"]
IN = CFG["in"]
MOT = CFG["motor"]                  # нативная карта команд (1248/1249 и т.д.)

# шаг мотора за такт (в единицах регистра = 10 мкм). 200 -> ~2 мм/такт
MOTOR_STEP = 200
HOME_END = 65000                    # «в конец» — условный дальний предел (ед. 10 мкм)


def _reg(mapping, m):
    return int(mapping[str(m)])


def _read_word(ctx, addr):
    return ctx.getValues(HR, int(addr), 1)[0]


def _write_word(ctx, addr, value):
    ctx.setValues(HR, int(addr), [int(value) & 0xFFFF])


def _move_toward(current, target, step):
    if current < target:
        return min(current + step, target)
    if current > target:
        return max(current - step, target)
    return current


def _decode_cmd(code):
    """Код команды -> (мотор, операция). База М1=0x1000, М2=0x2000; смещение = операция."""
    for m in (1, 2):
        base = int(MOT["cmd_base"][str(m)])
        if base <= code <= base + 0x0FFF:
            off = code - base
            for op, o in MOT["cmd_op"].items():
                if int(o) == off:
                    return m, op
            return m, None
    return None, None


def sim_loop(ctx, period):
    """Раз в такт: обрабатываем нативные команды (1248/1249), двигаем позиции, отдаём телеметрию.
    Командный регистр «потребляется» (сбрасывается в 0) после обработки — так повторное нажатие
    той же кнопки (одинаковый код) снова триггерит команду, как на реальной плате."""
    pos = {1: 0, 2: 0}
    target = {1: 0, 2: 0}

    _write_word(ctx, 1520, 6)      # версия софта (чтобы пробник нашёл unit)
    _write_word(ctx, 1521, 32)     # версия модуля (hw2.0)
    _write_word(ctx, 1531, 65535)  # серийник
    tick = 0
    while True:
        # 1) обработать команды моторов (edge через «потребление» регистра)
        for m in (1, 2):
            creg = _reg(MOT["cmd_reg"], m)
            code = _read_word(ctx, creg)
            if code:
                mm, op = _decode_cmd(code)
                if mm == m and op:
                    if op == "goto":
                        target[m] = _read_word(ctx, _reg(MOT["pos_set"], m))
                    elif op == "shift":
                        target[m] = pos[m] + _read_word(ctx, _reg(MOT["shift"], m))
                    elif op == "steps":
                        target[m] = pos[m] + _read_word(ctx, _reg(MOT["steps"], m))
                    elif op == "home_start" or op == "find_zero":
                        target[m] = 0
                    elif op == "home_end":
                        target[m] = HOME_END
                    elif op == "stop":
                        target[m] = pos[m]
                    elif op == "set_zero":
                        pos[m] = 0
                        target[m] = 0
                _write_word(ctx, creg, 0)   # потребить команду

            pos[m] = _move_toward(pos[m], target[m], MOTOR_STEP)
            moving = pos[m] != target[m]
            _write_word(ctx, _reg(MOT["pos_read"], m), pos[m])       # абс. позиция (1274/1275)
            _write_word(ctx, _reg(MOT["enable"], m), 1 if moving else _read_word(ctx, _reg(MOT["enable"], m)))

        pos1, pos2 = pos[1], pos[2]

        # 2) основной входной блок IN (1270..1279): позиции, темп, 12В, датчик
        _write_word(ctx, READ_BASE + IN["pos1"]["off"], pos1)   # 1274
        _write_word(ctx, READ_BASE + IN["pos2"]["off"], pos2)   # 1275
        _write_word(ctx, READ_BASE + IN["pos1_ai"]["off"], pos1)  # 1271 датчик ~ позиция М1
        _write_word(ctx, READ_BASE + IN["temp"]["off"], 39 + random.randint(-1, 1))    # 1273 °C
        _write_word(ctx, READ_BASE + IN["u12v"]["off"], 12030 + random.randint(-10, 10))  # 1272 мВ
        _write_word(ctx, READ_BASE + IN["di"]["off"], 0)

        # 3) полная телеметрия (родные регистры платы) — чтобы страница показывала всё
        _write_word(ctx, 150, 0b000101)                       # DI (нативный) — пара входов
        _write_word(ctx, 1285, pos1)                          # энкодер М1 ≈ позиция
        _write_word(ctx, 1280, pos1 // 2)                     # шаги М1
        _write_word(ctx, 1281, pos2 // 2)                     # шаги М2
        _write_word(ctx, 1276, random.randint(0, 4))          # расхождение М1
        _write_word(ctx, 1300, 1 if pos2 != target[2] else 0)  # state М2
        _write_word(ctx, 1282, 3200 + random.randint(-40, 40))  # FAN1
        _write_word(ctx, 1283, 3100 + random.randint(-40, 40))  # FAN2
        _write_word(ctx, 1528, 57)                            # время цикла (мкс)
        _write_word(ctx, 1529, 60)                            # время цикла пиковое
        _write_word(ctx, 1523, tick // 10)                    # секунд от старта
        _write_word(ctx, 1532, 0b0000000000000011)            # инициализация + LAN ok
        # настройки моторов (read-only вкладка)
        for reg, val in ((1205, 100), (1207, 90), (1206, 20), (1208, 20), (1209, 500),
                         (1210, 500), (1231, 80), (1233, 60), (1234, 50), (1236, 300),
                         (1212, 3), (1214, 200), (1217, 200), (1215, 2000), (1218, 2000),
                         (1213, 100), (1216, 100)):
            _write_word(ctx, reg, val)
        # охлаждение (read-only вкладка)
        for reg, val in ((1221, 45), (1222, 40), (1223, 50), (1224, 45), (1225, 55), (1226, 50)):
            _write_word(ctx, reg, val)

        # для этапа A — имитируем ПЛК: СВ (×100) и стадия варки
        _write_word(ctx, 500, 8262 + random.randint(-3, 3))
        _write_word(ctx, 501, 5)

        tick += 1
        if tick % 20 == 0:
            dq = _read_word(ctx, WRITE_BASE + OUT["valves"])
            led = _read_word(ctx, CFG["led"]["bright"])
            print(f"[sim] pos1={pos1} pos2={pos2} t1={target[1]} t2={target[2]} "
                  f"dq={dq:06b} led={led}% freq={_read_word(ctx, CFG['led']['freq'])}", flush=True)
        time.sleep(period)


def main():
    ap = argparse.ArgumentParser(description="Эмулятор платы микроскопа (Modbus TCP)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=15020)
    args = ap.parse_args()

    # блок holding-регистров 0..1599 (покрывает и системные 1520..1545), zero_mode -> адрес == индекс
    block = ModbusSequentialDataBlock(0, [0] * 1600)
    slave = ModbusSlaveContext(hr=block, zero_mode=True)
    ctx = ModbusServerContext(slaves=slave, single=True)

    period = CFG["poll_interval_ms"] / 1000.0
    threading.Thread(target=sim_loop, args=(slave, period), daemon=True).start()

    print(f"Эмулятор платы микроскопа слушает {args.host}:{args.port} "
          f"(WRITE {WRITE_BASE}.., READ {READ_BASE}..). Ctrl+C — стоп.", flush=True)
    StartTcpServer(context=ctx, address=(args.host, args.port))


if __name__ == "__main__":
    main()
