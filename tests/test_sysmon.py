"""sysmon — удалённый мониторинг: причина старта, настройки, правила тревог, снимок без секретов, отправка на приёмник (локальный http)."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import sysmon

TOKEN = "SECRET-TOKEN-1234567"


@pytest.fixture
def sm(tmp_path, monkeypatch):
    sysmon.close()
    monkeypatch.setattr(sysmon, "DB_PATH", tmp_path / "sysmon.db")
    monkeypatch.setattr(sysmon, "CONFIG_PATH", tmp_path / "monitor.json")
    yield sysmon
    sysmon.close()


def make_sources(**over):
    src = {
        "cfg": lambda: {"camera_serial": "S1", "cv": {"enabled": True}},
        "micro_on": lambda: True,
        "state": lambda: {"stage": 7, "stage_ok": True, "cyclic": True, "manual": False, "fault": False,
                          "cycle_params": {"trigger_mode": "time"}},
        "status": lambda: {"connected": True},
        "ext": lambda: {"uptime_s": 1000, "temp": 41, "u12v": 12000},
        "cv": lambda: {"state": "ok", "message": ""},
        "services": lambda: {"cv": "healthy", "video": "healthy", "motion": "off"},
        "camera": lambda: {"serial": "S1", "streaming": True, "current": {"exposure_time": 170.0, "exposure_auto": "Off"},
                           "saved": {"exposure_time": 170.0, "exposure_auto": "Off"}},
        "probes": lambda serial, since: {"last_t": time.time(), "today": 12},
    }
    src.update(over)
    return src


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


# ------------------------------------------------------------------------------------------------- старт и причина

def test_start_reasons(sm):
    reasons = [sm.register_start(1000.0, 500.0, "1.0.0")["reason"],
               sm.register_start(2000.0, 500.0, "1.0.0")["reason"],      # то же время загрузки, та же версия
               sm.register_start(3000.0, 505.0, "1.0.1")["reason"],      # разница в пределах допуска, версия новая
               sm.register_start(4000.0, 3900.0, "1.0.1")["reason"]]     # Windows загружена заново
    assert reasons == ["first", "app_restart", "app_update", "pc_reboot"]
    items = sm.starts()
    assert [i["reason"] for i in items] == ["pc_reboot", "app_update", "app_restart", "first"]
    assert items[0]["level"] == "critical" and items[2]["level"] == "critical" and items[1]["level"] == "info"


def test_start_gap_and_clean_stop(sm):
    sm.register_start(1000.0, 100.0, "1.0.0")
    sm.beat(1100.0)
    sm.mark_clean_stop()
    s = sm.register_start(1700.0, 100.0, "1.0.0")
    assert s["clean"] is True and s["gap_s"] == 600
    assert "10 мин" in s["text"] and "нештатная" not in s["text"]
    s = sm.register_start(2000.0, 100.0, "1.0.0")                       # прошлый запуск не отметил штатную остановку
    assert s["clean"] is False and "нештатная" in s["text"]


def test_start_clock_went_back(sm):
    sm.register_start(5000.0, 100.0, "1.0.0")
    s = sm.register_start(1000.0, 100.0, "1.0.0")                       # часы ушли назад — длительность не выдумываем
    assert s["gap_s"] is None


def test_boot_time_is_plausible():
    now = time.time()
    b = sysmon.boot_time(now)
    assert b is None or now - 3 * 365 * 86400 < b <= now


# ------------------------------------------------------------------------------------------------------- настройки

def test_config_defaults_and_validation(sm):
    assert sm.load_config()["enabled"] is False and sm.load_config()["interval_s"] == 300
    cfg = sm.save_config({"enabled": True, "url": "https://mon.example.ru/", "token": TOKEN, "plant": "Кир", "interval_s": 5})
    assert cfg["url"] == "https://mon.example.ru" and cfg["interval_s"] == 60, "хвостовой / снимается, интервал не короче минуты"
    assert sm.save_config({"token": ""})["token"] == TOKEN, "пустой токен в правке = не менять"
    pub = sm.public_config()
    assert "token" not in pub and pub["token_set"] is True and pub["token_tail"] == TOKEN[-4:]
    for ok in ("http://127.0.0.1:9", "http://localhost:8000", "http://192.168.1.50:8000", "https://mon.example.ru"):
        assert sm.validate_url(ok) == ok
    for bad in ("http://example.com", "ftp://x.ru", "mon.example.ru"):
        with pytest.raises(ValueError):
            sm.validate_url(bad)
    with pytest.raises(ValueError):
        sm.save_config({"url": "http://example.com"})
    assert sm.load_config()["url"] == "https://mon.example.ru", "отклонённый адрес файл не меняет"


def test_http_in_closed_network_needs_explicit_switch(sm):
    zt = "http://11.11.11.12:8769"                                      # адрес внутри ZeroTier: формально «белый», но сеть закрытая
    with pytest.raises(ValueError, match="ZeroTier"):
        sm.save_config({"url": zt, "token": TOKEN})
    cfg = sm.save_config({"url": zt, "token": TOKEN, "allow_http": True, "enabled": True})
    assert cfg["url"] == zt and cfg["allow_http"] is True and sm.public_config()["allow_http"] is True
    with pytest.raises(ValueError):
        sm.save_config({"allow_http": False})                           # сняли галочку при http-адресе — сохранить нельзя
    assert sm.load_config()["allow_http"] is True, "отклонённая правка файл не меняет"
    assert sm.validate_url("https://mon.example.ru", False) == "https://mon.example.ru"


def test_closed_network_goes_direct_not_through_proxy(sm, monkeypatch):
    for url, allow, expect in (("http://11.11.11.12:8769", True, True), ("http://11.11.11.12:8769", False, False),
                               ("http://192.168.1.5:8769", False, True), ("http://127.0.0.1:8769", False, True),
                               ("https://monitor.example.ru", False, False), ("https://monitor.example.ru", True, False),
                               ("https://11.11.11.12", False, False), ("http://localhost:8769", False, True)):
        assert sm.closed_network(url, allow) is expect, (url, allow)
    seen = []
    monkeypatch.setattr(sm, "_post", lambda url, token, body, via_proxy=True, timeout=10: (seen.append(via_proxy) or True, 200, ""))
    sm.save_config({"enabled": True, "url": "http://11.11.11.12:8769", "token": TOKEN, "allow_http": True, "system_proxy": True})
    ag = sm.Agent(sources=make_sources(), clock=Clock())
    assert ag.send()["ok"] is True and seen == [False], "ZeroTier-адрес: напрямую, как бы ни был настроен системный прокси"
    sm.save_config({"url": "https://monitor.example.ru", "allow_http": False})
    ag.send()
    assert seen == [False, True], "HTTPS-домен: через системный прокси, как и раньше"


def test_send_refuses_public_http_even_if_file_edited_by_hand(sm):
    sm.CONFIG_PATH.write_text(json.dumps({"enabled": True, "url": "http://example.com", "token": TOKEN}), encoding="utf-8")
    res = sm.Agent(sources=make_sources(), clock=Clock()).send()
    assert res["ok"] is False and "https" in res["error"], "токен по открытому http в интернет не уходит"


def test_config_survives_broken_file(sm):
    sm.CONFIG_PATH.write_text("{не json", encoding="utf-8")
    assert sm.load_config()["interval_s"] == 300


# ------------------------------------------------------------------------------------------------------------ тревоги

def test_camera_exposure_alert_open_and_close(sm):
    cam = {"cur": 5000.0}
    src = make_sources(camera=lambda: {"serial": "S1", "streaming": True, "current": {"exposure_time": cam["cur"], "exposure_auto": "Off"},
                                       "saved": {"exposure_time": 170.0, "exposure_auto": "Off"}})
    ag = sm.Agent(sources=src, clock=Clock())
    assert ag.tick(1.0) == [], "с первой проверки тревога не открывается (подтверждение)"
    ev = ag.tick(16.0)
    assert [e["data"]["code"] for e in ev] == ["camera_exposure"] and ev[0]["level"] == "high"
    assert "экспозиция 5000 вместо 170" in ev[0]["text"]
    cam["cur"] = 170.0
    ev = ag.tick(31.0)
    assert ev and ev[0]["data"]["state"] == "closed" and ev[0]["text"].startswith("Снято:")
    assert ag.active == {}


def test_camera_not_streaming_keeps_alert(sm):
    state = {"streaming": True}
    src = make_sources(camera=lambda: {"serial": "S1", "streaming": state["streaming"],
                                       "current": {"exposure_time": 5000.0, "exposure_auto": "Off"} if state["streaming"] else None,
                                       "saved": {"exposure_time": 170.0, "exposure_auto": "Off"}})
    ag = sm.Agent(sources=src, clock=Clock())
    ag.tick(1.0)
    ag.tick(16.0)
    assert "camera_exposure" in ag.active
    state["streaming"] = False
    assert ag.tick(31.0) == [] and "camera_exposure" in ag.active, "камера закрыта — неизвестно, тревога не снимается"


def test_overexposed_and_service_alerts(sm):
    src = make_sources(cv=lambda: {"state": "overexposed", "message": "x"}, services=lambda: {"cv": "gave_up"})
    ag = sm.Agent(sources=src, clock=Clock())
    ev = ag.tick(1.0)
    assert [e["data"]["code"] for e in ev] == ["overexposed"], "сервис подтверждается со второй проверки"
    ev = ag.tick(16.0)
    assert [e["data"]["code"] for e in ev] == ["service_cv"]
    assert {a["code"] for a in ag.snapshot()["alerts"]} == {"overexposed", "service_cv"}


def test_overexposed_ignored_when_no_varka(sm):
    st = {"stage": 11, "stage_ok": False, "cyclic": True}
    ag = sm.Agent(sources=make_sources(state=lambda: st, cv=lambda: {"state": "overexposed", "message": "x"}), clock=Clock())
    assert ag.tick(1.0) == [], "вне варки старый статус пересвета тревогой не считается"


def test_plate_down_needs_four_checks(sm):
    ag = sm.Agent(sources=make_sources(status=lambda: {"connected": False}), clock=Clock())
    assert [ag.tick(float(i)) for i in range(3)] == [[], [], []]
    ev = ag.tick(4.0)
    assert [e["data"]["code"] for e in ev] == ["plate_down"]


def test_fault_alert(sm):
    ag = sm.Agent(sources=make_sources(state=lambda: {"stage": 7, "stage_ok": True, "fault": True, "fault_msg": "подвод не сошёлся"}),
                  clock=Clock())
    ev = ag.tick(1.0)
    assert ev[0]["data"]["code"] == "fault" and "подвод не сошёлся" in ev[0]["text"] and ev[0]["level"] == "high"


def test_no_probes_rule(sm):
    clk = Clock(10_000.0)
    probes = {"v": {"last_t": 1.0, "today": 0}}
    ag = sm.Agent(sources=make_sources(probes=lambda s, since: probes["v"]), clock=clk)
    ag._cfg = dict(ag._cfg, no_probe_min=30)
    assert ag.tick(clk.t) == [], "условия сложились только что — ждём"
    clk.t += 29 * 60
    assert ag.tick(clk.t) == []
    clk.t += 2 * 60
    ev = ag.tick(clk.t)
    assert ev and ev[0]["data"]["code"] == "no_probes" and "31 мин" in ev[0]["text"]
    probes["v"] = {"last_t": clk.t, "today": 1}                         # проба пошла — тревога снята
    ev = ag.tick(clk.t + 15)
    assert ev and ev[0]["data"]["state"] == "closed"


def test_no_probes_unknown_when_journal_fails(sm):
    def broken(serial, since):
        raise RuntimeError("db locked")
    clk = Clock(10_000.0)
    ag = sm.Agent(sources=make_sources(probes=broken), clock=clk)
    ag._cfg = dict(ag._cfg, no_probe_min=5)
    ag.tick(clk.t)
    clk.t += 600
    assert ag.tick(clk.t) == [] and ag.last_data["errors"], "журнал недоступен — не тревога, а запись в errors"


def test_no_probes_not_when_manual_or_waiting_sv(sm):
    clk = Clock(10_000.0)
    st = {"stage": 4, "stage_ok": True, "cyclic": True, "manual": True, "sv": 80, "cycle_params": {"trigger_mode": "sv", "sv_from": 84, "sv_to": 92}}
    ag = sm.Agent(sources=make_sources(state=lambda: st, probes=lambda s, since: {"last_t": 1.0, "today": 0}), clock=clk)
    ag._cfg = dict(ag._cfg, no_probe_min=5)
    ag.tick(clk.t)
    clk.t += 3600
    assert ag.tick(clk.t) == [], "ручной режим"
    st["manual"] = False
    clk.t += 15
    ag.tick(clk.t)
    clk.t += 3600
    assert ag.tick(clk.t) == [], "СВ 80 ниже диапазона 84–92 — проба и не должна идти"


def test_plate_reboot_event(sm):
    up = {"v": 5000}
    ag = sm.Agent(sources=make_sources(ext=lambda: {"uptime_s": up["v"]}), clock=Clock())
    assert ag.tick(1.0) == []
    up["v"] = 5015
    assert ag.tick(16.0) == []
    up["v"] = 20
    ev = ag.tick(31.0)
    assert ev and ev[0]["kind"] == "plate_reboot" and ev[0]["level"] == "high"


def test_source_failure_does_not_break_pulse(sm):
    def boom():
        raise RuntimeError("плата отвалилась")
    ag = sm.Agent(sources=make_sources(state=boom, ext=boom), clock=Clock())
    ag.tick(1.0)
    snap = ag.snapshot()
    assert snap["errors"] and "text" in snap


# ------------------------------------------------------------------------------------------------------------ снимок

def test_snapshot_text_and_no_secrets(sm):
    sm.save_config({"enabled": True, "url": "http://127.0.0.1:1", "token": TOKEN, "plant": "Кир"})
    ag = sm.Agent(sources=make_sources(cv=lambda: {"state": "no_camera", "message": "rtsp://admin:hunter2@10.0.0.5/stream не открылся"}),
                  clock=Clock())
    ag.tick(1.0)
    snap = ag.snapshot()
    blob = json.dumps(snap, ensure_ascii=False)
    assert TOKEN not in blob and "hunter2" not in blob, "ни токена, ни пароля камеры в снимке"
    assert snap["plant"] == "Кир" and snap["micro"]["stage"] == 7 and snap["micro"]["probes_today"] == 12
    assert snap["micro"]["plate_12v"] == 12.0 and snap["services"]["cv"] == "ok"
    assert snap["text"].splitlines()[0].startswith("Кир · ПК работает")
    assert "стадия 7 Рост" in snap["text"] and "Тревоги:" in snap["text"]


def test_snapshot_without_microscope(sm):
    ag = sm.Agent(sources=make_sources(micro_on=lambda: False), clock=Clock())
    snap = ag.snapshot()
    assert "micro" not in snap and snap["camera"]["set"] is True


def test_fmt_dur():
    assert sysmon.fmt_dur(45) == "45 с" and sysmon.fmt_dur(7 * 60) == "7 мин"
    assert sysmon.fmt_dur(2 * 3600 + 15 * 60) == "2 ч 15 мин" and sysmon.fmt_dur(3 * 86400 + 4 * 3600) == "3 д 4 ч"


# ------------------------------------------------------------------------------------------------------ outbox и отправка

def test_outbox_roundtrip_and_cap(sm, monkeypatch):
    e1 = sm.outbox_add("start", "critical", "ПК перезагрузился rtsp://a:pw@h/x", {"k": 1})
    assert "pw" not in e1["text"]
    assert [e["eid"] for e in sm.outbox_batch()] == [e1["eid"]]
    sm.outbox_done([e1["eid"]])
    assert sm.outbox_count() == 0
    monkeypatch.setattr(sm, "OUTBOX_MAX", 20)
    for i in range(35):
        sm.outbox_add("alert", "info", "e%d" % i)
    assert sm.outbox_count() == 20 and sm.outbox_batch()[0]["text"] == "e15", "старые отбрасываются"


class Receiver:
    """Фейковый приёмник: пишет запросы, отвечает заданным кодом."""

    def __init__(self):
        self.code = 200
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
                outer.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                self.send_response(outer.code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def rx():
    r = Receiver()
    yield r
    r.close()


def test_send_delivers_snapshot_and_events_once(sm, rx):
    sm.save_config({"enabled": True, "url": rx.url, "token": TOKEN, "plant": "Буи", "system_proxy": False})
    ag = sm.Agent(sources=make_sources(), clock=Clock())
    ag._emit("alert", "high", "Камера: экспозиция 5000 вместо 170", {"code": "camera_exposure", "state": "open"})
    res = ag.send()
    assert res["ok"] is True and res["sent_events"] == 1
    r = rx.requests[0]
    assert r["path"] == "/ingest" and r["auth"] == "Bearer " + TOKEN
    assert r["body"]["schema"] == 1 and r["body"]["snapshot"]["plant"] == "Буи" and len(r["body"]["events"]) == 1
    assert TOKEN not in json.dumps(r["body"], ensure_ascii=False)
    assert sm.outbox_count() == 0
    assert ag.send()["sent_events"] == 0 and len(rx.requests[1]["body"]["events"]) == 0, "доставленное повторно не шлётся"


def test_send_failure_keeps_events_and_retries_with_same_eid(sm, rx):
    sm.save_config({"enabled": True, "url": rx.url, "token": TOKEN, "system_proxy": False})
    clk = Clock()
    ag = sm.Agent(sources=make_sources(), clock=clk)
    ag._emit("start", "critical", "ПК перезагрузился")
    rx.code = 500
    res = ag.send()
    assert res["ok"] is False and "500" in res["error"] and sm.outbox_count() == 1
    assert ag.status["ok"] is False and ag.status["fails"] == 1
    assert ag._next_send - clk.t == sm.RETRY_S[0], "после неудачи повтор по короткой паузе"
    rx.code = 200
    assert ag.send()["ok"] is True and sm.outbox_count() == 0
    assert rx.requests[0]["body"]["events"][0]["eid"] == rx.requests[1]["body"]["events"][0]["eid"], "id события не меняется — сервер отсекает дубль"
    assert ag.status["ok"] is True and ag.status["fails"] == 0


def test_shorter_interval_pulls_next_send_closer(sm, rx):
    sm.save_config({"enabled": True, "url": rx.url, "token": TOKEN, "interval_s": 900, "system_proxy": False})
    clk = Clock()
    ag = sm.Agent(sources=make_sources(), clock=clk)
    ag.send()
    assert ag._next_send == clk.t + 900
    sm.save_config({"interval_s": 120})
    ag._cfg = sm.load_config()
    ag._sync_schedule()
    assert ag._next_send == clk.t + 120
    sm.save_config({"interval_s": 1800})
    ag._cfg = sm.load_config()
    ag._sync_schedule()
    assert ag._next_send == clk.t + 120, "удлинение интервала уже назначенный срок не двигает"


def test_send_bad_token_message(sm, rx):
    sm.save_config({"enabled": True, "url": rx.url, "token": TOKEN, "system_proxy": False})
    rx.code = 401
    res = sm.Agent(sources=make_sources(), clock=Clock()).send()
    assert res["ok"] is False and "токен" in res["error"] and res["http"] == 401


def test_send_unreachable_server(sm):
    sm.save_config({"enabled": True, "url": "http://127.0.0.1:1", "token": TOKEN, "system_proxy": False})
    ag = sm.Agent(sources=make_sources(), clock=Clock())
    res = ag.send()
    assert res["ok"] is False and res["error"].startswith("Нет связи") and res["http"] is None


def test_send_requires_settings_and_force(sm, rx):
    ag = sm.Agent(sources=make_sources(), clock=Clock())
    assert "не заданы" in ag.send()["error"].lower()
    sm.save_config({"enabled": False, "url": rx.url, "token": TOKEN, "system_proxy": False})
    assert ag.send()["ok"] is False and not rx.requests, "отправка выключена — ничего не уходит"
    assert ag.send(force=True)["ok"] is True and len(rx.requests) == 1, "«Проверить связь» шлёт и при выключенной галочке"


def test_emit_goes_to_outbox_only_when_configured(sm):
    ag = sm.Agent(sources=make_sources(), clock=Clock())
    ag._emit("alert", "high", "без настроек")
    assert sm.outbox_count() == 0 and len(ag.recent) == 1, "не настроено — копить в очередь незачем, но локальный журнал ведём"
    sm.save_config({"enabled": True, "url": "http://127.0.0.1:1", "token": TOKEN})
    ag._cfg = sm.load_config()
    ag._emit("alert", "high", "с настройками")
    assert sm.outbox_count() == 1 and ag._kick.is_set(), "срочное событие будит отправку"
    ag._kick.clear()
    ag._emit("alert", "info", "снято")
    assert not ag._kick.is_set(), "обычное событие ждёт пульса"


def test_state_for_page(sm):
    ag = sm.Agent(sources=make_sources(), clock=Clock())
    ag.start_info = sm.register_start(ag.started_at, ag.boot, "1.0.0")
    st = ag.state()
    assert set(st) >= {"snapshot", "alerts", "send", "config", "recent", "starts"}
    assert "token" not in st["config"] and st["send"]["configured"] is False


# ------------------------------------------------------------------------------------- датчики: положения и автокалибровка

def pos(step="подвод", t=1_000_000.0, target=400, calc=401, enc=399, ai=405, timeout=False):
    return {"t": t, "step": step, "target": target, "calc": calc, "enc": enc, "ai": ai, "timeout": timeout, "sv": 88.0, "stage": 7}


def configured_agent(sm, rx_url="http://127.0.0.1:1", **src):
    sm.save_config({"enabled": True, "url": rx_url, "token": TOKEN, "system_proxy": False})
    ag = sm.Agent(sources=make_sources(**src), clock=Clock())
    ag._cfg = sm.load_config()
    return ag


def test_position_verdict(sm):
    cfg = sm.load_config()
    assert sm.position_verdict(pos("подвод", ai=420), cfg) == {"gap": 21, "ok": True, "why": ""}
    bad = sm.position_verdict(pos("подвод", ai=470), cfg)                       # 71 мкм на подводе — допуск 50
    assert bad["ok"] is False and bad["gap"] == 71 and "разошлись на 71" in bad["why"]
    assert sm.position_verdict(pos("отвод", ai=470), cfg)["ok"] is True, "на отводе допуск 100"
    assert sm.position_verdict(pos("отвод", ai=520), cfg)["ok"] is False
    to = sm.position_verdict(pos("подвод", timeout=True), cfg)
    assert to["ok"] is False and "не доехал" in to["why"]
    assert sm.position_verdict(pos(ai=None, calc=401, enc=399), cfg) == {"gap": 2, "ok": True, "why": ""}, "нет аналогового — расчётная против энкодера"
    assert sm.position_verdict(pos(ai=None, calc=None, enc=None), cfg)["ok"] is True, "нет значений — не придираемся"


def test_position_config_has_defaults_and_is_clamped(sm):
    cfg = sm.load_config()
    assert (cfg["gap_far_um"], cfg["gap_near_um"], cfg["drift_um"], cfg["overdue_h"], cfg["bad_streak"]) == (100, 50, 100, 24, 2)
    sm.CONFIG_PATH.write_text(json.dumps({"gap_near_um": 1, "bad_streak": 99, "overdue_h": "x"}), encoding="utf-8")
    cfg = sm.load_config()
    assert cfg["gap_near_um"] == 10 and cfg["bad_streak"] == 10 and cfg["overdue_h"] == 24


def test_bad_probe_is_yellow_and_sent_at_once_then_red_on_repeat_then_cleared(sm):
    ag = configured_agent(sm)
    ag.add_positions(pos("подвод", ai=500))                                     # расхождение 101 > 50
    ev = ag.tick(ag.clock())
    assert [(e["data"]["code"], e["level"]) for e in ev] == [("pos_approach", "medium")]
    assert "Подвод: датчики разошлись" in ev[0]["text"]
    assert ag._kick.is_set(), "жёлтое предупреждение не срочное, но уходит сразу"
    ag._kick.clear()
    assert ag.tick(ag.clock()) == [], "ничего нового — без повторов"
    ag.add_positions(pos("подвод", ai=500))                                     # вторая плохая подряд
    ev = ag.tick(ag.clock())
    assert [(e["data"]["code"], e["level"]) for e in ev] == [("pos_approach", "high")]
    assert "проб подряд: 2" in ev[0]["text"] and ag._kick.is_set()
    ag.add_positions(pos("подвод"))                                             # хорошая проба снимает
    ev = ag.tick(ag.clock())
    assert [(e["data"]["code"], e["data"]["state"]) for e in ev] == [("pos_approach", "closed")]
    assert ag.active == {}


def test_retract_and_approach_are_counted_separately(sm):
    ag = configured_agent(sm)
    ag.add_positions(pos("отвод", target=20000, calc=20001, enc=20002, ai=20300))   # 300 мкм > 100
    ag.add_positions(pos("подвод"))                                                 # хорошая проба подвода не снимает тревогу отвода
    ev = ag.tick(ag.clock())
    assert [(e["data"]["code"], e["level"]) for e in ev] == [("pos_retract", "medium")]
    ag.add_positions(pos("подвод", timeout=True))                                   # другой шаг — своя серия, красной ещё нет
    ev = ag.tick(ag.clock())
    assert [(e["data"]["code"], e["level"]) for e in ev] == [("pos_approach", "medium")]
    assert ag.active["pos_retract"]["level"] == "medium"


def test_old_bad_probe_does_not_keep_alert(sm):
    ag = configured_agent(sm)
    ag.add_positions(pos("подвод", ai=500))
    ag.tick(ag.clock())
    assert "pos_approach" in ag.active
    ev = ag.tick(ag.clock() + sm.POS_STALE_S + 60)
    assert [e["data"]["state"] for e in ev] == ["closed"], "через несколько часов без проб плохая проба уже не держит тревогу"


def test_bad_streak_from_config(sm):
    sm.save_config({"bad_streak": 1})
    ag = configured_agent(sm)
    ag.add_positions(pos("подвод", ai=500))
    ev = ag.tick(ag.clock())
    assert ev[0]["level"] == "high", "bad_streak=1 — красная с первой плохой пробы"


def test_good_probes_raise_no_alert(sm):
    ag = configured_agent(sm)
    ag.add_positions(pos("отвод", target=20000, calc=20001, enc=20002, ai=20010))
    ag.add_positions(pos("подвод"))
    assert ag.tick(ag.clock()) == []


def test_positions_ride_with_pulse_and_do_not_wake_sender(sm, rx):
    ag = configured_agent(sm, rx.url)
    ag.add_positions(pos("подвод"))
    assert not ag._kick.is_set(), "хорошая проба не будит отправку, уйдёт с ближайшим пульсом"
    assert ag.send()["ok"] is True
    evs = rx.requests[0]["body"]["events"]
    assert [e["kind"] for e in evs] == ["positions"] and evs[0]["level"] == "info"
    d = evs[0]["data"]
    assert d["step"] == "подвод" and d["ok"] is True and d["gap"] == 6 and d["enc"] == 399 and d["target"] == 400
    assert "Подвод: цель 400" in evs[0]["text"]


def test_positions_not_queued_when_not_configured(sm):
    ag = sm.Agent(sources=make_sources(), clock=Clock())
    ag.add_positions(pos())
    assert sm.outbox_count() == 0 and len(ag.positions) == 1, "не настроено — в очередь не копим, но для правил помним"


def test_positions_do_not_push_out_important_events(sm, monkeypatch):
    monkeypatch.setattr(sm, "POS_KEEP", 5)
    ag = configured_agent(sm)
    ag._emit("start", "critical", "ПК перезагрузился")
    for i in range(12):
        ag.add_positions(pos(t=1_000_000.0 + i))
    kinds = [e["kind"] for e in sm.outbox_batch()]
    assert kinds.count("positions") == 5 and kinds.count("start") == 1


def test_add_positions_ignores_garbage(sm):
    ag = configured_agent(sm)
    ag.add_positions(None)
    ag.add_positions({"step": "возврат"})
    ag.add_positions("x")
    assert len(ag.positions) == 0 and sm.outbox_count() == 0


def test_record_positions_without_agent_is_silent(sm, monkeypatch):
    monkeypatch.setattr(sm, "_agent", None)
    sm.record_positions(pos())


def test_record_positions_reaches_agent(sm, monkeypatch):
    ag = configured_agent(sm)
    monkeypatch.setattr(sm, "_agent", ag)
    sm.record_positions(pos())
    assert len(ag.positions) == 1


def autocal_state(**ac):
    base = {"enabled": True, "every_n": 1, "count": 0, "failed": False, "fail_msg": "", "result": "готово"}
    base.update(ac)
    return lambda: {"stage": 7, "stage_ok": True, "cyclic": True, "manual": False, "fault": False, "cycle_params": {"trigger_mode": "time"},
                    "autocal": base}


def iso(t):
    from datetime import datetime
    return datetime.fromtimestamp(t).isoformat(timespec="seconds")


def ac_ev(events):
    return [e for e in events if str(e["data"].get("code", "")).startswith("autocal")]


def test_autocal_failed_is_red_at_once(sm):
    ag = configured_agent(sm, state=autocal_state(failed=True, fail_msg="автокалибровка не удалась: датчик не сошёлся"))
    ev = ac_ev(ag.tick(ag.clock()))
    assert [(e["data"]["code"], e["level"]) for e in ev] == [("autocal_failed", "high")]
    assert "датчик не сошёлся" in ev[0]["text"] and ag._kick.is_set()


def test_autocal_drift_yellow_then_red_on_second_calibration(sm):
    now = time.time()
    last = {"v": {"ts": iso(now - 600), "result": "готово", "diff_before": 150, "diff_after": 3}}

    def state():
        return autocal_state(last=last["v"])()
    ag = configured_agent(sm, state=state)
    ag.clock = Clock(now)
    ev = ac_ev(ag.tick(now))
    assert [(e["data"]["code"], e["level"]) for e in ev] == [("autocal_drift", "medium")]
    assert "150 мкм" in ev[0]["text"] and ag._kick.is_set(), "«ноль уполз» — жёлтое, уходит сразу"
    assert ac_ev(ag.tick(now + 15)) == [], "та же калибровка не считается дважды"
    last["v"] = {"ts": iso(now + 3600), "result": "готово", "diff_before": -130, "diff_after": 2}
    ev = ac_ev(ag.tick(now + 3700))
    assert [(e["data"]["code"], e["level"]) for e in ev] == [("autocal_drift", "high")], "второй раз подряд — красная"
    last["v"] = {"ts": iso(now + 7200), "result": "готово", "diff_before": 12, "diff_after": 1}
    ev = ac_ev(ag.tick(now + 7300))
    assert [e["data"]["state"] for e in ev] == ["closed"]


def test_autocal_small_drift_is_quiet_and_old_result_ignored(sm):
    now = time.time()
    ag = configured_agent(sm, state=autocal_state(last={"ts": iso(now - 300), "result": "готово", "diff_before": 40, "diff_after": 1}))
    assert ac_ev(ag.tick(now)) == []
    ag2 = configured_agent(sm, state=autocal_state(last={"ts": iso(now - 3 * 86400), "result": "готово", "diff_before": 900, "diff_after": 1}))
    assert ac_ev(ag2.tick(now)) == [], "результат трёхдневной давности — не повод"


def test_autocal_overdue_counts_from_start_of_work(sm):
    now = time.time()
    old = {"ts": iso(now - 5 * 86400), "result": "готово", "diff_before": 5, "diff_after": 1}
    ag = configured_agent(sm, state=autocal_state(last=old))
    assert ac_ev(ag.tick(now)) == [], "варка только началась — отсчёт с этого момента, не с прошлой калибровки"
    ev = ac_ev(ag.tick(now + 25 * 3600))
    assert [(e["data"]["code"], e["level"]) for e in ev] == [("autocal_overdue", "medium")]
    assert "успешной не было уже 1 д" in ev[0]["text"]


def test_autocal_overdue_off_when_disabled_or_idle(sm):
    now = time.time()
    ag = configured_agent(sm, state=autocal_state(enabled=False))
    ag.tick(now)
    assert ac_ev(ag.tick(now + 30 * 3600)) == []
    idle = lambda: {"stage": 1, "stage_ok": False, "cyclic": False, "manual": False, "fault": False,
                    "autocal": {"enabled": True, "failed": False}}
    ag2 = configured_agent(sm, state=idle)
    ag2.tick(now)
    assert ac_ev(ag2.tick(now + 30 * 3600)) == [], "аппарат стоит — калибровки и не ждём"


def test_snapshot_has_autocal_and_positions_text(sm):
    now = time.time()
    ag = configured_agent(sm, state=autocal_state(last={"ts": iso(now - 7200), "result": "готово", "diff_before": 12, "diff_after": 1}))
    ag.clock = Clock(now)
    ag.add_positions(pos("отвод", t=now, target=20000, calc=20001, enc=20002, ai=20012))
    ag.add_positions(pos("подвод", t=now))
    snap = ag.snapshot()
    m = snap["micro"]
    assert m["autocal"]["diff_before"] == 12 and m["autocal"]["failed"] is False and m["autocal"]["last_result"] == "готово"
    assert set(m["positions"]) == {"retract", "approach"} and m["positions"]["approach"]["ok"] is True
    assert "Автокалибровка: готово, 2 ч 0 мин назад, ноль уползал на 12 мкм" in snap["text"]
    assert "отвод и подвод в норме, расхождение датчиков до 11 мкм" in snap["text"].lower()
    ag.add_positions(pos("подвод", t=now, ai=500))
    ag.tick(now)
    text = ag.snapshot()["text"]
    assert "в норме" not in text and "Подвод: датчики разошлись" in text, "плохая проба: строка «в норме» уходит, остаётся тревога"


def test_snapshot_without_autocal_state_still_works(sm):
    ag = sm.Agent(sources=make_sources(), clock=Clock())
    m = ag.snapshot()["micro"]
    assert m["autocal"] is None and m["positions"] == {}


# ------------------------------------------------------------------------------------------------ токен: неверный не сохраняем

REAL_TOKEN = "Xk3_9aQ-LmP7vT2uYb8NcZ4wRhJd5sEg"            # как выдаёт панель: 32 символа, есть «-» и «_»


def test_token_problem_catches_typical_copy_mistakes(sm):
    assert sm.token_problem(REAL_TOKEN) == "" and sm.token_problem(TOKEN) == ""
    addr = sm.token_problem("http://192.168.2.198:8769")
    assert "похоже на адрес" in addr and "Ctrl+A" in addr
    assert "похоже на адрес" in sm.token_problem("192.168.2.198:8769"), "адрес без http:// тоже"
    assert "слишком короткий (3 симв." in sm.token_problem("kir")
    assert "слишком короткий" in sm.token_problem(REAL_TOKEN.split("-")[0]), "обрубок после двойного щелчка"
    assert "лишние символы" in sm.token_problem(REAL_TOKEN + " " + REAL_TOKEN)
    assert "слишком короткий (0" in sm.token_problem("")


def test_save_config_refuses_bad_token_and_keeps_old(sm):
    sm.save_config({"url": "https://mon.example.ru", "token": REAL_TOKEN})
    for bad in ("http://192.168.2.198:8769", "kir", REAL_TOKEN[:9]):
        with pytest.raises(ValueError, match="Токен неверный"):
            sm.save_config({"token": bad})
    assert sm.load_config()["token"] == REAL_TOKEN, "неверный токен не должен затирать рабочий"
    assert sm.save_config({"token": "  "})["token"] == REAL_TOKEN, "пусто = не менять"
    assert sm.save_config({"token": " " + REAL_TOKEN[::-1] + " "})["token"] == REAL_TOKEN[::-1], "пробелы по краям срезаются"


def test_public_config_shows_token_length_and_tail_not_token(sm):
    sm.save_config({"url": "https://mon.example.ru", "token": REAL_TOKEN})
    pub = sm.public_config()
    assert pub["token_set"] is True and pub["token_len"] == 32 and pub["token_tail"] == REAL_TOKEN[-4:] and "token" not in pub
    sm.CONFIG_PATH.write_text(json.dumps({"url": "https://mon.example.ru", "token": "http://192.168.2.198:8769"}), encoding="utf-8")
    pub = sm.public_config()
    assert pub["token_len"] == 25 and pub["token_tail"] == "8769", "по хвосту «8769» видно, что в поле попал адрес"


def test_send_with_garbage_saved_token_says_why_and_does_not_hammer(sm, rx):
    sm.CONFIG_PATH.write_text(json.dumps({"enabled": True, "url": rx.url, "token": "http://192.168.2.198:8769", "system_proxy": False}),
                              encoding="utf-8")
    clk = Clock()
    ag = sm.Agent(sources=make_sources(), clock=clk)
    res = ag.send(force=True)
    assert res["ok"] is False and "похоже на адрес" in res["error"] and not rx.requests, "на сервер с таким токеном даже не ходим"
    assert "похоже на адрес" in ag.status["error"] and ag.status["ok"] is False
    assert ag._next_send >= clk.t + 60, "следующая попытка не через 5 секунд: без бесконечного цикла"
