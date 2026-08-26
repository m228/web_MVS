"""Read-only диагностика РЕАЛЬНОЙ платы микроскопа по Modbus TCP. НИЧЕГО НЕ ПИШЕТ.

Первый безопасный шаг проверки платы без ПЛК: убедиться, что плата отвечает по сети,
на каком unit, и что входные регистры (1270..) осмысленны (позиции/темп/12В). Движение
НЕ трогаем — только чтение.

Запуск из корня проекта:
  python scripts/plate_probe.py                       # host/порт из plate_config, автоперебор unit
  python scripts/plate_probe.py --host 192.168.1.153  # явный IP платы
  python scripts/plate_probe.py --unit 255            # конкретный unit
  python scripts/plate_probe.py --scan 1240 1290      # + скан диапазона регистров
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pymodbus.client import ModbusTcpClient

import plate_config


def read(client, addr, count, unit):
    try:
        rr = client.read_holding_registers(addr, count=count, slave=unit)
        return None if rr.isError() else rr.registers
    except Exception:
        return None


def main():
    cfg = plate_config.load()
    ap = argparse.ArgumentParser(description="Read-only проба платы микроскопа")
    ap.add_argument("--host", default=cfg["host"])
    ap.add_argument("--port", type=int, default=cfg["port"])
    ap.add_argument("--unit", type=int, default=None, help="если не задан — перебор 255,1,0,254")
    ap.add_argument("--scan", nargs=2, type=int, metavar=("FROM", "TO"),
                    help="просканировать диапазон holding-регистров")
    args = ap.parse_args()

    read_base, write_base, block_len = cfg["read_base"], cfg["write_base"], cfg["block_len"]
    print(f"Плата {args.host}:{args.port}  (read {read_base}.., write {write_base}..)")
    print("-" * 60)

    client = ModbusTcpClient(args.host, port=args.port, timeout=2.0)
    if not client.connect():
        print("НЕТ TCP-СВЯЗИ. Проверь: плата запитана, в сети, IP/порт верны, файрвол.")
        return
    print("TCP-соединение: OK")

    units = [args.unit] if args.unit is not None else [255, 1, 0, 254]
    good = None
    for u in units:
        regs = read(client, read_base, block_len, u)
        if regs is not None:
            print(f"unit {u}: ОТВЕЧАЕТ  ->  read {read_base}..{read_base+block_len-1}: {regs}")
            good = u
            break
        print(f"unit {u}: нет ответа")

    if good is None:
        print("\nПлата не ответила ни на один unit по Modbus. Связь TCP есть, но регистры "
              "не читаются — уточни адрес чтения/unit у поставщика платы.")
        client.close()
        return

    regs = read(client, read_base, block_len, good)
    print(f"\nВходы IN[0..{block_len-1}] (unit {good}), по карте plate_config:")
    for name, spec in cfg["in"].items():
        off = spec["off"]
        if off < len(regs):
            raw = regs[off]
            print(f"  IN[{off}] {name:8s}: raw={raw:6d}  ->  {raw * spec['scale']}")

    out = read(client, write_base, block_len, good)
    print(f"\nВыходной блок WRITE[{write_base}..] (текущее содержимое): {out}")

    if args.scan:
        lo, hi = args.scan
        print(f"\nСкан регистров {lo}..{hi}:")
        for a in range(lo, hi + 1):
            r = read(client, a, 1, good)
            if r is not None and r[0] != 0:
                print(f"  {a}: {r[0]}")
        print("(показаны только ненулевые)")

    client.close()
    print("\nГотово. Это была ТОЛЬКО ПРОВЕРКА ЧТЕНИЯ — плата не тронута.")


if __name__ == "__main__":
    main()
