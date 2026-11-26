"""sound — этапы известковой печи по звуку камеры: ночью картинка тёмная, а камни слышно так же, как днём.

На камеру с вычислением «этапы по звуку» — свой ffmpeg, тянущий ТОЛЬКО звук (16 кГц моно), и детектор:
    уровень    энергия 2–8 кГц (удары камней о железо) блоками по 0,1 с, сглаженная за 0,5 с;
    фон        20-й перцентиль уровня за последние 5 минут — громкость камеры и шум площадки учитываются сами;
    событие    уровень выше фона на threshold_db (30) не меньше 1 с; кончилось — тише 2,5 с;
    этап       первое событие — сброс; следующее через 8–45 с после него — засыпка; прокрутку по звуку не слышно.
Код этапа держится, пока идёт звук, и не меньше 3 с (сценарий пишет в ПЛК раз в секунду). Звук громче порога дольше
30 с — не камни (дождь, пар, техника): код снимается до тишины. Нет звука — код 0. От картинки это вычисление не зависит.
История для графика: на каждую секунду — максимум уровня над фоном и самый «старший» этап, уходит в sink (база motion.db).
Проверено на записях 26.11: ночь 4 сброса и 4 засыпки, день 3 и 3, лишних нет; порог от +24 до +34 дБ даёт то же.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

import numpy as np

import motion_videorec as vr

log = logging.getLogger("motion.sound")

SR = 16000
BLOCK = SR // 10                 # 0,1 с
NFFT = 1024
LOW_HZ = 2000
BG_S = 300.0                     # за сколько секунд считается фон
BG_MIN_S = 30.0                  # раньше этого фона ещё нет — «слушаю»
ON_S = 1.0                       # столько звук должен быть громче порога, чтобы стать событием
OFF_S = 2.5                      # столько тишины — событие кончилось
HOLD_S = 3.0                     # код этапа держим не меньше этого с момента обнаружения
LONG_S = 30.0                    # громче порога дольше — это не камни
PAIR_S = (8.0, 45.0)             # засыпка начинается через столько секунд после начала сброса
HIST_S = 180                     # сколько секунд уровня отдаём панели (по точке в секунду)
STALL_S = 20.0                   # звука нет дольше — ffmpeg перезапускается
SILENT_DB = -100.0               # тише этого — не звук, а нули (микрофон камеры выключен): в фон не берём
STAGE_NAMES = {"listen": "слушаю фон", "wait": "ожидание", "drop": "сброс", "load": "засыпка", "noise": "долгий шум"}
HIST_STAGES = ("wait", "drop", "load", "noise")     # номер этапа в истории (таблица sound); при прореживании берётся больший
DEFAULT_CODES = {"wait": 0, "drop": 1, "load": 2}
_WINDOW = np.hanning(NFFT).astype(np.float32)
_STARTS = (0, (BLOCK - NFFT) // 2, BLOCK - NFFT)
_LOW = int(LOW_HZ * NFFT / SR)


def level_db(block: np.ndarray) -> float:
    """Уровень 2–8 кГц блока из BLOCK отсчётов (−1…1), дБ в условных единицах."""
    e = sum(float((np.abs(np.fft.rfft(block[s:s + NFFT] * _WINDOW))[_LOW:] ** 2).sum()) for s in _STARTS) / len(_STARTS)
    return 10.0 * np.log10(e + 1e-12)


class SoundDetector:
    """Чистая логика: на входе уровень очередного блока и его время, на выходе этап печи."""

    def __init__(self, threshold_db: float = 30.0):
        self.threshold = float(threshold_db)
        self.hist: deque = deque(maxlen=int(BG_S * 10))
        self.smooth: deque = deque(maxlen=5)
        self.series: deque = deque(maxlen=HIST_S)      # уровень над фоном по секундам (максимум за секунду) — график в панели
        self.drops = self.loads = 0
        self.last_drop: Optional[float] = None         # начало последнего сброса (для панели)
        self.reset()

    def reset(self):
        """Звук прерывался: текущее событие и пару «сброс — засыпка» забываем, фон оставляем."""
        self.bg: Optional[float] = None
        self._bg_at = -1e18
        self._sec = None
        self.smooth.clear()
        self.rel = 0.0
        self.stage = "wait" if len(self.hist) >= BG_MIN_S * 10 else "listen"
        self.since: Optional[float] = None
        self._now: Optional[float] = None
        self.active = False
        self._above = self._quiet = self._start = self._seen = None
        self._pair_from: Optional[float] = None

    def _go(self, stage: str, now: float):
        if stage != self.stage:
            log.info("этапы по звуку: %s -> %s (%+.0f дБ над фоном)", STAGE_NAMES[self.stage], STAGE_NAMES[stage], self.rel)
            self.stage, self.since = stage, now

    def step(self, now: float, db: float) -> str:
        if self._now is not None and now < self._now - 1.0:
            self.reset()                       # часы ПК перевели назад: иначе фон и удержание кода замрут на величину перевода
        self._now = now
        if self.since is None:
            self.since = now
        self.smooth.append(db)
        level = 10.0 * np.log10(np.mean([10.0 ** (v / 10.0) for v in self.smooth]))
        if db > SILENT_DB:                     # нули занизили бы фон, и обычный шум стал бы «сбросом»
            self.hist.append(level)
        if len(self.hist) < BG_MIN_S * 10:
            self._go("listen", now)
            return self.stage
        if self.bg is None or now - self._bg_at >= 1.0:
            self.bg, self._bg_at = float(np.percentile(self.hist, 20)), now
        self.rel = level - self.bg
        sec = int(now)
        if sec != self._sec:
            self._sec = sec
            self.series.append(self.rel)
        else:
            self.series[-1] = max(self.series[-1], self.rel)
        loud = self.rel >= self.threshold
        if self.stage == "listen":
            self._go("wait", now)
        if not self.active:
            self._above = (self._above if self._above is not None else now) if loud else None
            if loud and now - self._above >= ON_S:
                self.active, self._start, self._seen, self._quiet = True, self._above, now, None
                load = self._pair_from is not None and PAIR_S[0] <= self._start - self._pair_from <= PAIR_S[1]
                if load:
                    self.loads += 1
                    self._pair_from = None
                else:
                    self.drops += 1
                    self._pair_from = self.last_drop = self._start
                self._go("load" if load else "drop", now)
        else:
            self._quiet = None if loud else (self._quiet if self._quiet is not None else now)
            if self._quiet is not None and now - self._quiet >= OFF_S:
                self.active, self._above = False, None
        if self.active and loud and now - self._start > LONG_S:
            if self.stage != "noise":
                self._pair_from = None
                self._go("noise", now)
        elif self.stage in ("drop", "load", "noise") and not self.active and (self._seen is None or now - self._seen >= HOLD_S):
            self._go("wait", now)
        return self.stage


def build_cmd(ffmpeg: str, url: str) -> list:
    low = str(url).lower()
    if low.startswith(("rtsp://", "rtsps://")):
        src = ["-rtsp_transport", "tcp", "-timeout", "10000000", "-allowed_media_types", "audio"]       # картинку с камеры не тянем
    elif "://" in low:
        src = []
    else:
        src = ["-re"]                 # файл читаем в реальном времени, как живой источник
    return [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostats", *src, "-i", url, "-map", "0:a:0", "-vn",
            "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", "-flush_packets", "1", "-f", "s16le", "pipe:1"]


class Listener(threading.Thread):
    """Держит ffmpeg со звуком одной камеры и кормит детектор; после обрыва перезапускает."""

    def __init__(self, cid: str, url: str, threshold_db: float, ffmpeg: str, stall_s: float = STALL_S, clock: Callable = time.time,
                 sink: Optional[Callable] = None):
        super().__init__(daemon=True, name="sound-" + cid)
        self.cid, self.url, self.ffmpeg, self.stall_s, self.clock = cid, url, ffmpeg, stall_s, clock
        self.sink = sink                   # (камера, секунда, уровень над фоном, номер этапа) — история на диск
        self._point: Optional[list] = None # набирающаяся секунда истории [секунда, максимум уровня, этап]
        self._sink_failed = False
        self.detector = SoundDetector(threshold_db)
        self.error = ""
        self._last = 0.0                   # monotonic последнего блока звука
        self._halt = threading.Event()
        self._lock = threading.Lock()

    def stop(self):
        self._halt.set()

    @property
    def alive_sound(self) -> bool:
        return self.is_alive() and self._last > 0 and time.monotonic() - self._last < 5.0

    def set_threshold(self, db: float):
        self.detector.threshold = float(db)

    def _read(self, pipe):
        t, buf = None, b""
        need = BLOCK * 2
        while True:
            chunk = pipe.read(need - len(buf))
            if not chunk:
                return
            buf += chunk
            if len(buf) < need:
                continue
            x = np.frombuffer(buf, dtype="<i2").astype(np.float32) / 32768.0
            buf = b""
            now = self.clock()
            # время ведём по числу отсчётов: звук приходит пачками; разъехались с часами больше чем на 5 с — подравниваем
            t = now if t is None or abs(now - t) > 5.0 else t + 0.1
            db = level_db(x)
            with self._lock:
                self.detector.step(t, db)
                self._last = time.monotonic()
                self.error = ""
                done = self._collect(t, db)
            self._save(done)                # вне замка: медленный диск не задерживает состояние камеры

    def _collect(self, t: float, db: float) -> Optional[list]:
        """Добавить блок в секунду истории; вернуть предыдущую секунду, когда началась следующая. Звать под self._lock."""
        d = self.detector
        if d.stage == "listen" or d.bg is None or db <= SILENT_DB:      # фона ещё нет или вместо звука нули — на графике пусто
            return None
        sec, code, cur = int(t), HIST_STAGES.index(d.stage), self._point
        if cur is not None and cur[0] == sec:
            cur[1], cur[2] = max(cur[1], float(d.rel)), max(cur[2], code)
            return None
        self._point = [sec, float(d.rel), code]
        return cur

    def _save(self, point: Optional[list]):
        if point is None or self.sink is None:
            return
        try:
            self.sink(self.cid, *point)
        except Exception as e:  # noqa: BLE001 — диск не должен ронять приём звука
            if not self._sink_failed:          # один раз: пачка уходит раз в несколько секунд, иначе журнал забьётся
                self._sink_failed = True
                log.warning("[%s] история звука на диск не пишется: %s", self.cid, e)

    def _run_once(self):
        with self._lock:
            self.detector.reset()
            self._last = 0.0
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(build_cmd(self.ffmpeg, self.url), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=flags)
        vr._kill_with_parent(proc)
        tail: deque = deque(maxlen=3)
        readers = [threading.Thread(target=self._read, args=(proc.stdout,), daemon=True),
                   threading.Thread(target=lambda: [tail.append(vr.mask(x.decode("utf-8", "replace").strip())) for x in iter(proc.stderr.readline, b"")],
                                    daemon=True)]
        for r in readers:
            r.start()
        started, stalled = time.monotonic(), False
        try:
            while proc.poll() is None and not self._halt.wait(0.5):
                if time.monotonic() - (self._last or started) > self.stall_s:
                    stalled = True
                    break
        finally:
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    pass
            for r in readers:
                r.join(2)
            with self._lock:
                done, self._point = self._point, None
            self._save(done)                    # последняя неполная секунда перед обрывом
            for pipe in (proc.stdout, proc.stderr):
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass
        if not self._halt.is_set():
            self.error = ("звука нет %d с" % int(self.stall_s)) if stalled else (" · ".join(list(tail)[-2:]) or "поток звука оборвался")[:300]
        return self._last > 0

    def run(self):
        delay, said = vr.RETRY_MIN_S, None
        while not self._halt.is_set():
            t0 = time.monotonic()
            try:
                got = self._run_once()
            except Exception as e:  # noqa: BLE001 — поток не должен умирать молча
                got, self.error = False, vr.mask("звук не запустился: %s" % e)
            with self._lock:
                self._last = 0.0
            if self._halt.is_set():
                break
            if got and time.monotonic() - t0 > vr.RETRY_MAX_S:
                delay, said = vr.RETRY_MIN_S, None
            if self.error != said:
                said = self.error
                log.warning("[%s] звук камеры прервался: %s", self.cid, self.error)
            self._halt.wait(delay)
            delay = min(delay * 2, vr.RETRY_MAX_S)

    def snapshot(self, now: float) -> dict:
        with self._lock:
            d, ok = self.detector, self.alive_sound
            stage = d.stage if ok else None
            return {"stage": stage, "stage_name": STAGE_NAMES[stage] if stage else "нет звука", "since_s": int(max(0.0, now - d.since)) if ok and d.since else 0,
                    "level": round(float(d.rel), 1) if ok and d.bg is not None else None, "series": [round(float(v)) for v in d.series] if ok else [],
                    "drops": d.drops, "loads": d.loads, "drop_ago_s": None if d.last_drop is None else int(max(0.0, now - d.last_drop)),
                    "error": "" if ok else (self.error or "подключаюсь")}


def group_state(group: dict, listener: Optional[Listener], now: float, blocked: str = "") -> dict:
    """Запись группы «этапы по звуку» в состоянии камеры: value — код для ПЛК; self_alive — значение считается и без картинки."""
    if listener is None:
        s = {"stage": None, "stage_name": "нет звука", "since_s": 0, "level": None, "series": [], "drops": 0, "loads": 0, "drop_ago_s": None,
             "error": blocked or "подключаюсь"}
    else:
        s = listener.snapshot(now)
    code = group["codes"]
    stage = s["stage"]
    value = code.get(stage, code["wait"]) if stage else 0       # «слушаю» и «долгий шум» — код ожидания, нет звука — 0
    return {"id": group["id"], "name": group["name"], "type": "kiln_sound", "value": int(value), "bits": [int(stage == "drop"), int(stage == "load")],
            "threshold": group["threshold_db"], "self_alive": stage is not None, **s}


class SoundHub:
    """Слушатели камер: следит за настройками (камера включена и у неё есть вычисление по звуку) и отдаёт состояния групп."""

    def __init__(self, cameras: Callable[[], list], ffmpeg: Callable[[], Optional[str]] = vr.find_ffmpeg, tick_s: float = 2.0,
                 sink: Optional[Callable] = None):
        self.cameras, self.ffmpeg, self.tick_s, self.sink = cameras, ffmpeg, tick_s, sink
        self.listeners: dict[str, Listener] = {}
        self._blocked = ""
        self._lock = threading.RLock()
        self._halt = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @staticmethod
    def _group(cam: dict) -> Optional[dict]:
        return next((g for g in cam.get("groups") or [] if g["type"] == "kiln_sound"), None)

    def sync(self):
        with self._lock:
            if self._halt.is_set():
                return
            wanted = {}
            for c in self.cameras():
                g = self._group(c)
                if g and c.get("enabled", True) and c.get("url"):
                    wanted[c["id"]] = (c["url"], g["threshold_db"])
            exe = self.ffmpeg() if wanted else None
            self._blocked = "" if exe or not wanted else "нет ffmpeg — запустите motion_service\\setup.bat"
            if self._blocked:
                wanted = {}
            self._stop([cid for cid, x in self.listeners.items() if cid not in wanted or x.url != wanted[cid][0] or not x.is_alive()])
            for cid, (url, thr) in wanted.items():
                x = self.listeners.get(cid)
                if x:
                    x.set_threshold(thr)                 # порог меняется на ходу: фон не теряется
                    continue
                x = self.listeners[cid] = Listener(cid, url, thr, exe, sink=self.sink)
                x.start()
                log.info("[%s] слушаю звук камеры: %s", cid, vr.mask(url))

    def _stop(self, cids: list):
        gone = [self.listeners.pop(c) for c in cids if c in self.listeners]
        for x in gone:
            x.stop()
        for x in gone:
            x.join(10)
            log.info("[%s] звук камеры больше не слушаю", x.cid)

    def states(self, cam: dict, now: Optional[float] = None) -> list:
        """Записи групп по звуку для состояния камеры (пусто, если таких вычислений нет)."""
        g = self._group(cam)
        if not g:
            return []
        blocked = "КЗ камеры выключено" if not cam.get("enabled", True) else self._blocked
        return [group_state(g, self.listeners.get(cam["id"]), time.time() if now is None else now, blocked)]

    def _loop(self):
        while not self._halt.is_set():
            try:
                self.sync()
            except Exception as e:  # noqa: BLE001
                log.warning("звук камер: сбой присмотра: %s", vr.mask(e))
            self._halt.wait(self.tick_s)

    def start(self):
        self._halt.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="sound-hub")
        self._thread.start()

    def shutdown(self):
        self._halt.set()
        if self._thread:
            self._thread.join(15)
        with self._lock:
            self._stop(list(self.listeners))
