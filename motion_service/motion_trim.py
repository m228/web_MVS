"""trim — нарезка записи вокруг событий: звук пишется весь, а видео остаётся только там, где что-то происходило.

Кусок записи (10 минут) разбирается после закрытия. Событие — это движение в окнах камеры (секундные тренды motion.db) или
громкий звук (та же полоса 2–8 кГц над фоном, что у этапов по звуку, но считается по самому файлу куска — часы совпадают).
Вокруг событий берётся запас «до» и «после». Отрезки, про которые судить нечем (камера не анализировалась, звука в файле
нет или в нём нули), считаются событиями: лучше сохранить лишнее, чем выбросить нужное. Получается:
    <время>_clip.mkv    видео со звуком вокруг событий (вырезано без пережатия, начало — по опорному кадру, раз в 2 с);
    <время>_audio.mka   весь звук куска;
    <время>_full.mkv    исходный кусок — удаляется через заданное число часов;
    <время>_kept.mkv    кусок оставлен целиком: событий почти на всё время, судить нечем или нарезка не удалась.
Полный кусок меняет имя только после того, как все вырезанные файлы прочитаны обратно и их длина сошлась.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

# motion_sound берётся внутри функций: он сам импортирует motion_videorec, а тот — этот модуль (кольцо при импорте)
SR = 16000

log = logging.getLogger("motion")

RAW_RE = re.compile(r"^(\d{8}_\d{6})\.mkv$")          # ещё не разобранный кусок
SOUND_MIN_S = 0.5            # звук громче порога не меньше этого — событие (короче — щелчок)
GAP_S = 5.0                  # данных анализа или звука нет дольше — там могло быть что угодно
ROW_S = 1.5                  # строка тренда движения покрывает столько секунд
MERGE_GAP_S = 5.0            # отрезки ближе этого склеиваются
KEEP_WHOLE = 0.9             # событиями занята такая доля куска — резать незачем
MIN_QUIET_BLOCKS = 100       # меньше 10 с настоящего звука — фон не определить
CUT_SLACK_S = 2.5            # на столько вырезанный файл может быть короче заказанного (опорные кадры, звук)
RUN_TIMEOUT_S = 180.0
STAGED = ".trim"             # добавка к имени исходника на время выкладки вырезанных файлов


class Aborted(Exception):
    """Сервис останавливается: кусок не трогаем, разберём после запуска."""


def start_time(name: str) -> float:
    """Время начала куска из его имени (местное время этого ПК)."""
    return time.mktime(time.strptime(name[:15], "%Y%m%d_%H%M%S"))


def stamp(t: float) -> str:
    return time.strftime("%Y%m%d_%H%M%S", time.localtime(t))


def _runs(mask: np.ndarray, min_len: int) -> list:
    """Отрезки подряд идущих True длиной не меньше min_len: [(с, по)] в номерах блоков."""
    edges = np.flatnonzero(np.diff(np.concatenate(([0], mask.view(np.int8), [0]))))
    return [(int(a), int(b)) for a, b in zip(edges[::2], edges[1::2]) if b - a >= min_len]


def sound_events(x: np.ndarray, threshold_db: float) -> tuple:
    """Звук куска (16 кГц, −1…1) -> (громкие отрезки, отрезки без настоящего звука) в секундах от начала."""
    import motion_sound as snd
    n = len(x) // snd.BLOCK
    sec = snd.BLOCK / SR
    if not n:
        return [], []
    db = np.array([snd.level_db(x[i * snd.BLOCK:(i + 1) * snd.BLOCK]) for i in range(n)])
    real = db > snd.SILENT_DB                      # нули — не звук: микрофон выключен или поток пустой
    if int(real.sum()) < MIN_QUIET_BLOCKS:
        return [], [(0.0, n * sec)]
    power = 10.0 ** (db / 10.0)
    total = np.cumsum(np.concatenate(([0.0], power)))
    lo = np.maximum(np.arange(n) - 4, 0)           # сглаживание за 0,5 с, как у детектора этапов
    level = 10.0 * np.log10((total[1:] - total[lo]) / (np.arange(n) + 1 - lo) + 1e-30)
    bg = float(np.percentile(level[real], 20))
    loud = real & (level - bg >= threshold_db)
    events = [(a * sec, b * sec) for a, b in _runs(loud, int(round(SOUND_MIN_S / sec)))]
    silent = [(a * sec, b * sec) for a, b in _runs(~real, int(round(GAP_S / sec)))]
    return events, silent


def motion_events(rows: list, t0: float, lo: float, hi: float) -> tuple:
    """Строки тренда [(время, движение, картинка в норме)] -> (отрезки с движением, отрезки без данных на [lo, hi]), с от t0."""
    events, unknown, seen = [], [], lo
    for ts, moving, ok in rows:
        if not ok:
            continue
        a = ts - t0
        if moving:
            events.append((a, a + ROW_S))
        if a >= hi or a + ROW_S <= lo:
            continue
        if a - seen > GAP_S:
            unknown.append((seen, a))
        seen = max(seen, a + ROW_S)
    if hi - seen > GAP_S:
        unknown.append((seen, hi))
    return events, unknown


def plan(intervals: list, dur: float, pre_s: float, post_s: float) -> list:
    """Запас вокруг отрезков, обрезка по границам куска и склейка близких. -> [(с, по)] по порядку."""
    padded = sorted((max(0.0, a - pre_s), min(dur, b + post_s)) for a, b in intervals)
    out: list = []
    for a, b in padded:
        if b - a < 0.2:
            continue
        if out and a - out[-1][1] <= MERGE_GAP_S:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


class Media:
    """Вызовы ffmpeg для нарезки: низкий приоритет, прерываются вместе с сервисом."""

    def __init__(self, ffmpeg: str, halt: threading.Event, on_start: Optional[Callable] = None):
        self.ffmpeg, self.halt, self.on_start = ffmpeg, halt, on_start

    def run(self, args: list, stdout=subprocess.DEVNULL) -> tuple:
        """-> (код завершения, текст stderr). Вывод ошибок — во временный файл: труба не переполнится и не подвесит ожидание."""
        if self.halt.is_set():
            raise Aborted()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
        with tempfile.TemporaryFile() as err:
            proc = subprocess.Popen([self.ffmpeg, "-hide_banner", "-nostdin", *args], stdin=subprocess.DEVNULL, stdout=stdout, stderr=err,
                                    creationflags=flags)
            if self.on_start:
                self.on_start(proc)
            started = time.monotonic()
            try:
                while proc.poll() is None:
                    if self.halt.wait(0.05):
                        raise Aborted()
                    if time.monotonic() - started > RUN_TIMEOUT_S:
                        raise RuntimeError("ffmpeg не закончил за %d с" % int(RUN_TIMEOUT_S))
            finally:
                if proc.poll() is None:
                    proc.kill()
                    try:
                        proc.wait(5)
                    except subprocess.TimeoutExpired:
                        pass
            err.seek(0)
            return proc.returncode, err.read().decode("utf-8", "replace")

    def probe(self, path: Path) -> tuple:
        """-> (длительность, с или None; есть ли звук; есть ли картинка)."""
        _, text = self.run(["-i", str(path)])
        m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", text)
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None
        return dur, bool(re.search(r"Stream #0:\d+.*?: Audio:", text)), bool(re.search(r"Stream #0:\d+.*?: Video:", text))

    def audio(self, path: Path, start: float = 0.0, seconds: Optional[float] = None) -> np.ndarray:
        """Звук файла (16 кГц моно). Пустой массив — прочитать нечего."""
        args = ["-v", "error"]
        if start > 0:
            args += ["-ss", "%.3f" % start]
        if seconds is not None:
            args += ["-t", "%.3f" % seconds]
        args += ["-i", str(path), "-map", "0:a:0", "-vn", "-af", "aresample=async=1:first_pts=0",      # дыры в звуке — тишиной, иначе съедет время
                 "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"]
        with tempfile.TemporaryFile() as out:
            self.run(args, stdout=out)               # оборванный кусок ffmpeg читает с ошибкой в конце — берём, что прочиталось
            out.seek(0)
            raw = out.read()
        return np.frombuffer(raw[:len(raw) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0

    def copy(self, src: Path, dst: Path, start: float = 0.0, seconds: Optional[float] = None, audio_only: bool = False):
        """Вырезать без пережатия."""
        args = ["-v", "error", "-y"]
        if start > 0:
            args += ["-ss", "%.3f" % start]
        args += ["-i", str(src)]
        if seconds is not None:
            args += ["-t", "%.3f" % seconds]
        args += ["-map", "0:a:0"] if audio_only else ["-map", "0"]
        code, text = self.run(args + ["-c", "copy", "-avoid_negative_ts", "make_zero", "-f", "matroska", str(dst)])
        if code:
            raise RuntimeError("ffmpeg: " + (text.strip().splitlines() or ["код %s" % code])[-1][:200])


def _free_name(folder: Path, t: float, suffix: str) -> Path:
    while True:
        p = folder / (stamp(t) + suffix)
        if not p.exists():
            return p
        t += 1.0


def keep_whole(piece: Path) -> Path:
    """Кусок остаётся целиком: …_kept.mkv. Существующий файл не затирается (часы ПК могли перевести назад)."""
    target = _free_name(piece.parent, start_time(piece.name), "_kept.mkv")
    os.replace(piece, target)
    return target


def _sync(path: Path):
    with open(path, "rb+") as f:
        os.fsync(f.fileno())


def recover(folder: Path) -> list:
    """Куски, чья выкладка оборвалась (сбой, выключение питания), остаются целиком: …_kept.mkv. -> что вернули."""
    back = []
    for src in folder.glob("*" + STAGED):
        try:
            back.append(keep_whole(src).name)
        except (OSError, ValueError) as e:
            log.warning("кусок %s после прерванной нарезки не переименовать: %s", src.name, e)
    return back


def _commit(piece: Path, parts: list, full_h: float) -> list:
    """Вырезанные части → итоговые имена, исходный кусок → полная копия (или удаляется). При сбое имена возвращаются.
    Пока не выложено всё, исходник лежит под промежуточным именем: его не трогают ни лимит места, ни срок полных копий,
    а после сбоя или выключения recover() вернёт его целым куском. Переименовывается он первым: занят другой программой —
    копии не наплодятся. -> имена выложенных файлов."""
    folder, t0 = piece.parent, start_time(piece.name)
    for part, _, _ in parts:
        _sync(part)                                   # отключат питание — не должно быть пустых отрезков без исходника
    staged = folder / (piece.name + STAGED)
    os.replace(piece, staged)
    made: list = []
    try:
        for part, t, suffix in parts:
            target = _free_name(folder, t, suffix)
            os.replace(part, target)
            made.append((part, target))
    except OSError:
        for src, dst in [(target, part) for part, target in made] + [(staged, piece)]:
            try:
                os.replace(src, dst)                  # каждый шаг сам по себе: сбой одного не отменяет остальные
            except OSError as e:
                log.warning("откат нарезки: %s не переименовать (%s)", src.name, e)
        raise
    try:
        if full_h > 0:
            os.utime(staged)                          # срок хранения полного куска считаем от разбора
            os.replace(staged, _free_name(folder, t0, "_full.mkv"))
        else:
            staged.unlink()
    except OSError as e:                              # занят — всё вырезано, а исходник recover() оставит целым куском
        log.warning("исходный кусок %s после нарезки занят: %s", piece.name, e)
    return [target.name for _, target in made]


def trim_piece(media: Media, piece: Path, cfg: dict, activity: Optional[Callable], prev: Optional[Path] = None,
               nxt: Optional[Path] = None, commit_lock=None, current: Optional[Callable[[], dict]] = None) -> dict:
    """Разобрать один кусок. prev / nxt — соседние по времени файлы (для событий на стыке).
    activity(t0, t1) -> строки тренда движения или None, если у камеры нет окон движения.
    current() -> настройки на момент записи результата: режим успели выключить — кусок не трогаем.
    -> {"result": "clips" | "kept", "reason", "clips": [имена], "dur_s", "left_s"}. Исключение — исходный кусок не тронут."""
    lock = commit_lock or threading.Lock()
    folder, name = piece.parent, piece.name
    t0 = start_time(name)
    pre, post, thr = float(cfg["pre_s"]), float(cfg["post_s"]), float(cfg["sound_db"])
    dur, has_audio, has_video = media.probe(piece)
    samples = media.audio(piece) if has_audio else np.zeros(0, np.float32)
    audio_s = len(samples) / SR
    closed = dur is not None
    dur = max(dur or 0.0, audio_s)

    def keep(reason: str) -> dict:
        with lock:
            keep_whole(piece)
        return {"result": "kept", "reason": reason, "clips": [], "dur_s": round(dur), "left_s": round(dur)}

    if dur <= 0 or not has_video:
        return keep("файл не читается или в нём нет картинки")
    if not closed:                                    # запись оборвали: сколько в файле картинки, неизвестно
        return keep("кусок не закрыт (запись оборвалась) — длина неизвестна")
    if has_audio and audio_s <= 0:
        return keep("звук в файле не читается")
    found, judged = [], False
    if activity is not None:
        rows = activity(t0 - post - 2.0, t0 + dur + pre + 2.0)
        if rows is not None:
            judged = True
            events, unknown = motion_events(rows, t0, 0.0, dur)
            found += events + unknown
    if thr > 0:
        judged = True
        if audio_s <= 0:
            found.append((0.0, dur))                 # звук просили слушать, а его нет
        else:
            head = np.zeros(0, np.float32)
            tail = np.zeros(0, np.float32)
            if prev is not None:                     # хвост прошлого куска: «после» события на стыке
                pdur = media.probe(prev)[0]
                if pdur and abs(start_time(prev.name) + pdur - t0) <= 5.0:
                    head = media.audio(prev, max(0.0, pdur - post - 2.0))
            if nxt is not None and abs(t0 + dur - start_time(nxt.name)) <= 5.0:
                tail = media.audio(nxt, 0.0, pre + 2.0)
            events, silent = sound_events(np.concatenate((head, samples, tail)), thr)
            shift = len(head) / SR
            found += [(a - shift, b - shift) for a, b in events]
            found += [(max(0.0, a - shift), min(dur, b - shift)) for a, b in silent if b - shift > 0 and a - shift < dur]
            if dur - audio_s > GAP_S:
                found.append((audio_s, dur))         # звук кончился раньше картинки
    if not judged:
        return keep("судить нечем: нет окон движения и звук не слушается")
    clips = plan(found, dur, pre, post)
    left = sum(b - a for a, b in clips)
    if left >= KEEP_WHOLE * dur:
        return keep("события почти на всё время")

    parts: list = []                                 # (временный файл, итоговое имя)
    try:
        for i, (a, b) in enumerate(clips):
            part = folder / ("%s_%d.part" % (name[:15], i))
            parts.append((part, t0 + a, "_clip.mkv"))
            media.copy(piece, part, a, b - a)
            got, _, video = media.probe(part)
            if not video or got is None or got < min(b, dur) - a - CUT_SLACK_S:
                raise RuntimeError("вырезанный отрезок %d с вышел короче заказанного" % int(b - a))
        if audio_s > 0:
            part = folder / ("%s_a.part" % name[:15])
            parts.append((part, t0, "_audio.mka"))
            media.copy(piece, part, audio_only=True)
            got = media.probe(part)[0]
            if got is None or got < audio_s - CUT_SLACK_S:
                raise RuntimeError("звук отдельным файлом вышел короче куска")
        with lock:
            now = current() if current else cfg
            if now.get("keep", "events") != "events":
                raise Aborted()
            made = _commit(piece, parts, float(now.get("full_h", cfg["full_h"])))
    finally:
        for part, _, _ in parts:
            if part.exists():
                try:
                    part.unlink()
                except OSError:
                    pass
    return {"result": "clips", "reason": "", "clips": [n for n in made if n.endswith("_clip.mkv")], "dur_s": round(dur), "left_s": round(left)}
