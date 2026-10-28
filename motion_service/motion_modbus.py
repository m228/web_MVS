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


class ModbusLink:
    def __init__(self, host: str, port: int = 502, device_id: int = 1, client_factory=None):
        self.host, self.port, self.device_id = host, int(port), int(device_id)
        self._factory = client_factory
        self._client = None
        self._kw: Optional[str] = None
        self._lock = threading.Lock()
        self._last_err_log = 0.0
        self.ok = False                 # последняя операция прошла
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
    def write(self, items) -> bool:
        """items = [(kind, address, value)], kind: holding | coil. True — все записи прошли."""
        if not items:
            return True
        with self._lock:
            try:
                if not self._ensure():
                    self._err("Modbus: нет связи с %s:%s" % (self.host, self.port))
                    return False
                kw = {self._unit_kw(): self.device_id}
                for kind, addr, val in items:
                    if kind == "coil":
                        res = self._client.write_coil(int(addr), bool(val), **kw)
                    else:
                        res = self._client.write_register(int(addr), int(val) & 0xFFFF, **kw)
                    if res.isError():
                        self._err("Modbus %s:%s: ошибка записи %s@%s: %s" % (self.host, self.port, kind, addr, res))
                        return False
                self.ok, self.last_error, self.last_ok_ts = True, "", time.time()
                return True
            except Exception as e:
                self._err("Modbus %s:%s: исключение: %s" % (self.host, self.port, e))
                self._drop()
                return False

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
