"""Скрытие варок из отчёта: пробы остаются в журнале, но db.read_rows / отчёт / тренды их не видят; «Вернуть» возвращает;
скрытая варка не дописывается обратно бэкфиллом из cv_results; идущую варку скрыть нельзя.

Без железа и боевой базы (DATA_DIR → tmp, см. conftest.data_dir)."""
import json
import time

import cv_store
import db
import report

BASE = 1_760_000_000
OLD = 3 * 3600        # варки «старые» (законченные): последняя проба давно


def _row(t, sv, cook, stage=7):
    return {"ts": time.strftime(cv_store.TS_FMT, time.localtime(t)), "t": float(t), "sv": sv,
            "stage": stage, "cook_time": cook, "fines_m3": 4.0, "vtot_m3": 1.0}


def _boil(t0, n=6):
    return [_row(t0 + i * 90, 85.0 + i * 0.5, 600 + i * 90) for i in range(n)]


def _seed(serial, rows):
    cv_store._HIST_FILLED.add(serial)          # не лезть в cv_results — только журнал
    db.upsert_many(serial, rows)


def _three_boils(serial="H1"):
    a, b = _boil(BASE), _boil(BASE + OLD)
    now = BASE + 2 * OLD + 1000
    c = _boil(now - 300)                         # идёт: последняя проба моложе BOIL_OPEN_S
    _seed(serial, a + b + c)
    return a, b, c, now + 90 * 5


def test_hidden_rows_not_read_unless_asked(data_dir):
    a, b, c, now = _three_boils()
    hid = db.hide_boil("H1", a[0]["t"], a[-1]["t"], "тест", now)
    assert db.count("H1") == 18                                      # пробы целы
    assert len(db.read_rows("H1")) == 12                             # скрытые не отдаются
    assert len(db.read_rows("H1", with_hidden=True)) == 18           # а по требованию — да
    assert [h["id"] for h in db.hidden_boils("H1")] == [hid]
    assert db.hidden_boils("H1")[0]["n"] == 6 and db.hidden_boils("H1")[0]["reason"] == "тест"
    assert db.hidden_boils("H1", BASE + OLD, BASE + 2 * OLD) == []   # период без начала скрытой варки
    assert db.unhide_boil("H1", hid) and not db.unhide_boil("H1", hid)
    assert len(db.read_rows("H1")) == 18


def test_hide_boils_uses_journal_bounds_and_skips_open(data_dir):
    a, b, c, now = _three_boils()
    res = cv_store.hide_boils("H1", [a[0]["t"], c[0]["t"], 12345.0, "x"], "неправильные подсчёты", now=now)
    assert [(h["t_from"], h["t_to"], h["n"]) for h in res["hidden"]] == [(a[0]["t"], a[-1]["t"], 6)]
    assert sorted(s["why"] for s in res["skipped"]) == sorted(["варка ещё идёт", "варки нет в журнале (уже скрыта?)", "неверное время"])
    groups = cv_store.boil_groups("H1")
    assert [g[0]["t"] for g in groups] == [b[0]["t"], c[0]["t"]]     # первая варка исчезла, остальные на месте
    again = cv_store.hide_boils("H1", [a[0]["t"]], "ещё раз", now=now)
    assert not again["hidden"]                                       # уже скрытую повторно не скрываем


def test_report_and_trend_exclude_hidden(data_dir):
    a, b, c, now = _three_boils()
    cv_store.hide_boils("H1", [a[0]["t"]], "сбой записи проб", now=now)
    rep = report.build("H1", BASE - 10, now)
    assert [x["t_start"] for x in rep["boils"]] == [b[0]["t"], c[0]["t"]]
    assert len(cv_store.boils("H1", now=now)) == 2


def test_backfill_does_not_resurrect_hidden(data_dir, monkeypatch):
    a, b, c, now = _three_boils()
    cv_store.hide_boils("H1", [a[0]["t"]], "тест", now=now)
    sd = cv_store._serial_dir("H1")
    for r in a:                                   # кадры скрытой варки ещё лежат в cv_results
        (sd / r["ts"]).mkdir(parents=True)
        (sd / r["ts"] / "result.json").write_text(json.dumps({"ts": r["ts"]}), encoding="utf-8")
    appended = []
    monkeypatch.setattr(cv_store, "_with_volume", lambda p, r, *x, **k: r)
    monkeypatch.setattr(cv_store, "_hist_append", lambda serial, row: appended.append(row["ts"]))
    monkeypatch.setattr(cv_store, "recompute_journal", lambda *x, **k: 0)
    cv_store._HIST_FILLED.discard("H1")
    cv_store._hist_backfill("H1")
    assert appended == []                         # проба скрытой варки уже в журнале — заново не дописывается


def test_old_foreign_db_without_hidden_table_still_reads(data_dir):
    _three_boils()
    db.conn().execute("DROP TABLE boil_hidden")  # чужая база более старой версии (database/<завод>/mvs.db, только чтение)
    assert len(db.read_rows("H1")) == 18 and db.hidden_boils("H1") == []


def test_api_hide_unhide_roundtrip(data_dir):
    import app as web
    a, b, c, now = _three_boils()
    assert web.cv_boil_hide({"starts": []}, serial="H1")["ok"] is False
    res = web.cv_boil_hide({"starts": [a[0]["t"]], "reason": "неправильные подсчёты"}, serial="H1")
    assert res["ok"] and len(res["hidden"]) == 1
    hid = res["hidden"][0]["id"]
    assert len(db.read_rows("H1")) == 12
    assert web.cv_boil_unhide({"ids": [hid]}, serial="H1") == {"ok": True, "unhidden": [hid]}
    assert web.cv_boil_unhide({"ids": [hid]}, serial="H1")["ok"] is False
    assert len(db.read_rows("H1")) == 18
