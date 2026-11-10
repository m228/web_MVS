"""Настройки источника данных аппарата (вкладка «ПЛК» на микроскопе и на /multi): Modbus или OPC UA — только чтение.

Здесь проверка того, что пришло со страницы, и то, что ей отдаём. Сам источник — sv_source.SvSource, клиент OPC UA —
opcua_client. Логин/пароль OPC UA уходят в data\\opcua_secret.json (opcua_client.save_secret) и обратно не отдаются.
"""
from __future__ import annotations

from copy import deepcopy

import plate_config
import opcua_client
import stage_map

FIELDS = tuple(plate_config.DEFAULTS["sv_source"]["fields"].keys())
NODE_MAX = 512
SEED_UNITS = {"kg": "кг", "l": "л", "pct": "%"}       # единица затравки: ключ в конфиге → подпись на экране
# виды затравки: поле ПЛК → (ключ единицы в sv_source, подпись). seed_qty — общая (первая версия, 2.1.51), остальные — по виду
SEED_FIELDS = {"seed_qty": ("seed_unit", "Затравка"),
               "seed_slurry": ("seed_slurry_unit", "Затравка сларри"),
               "seed_mu": ("seed_mu_unit", "Затравка МУ (маточник)")}


def seed_unit(cfg: dict | None = None, field: str = "seed_qty") -> str:
    """Единица затравки из конфига (kg | l | pct); мусор — kg. field — какое из полей SEED_FIELDS."""
    key = SEED_FIELDS.get(field, SEED_FIELDS["seed_qty"])[0]
    u = str(((cfg if cfg is not None else plate_config.load()).get("sv_source") or {}).get(key) or "kg")
    return u if u in SEED_UNITS else "kg"


def _int(v, name, lo, hi):
    try:
        x = int(v)
    except (TypeError, ValueError):
        raise ValueError("%s: нужно целое число" % name)
    if not lo <= x <= hi:
        raise ValueError("%s: от %d до %d" % (name, lo, hi))
    return x


def _scale(v, name):
    try:
        x = float(v if v not in (None, "") else 1)
    except (TypeError, ValueError):
        raise ValueError("%s: масштаб — число" % name)
    if x == 0 or x != x:
        raise ValueError("%s: масштаб не может быть 0" % name)
    return x


def normalize(body: dict) -> tuple[dict, dict | None]:
    """Тело запроса страницы → (патч sv_source для plate_config, правка логина/пароля или None). Ошибка → ValueError."""
    if not isinstance(body, dict):
        raise ValueError("ожидался JSON-объект")
    out: dict = {}
    if "enabled" in body:
        out["enabled"] = bool(body["enabled"])
    if "protocol" in body:
        proto = str(body["protocol"]).lower()
        if proto not in ("modbus", "opcua"):
            raise ValueError("протокол: modbus или opcua")
        out["protocol"] = proto
    if "period_s" in body:
        try:
            out["period_s"] = min(60.0, max(0.2, float(body["period_s"])))
        except (TypeError, ValueError):
            raise ValueError("период опроса — число секунд")
    for key, _ in SEED_FIELDS.values():
        if key in body:
            u = str(body[key] or "").lower()
            if u not in SEED_UNITS:
                raise ValueError("единица затравки: кг, л или %")
            out[key] = u
    if "stage_map" in body:
        out["stage_map"] = stage_map.normalize(body["stage_map"])
    mb = body.get("modbus")
    if isinstance(mb, dict):
        if "host" in mb:
            out["host"] = str(mb["host"] or "").strip()
        if "port" in mb:
            out["port"] = _int(mb["port"], "порт Modbus", 1, 65535)
        if "unit" in mb:
            out["unit"] = _int(mb["unit"], "unit Modbus", 0, 255)
        if isinstance(mb.get("fields"), dict):
            fields = {}
            for name, spec in mb["fields"].items():
                if name not in FIELDS or not isinstance(spec, dict):
                    continue
                fields[name] = {"reg": _int(spec.get("reg", 0) or 0, "%s: регистр" % name, 0, 65535),
                                "scale": _scale(spec.get("scale"), name), "signed": bool(spec.get("signed", False))}
            out["fields"] = fields
    secret = None
    op = body.get("opcua")
    if isinstance(op, dict):
        conn = opcua_client.normalize_conn(op)
        o = {k: conn[k] for k in ("endpoint", "security", "mode", "auth", "timeout_s")}
        if isinstance(op.get("fields"), dict):
            fields = {}
            for name, spec in op["fields"].items():
                if name not in FIELDS or not isinstance(spec, dict):
                    continue
                node = str(spec.get("node") or "").strip()
                if len(node) > NODE_MAX:
                    raise ValueError("%s: слишком длинный NodeId" % name)
                fields[name] = {"node": node, "scale": _scale(spec.get("scale"), name)}
            o["fields"] = fields
        out["opcua"] = o
        if "username" in op or op.get("password") or op.get("clear_password"):
            # пустой пароль со страницы = «оставить прежний» (страница его не знает и не показывает)
            secret = {"username": str(op.get("username") or "").strip() if "username" in op else None,
                      "password": "" if op.get("clear_password") else str(op["password"]) if op.get("password") else None}
    return out, secret


def public(cfg: dict | None = None) -> dict:
    """Настройки для страницы: протокол, Modbus, OPC UA (без пароля) + логин и «пароль задан»."""
    sv = deepcopy((cfg if cfg is not None else plate_config.load()).get("sv_source") or {})
    op = sv.get("opcua") or {}
    return {
        "enabled": bool(sv.get("enabled")),
        "protocol": sv.get("protocol") or "modbus",
        "period_s": sv.get("period_s", 1.0),
        "seed_unit": seed_unit({"sv_source": sv}), "seed_units": SEED_UNITS,
        "seed_fields": {f: {"unit_key": k, "title": t, "unit": seed_unit({"sv_source": sv}, f)} for f, (k, t) in SEED_FIELDS.items()},
        "stage_map": stage_map.public(sv),
        "modbus": {"host": sv.get("host", ""), "port": sv.get("port", 502), "unit": sv.get("unit", 255),
                   "fields": {k: {"reg": int((v or {}).get("reg", 0) or 0), "scale": (v or {}).get("scale", 1),
                                  "signed": bool((v or {}).get("signed", False))} for k, v in (sv.get("fields") or {}).items()}},
        "opcua": {"endpoint": op.get("endpoint", ""), "security": op.get("security", "None"),
                  "mode": op.get("mode", "SignAndEncrypt"), "auth": op.get("auth", "anonymous"),
                  "timeout_s": op.get("timeout_s", 3), "fields": deepcopy(op.get("fields") or {}),
                  **opcua_client.secret_public()},
        "policies": list(opcua_client.POLICIES),
        "modes": list(opcua_client.MODES),
        "fields": list(FIELDS),
    }


def conn_from_body(body: dict) -> tuple[dict, dict]:
    """Подключение для обзора/пробного чтения: адрес и защита со страницы (могут быть ещё не сохранены).
    Логин/пароль — со страницы. Пароль пустой — берём сохранённый в data\\opcua_secret.json, но ТОЛЬКО для сохранённого адреса
    сервера: иначе запросом с чужим адресом можно было бы увести сохранённый пароль на любой хост."""
    body = body or {}
    conn = opcua_client.normalize_conn(body)
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    if conn["auth"] == "user" and not password:
        saved = str(((plate_config.load().get("sv_source") or {}).get("opcua") or {}).get("endpoint") or "").strip()
        if not saved or saved != conn["endpoint"]:
            raise ValueError("для этого адреса впиши пароль (сохранённый пароль отдаём только сохранённому серверу)")
        stored = opcua_client.load_secret()
        username, password = username or stored["username"], stored["password"]
    return conn, {"username": username, "password": password}


def modbus_read_test(host: str, port: int, unit: int, regs: list) -> dict:
    """Пробное чтение регистров Modbus (кнопка «Проверить»): {reg: значение | None}. Только чтение (FC03)."""
    from pymodbus.client import ModbusTcpClient
    host = str(host or "").strip()
    if not host:
        raise ValueError("не задан IP контроллера")
    port, unit = _int(port, "порт", 1, 65535), _int(unit, "unit", 0, 255)
    regs = sorted({_int(r, "регистр", 0, 65535) for r in (regs or [])} - {0})
    if not regs:
        raise ValueError("нет ни одного регистра")
    if len(regs) > 32:
        raise ValueError("за раз не больше 32 регистров")
    c = ModbusTcpClient(host, port=port, timeout=2)
    try:
        if not c.connect():
            raise ValueError("нет связи с %s:%d" % (host, port))
        out = {}
        for r in regs:
            rr = c.read_holding_registers(r, count=1, slave=unit)
            if rr.isError():
                if not out:
                    raise ValueError("контроллер не отвечает на чтение (%s) — проверь unit и адрес" % (rr,))
                out[r] = None
            else:
                out[r] = rr.registers[0]
        return out
    finally:
        c.close()
