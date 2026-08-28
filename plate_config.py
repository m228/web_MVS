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
    # связь с платой (реальный IP платы «ПЧ Модуль», из конфигуратора: 192.168.1.128:502)
    "host": "192.168.1.128",
    "port": 502,
    "unit": 254,               # подтверждено пробником: плата отвечает на unit 254
    "poll_interval_ms": 100,   # такт опроса и автомата (из таймеров ST: 1 такт = 100 мс)
    "timeout_s": 1.0,

    # блоки обмена
    "write_base": 1250,        # OUT[0..9]
    "read_base": 1270,         # IN[0..9]
    "block_len": 10,

    # смещения выходов внутри WRITE-блока (индекс OUT[]). Пишутся по изменению (FC06).
    # ВНИМАНИЕ: команды моторам идут НЕ сюда, а в нативные регистры 1248/1249 (см. "motor"):
    # 1251/1252 из проекта ПЛК — НЕ командные (плата слушает 1248/1249, эталон — конфигуратор).
    "out": {
        "valves": 0,           # OUT[0]=1250, биты DQ: .0 промывка трубки, .1 промывка стекла, .2 воздух
        "m1_sp": 3,            # OUT[3]=1253 = m1_SP/10 (ед. 10 мкм)
        "m2_sp": 4,            # OUT[4]=1254 = m2_SP/10
        "led_bright": 7,       # OUT[7]=1257 яркость LED (%)
        "led_on": 8,           # OUT[8]=1258, бит .0 — LED вкл
    },

    # НАТИВНАЯ карта управления платой (из родного конфигуратора; Docs/microscope_board_config.md).
    # Команда мотору пишется в СВОЙ регистр (М1=1248, М2=1249) одиночной записью — так же, как это
    # делает конфигуратор (кнопка = записать код в командный регистр). Значения (позиция/шаги/сдвиг)
    # пишутся в свои регистры ДО команды.
    "motor": {
        "cmd_reg":  {"1": 1248, "2": 1249},   # командный регистр мотора
        "pos_set":  {"1": 1253, "2": 1254},   # «установить позицию»: пишем мкм/10
        "steps":    {"1": 1242, "2": 1243},   # «сделать шагов»: число шагов
        "shift":    {"1": 1246, "2": 1247},   # «сдвинуть на X мкм»: пишем мкм/10
        "dir":      {"1": 1240, "2": 1241},   # направление вперёд (бит .0)
        "enable":   {"1": 1219, "2": 1220},   # разрешение мотора (бит .0)
        "pos_read": {"1": 1274, "2": 1275},   # абс. позиция (raw*10 = мкм)
        "um_div": 10,                          # позиция/сдвиг: значение регистра = мкм / um_div
        # код команды = cmd_base[мотор] + cmd_op[операция] (М1=0x100X, М2=0x200X)
        "cmd_base": {"1": 0x1000, "2": 0x2000},
        "cmd_op": {
            "find_zero": 0x0, "steps": 0x1, "home_start": 0x3, "home_end": 0x4,
            "shift": 0x5, "goto": 0x6, "stop": 0x7, "set_zero": 0x8,
        },
    },

    # LED-фара: яркость (%), частота импульсов (Гц 1000..5000), бит включения
    "led": {"bright": 1257, "freq": 1202, "on_bit": 1258},

    # дискретные выходы DQ (клапаны и пр.), битовое слово в регистре 1250 (6 бит)
    "dq": {
        "reg": 1250,
        "labels": ["промывка трубки", "промывка стекла", "воздух охлаждения",
                   "выход 4", "выход 5", "выход 6"],
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

    # уставки автомата. SP в мкм. SP[0] — отвод от стекла (40 мм); SP[1..49] — зазор
    # (мин. расстояние до стекла) по порогам СВ; SP[50] — время промывки стекла (мс).
    # Боевые значения из оригинальной программы MicroScope (скриншот Макса 2026-08-25):
    #   СВ 75->100мкм, 81->600, 83->800, 85->900, 88->1000, 89->1100,
    #   90->1200, 91->1300, 92->1400, 95->1600.
    "SP": [40000, 100, 600, 800, 900, 1000, 1100, 1200, 1300, 1400, 1600] + [0] * 39 + [10000],
    # SVSP[i] — порог СВ (Brix) для выбора SP[i]; берётся последний i, где SV>=SVSP[i]
    # (массив по возрастанию!). 0 = не задано.
    "SVSP": [0.0, 75.0, 81.0, 83.0, 85.0, 88.0, 89.0, 90.0, 91.0, 92.0, 95.0] + [0.0] * 40,

    # доп. телеметрия с платы (родные регистры из конфигуратора «ПЧ Модуль») — для показа
    # на странице. ext_reads: блоки [base, count] читаем одним запросом;
    # ext_map: имя -> {reg(абс.адрес), scale, kind}. kind um/num -> raw*scale; bits/bool -> raw.
    # блоки читаем одним запросом FC03; ext_map раскладывает по имени.
    # kind: um/num -> raw*scale (число); bits/bool -> raw (как есть).
    # ВАЖНО: держим блоки УЗКИМИ по реально существующим регистрам. Один большой блок с «дырами»
    # (резервные адреса) реальная плата может отвергнуть целиком -> пропадёт вся телеметрия блока.
    # [1219,82]=1219..1300 — проверен на реальной плате (телеметрия читалась). _read_ext толерантен:
    # падение одного блока не рушит другие.
    "ext_reads": [[150, 1], [1202, 1], [1205, 14], [1219, 82], [1520, 14]],
    "ext_map": {
        # --- моторы: состояние/позиции ---
        "m1_enable":  {"reg": 1219, "scale": 1,  "kind": "bool"},
        "m2_enable":  {"reg": 1220, "scale": 1,  "kind": "bool"},
        "m1_dir":     {"reg": 1240, "scale": 1,  "kind": "bool"},
        "m2_dir":     {"reg": 1241, "scale": 1,  "kind": "bool"},
        "m1_pos":     {"reg": 1274, "scale": 10, "kind": "um"},
        "m2_pos":     {"reg": 1275, "scale": 10, "kind": "um"},
        "m1_steps":   {"reg": 1280, "scale": 1,  "kind": "num"},
        "m2_steps":   {"reg": 1281, "scale": 1,  "kind": "num"},
        "m1_enc":     {"reg": 1285, "scale": 10, "kind": "um"},
        "m1_slip":    {"reg": 1276, "scale": 10, "kind": "um"},
        "m2_state":   {"reg": 1300, "scale": 1,  "kind": "num"},
        "sensor":     {"reg": 1271, "scale": 10, "kind": "um"},   # датчик перемещения
        # --- выходы/LED ---
        "dq":         {"reg": 1250, "scale": 1,  "kind": "bits"},
        "di":         {"reg": 150,  "scale": 1,  "kind": "bits"}, # дискретные входы (нативный)
        "led_bright": {"reg": 1257, "scale": 1,  "kind": "num"},
        "led_freq":   {"reg": 1202, "scale": 1,  "kind": "num"},
        # --- питание/термо/вентиляторы ---
        "u12v":       {"reg": 1272, "scale": 1,  "kind": "num"},  # мВ
        "temp":       {"reg": 1273, "scale": 1,  "kind": "num"},  # °C
        "fan1":       {"reg": 1282, "scale": 1,  "kind": "num"},
        "fan2":       {"reg": 1283, "scale": 1,  "kind": "num"},
        # --- система ---
        "cycle_us":     {"reg": 1528, "scale": 1, "kind": "num"},
        "cycle_peak_us":{"reg": 1529, "scale": 1, "kind": "num"},
        "version":      {"reg": 1520, "scale": 1, "kind": "num"},
        "module_ver":   {"reg": 1521, "scale": 1, "kind": "num"},  # 19=hw1.3, 32=hw2.0
        "serial":       {"reg": 1531, "scale": 1, "kind": "num"},
        "uptime_s":     {"reg": 1523, "scale": 1, "kind": "num"},
        "init_status":  {"reg": 1532, "scale": 1, "kind": "bits"},
        # --- настройки моторов (только чтение; отдельная вкладка на странице) ---
        "m1_speed":     {"reg": 1205, "scale": 1, "kind": "num"},
        "m2_speed":     {"reg": 1207, "scale": 1, "kind": "num"},
        "m1_minspeed":  {"reg": 1206, "scale": 1, "kind": "num"},
        "m2_minspeed":  {"reg": 1208, "scale": 1, "kind": "num"},
        "m1_accel":     {"reg": 1209, "scale": 1, "kind": "num"},
        "m2_accel":     {"reg": 1210, "scale": 1, "kind": "num"},
        "m1_maxtravel": {"reg": 1231, "scale": 1, "kind": "num"},  # мм
        "m2_maxtravel": {"reg": 1233, "scale": 1, "kind": "num"},
        "m1_stop_sensor":{"reg": 1234,"scale": 1, "kind": "num"},  # мкм
        "m1_slip_limit":{"reg": 1236, "scale": 1, "kind": "num"},  # мкм
        "step_divider": {"reg": 1212, "scale": 1, "kind": "num"},  # 0..3,7
        "m1_steps_rev": {"reg": 1214, "scale": 1, "kind": "num"},
        "m2_steps_rev": {"reg": 1217, "scale": 1, "kind": "num"},
        "m1_dist_rev":  {"reg": 1215, "scale": 1, "kind": "num"},  # мкм
        "m2_dist_rev":  {"reg": 1218, "scale": 1, "kind": "num"},
        "m1_k_steps_mm":{"reg": 1213, "scale": 1, "kind": "num"},
        "m2_k_steps_mm":{"reg": 1216, "scale": 1, "kind": "num"},
        # --- охлаждение (только чтение; отдельная вкладка) ---
        "fan_on_temp":  {"reg": 1221, "scale": 1, "kind": "num"},
        "fan_off_temp": {"reg": 1222, "scale": 1, "kind": "num"},
        "air_on_temp":  {"reg": 1223, "scale": 1, "kind": "num"},
        "air_off_temp": {"reg": 1224, "scale": 1, "kind": "num"},
        "cam_on_temp":  {"reg": 1225, "scale": 1, "kind": "num"},
        "cam_off_temp": {"reg": 1226, "scale": 1, "kind": "num"},
    },

    # регистры «разрешение мотора» (вне OUT-блока) — для ручного движения и аварийного стопа
    "motor_enable": {"1": 1219, "2": 1220},

    # ЧАСТОТА ОТБОРА ПРОБЫ в циклическом режиме (как часто микроскоп сам делает снимок),
    # а НЕ длительность стадии! Сек, ключ — стадия M[0,2].mode, "default" — по умолчанию.
    # ПЛЕЙСХОЛДЕР: Макс уточнит реальный интервал снимка (стадии идут 20-30+ мин, отбирать
    # пробу можно, напр., раз в 5-10 мин). Можно задать разный интервал по стадиям.
    "cycle_period_s": {"default": 600},

    # почасовая промывка от засахаривания (временный приём из ST; отключаемо)
    "hourly_wash": {"enabled": True, "minute": 3, "sec_on": 30, "sec_off": 59},

    # начальные значения для этапа B (СВ/стадия — ручные, пока нет источника из ПЛК)
    "manual_sv": 0.0,
    "manual_stage": 0,
    "led_bright": 0,

    # камера микроскопа (GigE) для картинки на странице.
    # camera_serial — то, что нужно стриму; camera_ip — для показа/справки (пусто -> плейсхолдер)
    "camera_serial": "",
    "camera_ip": "",

    # ЗАДЕЛ под этап A: источник СВ/стадии по Modbus (Макс пропишет адрес позже).
    # enabled=False -> СВ берётся из ручного ввода/дефолта, подвод по SP[1].
    # ЗАДЕЛ под этап A: данные АППАРАТА с контроллера (ПЛК) по Modbus TCP. Отдельное устройство.
    # enabled=false -> не читаем, на странице «(—)». Макс впишет реальные адреса регистров.
    "sv_source": {
        "enabled": True,       # включено — читаем данные аппарата с ПЛК (Макс подтвердил 2026-08-28)
        "host": "10.20.2.180", # IP контроллера аппарата (M580), дал Макс
        "port": 502,
        "unit": 255,           # unit Modbus-сервера M580 (подтверждено Максом)
        "period_s": 1.0,
        # ПОЛЯ аппарата: name -> {reg, scale, signed?}. reg=0 -> поле не читается («(—)»).
        # инж.значение = регистр / scale. signed=true -> знаковый int16 (может быть отрицательным).
        #
        # Адреса = located-регистры %MW, куда ST-код ПЛК копирует данные варки (2026-08-28, Макс):
        # блок %MW650..%MW658. Масштабы согласованы с ПЛК: он домножает бриксометр ×100,
        # разрежение/давление ×10, остальное шлёт как есть (REAL_TO_INT -> целое).
        # ⚠️ Значения верны ТОЛЬКО если ST-код реально домножает (Brix×100, разреж/давл×10).
        "fields": {
            "sv":        {"reg": 650, "scale": 100},                  # Бриксометр (СВ/Brix)   AI[1][50], ПЛК ×100
            "temp_app":  {"reg": 651, "scale": 1},                    # температура в ВА, °C    AI[1][13]
            "level":     {"reg": 652, "scale": 1},                    # уровень ВА, %           AI[1][12]
            "current":   {"reg": 653, "scale": 1},                    # ток циркулятора, A      AI[1][180]
            "press_top": {"reg": 654, "scale": 10, "signed": True},   # разрежение СВЕРХУ       AI[1][192], ПЛК ×10
            "press_bot": {"reg": 655, "scale": 10, "signed": True},   # давление СНИЗУ (коллектор) AI[1][193], ПЛК ×10
            "vacuum":    {"reg": 0,   "scale": 1000, "signed": True}, # свободно (разрежение = press_top@654)
            "stage":     {"reg": 656, "scale": 1},                    # Mode — стадия варки     AO[11]
            "substage":  {"reg": 657, "scale": 1},                    # Submode — подстадия     AO[12]
            "cook_time": {"reg": 658, "scale": 1},                    # TcookingFull, сек       M[0][1].TCooking
        },
        # совместимость со старым конфигом (используется, если в fields нет sv/stage):
        "sv_register": 0,
        "sv_scale": 100,
        "stage_register": 0,
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
    """Создать ПУСТОЙ plate_config.json при первом запуске (хранит только правки пользователя).
    Раньше сюда писался полный слепок DEFAULTS — из-за этого правки DEFAULTS в коде «затирались»
    старым файлом (напр. sv_source не включался). Теперь база всегда из DEFAULTS, файл — оверрайды."""
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.parent / (CONFIG_PATH.name + ".tmp")
        tmp.write_text("{}", encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)
        log_event("plate_config", "Создан пустой plate_config.json (правки поверх DEFAULTS)", "info",
                  {"path": str(CONFIG_PATH)})
    except Exception as e:
        log_event("plate_config", "Не удалось создать plate_config.json", "warn", {"error": str(e)})


def _drop_stale_sv_source(user):
    """Само-исцеление: старый полный слепок DEFAULTS в файле мог перекрыть новые адреса ПЛК
    (все fields.reg=0, enabled=false). Если sv_source в файле — такой плейсхолдер (нет ни одного
    ненулевого reg), выкидываем его из оверрайдов, чтобы применился свежий sv_source из DEFAULTS."""
    if not isinstance(user, dict):
        return user
    sv = user.get("sv_source")
    if not isinstance(sv, dict):
        return user
    fields = sv.get("fields") or {}
    regs = [int((f or {}).get("reg", 0) or 0) for f in fields.values() if isinstance(f, dict)]
    if not any(regs) and int(sv.get("sv_register", 0) or 0) == 0:
        user = dict(user)
        user.pop("sv_source", None)
        log_event("plate_config", "Старый sv_source в plate_config.json проигнорирован — "
                  "берём адреса ПЛК из DEFAULTS", "info")
    return user


def load():
    """Собранный конфиг: DEFAULTS + правки из plate_config.json (если есть)."""
    if not CONFIG_PATH.is_file():
        _write_default_file()
        return deepcopy(DEFAULTS)
    try:
        user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return _deep_merge(DEFAULTS, _drop_stale_sv_source(user))
    except Exception as e:
        log_event("plate_config", "Ошибка чтения plate_config.json — берём значения по умолчанию",
                  "warn", {"error": str(e)})
        return deepcopy(DEFAULTS)


def save(patch):
    """Слить patch в plate_config.json (поверх существующих правок) и записать атомарно.
    Хранит только пользовательские правки; недостающее берётся из DEFAULTS в load().
    Возвращает собранный конфиг (load())."""
    try:
        current = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.is_file() else {}
        if not isinstance(current, dict):
            current = {}
    except Exception:
        current = {}
    merged = _deep_merge(current, patch or {})
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.parent / (CONFIG_PATH.name + ".tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)
        log_event("plate_config", "plate_config.json обновлён со страницы", "info",
                  {"keys": list((patch or {}).keys())})
    except Exception as e:
        log_event("plate_config", "Не удалось сохранить plate_config.json", "warn", {"error": str(e)})
    return load()
