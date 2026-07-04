"""Точка входа для собранного бандла (PyInstaller).

В exe нет CLI `uvicorn`, поэтому поднимаем сервер программно. Перед импортом
приложения переходим в каталог бандла (BUNDLE_DIR), потому что app.py ссылается
на ассеты относительными путями (`page/static`, `page/index.html`). Пользовательские
данные при этом пишутся в DATA_DIR абсолютными путями (см. paths.py), а не в CWD.

Запуск из исходников тоже работает (`python run.py`), но в обычной разработке
удобнее `uvicorn app:app --reload`.
"""
import os
import webbrowser

import uvicorn

from paths import BUNDLE_DIR, read_version

HOST = "0.0.0.0"
PORT = 8000


def main():
    # ассеты (page/) ищутся относительно CWD -> переходим в каталог бандла
    os.chdir(BUNDLE_DIR)
    print(f"web_MVS {read_version()} -> http://localhost:{PORT}")
    try:
        webbrowser.open(f"http://localhost:{PORT}")
    except Exception:
        pass
    uvicorn.run("app:app", host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
