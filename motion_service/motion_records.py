"""records — разбор записей метрик (таблицы rec / rec_zone в data\\motion.db) и архив «одним файлом» для передачи на разбор.

Запись камеры (motion_camera.start_record) пишет строку на каждый обработанный кадр: время, значения зон, alive (1/0) и status
(«ok» или причина: картинка застыла, битый поток, нет кадров…). Отдельные строки-события («событие: поток оборвался») имеют
пустой alive. Здесь по ним считается сводка: сколько времени камера была в норме, когда и на сколько пропадала.
Архив = summary.txt + CSV каждого запуска записи за последние дни (CSV собирается из базы, Excel открывает). В сводке по каждой зоне — среднее / мин / макс flow и diff и доля времени
«крутится» (по кадрам в норме): по ним ставят пороги on/off.
"""
from __future__ import annotations

import io
import itertools
import statistics
import time
import zipfile
from pathlib import Path
from typing import Iterable, Optional

from motion_store import store_for

MAX_LISTED = 40            # сколько сбоев/событий перечислять на запуск (остальные — числом)
GAP_MIN_S = 3.0            # пропуск в строках короче этого сбоем не считаем
GAP_K = 5.0                # ... и короче GAP_K обычных интервалов между строками


def analyze(rows: Iterable) -> Optional[dict]:
    """Сбои камеры: подряд идущие строки с одним статусом != ok и пропуски в самих строках (кадров не было, запись молчала).
    rows — поток (ts, alive, status); alive у события None. Считает потоком: запись за недели в память не грузится.
    {"t0","t1","rows","bad":[(начало, конец, что)],"events":[(время, текст)]} или None, если строк состояния нет."""
    events: list = []

    def states():
        for r in rows:
            if r[1] is None:
                events.append((r[0], r[2][len("событие: "):] if r[2].startswith("событие: ") else r[2]))
            else:
                yield r

    it = states()
    head = list(itertools.islice(it, 2000))
    if not head:
        return None
    oks = [r for r in head if r[2] == "ok"]            # обычный шаг строк — по кадрам в норме: старт в простое (камера грузится) шаг не занижает
    dts = [b[0] - a[0] for a, b in zip(oks, oks[1:])]
    gap = max(GAP_MIN_S, GAP_K * (statistics.median(dts) if dts else 0.0))
    bad: list = []
    cur = None
    prev = None
    n = 0

    def close(end):
        nonlocal cur
        if cur and cur[1] != "ok":
            bad.append((cur[0], end, cur[1]))
        cur = None

    for ts, _alive, status in itertools.chain(head, it):
        n += 1
        if prev is not None and ts - prev > gap:
            close(prev)
            bad.append((prev, ts, "нет данных (кадров и строк не было)"))
        if cur is None or cur[1] != status:
            close(ts)
            cur = (ts, status)
        prev = ts
    close(prev)
    return {"t0": head[0][0], "t1": prev, "rows": n, "bad": bad, "events": events}


def _dur(sec: float) -> str:
    sec = int(round(sec))
    if sec < 60:
        return "%d с" % sec
    if sec < 3600:
        return "%d мин %d с" % (sec // 60, sec % 60)
    return "%d ч %d мин" % (sec // 3600, sec % 3600 // 60)


def _clock(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def summary_text(store, runs: list, names: Optional[dict] = None, since: float = 0.0) -> str:
    """Человекочитаемая сводка по запускам записи: вердикт сверху, дальше по запуску — сколько в норме, список сбоев и значения зон.
    names — {id камеры: {"name": ..., "zones": {id зоны: название}}} из конфига: подписи вместо id (необязательно)."""
    names = names or {}
    blocks: list = []
    trouble: list = []
    spans: dict = {}                                   # камера → [(начало, конец)] запусков: между ними — перерывы (перезапуск сервиса, обновление)
    for run in runs:
        cam = run["cam"]
        title = "%s — запись с %s" % (cam, _clock(run["t0"]))
        a = analyze(store.run_rows(run, since))
        if a is None:
            blocks.append("%s — строк нет (запись только началась или камера ничего не отдала)" % title)
            continue
        spans.setdefault(cam, []).append((a["t0"], a["t1"]))
        span = a["t1"] - a["t0"]
        lost = sum(e - s for s, e, _ in a["bad"])
        share = 100.0 if span <= 0 else max(0.0, 100.0 * (1 - lost / span))
        out = [title,
               "  запись: %s → %s (%s), строк %d" % (_clock(a["t0"]), _clock(a["t1"]), _dur(span), a["rows"]),
               "  в норме: %.1f %% времени; сбоев: %d%s" % (share, len(a["bad"]),
                                                          " (всего %s, самый долгий %s)" % (_dur(lost), _dur(max(e - s for s, e, _ in a["bad"]))) if a["bad"] else "")]
        if a["bad"]:
            trouble.append("%s (%d)" % (cam, len(a["bad"])))
            out.append("  сбои:")
            out += ["    %s  %s  %s" % (_clock(s), _dur(e - s).rjust(9), what) for s, e, what in a["bad"][:MAX_LISTED]]
            if len(a["bad"]) > MAX_LISTED:
                out.append("    … и ещё %d" % (len(a["bad"]) - MAX_LISTED))
        zs = store.run_zone_stats(run, since)
        if zs:
            zn = (names.get(cam) or {}).get("zones") or {}
            out.append("  зоны (только кадры в норме; значения сглажены, как в графиках; «крутится» — доля времени с битом 1):")
            for z, st in zs.items():
                out.append("    %s: diff ср %.2f мин %.2f макс %.2f · flow ср %.2f мин %.2f макс %.2f · крутится %.0f %% (кадров %d)" % (
                    (zn.get(z) + " (" + z + ")") if zn.get(z) else z, *st["diff"], *st["flow"], 100.0 * st["bit"], st["n"]))
        if a["events"]:
            out.append("  события связи (%d):" % len(a["events"]))
            out += ["    %s  %s" % (_clock(t), what) for t, what in a["events"][:MAX_LISTED]]
            if len(a["events"]) > MAX_LISTED:
                out.append("    … и ещё %d" % (len(a["events"]) - MAX_LISTED))
        blocks.append("\n".join(out))
    breaks = []
    for cam, sp in spans.items():
        sp.sort()
        for (_s0, e0), (s1, _e1) in zip(sp, sp[1:]):
            if s1 - e0 > GAP_MIN_S:
                breaks.append("  %s: %s → %s (%s)" % (cam, _clock(e0), _clock(s1), _dur(s1 - e0)))
    if breaks:
        blocks.append("Перерывы между запусками записи (сервис перезапускался — обновление, сбой; запись сама продолжается):\n" + "\n".join(breaks[:MAX_LISTED]))
    head = ["Сводка записей КЗ — %s" % _clock(time.time()),
            "Запусков записи: %d. %s" % (len(runs), "Сбои были: " + ", ".join(trouble) if trouble else "Сбоев не найдено: камеры работали без перебоев."),
            "Сбой = статус не «ok» (картинка застыла, битый поток, нет кадров) или пропуск в строках дольше %d с / %d обычных интервалов." % (GAP_MIN_S, GAP_K),
            ""]
    return "\n".join(head) + "\n" + "\n\n".join(blocks) + "\n"


def build_zip(base: Path, days: float = 7.0, now: Optional[float] = None, names: Optional[dict] = None) -> Optional[bytes]:
    """Архив: summary.txt + CSV каждого запуска записи за последние days суток (собираются из базы). Записей нет — None."""
    now = time.time() if now is None else now
    base = Path(base)
    since = now - days * 86400
    store = store_for(base)
    runs = store.runs(since)
    if not runs:
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("summary.txt", summary_text(store, runs, names, since).encode("utf-8-sig"))
        used: set = set()
        for run in runs:
            name = "%s_%s.csv" % (run["cam"], time.strftime("%Y%m%d_%H%M%S", time.localtime(run["t0"])))
            while name in used:
                name = name[:-4] + "_b.csv"
            used.add(name)
            with z.open(name, "w", force_zip64=True) as raw:           # CSV идёт в архив потоком: запуск за недели в памяти не собирается
                out = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
                store.write_run_csv(run, out, since)
                out.flush()
                out.detach()
    return buf.getvalue()
