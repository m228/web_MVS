"""Приёмник мониторинга: метка «старая версия» (панель, бот), утренняя сводка в Telegram (только факты), учёт перерывов самого приёмника."""
import base64
import json
import sys
import time
import urllib.request
from pathlib import Path

import pytest

MS = Path(__file__).resolve().parent.parent / "monitor_server"
sys.path.insert(0, str(MS))

import monitor_app  # noqa: E402
import monitor_release as mr  # noqa: E402
import monitor_store as ms  # noqa: E402
import monitor_telegram as mt  # noqa: E402

PANEL = (MS / "panel.html").read_text(encoding="utf-8")
PASSWORD = "p4ssw0rd-Test"


def local(day, hm):
    """Локальное время «2026-11-02», «08:05» -> unix-время."""
    return time.mktime(time.strptime(day + " " + hm, "%Y-%m-%d %H:%M"))


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Chat:
    """Заглушка клиента Telegram: помнит отправленное, может «не доставлять»."""

    def __init__(self):
        self.sent, self.ok = [], True

    def send(self, chat_id, text, markup=None):
        if not self.ok:
            return False, 0.0, "нет связи с Telegram"
        self.sent.append(text)
        return True, 0.0, ""


def pulse(version="2.0.50", text="x", micro=None, alerts=None, events=None):
    snap = {"plant": "Кир", "app_version": version, "interval_s": 300, "text": text, "alerts": alerts or []}
    if micro is not None:
        snap["micro"] = micro
    return {"schema": 1, "sent_t": 0, "snapshot": snap, "events": events or []}


def ev(eid, t, text, kind="alert", level="high", **data):
    return {"eid": eid, "t": t, "kind": kind, "level": level, "text": text, "data": data}


# ------------------------------------------------------------------------------------------------------------- версия


def test_parse_and_compare_versions():
    assert mr.parse_version("v2.0.50") == (2, 0, 50) and mr.parse_version("2.0.7") == (2, 0, 7)
    assert mr.parse_version("dev") is None and mr.parse_version(None) is None and mr.parse_version("2.0") is None
    assert mr.is_older("2.0.43", "2.0.50") and mr.is_older("2.0.9", "2.0.10"), "числа, а не строки: 9 < 10"
    assert not mr.is_older("2.0.50", "2.0.50") and not mr.is_older("2.1.0", "2.0.99")
    assert not mr.is_older("dev", "2.0.50") and not mr.is_older("2.0.43", None), "не разобрали — не утверждаем"


def test_releases_keeps_last_value_when_github_is_unreachable():
    answers = ["2.0.50", OSError("нет сети")]

    def fetch(url):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a
    r = mr.Releases(url="http://x", fetch=fetch)
    assert r.latest is None, "пока не спрашивали — версии нет"
    assert r.refresh() is True and r.latest == "2.0.50"
    assert r.refresh() is False and r.latest == "2.0.50" and "нет сети" in r.error


def test_releases_disabled_with_empty_url():
    r = mr.Releases(url="", fetch=lambda u: "9.9.9")
    assert r.refresh() is False and r.latest is None
    r.start()
    assert r._thread is None


def test_fetch_latest_reads_tag_name(monkeypatch):
    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"tag_name": "v2.0.50"}).encode()
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0: R())
    assert mr.fetch_latest("http://x") == "2.0.50"
    R.read = lambda self: json.dumps({"tag_name": "nightly"}).encode()
    with pytest.raises(ValueError):
        mr.fetch_latest("http://x")


def _api_plants(app):
    import socket
    import threading
    import uvicorn
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        req = urllib.request.Request("http://127.0.0.1:%d/api/plants" % port,
                                     headers={"Authorization": "Basic " + base64.b64encode(("max:" + PASSWORD).encode()).decode()})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=10) as r:
            return json.loads(r.read())
    finally:
        server.should_exit = True
        th.join(timeout=5)


def test_panel_marks_old_version_and_api_gives_latest(tmp_path):
    store = ms.Store(tmp_path / "m.db")
    rel = mr.Releases(url="http://x", fetch=lambda u: "2.0.50")
    rel.refresh()
    assert _api_plants(monitor_app.create_app(store, PASSWORD, "2.0.50", PANEL, watchdog=False, releases=rel))["latest_version"] == "2.0.50"
    assert _api_plants(monitor_app.create_app(store, PASSWORD, "2.0.50", PANEL, watchdog=False))["latest_version"] is None
    assert "latestV = j.latest_version" in PANEL and "verOlder(snap.app_version, latestV)" in PANEL and "есть " in PANEL
    store.close()


def test_bot_reports_old_version_only_when_known(tmp_path):
    clk = Clock(local("2026-11-02", "10:00"))
    store = ms.Store(tmp_path / "m.db", clock=clk)
    pid, _ = store.create_plant("Кир")
    store.ingest(pid, pulse(version="2.0.43"))
    store.ingest(store.create_plant("Буи")[0], pulse(version="2.0.50"))
    plants = store.plants_view()
    kir = next(p for p in plants if p["name"] == "Кир")
    bui = next(p for p in plants if p["name"] == "Буи")
    assert "⚠ Старая версия приложения: 2.0.43, есть 2.0.50" in mt.format_plant(kir, clk.t, "2.0.50").split("\n")
    assert "Старая версия" not in mt.format_plant(kir, clk.t), "последняя версия неизвестна — ничего не утверждаем"
    assert "Старая версия" not in mt.format_plant(bui, clk.t, "2.0.50")
    allv = mt.format_all(plants, clk.t, "2.0.50").split("\n")
    assert any(ln.startswith("🟢 Кир") and ln.endswith("старая версия 2.0.43, есть 2.0.50") for ln in allv)
    assert not any("Буи" in ln and "старая" in ln for ln in allv)
    store.close()


# ------------------------------------------------------------------------------------------- перерывы самого приёмника


def test_restart_after_long_silence_is_recorded_as_outage(tmp_path):
    path = tmp_path / "m.db"
    clk = Clock(1_000_000.0)
    s = ms.Store(path, clock=clk)
    clk.t += 60
    s.touch_alive()
    s.close()
    clk.t += 20                                          # перезапуск за секунды — не перерыв
    s = ms.Store(path, clock=clk)
    assert s.outages(0, 2_000_000) == []
    clk.t += 600
    s.touch_alive()
    s.close()
    clk.t += 3000                                        # молчал 50 минут
    s = ms.Store(path, clock=clk)
    assert s.outages(0, 2_000_000) == [(1_000_080 + 600, 1_000_080 + 600 + 3000)]
    assert s.outages(1_000_000 + 1000, 1_000_000 + 2000) == [(1_001_000, 1_002_000)], "по окну обрезается"
    s.close()


# ----------------------------------------------------------------------------------------------------- утренняя сводка


@pytest.fixture
def env(tmp_path):
    clk = Clock(local("2026-11-02", "07:59"))
    store = ms.Store(tmp_path / "m.db", clock=clk, notify={"fresh_s": 900})
    chat = Chat()
    cfg = dict(mt.DEFAULTS, token="t", chat_ids=[1], api_base="http://x")
    n = mt.Notifier(store, chat, cfg, clock=clk, releases=mr.Releases(url="http://x", fetch=lambda u: "2.0.50"))
    n.releases.refresh()

    class E:
        pass
    e = E()
    e.store, e.clk, e.chat, e.cfg, e.n = store, clk, chat, cfg, n
    e.t0, e.t1 = local("2026-11-01", "20:00"), local("2026-11-02", "08:00")
    yield e
    store.close()


def test_morning_not_before_time_sent_once_and_not_after_window(env):
    e = env
    e.store.ingest(e.store.create_plant("Кир")[0], pulse())
    assert e.n.morning() is False and e.chat.sent == [], "07:59 — рано"
    e.clk.t = e.t1 + 5 * 60
    assert e.n.morning() is True and len(e.chat.sent) == 1
    assert e.n.morning() is False and len(e.chat.sent) == 1, "второй раз за день не шлём"
    e.clk.t = local("2026-11-03", "08:01")
    assert e.n.morning() is True and len(e.chat.sent) == 2, "на следующий день снова"
    e.clk.t = local("2026-11-04", "11:30")
    assert e.n.morning() is False and len(e.chat.sent) == 2, "приёмник включили днём — это не утро"


def test_morning_retries_until_delivered_and_survives_restart(env, tmp_path):
    e = env
    e.store.ingest(e.store.create_plant("Кир")[0], pulse())
    e.clk.t = e.t1 + 60
    e.chat.ok = False
    assert e.n.morning() is False and e.n.state["last_error"]
    e.clk.t += 600
    e.chat.ok = True
    assert e.n.morning() is True and len(e.chat.sent) == 1
    n2 = mt.Notifier(e.store, e.chat, e.cfg, clock=e.clk)             # «перезапуск»: отметка в БД
    assert n2.morning() is False and len(e.chat.sent) == 1


def test_morning_can_be_switched_off_or_moved(env, tmp_path):
    e = env
    e.store.ingest(e.store.create_plant("Кир")[0], pulse())
    e.clk.t = e.t1 + 60
    e.n.cfg["morning"] = None
    assert e.n.morning() is False and e.chat.sent == []
    assert mt.parse_hhmm("07:30") == (7, 30) and mt.parse_hhmm("8:05") == (8, 5)
    assert mt.parse_hhmm("off") is None and mt.parse_hhmm(False) is None and mt.parse_hhmm("25:00") is None and mt.parse_hhmm("") is None
    (tmp_path / "server.json").write_text(json.dumps({"telegram": {"token": "t", "chat_ids": [1]}}), encoding="utf-8")
    assert mt.load_config(tmp_path)["morning"] == (8, 0), "по умолчанию 08:00"
    (tmp_path / "server.json").write_text(json.dumps({"telegram": {"token": "t", "chat_ids": [1], "morning": "07:30"}}), encoding="utf-8")
    assert mt.load_config(tmp_path)["morning"] == (7, 30)
    (tmp_path / "server.json").write_text(json.dumps({"telegram": {"token": "t", "chat_ids": [1], "morning": False}}), encoding="utf-8")
    assert mt.load_config(tmp_path)["morning"] is None


def test_morning_text_has_only_facts_from_the_journal(env):
    e = env
    kir = e.store.create_plant("Кир")[0]
    e.store.create_plant("Буи")
    t = lambda hm, day="2026-11-02": local(day, hm)  # noqa: E731
    e.store.ingest(kir, pulse(version="2.0.43", micro={"probes_today": 56}, events=[
        ev("a1", t("21:10", "2026-11-01"), "Подвод: датчики разошлись на 124 мкм (допуск 50)", level="medium", code="pos_approach", state="open"),
        ev("a2", t("23:40", "2026-11-01"), "Подвод: датчики разошлись на 110 мкм (допуск 50)", level="high", code="pos_approach", state="open"),
        ev("s1", t("06:20"), "Приложение обновлено до v2.0.43", kind="start", level="info"),
        ev("p1", t("06:23"), "Плата микроскопа перезагрузилась", kind="plate_reboot"),
        ev("old", t("15:00", "2026-11-01"), "Старая тревога вне ночного окна", level="high", code="x", state="open"),
    ], alerts=[{"code": "pos_approach", "level": "high", "text": "Подвод: датчики разошлись на 110 мкм", "since": t("21:10", "2026-11-01")}]))
    e.clk.t = e.t1 + 120
    e.n.morning()
    text = e.chat.sent[0]
    lines = text.split("\n")
    assert lines[0] == "☀ Сводка за ночь 20:00–08:00 (02.11)"
    assert "🟢 Кир — в сети, пульс 3 мин назад · версия 2.0.43 (есть 2.0.50)" in lines
    assert "• Связь: обрывов не было" in lines
    assert "• Запуски: 06:20 Приложение обновлено до v2.0.43" in lines
    assert "• Перезагрузок платы: 1 (06:23)" in lines
    assert "• Тревоги за ночь:" in lines
    assert any(ln.strip().startswith("🔴 Подвод: датчики разошлись на 110 мкм (допуск 50) — открытий: 2, первое в 21:10") for ln in lines)
    assert "Старая тревога" not in text, "события вне окна 20:00–08:00 не берём"
    assert any(ln.startswith("• Сейчас открыто: Подвод: датчики разошлись на 110 мкм (с 01.11 21:10)") for ln in lines)
    assert "• Проб с 00:00 до пульса 07:59: 56" in lines
    assert "⚪ Буи — ещё не выходил на связь" in lines and "• нет данных: завод ещё не присылал пульс" in lines
    for verdict in ("всё хорошо", "в норме", "норма", "отлично", "проблем нет"):
        assert verdict not in text.lower(), "сводка перечисляет факты и не ставит оценок"


def test_morning_quiet_night_says_what_was_not_there(env):
    e = env
    e.store.ingest(e.store.create_plant("Кир")[0], pulse(version="2.0.50"))
    e.clk.t = e.t1 + 60
    e.n.morning()
    lines = e.chat.sent[0].split("\n")
    assert "• Связь: обрывов не было" in lines and "• Запуски ПК и приложения: не было" in lines and "• Тревоги за ночь: не было" in lines
    assert "• Сейчас открытых тревог нет" in lines
    assert not any("версия 2.0.50 (есть" in ln for ln in lines), "версия последняя — без пометки"
    assert not any("Проб" in ln for ln in lines), "нет данных о пробах — строку не выдумываем"


def test_morning_says_when_the_receiver_itself_was_down(env):
    e = env
    e.store.ingest(e.store.create_plant("Кир")[0], pulse())
    e.store.db.execute("INSERT INTO outages (start_t, end_t) VALUES (?,?)", (local("2026-11-02", "03:10"), local("2026-11-02", "03:40")))
    e.store.db.commit()
    e.clk.t = e.t1 + 60
    e.n.morning()
    lines = e.chat.sent[0].split("\n")
    assert any(ln.startswith("⚠ Приёмник не работал 03:10–03:40") for ln in lines)
    assert "• Связь: обрывов не зафиксировано" in lines and "• Тревоги за ночь: не зафиксировано" in lines
    assert not any(ln.endswith("не было") for ln in lines), "пока приёмник молчал, «не было» утверждать нельзя"


def test_morning_lists_connection_losses(env):
    e = env
    kir = e.store.create_plant("Кир")[0]
    e.store.ingest(kir, pulse())
    e.store.db.execute("INSERT INTO events (plant_id, eid, t, recv_t, kind, level, text) VALUES (?,?,?,?,?,?,?)",
                       (kir, "off1", local("2026-11-02", "02:10"), local("2026-11-02", "02:10"), "offline", "critical", "Нет пульса уже 12 мин"))
    e.store.db.commit()
    e.clk.t = e.t1 + 60
    e.n.morning()
    assert "• Связь: обрывов 1 (02:10)" in e.chat.sent[0].split("\n")
