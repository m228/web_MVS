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
    """Предзагрузка драйвера в чистом главном потоке ДО старта uvicorn.

    Симптом: в --diag (главный поток, без event loop) продюсер перечисляет 10 камер,
    а внутри uvicorn (и на старте, и по /api/cams) — 0. Разница — только запуск
    uvicorn на весь процесс. Инициализируем продюсер здесь, пока uvicorn ещё НЕ
    запущен: продюсер откроет System-модуль в том же контексте, что и рабочий --diag,
    и (гипотеза) продолжит видеть камеры дальше. Плюс лог — решающая проба: если тут
    камеры есть, а в lifespan уже нет, значит виноват именно uvicorn.run."""
    try:
        import threading
        from logger import log_event
        from camera_core import manager
        manager.load_driver()
        cams = manager.scan_cams()
        real = [s for s in (cams or {}) if s != "DA123123"]
        log_event("run.preflight", "Предзагрузка драйвера до uvicorn", "info",
                  {"thread": threading.current_thread().name,
                   "device_count_online": len(real), "serials": real})
    except Exception as exc:
        try:
            from logger import log_event
            log_event("run.preflight", "Предзагрузка не удалась", "warn", {"error": str(exc)})
        except Exception:
            pass


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
