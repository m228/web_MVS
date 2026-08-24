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

# шаг мотора за такт (в единицах регистра = 10 мкм). 200 -> ~2 мм/такт
MOTOR_STEP = 200
CMD_SET_SP1 = 0x1006
CMD_SET_SP2 = 0x2006


def _read_word(ctx, addr):
    return ctx.getValues(HR, addr, 1)[0]


def _write_word(ctx, addr, value):
    ctx.setValues(HR, addr, [int(value) & 0xFFFF])


def _move_toward(current, target, step):
    if current < target:
        return min(current + step, target)
    if current > target:
        return max(current - step, target)
    return current


def sim_loop(ctx, period):
    """Раз в такт: читаем команды из OUT, двигаем позиции, обновляем IN."""
    # начальные значения входов
    pos1 = 0
    pos2 = 0
    # цель защёлкивается по импульсу команды и держится, пока не доедем (как реальная плата)
    target1 = 0
    target2 = 0
    _write_word(ctx, READ_BASE + IN["pos1"]["off"], pos1)
    _write_word(ctx, READ_BASE + IN["pos2"]["off"], pos2)
    tick = 0
    while True:
        cmd1 = _read_word(ctx, WRITE_BASE + OUT["cmd1"])
        cmd2 = _read_word(ctx, WRITE_BASE + OUT["cmd2"])
        m1_sp = _read_word(ctx, WRITE_BASE + OUT["m1_sp"])   # ед. 10 мкм
        m2_sp = _read_word(ctx, WRITE_BASE + OUT["m2_sp"])

        # команда «установить SP» защёлкивает цель; едем к ней и после сброса cmd
        if cmd1 == CMD_SET_SP1:
            target1 = m1_sp
        if cmd2 == CMD_SET_SP2:
            target2 = m2_sp
        pos1 = _move_toward(pos1, target1, MOTOR_STEP)
        pos2 = _move_toward(pos2, target2, MOTOR_STEP)

        # обновляем входной блок IN (то, что читает клиент)
        _write_word(ctx, READ_BASE + IN["pos1"]["off"], pos1)
        _write_word(ctx, READ_BASE + IN["pos2"]["off"], pos2)
        # аналоговая позиция ~ позиция М1 (для логики «pos1_ai<20 = увидел стекло»)
        _write_word(ctx, READ_BASE + IN["pos1_ai"]["off"], pos1)
        # температура ~25.0 °C и питание ~12.00 В с лёгким шумом
        _write_word(ctx, READ_BASE + IN["temp"]["off"], 250 + random.randint(-2, 2))
        _write_word(ctx, READ_BASE + IN["u12v"]["off"], 1200 + random.randint(-5, 5))
        _write_word(ctx, READ_BASE + IN["di"]["off"], 0)

        tick += 1
        if tick % 20 == 0:  # раз ~2 с печатаем состояние
            valves = _read_word(ctx, WRITE_BASE + OUT["valves"])
            print(f"[sim] pos1={pos1} pos2={pos2} m1_sp={m1_sp} cmd1=0x{cmd1:04x} "
                  f"valves={valves:02b} led_br={_read_word(ctx, WRITE_BASE + OUT['led_bright'])}",
                  flush=True)
        time.sleep(period)


def main():
    ap = argparse.ArgumentParser(description="Эмулятор платы микроскопа (Modbus TCP)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=15020)
    args = ap.parse_args()

    # блок holding-регистров 0..1299 (покрывает 1250..1279), zero_mode -> адрес == индекс
    block = ModbusSequentialDataBlock(0, [0] * 1300)
    slave = ModbusSlaveContext(hr=block, zero_mode=True)
    ctx = ModbusServerContext(slaves=slave, single=True)

    period = CFG["poll_interval_ms"] / 1000.0
    threading.Thread(target=sim_loop, args=(slave, period), daemon=True).start()

    print(f"Эмулятор платы микроскопа слушает {args.host}:{args.port} "
          f"(WRITE {WRITE_BASE}.., READ {READ_BASE}..). Ctrl+C — стоп.", flush=True)
    StartTcpServer(context=ctx, address=(args.host, args.port))


if __name__ == "__main__":
    main()
