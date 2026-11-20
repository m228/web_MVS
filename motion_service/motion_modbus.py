"""modbus_out — запись результатов в контроллер по Modbus TCP.

Один ModbusLink на (host, port, device_id) — на нём могут сидеть несколько камер, обращения под замком
(клиент pymodbus не потокобезопасен). Ошибки связи НЕ валят детекцию: пишем в лог не чаще раза в 10 с и пробуем снова.

Совместимость pymodbus: в 3.6–3.9 адрес устройства называется slave=, с 3.10 — device_id=. Имя берём из сигнатуры
клиента, поэтому сервис работает и в venv со свежим pymodbus, и с тем, что закреплён в основном приложении.
"""
from __future__ import annotations

import inspect
import logging
import threading
import time
from typing import Optional

log = logging.getLogger("motion.modbus")

MAX_REGS = 100          # регистров в одном запросе (предел протокола 123, берём с запасом)


class ModbusLink:
    def __init__(self, host: str, port: int = 502, device_id: int = 1, client_factory=None):
        self.host, self.port, self.device_id = host, int(port), int(device_id)
        self._factory = client_factory
        self._client = None
        self._kw: Optional[str] = None
        self._lock = threading.Lock()
        self._last_err_log = 0.0
        self.ok = False                 # последняя операция прошла
        self.multi = True               # подряд идущие регистры — одним запросом (функция 16); False — контроллер её не принял
        self.requests = 0               # сколько Modbus-запросов записи ушло (для проверки и диагностики)
        self.last_error = ""
        self.last_ok_ts = 0.0

    # --- служебное ---
    def _make_client(self):
        if self._factory:
            return self._factory(self.host, self.port)
        from pymodbus.client import ModbusTcpClient
        return ModbusTcpClient(self.host, port=self.port, timeout=2)

    def _unit_kw(self) -> str:
        if self._kw is None:
            try:
                params = inspect.signature(self._client.write_register).parameters
                self._kw = "device_id" if "device_id" in params else "slave"
            except (TypeError, ValueError):
                self._kw = "device_id"
        return self._kw

    def _ensure(self) -> bool:
        if self._client is None:
            self._client = self._make_client()
            self._kw = None
        if not getattr(self._client, "connected", False):
            return bool(self._client.connect())
        return True

    def _err(self, msg: str):
        self.ok, self.last_error = False, msg
        now = time.time()
        if now - self._last_err_log > 10:
            self._last_err_log = now
            log.warning(msg)

    def _drop(self):
        try:
            if self._client:
                self._client.close()
        except Exception:
            pass
        self._client = None

    # --- операции ---
    @staticmethod
    def _runs(items) -> list:
        """items → операции записи в порядке списка: ("regs", адрес, [значения]) — подряд идущие holding-адреса (≥ 2, не больше MAX_REGS)
        одним запросом, ("reg", адрес, значение), ("coil", адрес, значение). Серия ставится на место первого своего регистра.
        Повтор адреса в одной пачке — побеждает последнее значение."""
        regs: dict = {}
        for kind, addr, val in items:
            if kind != "coil":
                regs[int(addr)] = int(val) & 0xFFFF
        run_of: dict = {}                      # адрес → (первый адрес серии, длина)
        run: list = []
        for a in sorted(regs) + [None]:
            if run and (a is None or a != run[-1] + 1 or len(run) >= MAX_REGS):
                for x in run:
                    run_of[x] = (run[0], len(run))
                run = []
            if a is not None:
                run.append(a)
        ops: list = []
        done: set = set()
        for kind, addr, val in items:
            if kind == "coil":
                ops.append(("coil", int(addr), bool(val)))
                continue
            first, n = run_of[int(addr)]
            if first in done:
                continue
            done.add(first)
            ops.append(("regs", first, [regs[first + i] for i in range(n)]) if n > 1 else ("reg", first, regs[first]))
        return ops

    def write(self, items) -> bool:
        """items = [(kind, address, value)], kind: holding | coil. True — все записи прошли.

        Подряд идущие holding-регистры уходят ОДНИМ запросом (функция 16, Write Multiple Registers): все значения ложатся в ПЛК разом,
        ПЛК не увидит «половину» набора. Контроллер не принял функцию 16 (ответил ошибкой Modbus) — те же регистры пишутся по одному
        (функция 06), а к функции 16 на этом канале больше не возвращаемся (self.multi = False, одна запись в лог)."""
        if not items:
            return True
        with self._lock:
            try:
                if not self._ensure():
                    self._err("Modbus: нет связи с %s:%s" % (self.host, self.port))
                    return False
                kw = {self._unit_kw(): self.device_id}
                for kind, addr, val in (self._runs(items) if self.multi else [(("coil" if k == "coil" else "reg"), int(a), (bool(v) if k == "coil" else int(v) & 0xFFFF)) for k, a, v in items]):
                    if kind == "regs":
                        res = self._client.write_registers(addr, val, **kw)
                        self.requests += 1
                        if res.isError():
                            if not self._single(addr, val, kw):
                                self._err("Modbus %s:%s: ошибка записи holding@%s..%s: %s" % (self.host, self.port, addr, addr + len(val) - 1, res))
                                return False
                            self.multi = False
                            log.warning("Modbus %s:%s: контроллер не принял запись нескольких регистров (функция 16): %s; пишем по одному", self.host, self.port, res)
                        continue
                    if kind == "coil":
                        res = self._client.write_coil(addr, bool(val), **kw)
                    else:
                        res = self._client.write_register(addr, int(val) & 0xFFFF, **kw)
                    self.requests += 1
                    if res.isError():
                        self._err("Modbus %s:%s: ошибка записи %s@%s: %s" % (self.host, self.port, "coil" if kind == "coil" else "holding", addr, res))
                        return False
                self.ok, self.last_error, self.last_ok_ts = True, "", time.time()
                return True
            except Exception as e:
                self._err("Modbus %s:%s: исключение: %s" % (self.host, self.port, e))
                self._drop()
                return False

    def _single(self, addr: int, vals: list, kw: dict) -> bool:
        """Запасной путь: регистры пачки по одному (функция 06). True — все прошли."""
        for i, v in enumerate(vals):
            res = self._client.write_register(addr + i, int(v) & 0xFFFF, **kw)
            self.requests += 1
            if res.isError():
                return False
        return True

    def read_holding(self, address: int) -> tuple[bool, str]:
        """Только чтение одного регистра — проверка связи без записи в ПЛК. (успех, значение или текст ошибки)."""
        with self._lock:
            try:
                if not self._ensure():
                    return False, "нет связи с %s:%s" % (self.host, self.port)
                res = self._client.read_holding_registers(int(address), count=1, **{self._unit_kw(): self.device_id})
                if res.isError():
                    return False, "ПЛК ответил ошибкой: %s" % (res,)
                return True, str(res.registers[0])
            except Exception as e:
                self._drop()
                return False, str(e) or e.__class__.__name__

    def close(self):
        with self._lock:
            self._drop()


class LinkRegistry:
    """Общие каналы на (host, port, device_id): две камеры в один ПЛК пишут через один клиент."""

    def __init__(self, client_factory=None):
        self._links: dict = {}
        self._lock = threading.Lock()
        self._factory = client_factory

    def get(self, host: str, port: int, device_id: int) -> ModbusLink:
        key = (host, int(port), int(device_id))
        with self._lock:
            if key not in self._links:
                self._links[key] = ModbusLink(host, port, device_id, self._factory)
            return self._links[key]

    def close_all(self):
        with self._lock:
            links, self._links = list(self._links.values()), {}
        for l in links:
            l.close()
