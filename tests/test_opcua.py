"""OPC UA (только чтение): клиент opcua_client против эмулятора scripts/opcua_sim.py, SvSource по OPC UA,
настройки вкладки «ПЛК» (plc_source) и что Modbus-путь не изменился. Боевые файлы не трогаем (DATA_DIR → tmp)."""
import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

asyncua = pytest.importorskip("asyncua")

import opcua_client  # noqa: E402
import plate_config  # noqa: E402
import plc_source  # noqa: E402
from sv_source import SvSource  # noqa: E402

PORT = 48431
USERS = {"op": "123"}


@pytest.fixture(scope="module")
def sim():
    from opcua_sim import start_sim
    s = start_sim(PORT, secure=True, users=USERS, animate=False)
    yield s
    s.stop()


@pytest.fixture
def pki(tmp_path, monkeypatch):
    """Сертификат клиента и логин/пароль — во временной папке теста."""
    monkeypatch.setattr(opcua_client, "PKI_DIR", tmp_path / "pki")
    monkeypatch.setattr(opcua_client, "CERT_PATH", tmp_path / "pki" / "c.der")
    monkeypatch.setattr(opcua_client, "KEY_PATH", tmp_path / "pki" / "k.pem")
    monkeypatch.setattr(opcua_client, "SECRET_PATH", tmp_path / "data" / "opcua_secret.json")
    return tmp_path


def secure_cfg(sim, **kw):
    return dict({"endpoint": sim.url, "security": "Basic256Sha256", "mode": "SignAndEncrypt", "auth": "user", "timeout_s": 3}, **kw)


def wait(pred, timeout=15.0):
    t = time.time()
    while time.time() - t < timeout:
        if pred():
            return True
        time.sleep(0.1)
    return False


# ───────────────────────── клиент ─────────────────────────
def test_endpoints_list_security_modes(sim):
    eps = opcua_client.get_endpoints(sim.url)
    keys = {(e["security"], e["mode"]) for e in eps}
    assert ("None", "None") in keys and ("Basic256Sha256", "SignAndEncrypt") in keys and ("Basic256Sha256", "Sign") in keys
    assert all(e["supported"] for e in eps)
    assert all("UserName" in e["tokens"] for e in eps)


def test_browse_and_read_encrypted_with_login(sim, pki):
    def work(c):
        top = c.browse()
        va = next(x for x in top if x["name"] == "VA")
        kids = {x["name"]: x for x in c.browse(va["node"])}
        vals = c.read_many([sim.node_id("Brix"), sim.node_id("Mode"), sim.node_id("Broken"), "ns=2;s=Нет.Такого", "мусор"])
        return kids, vals
    kids, vals = opcua_client.with_conn(secure_cfg(sim), work, {"username": "op", "password": "123"})
    assert kids["Brix"]["cls"] == "Variable" and kids["Brix"]["value"] == pytest.approx(86.4) and kids["Brix"]["type"] == "Float"
    assert kids["Recipe"]["has_children"] and not kids["Brix"]["has_children"]
    assert kids["Broken"]["value"] is None and kids["Broken"]["status"]          # плохой статус — без значения
    assert vals[sim.node_id("Brix")] == pytest.approx(86.4)                       # Float32 округлён, не 86.4000015
    assert vals[sim.node_id("Mode")] == 5
    assert vals[sim.node_id("Broken")] is None and vals["ns=2;s=Нет.Такого"] is None and vals["мусор"] is None
    assert opcua_client.CERT_PATH.is_file() and opcua_client.KEY_PATH.is_file()   # сертификат клиента сделан сам


def test_wrong_password_and_anonymous_denied_without_thread_leak(sim, pki):
    before = threading.active_count()
    with pytest.raises(Exception):
        opcua_client.with_conn(secure_cfg(sim), lambda c: 1, {"username": "op", "password": "плохой"})
    with pytest.raises(Exception):
        opcua_client.with_conn({"endpoint": sim.url}, lambda c: 1, {})
    for _ in range(3):                                                          # мёртвый адрес
        with pytest.raises(Exception):
            opcua_client.with_conn({"endpoint": "opc.tcp://127.0.0.1:48499", "timeout_s": 0.5}, lambda c: 1)
    assert wait(lambda: threading.active_count() <= before, 5)                  # потоки asyncio погашены


def test_normalize_conn_rejects_bad_values():
    for bad in ({"endpoint": "http://x"}, {"security": "Basic128"}, {"mode": "Encrypt"}, {"auth": "cert"}):
        with pytest.raises(ValueError):
            opcua_client.normalize_conn(bad)


def test_secret_never_public(pki):
    pub = opcua_client.save_secret("op", "секрет")
    assert pub == {"username": "op", "has_password": True}
    opcua_client.save_secret(username="op2")                                    # пароль не трогаем
    assert opcua_client.load_secret() == {"username": "op2", "password": "секрет"}


def test_scan_finds_open_port(sim):
    found = opcua_client.scan("127.0.0.1/32", PORT)
    assert found == [{"host": "127.0.0.1", "url": "opc.tcp://127.0.0.1:%d" % PORT}]
    with pytest.raises(ValueError):
        opcua_client.scan("10.0.0.0/16")                                        # слишком большая подсеть


# ───────────────────────── SvSource по OPC UA ─────────────────────────
def opc_source_cfg(sim, **fields):
    return {"protocol": "opcua", "period_s": 0.2, "glitch_filter": False,
            "opcua": dict(secure_cfg(sim), fields={k: {"node": sim.node_id(v), "scale": 1} for k, v in fields.items()})}


def test_sv_source_reads_opcua(sim, pki):
    opcua_client.save_secret("op", "123")
    seen = []
    src = SvSource(opc_source_cfg(sim, sv="Brix", stage="Mode", temp_app="T_app", v_product="V_product", level="Broken"),
                   on_update=lambda sv, st: seen.append((sv, st)))
    assert src.protocol == "opcua" and src.host == sim.url
    src.start()
    try:
        assert wait(lambda: src.status()["connected"])
        st = src.status()
        assert st["protocol"] == "opcua"
        v = st["values"]
        assert v["sv"] == pytest.approx(86.4) and v["stage"] == 5 and isinstance(v["stage"], int)
        assert v["temp_app"] == pytest.approx(71.2) and v["v_product"] == 100.0
        assert v["level"] is None                                               # тег с плохим статусом — «(—)», не 0
        assert seen and seen[-1][0] == pytest.approx(86.4) and seen[-1][1] == 5
        sim.set("Mode", 7)
        assert wait(lambda: src.status()["values"]["stage"] == 7)
        assert src.requests > 0
    finally:
        src.stop()


def test_sv_source_opcua_scale_and_float_stage(sim, pki):
    opcua_client.save_secret("op", "123")
    cfg = opc_source_cfg(sim, sv="Brix", stage="Brix")
    cfg["opcua"]["fields"]["sv"]["scale"] = 10
    src = SvSource(cfg)
    src.start()
    try:
        assert wait(lambda: src.status()["connected"])
        v = src.status()["values"]
        assert v["sv"] == pytest.approx(8.64) and v["stage"] == 86                # стадия — целое даже из Float
    finally:
        src.stop()


def test_sv_source_opcua_bad_login_reports_error(sim, pki):
    opcua_client.save_secret("op", "плохой")
    src = SvSource(opc_source_cfg(sim, sv="Brix"))
    src.start()
    try:
        assert wait(lambda: src.status()["error"] is not None)
        assert not src.status()["connected"]
        assert "неверный логин/пароль" in src.status()["error"] and "BadUserAccessDenied" in src.status()["error"]   # по-русски + код
    finally:
        src.stop()


def test_sv_source_opcua_reconnects_after_server_restart(pki):
    from opcua_sim import start_sim
    port = PORT + 1
    s1 = start_sim(port, animate=False)
    src = SvSource({"protocol": "opcua", "period_s": 0.2, "glitch_filter": False,
                    "opcua": {"endpoint": s1.url, "fields": {"sv": {"node": s1.node_id("Brix")}}}})
    src.start()
    try:
        assert wait(lambda: src.status()["connected"])
        s1.stop()
        assert wait(lambda: not src.status()["connected"], 20)
        s2 = start_sim(port, animate=False)
        try:
            assert wait(lambda: src.status()["connected"], 30)
            assert src.status()["values"]["sv"] == pytest.approx(86.4)
        finally:
            s2.stop()
    finally:
        src.stop()


def test_sv_source_without_nodes_does_not_start():
    src = SvSource({"protocol": "opcua", "opcua": {"endpoint": "opc.tcp://127.0.0.1:1", "fields": {"sv": {"node": ""}}}})
    src.start()
    assert src._thread is None                                                  # читать нечего — поток не нужен


# ───────────────────────── Modbus не изменился ─────────────────────────
def test_modbus_source_fields_unchanged():
    sv = plate_config.DEFAULTS["sv_source"]
    src = SvSource(sv)
    assert src.protocol == "modbus" and src.host == sv["host"]
    assert src.fields["sv"] == (650, 100.0, False) and src.fields["temp_app"] == (651, 1.0, True)
    assert "vacuum" not in src.fields                                           # reg 0 — не читается
    assert src._value("stage", 5, 1.0) == 5 and src._value("sv", 8640, 100.0) == pytest.approx(86.4)


# ───────────────────────── настройки вкладки «ПЛК» ─────────────────────────
def test_plc_normalize_and_public_hide_password(data_dir, pki):
    patch, secret = plc_source.normalize({
        "enabled": True, "protocol": "opcua", "period_s": 2,
        "modbus": {"host": "10.0.0.5", "port": 502, "unit": 1, "fields": {"sv": {"reg": 700, "scale": 100}, "лишнее": {"reg": 1}}},
        "opcua": {"endpoint": "opc.tcp://10.0.0.9:4840", "security": "Basic256Sha256", "mode": "Sign", "auth": "user",
                  "username": "op", "password": "секрет", "fields": {"sv": {"node": "ns=3;s=Brix", "scale": 1}}}})
    assert patch["fields"] == {"sv": {"reg": 700, "scale": 100.0, "signed": False}}            # неизвестные поля отброшены
    assert patch["opcua"]["fields"]["sv"]["node"] == "ns=3;s=Brix" and "password" not in json.dumps(patch)
    assert secret == {"username": "op", "password": "секрет"}
    opcua_client.save_secret(**secret)
    plate_config.save({"sv_source": patch})
    pub = plc_source.public()
    assert pub["protocol"] == "opcua" and pub["opcua"]["has_password"] and pub["opcua"]["username"] == "op"
    assert "секрет" not in json.dumps(pub, ensure_ascii=False)
    # пустой пароль со страницы — прежний остаётся
    _, secret2 = plc_source.normalize({"opcua": {"endpoint": "", "username": "op"}})
    assert secret2 == {"username": "op", "password": None}


@pytest.mark.parametrize("body", [
    {"protocol": "profibus"},
    {"modbus": {"port": 70000}},
    {"modbus": {"fields": {"sv": {"reg": 1, "scale": 0}}}},
    {"opcua": {"endpoint": "10.0.0.1:4840"}},
    {"opcua": {"fields": {"sv": {"node": "x" * 600}}}},
])
def test_plc_normalize_rejects(body):
    with pytest.raises(ValueError):
        plc_source.normalize(body)


def test_opcua_config_saved_from_page_not_dropped_as_stale(data_dir):
    # в файле только OPC UA (Modbus-регистров нет) — это не старый слепок DEFAULTS, выкидывать нельзя
    plate_config.save({"sv_source": {"protocol": "opcua", "opcua": {"endpoint": "opc.tcp://1.2.3.4:4840"}}})
    sv = plate_config.load()["sv_source"]
    assert sv["protocol"] == "opcua" and sv["opcua"]["endpoint"] == "opc.tcp://1.2.3.4:4840"
    assert sv["fields"]["sv"]["reg"] == 650                                     # Modbus-поля — из DEFAULTS
    # старый слепок (все reg = 0, без opcua) по-прежнему выкидывается
    plate_config.CONFIG_PATH.write_text(json.dumps({"sv_source": {"enabled": False, "fields": {"sv": {"reg": 0}}}}), encoding="utf-8")
    assert plate_config.load()["sv_source"]["enabled"] is True


def test_api_save_restarts_only_source(data_dir, pki, sim, monkeypatch):
    import app as web
    from microscope_service import micro
    calls = []
    monkeypatch.setattr(micro, "set_plc_source", lambda patch: (calls.append(patch), plate_config.save({"sv_source": patch})))
    r = web.plc_source_save({"protocol": "opcua", "opcua": {"endpoint": sim.url, "auth": "user", "username": "op", "password": "123"}})
    assert r["status"] == "ok" and calls and calls[0]["protocol"] == "opcua"
    assert "123" not in json.dumps(r) and r["config"]["opcua"]["has_password"]
    bad = web.plc_source_save({"protocol": "x"})
    assert bad["status"] == "error"
    # обзор и пробное чтение — сохранённым паролем (страница его не присылает)
    br = web.opcua_browse({"endpoint": sim.url, "security": "Basic256Sha256", "mode": "SignAndEncrypt", "auth": "user", "node": "ns=%d;s=VA" % sim.ns})
    assert br["ok"] and any(x["name"] == "Brix" for x in br["items"])
    rd = web.opcua_read({"endpoint": sim.url, "auth": "user", "nodes": [sim.node_id("Submode")]})
    assert rd["ok"] and rd["values"][sim.node_id("Submode")] == 2
    # сохранённый пароль чужому адресу не отдаём (иначе запросом его можно увести на любой хост)
    other = web.opcua_browse({"endpoint": "opc.tcp://127.0.0.1:48431/другой", "auth": "user"})
    assert other["ok"] is False and "впиши пароль" in other["error"]
    ep = web.opcua_endpoints({"endpoint": "opc.tcp://127.0.0.1:48499", "timeout_s": 0.5})
    assert ep["ok"] is False and ep["error"]


# ───────────────────────── после ревью ─────────────────────────
def test_saved_password_only_for_saved_endpoint(data_dir, pki):
    opcua_client.save_secret("op", "секрет")
    plate_config.save({"sv_source": {"protocol": "opcua", "opcua": {"endpoint": "opc.tcp://10.0.0.9:4840"}}})
    _, sec = plc_source.conn_from_body({"endpoint": "opc.tcp://10.0.0.9:4840", "auth": "user"})
    assert sec == {"username": "op", "password": "секрет"}
    with pytest.raises(ValueError):
        plc_source.conn_from_body({"endpoint": "opc.tcp://evil:4840", "auth": "user"})
    _, sec2 = plc_source.conn_from_body({"endpoint": "opc.tcp://evil:4840", "auth": "user", "username": "u", "password": "p"})
    assert sec2 == {"username": "u", "password": "p"}                          # свой пароль в запросе — можно


def test_endpoint_with_login_in_url_rejected():
    with pytest.raises(ValueError):
        opcua_client.normalize_conn({"endpoint": "opc.tcp://op:123@10.0.0.9:4840"})


@pytest.mark.parametrize("subnet,port", [("8.8.8.0/24", 4840), ("10.0.0.0/24", 70000), ("мусор", 4840)])
def test_scan_limits(subnet, port):
    with pytest.raises(ValueError):
        opcua_client.scan(subnet, port)


def test_scan_one_at_a_time():
    assert opcua_client._scan_lock.acquire(blocking=False)
    try:
        with pytest.raises(ValueError):
            opcua_client.scan("127.0.0.1/32", PORT)
    finally:
        opcua_client._scan_lock.release()


def test_clear_password_and_modbus_regs_limit():
    _, sec = plc_source.normalize({"opcua": {"clear_password": True}})
    assert sec == {"username": None, "password": ""}
    with pytest.raises(ValueError):
        plc_source.modbus_read_test("127.0.0.1", 502, 1, list(range(1, 40)))


def test_adopt_valves_keeps_dose():
    a, b = SvSource({"host": "x"}), SvSource({"host": "x"})
    a._valves_acc = {"v_water": [120.0, 3.0]}
    b.adopt_valves(a)
    assert b.take_valves() == {"water_dose": 120, "water_s": 3}


def test_stopped_source_closes_session_and_stays_quiet(sim, pki):
    opcua_client.save_secret("op", "123")
    seen = []
    src = SvSource(opc_source_cfg(sim, sv="Brix"), on_update=lambda sv, st: seen.append(sv))
    before = threading.active_count()
    src.start()
    assert wait(lambda: src.status()["connected"])
    src.stop()
    n = len(seen)
    time.sleep(0.8)
    assert len(seen) == n                                                       # после stop в автомат ничего не уходит
    assert src._client is None and not src._thread.is_alive()
    assert wait(lambda: threading.active_count() <= before, 5)                  # поток asyncua погашен


def test_all_tags_bad_reports_hint(sim, pki):
    opcua_client.save_secret("op", "123")
    src = SvSource(opc_source_cfg(sim, sv="Broken"))
    src.start()
    try:
        assert wait(lambda: src.status()["connected"])
        assert wait(lambda: "ни один тег" in (src.status()["error"] or ""))
    finally:
        src.stop()
