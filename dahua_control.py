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
            "optical_zoom": False, "error": None}
    if not host:
        caps["error"] = "no_host"
        return caps

    # 1) связь + модель
    try:
        info = _parse_kv(_cgi(host, user, password, "magicBox.cgi?action=getSystemInfo"))
        caps["reachable"] = True
        caps["model"] = info.get("deviceType", "")
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
