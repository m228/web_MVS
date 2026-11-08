"""motion_service для фейк-режима: настоящий сервис (motion_service/server.py), но камеры с адресами 127.0.0.x открываются
не через RTSP, а как FakeCapture (синтетические кадры из fake_frames). Остальные адреса — как обычно.

Запускает scripts/fake_server.py; руками: python scripts/fake_motion_run.py (порт MOTION_PORT, данные MOTION_DATA)."""
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(REPO / "motion_service"), str(REPO)]

import fake_frames  # noqa: E402
import fake_mode  # noqa: E402
import server as motion_server  # noqa: E402


def capture_factory(url, transport="tcp", hw_accel="off"):
    if fake_mode.is_fake_url(url):
        return fake_frames.FakeCapture("rtsp-" + url.split("@")[-1].split(":")[0], fps=8.0, kind="rtsp")
    return motion_server.open_capture(url, transport, hw_accel)


if __name__ == "__main__":
    import uvicorn
    os.environ.setdefault("MOTION_AUTORECORD", "0")        # в фейке не копим «сырое» на диск
    uvicorn.run(motion_server.create_app(capture_factory=capture_factory),
                host=os.environ.get("MOTION_HOST", "127.0.0.1"), port=int(os.environ.get("MOTION_PORT", "8768")), log_level="warning")
