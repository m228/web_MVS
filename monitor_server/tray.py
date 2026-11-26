"""Значок приёмника в области уведомлений Windows (трее) и надзор за сервером.

Запускается вместо server.py (автозапуск — install_autostart.bat): держит server.py дочерним процессом без окна и рисует значок.
Меню значка: «Открыть панель» (двойной щелчок), «Обновить» (подтянуть свежий код приёмника из git и перезапустить сервер), «Перезапустить сервер»
(подхватит код, который уже лежит на диске), «Выход» (останавливает и сервер).
Сервер упал — надзор поднимает его снова (пауза растёт, если падает сразу после старта). Не поставилась библиотека значка (pystray / Pillow) —
сервер всё равно работает, просто без значка: мониторинг важнее картинки.
Журнал — tray.log; журнал самого сервера по-прежнему service.log.
"""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

log = logging.getLogger("monitor.tray")

CREATE_NO_WINDOW = 0x08000000
MUTEX_NAME = "Local\\web_MVS_monitor_tray"
OK_AFTER_S = 10          # дочерний процесс прожил столько — старт считается удачным, счётчик быстрых падений сбрасывается
BACKOFF_MAX_S = 60
GIT_TIMEOUT_S = 90
# что подтягиваем при «Обновить»: только код приёмника и VERSION, а не весь репозиторий (в папке могут идти чужие правки); данные и журналы не трогаем
UPDATE_PATHS = ("monitor_server", "VERSION")
UPDATE_KEEP = (":(exclude)monitor_server/data", ":(exclude)monitor_server/tray.log", ":(exclude)monitor_server/service.log",
               ":(exclude)monitor_server/.venv", ":(exclude)monitor_server/__pycache__")


def _git(root: Path, *args: str, git: str = "git") -> subprocess.CompletedProcess:
    kw = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace", "timeout": GIT_TIMEOUT_S, "stdin": subprocess.DEVNULL,
          "env": dict(os.environ, GIT_TERMINAL_PROMPT="0")}      # запрос пароля в окне, которого нет, повесил бы обновление
    if os.name == "nt":
        kw["creationflags"] = CREATE_NO_WINDOW
    return subprocess.run([git, "-C", str(root), *args], **kw)


def _first_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1][:200] if lines else "git ничего не ответил"


def update_files(root: Path, remote: str = "origin", branch: str = "main", git: str = "git") -> dict:
    """Кнопка «Обновить»: скачать свежий main и заменить файлы приёмника (monitor_server и VERSION) без переключения веток и без чужих правок
    в остальном дереве. Возвращает {ok, changed, version, tray_changed, message}; changed — файлы действительно заменены (серверу нужен перезапуск)."""
    root = Path(root)
    res = {"ok": False, "changed": False, "version": "", "tray_changed": False, "message": ""}
    ref = "%s/%s" % (remote, branch)
    try:
        if not (root / ".git").exists():
            res["message"] = "рядом с приёмником нет git-репозитория — обнови файлы вручную"
            return res
        f = _git(root, "fetch", "--quiet", remote, branch, git=git)
        if f.returncode != 0:
            res["message"] = "не удалось скачать обновление: " + _first_line(f.stderr)
            return res
        same = _git(root, "diff", "--quiet", ref, "--", *UPDATE_PATHS, *UPDATE_KEEP, git=git)
        if same.returncode == 0:
            res.update(ok=True, version=_read_version(root), message="уже последняя версия")
            return res
        if same.returncode != 1:
            res["message"] = "не удалось сравнить файлы: " + _first_line(same.stderr)
            return res
        names = _git(root, "diff", "--name-only", ref, "--", *UPDATE_PATHS, *UPDATE_KEEP, git=git).stdout.split()
        r = _git(root, "restore", "--source=" + ref, "--worktree", "--", *UPDATE_PATHS, *UPDATE_KEEP, git=git)
        if r.returncode != 0:
            res["message"] = "не удалось заменить файлы: " + _first_line(r.stderr)
            return res
        res.update(ok=True, changed=True, version=_read_version(root), tray_changed="monitor_server/tray.py" in names,
                   message="обновлено до v%s" % (_read_version(root) or "?"))
        return res
    except FileNotFoundError:
        res["message"] = "git не найден на этом ПК — обнови файлы вручную"
    except subprocess.TimeoutExpired:
        res["message"] = "git не ответил за %d с" % GIT_TIMEOUT_S
    except Exception as e:
        res["message"] = "ошибка обновления: %s" % e
    return res


def _read_version(root: Path) -> str:
    try:
        return (Path(root) / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def panel_url(host: str, port: int) -> str:
    """Адрес панели для кнопки «Открыть»: приёмник на «всех адресах» открываем через этот же ПК."""
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    return "http://%s:%d/" % (host, port)


def acquire_single_instance(name: str = MUTEX_NAME):
    """Один значок на ПК: второй запуск (двойной щелчок по ярлыку, повторный вход) тихо выходит. Возвращает «ручку» (держать до конца) или None."""
    if os.name != "nt":
        return object()
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW.restype = ctypes.c_void_p
    handle = k32.CreateMutexW(None, False, name)
    if not handle:
        return object()                       # не смогли создать — лучше запуститься, чем остаться без мониторинга
    if k32.GetLastError() == 183:             # ERROR_ALREADY_EXISTS
        k32.CloseHandle(ctypes.c_void_p(handle))
        return None
    return handle


class Supervisor:
    """Держит дочерний процесс (server.py): запускает, перезапускает по команде, поднимает после падения."""

    def __init__(self, cmd: list, cwd: Path, env: Optional[dict] = None, log_path: Optional[Path] = None,
                 backoff_base_s: float = 2.0, clock: Callable[[], float] = time.time):
        self.cmd, self.cwd, self.env, self.log_path = cmd, str(cwd), env, log_path
        self.backoff_base_s, self.clock = backoff_base_s, clock
        self.proc: Optional[subprocess.Popen] = None
        self.started_t = 0.0
        self.restarts = 0                      # сколько раз надзор или человек поднимал сервер заново
        self.fast_fails = 0                    # сколько раз подряд сервер упал, не дожив до OK_AFTER_S
        self._lock = threading.RLock()
        self._killed_by_us: Optional[subprocess.Popen] = None     # процесс, который мы сами убили (restart): это не падение
        self._wake = threading.Event()         # «проснись и проверь» — по команде restart/stop
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._logf = None

    # ---- состояние для подсказки значка
    @property
    def pid(self) -> Optional[int]:
        p = self.proc
        return p.pid if p is not None and p.poll() is None else None

    def status(self) -> str:
        if self._stop.is_set():
            return "остановлен"
        if self.pid is None:
            return "не запускается" if self.fast_fails >= 3 else "запускается"
        return "работает" if self.clock() - self.started_t >= 3 else "запускается"

    # ---- запуск и остановка
    def _spawn(self) -> None:
        kw = {"cwd": self.cwd, "env": self.env, "stdin": subprocess.DEVNULL}
        if os.name == "nt":
            kw["creationflags"] = CREATE_NO_WINDOW
        if self.log_path is not None:
            if self._logf is None or self._logf.closed:
                self._logf = open(self.log_path, "a", encoding="utf-8", buffering=1)
            kw["stdout"] = kw["stderr"] = self._logf
        self.proc = subprocess.Popen(self.cmd, **kw)
        self.started_t = self.clock()
        log.info("сервер запущен, pid %d", self.proc.pid)

    def _kill(self, proc: Optional[subprocess.Popen]) -> None:
        if proc is None or proc.poll() is not None:
            return
        if os.name == "nt":
            # в venv pythonw.exe — лаунчер, настоящий сервер его дочерний процесс: убиваем всё дерево, а не только лаунчер
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdin=subprocess.DEVNULL, capture_output=True,
                           creationflags=CREATE_NO_WINDOW, timeout=15)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="monitor-supervisor", daemon=True)
        self._thread.start()

    def restart(self) -> None:
        """Убить сервер — надзор сразу поднимет новый (перечитает код и настройки)."""
        with self._lock:
            log.info("перезапуск по команде")
            self.fast_fails = 0
            self.restarts += 1
            self._killed_by_us = self.proc
            self._kill(self.proc)
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            self._kill(self.proc)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=8)
        if self._logf is not None:
            self._logf.close()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                with self._lock:
                    if self._stop.is_set():
                        break
                    if self.proc is None or self.proc.poll() is not None:
                        self._spawn()
            except Exception:
                log.exception("не удалось запустить сервер")
                self.fast_fails += 1
            proc = self.proc
            # ждём конца процесса или команды; опрос раз в 0,5 с — без лишней нагрузки
            while not self._stop.is_set() and proc is not None and proc.poll() is None and not self._wake.is_set():
                self._wake.wait(0.5)
            self._wake.clear()
            if self._stop.is_set():
                break
            lived = self.clock() - self.started_t
            if proc is not None and proc.poll() is not None and proc is not self._killed_by_us:
                log.warning("сервер завершился сам (код %s) через %.0f с", proc.returncode, lived)
                self.restarts += 1
                self.fast_fails = self.fast_fails + 1 if lived < OK_AFTER_S else 0
                delay = min(BACKOFF_MAX_S, self.backoff_base_s * (2 ** max(0, self.fast_fails - 1))) if self.fast_fails else self.backoff_base_s
                self._stop.wait(delay)


# ---------------------------------------------------------------------------------------------------------------------- значок

def tooltip(sup: Supervisor, version: str) -> str:
    txt = "web_MVS панель — %s" % sup.status()
    if version:
        txt += " (v%s)" % version
    if sup.restarts:
        txt += ", перезапусков: %d" % sup.restarts
    return txt[:120]                            # подсказка значка в Windows — до 127 символов


def ask_yes_no(text: str, title: str) -> bool:
    if os.name != "nt":
        return True
    MB_YESNO, MB_ICONQUESTION, MB_TOPMOST, IDYES = 0x4, 0x20, 0x40000, 6
    return ctypes.windll.user32.MessageBoxW(None, text, title, MB_YESNO | MB_ICONQUESTION | MB_TOPMOST) == IDYES


def run_tray(sup: Supervisor, url: str, icon_path: Path, version: str) -> bool:
    """Показать значок и ждать «Выход». False — значок не получился (нет библиотеки): надзор продолжает работать без него."""
    try:
        import pystray
        from PIL import Image
    except Exception as e:
        log.error("значка в трее не будет (не хватает библиотеки: %s) — запусти setup.bat; сервер работает без значка", e)
        return False

    def open_panel(icon=None, item=None):
        webbrowser.open(url)

    def restart(icon=None, item=None):
        sup.restart()
        icon.notify("Сервер перезапускается — через пару секунд будет готов", "web_MVS панель")

    shown_version = [version]

    def update(icon=None, item=None):
        icon.notify("Проверяю обновление…", "web_MVS панель")
        res = update_files(HERE.parent)
        log.info("обновление по кнопке: %s", res["message"])
        if res["changed"]:
            shown_version[0] = res["version"]
            sup.restart()
            extra = " Значок обновится после следующего запуска." if res["tray_changed"] else ""
            icon.notify("Обновлено до v%s, сервер перезапускается.%s" % (res["version"] or "?", extra), "web_MVS панель")
        elif res["ok"]:
            icon.notify("Уже последняя версия (v%s)" % (res["version"] or "?"), "web_MVS панель")
        else:
            icon.notify("Не обновилось: " + res["message"], "web_MVS панель")

    def quit_(icon=None, item=None):
        if ask_yes_no("Остановить приёмник мониторинга?\nПока он остановлен, заводы не смогут присылать пульс, а в Telegram ничего не придёт.",
                      "web_MVS панель"):
            log.info("выход по команде из меню")
            sup.stop()
            icon.stop()

    menu = pystray.Menu(pystray.MenuItem("Открыть панель", open_panel, default=True),
                        pystray.MenuItem("Обновить", update),
                        pystray.MenuItem("Перезапустить сервер", restart),
                        pystray.Menu.SEPARATOR,
                        pystray.MenuItem("Выход", quit_))
    icon = pystray.Icon("web_MVS_panel", Image.open(icon_path), tooltip(sup, version), menu)

    def refresh():                              # подсказка значка («работает / перезапусков: N») обновляется раз в 5 с
        while not sup._stop.wait(5):
            icon.title = tooltip(sup, shown_version[0])

    threading.Thread(target=refresh, name="monitor-tray-title", daemon=True).start()
    icon.run()                                  # блокирует до icon.stop()
    return True


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[logging.FileHandler(HERE / "tray.log", encoding="utf-8")])


def main(argv: Optional[list] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    _setup_logging()
    guard = acquire_single_instance()
    if guard is None:
        log.info("значок уже запущен — выхожу")
        return 0
    import server                                # load_listen / read_version: тот же адрес и версия, что у сервера
    data = Path(os.environ.get("MONITOR_DATA") or HERE / "data")
    host, port = server.load_listen(data)
    version = server.read_version()
    exe = Path(sys.executable)
    py = exe.with_name("pythonw.exe") if exe.with_name("pythonw.exe").exists() else exe       # без окна, как и раньше в автозапуске
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", MONITOR_PARENT_PID=str(os.getpid()))
    sup = Supervisor([str(py), str(HERE / "server.py")], HERE, env=env, log_path=HERE / "service.log")
    log.info("надзор запущен: v%s, панель %s", version, panel_url(host, port))
    sup.start()
    try:
        try:
            shown = False if "--no-tray" in argv else run_tray(sup, panel_url(host, port), HERE / "static" / "icon-192.png", version)
        except Exception:
            log.exception("значок в трее упал — сервер продолжает работать без него")
            shown = False
        if not shown:
            while not sup._stop.wait(1):          # без значка просто живём, пока не остановят
                pass
    except KeyboardInterrupt:
        pass
    finally:
        sup.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
