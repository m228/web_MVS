import asyncio
import os
import threading
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles

from logger import get_events, log_event

from camera_core import manager, build_rtsp_url, replace_host_in_url
import rtsp_store
import save_settings
import net_tools
import updater
from microscope_service import micro
from paths import read_version, BUNDLE_DIR, DATA_DIR


def api_log(source: str, message: str, level: str = "info", payload: dict | None = None):
    log_event(source, message, level, payload)


def _is_rtsp_scheme(url: str) -> bool:
    # только rtsp(s): иначе cv2.VideoCapture открыл бы file://, http:// и пр.
    # (чтение локальных файлов / запросы во внутреннюю сеть — SSRF)
    return urlparse(url or "").scheme.lower() in ("rtsp", "rtsps")


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
    # плата микроскопа + автомат (пытается подключиться к плате; без неё — тихий реконнект)
    micro.start()
    yield
    micro.stop()
    api_log("app", "Остановка приложения")


# фронтенд лежит в бандле (под заморозкой — в _internal, см. paths.BUNDLE_DIR),
# поэтому пути строим от BUNDLE_DIR, а не относительно CWD запуска
PAGE_DIR = BUNDLE_DIR / "page"

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(PAGE_DIR / "static")), name="static")


@app.get("/")
def home():
    return FileResponse(str(PAGE_DIR / "index.html"))


@app.get("/camera")
def camera():
    return FileResponse(str(PAGE_DIR / "camera.html"))


@app.get("/rtsp")
def rtsp_page():
    return FileResponse(str(PAGE_DIR / "rtsp.html"))


@app.get("/multi")
def multi_page():
    return FileResponse(str(PAGE_DIR / "multi.html"))


@app.get("/network")
def network_page():
    return FileResponse(str(PAGE_DIR / "network.html"))


@app.get("/microscope")
def microscope_page():
    return FileResponse(str(PAGE_DIR / "microscope.html"))


@app.get("/api/debug/logs")
def api_debug_logs(since_id: int = 0):
    return get_events(since_id)


# версия поставки + окружение (Python/genicam/harvesters + .cti) — удобно проверить
# на целевой машине, что обновление применилось и драйвер тот же
@app.get("/api/debug/info")
def api_debug_info():
    return {
        "version": read_version(),
        "data_dir": str(DATA_DIR),
        "bundle_dir": str(BUNDLE_DIR),
        **manager.log_environment(),
    }


# --- самообновление из релизов GitHub (см. updater.py) ---

@app.get("/api/update/check")
def api_update_check():
    api_log("api.update", "Проверка обновлений")
    return updater.check_latest()


@app.get("/api/update/download")
def api_update_download():
    api_log("api.update", "Скачивание обновления")
    return updater.download_latest()


@app.get("/api/update/apply")
def api_update_apply():
    api_log("api.update", "Применение обновления (перезапуск)")
    result = updater.apply_update()
    if result.get("ok"):
        # даём ответу уйти клиенту, затем выходим — апдейтер ждёт выхода процесса,
        # заменяет файлы и снова запускает приложение
        threading.Timer(2.0, lambda: os._exit(0)).start()
    return result


# ---------- Микроскоп: плата micro (Modbus TCP) + автомат ----------
# По соглашению проекта эндпоинты GET, «сеттеры» тоже GET, с log_event.

@app.get("/api/micro/telemetry")
def micro_telemetry():
    # один опрос для страницы: связь с платой + телеметрия + автомат + источник СВ (ПЛК)
    return {"connection": micro.status(), "telemetry": micro.telemetry(),
            "fsm": micro.state(), "plc": micro.sv_status()}


@app.get("/api/micro/status")
def micro_status():
    return micro.status()


@app.get("/api/micro/command")
def micro_command(cmd: int):
    micro.command(cmd)
    api_log("api.micro.command", "Команда микроскопу", payload={"cmd": cmd})
    return {"status": "ok", "cmd": cmd}


@app.get("/api/micro/led")
def micro_led(bright: int | None = None, on: int | None = None):
    micro.set_led(bright, None if on is None else bool(on))
    api_log("api.micro.led", "Подсветка микроскопа", payload={"bright": bright, "on": on})
    return {"status": "ok"}


@app.get("/api/micro/sv")
def micro_sv(value: float):
    micro.set_sv(value)
    return {"status": "ok", "sv": value}


@app.get("/api/micro/stage")
def micro_stage(value: int):
    micro.set_stage(value)
    return {"status": "ok", "stage": value}


@app.get("/api/micro/cyclic")
def micro_cyclic(on: int):
    micro.set_cyclic(bool(on))
    api_log("api.micro.cyclic", "Циклический режим микроскопа", payload={"on": bool(on)})
    return {"status": "ok", "cyclic": bool(on)}


@app.get("/api/micro/stop")
def micro_stop(on: int = 1):
    micro.stop_movement(bool(on))
    api_log("api.micro.stop", "Стоп движения микроскопа", "warn", {"on": bool(on)})
    return {"status": "ok", "inhibit": bool(on)}


@app.get("/api/micro/config")
def micro_config():
    return micro.config()


@app.get("/api/micro/reload")
def micro_reload():
    # применить правки plate_config.json (SP/SVSP/периоды/адреса) без перезапуска приложения
    data = micro.reload()
    api_log("api.micro.reload", "Перезагрузка конфига микроскопа", payload=data)
    return data


@app.get("/api/micro/settings")
def micro_settings(
    camera_serial: str | None = None,
    camera_ip: str | None = None,
    host: str | None = None,
    port: int | None = None,
    unit: int | None = None,
):
    # правка IP камеры/платы прямо со страницы: сохраняем в plate_config.json и перезапускаем
    patch = {}
    if camera_serial is not None:
        patch["camera_serial"] = camera_serial
    if camera_ip is not None:
        patch["camera_ip"] = camera_ip
    if host is not None:
        patch["host"] = host
    if port is not None:
        patch["port"] = port
    if unit is not None:
        patch["unit"] = unit
    data = micro.apply_settings(patch)
    api_log("api.micro.settings", "Изменены настройки микроскопа", payload={"patch": patch})
    return {"status": "ok", "applied": patch,
            "host": data.get("host"), "camera_serial": data.get("camera_serial")}


# ВАЖНО (frozen): перечисление/control GenTL-продюсера Hikrobot работает только в
# ГЛАВНОМ потоке (см. bug.txt / run._warmup). Синхронные (def) эндпоинты FastAPI
# гонит в потоках пула → там продюсер отдаёт 0 устройств. Поэтому все эндпоинты,
# трогающие продюсер (scan/list/ip/info/network/change_ip/force_ip), делаем async —
# они выполняются на event-loop uvicorn, а это и есть главный поток. Стрим остаётся
# синхронным: GigE идёт через MVS SDK (свой handle, кэш device_info), продюсер не нужен.
@app.get("/api/cams")
async def api_cams():
    return manager.scan_cams()


# детальный список с разбивкой по сетевым интерфейсам (как в MVS)
@app.get("/api/cams/detailed")
async def api_cams_detailed():
    manager.scan_cams()
    return manager.list_devices_grouped()


@app.get("/api/status")
async def api_status():
    try:
        return {"status": manager.check()}
    except Exception as error:
        api_log("api.status", "Ошибка получения статуса драйвера", "error", {"error": str(error)})
        return {"status": False, "error": str(error)}


@app.get("/api/ip")
async def get_ip(serial_number: str, interface_id: str = "", device_handle: str = ""):
    # interface/handle передаём разово, не мутируя общее состояние воркера:
    # параллельные запросы фронта иначе перетирали бы выбор друг друга
    return manager.get(serial_number).get_ip(interface_id or None, device_handle or None)


@app.get("/api/count_cams")
def count_cams():
    return manager.count_cams()


@app.get("/api/get_network_settings")
async def network_settings(serial_number: str, interface_id: str = "", device_handle: str = ""):
    worker = manager.get(serial_number)
    # interface/handle разово, без мутации общего состояния (см. /api/ip)
    ip, mask, gateway, dhcp = worker.get_network_settings(interface_id or None, device_handle or None)
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
    # GET, меняющий состояние (advanced_settings=True) — это по соглашению проекта:
    # все эндпоинты GET-only (см. CLAUDE.md), поэтому и «сеттеры» тоже GET
    data = manager.get(serial_number).set_advanced()
    api_log("api.network_settings_advanced", "Включены расширенные сетевые настройки", payload=data)
    return data


# ForceIP — задать IP камере, недоступной из-за чужой подсети (control не открыть)
@app.get("/api/force_ip")
async def force_ip(serial_number: str, ip: str, mask: str = "", gateway: str = ""):
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
    # проверяем схему уже при сохранении, а не только при стриминге — иначе в базу
    # попадает мусор (напр. file://), который потом отклоняется при попытке смотреть
    if not _is_rtsp_scheme(url):
        api_log("api.rtsp.save", "Отклонён RTSP-URL с недопустимой схемой", "warn", {"url": url})
        return {"error": "invalid_rtsp_scheme"}
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
async def change_ip(
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
    width: int | None = Query(None, gt=0),
    height: int | None = Query(None, gt=0),
    offset_x: int | None = Query(None, ge=0),
    offset_y: int | None = Query(None, ge=0),
    fps: float | None = Query(None, gt=0),
    exposure_auto: str | None = None,
    exposure_time: float | None = Query(None, gt=0),
    pixel_format: str | None = None,
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
    worker = manager.get(serial_number)
    return {**worker.metrics,
            "photo": worker.save_photo,
            "video": worker.save_video,
            "photo_count": worker.photo_saved_count,
            "video_elapsed": worker.video_elapsed()}


@app.get("/api/camera/data_limit")
def data_limit(serial_number: str):
    return manager.get(serial_number).data_limit


@app.get("/api/camera/info")
async def camera_info(serial_number: str, interface_id: str = "", device_handle: str = ""):
    worker = manager.get(serial_number)
    # interface/handle разово, без мутации общего состояния (см. /api/ip)
    data = worker.get_info(interface_id or None, device_handle or None)
    if not data:
        api_log("api.camera.info", "Не удалось получить информацию о камере", "warn", {"serial_number": serial_number})
        return {"error": "Не удалось получить информацию о камере"}
    api_log("api.camera.info", "Получена информация о камере",
            payload={"serial_number": serial_number, "count": len(data.get("items", []))})
    return data


@app.get("/api/camera/on_save_photo")
def on_save_photo(serial_number: str, interval: int, project: str = ""):
    data = manager.get(serial_number).on_photo(interval, project)
    api_log("api.camera.on_save_photo", "Включено сохранение фото",
            payload={"interval": interval, "project": project, "result": data})
    return data


@app.get("/api/camera/off_save_photo")
def off_save_photo(serial_number: str):
    data = manager.get(serial_number).off_photo()
    api_log("api.camera.off_save_photo", "Выключено сохранение фото", payload=data)
    return data


@app.get("/api/camera/on_save_video")
def on_save_video(serial_number: str, duration: int, project: str = ""):
    data = manager.get(serial_number).on_video(duration, project)
    api_log("api.camera.on_save_video", "Включена запись видео",
            payload={"duration": duration, "project": project, "result": data})
    return data


@app.get("/api/camera/off_save_video")
def off_save_video(serial_number: str):
    data = manager.get(serial_number).off_video()
    api_log("api.camera.off_save_video", "Выключена запись видео", payload=data)
    return data


@app.get("/api/camera/status_video_photo")
def status_video_photo(serial_number: str):
    worker = manager.get(serial_number)
    return {
        "video": worker.save_video,
        "photo": worker.save_photo,
        "photo_count": worker.photo_saved_count,
        "video_elapsed": worker.video_elapsed(),
    }


# текущий конфиг запуска камеры (фактические значения с камеры) — для значка «инфо»
@app.get("/api/camera/current_config")
def camera_current_config(serial_number: str):
    return manager.get(serial_number).current_config or {}


# сохранённые настройки автосохранения (имя проекта + интервал/длительность) для префилла
# модалок. Общий для GigE и RTSP — ключ по серийнику. Читает JSON-стор save_settings.
@app.get("/api/save_settings")
def get_save_settings(serial_number: str):
    return save_settings.get(serial_number)


# ---------- RTSP-камера (просмотр / запись / снимки) ----------


def _resolve_rtsp_url(url, ip, username, password, channel, subtype):
    if url:
        if not _is_rtsp_scheme(url):
            log_event("api.rtsp.stream", "Отклонён RTSP-URL с недопустимой схемой", "warn", {"url": url})
            return None
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
    return {**worker.metrics,
            "photo": worker.save_photo,
            "video": worker.save_video,
            "photo_count": worker.photo_saved_count,
            "video_elapsed": worker.video_elapsed(),
            "zoom_factor": worker.zoom_factor}


@app.get("/api/rtsp/on_save_photo")
def rtsp_on_save_photo(serial_number: str, interval: int, project: str = ""):
    worker = manager.get_rtsp(serial_number)
    if worker is None:
        return {"error": "rtsp_not_connected"}
    data = worker.on_photo(interval, project)
    api_log("api.rtsp.on_save_photo", "Включено автосохранение фото (RTSP)",
            payload={"interval": interval, "project": project, "result": data})
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
def rtsp_on_save_video(serial_number: str, duration: int, project: str = ""):
    worker = manager.get_rtsp(serial_number)
    if worker is None:
        return {"error": "rtsp_not_connected"}
    data = worker.on_video(duration, project)
    api_log("api.rtsp.on_save_video", "Включена запись видео (RTSP)",
            payload={"duration": duration, "project": project, "result": data})
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


@app.get("/api/rtsp/capabilities")
def rtsp_capabilities(serial_number: str, refresh: int = 0):
    worker = manager.get_rtsp(serial_number)
    if worker is None:
        return {"error": "rtsp_not_connected"}
    data = worker.get_capabilities(refresh=bool(refresh))
    api_log("api.rtsp.capabilities", "Опрос возможностей RTSP-камеры",
            payload={"serial_number": serial_number, "result": data})
    return data


@app.get("/api/rtsp/light")
def rtsp_light(serial_number: str, on: int, level: int = 100):
    worker = manager.get_rtsp(serial_number)
    if worker is None:
        return {"error": "rtsp_not_connected"}
    data = worker.set_light(bool(on), level)
    api_log("api.rtsp.light", "Управление белым прожектором (RTSP)",
            payload={"serial_number": serial_number, "on": bool(on), "level": level, "result": data})
    return data


@app.get("/api/rtsp/light_state")
def rtsp_light_state(serial_number: str):
    worker = manager.get_rtsp(serial_number)
    if worker is None:
        return {"error": "rtsp_not_connected"}
    return worker.get_light()


@app.get("/api/rtsp/zoom")
def rtsp_zoom(serial_number: str, factor: float | None = None,
              px: float | None = None, py: float | None = None):
    worker = manager.get_rtsp(serial_number)
    if worker is None:
        return {"error": "rtsp_not_connected"}
    data = worker.set_zoom(factor, px, py)
    api_log("api.rtsp.zoom", "Цифровой зум RTSP",
            payload={"serial_number": serial_number, "factor": factor,
                     "px": px, "py": py, "result": data})
    return data


@app.get("/api/rtsp/optical_zoom")
def rtsp_optical_zoom(serial_number: str, direction: str):
    worker = manager.get_rtsp(serial_number)
    if worker is None:
        return {"error": "rtsp_not_connected"}
    data = worker.optical_zoom(direction)
    api_log("api.rtsp.optical_zoom", "Оптический зум RTSP",
            payload={"serial_number": serial_number, "direction": direction, "result": data})
    return data


# ---------- настройки изображения RTSP (экспозиция / баланс белого / день-ночь) ----------
@app.get("/api/rtsp/image")
def rtsp_image(serial_number: str):
    worker = manager.get_rtsp(serial_number)
    if worker is None:
        return {"error": "rtsp_not_connected"}
    data = worker.get_image_settings()
    api_log("api.rtsp.image", "Опрос настроек изображения RTSP",
            payload={"serial_number": serial_number,
                     "reachable": data.get("reachable"), "error": data.get("error")})
    return data


@app.get("/api/rtsp/exposure")
def rtsp_exposure(serial_number: str, compensation: int | None = None,
                  gain_min: int | None = None, gain_max: int | None = None):
    worker = manager.get_rtsp(serial_number)
    if worker is None:
        return {"error": "rtsp_not_connected"}
    data = worker.set_exposure(compensation, gain_min, gain_max)
    api_log("api.rtsp.exposure", "Настройка экспозиции RTSP",
            payload={"serial_number": serial_number, "compensation": compensation,
                     "gain_min": gain_min, "gain_max": gain_max, "result": data})
    return data


@app.get("/api/rtsp/white_balance")
def rtsp_white_balance(serial_number: str, mode: str):
    worker = manager.get_rtsp(serial_number)
    if worker is None:
        return {"error": "rtsp_not_connected"}
    data = worker.set_white_balance(mode)
    api_log("api.rtsp.white_balance", "Настройка баланса белого RTSP",
            payload={"serial_number": serial_number, "mode": mode, "result": data})
    return data


@app.get("/api/rtsp/day_night")
def rtsp_day_night(serial_number: str, mode: str):
    worker = manager.get_rtsp(serial_number)
    if worker is None:
        return {"error": "rtsp_not_connected"}
    data = worker.set_day_night(mode)
    api_log("api.rtsp.day_night", "Настройка день/ночь RTSP",
            payload={"serial_number": serial_number, "mode": mode, "result": data})
    return data


# ---------- сеть RTSP-камеры (смена IP-адреса) ----------
def _update_rtsp_store_url(old_url, new_url, new_ip):
    """Перенести сохранённую запись камеры на новый url/ip после смены IP.

    Только если запись со старым url уже есть в базе — новую не создаём (иначе
    засоряли бы базу камерами, которые пользователь не сохранял).
    """
    if not old_url or old_url == new_url:
        return
    entry = next((i for i in rtsp_store.load() if i.get("url") == old_url), None)
    if entry is None:
        return
    rtsp_store.remove(old_url)
    rtsp_store.save({
        "url": new_url,
        "label": entry.get("label", ""),
        "ip": new_ip,
        "scale": entry.get("scale", 100),
        "fps": entry.get("fps", 0),
    })


@app.get("/api/rtsp/network")
def rtsp_network(serial_number: str):
    worker = manager.get_rtsp(serial_number)
    if worker is None:
        return {"error": "rtsp_not_connected"}
    data = worker.get_network()
    api_log("api.rtsp.network", "Опрос сетевых настроек RTSP",
            payload={"serial_number": serial_number,
                     "reachable": data.get("reachable"), "ip": data.get("ip"),
                     "error": data.get("error")})
    return data


@app.get("/api/rtsp/set_network")
def rtsp_set_network(serial_number: str, ip: str = "", mask: str = "",
                     gateway: str = "", dhcp: int = 0):
    worker = manager.get_rtsp(serial_number)
    if worker is None:
        return {"error": "rtsp_not_connected"}

    old_url = worker.rtsp_url
    dhcp_on = bool(dhcp)
    api_log("api.rtsp.set_network", "Запрошена смена сетевых настроек RTSP", "warn",
            payload={"serial_number": serial_number, "ip": ip, "mask": mask,
                     "gateway": gateway, "dhcp": dhcp_on})

    result = worker.set_network(ip=ip, mask=mask, gateway=gateway, dhcp=dhcp_on)
    if not result.get("ok"):
        api_log("api.rtsp.set_network", "Смена сетевых настроек не удалась", "error",
                payload={"serial_number": serial_number, "result": result})
        return result

    # DHCP: новый IP заранее неизвестен (его выдаст сервер) — базу и воркер не трогаем
    if dhcp_on:
        result["dhcp"] = True
        api_log("api.rtsp.set_network", "Включён DHCP — новый IP выдаст сервер", "success",
                payload={"serial_number": serial_number})
        return result

    # статический адрес применён: новый url, обновление базы, сброс старого воркера
    new_ip = ip.strip()
    new_url = replace_host_in_url(old_url, new_ip)
    _update_rtsp_store_url(old_url, new_url, new_ip)
    manager.drop_rtsp(serial_number)

    result["new_ip"] = new_ip
    result["new_url"] = new_url
    api_log("api.rtsp.set_network", "Сетевые настройки применены, база обновлена", "success",
            payload={"serial_number": serial_number, "new_ip": new_ip, "new_url": new_url})
    return result
