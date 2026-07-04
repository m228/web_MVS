"""Единое определение каталогов программы и пользовательских данных.

Разделяем два вида путей, потому что в собранной поставке (PyInstaller) они
живут в разных местах:

* BUNDLE_DIR — где лежат КОД и АССЕТЫ (`page/`, `Driver/`). В собранном бандле
  это папка рядом с exe (`...\app\`), при запуске из исходников — корень репозитория.
* DATA_DIR — где лежат ПОЛЬЗОВАТЕЛЬСКИЕ ДАННЫЕ (`dataset/`, `Videos/`,
  `rtsp_cameras.json`). В собранной поставке это уровень ВЫШЕ бандла
  (`...\web_MVS\`), чтобы обновление, перезаписывающее папку `app\`, эти данные
  не стирало. При запуске из исходников — там же, где код.

Раскладка собранной поставки (one-folder PyInstaller кладёт ассеты в _internal\):

    C:\web_MVS\              <- распакованный архив (DATA_DIR = рядом с exe)
      web_MVS.exe            ┐ из архива (перезаписываются обновлением)
      _internal\            ─┤ BUNDLE_DIR (= sys._MEIPASS): page\ Driver\ VERSION
      run.bat               ┘
      dataset\  Videos\  rtsp_cameras.json   <- создаются в работе, не входят в архив,
                                                поэтому обновление их не затирает
"""
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    # ассеты бандла лежат в sys._MEIPASS (для one-folder это каталог _internal\)
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    # данные — рядом с exe (в той же папке, куда распакован архив)
    DATA_DIR = Path(sys.executable).resolve().parent
else:
    # запуск из исходников: код и данные в корне репозитория
    BUNDLE_DIR = Path(__file__).resolve().parent
    DATA_DIR = BUNDLE_DIR


def read_version() -> str:
    """Версия из файла VERSION рядом с кодом (или 'dev', если файла нет)."""
    try:
        return (BUNDLE_DIR / "VERSION").read_text(encoding="utf-8").strip() or "dev"
    except Exception:
        return "dev"
