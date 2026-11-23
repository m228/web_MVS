"""Отчёт «день / ночь» по видимости палок: нужен ли для дня и для ночи разный конфиг (цвета, ось, радиус) или хватает одного.

Три источника, всё только чтение:
  1. Секундные тренды (data\\motion.db) — как часто палка находилась в каждые 30 минут. Окна разбиты на «эпохи» — периоды, когда настройки зон
     камеры не менялись (по резервным копиям data\\backups). Если в эпоху с подогнанными цветами день и ночь одинаковы — свет конфиг не ломает.
  2. Снимки камер (data\\shots, раз в 20 мин): ТЕКУЩИЕ настройки прогоняются по дневным и ночным кадрам, находит ли боевой детектор палку и
     какая там яркость. Это прямой ответ на «подходит ли один конфиг и дню, и ночи».
  3. Журнал сторожа подгонки (data\\autocal_watch.jsonl): что и когда подгонялось, применялось, откатывалось.
Отдаётся готовым HTML (одна самодостаточная страница) — из сервиса (/cameras/{id}/autocal/report) и из tools/autocal_day_night_report.py.
"""
from __future__ import annotations

import html
import json
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

import motion_engine as me

DAY_FROM, DAY_TO = 8, 20            # день 08:00–20:00, ночь 20:00–08:00 (как в отчёте по частям суток)
SLOT_S = 1800                       # окно статистики
MIN_ERA_H = 2.0                     # короче — это правка настроек, а не период работы
GOOD_PCT = 95.0
TUNED_PCT = 90.0                    # период, где медиана окон не ниже, — «настройки подогнаны»


def is_day(ts: float) -> bool:
    return DAY_FROM <= time.localtime(ts).tm_hour < DAY_TO


def esc(x) -> str:
    return html.escape(str(x))


def heat(p: Optional[float]) -> str:
    if p is None:
        return "var(--none)"
    return "#dc2626" if p < 50 else "#f97316" if p < 80 else "#eab308" if p < GOOD_PCT else "#16a34a"


# ───────────── источники ─────────────
def load_trend(db: Path, cam: str, t0: float, t1: float) -> dict:
    """{зона: {номер окна: (секунд с картинкой, секунд «найдена»)}} за [t0, t1]; зоны — по именам в базе (старые rod3/rod4 тоже)."""
    c = sqlite3.connect("file:%s?mode=ro" % str(db).replace("\\", "/"), uri=True, timeout=20)
    try:
        names = dict(c.execute("SELECT id, name FROM names"))
        cid = next((i for i, n in names.items() if n == cam), None)
        out: dict = defaultdict(dict)
        if cid is None:
            return out
        for k, z, n, f in c.execute(
                "SELECT ts / ? AS k, zone, COUNT(*), SUM(bit AND diff > 0) FROM trend WHERE cam=? AND ts>=? AND ts<? AND ok=1 GROUP BY k, zone",
                (SLOT_S * 10, cid, int(t0 * 10), int(t1 * 10))):
            out[names.get(z, str(z))][int(k)] = (int(n), int(f or 0))
        return out
    finally:
        c.close()


def zone_signature(cam: dict) -> dict:
    """{зона: настройки, от которых зависит находка палки} — по включённым циклам камеры."""
    out = {}
    for z in cam.get("zones", []):
        if z.get("type") == "cycle":
            out[z["id"]] = {"red": list(z["red_hsv"]), "blue": list(z["blue_hsv"]), "c": [round(z["center"][0]), round(z["center"][1])],
                            "r": round(z["radius"]), "thin": z.get("thin"), "rm": z.get("red_more") or [], "bm": z.get("blue_more") or []}
    return out


def load_eras(data_dir: Path, cam_id: str, cur_cam: dict, t_min: float) -> list:
    """Периоды неизменных настроек палок камеры: [{from, to, sig}] по резервным копиям конфига (+ текущий конфиг). Время правки — из имени файла."""
    pts = []
    for f in sorted((data_dir / "backups").glob("motion_config_*.json")):
        try:
            ts = time.mktime(time.strptime(f.stem[len("motion_config_"):], "%Y%m%d_%H%M%S"))
            cam = next(c for c in json.loads(f.read_text(encoding="utf-8-sig"))["cameras"] if c["id"] == cam_id)
        except (ValueError, OSError, StopIteration, KeyError):
            continue
        pts.append((ts, zone_signature(cam)))
    cur = zone_signature(cur_cam)
    if not pts:
        pts.append((t_min, cur))                      # резервных копий нет — все настройки считаем неизменными за весь период
    elif pts[-1][1] != cur:
        pts.append((time.time(), cur))
    eras: list = []
    for ts, sig in pts:
        if eras and eras[-1]["sig"] == sig:
            continue
        if eras:
            eras[-1]["to"] = ts
        eras.append({"from": ts, "to": None, "sig": sig})
    if eras:
        eras[-1]["to"] = time.time()
    return [e for e in eras if e["to"] and e["to"] > t_min]


def era_stats(trend: dict, era: dict, only_zones: Optional[set] = None) -> dict:
    """По зонам эпохи: видимость днём и ночью (медиана 30-минутных окон — один плохой час среднее не портит), доля окон не ниже порога, худшее окно, часов данных."""
    out = {}
    for z in era["sig"]:
        if only_zones and z not in only_zones:
            continue
        d, n_ = [], []
        for k, (n, f) in trend.get(z, {}).items():
            ts = k * SLOT_S
            if ts < era["from"] + SLOT_S or ts + SLOT_S > era["to"] or n < 0.6 * SLOT_S:
                continue
            (d if is_day(ts) else n_).append(100.0 * f / n)
        allp = d + n_
        ok = lambda a: 100.0 * sum(1 for p in a if p >= GOOD_PCT) / len(a) if a else None  # noqa: E731
        out[z] = {"day": float(np.median(d)) if d else None, "night": float(np.median(n_)) if n_ else None, "ok_day": ok(d), "ok_night": ok(n_),
                  "all": float(np.median(allp)) if allp else 0.0, "dw": d, "nw": n_, "worst": min(allp) if allp else None, "hours": len(allp) * SLOT_S / 3600.0}
    return out


# ───────────── снимки ─────────────
def list_shots(data_dir: Path, cam_id: str, t_from: float) -> list:
    """[(время, путь)] снимков камеры из data\\shots\\<день>\\<ччммсс>\\<камера>.jpg новее t_from."""
    out = []
    for day in sorted((data_dir / "shots").glob("[0-9]" * 8)):
        for sh in sorted(day.glob("[0-9]" * 6)):
            p = sh / (cam_id + ".jpg")
            if not p.is_file():
                continue
            try:
                ts = time.mktime(time.strptime(day.name + sh.name, "%Y%m%d%H%M%S"))
            except ValueError:
                continue
            if ts >= t_from:
                out.append((ts, p))
    return out


def pick_even(items: list, n: int) -> list:
    if len(items) <= n:
        return items
    step = len(items) / float(n)
    return [items[int(i * step)] for i in range(n)]


def check_shots(cam: dict, shots: list, per_part: int = 24) -> dict:
    """Текущие настройки палок на кадрах днём и ночью: {зона: {"day": [...], "night": [...]}} — по кадру: (найдена, уверенность, яркость вокруг оси)."""
    cycles = [z for z in cam["zones"] if z["type"] == "cycle" and z["enabled"]]
    res = {z["id"]: {"day": [], "night": []} for z in cycles}
    picked = pick_even([s for s in shots if is_day(s[0])], per_part) + pick_even([s for s in shots if not is_day(s[0])], per_part)
    for ts, path in picked:
        frame = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            continue
        h, w = frame.shape[:2]
        for z in cycles:
            rt = me.CycleRT(z, cam.get("frame_size"), (w, h))
            found = bool(rt.step(frame, 0.0)) and rt.src in ("both", "red", "blue")
            v = float(cv2.cvtColor(frame[rt.y0:rt.y1, rt.x0:rt.x1], cv2.COLOR_BGR2HSV)[..., 2].mean())
            res[z["id"]]["day" if is_day(ts) else "night"].append((found, float(rt.conf), v, ts))
    return res


# ───────────── сборка страницы ─────────────
def _pct(v: Optional[float]) -> str:
    return "—" if v is None else "%d %%" % round(v)


def _sig_text(sig: dict) -> str:
    return "; ".join("%s: красный %s, синий %s, ось %s, R %s" % (z, "/".join(map(str, s["red"])), "/".join(map(str, s["blue"])), "·".join(map(str, s["c"])), s["r"])
                     for z, s in sig.items())


def build_report(data_dir, cam_id: str, cam: Optional[dict] = None, days: int = 10, shots: bool = True, now: Optional[float] = None, shots_per_part: int = 24) -> str:
    data_dir = Path(data_dir)
    now = now or time.time()
    if cam is None:
        cam = next(c for c in me.normalize_config(json.loads((data_dir / "motion_config.json").read_text(encoding="utf-8-sig")))["cameras"] if c["id"] == cam_id)
    t0 = now - days * 86400.0
    trend = load_trend(data_dir / "motion.db", cam_id, t0, now)
    eras = load_eras(data_dir, cam_id, cam, t0)
    cur_ids = {z["id"] for z in cam["zones"] if z["type"] == "cycle"}
    names = {z["id"]: z["name"] for z in cam["zones"]}

    # — эпохи —
    era_rows = []
    for e in eras:
        for z, v in era_stats(trend, e).items():
            if v["hours"] >= MIN_ERA_H:
                era_rows.append((e, z, v, None if v["day"] is None or v["night"] is None else v["day"] - v["night"]))

    # — полоса по суткам —
    by_zone_day: dict = defaultdict(lambda: defaultdict(dict))
    for z, rows in trend.items():
        for k, (n, f) in rows.items():
            if n >= 0.6 * SLOT_S:
                t = time.localtime(k * SLOT_S)
                by_zone_day[z][time.strftime("%d.%m", t)][t.tm_hour * 2 + t.tm_min // 30] = 100.0 * f / n
    marks = {}
    for e in eras:
        t = time.localtime(e["from"])
        marks[(time.strftime("%d.%m", t), t.tm_hour * 2 + t.tm_min // 30)] = _sig_text(e["sig"])
    strips = []
    for z in sorted(by_zone_day, key=lambda s: (s not in cur_ids, s)):
        days_ = by_zone_day[z]
        rows = []
        for day in sorted(days_, key=lambda s: (s[3:], s)):
            cells = "".join('<i style="background:%s"%s></i>' % (heat(days_[day].get(i)), (' class="mk" title="%s"' % esc("настройки менялись: " + marks[(day, i)])) if (day, i) in marks else
                            ' title="%s %02d:%02d — %s"' % (day, i // 2, 30 * (i % 2), _pct(days_[day].get(i)))) for i in range(48))
            rows.append('<div class="row"><span class="d">%s</span><span class="s">%s</span></div>' % (day, cells))
        strips.append('<h3>%s <small>(зона %s%s)</small></h3>%s' % (esc(names.get(z, z)), esc(z), "" if z in cur_ids else ", прежняя разметка", "".join(rows)))

    # — снимки —
    shot_html, shot_verdict = "", ""
    if shots:
        sh = list_shots(data_dir, cam_id, now - days * 86400.0)
        if sh:
            r = check_shots(cam, sh, shots_per_part)
            rows = []
            gaps = []
            for z, v in r.items():
                d, n_ = v["day"], v["night"]
                if not d or not n_:
                    continue
                fd, fn = 100.0 * sum(1 for x in d if x[0]) / len(d), 100.0 * sum(1 for x in n_ if x[0]) / len(n_)
                vd, vn = np.mean([x[2] for x in d]), np.mean([x[2] for x in n_])
                cd, cn = np.mean([x[1] for x in d]), np.mean([x[1] for x in n_])
                gaps.append(fd - fn)
                rows.append("<tr><td>%s</td><td>%d / %d</td><td class=n>%s</td><td class=n>%s</td><td class=n>%.2f</td><td class=n>%.2f</td><td class=n>%d</td><td class=n>%d</td></tr>" % (
                    esc(names.get(z, z)), len(d), len(n_), _pct(fd), _pct(fn), cd, cn, vd, vn))
            if rows:
                shot_html = ('<h2>Текущие настройки на снимках</h2><p class=sub>Боевой детектор с нынешними цветами, осью и радиусом прогнан по кадрам из data\\shots (раз в 20 минут) — '
                             'дневным (08–20) и ночным (20–08) за %d дн. Одиночный кадр без истории, поэтому строже боевого режима: он держит палку и там, где кадр не поймал.</p>'
                             '<table><tr><th>Вал</th><th>кадров день / ночь</th><th>найдена днём</th><th>найдена ночью</th><th>уверенность день</th><th>уверенность ночь</th>'
                             '<th>яркость день</th><th>яркость ночь</th></tr>%s</table>' % (days, "".join(rows)))
                worst = max(abs(g) for g in gaps)
                shot_verdict = ("Текущие настройки на снимках: днём и ночью находят палки близко (разница до %d п.п. при %d кадрах каждого времени) — отдельный конфиг для ночи не нужен." % (round(worst), len(d))
                                if worst < 10 else
                                "Текущие настройки на снимках: днём и ночью палки находятся по-разному (разница до %d п.п.) — стоит проверить отдельные настройки на ночь." % round(worst))

    # — журнал сторожа —
    ev_rows = []
    try:
        for line in (data_dir / "autocal_watch.jsonl").read_text(encoding="utf-8").splitlines()[-400:]:
            e = json.loads(line)
            if e.get("cam") == cam_id and e.get("kind") != "check":
                ev_rows.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                    time.strftime("%d.%m %H:%M", time.localtime(e["ts"])), "день" if is_day(e["ts"]) else "ночь", esc(e["kind"]), esc(e["msg"])))
    except (OSError, ValueError):
        pass

    # — итог —
    tuned = [(v, g) for _e, _z, v, g in era_rows if g is not None and v["all"] >= TUNED_PCT]        # периоды с подогнанными настройками
    loose = [abs(g) for _e, _z, v, g in era_rows if g is not None and v["all"] < TUNED_PCT]
    parts = []
    thr_html = ""
    if tuned:
        hrs = sum(v["hours"] for v, _g in tuned)
        dm, nm = float(np.mean([v["day"] for v, _g in tuned])), float(np.mean([v["night"] for v, _g in tuned]))
        dw, nw = [p for v, _g in tuned for p in v["dw"]], [p for v, _g in tuned for p in v["nw"]]
        parts.append("С подогнанными настройками (%d зоно-часов) палки находятся днём в %d %% времени, ночью в %d %% — ночью ниже на %d п.п. Это немного: свет хорошо "
                     "подогнанный конфиг не разваливает (отдельный «ночной» конфиг дал бы выигрыш порядка этих %d п.п.)." % (round(hrs), round(dm), round(nm), round(dm - nm), round(dm - nm)))
        rows = []
        for thr in (95, 90, 85, 80):
            a, b = 100.0 * sum(1 for p in dw if p < thr) / len(dw), 100.0 * sum(1 for p in nw if p < thr) / len(nw)
            rows.append("<tr><td>ниже %d %%</td><td class=n>%d %%</td><td class=n>%d %%</td></tr>" % (thr, round(a), round(b)))
        thr_html = ("<h2>Какой порог выбрать</h2><p class=sub>Как часто 30-минутное окно оказывается ниже порога в периоды с подогнанными настройками. "
                    "Чем чаще, тем чаще сторож будет запускать подгонку без нужды.</p><table><tr><th>Порог</th><th>окон днём</th><th>окон ночью</th></tr>%s</table>" % "".join(rows))
    else:
        parts.append("Периодов с подогнанными настройками (видимость от %d %%) достаточной длины пока нет — вывод о влиянии света сделать нельзя." % TUNED_PCT)
    if loose and tuned:
        parts.append("С неподходящими настройками разница день − ночь доходила до %d п.п.: свет добавляется к уже плохому конфигу." % round(max(loose)))
    if shot_verdict:
        parts.append(shot_verdict)
    verdict = "".join("<p>%s</p>" % esc(p) for p in parts)

    era_html = "".join(
        "<tr><td>%s — %s</td><td>%s</td><td class=n>%s</td><td class=n>%s</td><td class=n>%s</td><td class=n>%s / %s</td><td class=n>%s</td><td class=n>%d</td><td class=sm>%s</td></tr>" % (
            time.strftime("%d.%m %H:%M", time.localtime(e["from"])), time.strftime("%d.%m %H:%M", time.localtime(e["to"])), esc(names.get(z, z)),
            _pct(v["day"]), _pct(v["night"]), "—" if gap is None else "%+d п.п." % round(gap), _pct(v["ok_day"]), _pct(v["ok_night"]), _pct(v["worst"]), round(v["hours"]),
            esc(_sig_text({z: e["sig"][z]})))
        for e, z, v, gap in era_rows)

    css = """
:root{--bg:#fff;--fg:#111827;--mut:#6b7280;--line:#e5e7eb;--none:#e5e7eb}
@media (prefers-color-scheme:dark){:root{--bg:#0f1116;--fg:#e5e7eb;--mut:#9ca3af;--line:#262a33;--none:#262a33}}
body{margin:0;padding:24px 16px;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,Segoe UI,sans-serif}
main{max-width:1000px;margin:0 auto}h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 6px}h3{font-size:14px;margin:16px 0 4px}small{color:var(--mut);font-weight:400}
.sub{color:var(--mut);margin:0 0 10px}table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:5px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
.n{text-align:right;white-space:nowrap}.sm{color:var(--mut);font-size:11px}.row{display:flex;align-items:center;gap:8px;margin:1px 0}.d{width:42px;color:var(--mut);font-size:11px}
.s{display:flex;flex:1}.s i{flex:1;height:14px;margin-right:1px;border-radius:1px}.s i.mk{outline:2px solid var(--fg);outline-offset:-2px}
.legend i{display:inline-block;width:12px;height:12px;margin:0 4px 0 12px;vertical-align:-2px;border-radius:2px}.box{border:1px solid var(--line);border-radius:10px;padding:4px 16px;margin:14px 0}
.scale{display:flex;justify-content:space-between;color:var(--mut);font-size:11px;margin-left:50px}
"""
    legend = "".join('<i style="background:%s"></i>%s' % (c, t) for c, t in (("#dc2626", "&lt; 50 %"), ("#f97316", "50–80 %"), ("#eab308", "80–95 %"), ("#16a34a", "≥ 95 %"), ("var(--none)", "нет данных")))
    return ("<!doctype html><html lang=ru><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>Валы: день и ночь</title><style>%s</style></head><body><main>"
            "<h1>Валы: день и ночь</h1><p class=sub>%s · данные за %d дн. по %s · день 08–20, ночь 20–08 · сформировано %s</p>"
            "<div class=box>%s</div>"
            "<h2>Периоды неизменных настроек</h2><p class=sub>Видимость = доля секунд, когда палка найдена (секундные тренды, 30-минутные окна); день и ночь — медиана окон. Период начинается с правки "
            "цветов, оси или радиуса. Берутся периоды не короче %d ч.</p><table><tr><th>Период</th><th>Вал</th><th>день</th><th>ночь</th><th>день − ночь</th>"
            "<th>окон ≥ 95 %%: день / ночь</th><th>худшие 30 мин</th><th>часов</th><th>настройки</th></tr>%s</table>%s"
            "<h2>Видимость по получасам</h2><p class=sub>%s · рамка — момент, когда менялись настройки палок</p>"
            "<div class=scale><span>00</span><span>06</span><span>12</span><span>18</span><span>24</span></div>%s"
            "%s<h2>Журнал подгонки</h2>%s</main></body></html>") % (
        css, esc(cam.get("name") or cam_id), days, esc(data_dir), time.strftime("%d.%m.%Y %H:%M", time.localtime(now)), verdict, MIN_ERA_H,
        era_html or "<tr><td colspan=9>нет периодов достаточной длины</td></tr>", thr_html, '<span class=legend>%s</span>' % legend, "".join(strips), shot_html,
        "<table><tr><th>Когда</th><th></th><th>Событие</th><th>Что</th></tr>%s</table>" % "".join(ev_rows) if ev_rows else "<p class=sub>Сторож подгонки пока ничего не делал.</p>")
