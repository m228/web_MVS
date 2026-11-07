"""net_diag: разбор вывода команд (русская и английская Windows), поиск максимального пакета, таблица вердиктов judge(),
проверка больших запросов против настоящего приёмника (/netcheck), предложение исправлений и сохранение отчёта. Без настоящей сети."""
import http.server
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import net_diag as nd  # noqa: E402

PING_EN_OK = "Reply from 172.29.134.237: bytes=32 time=300ms TTL=127"
PING_RU_OK = "Ответ от 172.29.134.237: число байт=32 время=302мс TTL=127"
PING_FRAG_EN = "Packet needs to be fragmented but DF set."
PING_FRAG_RU = "Требуется фрагментация пакета, но установлен запрет фрагментации."
SUBIF = """   MTU  MediaSenseState   Bytes In  Bytes Out  Interface
------  ---------------  ---------  ---------  -------------
4294967295                1          0    5598129  Loopback Pseudo-Interface 1
  9000                1  50367154688  3462135466  Ethernet
  1404                1   22634833   23994310  ZeroTier One [166359304e974d49]
  2800                1    4286552   19331830  ZeroTier One [633e31d8a232164e]
"""
PEERS = """200 peers
<ztaddr>   <ver>  <role> <lat> <link>   <lastTX> <lastRX> <path>
0181a8632a 1.16.2 LEAF    -652 DIRECT   1860     2524     194.60.242.26/56728
56cd5c3b29 1.16.2 LEAF      15 RELAY    7508     7508     85.172.20.218/57862
633e31d8a2 1.16.2 LEAF     302 DIRECT   3902     3902     35.208.98.200/63416
778cde7190 -      PLANET    88 DIRECT   49236    70508    103.195.103.66/9993
"""


def test_ping_ok_any_language():
    assert nd.ping_ok(PING_EN_OK) and nd.ping_ok(PING_RU_OK)
    for bad in (PING_FRAG_EN, PING_FRAG_RU, "Request timed out.", "Превышен интервал ожидания для запроса.", "Reply from 1.2.3.4: TTL expired in transit.", ""):
        assert not nd.ping_ok(bad)


def test_parse_sc_state():
    assert nd.parse_sc_state("SERVICE_NAME: X\n        TYPE               : 10  WIN32_OWN_PROCESS\n        STATE              : 4  RUNNING\n") == "running"
    assert nd.parse_sc_state("        СОСТОЯНИЕ          : 1  STOPPED\n") == "stopped"
    assert nd.parse_sc_state("        STATE              : 2  START_PENDING\n") == "other"
    assert nd.parse_sc_state("[SC] EnumQueryServicesStatus:OpenService: ошибка 1060") is None


def test_parse_subinterfaces_and_adapter_lookup():
    rows = nd.parse_subinterfaces(SUBIF)
    assert {"mtu": 2800, "name": "ZeroTier One [633e31d8a232164e]"} in rows
    assert nd.zt_adapter_for("633e31d8a232164e", rows)["mtu"] == 2800
    assert nd.zt_adapter_for("166359304E974D49", rows)["mtu"] == 1404
    assert nd.zt_adapter_for("ffffffffffffffff", rows) is None


def test_parse_peers():
    assert nd.parse_peers(PEERS) == {"direct": 2, "relay": 1, "planet_latency": 88}
    assert nd.parse_peers("") == {"direct": 0, "relay": 0, "planet_latency": None}


@pytest.mark.parametrize("limit,expected_lo", [(1180, 1172), (1472, 1472), (800, 792)])
def test_find_max_payload(limit, expected_lo):
    ping = lambda host, size=None, df=False, tries=1, wait_ms=1500: (size or 0) <= limit       # noqa: E731
    got = nd.find_max_payload("x", ping)
    assert expected_lo <= got <= limit


def test_find_max_payload_nothing_passes():
    assert nd.find_max_payload("x", lambda *a, **k: False) is None


def test_recommended_mtu():
    assert nd.recommended_mtu(1180) == 1192
    assert nd.recommended_mtu(100) == 576
    assert nd.recommended_mtu(1472) == 1400


# ------------------------------------------------------------------------------------------------------------------ judge()

def net(status="OK", ips=("172.29.134.5",), mtu=2800):
    return {"id": "633e31d8a232164e", "name": "pc len", "status": status, "ips": list(ips), "adapter": "ZeroTier One [633e31d8a232164e]", "mtu": mtu}


def target(**kw):
    t = {"url": "http://172.29.134.237:8769", "host": "172.29.134.237", "port": 8769, "ping": True, "tcp": {"ok": True, "ms": 300, "err": ""},
         "get": {"ok": True, "code": 200, "version": "2.1.40"}, "get_proxy": None, "src_ip": "172.29.134.5", "skew": 1}
    t.update(kw)
    return t


def facts(**kw):
    t = kw.pop("target", target())
    f = {"admin": True, "app_running": True, "cfg": {"url": t["url"], "token_set": True, "enabled": True, "system_proxy": True}, "proxy": "",
         "zt": {"service": "running", "cli": "x", "online": True, "networks": [net()], "peers": {"direct": 3, "relay": 0, "planet_latency": 80}},
         "targets": [t], "primary": t, "reachable": t if (t["tcp"] or {}).get("ok") and (t["get"] or {}).get("ok") else None, "token_code": 400,
         "sizes": {"small_ping": True, "ping_tested": True, "max_payload": 1472, "posts": [{"size": s, "state": "ok", "ms": 300, "err": ""} for s in nd.POST_SIZES]}}
    f.update(kw)
    return f


def titles(f):
    return [v["title"] for v in nd.judge(f)]


def test_judge_all_fine():
    v = nd.judge(facts())
    assert [x["level"] for x in v] == [nd.OK] and "в порядке" in v[0]["title"]


def test_judge_mtu_problem_gives_fix_with_adapter():
    f = facts(sizes={"small_ping": True, "ping_tested": True, "max_payload": 1180,
                     "posts": [{"size": 1024, "state": "ok", "ms": 300, "err": ""}, {"size": 4096, "state": "timeout", "ms": 6000, "err": "таймаут"}]})
    v = nd.judge(f)
    assert v[0]["level"] == nd.FAIL and "MTU" in v[0]["title"]
    assert v[0]["fix"] == {"kind": "mtu", "adapter": "ZeroTier One [633e31d8a232164e]", "mtu": nd.recommended_mtu(1180)}


def test_judge_big_post_fails_but_ping_full_is_not_mtu():
    f = facts(sizes={"small_ping": True, "ping_tested": True, "max_payload": 1472,
                     "posts": [{"size": 1024, "state": "ok", "ms": 1, "err": ""}, {"size": 4096, "state": "timeout", "ms": 6000, "err": "таймаут"}]})
    v = nd.judge(f)
    assert v[0]["level"] == nd.FAIL and "MTU" not in v[0]["title"] and v[0]["fix"] is None


def test_judge_old_receiver_without_netcheck_still_flags_ping_mtu():
    f = facts(sizes={"small_ping": True, "ping_tested": True, "max_payload": 1100, "posts": [{"size": 1024, "state": "unsupported", "ms": 5, "err": ""}]})
    v = nd.judge(f)
    assert any(x["fix"] and x["fix"]["kind"] == "mtu" and x["fix"]["mtu"] == nd.recommended_mtu(1100) for x in v)
    assert not any(x["level"] == nd.OK for x in v)


def test_judge_zerotier_service_stopped_and_missing():
    f = facts(zt={"service": "stopped", "cli": None, "online": None, "networks": [], "peers": None}, reachable=None)
    assert nd.judge(f)[0]["fix"] == {"kind": "start_zt"}
    f = facts(zt={"service": None, "cli": None, "online": None, "networks": [], "peers": None}, reachable=None)
    assert "не установлен" in nd.judge(f)[0]["title"]


def test_judge_access_denied_and_no_address():
    assert "не одобрен" in titles(facts(zt={"service": "running", "cli": "x", "online": True, "networks": [net("ACCESS_DENIED")], "peers": None}))[0]
    assert "не выдан адрес" in " ".join(titles(facts(zt={"service": "running", "cli": "x", "online": True, "networks": [net(ips=())], "peers": None})))


def test_judge_zerotier_offline_offers_restart():
    f = facts(zt={"service": "running", "cli": "x", "online": False, "networks": [net()], "peers": None})
    assert nd.judge(f)[0]["fix"] == {"kind": "restart_zt"}


def test_judge_no_path_via_zerotier_vs_not_via_zerotier():
    t = target(ping=False, tcp={"ok": False, "ms": None, "err": "таймаут"}, get=None)
    assert "ZeroTier" in nd.judge(facts(target=t))[0]["title"] and "НЕ через" not in nd.judge(facts(target=t))[0]["title"]
    t2 = target(ping=False, tcp={"ok": False, "ms": None, "err": "таймаут"}, get=None, src_ip="192.168.0.5")
    assert "НЕ через ZeroTier" in nd.judge(facts(target=t2))[0]["title"]


def test_judge_traffic_bypasses_zerotier_even_when_reachable():
    v = nd.judge(facts(target=target(src_ip="10.0.0.7")))
    assert any("НЕ через ZeroTier" in x["title"] for x in v)


def test_judge_ping_ok_port_closed():
    t = target(tcp={"ok": False, "ms": None, "err": "Connection refused"}, get=None)
    v = nd.judge(facts(target=t))
    assert "порт 8769 закрыт" in v[0]["title"] and "allow_firewall" in v[0]["advice"]


def test_judge_proxy_blocks_receiver():
    t = target(get={"ok": False, "code": None, "err": "x"}, get_proxy={"ok": False, "code": None, "err": "y"})
    f = facts(target=t, proxy="http://proxy:3128")
    assert nd.judge(f)[0]["fix"] == {"kind": "proxy_off"}
    t = target(get={"ok": False, "code": None, "err": "x"}, get_proxy={"ok": True, "code": 200})
    assert nd.judge(facts(target=t))[0]["fix"] == {"kind": "proxy_on"}


def test_judge_token_rejected_clock_skew_and_disabled():
    assert "токен" in nd.judge(facts(token_code=401))[0]["title"]
    assert any("Часы" in x for x in titles(facts(target=target(skew=900))))
    f = facts()
    f["cfg"]["enabled"] = False
    assert any("не настроен" in x for x in titles(f))
    f = facts(app_running=False)
    assert any("не запущено" in x for x in titles(f))


def test_judge_configured_address_wrong_but_default_works():
    bad = target(url="http://1.2.3.4:8769", host="1.2.3.4", ping=False, tcp={"ok": False, "ms": None, "err": "таймаут"}, get=None)
    good = target()
    f = facts(target=bad, targets=[bad, good], reachable=good)
    assert "в настройках" in nd.judge(f)[0]["title"] and good["url"] in nd.judge(f)[0]["advice"]


# ------------------------------------------------------------------------------------- настоящий приёмник и заглушка

def test_check_sizes_against_real_receiver():
    from test_monitor_server import Srv, ms  # noqa: PLC0415  (поднимает настоящий uvicorn с приёмником)
    import tempfile
    store = ms.Store(Path(tempfile.mkdtemp()) / "m.db")
    srv = Srv(store)
    try:
        t = {"url": srv.base, "host": "127.0.0.1", "ping": False}
        assert srv.call("GET", "/netcheck", password=None)[0] == 405                   # по этому признаку клиент отличает новый приёмник от старого
        res = nd.check_sizes(nd.Report(lambda s: None, color=False), t, via_proxy=False)
        assert [p["state"] for p in res["posts"]] == ["ok"] * len(nd.POST_SIZES)
        code, body, _ = srv.call("POST", "/netcheck", password=None, raw=b"x" * 300_000)
        assert code == 413
        assert srv.json("POST", "/netcheck", password=None, raw=b"abc")[1] == {"ok": True, "got": 3}
        # токен: пустой пульс — 400 при верном токене, 401 при неверном
        _, token = store.create_plant("Тест")
        r = nd.check_token(nd.Report(lambda s: None, color=False), srv.base, token, False)
        assert r == 400
        assert nd.check_token(nd.Report(lambda s: None, color=False), srv.base, "wrong-token", False) == 401
        assert nd.check_token(nd.Report(lambda s: None, color=False), srv.base, "неверный", False) is None       # не латиница: не падаем
    finally:
        srv.close()


def test_check_sizes_old_receiver_is_unsupported_and_stops():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(404)
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *a):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        t = {"url": "http://127.0.0.1:%d" % httpd.server_address[1], "host": "127.0.0.1", "ping": False}
        res = nd.check_sizes(nd.Report(lambda s: None, color=False), t, via_proxy=False)
        assert [p["state"] for p in res["posts"]] == ["unsupported"]
    finally:
        httpd.shutdown()


def test_http_timeout_is_reported():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)                                         # принимает подключение в очередь, но не отвечает
    try:
        r = nd._http("http://127.0.0.1:%d/x" % s.getsockname()[1], timeout=0.5)
        assert not r["ok"] and r["err"] == "таймаут"
    finally:
        s.close()


# ----------------------------------------------------------------------------------------------- исправления и отчёт

def test_offer_fixes_asks_and_respects_no(monkeypatch):
    calls = []
    monkeypatch.setattr(nd, "apply_fix", lambda fix: calls.append(fix) or (True, "готово"))
    verdicts = [{"level": nd.FAIL, "title": "a", "advice": "", "fix": {"kind": "mtu", "adapter": "A", "mtu": 1200}},
                {"level": nd.FAIL, "title": "b", "advice": "", "fix": {"kind": "mtu", "adapter": "A", "mtu": 1200}},      # такая же команда — второй раз не спрашиваем
                {"level": nd.FAIL, "title": "c", "advice": "", "fix": {"kind": "restart_zt"}}]
    rep = nd.Report(lambda s: None, color=False)
    answers = iter(["н", "д"])
    assert nd.offer_fixes(rep, verdicts, lambda q: next(answers)) == 1
    assert calls == [{"kind": "restart_zt"}]


def test_offer_fixes_eof_means_no(monkeypatch):
    monkeypatch.setattr(nd, "apply_fix", lambda fix: pytest.fail("без ответа ничего менять нельзя"))

    def eof(q):
        raise EOFError
    assert nd.offer_fixes(nd.Report(lambda s: None, color=False), [{"level": nd.FAIL, "title": "a", "advice": "", "fix": {"kind": "restart_zt"}}], eof) == 0


def test_apply_fix_needs_admin(monkeypatch):
    monkeypatch.setattr(nd, "is_admin", lambda: False)
    ok, text = nd.apply_fix({"kind": "mtu", "adapter": "A", "mtu": 1200})
    assert not ok and "администратор" in text


def test_apply_mtu_builds_netsh_command(monkeypatch):
    seen = []
    monkeypatch.setattr(nd, "is_admin", lambda: True)
    monkeypatch.setattr(nd, "run_cmd", lambda args, **k: seen.append(args) or (0, "Ok."))
    assert nd.apply_fix({"kind": "mtu", "adapter": "ZeroTier One [633e31d8a232164e]", "mtu": 1200}) == (True, "Ok.")
    assert seen[0] == ["netsh", "interface", "ipv4", "set", "subinterface", "ZeroTier One [633e31d8a232164e]", "mtu=1200", "store=persistent"]


def test_main_writes_report_and_exit_code(monkeypatch, tmp_path):
    monkeypatch.setattr(nd, "DATA_DIR", tmp_path)
    monkeypatch.setattr(nd, "collect", lambda rep, cfg=None: facts())
    lines = []
    assert nd.main([], out=lines.append, ask=lambda q: "н") == 0
    files = list(tmp_path.glob("net_diag_*.txt"))
    assert len(files) == 1 and "Связь с приёмником в порядке" in files[0].read_text(encoding="utf-8")
    bad = facts(token_code=401)
    monkeypatch.setattr(nd, "collect", lambda rep, cfg=None: bad)
    assert nd.main([], out=lines.append, ask=lambda q: "н") == 1
