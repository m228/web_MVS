"""Управление IP-камерами Dahua по вендорскому HTTP CGI.

Только стандартная библиотека (`urllib`) + Digest-авторизация — новых зависимостей
не добавляем (см. CLAUDE.md: бандл раздаётся на «чистые» Windows). Учётные данные
берём из rtsp_url воркера, поэтому отдельно пароли не храним.

Проверено на живой камере DH-IPC-HFW2449TLP-S-LED-0280B-PRO
(прошивка 3.120, 2025-09-25):
- белый прожектор: table.Lighting_V2[0][профиль][0] с LightType=WhiteLight,
  Mode = Auto | Manual | Off; яркость MiddleLight[0].Light (1..100);
- оптического зума нет (фикс-объектив, ptz caps без Zoom);
- coaxialControlIO — Not Implemented (это для аналоговых HDCVI).
"""
import ipaddress
import urllib.error
import urllib.parse
import urllib.request

from logger import log_event

# сек на один запрос к камере (камера может тупить — но не даём висеть потоку)
CGI_TIMEOUT = 6

# профили сцен день/ночь/общий: режим света задаём сразу всем, чтобы сработало
# независимо от текущего día/noche-профиля камеры
WHITE_LIGHT_PROFILES = (0, 1, 2)
# индекс белого прожектора внутри профиля (0 = WhiteLight, 1 = AIMixLight)
WHITE_LIGHT_INDEX = 0


def parse_rtsp_credentials(rtsp_url):
    """rtsp://user:pass@host:port/... -> (host, user, password).

    host = None, если не удалось распарсить (тогда управление недоступно).
    """
    try:
        parsed = urllib.parse.urlparse(rtsp_url or "")
        host = parsed.hostname
        user = urllib.parse.unquote(parsed.username) if parsed.username else ""
        password = urllib.parse.unquote(parsed.password) if parsed.password else ""
        return host, user, password
    except Exception as e:
        log_event("dahua_control", "Не удалось разобрать rtsp_url", "warn", {"error": str(e)})
        return None, "", ""


def _cgi(host, user, password, query):
    """GET http://host/cgi-bin/<query> с Digest-авторизацией. Возвращает текст ответа.

    Бросает исключение при сетевой/авторизационной ошибке — вызывающий решает,
    что с этим делать (обычно логирует и возвращает {"error": ...}).
    """
    url = f"http://{host}/cgi-bin/{query}"
    pw_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    pw_mgr.add_password(None, f"http://{host}", user, password)
    opener = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(pw_mgr))
    with opener.open(url, timeout=CGI_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _parse_kv(text):
    """Ответ Dahua вида 'a=b\\nc=d' -> dict."""
    result = {}
    for line in text.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


def get_capabilities(host, user, password):
    """Опрос возможностей камеры Dahua.

    Возвращает dict: reachable, model, white_light, optical_zoom, error.
    Цифровой зум сюда не входит — он делается у нас и есть всегда.
    """
    caps = {"reachable": False, "model": "", "white_light": False,
            "optical_zoom": False, "image_settings": False, "error": None}
    if not host:
        caps["error"] = "no_host"
        return caps

    # 1) связь + модель
    try:
        info = _parse_kv(_cgi(host, user, password, "magicBox.cgi?action=getSystemInfo"))
        caps["reachable"] = True
        caps["model"] = info.get("deviceType", "")
        # настройки изображения (экспозиция/ББ/день-ночь) есть у любой Dahua, что отвечает по CGI
        caps["image_settings"] = True
    except Exception as e:
        caps["error"] = str(e)
        log_event("dahua_control", "Камера недоступна по CGI", "warn",
                  {"host": host, "error": str(e)})
        return caps

    # 2) белый прожектор: наличие WhiteLight в Lighting_V2
    try:
        light = _cgi(host, user, password,
                     "configManager.cgi?action=getConfig&name=Lighting_V2")
        caps["white_light"] = "WhiteLight" in light
    except Exception as e:
        log_event("dahua_control", "Не удалось прочитать конфиг света", "warn",
                  {"host": host, "error": str(e)})

    # 3) оптический зум: у PTZ-камер caps содержит поддержку Zoom;
    #    у фикс-объектива (наш случай) таких ключей нет
    try:
        ptz = _parse_kv(_cgi(host, user, password,
                             "ptz.cgi?action=getCurrentProtocolCaps&channel=0"))
        caps["optical_zoom"] = any(
            "Zoom" in key and value.strip().lower() == "true"
            for key, value in ptz.items()
        )
    except Exception:
        # нет ptz.cgi / не поддерживается — значит оптического зума нет
        pass

    return caps


def set_white_light(host, user, password, on, brightness=100):
    """Включить (Mode=Manual) или выключить (Mode=Off) белый прожектор.

    Режим применяется ко всем профилям сцены, чтобы сработало сразу.
    Возвращает True при ответе 'OK'.
    """
    mode = "Manual" if on else "Off"
    parts = []
    for profile in WHITE_LIGHT_PROFILES:
        base = f"Lighting_V2[0][{profile}][{WHITE_LIGHT_INDEX}]"
        parts.append(f"{base}.Mode={mode}")
        if on:
            level = max(1, min(100, int(brightness)))
            parts.append(f"{base}.MiddleLight[0].Light={level}")
    query = "configManager.cgi?action=setConfig&" + "&".join(parts)
    try:
        text = _cgi(host, user, password, query)
        return "OK" in text
    except Exception as e:
        log_event("dahua_control", "Ошибка управления белым светом", "error",
                  {"host": host, "on": bool(on), "error": str(e)})
        return False


def optical_zoom(host, user, password, direction):
    """Оптический (моторный) зум через ptz.cgi. direction: 'tele'|'wide'|'stop'.

    ВНИМАНИЕ: не протестировано на живой камере — под рукой только фикс-объектив.
    Используется документированный интерфейс Dahua PTZ (ZoomTele/ZoomWide).
    Показывается в UI только если get_capabilities вернул optical_zoom=True.
    """
    code = {"tele": "ZoomTele", "wide": "ZoomWide"}.get(direction)
    if direction == "stop":
        action, code = "stop", "ZoomTele"
    elif code:
        action = "start"
    else:
        return False
    query = (f"ptz.cgi?action={action}&channel=0&code={code}"
             f"&arg1=0&arg2=1&arg3=0")
    try:
        text = _cgi(host, user, password, query)
        return "OK" in text
    except Exception as e:
        log_event("dahua_control", "Ошибка оптического зума", "error",
                  {"host": host, "direction": direction, "error": str(e)})
        return False


def get_white_light(host, user, password):
    """Текущий режим белого прожектора: 'on' | 'off' | 'auto' | None."""
    try:
        kv = _parse_kv(_cgi(host, user, password,
                            "configManager.cgi?action=getConfig&name=Lighting_V2"))
    except Exception:
        return None
    mode = kv.get(f"table.Lighting_V2[0][0][{WHITE_LIGHT_INDEX}].Mode", "")
    return {"Manual": "on", "Off": "off", "Auto": "auto"}.get(mode, mode.lower() or None)


# ---------- настройки изображения (экспозиция / баланс белого / день-ночь) ----------
#
# Профили сцены Dahua — массив из 3 записей: [0]=Day, [1]=Night, [2]=Normal (Общий).
# Настройку пишем сразу во ВСЕ профили (как белый свет), чтобы изменение сработало
# при любом текущем профиле камеры и было видно немедленно. VideoInMode не трогаем.
# Имена полей и допустимые значения подтверждены на живой камере (DH-IPC-HFW2449TLP).
IMAGE_PROFILES = (0, 1, 2)

# пресеты баланса белого для UI (первые 7 из подтверждённых на камере)
WB_PRESETS = ("Auto", "Sunny", "Cloudy", "Home", "Office", "Night", "Outdoor")
# режимы день/ночь: эта прошивка принимает только эти два (Auto/Brightness → 400)
DAY_NIGHT_MODES = ("Color", "BlackWhite")


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _set_all_profiles(host, user, password, table, fields):
    """setConfig одного table (VideoInExposure/…) сразу для всех профилей (0,1,2).

    fields: {suffix: value}, напр. {"Compensation": 60}. Возвращает True при ответе 'OK'.
    """
    parts = []
    for profile in IMAGE_PROFILES:
        base = f"{table}[0][{profile}]"
        for suffix, value in fields.items():
            parts.append(f"{base}.{suffix}={urllib.parse.quote(str(value))}")
    query = "configManager.cgi?action=setConfig&" + "&".join(parts)
    text = _cgi(host, user, password, query)
    return "OK" in text


def get_image_settings(host, user, password):
    """Текущие настройки изображения (читаем из профиля [0]).

    Возвращает dict: reachable, exposure{compensation,gain_min,gain_max},
    white_balance{mode}, day_night{mode}, wb_presets[], day_night_modes[], error.
    """
    result = {
        "reachable": False,
        "exposure": {"compensation": None, "gain_min": None, "gain_max": None},
        "white_balance": {"mode": None},
        "day_night": {"mode": None},
        "wb_presets": list(WB_PRESETS),
        "day_night_modes": list(DAY_NIGHT_MODES),
        "error": None,
    }
    if not host:
        result["error"] = "no_host"
        return result
    try:
        exp = _parse_kv(_cgi(host, user, password,
                             "configManager.cgi?action=getConfig&name=VideoInExposure"))
        wb = _parse_kv(_cgi(host, user, password,
                            "configManager.cgi?action=getConfig&name=VideoInWhiteBalance"))
        dn = _parse_kv(_cgi(host, user, password,
                            "configManager.cgi?action=getConfig&name=VideoInDayNight"))
        result["reachable"] = True
        result["exposure"] = {
            "compensation": _to_int(exp.get("table.VideoInExposure[0][0].Compensation")),
            "gain_min": _to_int(exp.get("table.VideoInExposure[0][0].GainMin")),
            "gain_max": _to_int(exp.get("table.VideoInExposure[0][0].GainMax")),
        }
        result["white_balance"]["mode"] = wb.get("table.VideoInWhiteBalance[0][0].Mode")
        result["day_night"]["mode"] = dn.get("table.VideoInDayNight[0][0].Mode")
    except Exception as e:
        result["error"] = str(e)
        log_event("dahua_control", "Не удалось прочитать настройки изображения", "warn",
                  {"host": host, "error": str(e)})
    return result


def set_exposure(host, user, password, compensation=None, gain_min=None, gain_max=None):
    """Экспозиция (авто): компенсация 0..100 и пределы усиления GainMin/GainMax 0..100.

    Mode не трогаем — камера уже в авто (Mode=0), а эти три поля и есть авто-параметры.
    """
    if not host:
        return {"ok": False, "error": "no_host"}
    fields = {}
    if compensation is not None:
        fields["Compensation"] = max(0, min(100, int(compensation)))
    if gain_min is not None:
        fields["GainMin"] = max(0, min(100, int(gain_min)))
    if gain_max is not None:
        fields["GainMax"] = max(0, min(100, int(gain_max)))
    if not fields:
        return {"ok": False, "error": "no_fields"}
    try:
        ok = _set_all_profiles(host, user, password, "VideoInExposure", fields)
        return {"ok": ok}
    except Exception as e:
        log_event("dahua_control", "Ошибка настройки экспозиции", "error",
                  {"host": host, "error": str(e)})
        return {"ok": False, "error": str(e)}


def set_white_balance(host, user, password, mode):
    """Баланс белого: один из пресетов WB_PRESETS."""
    if not host:
        return {"ok": False, "error": "no_host"}
    if mode not in WB_PRESETS:
        return {"ok": False, "error": "bad_mode"}
    try:
        ok = _set_all_profiles(host, user, password, "VideoInWhiteBalance", {"Mode": mode})
        return {"ok": ok, "mode": mode}
    except Exception as e:
        log_event("dahua_control", "Ошибка настройки баланса белого", "error",
                  {"host": host, "mode": mode, "error": str(e)})
        return {"ok": False, "error": str(e)}


def set_day_night(host, user, password, mode):
    """Режим день/ночь: Color или BlackWhite."""
    if not host:
        return {"ok": False, "error": "no_host"}
    if mode not in DAY_NIGHT_MODES:
        return {"ok": False, "error": "bad_mode"}
    try:
        ok = _set_all_profiles(host, user, password, "VideoInDayNight", {"Mode": mode})
        return {"ok": ok, "mode": mode}
    except Exception as e:
        log_event("dahua_control", "Ошибка настройки день/ночь", "error",
                  {"host": host, "mode": mode, "error": str(e)})
        return {"ok": False, "error": str(e)}


# ---------- сетевые настройки (смена IP-адреса камеры) ----------
#
# Конфиг сети Dahua лежит в table.Network.* (getConfig&name=Network). Активный
# проводной интерфейс — table.Network.DefaultInterface (обычно eth0). Меняем IP/маску/
# шлюз/DHCP через setConfig на этом интерфейсе. Камера применяет смену сразу (без
# ребута) и переключает адрес — старое соединение после ответа 'OK' обрывается.


def get_network(host, user, password):
    """Текущие сетевые настройки камеры.

    Возвращает dict: reachable, interface, ip, mask, gateway, dhcp(bool), mac, error.
    """
    result = {"reachable": False, "interface": "eth0", "ip": "", "mask": "",
              "gateway": "", "dhcp": False, "mac": "", "error": None}
    if not host:
        result["error"] = "no_host"
        return result
    try:
        kv = _parse_kv(_cgi(host, user, password,
                            "configManager.cgi?action=getConfig&name=Network"))
    except Exception as e:
        result["error"] = str(e)
        log_event("dahua_control", "Не удалось прочитать сетевые настройки", "warn",
                  {"host": host, "error": str(e)})
        return result

    result["reachable"] = True
    iface = kv.get("table.Network.DefaultInterface", "").strip() or "eth0"
    base = f"table.Network.{iface}"
    result["interface"] = iface
    result["ip"] = kv.get(f"{base}.IPAddress", "")
    result["mask"] = kv.get(f"{base}.SubnetMask", "")
    result["gateway"] = kv.get(f"{base}.DefaultGateway", "")
    result["dhcp"] = kv.get(f"{base}.DhcpEnable", "").strip().lower() == "true"
    result["mac"] = kv.get(f"{base}.PhysicalAddress", "")
    return result


def set_network(host, user, password, ip=None, mask=None, gateway=None,
                dhcp=None, interface=None):
    """Сменить сетевые настройки камеры (IP/маска/шлюз или включить DHCP).

    dhcp=True   → включаем DHCP (IP/маску/шлюз не задаём — их выдаст сервер);
    dhcp=False  → статический адрес: обязательны корректные ip и mask, gateway опционален.

    IP/маску/шлюз валидируем через ipaddress (стдлиб). Возвращает {ok, error}.
    """
    if not host:
        return {"ok": False, "error": "no_host"}
    iface = (interface or "eth0").strip() or "eth0"
    parts = []

    if dhcp:
        parts.append(f"Network.{iface}.DhcpEnable=true")
    else:
        try:
            ipaddress.ip_address((ip or "").strip())
        except (ValueError, TypeError):
            return {"ok": False, "error": "bad_ip"}
        # проверяем IP и маску вместе: некорректная маска даст ValueError
        try:
            ipaddress.IPv4Network(f"{ip}/{mask}", strict=False)
        except (ValueError, TypeError):
            return {"ok": False, "error": "bad_mask"}

        parts.append(f"Network.{iface}.DhcpEnable=false")
        parts.append(f"Network.{iface}.IPAddress={ip.strip()}")
        parts.append(f"Network.{iface}.SubnetMask={mask.strip()}")

        gateway = (gateway or "").strip()
        if gateway:
            try:
                ipaddress.ip_address(gateway)
            except (ValueError, TypeError):
                return {"ok": False, "error": "bad_gateway"}
            parts.append(f"Network.{iface}.DefaultGateway={gateway}")

    query = "configManager.cgi?action=setConfig&" + "&".join(parts)
    try:
        text = _cgi(host, user, password, query)
        ok = "OK" in text
        return {"ok": ok, "error": None if ok else "camera_rejected"}
    except Exception as e:
        # смена адреса могла оборвать соединение уже ПОСЛЕ применения — честно
        # сообщаем об ошибке связи, пусть пользователь проверит переподключением
        log_event("dahua_control", "Ошибка смены сетевых настроек", "error",
                  {"host": host, "iface": iface, "dhcp": bool(dhcp), "error": str(e)})
        return {"ok": False, "error": str(e)}
