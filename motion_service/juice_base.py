"""База для обучения «Сок»: раз в N минут кадр камеры и цвет/мутность/состояние каждого стакана + ручная метка оператора.

Складывается в data\\juice_base\\<камера>\\<ГГГГММДД>\\: <ЧЧММСС>.jpg и index.csv (одна строка на стакан на момент).
Включает оператор на вкладке «Сок» (камера.juice_base = {on, every_min}); выключил — запись прекращается. Старше KEEP_DAYS суток убираем.
"""
from __future__ import annotations

import csv
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

log = logging.getLogger("motion.juice")

KEEP_DAYS = 14
JPEG_W = 1280                  # ширина сохранённого кадра
MIN_FREE_BYTES = 1 << 30       # свободного места меньше — не пишем
LEVELS = ("clear", "turbid", "dirty")
LABELS = ("",) + LEVELS        # ручная метка: нет / прозрачный / мутный / грязный
COLS = ["time", "file", "zone", "name", "L", "a", "b", "turb", "level", "valid", "label"]


def base_dir(data_dir: Path, cam_id: str) -> Path:
    return Path(data_dir) / "juice_base" / re.sub(r"[^\w.-]", "_", str(cam_id))


def rows_for(state: dict, zones_cfg: list) -> list:
    """Строки по стаканам из состояния камеры (state["zones"]) и конфига зон (метка)."""
    label = {z["id"]: z.get("label", "") for z in zones_cfg if z.get("type") == "clarity"}
    out = []
    for z in state.get("zones", []):
        if z.get("type") != "clarity" or z["id"] not in label:
            continue
        lab = z.get("lab") or [None, None, None]
        ok = bool(z.get("valid"))                          # нет эталонов / цвет не на шкале: мутность и состояние пустые (не «0 — чистый»), цвет всё равно пишем
        empty = bool(z.get("empty"))                       # нет движения: стакан пуст — пишем состояние «empty», мутность пустая (осадок — не сок)
        out.append([z["id"], z.get("name", z["id"]), lab[0], lab[1], lab[2], z.get("turb", "") if ok else "", "empty" if empty else z.get("level_name", "") if ok else "", int(ok), label[z["id"]]])
    return out


def worth_saving(rows: list) -> bool:
    """Снимок нужен, если хоть у одного стакана есть цвет — даже без эталонов: база для обучения и нужна, чтобы эталоны потом подобрать по данным."""
    return any(r[2] not in (None, "") for r in rows)


def save(data_dir: Path, cam_id: str, ts: float, frame: Optional[np.ndarray], rows: list) -> Optional[Path]:
    """Записать кадр и строки. -> путь JPEG или None (нет кадра/строк, мало места, ошибка диска — камеру это не ломает)."""
    if frame is None or not rows:
        return None
    try:
        free = shutil.disk_usage(str(data_dir)).free
        if free < MIN_FREE_BYTES:
            log.warning("база сока: на диске меньше %d МБ, кадр пропущен", MIN_FREE_BYTES >> 20)
            return None
        f = frame
        if f.ndim == 2:
            f = cv2.cvtColor(f, cv2.COLOR_GRAY2BGR)
        h, w = f.shape[:2]
        if w > JPEG_W:
            f = cv2.resize(f, (JPEG_W, int(round(h * JPEG_W / w))), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 88])
        if not ok:
            return None
        folder = base_dir(data_dir, cam_id) / time.strftime("%Y%m%d", time.localtime(ts))
        folder.mkdir(parents=True, exist_ok=True)
        name = time.strftime("%H%M%S", time.localtime(ts)) + ".jpg"
        (folder / name).write_bytes(buf.tobytes())
        idx = folder / "index.csv"
        new = not idx.exists()
        with open(idx, "a", newline="", encoding="utf-8-sig") as fh:
            wr = csv.writer(fh)
            if new:
                wr.writerow(COLS)
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
            for r in rows:
                wr.writerow([stamp, name] + [("%.2f" % v if isinstance(v, float) else v) for v in r])
        return folder / name
    except OSError as e:
        log.warning("база сока: не записано: %s", e)
        return None


def purge(data_dir: Path, cam_id: str, now: Optional[float] = None, days: float = KEEP_DAYS) -> int:
    """Убрать папки дней старше days суток. -> сколько убрано."""
    base, n = base_dir(data_dir, cam_id), 0
    if not base.is_dir():
        return 0
    edge = time.strftime("%Y%m%d", time.localtime((now or time.time()) - days * 86400))
    for d in base.iterdir():
        if d.is_dir() and len(d.name) == 8 and d.name.isdigit() and d.name < edge:
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n
