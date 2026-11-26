"""videorec — запись видео со звуком камер на диск: материал для разметки звуков и обучения модели.

На камеру — свой процесс ffmpeg (отдельное подключение, анализ движения он не трогает): по умолчанию второй поток камеры,
по желанию только вырезанная область, 5 кадров/с, H.264; звук камеры копируется в файл как есть, без пережатия.
Файлы по 10 минут: data\\video_rec\\<камера>\\<ГГГГММДД_ЧЧММСС>.mkv — имя это местное время начала куска (по нему запись
сопоставляется с базой движения). Оборвалась связь — ffmpeg перезапускается сам. Все записи вместе держатся в пределах
лимита: старые куски удаляются. Настройки — data\\video_rec.json, отдельно от motion_config.json: включение записи и смена
области не перезапускают анализ.
Режим «только вокруг событий» (keep = events): закрытый кусок разбирает motion_trim — остаются видео вокруг движения и громких
звуков и весь звук. Режется только записанное после включения режима.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

import motion_trim as trim

log = logging.getLogger("motion")

SEGMENT_S = 600                  # длина куска, с
CRF = 28                         # качество H.264: больше число — меньше файл
DEFAULT_LIMIT_GB = 20.0
MIN_FREE_BYTES = 2 << 30         # свободного на диске меньше — запись встаёт (чужие файлы не наше дело, свои сверх лимита не трогаем)
RESUME_EXTRA_BYTES = 1 << 29     # а возобновляется, когда свободно на полгигабайта больше порога
STALL_S = 40.0                   # кадров нет дольше — ffmpeg завис, перезапускаем
RETRY_MIN_S, RETRY_MAX_S = 5.0, 60.0
PURGE_EVERY_S = 60.0
REC_KEYS = ("on", "stream", "area", "fps")             # от них зависит команда ffmpeg: смена остальных запись не перезапускает
CAM_DEFAULTS = {"on": False, "stream": "sub", "area": None, "fps": 5,
                "keep": "all", "pre_s": 10, "post_s": 20, "sound_db": 20, "full_h": 24}
TRIM_NUMBERS = {"pre_s": (0, 120, "секунд до события"), "post_s": (0, 300, "секунд после события"),
                "sound_db": (0, 60, "порог звука, дБ (0 — звук не слушать)"), "full_h": (0, 720, "часов хранить полные куски")}
TRIM_EVERY_S = 10.0
TRIM_TRIES = 3                   # столько раз пробуем нарезать кусок, потом оставляем целиком
# удаляем только то, что назвали сами
FILE_RE = re.compile(r"^\d{8}_\d{6}(?:(?:_full|_kept|_clip)?\.mkv|_audio\.mka)$")


def mask(text: str) -> str:
    """Логин и пароль из rtsp-адреса не должны попадать в журнал и на страницу. Режем с запасом — до последней «@» строки:
    в пароле бывают пробел, кавычка, «/» и сама «@»; бывает пароль и параметром адреса (password=…)."""
    out = re.sub(r"(?<=://)[^\n]*@", "***@", str(text))
    return re.sub(r"(?i)\b(password|passwd|pwd)=[^&\s'\"]+", r"\1=***", out)


def find_ffmpeg() -> Optional[str]:
    """MOTION_FFMPEG → ffmpeg из пакета imageio-ffmpeg (requirements.txt) → из PATH."""
    env = os.environ.get("MOTION_FFMPEG")
    if env:
        return env if Path(env).is_file() else None
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        return shutil.which("ffmpeg")


def sub_url(url: str) -> tuple:
    """Адрес второго (лёгкого) потока: Dahua subtype=1, Hikvision Channels/102. -> (адрес, распознан ли он)."""
    if not str(url).lower().startswith("rtsp"):
        return url, True
    if re.search(r"subtype=\d+", url):
        return re.sub(r"subtype=\d+", "subtype=1", url, count=1), True
    m = re.search(r"(?i)/streaming/channels/\d+?0([123])(?=$|[/?])", url)
    if m:
        return (url[:m.start(1)] + "2" + url[m.end(1):]) if m.group(1) == "1" else url, True
    return url, False


def is_sub_url(url: str) -> bool:
    """В самом адресе камеры уже стоит второй (или третий) поток."""
    return bool(re.search(r"(?i)subtype=[1-9]|/streaming/channels/\d+?0[23](?=$|[/?])", str(url)))


def normalize_camera(raw: dict) -> dict:
    """Настройки записи одной камеры; ValueError — с текстом по-русски."""
    out = dict(CAM_DEFAULTS)
    out["on"] = bool(raw.get("on", False))
    out["stream"] = raw.get("stream", "sub")
    if out["stream"] not in ("sub", "main"):
        raise ValueError("поток записи: sub (второй) или main (основной)")
    try:
        out["fps"] = int(raw.get("fps", 5))
    except (TypeError, ValueError):
        raise ValueError("кадров в секунду — целое число от 1 до 25")
    if not 1 <= out["fps"] <= 25:
        raise ValueError("кадров в секунду — от 1 до 25")
    area = raw.get("area")
    if area is not None:
        try:
            x, y, w, h = (round(float(v), 4) for v in area)
        except (TypeError, ValueError):
            raise ValueError("область записи — четыре числа: x, y, ширина, высота в долях кадра")
        if not (0 <= x < 1 and 0 <= y < 1 and w >= 0.05 and h >= 0.05 and x + w <= 1.0001 and y + h <= 1.0001):
            raise ValueError("область записи выходит за кадр или меньше 5 % его стороны")
        out["area"] = [x, y, w, h]
    out["keep"] = raw.get("keep", "all")
    if out["keep"] not in ("all", "events"):
        raise ValueError("что хранить: all (всё) или events (видео только вокруг событий)")
    for key, (lo, hi, title) in TRIM_NUMBERS.items():
        try:
            v = float(raw.get(key, CAM_DEFAULTS[key]))
        except (TypeError, ValueError):
            raise ValueError("%s — число" % title)
        if not lo <= v <= hi:
            raise ValueError("%s — от %d до %d" % (title, lo, hi))
        out[key] = int(v) if v == int(v) else v
    if out["keep"] == "events" and raw.get("trim_from") is not None:
        try:
            out["trim_from"] = float(raw["trim_from"])    # куски, начатые раньше, не режем: их писали «целиком»
        except (TypeError, ValueError):
            pass
    return out


def _rec_part(cfg: dict) -> tuple:
    return tuple(cfg.get(k) for k in REC_KEYS)


def build_cmd(ffmpeg: str, url: str, cfg: dict, out_dir: Path, segment_s: int = SEGMENT_S) -> list:
    """Командная строка ffmpeg для одной камеры."""
    vf = ["fps=%d" % cfg["fps"]]
    if cfg["area"]:
        x, y, w, h = cfg["area"]
        # доли кадра, а не пиксели: разрешение второго потока другое; стороны — чётные (так требует yuv420p)
        vf.append("crop=trunc(iw*%s/2)*2:trunc(ih*%s/2)*2:trunc(iw*%s/2)*2:trunc(ih*%s/2)*2" % (w, h, x, y))
    else:
        vf.append("crop=trunc(iw/2)*2:trunc(ih/2)*2")
    low = str(url).lower()
    if low.startswith(("rtsp://", "rtsps://")):
        src = ["-rtsp_transport", "tcp", "-timeout", "10000000"]
    elif "://" in low:
        src = []
    else:
        src = ["-re"]                 # файл читаем в реальном времени, как живой источник
    return [ffmpeg, "-hide_banner", "-loglevel", "info", "-nostats", "-progress", "pipe:1", *src, "-i", url,
            "-map", "0:v:0", "-map", "0:a:0?", "-vf", ",".join(vf),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(CRF), "-pix_fmt", "yuv420p",
            "-tune", "zerolatency",                               # кодер не копит кадры у себя: убили процесс — потеряны не десятки секунд
            "-force_key_frames", "expr:gte(t,n_forced*2)",       # кусок режется только по опорному кадру
            "-c:a", "copy",
            "-f", "segment", "-segment_format", "matroska", "-segment_format_options", "flush_packets=1",     # сразу на диск
            "-segment_time", str(int(segment_s)),
            "-segment_atclocktime", "1", "-reset_timestamps", "1", "-strftime", "1",
            str(Path(out_dir) / "%Y%m%d_%H%M%S.mkv")]


# ─────────── ffmpeg не должен пережить сервис (Windows: job object, закрытый вместе с процессом) ───────────
_job = None
_job_lock = threading.Lock()


def _kill_with_parent(proc: subprocess.Popen):
    """Сервис убили (обновление, диспетчер задач) — Windows сама закрывает job и гасит ffmpeg, иначе он писал бы диск вечно."""
    global _job
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("IoInfo", ctypes.c_uint64 * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        with _job_lock:
            if _job is None:
                job = k32.CreateJobObjectW(None, None)
                info = Extended()
                info.Basic.LimitFlags = 0x2000              # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                if not job or not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                    raise ctypes.WinError(ctypes.get_last_error())
                _job = job
            if not k32.AssignProcessToJobObject(_job, int(proc._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
    except Exception as e:  # noqa: BLE001 — запись важнее подстраховки
        log.warning("запись видео: ffmpeg не привязан к сервису (%s)", e)


class CamRecorder(threading.Thread):
    """Держит ffmpeg одной камеры: запустил, следит за кадрами, перезапускает после обрыва."""

    def __init__(self, cid: str, url: str, cfg: dict, out_dir: Path, ffmpeg: str, segment_s: int = SEGMENT_S, stall_s: float = STALL_S):
        super().__init__(daemon=True, name="videorec-" + cid)
        self.cid, self.url, self.cfg, self.out_dir = cid, url, cfg, Path(out_dir)
        self.ffmpeg, self.segment_s, self.stall_s = ffmpeg, segment_s, stall_s
        self._halt = threading.Event()
        self.audio: Optional[str] = None       # описание звуковой дорожки; "" — камера звук не отдаёт; None — ещё неизвестно
        self.error = ""
        self._frames = 0
        self._last_frame = 0.0                 # monotonic последнего прироста кадров; 0 — в этом запуске кадров ещё не было
        self._tail: deque = deque(maxlen=6)

    def stop(self):
        self._halt.set()

    @property
    def running(self) -> bool:
        """Кадры идут прямо сейчас."""
        return self.is_alive() and self._last_frame > 0 and time.monotonic() - self._last_frame < 10.0

    def _read_stderr(self, pipe):
        outputs = False
        for raw in iter(pipe.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if not outputs:
                m = re.search(r"Stream #0:\d+.*?: Audio: (.+)", line)
                if m:
                    parts = [p.strip() for p in m.group(1).split(",")]
                    self.audio = ", ".join([parts[0].split()[0]] + parts[1:3])
                if line.startswith("Output #0"):
                    outputs = True
                    if self.audio is None:
                        self.audio = ""
            if line and re.search(r"(?i)error|fail|invalid|refused|timed out|not enough|unauthorized|denied|no such", line):
                # «[rtsp @ 000001e6…]»: адрес в памяти каждый запуск новый — без него одна и та же ошибка пишется в журнал один раз
                self._tail.append(mask(re.sub(r"\[([\w,]+) @ (?:0x)?[0-9a-fA-F]+\]\s*", r"\1: ", line)))

    def _read_progress(self, pipe):
        for raw in iter(pipe.readline, b""):
            if raw.startswith(b"frame="):
                try:
                    n = int(raw[6:])
                except ValueError:
                    continue
                if n > self._frames:
                    self._frames, self._last_frame = n, time.monotonic()
                    self.error = ""

    def _run_once(self) -> bool:
        """Один запуск ffmpeg до обрыва или остановки. -> шли ли кадры."""
        self._frames, self._last_frame, self.audio = 0, 0.0, None
        self._tail.clear()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))
        proc = subprocess.Popen(build_cmd(self.ffmpeg, self.url, self.cfg, self.out_dir, self.segment_s),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
        _kill_with_parent(proc)
        readers = [threading.Thread(target=self._read_stderr, args=(proc.stderr,), daemon=True),
                   threading.Thread(target=self._read_progress, args=(proc.stdout,), daemon=True)]
        for t in readers:
            t.start()
        started, stalled = time.monotonic(), False
        try:
            while proc.poll() is None and not self._halt.wait(0.5):
                if time.monotonic() - (self._last_frame or started) > self.stall_s:
                    stalled = True
                    break
        finally:
            if proc.poll() is None:
                if not stalled:
                    try:                        # «q» — ffmpeg сам дописывает и закрывает файл
                        proc.stdin.write(b"q")
                        proc.stdin.close()
                        proc.wait(8)
                    except (OSError, ValueError, subprocess.TimeoutExpired):
                        pass
                if proc.poll() is None:
                    proc.kill()
                    try:
                        proc.wait(5)
                    except subprocess.TimeoutExpired:
                        pass
            for t in readers:
                t.join(2)
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass
        if not self._halt.is_set():
            if stalled:
                self.error = "кадров нет %d с — перезапускаю" % int(self.stall_s)
            else:
                self.error = (" · ".join(list(self._tail)[-2:]) or "ffmpeg завершился (код %s)" % proc.returncode)[:300]
        return self._frames > 0

    def run(self):
        delay, said = RETRY_MIN_S, None
        while not self._halt.is_set():
            t0 = time.monotonic()
            try:
                got = self._run_once()
            except Exception as e:  # noqa: BLE001 — поток записи не должен умирать молча
                got, self.error = False, mask("запись не запустилась: %s" % e)
            if self._halt.is_set():
                break
            if got and time.monotonic() - t0 > RETRY_MAX_S:
                delay, said = RETRY_MIN_S, None
            if self.error != said:              # мёртвая камера не должна забивать журнал одной и той же строкой
                said = self.error
                log.warning("[%s] запись видео прервалась: %s", self.cid, self.error)
            self._halt.wait(delay)
            delay = min(delay * 2, RETRY_MAX_S)
        self._last_frame = 0.0


class VideoRec:
    """Настройки записи по камерам + их потоки + лимит места."""

    def __init__(self, data_dir: Path, cameras: Callable[[], list], ffmpeg: Callable[[], Optional[str]] = find_ffmpeg,
                 segment_s: int = SEGMENT_S, tick_s: float = 2.0, activity: Optional[Callable] = None,
                 settle_s: float = 60.0):
        self.dir = Path(data_dir)
        self.root = self.dir / "video_rec"
        self.path = self.dir / "video_rec.json"
        self.cameras = cameras                  # () -> камеры из настроек сервиса движения (id, url)
        self.ffmpeg = ffmpeg
        self.segment_s = segment_s
        self.tick_s = tick_s
        self.limit_gb = DEFAULT_LIMIT_GB
        self.limit_known = True                 # False — лимит человека потерян (файл настроек испорчен или пропал): ничего не удаляем
        self.settings: dict = {}
        self.recorders: dict[str, CamRecorder] = {}
        self._lock = threading.RLock()
        self._halt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._blocked = ""                      # почему запись не идёт ни у кого (нет ffmpeg, мало места)
        self.activity = activity                # (камера, с, по) -> строки тренда движения или None (окон движения нет)
        self.settle_s = settle_s                # кусок разбираем не раньше, чем через столько после последней записи в него
        self._trim_thread: Optional[threading.Thread] = None
        self._trimming: set = set()             # файлы в разборе: лимит места их не трогает
        self._trim_fail: dict = {}              # кусок -> сколько раз нарезка не удалась
        self._trim_skip: set = set()            # занятые кем-то куски: до перезапуска сервиса не трогаем, очередь идёт дальше
        self.trim_info: dict = {}               # камера -> {"text", "error"} для панели

    # --- настройки ---
    def load(self):
        try:
            d = json.loads(self.path.read_text(encoding="utf-8-sig"))
            cameras = dict(d.get("cameras") or {})
        except FileNotFoundError:
            self.limit_known = not self.files()             # настроек нет, а записи есть — лимит мог быть любым
        except (OSError, ValueError, TypeError, AttributeError) as e:
            self.limit_known = False
            log.warning("video_rec.json не прочитан (%s) — запись видео выключена, старые куски не удаляются", e)
        else:
            try:
                self.limit_gb, self.limit_known = min(max(float(d["limit_gb"]), 0.1), 10000.0), True
            except (KeyError, TypeError, ValueError):
                self.limit_known = False
            stamped = False
            for cid, raw in cameras.items():
                try:
                    s = self.settings[str(cid)] = normalize_camera(raw)
                except (ValueError, AttributeError) as e:
                    log.warning("[%s] настройки записи видео не приняты: %s", cid, e)
                    continue
                if s["keep"] == "events" and "trim_from" not in s:      # режим вписали в файл руками: старое не режем
                    s["trim_from"], stamped = time.time(), True
            if stamped:
                self._save()

    def _save(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"limit_gb": self.limit_gb if self.limit_known else None, "cameras": self.settings}, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())                            # отключили питание — файл не должен остаться пустым
        os.replace(tmp, self.path)

    def set_camera(self, cid: str, patch: dict) -> dict:
        """Поменять настройки камеры (только присланные поля). ValueError — плохие значения, ничего не тронуто."""
        with self._lock:
            raw = dict(self.settings.get(cid) or CAM_DEFAULTS)
            was = raw.get("keep", "all")
            raw.update({k: v for k, v in patch.items() if k in CAM_DEFAULTS})
            if raw.get("keep") == "events" and was != "events":
                raw["trim_from"] = time.time()
            self.settings[cid] = normalize_camera(raw)
            self._save()
        self.sync()
        return self.status()

    def set_limit(self, gb) -> dict:
        try:
            gb = float(gb)
        except (TypeError, ValueError):
            raise ValueError("лимит места — число гигабайт")
        if not 1 <= gb <= 10000:
            raise ValueError("лимит места — от 1 до 10000 ГБ")
        with self._lock:
            self.limit_gb, self.limit_known = gb, True
            self._save()
        self.purge()
        return self.status()

    # --- файлы ---
    def cam_dir(self, cid: str) -> Path:
        return self.root / (re.sub(r"[^\w.-]", "_", str(cid)).lstrip(".") or "cam")

    def files(self) -> list:
        """Все куски: [(имя, папка, размер)] — старые первыми."""
        out = []
        if not self.root.is_dir():
            return out
        for d in self.root.iterdir():
            if not d.is_dir():
                continue
            for f in d.iterdir():
                if FILE_RE.match(f.name):
                    try:
                        out.append((f.name, d, f.stat().st_size))
                    except OSError:
                        pass
        return sorted(out, key=lambda x: (x[0], x[1].name))

    def _writing(self, files: list) -> set:
        """{(папка, имя)} кусков, в которые идёт запись: самый новый неразобранный у каждой камеры с включённой записью."""
        newest = {d: name for name, d, _ in files if trim.RAW_RE.match(name)}
        return {(self.cam_dir(c), newest.get(self.cam_dir(c))) for c in list(self.recorders)}

    def purge(self) -> int:
        """Держать все записи в пределах лимита: сначала удаляются полные копии уже нарезанных кусков, потом всё по старшинству
        (пишущийся и разбираемый сейчас не трогаем). -> сколько удалено."""
        with self._lock:
            if not self.limit_known:
                return 0
            files = self.files()
            total = sum(s for _, _, s in files)
            busy = self._writing(files)
            n = 0
            for name, d, size in sorted(files, key=lambda x: not x[0].endswith("_full.mkv")):    # порядок внутри групп — старые первыми
                if total <= self.limit_gb * (1 << 30):
                    break
                if (d, name) in busy or d / name in self._trimming:
                    continue
                try:
                    (d / name).unlink()
                    total -= size
                    n += 1
                except OSError as e:
                    log.warning("старую запись %s не удалить: %s", name, e)
            if n:
                log.info("запись видео: удалено старых кусков: %d (лимит %g ГБ)", n, self.limit_gb)
            return n

    def _free(self) -> Optional[int]:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            return shutil.disk_usage(str(self.dir)).free
        except OSError:
            return None

    # --- потоки ---
    def sync(self):
        """Привести идущие записи к настройкам: лишние и изменившиеся остановить, недостающие запустить."""
        with self._lock:
            if self._halt.is_set():
                return
            # выключенное КЗ камеры выключает и запись: блок записи виден только при включённом, иначе её было бы не остановить
            urls = {c["id"]: c.get("url") for c in self.cameras() if c.get("enabled", True)}
            wanted = {}
            for cid, s in self.settings.items():
                url = urls.get(cid)
                if s["on"] and url:
                    wanted[cid] = (sub_url(url)[0] if s["stream"] == "sub" else url, s)
            exe, free, blocked = None, self._free(), ""
            if wanted:
                exe = self.ffmpeg()
                need = MIN_FREE_BYTES + (RESUME_EXTRA_BYTES if self._blocked else 0)   # у самой границы не щёлкать «стоп/старт»
                if not exe:
                    blocked = "нет ffmpeg — запустите motion_service\\setup.bat"
                elif free is not None and free < need:
                    blocked = "на диске меньше %d ГБ свободного места — запись остановлена" % (MIN_FREE_BYTES >> 30)
            if blocked != self._blocked:
                self._blocked = blocked
                if blocked:
                    log.warning("запись видео: %s", blocked)
            if blocked:
                wanted = {}
            old = [cid for cid, r in self.recorders.items()
                   if cid not in wanted or (r.url, _rec_part(r.cfg)) != (wanted[cid][0], _rec_part(wanted[cid][1])) or not r.is_alive()]
            self._stop(old)
            for cid, (url, s) in wanted.items():
                if cid not in self.recorders:
                    r = CamRecorder(cid, url, dict(s), self.cam_dir(cid), exe, self.segment_s)
                    self.recorders[cid] = r
                    r.start()
                    log.info("[%s] запись видео включена: %s", cid, mask(url))

    def _stop(self, cids: list):
        gone = [self.recorders.pop(c) for c in cids if c in self.recorders]
        for r in gone:
            r.stop()
        for r in gone:
            r.join(20)
            log.info("[%s] запись видео остановлена", r.cid)

    def _loop(self):
        next_purge = 0.0
        while not self._halt.is_set():
            try:
                self.sync()
                if time.monotonic() >= next_purge:
                    next_purge = time.monotonic() + PURGE_EVERY_S
                    self.purge()
            except Exception as e:  # noqa: BLE001
                log.warning("запись видео: сбой присмотра: %s", mask(e))
            self._halt.wait(self.tick_s)

    # --- нарезка ---
    def _next_piece(self, cid: str, cfg: dict) -> Optional[tuple]:
        """Самый старый кусок камеры, готовый к разбору: -> (кусок, предыдущий файл, следующий файл) или None."""
        folder = self.cam_dir(cid)
        files = self.files()
        mine = [name for name, d, _ in files if d == folder]
        raw = [n for n in mine if trim.RAW_RE.match(n)]
        whole = [n for n in mine if not n.endswith("_clip.mkv")]       # файлы со звуком всего куска
        writing = {name for d, name in self._writing(files) if d == folder}
        for name in raw:
            if folder / name in self._trim_skip:
                continue
            try:
                if trim.start_time(name) < cfg.get("trim_from", float("inf")):
                    continue
                age = time.time() - (folder / name).stat().st_mtime
            except (OSError, ValueError, OverflowError):
                continue
            # пишущийся кусок — только если в него давно ничего не пришло (камера пропала): он уже не вырастет
            ready = max(self.settle_s, float(cfg.get("pre_s", 0)) + 10.0)     # начало следующего куска нужно на «до события» вперёд
            if age < ready or (name in writing and age < self.segment_s + ready):
                continue
            prev = [n for n in whole if n[:15] < name[:15]]
            nxt = [n for n in whole if n[:15] > name[:15]]
            return folder / name, folder / prev[-1] if prev else None, folder / nxt[0] if nxt else None
        return None

    def _expire_full(self, cid: str, cfg: dict):
        """Полные копии нарезанных кусков старше заданного срока."""
        folder = self.cam_dir(cid)
        with self._lock:
            for name, d, _ in self.files():
                if d != folder or not name.endswith("_full.mkv") or d / name in self._trimming:
                    continue
                try:
                    if time.time() - (d / name).stat().st_mtime > float(cfg["full_h"]) * 3600:
                        (d / name).unlink()
                        log.info("[%s] запись видео: срок полного куска %s вышел — удалён", cid, name)
                except OSError as e:
                    log.warning("[%s] полный кусок %s не удалить: %s", cid, name, e)

    def trim_once(self) -> int:
        """Один проход нарезки: по одному куску на камеру. -> сколько кусков разобрано."""
        done = 0
        for cid, cfg in list(self.settings.items()):
            if cfg.get("keep") != "events" or self._halt.is_set():
                continue
            self._expire_full(cid, cfg)
            job = self._next_piece(cid, cfg)
            if not job:
                continue
            exe = self.ffmpeg()
            if not exe:
                self.trim_info[cid] = {"text": "", "error": "нарезка стоит: нет ffmpeg"}
                continue
            piece, prev, nxt = job
            media = trim.Media(exe, self._halt, _kill_with_parent)
            activity = (lambda a, b, c=cid: self.activity(c, a, b)) if self.activity else None
            self._trimming = {x for x in job if x}
            try:
                r = trim.trim_piece(media, piece, cfg, activity, prev, nxt, commit_lock=self._lock,
                                    current=lambda c=cid: self.settings.get(c) or {})
            except trim.Aborted:
                if self._halt.is_set():
                    return done
                continue                              # режим выключили, пока кусок разбирался
            except Exception as e:  # noqa: BLE001 — кусок цел, пробуем ещё; не вышло TRIM_TRIES раз — оставляем целиком
                tries = self._trim_fail[piece] = self._trim_fail.get(piece, 0) + 1
                why = mask(e)[:200]
                log.warning("[%s] нарезка куска %s не удалась (%d из %d): %s", cid, piece.name, tries, TRIM_TRIES, why)
                self.trim_info[cid] = {"text": "", "error": "нарезка %s не удалась: %s" % (piece.name, why)}
                if tries >= TRIM_TRIES:
                    try:
                        with self._lock:
                            trim.keep_whole(piece)
                        self._trim_fail.pop(piece, None)
                    except (OSError, ValueError) as err:
                        self._trim_skip.add(piece)
                        log.warning("[%s] кусок %s занят и не переименовывается (%s) — оставляю как есть до перезапуска", cid, piece.name, err)
                continue
            finally:
                self._trimming = set()
            done += 1
            self._trim_fail.pop(piece, None)
            if r["result"] == "kept":
                text = "кусок %s оставлен целиком: %s" % (piece.name[:15], r["reason"])
            else:
                text = "кусок %s: отрезков с событиями %d, оставлено %d с из %d с" % (piece.name[:15], len(r["clips"]), r["left_s"], r["dur_s"])
            self.trim_info[cid] = {"text": text, "error": ""}
            log.info("[%s] нарезка: %s", cid, text)
        return done

    def _trim_loop(self):
        for f in self.root.glob("*/*.part"):          # остатки нарезки, прерванной выключением
            try:
                f.unlink()
            except OSError:
                pass
        while not self._halt.is_set():
            try:
                busy = self.trim_once()
            except Exception as e:  # noqa: BLE001
                busy = 0
                log.warning("запись видео: сбой нарезки: %s", mask(e))
            self._halt.wait(1.0 if busy else TRIM_EVERY_S)

    def start(self):
        self._halt.clear()
        self.load()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="videorec")
        self._thread.start()
        self._trim_thread = threading.Thread(target=self._trim_loop, daemon=True, name="videotrim")
        self._trim_thread.start()

    def shutdown(self):
        self._halt.set()
        if self._trim_thread:
            self._trim_thread.join(15)
        if self._thread:
            self._thread.join(30)
        with self._lock:
            self._stop(list(self.recorders))

    def status(self) -> dict:
        files = self.files()
        per: dict = {}
        for name, d, size in files:
            p = per.setdefault(d, [0, 0, ""])
            p[0] += 1
            p[1] += size
            p[2] = name
        cams = {}
        recorders = dict(self.recorders)
        for cam in self.cameras():
            cid, url = cam["id"], cam.get("url")
            s = self.settings.get(cid) or CAM_DEFAULTS
            r = recorders.get(cid)
            n, size, last = per.get(self.cam_dir(cid), (0, 0, ""))
            mine = [name for name, d, _ in files if d == self.cam_dir(cid)]
            counts = {"clips": sum(x.endswith("_clip.mkv") for x in mine), "full": sum(x.endswith("_full.mkv") for x in mine)}
            info = self.trim_info.get(cid) if s.get("keep") == "events" else None
            warn = []
            if s["stream"] == "sub" and url and not sub_url(url)[1]:
                warn.append("второй поток в этом адресе не распознан — пишется поток из настроек камеры")
            if s["stream"] == "main" and url and is_sub_url(url):
                warn.append("в адресе камеры уже стоит второй поток — «основной» пишет его")
            if r and r.audio == "":
                warn.append("камера не отдаёт звук в этом потоке: включите на ней «Видео и звук» для него")
            if not self.limit_known:
                warn.append("настройки записи были потеряны: старые куски не удаляются, пока лимит не вписан заново")
            error = ""
            if s["on"]:
                error = "КЗ камеры выключено — запись стоит" if not cam.get("enabled", True) else self._blocked
            cams[cid] = {**s, "running": bool(r and r.running), "error": error or (r.error if r else ""),
                         "audio": r.audio if r else None, "warn": " · ".join(warn), "files": n, "size_mb": round(size / 1048576.0, 1),
                         "last_file": last, "folder": str(self.cam_dir(cid)), **counts,
                         "trim": (info or {}).get("text", ""), "trim_error": (info or {}).get("error", "")}
        free = self._free()
        return {"folder": str(self.root), "limit_gb": self.limit_gb, "limit_known": self.limit_known, "segment_s": self.segment_s,
                "total_mb": round(sum(s for _, _, s in files) / 1048576.0, 1), "free_gb": None if free is None else round(free / (1 << 30), 1),
                "cameras": cams}
