"""Разовый пересчёт объёма у старых проб: дописать в result.json каждой пробы мелочь/сростки по объёму
и столбцы объёма в строки журнала (*.jsonl).

Объёма в старых пробах нет (сняты до версии 1.8.11), но в objects_N.json есть всё нужное: размер, длина,
ширина, группа, причина брака. Скрипт считает из них, как это делает сервер при чтении.

Безопасность: перед записью result.json копируется в result.pre-volume.json (рядом), журнал — в *.jsonl.pre-volume.
Повторный запуск проб с объёмом не трогает. Данные кадров (jpg, objects) не меняются.

    .venv\\Scripts\\python.exe scripts\\backfill_volume.py E:\\cv_result\\bui
    .venv\\Scripts\\python.exe scripts\\backfill_volume.py cv_results\\camera --hist cv_history\\camera --fines 0.2 --k 0.88
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cv_store  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("probes", help="папка с пробами (внутри — папки 2026-..._HH_MM_SS)")
    ap.add_argument("--hist", help="папка журнала *.jsonl (по умолчанию — та же, что и probes)")
    ap.add_argument("--fines", type=float, help="сторона квадрата мелочи, мм (по умолчанию из конфига, 0,2)")
    ap.add_argument("--k", type=float, help="коэффициент толщины (по умолчанию из конфига)")
    ap.add_argument("--force", action="store_true", help="пересчитать и пробы, где объём уже есть (например, после смены порога)")
    ap.add_argument("--dry", action="store_true", help="только показать, что будет сделано")
    a = ap.parse_args()

    root = Path(a.probes)
    hist = Path(a.hist) if a.hist else root
    cfg = cv_store._volume_cfg_now()
    vol = cfg.setdefault("volume", {})
    if a.fines:
        vol["fines_side_mm"] = a.fines
    if a.k:
        vol["k_thick"] = a.k

    rows_by_ts, done, skipped, bad = {}, 0, 0, []
    for d in sorted(p for p in root.iterdir() if p.is_dir() and p.name[:2] == "20"):
        rf = d / "result.json"
        try:
            r = json.loads(rf.read_text(encoding="utf-8"))
        except Exception as e:
            bad.append("%s: %s" % (d.name, e))
            continue
        if a.force and r.get("summary"):
            r["summary"].pop("volume", None)
        had = bool((r.get("summary") or {}).get("volume"))
        if not had:
            r = cv_store._with_volume(d, r, cfg)
            if not (r.get("summary") or {}).get("volume"):
                bad.append("%s: нет объектов кадров" % d.name)
                continue
            if not a.dry:
                bak = d / "result.pre-volume.json"
                if not bak.exists():
                    shutil.copy2(rf, bak)
                rf.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
            done += 1
        else:
            skipped += 1
        row = cv_store._hist_row(r)
        if row:
            rows_by_ts[r["ts"]] = row

    jf_done = 0
    for jf in sorted(hist.glob("*.jsonl")):
        out, changed = [], 0
        for line in jf.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                out.append(line)
                continue
            src = rows_by_ts.get(row.get("ts"))
            if src:
                vol_keys = [k for k in src if k.startswith(("fines_", "agg_", "vtot_")) or k == "k_thick"]
                new = ({**row, **{k: src[k] for k in vol_keys}} if a.force
                       else {**row, **{k: v for k, v in src.items() if k not in row or row.get(k) is None}})
                changed += new != row
                row = new
            out.append(json.dumps(row, ensure_ascii=False))
        if changed and not a.dry:
            bak = jf.with_name(jf.name + ".pre-volume")
            if not bak.exists():
                shutil.copy2(jf, bak)
            jf.write_text("\n".join(out) + "\n", encoding="utf-8")
        jf_done += bool(changed)
        print("журнал %s: строк обновлено %d" % (jf.name, changed))

    print("проб пересчитано: %d · уже с объёмом: %d · не удалось: %d · журналов обновлено: %d%s"
          % (done, skipped, len(bad), jf_done, " (DRY — ничего не записано)" if a.dry else ""))
    for b in bad:
        print("  !", b)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
