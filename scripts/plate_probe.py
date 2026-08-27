"""Read-only диагностика РЕАЛЬНОЙ платы микроскопа по Modbus TCP. НИЧЕГО НЕ ПИШЕТ.

Карта регистров — из родного конфигуратора платы («ПЧ Модуль», скрины Макса 2026-08-26).
Плата: 192.168.1.128:502, два Ethernet (мини-свитч: сеть + камера).

Запуск из корня проекта:
  python scripts/plate_probe.py --host 192.168.1.128        # прочитать всю карту
  python scripts/plate_probe.py --host 192.168.1.128 --unit 255
  python scripts/plate_probe.py --host 192.168.1.128 --scan 1200 1300   # + скан диапазона
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# не падать на символах, которых нет в кодировке консоли (cp866/cp1251)
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

from pymodbus.client import ModbusTcpClient

import plate_config

# (адрес, имя, тип): тип "bits" -> печатаем двоично, "pos" -> raw и raw*10 (мкм), иначе как есть.
REGISTER_MAP = [
    ("--- СИСТЕМА ---", None, None),
    (1520, "версия софта", None),
    (1521, "версия модуля", None),
    (1523, "секунд от старта", None),
    (1524, "кругов секундомера от старта", None),
    (1531, "серийный номер", None),
    (1528, "время цикла (мкс)", None),
    (1529, "время цикла пиковое (мкс)", None),
    (1532, "статус биты (init/LAN/откл12V)", "bits"),
    (1533, "статус биты (EEPROM/FLASH)", "bits"),
    (1535, "блокировки", "bits"),

    ("--- ПИТАНИЕ / ТЕМПЕРАТУРА ---", None, None),
    (1272, "питание 12V (мВ)", None),
    (1273, "температура (C)", None),
    (1282, "обороты FAN1", None),
    (1283, "обороты FAN2", None),
    (1542, "откл. 12V", None),

    ("--- ВХОДЫ / ВЫХОДЫ ---", None, None),
    (150, "DI (дискр. входы)", "bits"),
    (1250, "DQ (клапаны: .0 трубка .1 стекло .2 воздух)", "bits"),
    (1271, "датчик перемещения", "pos"),

    ("--- МОТОР М1 (LED) ---", None, None),
    (1219, "разрешение мотора М1", None),
    (1240, "направление вперёд М1", None),
    (1274, "абс. позиция М1", "pos"),
    (1280, "абс. позиция М1 (шаги)", None),
    (1276, "расхождение М1", "pos"),
    (1285, "позиция М1 по энкодеру", "pos"),
    (1253, "установить позицию М1 (мкм)", None),
    (1246, "сдвинуть на X мкм М1", None),
    (1242, "сделать шагов М1", None),
    (1205, "скорость М1 (мм/с)", None),
    (1206, "мин скорость М1", None),
    (1209, "ускорение/замедление М1", None),
    (1213, "шагов на 1 мм М1", None),
    (1214, "шагов на оборот М1", None),
    (1215, "расстояние на оборот М1 (мкм)", None),
    (1231, "макс ход М1 (мм)", None),
    (1234, "стоп М1 при полож. аналог. датч.", None),
    (1236, "лимит проскальзывания М1", None),

    ("--- МОТОР М2 (фокус) ---", None, None),
    (1220, "разрешение мотора М2", None),
    (1241, "направление вперёд М2", None),
    (1275, "абс. позиция М2", "pos"),
    (1281, "абс. позиция М2 (шаги)", None),
    (1300, "state М2", None),
    (1254, "установить позицию М2 (мкм)", None),
    (1247, "сдвинуть на X мкм М2", None),
    (1243, "сделать шагов М2", None),
    (1207, "скорость М2 (мм/с)", None),
    (1208, "мин скорость М2", None),
    (1210, "ускорение/замедление М2", None),
    (1216, "шагов на 1 мм М2", None),
    (1217, "шагов на оборот М2", None),
    (1218, "расстояние на оборот М2 (мкм)", None),
    (1233, "макс ход М2 (мм)", None),

    ("--- LED ---", None, None),
    (1257, "яркость подсветки (%)", None),
    (1202, "частота импульсов LED (Гц)", None),

    ("--- ОХЛАЖДЕНИЕ ---", None, None),
    (1221, "темп вкл FAN", None),
    (1222, "темп откл FAN", None),
    (1223, "темп вкл воздуха", None),
    (1224, "темп откл воздуха", None),
    (1225, "темп вкл камеры", None),
    (1226, "темп откл камеры", None),

    ("--- СЧЁТЧИКИ / ЭНКОДЕРЫ ---", None, None),
    (176, "счётчик1 период импульсов", None),
    (177, "счётчик1 ширина импульсов", None),
    (179, "счётчик1 нараст. мл.слово", None),
    (181, "счётчик2 период импульсов", None),
    (182, "счётчик2 ширина импульсов", None),
    (184, "счётчик2 нараст. мл.слово", None),
    (186, "положение энкодера1", "pos"),
    (187, "положение энкодера2", "pos"),
    (1212, "делитель шагов", None),
    (1544, "настройка счётчика1", "bits"),
    (1545, "настройка счётчика2", "bits"),
]


def read(client, addr, count, unit):
    try:
        rr = client.read_holding_registers(addr, count=count, slave=unit)
        return None if rr.isError() else rr.registers
    except Exception:
        return None


def fmt(addr, name, kind, val):
    if val is None:
        return f"  #{addr:<5} {name:42s}: -"
    if kind == "bits":
        return f"  #{addr:<5} {name:42s}: {val}  (bin {val:016b})"
    if kind == "pos":
        return f"  #{addr:<5} {name:42s}: raw={val}  (x10={val*10} мкм?)"
    return f"  #{addr:<5} {name:42s}: {val}"


def main():
    cfg = plate_config.load()
    ap = argparse.ArgumentParser(description="Read-only проба платы микроскопа")
    ap.add_argument("--host", default=cfg["host"])
    ap.add_argument("--port", type=int, default=cfg["port"])
    ap.add_argument("--unit", type=int, default=None, help="если не задан — перебор 255,1,0,254")
    ap.add_argument("--scan", nargs=2, type=int, metavar=("FROM", "TO"),
                    help="просканировать диапазон holding-регистров (ненулевые)")
    args = ap.parse_args()

    print(f"Плата {args.host}:{args.port}")
    print("=" * 72)

    client = ModbusTcpClient(args.host, port=args.port, timeout=2.0)
    if not client.connect():
        print("НЕТ TCP-СВЯЗИ. Проверь: плата запитана, в сети, IP/порт верны, файрвол.")
        return
    print("TCP-соединение: OK")

    # ищем рабочий unit по регистру версии софта (#1520)
    units = [args.unit] if args.unit is not None else [255, 1, 0, 254]
    good = None
    for u in units:
        if read(client, 1520, 1, u) is not None:
            good = u
            print(f"unit {u}: ОТВЕЧАЕТ")
            break
        print(f"unit {u}: нет ответа")
    if good is None:
        print("\nПлата не ответила ни на один unit. TCP есть, но регистры не читаются — "
              "уточни unit у поставщика платы или попробуй --scan.")
        client.close()
        return

    print("\nПОЛНАЯ КАРТА РЕГИСТРОВ (raw-значения; для позиций x10 = мкм — проверить масштаб):")
    for addr, name, kind in REGISTER_MAP:
        if name is None:               # заголовок секции
            print(f"\n{addr}")
            continue
        regs = read(client, addr, 1, good)
        print(fmt(addr, name, kind, regs[0] if regs else None))

    if args.scan:
        lo, hi = args.scan
        print(f"\nСкан регистров {lo}..{hi} (ненулевые):")
        for a in range(lo, hi + 1):
            r = read(client, a, 1, good)
            if r is not None and r[0] != 0:
                print(f"  #{a}: {r[0]}")

    client.close()
    print("\nГотово. Это была ТОЛЬКО ПРОВЕРКА ЧТЕНИЯ — плата не тронута.")


if __name__ == "__main__":
    main()
