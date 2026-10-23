"""Видео-сервис: ролик варки по папкам проб web_MVS — фото с контурами + рассев + графики, MP4 (H.264).

Запускается РЯДОМ с web_MVS (как cv_service), своим venv. web_MVS раз в несколько секунд спрашивает /health: отвечает —
на странице микроскопа появляется вкладка «Ролик», нет — вкладки нет. Сборка идёт в фоне, по одной, процесс работает
с пониженным приоритетом — распознавание во время варки не тормозит.

Эндпоинты:
  GET  /health        — жив, версия, занят ли (и какая сборка идёт).
  POST /build         — {"dirs": [папки проб], "name": "файл.mp4"?, "per_probe": 0.5?, "smooth": 90?} → {"id"}.
  GET  /jobs/{id}     — состояние сборки: queued / running / done / error, кадров сделано / всего, имя файла.
  GET  /files         — готовые ролики в video\\ (имя, размер, время).
  GET  /file/{name}   — скачать ролик.

Порт 8767 (VIDEO_PORT), адрес 127.0.0.1 (VIDEO_HOST).
"""
from __future__ import annotations

import ctypes
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse

import render

HERE = Path(__file__).resolve().parent


def _read_version() -> str:
    """Версия сервиса = номер релиза web_MVS, с которым собран архив (файл VERSION). Из исходников файла нет — «dev»."""
    try:
        return (HERE / "VERSION").read_text(encoding="utf-8").strip() or "dev"
    except Exception:
        return "dev"


SERVICE_VERSION = _read_version()
app = FastAPI(title="web_MVS video service", version=SERVICE_VERSION)
_lock = threading.Lock()
_jobs: dict[str, dict] = {}            # последние сборки (в памяти; после перезапуска — только готовые файлы в video\)
_busy: Optional[str] = None             # id идущей сборки
_KEEP_JOBS = 20
_BAD = re.compile(r"[^\w\-. ]", re.UNICODE)


def _safe_name(name: str) -> str:
    """Имя файла ролика без путей и странных символов; всегда .mp4."""
    n = _BAD.sub("_", Path(str(name)).name).strip(" .") or "ролик"
    return n if n.lower().endswith(".mp4") else n + ".mp4"


def _low_priority():
    """Пониженный приоритет процесса: ffmpeg наследует его же — ролик собирается, когда CPU свободен."""
    try:
        if os.name == "nt":
            k = ctypes.windll.kernel32
            k.SetPriorityClass(k.GetCurrentProcess(), 0x4000)          # BELOW_NORMAL_PRIORITY_CLASS
        else:
            os.nice(10)
    except Exception:
        pass


@app.get("/health")
def health():
    job = _jobs.get(_busy) if _busy else None
    return {"ok": True, "service": "video", "version": SERVICE_VERSION, "busy": job is not None, "job": job}


@app.post("/build")
def build(req: dict = Body(...)):
    global _busy
    dirs = [Path(d) for d in (req.get("dirs") or []) if d and Path(d).is_dir()]
    if not dirs:
        raise HTTPException(400, "нет папок проб (кадры этой варки уже стёрты?)")
    with _lock:
        if _busy:
            raise HTTPException(409, "уже собирается другой ролик")
        jid = uuid.uuid4().hex[:12]
        job = {"id": jid, "state": "queued", "done": 0, "total": 0, "probes": 0, "file": None, "error": None,
               "created": time.time(), "finished": None}
        _jobs[jid] = job
        _busy = jid
        for old in list(_jobs)[:-_KEEP_JOBS]:
            _jobs.pop(old, None)
    threading.Thread(target=_run, args=(job, dirs, req), daemon=True, name="video-" + jid).start()
    return {"id": jid}


def _num(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _run(job: dict, dirs: list[Path], req: dict):
    global _busy
    try:
        job["state"] = "running"
        probes = render.load_probes(dirs)
        if not probes:
            raise ValueError("у проб нет кадров с контурами (кадры этой варки уже стёрты?)")
        job["probes"] = len(probes)
        name = _safe_name(req.get("name") or render.Boil(probes).file_name())

        def prog(done: int, total: int):
            job["done"], job["total"] = done, total

        render.render_video(probes, render.OUT_DIR / name, per_probe=_num(req.get("per_probe"), render.PER_PROBE_S),
                            smooth_s=_num(req.get("smooth"), render.SMOOTH_S), progress=prog)
        job.update(state="done", file=name, finished=time.time())
    except Exception as e:
        job.update(state="error", error=str(e) or e.__class__.__name__, finished=time.time())
    finally:
        with _lock:
            _busy = None


@app.get("/jobs/{jid}")
def job_state(jid: str):
    job = _jobs.get(jid)
    if not job:
        raise HTTPException(404, "нет такой сборки (сервис перезапускали?)")
    return job


@app.get("/files")
def files():
    out = []
    if render.OUT_DIR.exists():
        for p in render.OUT_DIR.glob("*.mp4"):
            if p.name.endswith(".part.mp4"):
                continue
            st = p.stat()
            out.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
    out.sort(key=lambda f: f["mtime"], reverse=True)
    return {"files": out}


@app.get("/file/{name}")
def file(name: str):
    p = render.OUT_DIR / _safe_name(name)
    if not p.is_file():
        raise HTTPException(404, "нет такого ролика")
    return FileResponse(str(p), media_type="video/mp4", filename=p.name)


if __name__ == "__main__":
    import uvicorn
    _low_priority()
    port = int(os.environ.get("VIDEO_PORT", "8767"))
    host = os.environ.get("VIDEO_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port, log_level="info")
