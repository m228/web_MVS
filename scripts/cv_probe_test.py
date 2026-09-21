"""Оффлайн-прогон CV-конвейера на реальных кадрах пробы (Фаза 2).

Берёт кадры из папки, для каждого: classic-детектор (cv_service) → cv_analyzer →
печатает рассев и сохраняет overlay. Помогает калибровать пороги на настоящих кристаллах
до готовой YOLO-модели.

Запуск (из корня репо, интерпретатор с opencv/numpy):
    python scripts/cv_probe_test.py <папка_с_png> [сколько_кадров]
"""
import glob
import json
import os
import sys

import cv2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cv_service"))

import cv_analyzer
from detector import build_detector
from sahi_tiler import run_tiled


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\New\Documents\microscope\DA7186922"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tmp", "cv_test")
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    files = sorted(glob.glob(os.path.join(src, "*.png")))[:limit]
    print(f"кадров: {len(files)} | вывод: {out_dir}\n")

    det = build_detector("classic")
    # SAHI-тайлинг классике даёт лучшее покрытие; финальное качество — за YOLO (Фаза 8).
    # shape-reject у классики ОТКЛЮЧАЕМ: рваные контуры недостоверны, брак/форму даст YOLO-маска.
    # Сейчас показываем честный рассев ПО РАЗМЕРУ (эквив. диаметр, мкм).
    classic_cfg = {"shape": {"min_circularity": 0.0, "max_aspect": 999.0, "min_solidity": 0.0}}
    for f in files:
        img = cv2.imread(f)
        if img is None:
            print("FAIL:", f)
            continue
        dets, timing = run_tiled(det, img, tiles=6, conf=0.3)
        objects = [d.as_dict() for d in dets]
        res = cv_analyzer.analyze(img, objects, cv_cfg=classic_cfg, with_overlay=True)
        s = res["summary"]
        name = os.path.basename(f)
        print(f"{name}")
        print(f"  кристаллов: {s['count']} | группы {s['groups']} ({s['groups_pct']})")
        print(f"  размер мкм: средн {s['size_um']['mean']} медиана {s['size_um']['median']} "
              f"p10 {s['size_um']['p10']} p90 {s['size_um']['p90']} CV% {s['size_um']['cv_pct']}")
        print(f"  плотность {s['density_per_mm2']}/мм² | брак {s['reject_pct']}% | "
              f"качество {s['quality']} (blur {s['blur']}) | тайлинг {timing['grid']} {timing['total_ms']}мс\n")
        overlay = res.pop("_overlay")
        cv2.imwrite(os.path.join(out_dir, "overlay_" + name), overlay)
        with open(os.path.join(out_dir, name + ".json"), "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
