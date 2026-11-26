"""Клиент OPC UA — ТОЛЬКО чтение: данные аппарата (СВ, стадия, температура, …) для sv_source и обзор сервера со страницы.

Записи в сервер здесь нет и быть не должно (решение Макса 2026-11-09): только read / browse / endpoints.

Библиотека asyncua (opcua-asyncio), синхронная обёртка asyncua.sync: у каждого подключения свой поток с asyncio-циклом
(ThreadLoop). Поток надо гасить и при неудачном подключении — иначе на каждом переподключении копится по потоку
(см. _dispose).

Безопасность: «None» или политики с подписью / шифрованием (Basic256Sha256, Aes128_Sha256_RsaOaep, Aes256_Sha256_RsaPss),
вход анонимно или логином/паролем. Для шифрования у клиента свой самоподписанный сертификат (data\\opcua_pki\\, делается
один раз на 10 лет) — его надо один раз «доверить» на сервере (скачать со страницы: /api/opcua/client_cert).
Логин и пароль лежат в data\\opcua_secret.json и через API наружу не отдаются (secret_public).
"""
from __future__ import annotations

import datetime
import ipaddress
import json
import os
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from paths import DATA_DIR

SECRET_PATH = DATA_DIR / "data" / "opcua_secret.json"
PKI_DIR = DATA_DIR / "data" / "opcua_pki"
CERT_PATH = PKI_DIR / "web_mvs_client.der"
KEY_PATH = PKI_DIR / "web_mvs_client_key.pem"
APP_URI = "urn:web_mvs:client"
DEFAULT_PORT = 4840

POLICIES = ("None", "Basic256Sha256", "Aes128_Sha256_RsaOaep", "Aes256_Sha256_RsaPss")
MODES = ("Sign", "SignAndEncrypt")
BROWSE_LIMIT = 500          # детей узла больше — показываем первые по алфавиту + поиск по имени (страница не должна захлебнуться)
FIND_MAX = 100              # длина строки поиска в узле
READ_CHUNK = 50             # узлов в одном Read: у многих ПЛК/шлюзов MaxNodesPerRead около 100 и меньше

_cert_lock = threading.Lock()
_secret_lock = threading.Lock()
_scan_lock = threading.Lock()


# ───────────────────────────── логин / пароль (data\opcua_secret.json) ─────────────────────────────
def load_secret() -> dict:
    try:
        d = json.loads(SECRET_PATH.read_text(encoding="utf-8"))
        return {"username": str(d.get("username") or ""), "password": str(d.get("password") or "")}
    except Exception:
        return {"username": "", "password": ""}


def save_secret(username: Optional[str] = None, password: Optional[str] = None) -> dict:
    """None — поле не трогаем (пароль на странице пустой = «оставить прежний»)."""
    with _secret_lock:
        cur = load_secret()
        if username is not None:
            cur["username"] = str(username)
        if password is not None:
            cur["password"] = str(password)
        SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SECRET_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:              # на диск до переименования: после отключения питания не пустой
            fh.write(json.dumps(cur, ensure_ascii=False))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, SECRET_PATH)
        return secret_public(cur)


def secret_public(sec: Optional[dict] = None) -> dict:
    """Что можно показать на странице: логин и есть ли пароль. Сам пароль — никогда."""
    sec = sec if sec is not None else load_secret()
    return {"username": sec.get("username", ""), "has_password": bool(sec.get("password"))}


# ───────────────────────────── сертификат клиента ─────────────────────────────
def ensure_client_cert() -> tuple[Path, Path]:
    """Самоподписанный сертификат клиента (10 лет). Есть — не трогаем: сервер его уже мог «доверить»."""
    with _cert_lock:
        if CERT_PATH.is_file() and KEY_PATH.is_file():
            return CERT_PATH, KEY_PATH
        from cryptography.hazmat.primitives.serialization import Encoding
        from cryptography import x509
        from cryptography.x509.oid import ExtendedKeyUsageOID
        from asyncua.crypto.cert_gen import (dump_private_key_as_pem, generate_private_key,
                                             generate_self_signed_app_certificate)
        PKI_DIR.mkdir(parents=True, exist_ok=True)
        host = socket.gethostname() or "web-mvs"
        try:
            dns = host.encode("idna").decode("ascii")           # имя ПК кириллицей — в сертификат только ASCII (punycode)
        except Exception:
            dns = "web-mvs"
        key = generate_private_key()
        cert = generate_self_signed_app_certificate(
            key, APP_URI, {"commonName": "web_MVS %s" % host, "organizationName": "Maximum Automation"},
            [x509.UniformResourceIdentifier(APP_URI), x509.DNSName(dns)],
            extended=[ExtendedKeyUsageOID.CLIENT_AUTH], days=3650)
        KEY_PATH.write_bytes(dump_private_key_as_pem(key))
        CERT_PATH.write_bytes(cert.public_bytes(encoding=Encoding.DER))
        return CERT_PATH, KEY_PATH


# ───────────────────────────── настройки подключения ─────────────────────────────
def normalize_conn(cfg: Optional[dict]) -> dict:
    """Раздел sv_source.opcua → проверенные параметры подключения. Ошибка → ValueError по-русски."""
    cfg = cfg or {}
    url = str(cfg.get("endpoint") or "").strip()
    if url and not url.lower().startswith("opc.tcp://"):
        raise ValueError("адрес сервера OPC UA должен начинаться с opc.tcp://")
    if "@" in url.split("://", 1)[-1].split("/", 1)[0]:
        raise ValueError("логин и пароль в адресе не пишем — для них поля «Логин» и «Пароль»")
    policy = str(cfg.get("security") or "None")
    if policy not in POLICIES:
        raise ValueError("неизвестная политика безопасности: %s" % policy)
    mode = str(cfg.get("mode") or "SignAndEncrypt")
    if mode not in MODES:
        raise ValueError("неизвестный режим безопасности: %s" % mode)
    auth = str(cfg.get("auth") or "anonymous")
    if auth not in ("anonymous", "user"):
        raise ValueError("вход: anonymous или user")
    try:
        timeout = min(30.0, max(0.5, float(cfg.get("timeout_s", 3))))
    except (TypeError, ValueError):
        raise ValueError("таймаут OPC UA — число секунд")
    return {"endpoint": url, "security": policy, "mode": mode, "auth": auth, "timeout_s": timeout}


def _new_client(url: str, timeout: float):
    from asyncua.sync import Client
    # sync_wrapper_timeout — сколько ждать ответа из потока asyncua; без него зависший сервер держал бы поток 120 с
    c = Client(url, timeout=timeout, sync_wrapper_timeout=timeout * 3 + 5)
    c.application_uri = APP_URI
    return c


CLOSE_TIMEOUT = 3.0     # закрытие сессии ждём не дольше: мёртвая сеть не должна держать перезапуск источника десятки секунд


def _dispose(client, connected: bool):
    """Закрыть подключение и ОБЯЗАТЕЛЬНО погасить его поток asyncio. connected — сессия могла быть создана (в т.ч. connect
    упал на входе): пробуем закрыть её на сервере, иначе она висит там до таймаута и съедает лимит сессий."""
    if client is None:
        return
    try:
        client.tloop.timeout = min(client.tloop.timeout or CLOSE_TIMEOUT, CLOSE_TIMEOUT)
    except Exception:
        pass
    try:
        if connected:
            client.disconnect()                                 # disconnect сам гасит поток (в finally)
            return
    except Exception:
        pass
    try:
        if client.close_tloop and client.tloop.is_alive():
            client.tloop.stop()
    except Exception:
        pass


class OpcUaConn:
    """Одно подключение к серверу OPC UA (сессия). Не потокобезопасно: пользуется один поток (sv_source или запрос страницы)."""

    def __init__(self, cfg: dict, secret: Optional[dict] = None):
        self.cfg = normalize_conn(cfg)
        if not self.cfg["endpoint"]:
            raise ValueError("не задан адрес сервера OPC UA")
        self._secret = secret if secret is not None else load_secret()
        self._client = None
        self._nodes: dict = {}

    def connect(self):
        from asyncua import ua
        from asyncua.crypto import security_policies as sp
        c = _new_client(self.cfg["endpoint"], self.cfg["timeout_s"])
        try:
            if self.cfg["security"] != "None":
                cert, key = ensure_client_cert()
                policy = getattr(sp, "SecurityPolicy" + self.cfg["security"].replace("_", ""))
                c.set_security(policy, str(cert), str(key), mode=getattr(ua.MessageSecurityMode, self.cfg["mode"]))
            if self.cfg["auth"] == "user":
                if not self._secret.get("username"):
                    raise ValueError("вход по логину, но логин не задан")
                c.set_user(self._secret["username"])
                c.set_password(self._secret.get("password", ""))
            c.connect()
        except BaseException:
            _dispose(c, True)                                   # сессия могла открыться (вход отклонён) — закрыть на сервере
            raise
        self._client = c
        self._nodes = {}

    def close(self):
        c, self._client = self._client, None
        _dispose(c, True)

    @property
    def connected(self) -> bool:
        return self._client is not None

    def _node(self, nid: str):
        n = self._nodes.get(nid)
        if n is None:
            n = self._nodes[nid] = self._client.get_node(nid)
        return n

    def _read_attr(self, nodes, attr):
        """Read пачками по READ_CHUNK узлов (сервер с маленьким MaxNodesPerRead иначе ответит BadTooManyOperations)."""
        out = []
        for i in range(0, len(nodes), READ_CHUNK):
            out += self._client.read_attributes(nodes[i:i + READ_CHUNK], attr)
        return out

    def read_many(self, node_ids) -> dict:
        """{NodeId: значение} одним запросом Read. Узел с плохим статусом (нет такого, нет доступа, нет связи у сервера с ПЛК)
        → None, а не 0: на странице «(—)», в пробу не идёт. Сетевая ошибка — исключение (sv_source переподключится)."""
        from asyncua import ua
        ids = [str(n) for n in node_ids if n]
        if not ids:
            return {}
        out = {}
        nodes, good_ids = [], []
        for nid in ids:
            try:
                nodes.append(self._node(nid))
                good_ids.append(nid)
            except Exception:                                  # NodeId не разбирается — значения нет
                out[nid] = None
        if nodes:
            dvs = self._read_attr(nodes, ua.AttributeIds.Value)
            for nid, dv in zip(good_ids, dvs):
                # Uncertain (датчик под сомнением) тоже не берём: СВ/стадия рулят автоматом, лучше «(—)», чем сомнительное число
                out[nid] = _plain(dv.Value.Value) if dv.StatusCode.is_good() else None
        return out

    def probe(self, node_id: str) -> dict:
        """«Проверить тег»: один Read (класс, имя, значение, тип) → что пришло и, если значения нет, почему — по-русски.
        Только чтение. {node, name, cls, type, value, number, status, good, time, why}."""
        from asyncua import ua
        nid = str(node_id or "").strip()
        if not nid:
            raise ValueError("впиши NodeId тега, например ns=1;s=…")
        if len(nid) > 512:
            raise ValueError("слишком длинный NodeId")
        out = {"node": nid, "name": "", "cls": "", "type": "", "value": None, "number": None,
               "status": "", "good": False, "time": None, "why": ""}
        try:
            node = self._client.get_node(nid)
        except Exception:
            out.update(status="BadNodeIdInvalid", why=_TAG_WHY["BadNodeIdInvalid"])
            return out
        A = ua.AttributeIds
        cls_dv, name_dv, val_dv, type_dv = node.read_attributes([A.NodeClass, A.DisplayName, A.Value, A.DataType])
        if not cls_dv.StatusCode.is_good():                     # узла нет вовсе
            st = cls_dv.StatusCode.name
            out.update(status=st, why=_TAG_WHY.get(st, "сервер ответил %s" % st))
            return out
        out["cls"] = ua.NodeClass(cls_dv.Value.Value).name
        if name_dv.StatusCode.is_good() and name_dv.Value.Value is not None:
            out["name"] = name_dv.Value.Value.Text or ""
        if out["cls"] != "Variable":
            out["why"] = "это не тег-значение, а %s — раскрой его в дереве и выбери переменную внутри" % out["cls"]
            return out
        if type_dv.StatusCode.is_good():
            out["type"] = _type_name(type_dv.Value.Value)
        st = val_dv.StatusCode.name
        ts = val_dv.SourceTimestamp or val_dv.ServerTimestamp
        if ts is not None:
            out["time"] = (ts if ts.tzinfo else ts.replace(tzinfo=datetime.timezone.utc)).isoformat()
        out["status"], out["good"] = st, val_dv.StatusCode.is_good()
        raw = val_dv.Value.Value if val_dv.Value is not None else None
        out["value"] = _show(raw)
        if out["good"]:
            out["number"] = _plain(raw)
            if out["number"] is None:
                out["why"] = "значение не число (%s) — на поле аппарата его назначить нельзя" % (out["type"] or type(raw).__name__)
        elif st.startswith("Uncertain"):
            out["why"] = "значение «под сомнением» (%s) — в автомат такое не идёт, на странице будет «(—)»" % st
        else:
            out["why"] = _TAG_WHY.get(st, "сервер не отдал значение: %s" % st)
        return out

    def browse(self, node_id: Optional[str] = None, find: str = "") -> list:
        """Дети узла (по умолчанию — Objects): [{node, name, cls, type, value, has_children}]. Значения переменных — одним Read."""
        return self.browse_page(node_id, find)["items"]

    def browse_page(self, node_id: Optional[str] = None, find: str = "") -> dict:
        """Дети узла для «проводника»: {items, total, matched}.
        Сервер может ссылаться на один узел дважды (Organizes + HasNotifier/HasEventSource у SCADA) — дубли по NodeId убираем.
        Детей бывает тысячи (AS у PCS7): сортируем ВСЕХ по имени, фильтруем по find, и только потом режем до BROWSE_LIMIT —
        иначе в дерево попадали первые 500 в порядке сервера, а остальные теги пропадали без следа."""
        from asyncua import ua
        parent = self._client.get_node(node_id) if node_id else self._client.nodes.objects
        seen, descs = set(), []
        for d in parent.get_children_descriptions():
            nid = d.NodeId.to_string()
            if nid in seen:
                continue
            seen.add(nid)
            descs.append((nid, d.DisplayName.Text or d.BrowseName.Name or nid, d))
        total = len(descs)
        q = str(find or "").strip().lower()[:FIND_MAX]
        if q:
            descs = [x for x in descs if q in x[1].lower() or q in (x[2].BrowseName.Name or "").lower()]
        matched = len(descs)
        descs.sort(key=lambda x: (x[2].NodeClass.name == "Variable", x[1].lower()))
        items, var_nodes = [], []
        for nid, name, d in descs[:BROWSE_LIMIT]:
            cls = d.NodeClass.name
            item = {"node": nid, "name": name,
                    "browse": d.BrowseName.to_string(), "cls": cls, "type": "", "value": None,
                    "has_children": cls in ("Object", "View", "Variable")}
            items.append(item)
            if cls == "Variable":
                var_nodes.append((item, self._client.get_node(d.NodeId)))
        if var_nodes:
            nodes = [n for _, n in var_nodes]
            vals = self._read_attr(nodes, ua.AttributeIds.Value)
            types = self._read_attr(nodes, ua.AttributeIds.DataType)
            for (item, _), dv, tv in zip(var_nodes, vals, types):
                item["value"] = _show(dv.Value.Value) if dv.StatusCode.is_good() else None
                item["status"] = "" if dv.StatusCode.is_good() else dv.StatusCode.name
                item["type"] = _type_name(tv.Value.Value) if tv.StatusCode.is_good() else ""
                # переменная-число — лист дерева; структура/массив (UDT у Siemens/Schneider) — раскрывается до своих полей
                item["has_children"] = dv.StatusCode.is_good() and not isinstance(dv.Value.Value, (bool, int, float, str))
        return {"items": items, "total": total, "matched": matched}


def _plain(v):
    """Значение OPC UA → число для sv_source: bool → 0/1, число как есть, остальное (строка, массив, структура) → None."""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return _round(v)
    return None


def _round(v: float):
    """Float32 сервера (86.4 → 86.4000015258789) — до 6 значащих цифр; NaN/inf — значения нет."""
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return float("%.6g" % v)


def _show(v):
    """Значение для показа в дереве (JSON): число/строка/bool, длинное — обрезано."""
    if isinstance(v, float):
        return _round(v)
    if v is None or isinstance(v, (bool, int)):
        return v
    s = str(v)
    return s if len(s) <= 60 else s[:57] + "…"


def _type_name(nodeid) -> str:
    try:
        from asyncua import ua
        if nodeid.NamespaceIndex == 0:
            return ua.ObjectIdNames.get(nodeid.Identifier, str(nodeid.Identifier))
        return nodeid.to_string()
    except Exception:
        return ""


# ───────────────────────────── понятный текст ошибки ─────────────────────────────
_HINTS = {
    "BadUserAccessDenied": "сервер не пустил: неверный логин/пароль или анонимный вход запрещён",
    "BadIdentityTokenInvalid": "сервер не принимает такой способ входа — выбери другой (анонимно / логин)",
    "BadIdentityTokenRejected": "сервер отклонил логин/пароль",
    "BadSecurityChecksFailed": "сервер не доверяет сертификату web_MVS — добавь его в доверенные на сервере (ссылка «сертификат»)",
    "BadCertificateUntrusted": "сервер не доверяет сертификату web_MVS — добавь его в доверенные на сервере (ссылка «сертификат»)",
    "BadCertificateInvalid": "сертификат отклонён сервером",
    "BadSecurityPolicyRejected": "сервер не поддерживает выбранную защиту — выбери режим из предложенных",
    "BadSecurityModeRejected": "сервер не поддерживает выбранный режим защиты — выбери из предложенных",
    "BadTooManySessions": "на сервере кончились сессии — закрой лишние подключения",
    "BadTcpEndpointUrlInvalid": "сервер не знает такой адрес (путь после порта) — возьми адрес из его списка",
}


_NO_LINK = "сервер OPC UA на связи, но значения у него нет — нет связи с контроллером/датчиком или тег не обновляется (%s)"
_TAG_WHY = {
    "BadNodeIdUnknown": "такого тега на сервере нет — проверь NodeId целиком: ns=… и строку после s= до последнего символа",
    "BadNodeIdInvalid": "NodeId записан неверно — нужен вид ns=1;s=Имя или ns=2;i=1234",
    "BadNotReadable": "тег закрыт на чтение на сервере",
    "BadUserAccessDenied": "этому входу нельзя читать тег — попробуй вход по логину",
    "BadAttributeIdInvalid": "у этого узла нет значения",
    **{k: _NO_LINK % k for k in ("BadNotConnected", "BadNoCommunication", "BadCommunicationError", "BadOutOfService",
                                 "BadWaitingForInitialData", "BadServerNotConnected", "BadDeviceFailure", "BadSensorFailure",
                                 "BadConfigurationError")},
}


def explain(e: BaseException) -> str:
    """Исключение asyncua/сети → короткая причина по-русски (код ошибки сервера — в скобках, чтобы было что искать)."""
    name = type(e).__name__
    if name in _HINTS:
        return "%s (%s)" % (_HINTS[name], name)
    if isinstance(e, TimeoutError) or name in ("TimeoutError", "CancelledError"):
        return "сервер не ответил вовремя — проверь адрес, порт и сеть"
    if isinstance(e, ConnectionRefusedError):
        return "соединение отклонено: на этом адресе/порту нет сервера OPC UA"
    if isinstance(e, OSError):
        return "нет связи с сервером: %s" % (e.strerror or e)
    return str(e) or name


# ───────────────────────────── разовые операции для страницы ─────────────────────────────
def get_endpoints(url: str, timeout: float = 3.0) -> list:
    """Что предлагает сервер: [{security, mode, tokens, url}] — без входа, без сессии (GetEndpoints)."""
    url = normalize_conn({"endpoint": url, "timeout_s": timeout})["endpoint"]
    if not url:
        raise ValueError("не задан адрес сервера OPC UA")
    c = _new_client(url, timeout)
    try:
        eps = c.connect_and_get_server_endpoints()
    finally:
        _dispose(c, False)                                   # сессии не было — гасим только поток
    out, seen = [], set()
    for e in eps:
        pol = (e.SecurityPolicyUri or "").rsplit("#", 1)[-1] or "None"
        mode = e.SecurityMode.name if e.SecurityMode is not None else "None"
        mode = "None" if mode.startswith("None") else mode
        tokens = sorted({t.TokenType.name for t in (e.UserIdentityTokens or [])})
        key = (pol, mode)
        if key in seen:
            continue
        seen.add(key)
        out.append({"security": pol, "mode": mode, "tokens": tokens, "url": e.EndpointUrl,
                    "supported": pol in POLICIES and (pol == "None") == (mode == "None")})
    return out


def with_conn(cfg: dict, fn, secret: Optional[dict] = None):
    """Открыть сессию, выполнить fn(conn), закрыть. Для обзора/пробного чтения со страницы — отдельно от sv_source."""
    conn = OpcUaConn(cfg, secret)
    conn.connect()
    try:
        return fn(conn)
    finally:
        conn.close()


def local_subnets() -> list:
    """Подсети /24 этого ПК (по его IPv4), без 127.x и 169.254.x — для поиска серверов."""
    nets = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except Exception:
        infos = []
    for info in infos:
        ip = info[4][0]
        if ip.startswith("127.") or ip.startswith("169.254."):
            continue
        net = str(ipaddress.ip_network(ip + "/24", strict=False))
        if net not in nets:
            nets.append(net)
    return nets


def scan(subnet: str, port: int = DEFAULT_PORT, timeout: float = 0.4, limit: int = 1024) -> list:
    """Поиск серверов OPC UA: TCP-порт port открыт на адресах подсети → [{host, url}]. Только попытка соединения,
    без входа и без записи. Подсеть не больше /22 (limit адресов), чтобы поиск шёл секунды, а не минуты."""
    try:
        net = ipaddress.ip_network(str(subnet).strip(), strict=False)
    except ValueError:
        raise ValueError("подсеть пишется так: 192.168.1.0/24")
    if net.version != 4 or net.num_addresses > limit:
        raise ValueError("подсеть IPv4 не больше /22, например 192.168.1.0/24")
    if not net.is_private or net.is_loopback and net.num_addresses > 1:
        raise ValueError("искать можно только в заводской (частной) сети: 10.x, 172.16–31.x, 192.168.x")
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError("порт — от 1 до 65535")
    if not _scan_lock.acquire(blocking=False):
        raise ValueError("поиск уже идёт — дождись его")
    try:
        return _scan(net, port, timeout)
    finally:
        _scan_lock.release()


def _scan(net, port, timeout):
    def probe(ip):
        try:
            with socket.create_connection((str(ip), port), timeout=timeout):
                return str(ip)
        except OSError:
            return None

    hosts = list(net.hosts()) if net.num_addresses > 1 else [net.network_address]
    with ThreadPoolExecutor(max_workers=64) as ex:
        found = [h for h in ex.map(probe, hosts) if h]
    return [{"host": h, "url": "opc.tcp://%s:%d" % (h, port)} for h in found]
