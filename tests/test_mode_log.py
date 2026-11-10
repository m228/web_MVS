"""Журнал режима автомата («тренд Автомат / Ручной»): FSM сообщает смену режима, сервис пишет её в mvs.db вместе с правками цикла,
тренд отдаёт полосу, мониторинг поднимает тревогу «варка идёт, а Автомат выключен».

Без платы и камеры (FakePlate из test_fsm), DATA_DIR → tmp (фикстура data_dir)."""
import time

import db
from test_fsm import fsm  # noqa: F401  (фикстура автомата без платы)


def test_fsm_reports_mode_changes_and_first_tick(fsm):
    got = []
    fsm.on_mode_change = lambda state, stage, first: got.append((state, stage, first))
    fsm.tick()
    assert got == [("off", None, True)]                 # первый такт после запуска: Автомат выключен (сам не включается), стадия ещё не получена
    fsm.set_stage(7)
    fsm.set_cyclic(True)
    fsm.tick()
    fsm.tick()                                           # без изменений — не пишем повторно
    fsm.set_manual(True)
    fsm.tick()
    fsm.set_manual(False)
    fsm.set_cyclic(True)
    fsm.tick()
    assert [g[0] for g in got] == ["off", "auto", "manual", "auto"]
    assert got[1] == ("auto", 7, False) and got[2][2] is False


def test_fsm_fault_has_priority(fsm):
    fsm.set_cyclic(True)
    fsm.tick()
    with fsm._lock:
        fsm._raise_fault("тест")
    assert fsm.mode_state() == "fault"


def test_mode_window_before_events_and_empty(data_dir):
    assert db.mode_window(0, 10) == {"before": None, "events": []}      # записей нет — полоса не рисуется
    t0 = 1_760_000_000.0
    db.mode_add(t0, "start", "off", None, "Запуск приложения: Автомат выключен — проб нет")
    db.mode_add(t0 + 100, "mode", "auto", 3, "Автомат включён")
    db.mode_add(t0 + 150, "setting", None, 3, "Настройки цикла: стадия от 3 → 8")
    db.mode_add(t0 + 900, "mode", "manual", 7, "Ручной режим")
    w = db.mode_window(t0 + 120, t0 + 1000)
    assert w["before"]["state"] == "auto"                                 # состояние на левом краю окна — последнее переключение до него
    assert [e["kind"] for e in w["events"]] == ["setting", "mode"]
    assert [e["state"] for e in db.mode_last(10)][:2] == ["manual", None]  # новые первыми


def test_service_logs_mode_and_cycle_patch(data_dir):
    from microscope_service import MicroscopeService
    svc = MicroscopeService()
    svc.cfg = {"probe_cycle": {"stage_from": 3, "stage_to": 9, "ignore_stage": False, "pause_sec": 1}, "camera_mode": "auto"}
    svc._log_mode("off", None, True)
    svc._log_mode("auto", 3, False)
    svc._log_mode("off", 5, True)                                          # второй «первый такт» — перезапуск автомата после применения настроек
    svc._log_cycle_patch({"probe_cycle": {"stage_from": 8, "stage_to": 9, "ignore_stage": True}, "camera_mode": "stream"})
    svc._log_cycle_patch({"probe_cycle": {"stage_to": 9}})                 # ничего не изменилось — не пишем
    ev = list(reversed(db.mode_last(10)))
    assert [(e["kind"], e["state"]) for e in ev] == [("start", "off"), ("mode", "auto"), ("mode", "off"), ("setting", None)]
    assert ev[0]["text"].startswith("Запуск приложения") and ev[2]["text"].startswith("Автомат перезапущен")
    assert ev[3]["text"] == "Настройки цикла: стадия от 3 сгущение → 8 уваривание, варить без стадии выкл → вкл, режим камеры auto → stream"


def test_trend_response_has_modes(data_dir):
    import app as web
    now = time.time()
    db.mode_add(now - 600, "mode", "off", 4, "Автомат выключен — проб нет")
    res = web.cv_trend(serial="NOSER", series=None, limit=100, t_from=now - 3600, t_to=now)
    assert res["modes"]["events"][0]["state"] == "off" and res["modes"]["before"] is None
    assert web.micro_mode_log(limit=5)["events"][0]["text"].startswith("Автомат выключен")


def test_cycle_patch_uses_stage_map_names_and_extra(data_dir):
    """Чужой контроллер: в журнал стадии пишутся названиями из карты; «плюс» стадии — отдельная строка; правка карты — своя запись."""
    from microscope_service import MicroscopeService
    svc = MicroscopeService()
    custom = {"stage_map": {"profile": "custom", "rows": [{"code": 30, "name": "СГУЩЕНИЕ", "to": 3}, {"code": 70, "name": "ИСТОЩЕНИЕ", "to": 8}]}}
    svc.cfg = {"probe_cycle": {"stage_from": 3, "stage_to": 9}, "sv_source": custom}
    svc._log_cycle_patch({"probe_cycle": {"stage_to": 8, "stage_extra": [20, 21]}})
    t = db.mode_last(1)[0]["text"]
    assert t == "Настройки цикла: стадия до 9 готовность → 8 ИСТОЩЕНИЕ, плюс стадии 20 УНВ → 20 УНВ, 21", t     # было умолчание [20]
    svc._log_cycle_patch({"probe_cycle": {"stage_extra": []}})
    svc.cfg["probe_cycle"]["stage_extra"] = [20, 21]
    svc._log_cycle_patch({"probe_cycle": {"stage_extra": []}})
    assert db.mode_last(1)[0]["text"] == "Настройки цикла: плюс стадии 20 УНВ, 21 → нет"
    svc._log_stage_map_change({"stage_map": {"profile": "ours"}}, custom)
    assert db.mode_last(1)[0]["text"].startswith("Карта стадий изменена: профиль наша → другая")
    n = len(db.mode_last(50))
    svc._log_stage_map_change(custom, custom)                              # ничего не поменялось — не пишем
    assert len(db.mode_last(50)) == n
