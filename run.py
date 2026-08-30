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


def _warmup():
    """Построить список камер в ГЛАВНОМ потоке ДО старта uvicorn (frozen: 0 камер).

    ВАЖНО (2026-08-30): раньше здесь создавался ОТДЕЛЬНЫЙ временный Harvester с
    update()+reset() «для прогрева». Оказалось, что reset() Hikrobot-продюсера портит
    его на ВЕСЬ процесс — после этого manager.load_driver в том же процессе перечисляет
    0 камер, хотя diag (отдельный процесс, без этого reset) находит камеру ОДНИМ enum.
    Поэтому временный Harvester убран: делаем РОВНО ОДИН enum через manager (как diag
    STAGE 1 — свежий Harvester, один update), и список сохраняется (фикс load_driver
    не даёт рабочим потокам uvicorn его затереть)."""
    try:
        import threading as _th
        from logger import log_event as _le
        from camera_core import manager as _cam
        _cam.load_driver()
        _cam.scan_cams()
        _le("run.warmup", "Camera manager прогрет в главном потоке (один enum, как diag)",
            "info", {"thread": _th.current_thread().name, "cams": len(_cam.cam_online)})
    except Exception as exc:
        try:
            from logger import log_event as _le
            _le("run.warmup", "Прогрев camera manager не выполнен", "warn", {"error": str(exc)})
        except Exception:
            pass


def main():
    if "--diag" in sys.argv or os.environ.get("WEB_MVS_DIAG"):
        run_diag()
        return
    # ассеты (page/) ищутся относительно CWD -> переходим в каталог бандла
    os.chdir(BUNDLE_DIR)
    # прогрев продюсера в главном потоке до uvicorn — иначе в exe рабочие потоки
    # видят 0 камер (см. _warmup)
    _warmup()
    print(f"web_MVS {read_version()} -> http://localhost:{PORT}")
    # --no-browser: автозапуск при входе в Windows (задача планировщика, см. autostart.py)
    # не должен открывать окно браузера каждый раз. Захват RTSP теперь фоновый,
    # поэтому для записи браузер не нужен.
    if "--no-browser" not in sys.argv and not os.environ.get("WEB_MVS_NO_BROWSER"):
        try:
            webbrowser.open(f"http://localhost:{PORT}")
        except Exception:
            pass
    # ws="none": WebSocket не используем (стрим через MJPEG/HTTP) -> не грузим websockets,
    # заодно уходит DeprecationWarning от uvicorn+websockets 14+
    uvicorn.run("app:app", host=HOST, port=PORT, log_level="info", ws="none")


if __name__ == "__main__":
    main()
