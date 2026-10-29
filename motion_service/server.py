"""Сервис движения: анализ видео с IP-камер по зонам (датчик вращения, уровень шахты, жом на ленте) → Modbus.

Запускается РЯДОМ с web_MVS (как cv_service и video_service), своим venv; web_MVS раз в минуту спрашивает /health.
Настройки (камеры, зоны, выводы в ПЛК) лежат в data\\motion_config.json и правятся в мультипросмотре web_MVS (кнопка «КЗ», нижняя панель)
(или PUT /config). Каждая камера — свой поток; изменили одну — перезапускается только она.

Эндпоинты:
  GET  /health                    — жив, версия, сколько камер и сколько из них «живы».
  GET  /config   PUT /config      — весь конфиг (проверяется; ошибка → 400 с текстом по-русски).
  GET  /state                     — состояние всех камер (биты зон, метрики flow/diff, группы, ПЛК) и сценариев записи.
  GET  /cameras/{id}/state        — то же по одной камере.
  GET  /cameras/{id}/snapshot     — последний кадр JPEG (?w=1280 — уменьшить; заголовки X-Frame-W/H — размер оригинала).
  GET  /cameras/{id}/history      — ряды flow/diff/bit для графиков калибровки (?n=300).
  POST /cameras/{id}/record       — {"on": true|false}: писать метрики каждого кадра в CSV (data\\records).
  POST /modbus/ping               — {"host","port","device_id","address"}: прочитать один регистр (только чтение).
  GET  /logs                      — последние строки журнала сервиса.

Порт 8768 (MOTION_PORT), адрес 127.0.0.1 (MOTION_HOST). Данные — MOTION_DATA (по умолчанию data\\ рядом).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Optional

from fastapi import Body, FastAPI, HTTPException, Response

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
# «Stream timeout triggered…» от ffmpeg на каждой попытке подключения к мёртвой камере — шум; о смене состояния камеры пишем сами
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")

import motion_engine as engine  # noqa: E402
from motion_camera import CameraWorker, open_capture  # noqa: E402
from motion_modbus import LinkRegistry, ModbusLink  # noqa: E402
from motion_scenarios import ScenarioRunner  # noqa: E402

log = logging.getLogger("motion")


def _read_version() -> str:
    try:
        return (HERE / "VERSION").read_text(encoding="utf-8").strip() or "dev"
    except Exception:
        return "dev"


SERVICE_VERSION = _read_version()


class RingLog(logging.Handler):
    def __init__(self, n=300):
        super().__init__()
        self.lines: deque = deque(maxlen=n)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

    def emit(self, record):
        try:
            self.lines.append(self.format(record))
        except Exception:
            pass


class Manager:
    """Конфиг на диске + рабочие потоки камер."""

    def __init__(self, data_dir: Path, capture_factory: Callable = open_capture, client_factory=None):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "motion_config.json"
        self.capture_factory = capture_factory
        self.links = LinkRegistry(client_factory)
        self.client_factory = client_factory
        self.workers: dict[str, CameraWorker] = {}
        self.cfg = engine.default_config()
        self._lock = threading.RLock()
        self.scenarios = ScenarioRunner(self)

    # --- конфиг ---
    def load(self):
        if not self.path.is_file():
            log.info("Конфига нет — пустой (камеры включаются кнопкой «КЗ» в мультипросмотре web_MVS)")
            return
        try:
            self.cfg = engine.normalize_config(json.loads(self.path.read_text(encoding="utf-8-sig")))
        except Exception as e:
            bad = self.path.with_name(self.path.name + ".corrupt")
            try:
                os.replace(self.path, bad)
            except OSError:
                pass
            log.error("Конфиг не прочитан (%s) — файл сохранён как %s, стартуем пустыми", e, bad.name)
            self.cfg = engine.default_config()

    def _save(self, cfg: dict):
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        bk = self.dir / "backups"
        bk.mkdir(exist_ok=True)
        (bk / ("motion_config_%s.json" % time.strftime("%Y%m%d_%H%M%S"))).write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        for old in sorted(bk.glob("motion_config_*.json"))[:-20]:
            try:
                old.unlink()
            except OSError:
                pass

    def apply(self, raw: dict, save: bool = True) -> dict:
        """Проверить, сохранить, перезапустить ТОЛЬКО изменившиеся камеры. ValueError — конфиг плохой, ничего не тронуто."""
        new = engine.normalize_config(raw)
        with self._lock:
            old_cams = {c["id"]: c for c in self.cfg["cameras"]}
            same_mb = new["modbus"] == self.cfg["modbus"]
            if save:
                self._save(new)
            self.cfg = new
            keep = {}
            for c in new["cameras"]:
                w = self.workers.get(c["id"])
                if w and old_cams.get(c["id"]) == c and same_mb and w.is_alive():
                    keep[c["id"]] = w
            for cid, w in list(self.workers.items()):
                if cid not in keep:
                    w.stop()
            self.workers = keep
            for c in new["cameras"]:
                if c["id"] not in self.workers:
                    w = CameraWorker(c, new["modbus"], self.links, self.dir, self.capture_factory)
                    self.workers[c["id"]] = w
                    w.start()
            return new

    def start(self):
        self.load()
        self.apply(self.cfg, save=False)
        self.scenarios.start()

    def shutdown(self):
        self.scenarios.stop()
        with self._lock:
            for w in self.workers.values():
                w.stop()
            self.workers = {}
        self.links.close_all()

    def worker(self, cid: str) -> CameraWorker:
        w = self.workers.get(cid)
        if not w:
            raise HTTPException(404, "нет такой камеры: %s" % cid)
        return w


def create_app(data_dir: Optional[Path] = None, capture_factory: Callable = open_capture, client_factory=None) -> FastAPI:
    data_dir = Path(data_dir or os.environ.get("MOTION_DATA") or HERE / "data")
    ring = RingLog()
    logging.getLogger("motion").addHandler(ring)
    mgr = Manager(data_dir, capture_factory, client_factory)

    @asynccontextmanager
    async def lifespan(_app):
        mgr.start()
        try:
            yield
        finally:
            mgr.shutdown()

    app = FastAPI(title="web_MVS motion service", version=SERVICE_VERSION, lifespan=lifespan)
    app.state.mgr = mgr

    @app.get("/health")
    def health():
        sts = [w.state() for w in mgr.workers.values()]
        alive = sum(1 for s in sts if s["result"] and s["result"]["alive"])
        return {"ok": True, "service": "motion", "version": SERVICE_VERSION, "cameras": len(sts), "alive": alive}

    @app.get("/config")
    def get_config():
        return mgr.cfg

    @app.put("/config")
    def put_config(cfg: dict = Body(...)):
        try:
            new = mgr.apply(cfg)
        except ValueError as e:
            raise HTTPException(400, str(e))
        log.info("Конфиг сохранён: камер %d", len(new["cameras"]))
        return new

    @app.get("/state")
    def state():
        return {"cameras": [w.state() for w in mgr.workers.values()], "scenarios": mgr.scenarios.snapshot()}

    @app.get("/cameras/{cid}/state")
    def cam_state(cid: str):
        return mgr.worker(cid).state()

    @app.get("/cameras/{cid}/snapshot")
    def snapshot(cid: str, w: int = 0):
        r = mgr.worker(cid).snapshot_jpeg(max(0, min(int(w), 4096)))
        if r is None:
            raise HTTPException(404, "кадра ещё нет (камера не подключилась)")
        data, (fw, fh) = r
        return Response(content=data, media_type="image/jpeg",
                        headers={"X-Frame-W": str(fw), "X-Frame-H": str(fh), "Cache-Control": "no-store"})

    @app.get("/cameras/{cid}/history")
    def history(cid: str, n: int = 300, window: float = 0, points: int = 600):
        # window > 0 — последние window секунд из длинной истории (до 2 ч), ужатые до points точек; иначе последние n кадров
        return mgr.worker(cid).history(max(1, min(int(n), 900)), max(0.0, min(float(window), 7200.0)), max(10, min(int(points), 2000)))

    @app.post("/cameras/{cid}/record")
    def record(cid: str, req: dict = Body(...)):
        wk = mgr.worker(cid)
        if req.get("on"):
            return {"recording": wk.start_record()}
        wk.stop_record()
        return {"recording": None}

    @app.post("/modbus/ping")
    def modbus_ping(req: dict = Body(...)):
        try:
            mb = engine.normalize_modbus(req, base=None)
            addr = int(req.get("address", 0))
        except ValueError as e:
            raise HTTPException(400, str(e))
        link = ModbusLink(mb["host"], mb["port"], mb["device_id"], client_factory)
        ok, info = link.read_holding(addr)
        link.close()
        return {"ok": ok, "info": info}

    @app.get("/logs")
    def logs(n: int = 100):
        return {"lines": list(ring.lines)[-max(1, min(int(n), 300)):]}

    return app


def main():
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    host = os.environ.get("MOTION_HOST", "127.0.0.1")
    port = int(os.environ.get("MOTION_PORT", "8768"))
    log.info("motion_service %s -> http://%s:%d", SERVICE_VERSION, host, port)
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
