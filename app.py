import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles

from logger import get_events, log_event

from camera_core import manager, build_rtsp_url
import rtsp_store
import net_tools


def api_log(source: str, message: str, level: str = "info", payload: dict | None = None):
    log_event(source, message, level, payload)


@asynccontextmanager
async def lifespan(app: FastAPI):
    api_log("app", "Запуск приложения")
    # версии Python/genicam/harvesters + дата .cti — видно, не обновилась ли
    # библиотека (типовая причина "драйвер раньше работал, теперь нет")
    manager.log_environment()
    manager.load_driver()
    # даём продюсеру время на обнаружение камер, иначе первый опрос ловит ошибки
    await asyncio.sleep(2.0)
    manager.scan_cams()
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


@app.get("/rtsp")
def rtsp_page():
    return FileResponse("page/rtsp.html")


@app.get("/multi")
def multi_page():
    return FileResponse("page/multi.html")


@app.get("/network")
def network_page():
    return FileResponse("page/network.html")


@app.get("/api/debug/logs")
def api_debug_logs(since_id: int = 0):
    return get_events(since_id)


@app.get("/api/cams")
def api_cams():
    return manager.scan_cams()


# детальный список с разбивкой по сетевым интерфейсам (как в MVS)
@app.get("/api/cams/detailed")
def api_cams_detailed():
    manager.scan_cams()
    return manager.list_devices_grouped()


# выбрать конкретную запись (handle) и/или интерфейс, через которые открывать камеру
@app.get("/api/camera/select_interface")
def select_interface(serial_number: str, interface_id: str = "", device_handle: str = ""):
    data = manager.get(serial_number).select_interface(
        interface_id or None, device_handle or None)
    api_log("api.camera.select_interface", "Выбрана запись камеры",
            payload={"serial_number": serial_number, **data})
    return data


@app.get("/api/status")
def api_status():
    try:
        return {"status": manager.check()}
    except Exception as error:
        api_log("api.status", "Ошибка получения статуса драйвера", "error", {"error": str(error)})
        return {"status": False, "error": str(error)}


@app.get("/api/ip")
def get_ip(serial_number: str, interface_id: str = "", device_handle: str = ""):
    worker = manager.get(serial_number)
    if interface_id:
        worker.interface_id = interface_id
    if device_handle:
        worker.device_handle = device_handle
    return worker.get_ip()


@app.get("/api/count_cams")
def count_cams():
    return manager.count_cams()


@app.get("/api/get_network_settings")
def network_settings(serial_number: str, interface_id: str = "", device_handle: str = ""):
    worker = manager.get(serial_number)
    if interface_id:
        worker.interface_id = interface_id
    if device_handle:
        worker.device_handle = device_handle
    ip, mask, gateway, dhcp = worker.get_network_settings()
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
def network_settings_advanced(serial_number: str):
    data = manager.get(serial_number).set_advanced()
    api_log("api.network_settings_advanced", "Включены расширенные сетевые настройки", payload=data)
    return data


# ForceIP — задать IP камере, недоступной из-за чужой подсети (control не открыть)
@app.get("/api/force_ip")
def force_ip(serial_number: str, ip: str, mask: str = "", gateway: str = ""):
    api_log("api.force_ip", "Запрошен ForceIP",
            payload={"serial_number": serial_number, "ip": ip, "mask": mask, "gateway": gateway})
    data = manager.force_ip(serial_number, ip, mask or None, gateway or None)
    api_log("api.force_ip", "Ответ ForceIP", payload={"serial_number": serial_number, "result": data})
    return data


# ---------- мини-база сохранённых RTSP-камер ----------
@app.get("/api/rtsp/saved")
def rtsp_saved():
    return {"items": rtsp_store.load()}


@app.get("/api/rtsp/save")
def rtsp_save(url: str, label: str = "", ip: str = "", scale: int = 100, fps: float = 0):
    items = rtsp_store.save({"url": url, "label": label, "ip": ip, "scale": scale, "fps": fps})
    api_log("api.rtsp.save", "RTSP-камера сохранена в базу", payload={"url": url, "count": len(items)})
    return {"items": items}


@app.get("/api/rtsp/remove_saved")
def rtsp_remove_saved(url: str):
    items = rtsp_store.remove(url)
    api_log("api.rtsp.remove_saved", "RTSP-камера удалена из базы", payload={"url": url, "count": len(items)})
    return {"items": items}


# ---------- сетевая оптимизация приёма GigE (замена утилит MVS) ----------
@app.get("/api/net/status")
def net_status():
    return net_tools.status()


@app.get("/api/net/enable_jumbo")
def net_enable_jumbo(adapter: str):
    data = net_tools.enable_jumbo(adapter)
    api_log("api.net.enable_jumbo", "Включение jumbo-кадров", payload={"adapter": adapter, "result": data})
    return data


@app.get("/api/net/enable_filter")
def net_enable_filter(adapter: str):
    data = net_tools.enable_filter(adapter)
    api_log("api.net.enable_filter", "Включение фильтр-драйвера GigE", payload={"adapter": adapter, "result": data})
    return data


@app.get("/api/net/disable_jumbo")
def net_disable_jumbo(adapter: str):
    data = net_tools.disable_jumbo(adapter)
    api_log("api.net.disable_jumbo", "Выключение jumbo-кадров", payload={"adapter": adapter, "result": data})
    return data


@app.get("/api/net/disable_filter")
def net_disable_filter(adapter: str):
    data = net_tools.disable_filter(adapter)
    api_log("api.net.disable_filter", "Выключение фильтр-драйвера GigE", payload={"adapter": adapter, "result": data})
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
    data = manager.get(serial_number).change_ip(ip, mask, gateway)
    api_log("api.change_ip", "Получен ответ изменения сетевых настроек", payload={**payload, "result": data})
    return data


@app.get("/api/camera/stream")
def camera_stream(
    serial_number: str,
    interface_id: str = "",
    device_handle: str = "",
    width: int = None,
    height: int = None,
    offset_x: int = None,
    offset_y: int = None,
    fps: float = None,
    exposure_auto: str = None,
    exposure_time: float = None,
    pixel_format: str = None,
):
    worker = manager.get(serial_number)
    if interface_id:
        worker.interface_id = interface_id
    if device_handle:
        worker.device_handle = device_handle
    api_log(
        "api.camera.stream",
        "Запрошен видеопоток",
        payload={
            "serial_number": serial_number,
            "interface_id": worker.interface_id,
            "width": width,
            "height": height,
            "offset_x": offset_x,
            "offset_y": offset_y,
            "fps": fps,
            "exposure_auto": exposure_auto,
            "exposure_time": exposure_time,
            "pixel_format": pixel_format,
        },
    )
    return StreamingResponse(
        worker.generate(
            width=width,
            height=height,
            offset_x=offset_x,
            offset_y=offset_y,
            fps=fps,
            exposure_auto=exposure_auto,
            exposure_time=exposure_time,
            pixel_format=pixel_format,
        ),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/api/camera/close_stream")
def close_stream(serial_number: str):
    data = manager.get(serial_number).close()
    api_log("api.camera.close_stream", "Запрошена мягкая остановка потока", payload=data)
    return data


@app.get("/api/camera/close_stream_force")
def close_stream_force(serial_number: str):
    data = manager.get(serial_number).force_close()
    api_log("api.camera.close_stream_force", "Запрошена принудительная остановка потока", "warn", data)
    return data


@app.get("/api/camera/stream_state")
def stream_state(serial_number: str):
    return manager.get(serial_number).stream_state()


@app.get("/api/camera/metrics")
def metrics(serial_number: str):
    return manager.get(serial_number).metrics


@app.get("/api/camera/data_limit")
def data_limit(serial_number: str):
    return manager.get(serial_number).data_limit


@app.get("/api/camera/info")
def camera_info(serial_number: str, interface_id: str = "", device_handle: str = ""):
    worker = manager.get(serial_number)
    if interface_id:
        worker.interface_id = interface_id
    if device_handle:
        worker.device_handle = device_handle
    data = worker.get_info()
    if not data:
        api_log("api.camera.info", "Не удалось получить информацию о камере", "warn", {"serial_number": serial_number})
        return {"error": "Не удалось получить информацию о камере"}
    api_log("api.camera.info", "Получена информация о камере",
            payload={"serial_number": serial_number, "count": len(data.get("items", []))})
    return data


@app.get("/api/camera/on_save_photo")
def on_save_photo(serial_number: str, interval: int):
    data = manager.get(serial_number).on_photo(interval)
    api_log("api.camera.on_save_photo", "Включено сохранение фото", payload={"interval": interval, "result": data})
    return data


@app.get("/api/camera/off_save_photo")
def off_save_photo(serial_number: str):
    data = manager.get(serial_number).off_photo()
    api_log("api.camera.off_save_photo", "Выключено сохранение фото", payload=data)
    return data


@app.get("/api/camera/on_save_video")
def on_save_video(serial_number: str, duration: int):
    data = manager.get(serial_number).on_video(duration)
    api_log("api.camera.on_save_video", "Включена запись видео", payload={"duration": duration, "result": data})
    return data


@app.get("/api/camera/off_save_video")
def off_save_video(serial_number: str):
    data = manager.get(serial_number).off_video()
    api_log("api.camera.off_save_video", "Выключена запись видео", payload=data)
    return data


@app.get("/api/camera/status_video_photo")
def status_video_photo(serial_number: str):
    worker = manager.get(serial_number)
    return {"video": worker.save_video, "photo": worker.save_photo}


# ---------- RTSP-камера (просмотр / запись / снимки) ----------


def _resolve_rtsp_url(url, ip, username, password, channel, subtype):
    if url:
        return url
    if ip:
        return build_rtsp_url(ip, username, password, channel, subtype)
    return None


@app.get("/api/rtsp/stream")
def rtsp_stream(
    serial_number: str,
    url: str = None,
    ip: str = None,
    username: str = "admin",
    password: str = "",
    channel: int = 1,
    subtype: int = 0,
    scale: int = 100,
    fps: float = None,
):
    rtsp_url = _resolve_rtsp_url(url, ip, username, password, channel, subtype)
    worker = manager.get_rtsp(serial_number, rtsp_url)
    if worker is None:
        api_log("api.rtsp.stream", "RTSP-камера не зарегистрирована", "warn", {"serial_number": serial_number})
        return {"error": "rtsp_url_required"}

    api_log("api.rtsp.stream", "Запрошен RTSP-видеопоток",
            payload={"serial_number": serial_number, "rtsp_url": worker.rtsp_url, "scale": scale, "fps": fps})
    return StreamingResponse(
        worker.generate(scale=scale, target_fps=fps),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/api/rtsp/snapshot")
def rtsp_snapshot(serial_number: str):
    worker = manager.get_rtsp(serial_number)
    if worker is None:
        api_log("api.rtsp.snapshot", "RTSP-камера не подключена", "warn", {"serial_number": serial_number})
        return {"error": "rtsp_not_connected"}

    data = worker.snapshot()
    if not data:
        api_log("api.rtsp.snapshot", "Не удалось получить снимок", "warn", {"serial_number": serial_number})
        return {"error": "snapshot_failed"}

    api_log("api.rtsp.snapshot", "Снимок RTSP сохранён", payload={"serial_number": serial_number})
    return Response(content=data, media_type="image/jpeg")


@app.get("/api/rtsp/close_stream")
def rtsp_close_stream(serial_number: str):
    worker = manager.get_rtsp(serial_number)
    if worker is None:
        return {"error": "rtsp_not_connected"}
    data = worker.close()
    api_log("api.rtsp.close_stream", "Запрошена мягкая остановка RTSP-потока", payload=data)
    return data


@app.get("/api/rtsp/close_stream_force")
def rtsp_close_stream_force(serial_number: str):
    worker = manager.get_rtsp(serial_number)
    if worker is None:
        return {"error": "rtsp_not_connected"}
    data = worker.force_close()
    api_log("api.rtsp.close_stream_force", "Запрошена принудительная остановка RTSP-потока", "warn", data)
    return data


@app.get("/api/rtsp/stream_state")
def rtsp_stream_state(serial_number: str):
    worker = manager.get_rtsp(serial_number)
    if worker is None:
        return {"serial_number": serial_number, "running": False, "closed": True}
    return worker.stream_state()


@app.get("/api/rtsp/metrics")
def rtsp_metrics(serial_number: str):
    worker = manager.get_rtsp(serial_number)
    if worker is None:
        return {"error": "rtsp_not_connected"}
    return worker.metrics


@app.get("/api/rtsp/on_save_photo")
def rtsp_on_save_photo(serial_number: str, interval: int):
    worker = manager.get_rtsp(serial_number)
    if worker is None:
        return {"error": "rtsp_not_connected"}
    data = worker.on_photo(interval)
    api_log("api.rtsp.on_save_photo", "Включено автосохранение фото (RTSP)", payload={"interval": interval, "result": data})
    return data


@app.get("/api/rtsp/off_save_photo")
def rtsp_off_save_photo(serial_number: str):
    worker = manager.get_rtsp(serial_number)
    if worker is None:
        return {"error": "rtsp_not_connected"}
    data = worker.off_photo()
    api_log("api.rtsp.off_save_photo", "Выключено автосохранение фото (RTSP)", payload=data)
    return data


@app.get("/api/rtsp/on_save_video")
def rtsp_on_save_video(serial_number: str, duration: int):
    worker = manager.get_rtsp(serial_number)
    if worker is None:
        return {"error": "rtsp_not_connected"}
    data = worker.on_video(duration)
    api_log("api.rtsp.on_save_video", "Включена запись видео (RTSP)", payload={"duration": duration, "result": data})
    return data


@app.get("/api/rtsp/off_save_video")
def rtsp_off_save_video(serial_number: str):
    worker = manager.get_rtsp(serial_number)
    if worker is None:
        return {"error": "rtsp_not_connected"}
    data = worker.off_video()
    api_log("api.rtsp.off_save_video", "Выключена запись видео (RTSP)", payload=data)
    return data


@app.get("/api/rtsp/status_video_photo")
def rtsp_status_video_photo(serial_number: str):
    worker = manager.get_rtsp(serial_number)
    if worker is None:
        return {"video": 0, "photo": False}
    return {"video": worker.save_video, "photo": worker.save_photo}
