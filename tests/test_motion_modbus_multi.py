"""Запись подряд идущих holding-регистров одним Modbus-запросом (функция 16) и запасной путь по одному (функция 06)."""
import sys
from pathlib import Path

MS = Path(__file__).resolve().parent.parent / "motion_service"
sys.path.insert(0, str(MS))

import motion_modbus as mm  # noqa: E402


class Res:
    def __init__(self, err=False):
        self.err = err

    def isError(self):
        return self.err

    def __repr__(self):
        return "ExceptionResponse(illegal function)" if self.err else "ok"


class Client:
    connected = True

    def __init__(self, multi_ok=True, single_ok=True, raise_multi=False):
        self.log = []
        self.multi_ok, self.single_ok, self.raise_multi = multi_ok, single_ok, raise_multi

    def connect(self):
        return True

    def close(self):
        self.connected = False

    def write_register(self, address, value, device_id=1):
        self.log.append(("06", address, value))
        return Res(not self.single_ok)

    def write_registers(self, address, values, device_id=1):
        if self.raise_multi:
            raise OSError("обрыв")
        self.log.append(("16", address, list(values)))
        return Res(not self.multi_ok)

    def write_coil(self, address, value, device_id=1):
        self.log.append(("05", address, bool(value)))
        return Res()


def link(client):
    return mm.ModbusLink("plc", 502, 1, lambda h, p: client)


def test_seven_consecutive_registers_go_in_one_request():
    c = Client()
    l = link(c)
    items = [("holding", 100 + i, 10 * i) for i in range(7)]
    assert l.write(items) is True
    assert c.log == [("16", 100, [0, 10, 20, 30, 40, 50, 60])]
    assert l.requests == 1


def test_order_in_the_list_does_not_matter_and_negative_values_are_masked():
    c = Client()
    l = link(c)
    assert l.write([("holding", 6, -1), ("holding", 5, 65535), ("holding", 4, 7)])
    assert c.log == [("16", 4, [7, 65535, 65535])]


def test_gaps_split_into_runs_and_single_registers_use_function_6():
    c = Client()
    l = link(c)
    items = [("holding", 10, 1), ("holding", 11, 2), ("holding", 13, 3), ("holding", 14, 4), ("holding", 20, 5), ("coil", 3, 1)]
    assert l.write(items)
    assert c.log == [("16", 10, [1, 2]), ("16", 13, [3, 4]), ("06", 20, 5), ("05", 3, True)]      # в порядке списка
    assert l.requests == 4


def test_duplicate_address_keeps_the_last_value():
    c = Client()
    l = link(c)
    assert l.write([("holding", 7, 1), ("holding", 7, 2)])
    assert c.log == [("06", 7, 2)]


def test_long_run_is_split_by_protocol_limit():
    c = Client()
    l = link(c)
    assert l.write([("holding", i, i) for i in range(250)])
    assert [(x[0], x[1], len(x[2])) for x in c.log] == [("16", 0, 100), ("16", 100, 100), ("16", 200, 50)]


def test_controller_without_function_16_falls_back_once_and_stays_on_single_writes():
    c = Client(multi_ok=False)
    l = link(c)
    items = [("holding", 100 + i, i) for i in range(3)]
    assert l.write(items) is True                                  # запись всё равно дошла
    assert c.log == [("16", 100, [0, 1, 2]), ("06", 100, 0), ("06", 101, 1), ("06", 102, 2)]
    assert l.multi is False
    c.log.clear()
    assert l.write(items) is True
    assert c.log == [("06", 100, 0), ("06", 101, 1), ("06", 102, 2)]     # функцию 16 больше не пробуем


def test_real_write_error_is_not_mistaken_for_missing_function_16():
    c = Client(multi_ok=False, single_ok=False)
    l = link(c)
    assert l.write([("holding", 1, 1), ("holding", 2, 2)]) is False
    assert l.multi is True and not l.ok and "ошибка записи" in l.last_error


def test_connection_error_in_multi_write_drops_the_link_and_keeps_function_16():
    c = Client(raise_multi=True)
    l = link(c)
    assert l.write([("holding", 1, 1), ("holding", 2, 2)]) is False
    assert l.multi is True and l._client is None
