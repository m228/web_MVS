"""Диагностика подключения GigE-камеры (Harvesters / genicam + Hikrobot MVS).

Запуск (в ТОМ ЖЕ venv, где работает приложение):
    python diag.py

Скрипт НИЧЕГО не меняет на камере — только перечисляет устройства, пробует их
открыть (create) и печатает подробности: версии, путь к .cti, наличие runtime
MVS, и полный traceback того места, где возникает -1003. Цель — понять, почему
enumerate проходит, а create() падает.
"""
import os
import sys
import platform
import traceback
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # чтобы не падать на не-cp1251 символах
except Exception:
    pass


def line():
    print("-" * 72)


print("== ВЕРСИИ ==")
print("python    :", platform.python_version(), "|", sys.executable)
for name in ("harvesters", "genicam", "numpy", "cv2"):
    try:
        module = __import__(name)
        print(f"{name:<10}:", getattr(module, "__version__", "?"))
    except Exception as exc:
        print(f"{name:<10}: НЕ установлен ({exc})")

line()
print("== ПРОДЮСЕР / RUNTIME ==")
try:
    from camera_core import _discover_cti, _find_mvs_runtime, _explain_error, MVS_GENTL_DIRS
except Exception as exc:
    print("Не удалось импортировать camera_core:", exc)
    raise

cti, source = _discover_cti()
print("cti       :", cti, f"(источник: {source})")
runtime = _find_mvs_runtime()
print("runtime   :", runtime or "НЕ НАЙДЕН  <-- без него create() обычно падает с -1003")
print("Папки установки MVS:")
for d in MVS_GENTL_DIRS:
    print("   ", "ЕСТЬ " if Path(d).is_dir() else "нет  ", d)
mvs_on_path = [d for d in os.environ.get("PATH", "").split(os.pathsep) if "MVS" in d.upper()]
print("Записи 'MVS' в PATH:", mvs_on_path or "нет")

line()
print("== ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ==")
for var in ("MVS_CTI_PATH", "GENICAM_GENTL64_PATH",
            "GENICAM_CACHE_V3_4", "GENICAM_CACHE_V3_3", "GENICAM_CACHE",
            "MVCAM_COMMON_RUNENV", "MVCAM_SDK_PATH"):
    print(f"  {var} = {os.environ.get(var)}")

if cti is None:
    print("\n.cti не найден — дальше идти некуда. Установи MVS SDK или положи .cti в Driver/.")
    sys.exit(1)

line()
print("== ПЕРЕЧИСЛЕНИЕ + СОЗДАНИЕ ==")
from harvesters.core import Harvester

h = Harvester()
h.add_file(str(cti))
h.update()
devices = h.device_info_list
print("Найдено устройств:", len(devices))
for i, d in enumerate(devices):
    try:
        print(f"  [{i}] serial={d.serial_number!r} "
              f"model={getattr(d, 'model', None)!r} "
              f"access_status={getattr(d, 'access_status', None)} "
              f"id={getattr(d, 'id_', None)!r}")
    except Exception as exc:
        print(f"  [{i}] <ошибка чтения info: {exc}>")

for i in range(len(devices)):
    line()
    print(f"create({i}) ...")
    ia = None
    try:
        ia = h.create(i)
        print("  create() OK. Пробую remote_device.node_map ...")
        try:
            node_map = ia.remote_device.node_map
            print("  node_map OK. Width =", node_map.Width.value,
                  "Height =", node_map.Height.value)
        except Exception as exc:
            print("  node_map ОШИБКА:", _explain_error(exc))
            traceback.print_exc()
    except Exception as exc:
        print("  create() ОШИБКА:", _explain_error(exc))
        traceback.print_exc()
    finally:
        if ia is not None:
            try:
                ia.destroy()
            except Exception:
                pass

try:
    h.reset()
except Exception:
    pass
print("\nГотово. Пришли мне весь вывод этого скрипта.")
