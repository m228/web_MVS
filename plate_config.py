"""Конфиг платы микроскопа `micro` (Modbus TCP slave) — адреса, масштабы, уставки.

Дефолты зашиты в коде (DEFAULTS), а поверх накладывается правимый JSON
`plate_config.json` в каталоге пользовательских данных (DATA_DIR), который создаётся
при первом запуске. Так боевые значения (реальные SP/SVSP, unit, Modbus-адрес СВ)
Макс правит в JSON, не трогая код.

Раскладка регистров — родная, из проекта M580 (см. Docs/microscope_plc_notes.md):
плата 192.168.1.153:502, WRITE holding 1250..1259 (OUT[0..9]), READ 1270..1279 (IN[0..9]).
"""
import json
import os
from copy import deepcopy

from logger import log_event
from paths import DATA_DIR

CONFIG_PATH = DATA_DIR / "plate_config.json"

# --- значения по умолчанию (ПЛЕЙСХОЛДЕРЫ для SP/SVSP — Макс подставит боевые) ---
DEFAULTS = {
    # связь с платой
    "host": "192.168.1.153",
    "port": 502,
    "unit": 254,               # unit_id из запроса ПЛК; проверить на железе (254/255/1)
    "poll_interval_ms": 100,   # такт опроса и автомата (из таймеров ST: 1 такт = 100 мс)
    "timeout_s": 1.0,

    # блоки обмена
    "write_base": 1250,        # OUT[0..9]
    "read_base": 1270,         # IN[0..9]
    "block_len": 10,

    # смещения выходов внутри WRITE-блока (индекс OUT[])
    "out": {
        "valves": 0,           # OUT[0], биты: .0 промывка трубки (CW.0), .1 промывка стекла (CW.1)
        "cmd1": 1,             # OUT[1] команда М1 (0x1006 = установить SP)
        "cmd2": 2,             # OUT[2] команда М2 (0x2006 = установить SP)
        "m1_sp": 3,            # OUT[3] = m1_SP/10 (ед. 10 мкм)
        "m2_sp": 4,            # OUT[4] = m2_SP/10
        "led_bright": 7,       # OUT[7]
        "led_on": 8,           # OUT[8], бит .0
    },

    # смещения входов внутри READ-блока (индекс IN[]) + масштаб в инженерную величину.
    # scale — множитель регистра, как в ПЛК (pos:=IN*10). Инж. значение = регистр * scale.
    "in": {
        "di":      {"off": 0, "scale": 1},
        "pos1_ai": {"off": 1, "scale": 10},
        "u12v":    {"off": 2, "scale": 1},
        "temp":    {"off": 3, "scale": 1},
        "pos1":    {"off": 4, "scale": 10},
        "pos2":    {"off": 5, "scale": 10},
    },

    # уставки автомата (для этапа B1). SP в мкм.
    # SP[0] — отвод от стекла (~40 мм); SP[1..49] — зазор по стадиям варки;
    # SP[50] — время промывки стекла (мс). Ниже — ПЛЕЙСХОЛДЕРЫ.
    "SP": [40000] + [2000] * 49 + [500],
    # SVSP[1..49] — пороги СВ (Brix) для выбора SP[i]; 0 = не задано.
    "SVSP": [0.0] * 51,

    # период цикла пробы по стадиям варки (сек); ключ — M[0,2].mode, "default" — по умолчанию
    "cycle_period_s": {"default": 120, "3": 120, "4": 90, "5": 90, "6": 90, "7": 60, "8": 60},

    # почасовая промывка от засахаривания (временный приём из ST; отключаемо)
    "hourly_wash": {"enabled": True, "minute": 3, "sec_on": 30, "sec_off": 59},

    # начальные значения для этапа B (СВ/стадия — ручные, пока нет источника из ПЛК)
    "manual_sv": 0.0,
    "manual_stage": 0,
    "led_bright": 0,

    # серийник камеры микроскопа (GigE) для картинки на странице; пусто -> плейсхолдер
    "camera_serial": "",

    # ЗАДЕЛ под этап A: источник СВ/стадии по Modbus (Макс пропишет адрес позже).
    # enabled=False -> СВ берётся из ручного ввода/дефолта, подвод по SP[1].
    "sv_source": {
        "enabled": False,
        "host": "",            # пусто -> тот же host, что и плата, либо адрес ПЛК
        "port": 502,
        "unit": 255,
        "sv_register": 0,      # holding-регистр СВ
        "sv_scale": 100,       # инж. СВ = регистр / sv_scale (напр. 100 -> Brix ×100)
        "stage_register": 0,   # holding-регистр стадии варки (M[0,2].mode)
    },
}


def _deep_merge(base, override):
    """Рекурсивно наложить override на base (не мутируя base). Списки заменяются целиком."""
    result = deepcopy(base)
    if not isinstance(override, dict):
        return result
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _write_default_file():
    """Создать plate_config.json с дефолтами при первом запуске (чтобы было что править)."""
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.parent / (CONFIG_PATH.name + ".tmp")
        tmp.write_text(json.dumps(DEFAULTS, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)
        log_event("plate_config", "Создан plate_config.json с настройками по умолчанию", "info",
                  {"path": str(CONFIG_PATH)})
    except Exception as e:
        log_event("plate_config", "Не удалось создать plate_config.json", "warn", {"error": str(e)})


def load():
    """Собранный конфиг: DEFAULTS + правки из plate_config.json (если есть)."""
    if not CONFIG_PATH.is_file():
        _write_default_file()
        return deepcopy(DEFAULTS)
    try:
        user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return _deep_merge(DEFAULTS, user)
    except Exception as e:
        log_event("plate_config", "Ошибка чтения plate_config.json — берём значения по умолчанию",
                  "warn", {"error": str(e)})
        return deepcopy(DEFAULTS)
