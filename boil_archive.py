"""boil_archive — архив варки: одна законченная варка → один ZIP, чтобы забрать её целиком и разобрать на другом компе.

Внутри (<серийник>_варка_2026-10-16_12-18__13-51.zip):
    README.md     — что где лежит
    boil.json     — паспорт варки: сводка (cv_store._boil_summary: фазы, мука финиша ± интервал, рассев, кристаллы), настройки
    timeline.csv  — по каждой пробе: режим ПЛК + CV (колонки cv_store.EXPORT_COLUMNS), «;», UTF-8 с BOM — открывается в Excel
    probes/<ts>/  — result.json, objects_N.json (контуры), frame_N, thumb.jpg. Кадры начала варки (подкачка 1, рост 1, подкачка 2)
                    пересжимаются в JPEG: мелочь там не нужна. Рост 2 и финиш — как есть, без потерь: JPEG даже q95 занижал муку
                    финиша на 7 % (замер на модели Кира 16.10), а перепрогон модели по этим кадрам должен давать заводские цифры.

Собирается во временный .part и переименовывается только целиком — Syncthing/копирование не схватит половину.
Сборкой рулит microscope_service (фон, только между варками); здесь — сама сборка, список и лимит места.
"""
from __future__ import annotations

import csv
import io
import json
import re
import time
import zipfile
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np

import cv_store
import cv_volume
from paths import DATA_DIR, read_version

DEFAULTS = {"enabled": False, "dir": "", "early_jpeg_quality": 90, "max_gb": 50.0}     # early_jpeg_quality 0 — всё без пересжатия
EARLY_PHASES = ("p1", "g1", "p2")      # начало варки: кадры этих фаз можно пересжать в JPEG
_NAME_RE = re.compile(r"_варка_(\d{4}-\d\d-\d\d_\d\d-\d\d)__(?:(\d{4}-\d\d-\d\d)_)?(\d\d-\d\d)\.zip$")

README = """# Архив варки

- `boil.json` — паспорт варки: начало/конец, СВ, фазы (подкачка/рост) и их длительность, мука финиша (`tail.fines`, % объёма
  по моделям M1 шар / M2 сфероид / M3 призма) и её 95 % интервал (`tail.fines_lo` / `tail.fines_hi`), рассев по ситам,
  крайние кристаллы, настройки порогов (`cfg`). `all` — все пробы финиша, `phases` — блоки по фазам.
- `timeline.csv` — строка на пробу: время, стадия/подстадия, фаза, СВ, температура, уровень, ток, вакуум, время варки,
  время с заводки; число кристаллов, размеры (мкм), группы, брак и его причины, мука по моделям (`fines_m1…m3`, `fines_avg`),
  рассев (`sieve_b0…b6`, % объёма: <0,2 / 0,2–0,5 / 0,5–0,7 / 0,7–0,8 / 0,8–1 / 1–1,2 / >1,2 мм), разломы.
- `probes/<время пробы>/` — `result.json` (сводка пробы и по-кадровые данные), `objects_N.json` (кристаллы кадра N:
  контур `poly` в пикселях, размер `size_um`, длина/ширина, форма, группа, брак), `frame_N.*` — кадр, `thumb.jpg` — миниатюра
  с контурами. Масштаб — в настройках CV завода (на Кире 1,629 мкм/пкс).
- Кадры {frames}.
"""


def settings(cv_cfg: Optional[dict]) -> dict:
    """Настройки архива варок (cv.boil_zip) поверх DEFAULTS."""
    return {**DEFAULTS, **(((cv_cfg or {}).get("boil_zip")) or {})}


def out_dir(st: dict) -> Path:
    """Папка архивов: из настройки или <данные>/boil_archive."""
    return Path(st["dir"]) if str(st.get("dir") or "").strip() else DATA_DIR / "boil_archive"


def archive_name(serial: str, g: list) -> str:
    """Имя архива по времени первой и последней пробы: «DA7186922_варка_2026-10-16_12-18__13-51.zip»
    (варка через полночь — у конца своя дата)."""
    a, b = time.localtime(g[0]["t"]), time.localtime(g[-1]["t"])
    end = time.strftime("%H-%M", b) if a[:3] == b[:3] else time.strftime("%Y-%m-%d_%H-%M", b)
    return "%s_варка_%s__%s.zip" % (cv_store._serial_dir(serial).name, time.strftime("%Y-%m-%d_%H-%M", a), end)


def _timeline_csv(rows: list) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(cv_store.EXPORT_COLUMNS)
    for r in rows:
        w.writerow(["" if r.get(c) is None else r.get(c) for c in cv_store.EXPORT_COLUMNS])
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


def _frame_bytes(p: Path, quality: int) -> tuple[str, bytes]:
    """Кадр в архив: quality 0 или кадр уже JPEG — как есть; иначе PNG → JPEG quality."""
    if not quality or p.suffix.lower() != ".png":
        return p.name, p.read_bytes()
    img = cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)     # fromfile — путь может быть кириллическим
    ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise RuntimeError("не удалось сжать кадр %s" % p)
    return p.stem + ".jpg", enc.tobytes()


def pack(serial: str, g: list, dest: Path, early_quality: int = 0, stop: Optional[Callable[[], bool]] = None,
         pause_s: float = 0.0) -> Optional[Path]:
    """Собрать архив варки g (строки журнала одной варки, по времени) в папку dest. early_quality — JPEG для кадров проб начала
    варки (EARLY_PHASES), 0 — без пересжатия; остальные кадры всегда как есть. stop() → True посреди сборки — бросаем,
    .part удаляем, возвращаем None (соберём в следующий раз). pause_s — пауза между пробами (не грузить CPU)."""
    dest.mkdir(parents=True, exist_ok=True)
    final = dest / archive_name(serial, g)
    part = final.with_name(final.name + ".part")
    avg_n = cv_volume.volume_cfg(cv_store._volume_cfg_now())["avg_n"]
    summary = cv_store._boil_summary(g, True, avg_n)
    timeline = cv_store.export_rows(serial, g[0]["t"], g[-1]["t"])
    sd = cv_store._serial_dir(serial)
    missing, frames_n = [], 0
    try:
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for r in g:
                if stop and stop():
                    raise InterruptedError
                d = sd / r["ts"]
                if not (d / "result.json").exists():
                    missing.append(r["ts"])
                    continue
                res = json.loads((d / "result.json").read_text(encoding="utf-8"))
                names = {}
                for p in sorted(d.glob("frame_*.*")):
                    name, data = _frame_bytes(p, early_quality if r.get("phase") in EARLY_PHASES else 0)
                    names[p.name] = name
                    z.writestr("probes/%s/%s" % (r["ts"], name), data, compress_type=zipfile.ZIP_STORED)   # картинки уже сжаты
                    frames_n += 1
                for f in res.get("frames") or []:                     # ссылки на кадры — на новые имена (PNG → JPEG)
                    if f.get("overlay") in names:
                        f["overlay"] = names[f["overlay"]]
                z.writestr("probes/%s/result.json" % r["ts"], json.dumps(res, ensure_ascii=False))
                for p in sorted(d.glob("objects_*.json")):
                    z.write(p, "probes/%s/%s" % (r["ts"], p.name))
                if (d / "thumb.jpg").exists():
                    z.write(d / "thumb.jpg", "probes/%s/thumb.jpg" % r["ts"], compress_type=zipfile.ZIP_STORED)
                if pause_s:
                    time.sleep(pause_s)
            meta = {"serial": sd.name, "version": read_version(), "packed": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "probes": len(g), "probes_missing": missing, "frames": frames_n,
                    "frames_format": (("начало варки (подкачка 1, рост 1, подкачка 2) — JPEG q%d; рост 2 и финиш — как в архиве проб, без потерь"
                                      % early_quality) if early_quality else "как в архиве проб, без пересжатия")}
            z.writestr("boil.json", json.dumps({**meta, "summary": summary}, ensure_ascii=False, indent=1))
            z.writestr("timeline.csv", _timeline_csv(timeline))
            z.writestr("README.md", README.format(frames=meta["frames_format"]))
        part.replace(final)
        return final
    except InterruptedError:
        part.unlink(missing_ok=True)
        return None
    except Exception:
        part.unlink(missing_ok=True)
        raise


def has_frames(serial: str, g: list) -> bool:
    """У варки ещё лежит хоть одна проба (ротация архива проб не стёрла всё) — есть что паковать."""
    sd = cv_store._serial_dir(serial)
    return any((sd / r["ts"] / "result.json").exists() for r in g)


def pending(serial: str, dest: Path, now: Optional[float] = None) -> list[list]:
    """Законченные варки (последняя проба старше BOIL_OPEN_S) без архива, у которых пробы ещё на диске — старые первыми."""
    now = time.time() if now is None else now
    have = {p.name for p in dest.glob("*.zip")} if dest.exists() else set()
    return [g for g in cv_store.boil_groups(serial)
            if now - g[-1]["t"] > cv_store.BOIL_OPEN_S and archive_name(serial, g) not in have and has_frames(serial, g)]


def listing(dest: Path) -> list[dict]:
    """Готовые архивы (новые первыми): имя, размер, начало/конец варки по имени."""
    out = []
    for p in sorted(dest.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True) if dest.exists() else []:
        m = _NAME_RE.search(p.name)
        out.append({"name": p.name, "size": p.stat().st_size, "mtime": p.stat().st_mtime,
                    "from": m.group(1) if m else None, "to": (m.group(2) + "_" if m and m.group(2) else "") + (m.group(3) if m else "")})
    return out


def enforce_limit(dest: Path, max_gb: float) -> list[str]:
    """Архивов больше max_gb — удаляем самые старые (по времени файла). Возвращает имена удалённых."""
    if not max_gb or max_gb <= 0 or not dest.exists():
        return []
    files = sorted(dest.glob("*.zip"), key=lambda p: p.stat().st_mtime)
    total, cap, gone = sum(p.stat().st_size for p in files), int(max_gb * 1e9), []
    for p in files:
        if total <= cap:
            break
        total -= p.stat().st_size
        p.unlink(missing_ok=True)
        gone.append(p.name)
    return gone
