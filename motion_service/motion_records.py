"""records — разбор CSV-записей метрик (data\records) и архив «одним файлом» для передачи на разбор.

Запись камеры (motion_camera.start_record) пишет строку на каждый обработанный кадр: время, значения зон, alive (1/0) и status
(«ok» или причина: картинка застыла, битый поток, нет кадров…). Отдельные строки-события («событие: поток оборвался») имеют
пустой alive. Здесь по таким файлам считается сводка: сколько времени камера была в норме, когда и на сколько пропадала.
Архив = summary.txt + все CSV за последние дни — его и кладут «на разбор».
"""
from __future__ import annotations

import csv
import io
import statistics
import time
import zipfile
from pathlib import Path
from typing import Optional

MAX_LISTED = 40            # сколько сбоев/событий перечислять на файл (остальные — числом)
GAP_MIN_S = 3.0            # пропуск в строках короче этого сбоем не считаем
GAP_K = 5.0                # ... и короче GAP_K обычных интервалов между строками


def read_rows(path: Path) -> list:
    """[(ts, alive, status)]; alive — 1/0, у строки-события None. Старые файлы без колонки status: статус по alive.
    Оборванная последняя строка (запись прервали на полуслове) пропускается."""
    rows: list = []
    cache: dict = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rd = csv.reader(fh, delimiter=";")
        head = next(rd, None)
        if not head or "alive" not in head:
            return rows
        ia = head.index("alive")
        ist = head.index("status") if "status" in head else None
        for r in rd:
            if len(r) <= ia or len(r[0]) < 23:
                continue
            sec = cache.get(r[0][:19])
            if sec is None:
                try:
                    sec = cache[r[0][:19]] = time.mktime(time.strptime(r[0][:19], "%Y-%m-%d %H:%M:%S"))
                except ValueError:
                    continue
            try:
                ts = sec + int(r[0][20:23]) / 1000.0
                alive = int(r[ia]) if r[ia] != "" else None
            except ValueError:
                continue
            status = r[ist] if ist is not None and len(r) > ist and r[ist] else ("ok" if alive else "не жива")
            rows.append((ts, alive, status))
    return rows


def analyze(rows: list) -> Optional[dict]:
    """Сбои камеры: подряд идущие строки с одним статусом != ok и пропуски в самих строках (кадров не было, запись молчала).
    {"t0","t1","rows","bad":[(начало, конец, что)],"events":[(время, текст)]} или None, если строк состояния нет."""
    st = [r for r in rows if r[1] is not None]
    if not st:
        return None
    dts = [b[0] - a[0] for a, b in zip(st[:2000], st[1:2000])]
    gap = max(GAP_MIN_S, GAP_K * (statistics.median(dts) if dts else 0.0))
    bad: list = []
    cur = None
    prev = None

    def close(end):
        nonlocal cur
        if cur and cur[1] != "ok":
            bad.append((cur[0], end, cur[1]))
        cur = None

    for ts, _alive, status in st:
        if prev is not None and ts - prev > gap:
            close(prev)
            bad.append((prev, ts, "нет данных (кадров и строк не было)"))
        if cur is None or cur[1] != status:
            close(ts)
            cur = (ts, status)
        prev = ts
    close(prev)
    events = [(r[0], r[2][len("событие: "):] if r[2].startswith("событие: ") else r[2]) for r in rows if r[1] is None]
    return {"t0": st[0][0], "t1": st[-1][0], "rows": len(st), "bad": bad, "events": events}


def _dur(sec: float) -> str:
    sec = int(round(sec))
    if sec < 60:
        return "%d с" % sec
    if sec < 3600:
        return "%d мин %d с" % (sec // 60, sec % 60)
    return "%d ч %d мин" % (sec // 3600, sec % 3600 // 60)


def _clock(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def camera_of(path: Path) -> str:
    """rtsp_1_20261030_190000.csv → rtsp_1"""
    return path.stem.rsplit("_", 2)[0]


def summary_text(files: list) -> str:
    """Человекочитаемая сводка по файлам записи: вердикт сверху, дальше по файлу — сколько в норме и список сбоев."""
    blocks: list = []
    trouble: list = []
    for p in files:
        try:
            a = analyze(read_rows(p))
        except OSError as e:
            blocks.append("%s — не прочитан: %s" % (p.name, e))
            continue
        if a is None:
            blocks.append("%s — строк нет (запись только началась или камера ничего не отдала)" % p.name)
            continue
        span = a["t1"] - a["t0"]
        lost = sum(e - s for s, e, _ in a["bad"])
        share = 100.0 if span <= 0 else max(0.0, 100.0 * (1 - lost / span))
        out = ["%s — файл %s" % (camera_of(p), p.name),
               "  запись: %s → %s (%s), строк %d" % (_clock(a["t0"]), _clock(a["t1"]), _dur(span), a["rows"]),
               "  в норме: %.1f %% времени; сбоев: %d%s" % (share, len(a["bad"]),
                                                          " (всего %s, самый долгий %s)" % (_dur(lost), _dur(max(e - s for s, e, _ in a["bad"]))) if a["bad"] else "")]
        if a["bad"]:
            trouble.append("%s (%d)" % (camera_of(p), len(a["bad"])))
            out.append("  сбои:")
            out += ["    %s  %s  %s" % (_clock(s), _dur(e - s).rjust(9), what) for s, e, what in a["bad"][:MAX_LISTED]]
            if len(a["bad"]) > MAX_LISTED:
                out.append("    … и ещё %d" % (len(a["bad"]) - MAX_LISTED))
        if a["events"]:
            out.append("  события связи (%d):" % len(a["events"]))
            out += ["    %s  %s" % (_clock(t), what) for t, what in a["events"][:MAX_LISTED]]
            if len(a["events"]) > MAX_LISTED:
                out.append("    … и ещё %d" % (len(a["events"]) - MAX_LISTED))
        blocks.append("\n".join(out))
    head = ["Сводка записей КЗ — %s" % _clock(time.time()),
            "Файлов: %d. %s" % (len(files), "Сбои были: " + ", ".join(trouble) if trouble else "Сбоев не найдено: камеры работали без перебоев."),
            "Сбой = статус не «ok» (картинка застыла, битый поток, нет кадров) или пропуск в строках дольше %d с / %d обычных интервалов." % (GAP_MIN_S, GAP_K),
            ""]
    return "\n".join(head) + "\n" + "\n\n".join(blocks) + "\n"


def build_zip(rec_dir: Path, days: float = 7.0, now: Optional[float] = None) -> Optional[bytes]:
    """Архив: summary.txt + CSV из rec_dir, изменённые за последние days суток. Файлов нет — None."""
    now = time.time() if now is None else now
    rec_dir = Path(rec_dir)
    files = sorted(p for p in rec_dir.glob("*.csv") if p.is_file() and now - p.stat().st_mtime <= days * 86400)
    if not files:
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("summary.txt", summary_text(files).encode("utf-8-sig"))
        for p in files:
            z.write(p, p.name)
    return buf.getvalue()
