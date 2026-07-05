"""Точка входа для собранного бандла (PyInstaller).

В exe нет CLI `uvicorn`, поэтому поднимаем сервер программно. Перед импортом
приложения переходим в каталог бандла (BUNDLE_DIR), потому что app.py ссылается
на ассеты относительными путями (`page/static`, `page/index.html`). Пользовательские
данные при этом пишутся в DATA_DIR абсолютными путями (см. paths.py), а не в CWD.

Запуск из исходников тоже работает (`python run.py`), но в обычной разработке
удобнее `uvicorn app:app --reload`.
"""
import os
import sys
import webbrowser

import uvicorn

from paths import BUNDLE_DIR, DATA_DIR, read_version

HOST = "0.0.0.0"
PORT = 8000


class _Tee:
    """Пишет одновременно в несколько потоков (консоль + файл)."""
    def __init__(self, *streams):
        self._streams = streams

    def write(self, data):
        for s in self._streams:
            try:
                s.write(data)
            except Exception:
                pass

    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass


def run_diag():
    """Прогон диагностики В ТОМ ЖЕ frozen-окружении, что и приложение.
    Запуск: web_MVS.exe --diag  (или env WEB_MVS_DIAG=1). Вывод дублируется в
    diag_output.txt рядом с exe — этот файл удобно прислать."""
    os.chdir(BUNDLE_DIR)
    out_path = DATA_DIR / "diag_output.txt"
    f = None
    orig = sys.stdout
    try:
        f = open(out_path, "w", encoding="utf-8")
        sys.stdout = _Tee(orig, f)
    except Exception:
        f = None
    try:
        import diag
        diag.main()
    except SystemExit:
        pass
    except Exception:
        import traceback
        traceback.print_exc()
    finally:
        sys.stdout = orig
        if f is not None:
            f.close()
    print(f"\nДиагностика сохранена в: {out_path}")
    try:
        input("Нажми Enter, чтобы закрыть окно...")
    except Exception:
        pass


def _preflight():
    """Диагностика перечисления камер ДО старта uvicorn (event loop ещё не мешает).

    Разводим три переменные, чтобы понять, что именно ломается в exe (в --diag камеры
    видны, как сервер — нет):
      A) СВЕЖИЙ Harvester в главном потоке — как в рабочем --diag;
      B) СВЕЖИЙ Harvester в ОТДЕЛЬНОМ потоке — проверка привязки продюсера к потоку;
      C) штатный путь manager.scan_cams в главном потоке — как в приложении.
    Все три пишут количество камер в «Историю событий». Сравнив A/B/C, увидим причину:
    свежий vs manager-Harvester, главный vs рабочий поток."""
    import threading
    from logger import log_event

    def _enum_fresh():
        try:
            from camera_core import _discover_cti
            from harvesters.core import Harvester
            cti, _ = _discover_cti()
            h = Harvester()
            h.add_file(str(cti))
            h.update()
            n = len(h.device_info_list)
            try:
                h.reset()
            except Exception:
                pass
            return n
        except Exception as exc:
            return f"ERR: {exc}"

    # A) свежий Harvester, главный поток
    try:
        a = _enum_fresh()
        log_event("run.preflight", "A: свежий Harvester (главный поток)", "info",
                  {"thread": threading.current_thread().name, "count": a})
    except Exception as exc:
        log_event("run.preflight", "A: ошибка", "warn", {"error": str(exc)})

    # B) свежий Harvester, отдельный поток
    box = {}
    def _worker():
        box["thread"] = threading.current_thread().name
        box["count"] = _enum_fresh()
    try:
        t = threading.Thread(target=_worker, name="preflight-worker")
        t.start(); t.join()
        log_event("run.preflight", "B: свежий Harvester (отдельный поток)", "info",
                  {"thread": box.get("thread"), "count": box.get("count")})
    except Exception as exc:
        log_event("run.preflight", "B: ошибка", "warn", {"error": str(exc)})

    # C) штатный путь приложения (manager) в главном потоке — заодно прогреваем драйвер
    try:
        from camera_core import manager
        manager.load_driver()
        cams = manager.scan_cams()
        real = [s for s in (cams or {}) if s != "DA123123"]
        log_event("run.preflight", "C: manager.scan_cams (главный поток)", "info",
                  {"thread": threading.current_thread().name, "online_real": len(real)})
    except Exception as exc:
        log_event("run.preflight", "C: ошибка", "warn", {"error": str(exc)})


def main():
    if "--diag" in sys.argv or os.environ.get("WEB_MVS_DIAG"):
        run_diag()
        return
    # ассеты (page/) ищутся относительно CWD -> переходим в каталог бандла
    os.chdir(BUNDLE_DIR)
    # предзагрузка драйвера в главном потоке до uvicorn (см. _preflight)
    _preflight()
    print(f"web_MVS {read_version()} -> http://localhost:{PORT}")
    try:
        webbrowser.open(f"http://localhost:{PORT}")
    except Exception:
        pass
    uvicorn.run("app:app", host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
