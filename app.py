from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from logger import get_events, log_event

import camera_core as core


def api_log(source: str, message: str, level: str = "info", payload: dict | None = None):
    log_event(source, message, level, payload)


@asynccontextmanager
async def lifespan(app: FastAPI):
    api_log("app", "Запуск приложения")
    core.load_driver()
    core.scan_cams()
    yield
    api_log("app", "Остановка приложения")


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="page/static"), name="static")


@app.get("/")
def home():
    return FileResponse("page/index.html")


@app.get("/camera")
def camera():
    return FileResponse("page/camera.html")


@app.get("/api/debug/logs")
def api_debug_logs(since_id: int = 0):
    return get_events(since_id)


@app.get("/api/cams")
def api_cams():
    data = core.scan_cams()
    api_log("api.cams", "Получен список камер", payload={"count": len(data)})
    return data


@app.get("/api/status")
def api_status():
    try:
        status = core.check()
        api_log("api.status", "Получен статус драйвера", payload={"status": status})
        return {"status": status}
    except Exception as error:
        api_log("api.status", "Ошибка получения статуса драйвера", "error", {"error": str(error)})
        return {"status": False, "error": str(error)}


@app.get("/api/ip")
def get_ip(serial_number: str):
    data = core.get_ip(serial_number)
    api_log("api.get_ip", "Получен IP камеры", payload={"serial_number": serial_number, "data": data})
    return data


@app.get("/api/count_cams")
def count_cams():
    data = core.count_cams()
    api_log("api.count_cams", "Получено количество камер", payload=data)
    return data


@app.get("/api/get_network_settings")
def network_settings(serial_number: str):
    ip, mask, gateway, dhcp = core.get_network_settings(serial_number)
    if ip is None:
        api_log(
            "api.get_network_settings",
            "Не удалось получить сетевые настройки",
            "warn",
            {"serial_number": serial_number},
        )
        return {"error": "Не удалось получить сетевые настройки"}

    data = {
        "ip": ip,
        "mask": mask,
        "gateway": gateway,
        "dhcp": dhcp,
    }
    api_log("api.get_network_settings", "Получены сетевые настройки", payload={"serial_number": serial_number, **data})
    return data


@app.get("/api/network_settings_advanced")
def network_settings_advanced():
    data = core.set_advanced_network_settings()
    api_log("api.network_settings_advanced", "Включены расширенные сетевые настройки", payload=data)
    return data


@app.get("/api/change_ip")
def change_ip(
    serial_number: str,
    ip: str,
    mask: str = "",
    gateway: str = "",
):
    payload = {
        "serial_number": serial_number,
        "ip": ip,
        "mask": mask,
        "gateway": gateway,
    }
    api_log("api.change_ip", "Запрошено изменение сетевых настроек", payload=payload)
    data = core.change_ip(serial_number, ip, mask, gateway)
    api_log("api.change_ip", "Получен ответ изменения сетевых настроек", payload={**payload, "result": data})
    return data


@app.get("/api/camera/stream")
def camera_stream(
    serial_number: str,
    width: int = None,
    height: int = None,
    offset_x: int = None,
    offset_y: int = None,
    fps: float = None,
    exposure_auto: str = None,
    exposure_time: float = None,
):
    api_log(
        "api.camera.stream",
        "Запрошен видеопоток",
        payload={
            "serial_number": serial_number,
            "width": width,
            "height": height,
            "offset_x": offset_x,
            "offset_y": offset_y,
            "fps": fps,
            "exposure_auto": exposure_auto,
            "exposure_time": exposure_time,
        },
    )
    return StreamingResponse(
        core.generate_stream(
            serial_number=serial_number,
            width=width,
            height=height,
            offset_x=offset_x,
            offset_y=offset_y,
            fps=fps,
            exposure_auto=exposure_auto,
            exposure_time=exposure_time,
        ),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/api/camera/close_stream")
def close_stream():
    data = core.close_stream()
    api_log("api.camera.close_stream", "Запрошена мягкая остановка потока", payload=data)
    return data


@app.get("/api/camera/close_stream_force")
def close_stream_force():
    data = core.close_stream_force()
    api_log("api.camera.close_stream_force", "Запрошена принудительная остановка потока", "warn", data)
    return data


@app.get("/api/camera/stream_state")
def stream_state():
    return core.get_stream_state()


@app.get("/api/camera/metrics")
def metrics():
    return core.get_metrics()


@app.get("/api/camera/data_limit")
def data_limit(serial_number: str):
    data = core.get_data_limit(serial_number)
    api_log("api.camera.data_limit", "Получены ограничения камеры", payload={"serial_number": serial_number})
    return data


@app.get("/api/camera/on_save_photo")
def on_save_photo(interval: int):
    data = core.on_save(interval)
    api_log("api.camera.on_save_photo", "Включено сохранение фото", payload={"interval": interval, "result": data})
    return data


@app.get("/api/camera/off_save_photo")
def off_save_photo():
    data = core.off_save()
    api_log("api.camera.off_save_photo", "Выключено сохранение фото", payload=data)
    return data


@app.get("/api/camera/on_save_video")
def on_save_video(duration: int):
    data = core.on_video(duration)
    api_log("api.camera.on_save_video", "Включена запись видео", payload={"duration": duration, "result": data})
    return data


@app.get("/api/camera/off_save_video")
def off_save_video():
    data = core.off_video()
    api_log("api.camera.off_save_video", "Выключена запись видео", payload=data)
    return data


@app.get("/api/camera/status_video_photo")
def status_video_photo():
    return {"video": core.video_enabled, "photo": core.photo_enabled}
