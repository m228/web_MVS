"""records — разбор CSV-записей метрик (data\records) и архив «одним файлом» для передачи на разбор.

Запись камеры (motion_camera.start_record) пишет строку на каждый обработанный кадр: время, значения зон, alive (1/0) и status
(«ok» или причина: картинка застыла, битый поток, нет кадров…). Отдельные строки-события («событие: поток оборвался») имеют
пустой alive. Здесь по таким файлам считается сводка: сколько времени камера была в норме, когда и на сколько пропадала.
Архив = summary.txt + все CSV за последние дни — его и кладут «на разбор». В сводке по каждой зоне — среднее / мин / макс
flow и diff и доля времени «крутится» (по кадрам в норме): по ним ставят пороги on/off.
"""
from __future__ import annotations

import csv
import io
import re
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


def zone_stats(path: Path) -> dict:
    """{зона: {"n", "flow": (ср, мин, макс), "diff": (ср, мин, макс), "bit": доля 0..1}} по строкам, где камера жива и status = ok
    (застывшие/битые кадры в среднее не идут). Файлы без зон или без status (старые) — по alive."""
    out: dict = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rd = csv.reader(fh, delimiter=";")
        head = next(rd, None)
        if not head or "alive" not in head:
            return out
        ia = head.index("alive")
        ist = head.index("status") if "status" in head else None
        cols = []                                       # (зона, индекс flow, индекс diff, индекс bit)
        for i, h in enumerate(head):
            if h.endswith("_flow") and h[:-5] + "_diff" in head and h[:-5] + "_bit" in head:
                z = h[:-5]
                cols.append((z, i, head.index(z + "_diff"), head.index(z + "_bit")))
        acc = {z: [0, 0.0, 1e30, -1e30, 0.0, 1e30, -1e30, 0] for z, *_ in cols}
        need = max([ia] + [c for _, a, b, c2 in cols for c in (a, b, c2)] + ([ist] if ist is not None else []))
        for r in rd:
            if len(r) <= need or r[ia] != "1" or (ist is not None and r[ist] != "ok"):
                continue
            for z, i_f, i_d, i_b in cols:
                try:
                    fl, df, bit = float(r[i_f]), float(r[i_d]), int(float(r[i_b]))
                except ValueError:
                    continue
                a = acc[z]
                a[0] += 1; a[1] += fl; a[2] = min(a[2], fl); a[3] = max(a[3], fl)
                a[4] += df; a[5] = min(a[5], df); a[6] = max(a[6], df); a[7] += bit
    for z, a in acc.items():
        if a[0]:
            out[z] = {"n": a[0], "flow": (a[1] / a[0], a[2], a[3]), "diff": (a[4] / a[0], a[5], a[6]), "bit": a[7] / a[0]}
    return out


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
    """rtsp_1_20261030_190000.csv → rtsp_1 (и rtsp_1_20261030_190000_b.csv — перезапуск в ту же секунду)"""
    m = re.fullmatch(r"(.*)_\d{8}_\d{6}(?:_b)*", path.stem)
    return m.group(1) if m else path.stem.rsplit("_", 2)[0]


def summary_text(files: list, names: Optional[dict] = None) -> str:
    """Человекочитаемая сводка по файлам записи: вердикт сверху, дальше по файлу — сколько в норме, список сбоев и значения зон.
    names — {id камеры: {"name": ..., "zones": {id зоны: название}}} из конфига: подписи вместо id (необязательно)."""
    names = names or {}
    blocks: list = []
    trouble: list = []
    spans: dict = {}                                   # камера → [(начало, конец)] файлов: между ними — перерывы (перезапуск сервиса, обновление)
    for p in files:
        try:
            a = analyze(read_rows(p))
        except OSError as e:
            blocks.append("%s — не прочитан: %s" % (p.name, e))
            continue
        if a is None:
            blocks.append("%s — строк нет (запись только началась или камера ничего не отдала)" % p.name)
            continue
        spans.setdefault(camera_of(p), []).append((a["t0"], a["t1"]))
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
        try:
            zs = zone_stats(p)
        except (OSError, csv.Error) as e:
            zs = {}
            out.append("  значения зон не прочитаны: %s" % e)
        if zs:
            zn = (names.get(camera_of(p)) or {}).get("zones") or {}
            out.append("  зоны (только кадры в норме; значения сглажены, как в графиках; «крутится» — доля времени с битом 1):")
            for z, s in zs.items():
                out.append("    %s: diff ср %.2f мин %.2f макс %.2f · flow ср %.2f мин %.2f макс %.2f · крутится %.0f %% (кадров %d)" % (
                    (zn.get(z) + " (" + z + ")") if zn.get(z) else z, *s["diff"], *s["flow"], 100.0 * s["bit"], s["n"]))
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
        blocks.append("Перерывы между файлами записи (сервис перезапускался — обновление, сбой; запись сама продолжается в новом файле):\n" + "\n".join(breaks[:MAX_LISTED]))
    head = ["Сводка записей КЗ — %s" % _clock(time.time()),
            "Файлов: %d. %s" % (len(files), "Сбои были: " + ", ".join(trouble) if trouble else "Сбоев не найдено: камеры работали без перебоев."),
            "Сбой = статус не «ok» (картинка застыла, битый поток, нет кадров) или пропуск в строках дольше %d с / %d обычных интервалов." % (GAP_MIN_S, GAP_K),
            ""]
    return "\n".join(head) + "\n" + "\n\n".join(blocks) + "\n"


def build_zip(rec_dir: Path, days: float = 7.0, now: Optional[float] = None, names: Optional[dict] = None) -> Optional[bytes]:
    """Архив: summary.txt + CSV из rec_dir, изменённые за последние days суток. Файлов нет — None."""
    now = time.time() if now is None else now
    rec_dir = Path(rec_dir)
    files = sorted(p for p in rec_dir.glob("*.csv") if p.is_file() and now - p.stat().st_mtime <= days * 86400)
    if not files:
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("summary.txt", summary_text(files, names).encode("utf-8-sig"))
        for p in files:
            z.write(p, p.name)
    return buf.getvalue()
