"""Защита консольного окна и файловый журнал собранного exe.

Зачем. web_MVS.exe — консольное приложение: у него есть окно консоли, и вывод uvicorn/print идёт туда. Две беды:

1. QuickEdit. Клик мышью по окну консоли включает «выделение», и ближайшая запись в консоль зависает, пока не нажмут Enter/Esc.
   uvicorn пишет строку на каждый запрос прямо в потоке обработки — замирает весь сервер: страница «Failed to fetch», пульса нет.
   После обновления окно приложения всплывает поверх экрана ровно в тот момент, когда оператор смотрит и кликает, — клик попадает в него.
   Поэтому QuickEdit выключаем у самого приложения (disable_quick_edit).
2. Журнала на диске не было: «Логи» в интерфейсе — только в памяти, а текст ошибки при падении оставался в окне консоли, которое
   вместе с процессом исчезает. Теперь вывод (stdout+stderr, в том числе трассировки) дублируется в logs\\app.log, а аварийные
   остановки на уровне C-кода (драйвер камеры) — в logs\\crash.log (faulthandler). Нет строки «выход» в конце — процесс убили/он упал.

Включается только в собранной поставке (run.py), из исходников ничего не меняет.
"""
import atexit
import ctypes
import faulthandler
import os
import re
import sys
import threading
import time
from pathlib import Path

LOG_DIR_NAME = "logs"
LOG_NAME = "app.log"
CRASH_NAME = "crash.log"
MAX_BYTES = 4 * 1024 * 1024            # потолок app.log; при превышении старый уходит в app.log.1 (всего до ~8 МБ)

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# успешный запрос в журнале доступа uvicorn: страницы опрашивают сервер каждую секунду — в файл они не нужны, только ошибки
_ACCESS_OK = re.compile(r'"\s*(?:GET|POST|PUT|DELETE|HEAD|OPTIONS|PATCH) [^"]*"\s+[23]\d\d\b')


def disable_quick_edit() -> bool:
    """Выключить QuickEdit и вставку у консоли процесса: клик по окну больше не замораживает запись в консоль.
    True — режим сменён; False — не Windows или у процесса нет консоли (stdin не консольный)."""
    if os.name != "nt":
        return False
    try:
        from ctypes import wintypes
        k = ctypes.windll.kernel32
        k.GetStdHandle.restype = wintypes.HANDLE
        k.GetStdHandle.argtypes = [wintypes.DWORD]
        k.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        h = k.GetStdHandle(wintypes.DWORD(-10))              # STD_INPUT_HANDLE
        mode = wintypes.DWORD(0)
        if not h or not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        # 0x40 QUICK_EDIT и 0x20 INSERT снимаем, 0x80 EXTENDED_FLAGS обязателен, иначе Windows оставит QuickEdit как был
        return bool(k.SetConsoleMode(h, (mode.value | 0x80) & ~0x40 & ~0x20))
    except Exception:
        return False


class LogFile:
    """Построчный файловый журнал: метка времени в начале каждой строки, без ANSI-цветов и без успешных запросов (2xx/3xx),
    с потолком размера (MAX_BYTES, один архивный файл). Ошибки записи глотает — журнал не должен ронять приложение."""

    def __init__(self, path, max_bytes: int = MAX_BYTES):
        self._path = str(path)
        self._max = int(max_bytes)
        self._lock = threading.Lock()
        self._bol = True
        self._f = open(self._path, "a", encoding="utf-8", errors="replace", buffering=1)
        try:
            self._size = os.path.getsize(self._path)
        except OSError:
            self._size = 0

    def write(self, data) -> None:
        if not data:
            return
        if isinstance(data, bytes):
            data = data.decode("utf-8", "replace")
        data = _ANSI.sub("", data)
        with self._lock:
            try:
                if self._bol and data.endswith("\n") and data.count("\n") == 1 and _ACCESS_OK.search(data):
                    return
                for part in data.splitlines(True):
                    if self._bol:
                        part = time.strftime("%Y-%m-%d %H:%M:%S ") + part
                    self._f.write(part)
                    self._size += len(part)
                    self._bol = part.endswith("\n")
                if self._size > self._max:
                    self._rotate()
            except Exception:
                pass

    def _rotate(self) -> None:
        self._f.close()
        try:
            os.replace(self._path, self._path + ".1")
        except OSError:
            pass
        self._f = open(self._path, "a", encoding="utf-8", errors="replace", buffering=1)
        self._size = 0

    def flush(self) -> None:
        with self._lock:
            try:
                self._f.flush()
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            try:
                self._f.close()
            except Exception:
                pass


class Tee:
    """Подменяет sys.stdout/sys.stderr: сначала файл (даже если консоль встала — последняя строка уже на диске), потом консоль.
    Любая ошибка консоли (нет консоли, кодировка cp866, закрыта) глотается. Остальные атрибуты (isatty, encoding, fileno...) —
    от консольного потока: uvicorn спрашивает у stderr isatty() и по нему красит вывод."""

    def __init__(self, log, console):
        self._log = log
        self._console = console

    def write(self, data):
        try:
            self._log.write(data)
        except Exception:
            pass
        if self._console is not None:
            try:
                return self._console.write(data)
            except Exception:
                pass
        return len(data) if data else 0

    def flush(self):
        try:
            self._log.flush()
        except Exception:
            pass
        if self._console is not None:
            try:
                self._console.flush()
            except Exception:
                pass

    def isatty(self):
        try:
            return bool(self._console is not None and self._console.isatty())
        except Exception:
            return False

    def __getattr__(self, name):
        if self._console is None:
            raise AttributeError(name)
        return getattr(self._console, name)


def install(data_dir, version: str = "", crash_dump: bool = True, register_exit: bool = True):
    """Включить защиту консоли и файловый журнал. Возвращает путь к app.log или None, если папку журнала создать не удалось
    (тогда приложение просто работает как раньше). Вызывать один раз, в самом начале main() — до прогрева драйвера камер."""
    disable_quick_edit()
    try:
        d = Path(data_dir) / LOG_DIR_NAME
        d.mkdir(parents=True, exist_ok=True)
        path = d / LOG_NAME
        log = LogFile(path)
    except Exception:
        return None
    sys.stdout = Tee(log, sys.stdout)
    sys.stderr = Tee(log, sys.stderr)
    if crash_dump:
        try:
            # падение внутри драйвера камеры (access violation) Python-исключением не ловится — faulthandler пишет трассировку потоков
            fh = open(d / CRASH_NAME, "a", encoding="utf-8", errors="replace", buffering=1)
            fh.write("=== %s запуск, pid %d ===\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid()))
            faulthandler.enable(file=fh, all_threads=True)
        except Exception:
            pass
    print("=== web_MVS %s: запуск, pid %d, аргументы %s ===" % (version or "?", os.getpid(), sys.argv[1:]))
    if register_exit:
        atexit.register(lambda: print("=== выход (штатный) ==="))
    return path
