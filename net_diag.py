"""Диагностика связи «завод → приёмник мониторинга»: почему пульс не доходит.

Запуск на ПК завода (вручную, работает и когда пульс вообще не ходит):
    web_MVS.exe --net-diag        (или NetDiag.bat; из исходников: python net_diag.py)
    --fix  — после проверки предложить исправления (каждое по подтверждению, нужны права администратора)

Ничего не меняет без вопроса «да/нет». Отчёт печатается в консоль и сохраняется в net_diag_<дата-время>.txt рядом с exe.

Устройство: collect() собирает ФАКТЫ (словарь), judge(факты) по таблице «симптом → причина → что делать» выдаёт ВЕРДИКТЫ,
apply_fix() выполняет исправление. Разбор вывода команд и judge() — чистые функции, проверяются тестами без сети.
Только стандартная библиотека и системные ping / netsh / sc / zerotier-cli, поэтому внутри exe Python на машине не нужен.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

try:
    from paths import DATA_DIR
except Exception:                                    # запуск «голого» файла рядом с чем угодно
    DATA_DIR = Path(__file__).resolve().parent

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
DEFAULT_SERVERS = ("http://172.29.134.237:8769", "http://11.11.11.12:8769", "http://192.168.2.198:8769")
PING_MAX_PAYLOAD = 1472              # 1500 − 20 (IP) − 8 (ICMP)
PING_MIN_PAYLOAD = 400               # ниже искать нет смысла: сеть с таким путём не работает
POST_SIZES = (1024, 4096, 16384, 65536)
POST_TIMEOUT_S = 6
ZT_SERVICE = "ZeroTierOneService"
SKEW_WARN_S = 120
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------------------------------------------------- команды и разбор вывода

def _oem_encoding() -> str:
    try:
        import ctypes
        return "cp%d" % ctypes.windll.kernel32.GetOEMCP()
    except Exception:
        return "utf-8"


def run_cmd(args: list[str] | str, timeout: float = 15, shell: bool = False) -> tuple[int, str]:
    """(код, весь вывод одной строкой). Не нашли команду / завис — (-1, ''); ничего не падает."""
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout, shell=shell, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return -1, ""
    raw = (r.stdout or b"") + (r.stderr or b"")
    for enc in ("utf-8", _oem_encoding()):
        try:
            return r.returncode, raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return r.returncode, raw.decode("utf-8", "replace")


def ping_ok(output: str) -> bool:
    """Ответ пришёл: в любом языке Windows в успешной строке есть «TTL=…» (в «TTL expired» и «fragmented» его нет)."""
    return bool(re.search(r"TTL\s*=\s*\d+", output or "", re.I))


def parse_sc_state(output: str) -> Optional[str]:
    """sc query: «STATE : 4 RUNNING» (цифра не зависит от языка). 'running' / 'stopped' / 'other'; None — службы нет."""
    m = re.search(r"(?:STATE|СОСТОЯНИЕ|Состояние)[^\d\n]*:\s*(\d)", output or "") or re.search(r":\s*(\d)\s+[A-Z_]{5,}", output or "")
    if not m:
        return None
    return {"4": "running", "1": "stopped"}.get(m.group(1), "other")


def parse_subinterfaces(output: str) -> list[dict]:
    """netsh interface ipv4 show subinterfaces: строки «MTU состояние принято отправлено имя» (заголовок локализован, строки — нет)."""
    rows = []
    for line in (output or "").splitlines():
        m = re.match(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\S.*?)\s*$", line)
        if m:
            rows.append({"mtu": int(m.group(1)), "name": m.group(5)})
    return rows


def parse_peers(output: str) -> dict:
    """zerotier-cli peers: сколько узлов на прямой связи и сколько через ретрансляцию (RELAY) / туннель; задержка до планеты."""
    direct = relay = 0
    planet_lat = None
    for line in (output or "").splitlines():
        p = line.split()
        if len(p) < 6 or not re.fullmatch(r"[0-9a-f]{10}", p[0]):
            continue
        role, lat, link = p[2], p[3], p[4]
        if role == "PLANET":
            try:
                planet_lat = int(lat)
            except ValueError:
                pass
        elif role == "LEAF":
            if link == "DIRECT":
                direct += 1
            elif link in ("RELAY", "TUNNELED"):
                relay += 1
    return {"direct": direct, "relay": relay, "planet_latency": planet_lat if planet_lat and planet_lat > 0 else None}


def zt_adapter_for(net_id: str, subifs: list[dict]) -> Optional[dict]:
    """Строка netsh про адаптер этой сети ZeroTier: имя вида «ZeroTier One [633e31d8a232164e]»."""
    for r in subifs:
        if net_id and net_id.lower() in r["name"].lower():
            return r
    return None


def system_proxy() -> str:
    """Адрес системного прокси (то, что подхватит urllib при system_proxy=True) или ''."""
    try:
        px = urllib.request.getproxies()
    except Exception:
        return ""
    return px.get("http") or px.get("https") or ""


def find_zt_cli() -> Optional[str]:
    for c in (shutil.which("zerotier-cli"), shutil.which("zerotier-cli.bat"),
              r"C:\Program Files (x86)\ZeroTier\One\zerotier-cli.bat", r"C:\Program Files\ZeroTier\One\zerotier-cli.bat"):
        if c and os.path.exists(c):
            return c
    return None


def is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ---------------------------------------------------------------------------------------------------------------- сбор фактов

class Report:
    """Печатает по мере работы (видно, что скрипт не завис) и копит текст для файла."""

    COLORS = {OK: "32", WARN: "33", FAIL: "31", INFO: "36"}
    MARKS = {OK: "[ OK ]", WARN: "[ !! ]", FAIL: "[FAIL]", INFO: "[ .. ]"}

    def __init__(self, out: Callable[[str], None] = print, color: Optional[bool] = None):
        self.out, self.lines = out, []
        self.color = sys.stdout.isatty() if color is None else color
        if self.color and os.name == "nt":
            os.system("")                                  # включает разбор ANSI-цветов в консоли Windows 10

    def raw(self, text: str = "") -> None:
        self.lines.append(text)
        self.out(text)

    def head(self, text: str) -> None:
        self.raw("")
        self.raw("== " + text + " ==")

    def step(self, level: str, text: str) -> None:
        mark = self.MARKS[level]
        self.lines.append("%s %s" % (mark, text))
        shown = "\x1b[%sm%s\x1b[0m" % (self.COLORS[level], mark) if self.color else mark
        self.out("%s %s" % (shown, text))


def _http(url: str, method: str = "GET", body: Optional[bytes] = None, headers: Optional[dict] = None, via_proxy: bool = False,
          timeout: float = 6) -> dict:
    """Один запрос. {ok, code, err, ms, data, date}. Любой ответ сервера (даже 4xx) — «дошло»: code есть, err пусто."""
    req = urllib.request.Request(url, data=body, method=method, headers=dict(headers or {}))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(None if via_proxy else {}))
    t0 = time.time()
    res = {"ok": False, "code": None, "err": "", "ms": None, "data": b"", "date": None}
    try:
        with opener.open(req, timeout=timeout) as r:
            res.update(code=r.status, data=r.read(65536), date=r.headers.get("Date"))
    except urllib.error.HTTPError as e:
        res.update(code=e.code, date=e.headers.get("Date") if e.headers else None)
        try:
            res["data"] = e.read(65536)
        except Exception:
            pass
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, http.client.HTTPException, OSError) as e:
        reason = getattr(e, "reason", e)
        res["err"] = "таймаут" if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason) else str(reason)
    except (UnicodeError, ValueError) as e:                  # например, в токене из monitor.json русские буквы — в заголовок они не идут
        res["err"] = "некорректный запрос: %s" % e
    res["ms"] = int((time.time() - t0) * 1000)
    res["ok"] = res["code"] is not None
    return res


def tcp_probe(host: str, port: int, timeout: float = 3) -> dict:
    t0 = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"ok": True, "ms": int((time.time() - t0) * 1000), "err": ""}
    except socket.timeout:
        return {"ok": False, "ms": None, "err": "таймаут"}
    except OSError as e:
        return {"ok": False, "ms": None, "err": e.strerror or str(e)}


def local_ip_for(host: str) -> str:
    """Какой свой адрес выберет Windows, чтобы идти к host (по нему видно, идёт ли трафик через ZeroTier)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 9))
            return s.getsockname()[0]
    except OSError:
        return ""


def ping_host(host: str, size: Optional[int] = None, df: bool = False, tries: int = 1, wait_ms: int = 1500) -> bool:
    args = ["ping", "-n", "1", "-w", str(wait_ms)] + (["-f"] if df else []) + (["-l", str(size)] if size else []) + [host]
    for _ in range(tries):
        code, out = run_cmd(args, timeout=wait_ms / 1000 + 4)
        if ping_ok(out):
            return True
    return False


def find_max_payload(host: str, ping: Callable[..., bool] = ping_host) -> Optional[int]:
    """Самый большой пакет без фрагментации, что проходит до host (бинарный поиск). None — не проходит даже минимальный."""
    if not ping(host, PING_MIN_PAYLOAD, df=True, tries=2):
        return None
    if ping(host, PING_MAX_PAYLOAD, df=True, tries=2):
        return PING_MAX_PAYLOAD
    lo, hi = PING_MIN_PAYLOAD, PING_MAX_PAYLOAD                  # lo проходит, hi — нет
    while hi - lo > 8:
        mid = (lo + hi) // 2
        if ping(host, mid, df=True, tries=2):
            lo = mid
        else:
            hi = mid
    return lo


def collect_config() -> dict:
    """Настройки мониторинга этого ПК (monitor.json). Нет файла или sysmon не грузится — читаем файл сами."""
    try:
        import sysmon
        c = sysmon.load_config()
        return {"url": c.get("url") or "", "token": c.get("token") or "", "enabled": bool(c.get("enabled")), "system_proxy": bool(c.get("system_proxy", True))}
    except Exception:
        try:
            c = json.loads((DATA_DIR / "monitor.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            c = {}
        return {"url": str(c.get("url") or ""), "token": str(c.get("token") or ""), "enabled": bool(c.get("enabled")), "system_proxy": bool(c.get("system_proxy", True))}


def collect_zerotier(rep: Report, facts: dict) -> None:
    rep.head("ZeroTier")
    zt = facts["zt"] = {"service": None, "cli": None, "online": None, "networks": [], "peers": None, "adapters": []}
    code, out = run_cmd(["sc", "query", ZT_SERVICE])
    zt["service"] = parse_sc_state(out) if code == 0 else None
    if zt["service"] is None:
        rep.step(WARN, "служба %s не найдена (ZeroTier не установлен?)" % ZT_SERVICE)
        return
    rep.step(OK if zt["service"] == "running" else FAIL, "служба %s: %s" % (ZT_SERVICE, {"running": "запущена", "stopped": "остановлена"}.get(zt["service"], "в переходном состоянии")))
    if zt["service"] != "running":
        return
    cli = zt["cli"] = find_zt_cli()
    if not cli:
        rep.step(WARN, "zerotier-cli не найден: сети и узлы посмотреть не могу")
    else:
        _, out = run_cmd([cli, "info"], shell=True)
        zt["online"] = "ONLINE" in out
        rep.step(OK if zt["online"] else FAIL, "ZeroTier %s" % ("в сети (достаёт до планеты)" if zt["online"] else "OFFLINE: не достаёт до планет (нет интернета/UDP заблокирован?)"))
        _, out = run_cmd([cli, "-j", "listnetworks"], shell=True)
        try:
            nets = json.loads(out[out.index("["):]) if "[" in out else []
        except ValueError:
            nets = []
        _, sub = run_cmd(["netsh", "interface", "ipv4", "show", "subinterfaces"])
        subifs = parse_subinterfaces(sub)
        for n in nets:
            ad = zt_adapter_for(n.get("id", ""), subifs)
            net = {"id": n.get("id"), "name": n.get("name"), "status": n.get("status"), "ips": [a.split("/")[0] for a in n.get("assignedAddresses", [])],
                   "adapter": ad["name"] if ad else None, "mtu": ad["mtu"] if ad else None}
            zt["networks"].append(net)
            ok = net["status"] == "OK" and net["ips"]
            rep.step(OK if ok else FAIL, "сеть «%s» (%s): %s, адрес %s, MTU адаптера %s" % (net["name"] or "?", net["id"], net["status"], ", ".join(net["ips"]) or "нет", net["mtu"] or "?"))
        if not nets:
            rep.step(FAIL, "ни одной сети ZeroTier (узел не подключён ни к одной)")
        _, out = run_cmd([cli, "peers"], shell=True)
        zt["peers"] = parse_peers(out)
        p = zt["peers"]
        rep.step(INFO, "узлов на прямой связи: %d, через ретрансляцию: %d%s" % (p["direct"], p["relay"], ", задержка до планеты %d мс" % p["planet_latency"] if p["planet_latency"] else ""))


def collect_local(rep: Report, facts: dict, cfg: dict) -> None:
    rep.head("Этот ПК")
    facts["admin"] = is_admin()
    rep.step(INFO, "права администратора: %s" % ("да" if facts["admin"] else "нет (исправления сети недоступны)"))
    facts["app_running"] = tcp_probe("127.0.0.1", 8000, 1)["ok"]
    rep.step(OK if facts["app_running"] else WARN, "приложение web_MVS (порт 8000): %s" % ("работает" if facts["app_running"] else "не отвечает (закрыто?)"))
    facts["cfg"] = {"url": cfg["url"], "token_set": bool(cfg["token"]), "enabled": cfg["enabled"], "system_proxy": cfg["system_proxy"]}
    if not cfg["url"]:
        rep.step(WARN, "адрес приёмника в monitor.json не задан: проверю адреса по умолчанию")
    else:
        rep.step(OK if cfg["enabled"] and cfg["token"] else WARN, "мониторинг: %s, токен %s, адрес %s" % ("включён" if cfg["enabled"] else "ВЫКЛЮЧЕН", "есть" if cfg["token"] else "НЕТ", cfg["url"]))
    facts["proxy"] = system_proxy()
    if facts["proxy"]:
        rep.step(WARN if cfg["system_proxy"] else INFO, "в Windows задан прокси %s; приложение %s" % (facts["proxy"], "его использует" if cfg["system_proxy"] else "его не использует"))
    else:
        rep.step(OK, "системного прокси нет")


def probe_target(rep: Report, url: str, via_proxy: bool) -> dict:
    """Один адрес приёмника: ping, TCP, GET /health (и через системный прокси, если он есть), по Date — расхождение часов."""
    u = urlparse(url)
    host, port = u.hostname or "", u.port or (443 if u.scheme == "https" else 80)
    t = {"url": url, "host": host, "port": port, "ping": None, "tcp": None, "get": None, "get_proxy": None, "src_ip": local_ip_for(host), "skew": None}
    t["ping"] = ping_host(host, tries=2)
    rep.step(OK if t["ping"] else WARN, "ping %s: %s" % (host, "отвечает" if t["ping"] else "нет ответа (ICMP мог быть закрыт)"))
    tcp = t["tcp"] = tcp_probe(host, port)
    rep.step(OK if tcp["ok"] else FAIL, "подключение к %s:%d: %s" % (host, port, ("%d мс" % tcp["ms"]) if tcp["ok"] else tcp["err"]))
    if not tcp["ok"]:
        return t
    g = t["get"] = _http(url + "/health", via_proxy=False)
    if g["ok"] and g["code"] == 200:
        try:
            ver = json.loads(g["data"].decode("utf-8")).get("version")
        except ValueError:
            ver = None
        g["version"] = ver
        rep.step(OK, "GET /health: 200, приёмник v%s, %d мс" % (ver or "?", g["ms"]))
        if g["date"]:
            try:
                t["skew"] = round(time.time() - parsedate_to_datetime(g["date"]).timestamp())
            except (TypeError, ValueError):
                pass
    else:
        rep.step(FAIL, "GET /health: %s" % (g["err"] or "HTTP %s" % g["code"]))
    if via_proxy:
        gp = t["get_proxy"] = _http(url + "/health", via_proxy=True)
        rep.step(OK if gp["ok"] and gp["code"] == 200 else FAIL, "GET /health через системный прокси: %s" % (("200, %d мс" % gp["ms"]) if gp["ok"] and gp["code"] == 200 else (gp["err"] or "HTTP %s" % gp["code"])))
    return t


def check_token(rep: Report, url: str, token: str, via_proxy: bool) -> Optional[int]:
    """Токен принят? Пустой пульс {}: приёмник отвечает 400 («формат»), если токен верный, и 401, если нет. Данных в базу не попадает."""
    r = _http(url + "/ingest", "POST", b"{}", {"Content-Type": "application/json", "Authorization": "Bearer " + token}, via_proxy=via_proxy)
    if r["code"] == 401:
        rep.step(FAIL, "токен: приёмник его не принял (HTTP 401)")
    elif r["code"] in (200, 400):
        rep.step(OK, "токен: принят")
    else:
        rep.step(WARN, "токен: непонятный ответ (%s)" % (r["err"] or "HTTP %s" % r["code"]))
    return r["code"]


def check_sizes(rep: Report, target: dict, via_proxy: bool) -> dict:
    """Большие запросы: ping без фрагментации + POST /netcheck растущих размеров (приёмник читает тело целиком и возвращает счёт)."""
    host, url = target["host"], target["url"]
    res = {"small_ping": target.get("ping"), "max_payload": None, "ping_tested": False, "posts": []}
    if target.get("ping"):
        res["ping_tested"] = True
        res["max_payload"] = find_max_payload(host)
        mp = res["max_payload"]
        if mp is None:
            rep.step(FAIL, "ping без фрагментации не проходит даже при %d байтах" % PING_MIN_PAYLOAD)
        elif mp >= PING_MAX_PAYLOAD:
            rep.step(OK, "ping без фрагментации проходит до %d байт (полный размер)" % mp)
        else:
            rep.step(WARN, "ping без фрагментации проходит только до %d байт (полный размер = %d)" % (mp, PING_MAX_PAYLOAD))
    probe = _http(url + "/netcheck", via_proxy=via_proxy)         # GET: у нового приёмника маршрут есть (405 «только POST»), у старого — 404; так без гонок с обрывом соединения
    if probe["code"] == 404:
        res["posts"].append({"size": POST_SIZES[0], "state": "unsupported", "ms": probe["ms"], "err": ""})
        rep.step(WARN, "POST больших данных не проверить: приёмник без /netcheck (обнови приёмник до 2.1.41+)")
        return res
    for size in POST_SIZES:
        r = _http(url + "/netcheck", "POST", b"x" * size, {"Content-Type": "application/octet-stream"}, via_proxy=via_proxy, timeout=POST_TIMEOUT_S)
        try:
            got = json.loads(r["data"].decode("utf-8")).get("got") if r["data"] else None
        except ValueError:
            got = None
        if r["code"] == 200 and got == size:
            state = "ok"
        elif r["code"] in (404, 405):
            state = "unsupported"
        elif r["code"] is None:
            state = "timeout" if r["err"] == "таймаут" else "error"
        else:
            state = "error"
        res["posts"].append({"size": size, "state": state, "ms": r["ms"], "err": r["err"]})
        if state == "unsupported":
            rep.step(WARN, "POST больших данных не проверить: приёмник без /netcheck (обнови приёмник до 2.1.41+)")
            break
        rep.step(OK if state == "ok" else FAIL, "POST %d КБ: %s" % (size // 1024, ("дошёл за %d мс" % r["ms"]) if state == "ok" else (r["err"] or "ответ HTTP %s" % r["code"])))
        if state != "ok":
            break                                            # дальше — те же и бóльшие: только ждать зря
    return res


def collect(rep: Report, cfg: Optional[dict] = None) -> dict:
    cfg = cfg or collect_config()
    facts: dict = {}
    collect_local(rep, facts, cfg)
    collect_zerotier(rep, facts)
    rep.head("Связь с приёмником")
    urls = [cfg["url"]] if cfg["url"] else []
    urls += [u for u in DEFAULT_SERVERS if u not in urls]
    via_proxy = bool(facts["proxy"]) and cfg["system_proxy"]
    facts["targets"] = []
    for i, url in enumerate(urls):
        rep.raw("-> %s%s" % (url, "  (адрес из настроек)" if i == 0 and cfg["url"] else ""))
        t = probe_target(rep, url, via_proxy)
        facts["targets"].append(t)
        if t["tcp"] and t["tcp"]["ok"]:
            break                                            # нашли рабочий адрес — остальные не мучаем
    facts["primary"] = facts["targets"][0]            # главный — адрес из настроек (или первый по умолчанию): по нему и вердикт
    reach = [t for t in facts["targets"] if t["tcp"] and t["tcp"]["ok"] and t["get"] and t["get"]["ok"]]
    facts["reachable"] = reach[0] if reach else None
    if reach:
        t = reach[0]
        if cfg["token"] and t["url"] == cfg["url"]:
            facts["token_code"] = check_token(rep, t["url"], cfg["token"], via_proxy)
        rep.head("Большие пакеты (MTU)")
        facts["sizes"] = check_sizes(rep, t, via_proxy)
    return facts


# ------------------------------------------------------------------------------------------------------------------- вердикты

def recommended_mtu(max_payload: int) -> int:
    """MTU адаптера ZeroTier по найденному максимуму: самый большой целый пакет, что проходит (payload + 28), с небольшим запасом."""
    return max(576, min(1400, ((max_payload + 28 - 16) // 4) * 4))


def judge(f: dict) -> list[dict]:
    """Факты → вердикты [{level, title, advice, fix?}], главные первыми. Чистая функция."""
    v: list[dict] = []
    add = lambda level, title, advice, fix=None: v.append({"level": level, "title": title, "advice": advice, "fix": fix})
    zt, cfg = f.get("zt") or {}, f.get("cfg") or {}
    nets = zt.get("networks") or []
    reach = f.get("reachable")
    prim = f.get("primary") or {}
    wants_zt = (prim.get("host") or "").startswith(("172.29.", "11.11.11."))

    if cfg and (not cfg.get("enabled") or not cfg.get("token_set")):
        add(WARN, "Мониторинг на этом ПК не настроен/выключен",
            "Открой в приложении страницу «Мониторинг»: включи отправку, адрес приёмника и токен (или дождись авто-регистрации и нажми «Принять» в панели).")
    if f.get("app_running") is False:
        add(WARN, "Приложение web_MVS не запущено", "Пульс шлёт само приложение. Запусти его (run.bat): без него «нет пульса» — это норма.")

    if wants_zt and zt.get("service") is None:
        add(FAIL, "ZeroTier не установлен", "Адрес приёмника лежит в сети ZeroTier, а клиента ZeroTier на ПК нет. Поставь ZeroTier One и подключись к сети.")
    elif zt.get("service") in ("stopped", "other"):
        add(FAIL, "Служба ZeroTier не работает", "Запусти службу ZeroTierOneService (services.msc) или перезагрузи ПК.", {"kind": "start_zt"})
    elif zt.get("service") == "running":
        if zt.get("online") is False:
            add(FAIL, "ZeroTier не выходит в интернет (OFFLINE)", "Узел не достаёт до планет: проверь интернет на ПК и что исходящий UDP (порт 9993) не заблокирован роутером/провайдером.", {"kind": "restart_zt"})
        if zt.get("cli") and not nets:
            add(FAIL, "ZeroTier ни к одной сети не подключён", "Подключи узел к нужной сети (zerotier-cli join <id сети> или значок в трее).")
        for n in nets:
            if n["status"] == "ACCESS_DENIED":
                add(FAIL, "Сеть ZeroTier «%s»: узел не одобрен" % n["name"], "В ZeroTier Central поставь галочку Auth у этого узла.")
            elif n["status"] == "REQUESTING_CONFIGURATION":
                add(FAIL, "Сеть ZeroTier «%s» не отвечает (REQUESTING_CONFIGURATION)" % n["name"], "Нет связи с контроллером сети: проверь интернет/UDP, перезапусти службу ZeroTier.", {"kind": "restart_zt"})
            elif n["status"] != "OK":
                add(FAIL, "Сеть ZeroTier «%s»: статус %s" % (n["name"], n["status"]), "Проверь сеть в ZeroTier Central.")
            elif not n["ips"]:
                add(WARN, "В сети ZeroTier «%s» узлу не выдан адрес" % n["name"], "В ZeroTier Central включи авто-выдачу адресов или назначь адрес узлу.")

    tcp = prim.get("tcp") or {}
    zt_ips_all = {ip for n in nets for ip in n["ips"]}
    chosen = reach or prim
    if wants_zt and zt_ips_all and tcp.get("ok") and chosen.get("src_ip") and chosen["src_ip"] not in zt_ips_all:
        add(WARN, "Трафик к приёмнику идёт НЕ через ZeroTier (свой адрес %s)" % chosen["src_ip"],
            "Адрес приёмника из сети ZeroTier, но Windows отправляет его другим путём: скорее всего VPN/TUN-адаптер или другой маршрут перехватывает трафик. Отключи VPN или поправь маршрут.")
    if not reach and prim:
        ping, got_tcp = prim.get("ping"), tcp.get("ok")
        src = prim.get("src_ip", "")
        zt_ips = {ip for n in nets for ip in n["ips"]}
        if not ping and not got_tcp:
            via = "через ZeroTier" if src in zt_ips else "НЕ через ZeroTier (адрес %s)" % (src or "?")
            add(FAIL, "До приёмника нет пути (ни ping, ни порт), трафик идёт %s" % via,
                "Возможно: приёмник выключен или спит; этот узел и приёмник в разных сетях ZeroTier; на узле приёмника нет одобрения. "
                "Проверь, что ПК с приёмником включён и в той же сети ZeroTier, и открой http://<адрес>:8769/health на ПК Макса." if src in zt_ips else
                "Windows отправляет трафик на адрес приёмника мимо ZeroTier: проверь маршрут/второй адаптер или подключение к сети ZeroTier.")
        elif ping and not got_tcp:
            add(FAIL, "Приёмник виден (ping идёт), но порт %s закрыт: %s" % (prim.get("port"), tcp.get("err")),
                "Приёмник не запущен или брандмауэр на ПК Макса режет порт: запусти приёмник, на его ПК выполни allow_firewall.bat.")
        elif got_tcp:
            g, gp = prim.get("get") or {}, prim.get("get_proxy")
            if gp and gp.get("ok") and not g.get("ok"):
                add(FAIL, "Прямое подключение не работает, а через системный прокси идёт", "Включи «системный прокси» в настройках мониторинга.", {"kind": "proxy_on"})
            elif f.get("proxy") and cfg.get("system_proxy") and not (gp or {}).get("ok"):
                add(FAIL, "Системный прокси %s не пускает к приёмнику" % f["proxy"], "Отключи использование системного прокси в настройках мониторинга (локальный адрес через прокси ходить не должен).", {"kind": "proxy_off"})
            else:
                add(FAIL, "Порт открыт, но приёмник не отвечает на /health", "Приёмник завис или это не он: перезапусти приёмник на ПК Макса.")
    if reach and prim and reach["url"] != prim["url"] and cfg.get("url"):
        add(FAIL, "Адрес приёмника в настройках (%s) не работает, а %s отвечает" % (prim["url"], reach["url"]),
            "Поменяй адрес на странице «Мониторинг» на %s." % reach["url"])
    link_suspect = False
    if reach:
        sk = reach.get("skew")
        if sk is not None and abs(sk) > SKEW_WARN_S:
            add(WARN, "Часы этого ПК расходятся с приёмником на %d с" % sk, "Поправь дату/время/часовой пояс в Windows (синхронизация времени).")
        if f.get("token_code") == 401:
            add(FAIL, "Приёмник не принимает токен этого завода", "В панели у завода нажми «Новый токен» и вставь его на странице «Мониторинг» (или удали токен — клиент попросит новый сам).")
        sz = f.get("sizes") or {}
        posts = sz.get("posts") or []
        bad = next((p for p in posts if p["state"] in ("timeout", "error")), None)
        mp = sz.get("max_payload")
        small_ok = bool(posts) and posts[0]["state"] == "ok"
        if bad and small_ok and (mp is None or mp < PING_MAX_PAYLOAD):
            fix_mtu = recommended_mtu(mp) if mp else 1200
            ad = next((n for n in nets if n["ips"] and reach.get("src_ip") in n["ips"]), None) or (nets[0] if nets else None)
            fix = {"kind": "mtu", "adapter": ad["adapter"], "mtu": fix_mtu} if ad and ad.get("adapter") else None
            add(FAIL, "Большие пакеты режутся по дороге (MTU/фрагментация): маленькое проходит, а POST от %d КБ — нет" % (bad["size"] // 1024),
                "Пульс с данными не помещается в то, что пропускает канал (ping без фрагментации идёт только до %s байт). Снизь MTU адаптера ZeroTier до %d (кнопка исправления ниже) "
                "или на всю сеть: ZeroTier Central → сеть → Advanced → MTU." % (mp if mp else "—", fix_mtu), fix)
        elif bad and small_ok:
            add(FAIL, "Маленькие запросы идут, большие (от %d КБ) — нет" % (bad["size"] // 1024),
                "ping без фрагментации проходит полностью, значит дело не в MTU: смотри брандмауэр/антивирус/прокси на пути.")
        elif posts and posts[0]["state"] in ("timeout", "error"):
            add(FAIL, "Даже маленький POST не проходит, хотя /health отвечает", "Смотри брандмауэр/антивирус/прокси: режут именно POST.")
        elif posts and posts[0]["state"] == "unsupported":
            add(INFO, "Большие запросы не проверены", "Приёмник старой версии (нет /netcheck): обнови его, чтобы проверка MTU была полной. По ping: %s." %
                ("полный размер проходит" if mp and mp >= PING_MAX_PAYLOAD else "проходит до %s байт" % mp if mp else "не проверен"))
            if mp is not None and mp < PING_MAX_PAYLOAD:
                link_suspect = True
                add(WARN, "ping без фрагментации режется выше %d байт" % mp, "Похоже на проблему MTU/фрагментации (ping по ZeroTier). Снизь MTU адаптера до %d." % recommended_mtu(mp),
                    {"kind": "mtu", "adapter": next((n["adapter"] for n in nets if n.get("adapter") and reach.get("src_ip") in n["ips"]), None), "mtu": recommended_mtu(mp)})
        if not link_suspect and not any(x["level"] == FAIL for x in v):
            big_ok = bool(posts) and all(p["state"] == "ok" for p in posts)
            token_ok = f.get("token_code") in (200, 400)
            checked = ["адрес отвечает"] + (["токен принят"] if token_ok else []) + (["большие запросы проходят"] if big_ok else [])
            add(OK, "Связь с приёмником в порядке" if big_ok else "Связь с приёмником есть (большие запросы не проверены)",
                "Проверено: %s. Если «нет пульса» всё равно — смотри, запущено ли приложение и включён ли мониторинг." % ", ".join(checked))
    if not v:
        add(INFO, "Явной причины не найдено", "Пришли файл отчёта net_diag_*.txt для разбора.")
    order = {FAIL: 0, WARN: 1, INFO: 2, OK: 3}
    return sorted(v, key=lambda x: order[x["level"]])


# ------------------------------------------------------------------------------------------------------------- исправления

def describe_fix(fix: dict) -> str:
    k = fix["kind"]
    if k == "mtu":
        return 'netsh interface ipv4 set subinterface "%s" mtu=%d store=persistent' % (fix["adapter"], fix["mtu"])
    if k in ("start_zt", "restart_zt"):
        return "net stop %s & net start %s" % (ZT_SERVICE, ZT_SERVICE) if k == "restart_zt" else "net start %s" % ZT_SERVICE
    if k == "proxy_off":
        return "monitor.json: system_proxy = false (не использовать прокси Windows)"
    if k == "proxy_on":
        return "monitor.json: system_proxy = true (использовать прокси Windows)"
    return str(fix)


def apply_fix(fix: dict) -> tuple[bool, str]:
    """Выполнить одно исправление. (успех, текст)."""
    k = fix["kind"]
    if k in ("mtu", "start_zt", "restart_zt") and not is_admin():
        return False, "нужны права администратора (запусти от имени администратора)"
    if k == "mtu":
        if not fix.get("adapter"):
            return False, "не знаю имя адаптера ZeroTier"
        code, out = run_cmd(["netsh", "interface", "ipv4", "set", "subinterface", fix["adapter"], "mtu=%d" % fix["mtu"], "store=persistent"])
        return code == 0, out.strip() or "готово"
    if k == "start_zt":
        code, out = run_cmd(["net", "start", ZT_SERVICE], timeout=60)
        return code == 0, out.strip()
    if k == "restart_zt":
        run_cmd(["net", "stop", ZT_SERVICE], timeout=60)
        code, out = run_cmd(["net", "start", ZT_SERVICE], timeout=60)
        return code == 0, out.strip()
    if k in ("proxy_off", "proxy_on"):
        try:
            import sysmon
            sysmon.save_config({"system_proxy": k == "proxy_on"})
            return True, "сохранено (применится при следующей отправке)"
        except Exception as e:
            return False, "не вышло: %s" % e
    return False, "неизвестное исправление"


# ----------------------------------------------------------------------------------------------------------------------- запуск

def print_verdicts(rep: Report, verdicts: list[dict]) -> None:
    rep.head("ВЫВОД")
    for x in verdicts:
        rep.step(x["level"], x["title"])
        rep.raw("       → " + x["advice"])


def offer_fixes(rep: Report, verdicts: list[dict], ask: Callable[[str], str] = input) -> int:
    """Предложить исправления по одному. Возвращает число применённых."""
    done, seen = 0, set()
    for x in verdicts:
        fix = x.get("fix")
        if not fix or describe_fix(fix) in seen:
            continue
        seen.add(describe_fix(fix))
        rep.raw("")
        rep.raw("Исправление: " + x["title"])
        rep.raw("Команда:     " + describe_fix(fix))
        try:
            ans = ask("Выполнить? [д/н]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans not in ("д", "y", "да", "yes"):
            rep.raw("Пропущено.")
            continue
        ok, text = apply_fix(fix)
        rep.step(OK if ok else FAIL, text or ("готово" if ok else "не вышло"))
        done += ok
    return done


def save_report(rep: Report) -> Optional[Path]:
    path = DATA_DIR / ("net_diag_%s.txt" % datetime.now().strftime("%Y%m%d-%H%M%S"))
    try:
        path.write_text("\n".join(rep.lines) + "\n", encoding="utf-8")
        return path
    except OSError:
        return None


def main(argv: Optional[list[str]] = None, out: Callable[[str], None] = print, ask: Callable[[str], str] = input) -> int:
    """0 — связь в порядке, 1 — найдены проблемы."""
    argv = sys.argv[1:] if argv is None else argv
    rep = Report(out)
    rep.raw("Диагностика связи с приёмником мониторинга · %s" % datetime.now().strftime("%d.%m.%Y %H:%M:%S"))
    try:
        from paths import read_version
        rep.raw("web_MVS %s · ПК %s" % (read_version(), socket.gethostname()))
    except Exception:
        rep.raw("ПК %s" % socket.gethostname())
    facts = collect(rep)
    verdicts = judge(facts)
    print_verdicts(rep, verdicts)
    problems = any(x["level"] == FAIL for x in verdicts)
    fixable = [x for x in verdicts if x.get("fix")]
    if fixable:
        if "--fix" in argv or _wants_fix(rep, ask):
            if offer_fixes(rep, verdicts, ask):
                rep.head("Проверка после исправления")
                facts = collect(rep)
                verdicts = judge(facts)
                print_verdicts(rep, verdicts)
                problems = any(x["level"] == FAIL for x in verdicts)
    path = save_report(rep)
    out("")
    out("Отчёт сохранён: %s" % path if path else "Отчёт сохранить не удалось")
    return 1 if problems else 0


def _wants_fix(rep: Report, ask: Callable[[str], str]) -> bool:
    rep.raw("")
    try:
        return ask("Есть исправления, которые могу применить. Показать? [д/н]: ").strip().lower() in ("д", "y", "да", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


if __name__ == "__main__":
    code = main()
    try:
        input("Нажми Enter, чтобы закрыть окно...")
    except Exception:
        pass
    sys.exit(code)
