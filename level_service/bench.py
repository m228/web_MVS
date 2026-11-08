"""Замер скорости модели уровня: процессор (torch) против OpenVINO на процессоре и на видеоядре Intel (UHD / Iris / Arc).

Зачем: перед тем как переводить сервис на видеоядро, надо увидеть цифры на ЭТОМ компьютере (видеоядро на бумаге быстрее, на деле — не всегда).
Заодно проверяется, что картинка не поплыла: площадь жидкости на тестовом кадре у всех вариантов должна совпасть до нескольких процентов.

Запуск (из папки level_service, лучше при остановленном сервисе — не мешать друг другу):
    bench.bat                                  — модель из model\\last_model.json, размеры 640 960 1280, тестовый кадр
    bench.bat model\\26x_detection.pt 640 1280 C:\\кадр.jpg

Первый запуск каждого размера на OpenVINO переводит модель в его формат (одна-две минуты, один раз; результат кладётся в bench_ov\\).
Ставит openvino сам (pip) — нужен интернет один раз. Результат печатается в окно и пишется в bench_result.txt рядом.
"""
from __future__ import annotations

import json
import os
import shutil
import statistics
import sys
import time
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
RUNS, WARM = 5, 2


def pick_model(arg: str | None) -> Path:
    if arg and arg.lower().endswith((".pt", ".onnx")):
        return Path(arg)
    try:
        return HERE / json.loads((HERE / "model" / "last_model.json").read_text(encoding="utf-8"))["path"]
    except (OSError, ValueError, KeyError):
        found = sorted((HERE / "model").glob("*.pt"))
        if found:
            return found[0]
    sys.exit("Модель не найдена: положите .pt в level_service\\model или укажите путь первым параметром.")


def test_frame(arg: str | None) -> np.ndarray:
    if arg and Path(arg).is_file():
        img = cv2.imread(arg)
        if img is not None:
            return img
    # без кадра: серый градиент 1920×1080 (скорость от содержимого почти не зависит, но маски на нём не найдутся — сверка площадей пропускается)
    g = np.tile(np.linspace(40, 200, 1920, dtype=np.uint8), (1080, 1))
    return cv2.merge([g, g, g])


def liquid_area(r) -> float:
    m = getattr(r, "masks", None)
    if m is None or len(m.data) == 0:
        return 0.0
    return float((m.data.cpu().numpy() > 0.5).any(axis=0).mean())


def timeit(fn) -> tuple:
    t = time.time()
    r = fn()
    first = time.time() - t
    for _ in range(WARM - 1):
        fn()
    ts = []
    for _ in range(RUNS):
        t = time.time()
        r = fn()
        ts.append(time.time() - t)
    return first, statistics.median(ts), r


def main():
    args = sys.argv[1:]
    model = pick_model(args[0] if args and args[0].lower().endswith((".pt", ".onnx")) else None)
    rest = [a for a in args if a != str(model) and not a.lower().endswith((".pt", ".onnx"))]
    sizes = [int(a) for a in rest if a.isdigit()] or [640, 960, 1280]
    frame = test_frame(next((a for a in rest if not a.isdigit()), None))
    out = []

    def say(s=""):
        print(s, flush=True)
        out.append(s)

    from ultralytics import YOLO
    try:
        import openvino as ov
        devs = ov.Core().available_devices
    except ImportError:
        say("openvino не установлен — ставлю (pip install openvino) ...")
        os.system('"%s" -m pip install "openvino>=2024.0"' % sys.executable)
        import openvino as ov
        devs = ov.Core().available_devices
    say("Модель: %s   кадр: %dx%d   устройства OpenVINO: %s" % (model, frame.shape[1], frame.shape[0], ", ".join(devs)))
    say("Время на кадр, секунды (медиана из %d), в скобках — первый вызов; «жидкость» — доля кадра, закрытая маской" % RUNS)
    say("%-6s %-22s %-14s %-12s" % ("вход", "вариант", "с/кадр", "жидкость"))
    base = YOLO(str(model))
    for sz in sizes:
        rows = []
        first, med, r = timeit(lambda: base.predict(frame, imgsz=sz, device="cpu", verbose=False)[0])
        rows.append(("torch · процессор", first, med, liquid_area(r)))
        d = HERE / "bench_ov" / str(sz)
        if not (d / (model.stem + "_openvino_model")).is_dir():
            d.mkdir(parents=True, exist_ok=True)
            shutil.copy2(model, d / model.name)
            say("  … перевожу модель в формат OpenVINO для входа %d (один раз) ..." % sz)
            YOLO(str(d / model.name)).export(format="openvino", imgsz=sz, half=True, dynamic=False, verbose=False)
        ovm = YOLO(str(d / (model.stem + "_openvino_model")), task="segment")
        for dev in ("intel:cpu", "intel:gpu"):
            name = dev.split(":")[1].upper()
            if not any(x == name or x.startswith(name + ".") for x in devs):
                rows.append(("OpenVINO · " + name, None, None, None))
                continue
            try:
                first, med, r = timeit(lambda: ovm.predict(frame, imgsz=sz, device=dev, verbose=False)[0])
                rows.append(("OpenVINO · " + {"CPU": "процессор", "GPU": "видеоядро"}[name], first, med, liquid_area(r)))
            except Exception as e:                      # noqa: BLE001 — показать причину и идти дальше
                rows.append(("OpenVINO · " + name + " (ошибка: %s)" % str(e)[:60], None, None, None))
        for label, first, med, area in rows:
            if med is None:
                say("%-6d %-22s %s" % (sz, label, "недоступно"))
            else:
                say("%-6d %-22s %-14s %-12s" % (sz, label, "%.2f (%.1f)" % (med, first), "%.1f %%" % (area * 100)))
    (HERE / "bench_result.txt").write_text("\n".join(out), encoding="utf-8")
    say("\nГотово: результат сохранён в %s" % (HERE / "bench_result.txt"))


if __name__ == "__main__":
    main()
