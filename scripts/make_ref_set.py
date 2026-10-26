"""Собрать эталонный набор для проверки модели (model_check) из разбора кадров Макса (tmp/cv_accuracy/ann).

Для каждого размеченного кадра: исходный PNG пробы, контуры эталона (модель − удалённое + добавленное + признанные целыми),
СВ, зона варки, порог муки пробы (fines_um). Плюс маска светлой зоны (свет ≥ 60 %) и параметры измерения, которыми
получены сегодняшние цифры (manifest.measure_cfg) — они фиксированы и от живых настроек формы не зависят.

usage: py -3 scripts/make_ref_set.py --out E:\\kir\\ref_set [--ann tmp/cv_accuracy/ann] [--flat tmp/cv_accuracy/tools/flat.npy] [--zone-lv 0.6]
Папку потом один раз копируют на ПК завода рядом с mvs.db (model_check ищет <папка базы>\\ref_set). Не входит в пакет программы.
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import cv2
import numpy as np

# параметры измерения эталонного разбора (tmp/cv_accuracy/tools/metrics.py): с ними получены −26 / −17 / −9 % для порогов 0,55 / 0,35 / 0,25
MEASURE_CFG = {"overlap": 0.15, "iou": 0.45, "um_per_px": 1.629, "tiles": 2, "conf": 0.55,
               "groups": {"small_max_um": 250.0, "medium_max_um": 400.0},
               "shape": {"suspect_aspect": 1.7, "min_circularity": 0.45, "min_solidity": 0.8},
               "cluster_gap_px": 0.0}
K_THICK = 0.88


def ref_polys(st: dict) -> list:
    deleted, uncut = set(st["deleted"]), set(st["uncut"])
    keep = [o["poly"] for i, o in enumerate(st["model"]) if "m%d" % i not in deleted and "m%d" % i not in uncut]
    return keep + [a["poly"] for a in st["added"]] + [st["model"][int(u[1:])]["poly"] for u in uncut]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--ann", default="tmp/cv_accuracy/ann")
    ap.add_argument("--flat", default="tmp/cv_accuracy/tools/flat.npy")
    ap.add_argument("--zone-lv", type=float, default=0.6)
    a = ap.parse_args()
    out, ann = Path(a.out), Path(a.ann)
    out.mkdir(parents=True, exist_ok=True)
    frames, skipped = [], []
    for d in sorted(ann.iterdir()):
        f = d / "state.json"
        if not f.exists():
            continue
        st = json.loads(f.read_text(encoding="utf-8"))
        if not st.get("done"):
            continue
        probe = Path(st["probe"])
        src = probe / ("frame_%s.png" % st["frame"])
        if not src.exists() or not (probe / "result.json").exists():
            skipped.append(d.name)
            continue
        fines_um = json.loads((probe / "result.json").read_text(encoding="utf-8"))["summary"]["volume_cfg"]["fines_um"]
        shutil.copy2(src, out / (d.name + ".png"))
        (out / (d.name + ".json")).write_text(json.dumps({"polys": ref_polys(st)}), encoding="utf-8")
        frames.append({"name": d.name, "image": d.name + ".png", "polys": d.name + ".json", "zone": st["zone"], "boil": st["boil"],
                       "sv": st["sv"], "stage": st["stage"], "fines_um": fines_um})
        print(".", end="", flush=True)
    print()
    flat = np.load(a.flat).astype(np.float32)
    ok, png = cv2.imencode(".png", ((flat >= a.zone_lv) * 255).astype(np.uint8))
    (out / "zone.png").write_bytes(png.tobytes())
    man = {"version": 1, "created": time.strftime("%Y-%m-%d"), "source": "разметка Макса, Кир, 15–17.10.2026 (70 кадров, 5 зон × 14)",
           "measure_cfg": MEASURE_CFG, "k_thick": K_THICK, "zone_lv": a.zone_lv, "frames": frames}
    (out / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    mb = sum(p.stat().st_size for p in out.iterdir()) / 1e6
    print("готово: %d кадров, %.0f МБ → %s%s" % (len(frames), mb, out, ("; пропущено (нет кадра пробы): %s" % ", ".join(skipped)) if skipped else ""))
    return 0 if frames else 1


if __name__ == "__main__":
    sys.exit(main())
