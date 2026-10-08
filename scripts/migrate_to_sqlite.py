r"""Перенос старых данных CV в SQLite (одна папка = один завод/камера).

Папка-источник — как на машине: журналы `YYYY-MM-DD.jsonl` и папки проб `YYYY-MM-DD_HH_MM_SS/result.json`
(в том числе плоская копия cv_results + cv_history, как в E:\cv_result\kir). Вложенные папки с журналами тоже
читаются; дубли отсеиваются по времени пробы (ts).

Правила:
  * строка журнала — главная; если пробы в журнале нет, строка строится из result.json (как при записи);
  * битые result.json (пустой, недокачанный) и папки без него пропускаются — проба остаётся строкой журнала;
  * исходные файлы не меняются.

Запуск:  .venv\Scripts\python.exe scripts\migrate_to_sqlite.py --src E:\cv_result\kir --serial DA7186922 [--db путь\mvs.db]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv_store  # noqa: E402
import db  # noqa: E402


def collect(src: Path) -> tuple[dict, dict]:
    """(строки по ts, статистика). Строки журнала главные, пробы без строки — из result.json."""
    rows, stat = {}, {"journal_files": 0, "journal_rows": 0, "dups": 0, "bad_lines": 0, "dirs": 0, "from_probe": 0, "bad_probe": 0}
    for f in sorted(src.rglob("*.jsonl")):                       # и вложенные (копии журналов)
        stat["journal_files"] += 1
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except Exception:
                stat["bad_lines"] += 1
                continue
            if r.get("ts") is None or r.get("t") is None:
                stat["bad_lines"] += 1
                continue
            stat["journal_rows"] += 1
            if r["ts"] in rows:
                stat["dups"] += 1
            else:
                rows[r["ts"]] = r
    for d in sorted(p for p in src.iterdir() if p.is_dir() and p.name[:4].isdigit() and "_" in p.name):
        stat["dirs"] += 1
        rp = d / "result.json"
        try:
            res = json.loads(rp.read_text(encoding="utf-8"))
        except Exception:
            stat["bad_probe"] += 1
            continue
        if res.get("ts") in rows:
            continue
        row = cv_store._hist_row(res)
        if row:
            rows[row["ts"]] = row
            stat["from_probe"] += 1
    return rows, stat


def migrate(src: Path, serial: str, db_path: str | None = None) -> dict:
    if db_path:
        db.set_path(db_path)
    rows, stat = collect(src)
    before = db.count(serial)
    n = db.upsert_many(serial, [rows[k] for k in sorted(rows)])
    after = db.count(serial)
    c = db.conn()
    span = c.execute("SELECT MIN(ts), MAX(ts), MAX(boil_id) FROM journal WHERE serial=?", (serial,)).fetchone()
    return {**stat, "serial": serial, "rows_written": n, "db_before": before, "db_after": after,
            "first": span[0], "last": span[1], "boils": span[2]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, help="папка с журналами и пробами одного завода")
    ap.add_argument("--serial", required=True, help="серийный номер камеры (ключ в базе)")
    ap.add_argument("--db", default=None, help="файл базы (по умолчанию <DATA_DIR>/mvs.db)")
    a = ap.parse_args()
    out = migrate(Path(a.src), a.serial, a.db)
    for k, v in out.items():
        print("%-14s %s" % (k, v))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
