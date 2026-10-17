"""Журнал проб в SQLite (db.py): нарезка на варки, порог СВ, правка, вставка «в прошлое».

Без железа и без боевой базы (DATA_DIR → tmp, см. conftest.data_dir).
"""
import db


def _row(i, sv, cook, stage=7, day="2026-10-06"):
    return {"ts": "%s_%02d_%02d_00" % (day, 10 + i // 60, i % 60), "t": 1000.0 + i * 90,
            "sv": sv, "stage": stage, "cook_time": cook, "substage": 73, "fines_m3": 4.0 + i,
            "sieve_m3_b2": 10.0, "extra_key": "x"}


def test_boil_segmentation_and_sv_max_run(data_dir):
    rows = [_row(0, 80.0, 600), _row(1, 86.0, 690), _row(2, 84.0, 780), _row(3, 88.0, 870),
            _row(4, 80.5, 20, stage=3)]          # новая варка: cook_time упало
    for r in rows:
        db.upsert_row("S1", r)
    got = db.read_rows("S1", with_boil=True)
    assert [g["boil_id"] for g in got] == [1, 1, 1, 1, 2]
    # sv_max_run — максимум СВ с начала варки (просадка 84 не сбрасывает)
    assert [g["sv_max_run"] for g in got] == [80.0, 86.0, 86.0, 88.0, 80.5]


def test_row_json_roundtrip_keeps_unknown_fields(data_dir):
    r = _row(0, 85.0, 600)
    db.upsert_row("S1", r)
    back = db.read_rows("S1")[0]
    assert back == r and back["extra_key"] == "x"   # поля вне схемы не теряются


def test_insert_in_the_past_rebuilds_boils(data_dir):
    for r in (_row(0, 80.0, 600), _row(1, 86.0, 690), _row(2, 88.0, 780)):
        db.upsert_row("S1", r)
    db.upsert_row("S1", _row(-1, 79.0, 510))        # дописали пробу раньше всех
    assert [g["boil_id"] for g in db.read_rows("S1", with_boil=True)] == [1, 1, 1, 1]


def test_patch_row_updates_column_and_json(data_dir):
    r = _row(0, 85.0, 600)
    db.upsert_row("S1", r)
    assert db.patch_row("S1", r["ts"], {"frac_zones": 2, "frac_pct": 1.5})
    assert not db.patch_row("S1", "нет-такой-ts", {"x": 1})
    got = db.read_rows("S1")[0]
    assert got["frac_zones"] == 2 and got["frac_pct"] == 1.5
    col = db.conn().execute("SELECT frac_zones FROM journal WHERE serial='S1' AND ts=?", (r["ts"],)).fetchone()[0]
    assert col == 2


def test_upsert_many_idempotent(data_dir):
    rows = [_row(i, 80.0 + i, 600 + i * 90) for i in range(4)]
    assert db.upsert_many("S2", rows + rows) == 8   # записей передано 8
    assert db.count("S2") == 4                        # но строк 4 (ключ serial+ts)
    assert db.days("S2") == ["2026-10-06"] and db.serials() == ["S2"]


def test_sv_glitch_low_and_deviation_nulled(data_dir):
    # разовый сбой СВ обнуляется: и явно низкий (<5), и резкий выброс (58 среди 87, |Δ|>15)
    low = db.null_sv_outliers([dict(r) for r in
                               [_row(0, 87.0, 600), _row(1, 0.0, 690), _row(2, 87.5, 780), _row(3, 88.0, 870)]])
    assert low[1]["sv"] is None and [c["sv"] for c in low if c["sv"] is not None] == [87.0, 87.5, 88.0]
    dev = db.null_sv_outliers([dict(r) for r in
                               [_row(0, 87.0, 600), _row(1, 87.5, 690), _row(2, 58.0, 780), _row(3, 88.0, 870)]])
    assert dev[2]["sv"] is None, "выброс 58 при соседях ~87 должен обнуляться"


def test_boil_split_on_long_gap(data_dir):
    # самая частая граница варок — пауза дольше BOIL_GAP_S (между пробами обычно 1–2 мин)
    r0 = _row(0, 85.0, 600)
    r1 = {**_row(1, 86.0, 690), "t": r0["t"] + db.BOIL_GAP_S + 100}
    db.upsert_row("S1", r0)
    db.upsert_row("S1", r1)
    assert [g["boil_id"] for g in db.read_rows("S1", with_boil=True)] == [1, 2]


def test_boil_split_sv_fallback_without_cook_time(data_dir):
    # запасной признак: при отсутствии cook_time резкое падение СВ делит варку
    rows = [{"ts": "2026-10-06_10_0%d_00" % i, "t": 1000.0 + i * 90, "sv": sv}
            for i, sv in enumerate((88.0, 88.5, 80.0))]   # без cook_time; 88.5→80.0 падение > BOIL_SV_DROP
    for r in rows:
        db.upsert_row("S3", r)
    assert [g["boil_id"] for g in db.read_rows("S3", with_boil=True)] == [1, 1, 2]
