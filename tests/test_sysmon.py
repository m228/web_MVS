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
