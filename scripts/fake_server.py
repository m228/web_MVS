"""Фейк-стенд web_MVS одной командой: приложение + эмулятор платы/ПЛК + сайдкары на синтетике. Без железа, наружу ничего.

    python scripts/fake_server.py [--port 8011] [--cycle 30] [--stage 5] [--no-sidecars] [--data-dir PATH] [--open]

Что поднимается (логи — <data>/fake_logs/*.log):
  plate_sim (Modbus 127.0.0.1:15020: плата микроскопа и ПЛК), cv_service :8765 (заглушка-детектор), video_service :8767,
  motion_service :8768 (на синтетических кадрах), level_service :8770, и само приложение на --port.
Данные — .fake_data/ (создаётся при первом запуске, повторный запуск ничего не затирает; --reset стирает его).
Ctrl+C останавливает всё. Подробнее — tasks/todo_fake-mode.md и README в fake_mode.py.
"""
import argparse
import atexit
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import fake_mode  # noqa: E402


def _kill_children_with_parent():
    """Windows: положить себя в job с KILL_ON_JOB_CLOSE — если этот процесс убьют жёстко (закрыли окно, TerminateProcess),
    дочерние сервисы не останутся висеть на портах. Дети наследуют job. Не вышло — не страшно (остаётся terminate в finally)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        class _Basic(ctypes.Structure):
            _fields_ = [("a", ctypes.c_int64), ("b", ctypes.c_int64), ("LimitFlags", wintypes.DWORD), ("c", ctypes.c_size_t),
                        ("d", ctypes.c_size_t), ("e", wintypes.DWORD), ("f", ctypes.c_size_t), ("g", wintypes.DWORD),
                        ("h", wintypes.DWORD)]

        class _IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in "abcdef"]

        class _Ext(ctypes.Structure):
            _fields_ = [("Basic", _Basic), ("IO", _IO), ("p1", ctypes.c_size_t), ("p2", ctypes.c_size_t),
                        ("p3", ctypes.c_size_t), ("p4", ctypes.c_size_t)]

        k32 = ctypes.windll.kernel32
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        job = k32.CreateJobObjectW(None, None)
        info = _Ext()
        info.Basic.LimitFlags = 0x2000                       # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info))
        k32.AssignProcessToJobObject(wintypes.HANDLE(job), k32.GetCurrentProcess())
        globals()["_JOB"] = job                              # держим дескриптор живым до конца процесса
    except Exception:
        pass


def _spawn(name, args, cwd, env, logdir):
    log = open(logdir / ("%s.log" % name), "ab")
    log.write(("\n=== %s · старт %s ===\n" % (name, time.strftime("%Y-%m-%d %H:%M:%S"))).encode("utf-8"))
    log.flush()
    flags = 0x08000000 if os.name == "nt" else 0        # CREATE_NO_WINDOW
    return subprocess.Popen([sys.executable] + args, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=log,
                            stderr=subprocess.STDOUT, creationflags=flags)


def main():
    for stream in (sys.stdout, sys.stderr):              # консоль Windows бывает cp1251: русский текст не должен ронять запуск
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=fake_mode.APP_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--stage", type=int, default=5, help="стадия варки в «ПЛК» (по умолчанию 5 — идёт проба)")
    ap.add_argument("--cycle", type=float, default=0.0, help="секунд на стадию: стадии 1..21 по кругу (0 — фиксирована)")
    ap.add_argument("--no-sidecars", action="store_true", help="не поднимать cv/video/motion/level")
    ap.add_argument("--data-dir", default="", help="каталог данных стенда (по умолчанию <репо>/.fake_data)")
    ap.add_argument("--reset", action="store_true", help="стереть каталог данных стенда и начать с чистого")
    ap.add_argument("--open", action="store_true", help="открыть браузер")
    args = ap.parse_args()

    if args.data_dir:
        os.environ["WEB_MVS_DATA_DIR"] = args.data_dir
    data = Path(os.environ.get("WEB_MVS_DATA_DIR") or fake_mode.default_data_dir()).resolve()
    if data == REPO:
        sys.exit("Каталог данных стенда не может быть корнем репозитория: %s" % data)
    if args.reset and data.exists():
        shutil.rmtree(data)
    os.environ["WEB_MVS_DATA_DIR"] = str(data)
    fake_mode.prepare_env()
    logdir = data / "fake_logs"
    logdir.mkdir(parents=True, exist_ok=True)

    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "PYTHONPATH")}
    env.update(PYTHONUNBUFFERED="1", WEB_MVS_FAKE="1", WEB_MVS_DATA_DIR=str(data))
    procs = []
    _kill_children_with_parent()
    atexit.register(lambda: [p.terminate() for p in procs if p.poll() is None])

    procs.append(_spawn("plate_sim", ["scripts/plate_sim.py", "--host", "127.0.0.1", "--port", str(fake_mode.PLATE_PORT),
                                      "--stage", str(args.stage), "--cycle", str(args.cycle)], REPO, env, logdir))
    if not args.no_sidecars:
        procs.append(_spawn("cv_service", ["server.py"], REPO / "cv_service", {**env, "CV_PORT": "8765", "CV_HOST": "127.0.0.1"}, logdir))
        try:
            import imageio_ffmpeg  # noqa: F401  (video_service сводит ролики через него; в общем venv его может не быть)
            procs.append(_spawn("video_service", ["server.py"], REPO / "video_service",
                                {**env, "VIDEO_PORT": "8767", "VIDEO_HOST": "127.0.0.1"}, logdir))
        except ImportError:
            print("video_service пропущен: нет imageio_ffmpeg (pip install imageio-ffmpeg, если нужна вкладка видео)", flush=True)
        procs.append(_spawn("motion_service", ["scripts/fake_motion_run.py"], REPO,
                            {**env, "MOTION_PORT": "8768", "MOTION_HOST": "127.0.0.1", "MOTION_DATA": str(data / "motion_data")}, logdir))
        if (REPO / "level_service" / "server.py").is_file():
            procs.append(_spawn("level_service", ["server.py"], REPO / "level_service",
                                {**env, "LEVEL_PORT": "8770", "LEVEL_HOST": "127.0.0.1"}, logdir))

    print("web_MVS FAKE -> http://%s:%d   (данные: %s)" % (args.host, args.port, data), flush=True)
    print("плата/ПЛК: 127.0.0.1:%d, сайдкары: %s, логи: %s" % (fake_mode.PLATE_PORT, "нет" if args.no_sidecars else "cv/video/motion/level", logdir),
          flush=True)
    if args.open:
        webbrowser.open("http://%s:%d" % (args.host, args.port))
    os.chdir(REPO)
    try:
        import uvicorn
        uvicorn.run("app:app", host=args.host, port=args.port, log_level="warning", ws="none")
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()


if __name__ == "__main__":
    main()
