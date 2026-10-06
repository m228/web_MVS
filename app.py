import warnings
# websockets не используем (стрим через MJPEG/HTTP); гасим DeprecationWarning от uvicorn+websockets 14+
warnings.filterwarnings("ignore", message=r".*websockets\.legacy is deprecated.*")
warnings.filterwarnings("ignore", message=r".*WebSocketServerProtocol is deprecated.*")

import asyncio
import json
import os
import threading
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Query, Body, Request
from fastapi.responses import FileResponse, StreamingResponse, Response, HTMLResponse
from fastapi.staticfiles import StaticFiles

from logger import get_events, log_event

from camera_core import manager, build_rtsp_url, replace_host_in_url
import plate_config
import rtsp_store
import save_settings
import net_tools
import updater
from microscope_service import micro
import cv_client
import fracture_lab
import cv_store
import autostart
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
    # плата микроскопа + автомат (пытается подключиться к плате; без неё — тихий реконнект).
    # Поднимаем ТОЛЬКО если микроскоп включён галочкой на главной (micro_enabled).
    if plate_config.load().get("micro_enabled", True):
        micro.start()
    else:
        api_log("app", "Микроскоп выключен галочкой — плата/ПЛК не поднимаются")
    # RTSP-камеры с галочкой «автоподключение» поднимаем сами: захват фоновый,
    # поэтому съёмка/автосохранение идут без открытого браузера (см. camera_core)
    manager.resume_rtsp_autostart()
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
    # если микроскоп выключен галочкой на главной — заглушка вместо страницы
    if not plate_config.load().get("micro_enabled", True):
        return HTMLResponse(
            "<!doctype html><html lang=ru><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>Микроскоп выключен</title>"
            "<style>body{margin:0;min-height:100vh;display:flex;align-items:center;"
            "justify-content:center;background:#0f1117;color:#e6e8ee;"
            "font-family:system-ui,Segoe UI,Arial,sans-serif}.b{text-align:center;"
            "padding:32px}.b h1{font-size:1.5rem;margin:0 0 10px}.b p{color:#9aa0ad;"
            "margin:0 0 22px}.b a{display:inline-block;padding:10px 20px;border-radius:10px;"
            "background:#3b5bdb;color:#fff;text-decoration:none;font-weight:600}</style>"
            "</head><body><div class=b><h1>Микроскоп выключен</h1>"
            "<p>Включите его галочкой на главной странице.</p>"
            "<a href='/'>На главную</a></div></body></html>")
    return FileResponse(str(PAGE_DIR / "microscope.html"))


@app.get("/api/micro/enabled")
def micro_enabled_get():
    return {"enabled": bool(plate_config.load().get("micro_enabled", True))}


@app.post("/api/micro/enabled")
def micro_enabled_set(on: int):
    enabled = bool(on)
    plate_config.save({"micro_enabled": enabled})
    if enabled:
        micro.start()      # поднять плату/ПЛК/автомат на лету
    else:
        micro.stop()       # разорвать связь с платой/ПЛК немедленно
    api_log("api.micro.enabled", "Микроскоп включён/выключен галочкой", payload={"enabled": enabled})
    return {"enabled": enabled}


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
            "fsm": micro.state(), "plc": micro.sv_status(), "ext": micro.ext()}


@app.get("/api/micro/status")
def micro_status():
    return micro.status()


@app.get("/api/micro/command")
def micro_command(cmd: int):
    micro.command(cmd)
    api_log("api.micro.command", "Команда микроскопу", payload={"cmd": cmd})
    return {"status": "ok", "cmd": cmd}


@app.get("/api/micro/led")
def micro_led(bright: int | None = None, freq: int | None = None, on: int | None = None):
    micro.led_native(bright, freq, None if on is None else bool(on))
    api_log("api.micro.led", "Подсветка микроскопа", payload={"bright": bright, "freq": freq, "on": on})
    return {"status": "ok"}


@app.get("/api/micro/sv")
def micro_sv(value: float):
    micro.set_sv(value)
    return {"status": "ok", "sv": value}


@app.get("/api/micro/stage")
def micro_stage(value: int):
    micro.set_stage(value)
    return {"status": "ok", "stage": value}


@app.get("/api/micro/camera_serial")
def micro_camera_serial(serial: str):
    # запомнить серийник камеры микроскопа для скринов/видео пробы (без перезапуска платы)
    return micro.set_camera_serial(serial)


@app.get("/api/micro/cycle_autostart")
def micro_cycle_autostart(on: int):
    # галочка «Автостарт цикла после перезапуска» (без перезапуска платы)
    return micro.set_cycle_autostart(bool(on))


@app.get("/api/micro/arrive_sensor")
def micro_arrive_sensor(mode: str):
    # датчик прихода мотора для цикла: enc (энкодер 1285) / calc (расчётная 1274) / ai (аналог 1271)
    return micro.set_arrive_sensor(mode)


@app.get("/api/micro/trigger_mode")
def micro_trigger_mode(mode: str):
    # переключатель триггера пробы time/sv — применяется сразу (без перезапуска платы)
    return micro.set_trigger_mode(mode)


@app.get("/api/micro/photo_enabled")
def micro_photo_enabled(on: int):
    # тумблер «Сырые фото»: вкл — кадры пробы дополнительно пишутся в датасет (без перезапуска платы)
    return micro.set_photo_enabled(bool(on))


@app.get("/api/micro/ignore_stage")
def micro_ignore_stage(on: int):
    # галочка «Варить без стадии» — авто-цикл без гейта стадии 3..9 (ручная варка)
    return micro.set_ignore_stage(bool(on))


@app.get("/api/micro/ignore_focus")
def micro_ignore_focus(on: int):
    # галочка «Не использовать фокус» — М2 не двигается по таблице СВ (0 тоже валиден)
    return micro.set_ignore_focus(bool(on))


@app.get("/api/micro/fine_approach")
def micro_fine_approach(enabled: int | None = None, coarse_tol_um: int | None = None,
                        fine_tol_um: int | None = None, max_retry: int | None = None,
                        pause_sec: float | None = None):
    # гибридный доезд подвода: грубо по расчётной 1274 → точный подгон по датчику 1271
    res = micro.set_fine_approach(
        None if enabled is None else bool(enabled),
        coarse_tol_um, fine_tol_um, max_retry, pause_sec)
    api_log("api.micro.fine_approach", "Довод по абсолютнику", payload=res)
    return res


@app.get("/api/micro/clear_fault")
def micro_clear_fault():
    # сброс аварии (подгон по датчику не сошёлся и т.п.)
    res = micro.clear_fault()
    api_log("api.micro.clear_fault", "Сброс аварии микроскопа", payload=res)
    return res


@app.get("/api/micro/autocal")
def micro_autocal(enabled: int | None = None, every_n: int | None = None,
                  sensor_lo: int | None = None, sensor_hi: int | None = None,
                  timeout_sec: int | None = None):
    # автокалибровка нуля М1 (раз в N варок на пропарке — стадия 11)
    res = micro.set_autocal(
        None if enabled is None else bool(enabled),
        every_n, sensor_lo, sensor_hi, timeout_sec)
    api_log("api.micro.autocal", "Автокалибровка нуля", payload=res)
    return res


@app.get("/api/micro/autocal/start")
def micro_autocal_start():
    # ручной запуск автокалибровки
    res = micro.start_autocal()
    api_log("api.micro.autocal.start", "Автокалибровка: ручной старт", payload=res)
    return res


@app.get("/api/micro/autocal/reset")
def micro_autocal_reset():
    # сброс счётчика варок в 0
    res = micro.reset_varka_count()
    api_log("api.micro.autocal.reset", "Сброс счётчика варок", payload=res)
    return res


@app.get("/api/micro/sensor_filter")
def micro_sensor_filter(enabled: int | None = None, avg_sec: float | None = None):
    # фильтр аналогового датчика перемещения 1271 (сглаживание дрожания): вкл/выкл + окно, сек
    res = micro.set_sensor_filter(None if enabled is None else bool(enabled), avg_sec)
    api_log("api.micro.sensor_filter", "Фильтр датчика 1271", payload=res)
    return res


@app.get("/api/micro/sensor_display_scale")
def micro_sensor_display_scale(value: float):
    # множитель ТОЛЬКО показа датчика на странице (свести датчик с позицией)
    return micro.set_sensor_display_scale(value)


@app.get("/api/micro/m1_stop_sensor")
def micro_m1_stop_sensor(value: float):
    # порог аппаратной блокировки «Стоп М1 при положении аналог. датчика» (рег. 1234, мкм)
    res = micro.set_m1_stop_sensor(value)
    api_log("api.micro.m1_stop_sensor", "Стоп М1 по датчику (запись в прошивку)", payload=res)
    return res


@app.get("/api/micro/autofocus")
def micro_autofocus(start: int = 0, end: int = 80000, coarse: int = 500, fine: int = 100):
    # запустить автофокус М2 (двухпроходный поиск по резкости), только в ручном режиме
    res = micro.autofocus(start, end, coarse, fine)
    api_log("api.micro.autofocus", "Автофокус", payload={"start": start, "end": end,
            "coarse": coarse, "fine": fine, "res": res})
    return res


@app.get("/api/micro/autofocus/status")
def micro_autofocus_status():
    return micro.autofocus_status()


@app.get("/api/micro/autofocus/stop")
def micro_autofocus_stop():
    return micro.autofocus_stop()


@app.get("/api/micro/cyclic")
def micro_cyclic(on: int):
    micro.set_cyclic(bool(on))
    api_log("api.micro.cyclic", "Циклический режим микроскопа", payload={"on": bool(on)})
    return {"status": "ok", "cyclic": bool(on)}


@app.get("/api/micro/sv_override")
def micro_sv_override(on: int):
    # DEBUG (убрать после отладки цикла): перехват ПЛК — ручной СВ не затирается sv_source
    res = micro.sv_override(bool(on))
    api_log("api.micro.sv_override", "DEBUG: перехват ПЛК (ручной СВ)", payload={"on": bool(on)})
    return {"status": "ok", **res}


@app.get("/api/micro/stop")
def micro_stop(on: int = 1):
    micro.stop_movement(bool(on))
    api_log("api.micro.stop", "Стоп движения микроскопа", "warn", {"on": bool(on)})
    return {"status": "ok", "inhibit": bool(on)}


@app.get("/api/micro/move")
def micro_move(m: int, pos: float):
    # идти мотором m (1/2) в позицию pos (мкм) нативной командой платы
    micro.move_motor(m, pos)
    api_log("api.micro.move", "Движение мотора в позицию", payload={"m": m, "pos": pos})
    return {"status": "ok", "m": m, "pos": pos}


@app.get("/api/micro/enable")
def micro_enable(m: int, on: int):
    micro.enable_motor(m, bool(on))
    api_log("api.micro.enable", "Разрешение мотора", payload={"m": m, "on": bool(on)})
    return {"status": "ok", "m": m, "on": bool(on)}


@app.get("/api/micro/estop")
def micro_estop():
    micro.estop()
    api_log("api.micro.estop", "АВАРИЙНЫЙ СТОП микроскопа", "warn", {})
    return {"status": "ok"}


# --- Ручной пульт платы (прямое управление, как родной конфигуратор) ---

@app.get("/api/micro/manual")
def micro_manual(on: int):
    data = micro.manual_mode(bool(on))
    api_log("api.micro.manual", "Ручной режим микроскопа", payload={"on": bool(on)})
    return {"status": "ok", **data}


@app.get("/api/micro/motor")
def micro_motor(m: int, op: str, value: float | None = None):
    # ручная команда мотору: goto/steps/shift/stop/find_zero/set_zero/home_start/home_end/
    # dir_fwd/dir_back/enable/disable — только в ручном режиме
    res = micro.motor_op(m, op, value)
    api_log("api.micro.motor", "Команда мотору (пульт)",
            "warn" if op in ("goto", "steps", "shift", "home_start", "home_end") else "info",
            {"m": m, "op": op, "value": value, "result": res})
    return res


@app.get("/api/micro/lock")
def micro_lock(bit: int, disabled: int):
    # блокировки прошивки (рег. 1535): disabled=1 → отключить блокировку, 0 → включить обратно
    res = micro.set_lock_bit(bit, bool(disabled))
    api_log("api.micro.lock", "Блокировка прошивки (пульт)", "warn",
            {"bit": bit, "disabled": bool(disabled), "result": res})
    return res


@app.get("/api/micro/dq")
def micro_dq(bit: int, on: int):
    res = micro.dq_bit(bit, bool(on))
    api_log("api.micro.dq", "Дискретный выход DQ (пульт)", payload={"bit": bit, "on": bool(on), "result": res})
    return res


@app.get("/api/micro/config")
def micro_config():
    return micro.config()


@app.get("/api/micro/reload")
def micro_reload():
    # применить правки plate_config.json (SP/SVSP/периоды/адреса) без перезапуска приложения
    data = micro.reload()
    api_log("api.micro.reload", "Перезагрузка конфига микроскопа", payload=data)
    return data


@app.get("/api/micro/take_sample")
def micro_take_sample():
    """Кнопка «Взять пробу»: запустить цикл пробы (отвод→промывка→подвод→выдержка→возврат)."""
    res = micro.take_sample()
    api_log("api.micro.take_sample", "Запуск цикла пробы «Взять пробу»", payload=res)
    return res


@app.get("/api/micro/cycle_reset")
def micro_cycle_reset():
    """Кнопка «Сброс»: прервать цикл — в Ожидание, закрыть клапаны."""
    res = micro.cycle_reset()
    api_log("api.micro.cycle_reset", "Сброс цикла пробы", payload=res)
    return res


@app.get("/api/micro/cycle_skip")
def micro_cycle_skip():
    """Кнопка «Вперёд»: перепрыгнуть на следующий шаг цикла."""
    res = micro.cycle_skip()
    api_log("api.micro.cycle_skip", "Перепрыг шага цикла", payload=res)
    return res


@app.get("/api/micro/config_dump")
def micro_config_dump():
    """Текущий полный конфиг платы (для просмотра/скачивания дампа)."""
    return plate_config.load()


@app.get("/api/version")
def api_version():
    """Версия программы (для шапки страницы микроскопа)."""
    return {"app": read_version()}


@app.get("/api/micro/config_snapshots")
def micro_config_snapshots():
    """Автокопии настроек (с датой) для отката на вкладке «Дамп»."""
    return {"snapshots": plate_config.list_snapshots()}


@app.get("/api/micro/config_restore")
def micro_config_restore(name: str):
    """Откат настроек на автокопию: текущие сохраняются в plate_config.backup.json, автомат перезапускается."""
    try:
        plate_config.replace_all(plate_config.read_snapshot(name))
    except Exception as e:
        api_log("api.micro.config_restore", "Ошибка отката настроек", "warn", {"name": name, "error": str(e)})
        return {"status": "error", "error": str(e)}
    data = micro.reload()
    api_log("api.micro.config_restore", "Настройки восстановлены из автокопии", payload={"name": name})
    return {"status": "ok", "host": data.get("host") if data else None}


@app.post("/api/micro/config_import")
def micro_config_import(payload: dict = Body(...)):
    """Импорт дампа: перезаписать конфиг целиком (с backup «before-import») и перезапустить автомат."""
    try:
        plate_config.replace_all(payload)
    except Exception as e:
        api_log("api.micro.config_import", "Ошибка импорта дампа", "warn", {"error": str(e)})
        return {"status": "error", "error": str(e)}
    data = micro.reload()
    api_log("api.micro.config_import", "Импортирован дамп настроек", payload={"keys": list(payload.keys())})
    return {"status": "ok", "host": data.get("host") if data else None}


@app.get("/api/micro/confirm_auto")
def micro_confirm_auto():
    """Оператор подтвердил переход в Автомат при старте варки (варка вошла в стадию 3, был ручной)."""
    res = micro.confirm_auto()
    api_log("api.micro.confirm_auto", "Подтверждён переход в Автомат при старте варки", payload=res)
    return res


@app.get("/api/micro/decline_auto")
def micro_decline_auto():
    """Оператор отклонил переход — остаёмся в ручном режиме."""
    res = micro.decline_auto()
    api_log("api.micro.decline_auto", "Отклонён переход в Автомат — остаёмся в ручном", payload=res)
    return res


@app.get("/api/micro/settings")
def micro_settings(
    camera_serial: str | None = None,
    camera_ip: str | None = None,
    camera_mode: str | None = None,
    hourly_wash: int | None = None,
    retract_pos: int | None = None,
    pre_wash_sec: int | None = None,
    dwell_sec: int | None = None,
    shot_interval_sec: int | None = None,
    pause_sec: int | None = None,
    photo_format: str | None = None,
    trigger_mode: str | None = None,
    arrive_sensor: str | None = None,
    sv_from: float | None = None,
    sv_to: float | None = None,
    stage_from: int | None = None,
    stage_to: int | None = None,
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
    if camera_mode is not None:
        patch["camera_mode"] = camera_mode   # "stream" | "auto" (режим съёмки; авто-цикл прочитает)
    if hourly_wash is not None:
        patch["hourly_wash"] = {"enabled": bool(hourly_wash)}   # ежечасная промывка вкл/выкл
    pc = {}
    if retract_pos is not None: pc["retract_pos"] = retract_pos
    if pre_wash_sec is not None: pc["pre_wash_sec"] = pre_wash_sec
    if dwell_sec is not None: pc["dwell_sec"] = dwell_sec
    if shot_interval_sec is not None: pc["shot_interval_sec"] = shot_interval_sec
    if pause_sec is not None: pc["pause_sec"] = pause_sec
    if photo_format is not None: pc["photo_format"] = photo_format
    if trigger_mode is not None: pc["trigger_mode"] = trigger_mode
    if arrive_sensor is not None: pc["arrive_sensor"] = arrive_sensor
    if sv_from is not None: pc["sv_from"] = sv_from
    if sv_to is not None: pc["sv_to"] = sv_to
    if stage_from is not None: pc["stage_from"] = stage_from
    if stage_to is not None: pc["stage_to"] = stage_to
    if pc:
        patch["probe_cycle"] = pc            # параметры цикла пробы (вкладка «Цикл»)
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


@app.get("/api/micro/recipe")
def micro_recipe(sp: str | None = None, svsp: str | None = None, focus: str | None = None):
    # сохранить таблицу подвода (кривая СВ->зазор->фокус): SP/SVSP/FOCUS приходят JSON-массивами.
    # Сохраняем в plate_config.json и горячо перезапускаем автомат (как «Перезагрузить конфиг»).
    patch = {}
    try:
        if sp is not None:
            patch["SP"] = [int(round(float(x))) for x in json.loads(sp)]
        if svsp is not None:
            patch["SVSP"] = [float(x) for x in json.loads(svsp)]
        if focus is not None:
            patch["FOCUS"] = [int(round(float(x))) for x in json.loads(focus)]
    except Exception as e:
        api_log("api.micro.recipe", "Ошибка разбора таблицы SP/SVSP/FOCUS", "warn", {"error": str(e)})
        return {"status": "error", "error": str(e)}
    micro.apply_settings(patch)
    api_log("api.micro.recipe", "Сохранена таблица подвода (SP/SVSP/FOCUS)",
            payload={"rows": len(patch.get("SVSP", []))})
    return {"status": "ok", "sp_len": len(patch.get("SP", [])), "svsp_len": len(patch.get("SVSP", []))}
# --- компьютерное зрение (CV): рассев кристаллов по скринам пробы (см. cv_service/) ---

def _cv_serial(serial: str | None) -> str:
    """Серийник камеры: из запроса или из конфига микроскопа (camera_serial)."""
    if serial:
        return serial
    return (micro.config() or {}).get("camera_serial", "") or ""


@app.get("/api/cv/health")
def cv_health():
    cv = micro.cv_config()
    url = cv.get("service_url", "http://127.0.0.1:8765")
    h = cv_client.health(url) if cv.get("enabled") else None
    return {"enabled": bool(cv.get("enabled")), "service_url": url,
            "online": h is not None, "service": h,
            "analysis": micro.cv_status()}   # статус последнего разбора (для строки статуса)


@app.get("/api/cv/settings")
def cv_settings_get():
    return micro.cv_config()


@app.post("/api/cv/settings")
def cv_settings_set(patch: dict = Body(...)):
    data = micro.set_cv(patch or {})
    api_log("api.cv.settings", "Изменены настройки CV", payload={"patch": patch})
    return {"status": "ok", "cv": data}


# --- разломы (Часть B) + автоподвод (Часть C) ---
@app.get("/api/cv/fracture/settings")
def cv_fracture_get():
    return micro.fracture_config()


@app.post("/api/cv/fracture/settings")
def cv_fracture_set(patch: dict = Body(...)):
    data = micro.set_fracture(patch or {})
    api_log("api.cv.fracture", "Изменены настройки разломов", payload={"patch": patch})
    return {"status": "ok", "fracture": data}


@app.get("/api/cv/model")
def cv_model_get():
    """Инфо о текущей модели сайдкара (имя/seg/устройство/классы)."""
    cv = micro.cv_config()
    url = cv.get("service_url", "http://127.0.0.1:8765")
    return cv_client.model_info(url) or {"error": "offline"}


@app.post("/api/cv/model/upload")
async def cv_model_upload(request: Request, name: str = "best.pt"):
    """Залить .pt из браузера → сайдкар сохранит и горячо загрузит (train локально → на Буи)."""
    cv = micro.cv_config()
    url = cv.get("service_url", "http://127.0.0.1:8765")
    raw = await request.body()
    res = cv_client.model_upload(url, raw, name=name)
    api_log("api.cv.model.upload", "Загрузка модели в сайдкар",
            payload={"name": name, "size": len(raw) if raw else 0, "ok": bool(res)})
    return res or {"error": "upload_failed"}


@app.get("/api/cv/model/load")
def cv_model_load(path: str):
    """Загрузить модель на сайдкаре по пути на его диске (горячо)."""
    cv = micro.cv_config()
    url = cv.get("service_url", "http://127.0.0.1:8765")
    res = cv_client.model_load(url, path)
    api_log("api.cv.model.load", "Загрузка модели по пути", payload={"path": path, "ok": bool(res)})
    return res or {"error": "load_failed"}


@app.get("/api/cv/approach/settings")
def cv_approach_get():
    return micro.approach_config()


@app.post("/api/cv/approach/settings")
def cv_approach_set(patch: dict = Body(...)):
    data = micro.set_approach(patch or {})
    api_log("api.cv.approach", "Изменены настройки автоподвода", payload={"patch": patch})
    return {"status": "ok", "approach": data}


# --- калибровка разломов по своим кадрам (см. fracture_lab.py) ---
@app.get("/api/cv/fracture/lab/snap")
def cv_lab_snap(label: str = "unknown"):
    res = micro.lab_snap(label)
    api_log("api.cv.fracture.lab", "Кадр калибровки разломов", payload={"label": label, "result": res})
    return res


@app.get("/api/cv/fracture/lab/list")
def cv_lab_list():
    return {"frames": fracture_lab.list_frames()}


@app.get("/api/cv/fracture/lab/image")
def cv_lab_image(name: str):
    try:
        return Response(content=fracture_lab.jpeg(name), media_type="image/jpeg")
    except Exception:
        return Response(status_code=404)


@app.get("/api/cv/fracture/lab/label")
def cv_lab_label(name: str, label: str):
    try:
        return {"status": "ok", **fracture_lab.set_label(name, label)}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/cv/fracture/lab/delete")
def cv_lab_delete(name: str):
    try:
        fracture_lab.delete(name)
        return {"status": "ok"}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/cv/fracture/lab/zones")
def cv_lab_zones(name: str, dark_thr: float = 30.0, min_area_frac: float = 0.0018):
    """Зоны-кандидаты на кадре (с контуром) при данных dark_thr / min_area_frac."""
    try:
        return {"zones": fracture_lab.zones(name, dark_thr, min_area_frac)}
    except Exception as e:
        return {"error": str(e), "zones": []}


@app.get("/api/cv/fracture/lab/all")
def cv_lab_all(dark_thr: float = 30.0, min_area_frac: float = 0.0018):
    """Зоны (только D/T/площадь) по всем размеченным кадрам — для оценки и подбора порогов."""
    return {"frames": fracture_lab.all_zones(dark_thr, min_area_frac)}


@app.get("/api/cv/last")
def cv_last(serial: str | None = None):
    return cv_store.get_last(_cv_serial(serial)) or {"empty": True}


@app.get("/api/cv/prev")
def cv_prev(serial: str | None = None):
    return cv_store.get_prev(_cv_serial(serial)) or {"empty": True}


@app.get("/api/cv/samples")
def cv_samples(serial: str | None = None, limit: int = 50):
    return {"samples": cv_store.list_samples(_cv_serial(serial), limit=limit)}


@app.get("/api/cv/result")
def cv_result(serial: str | None = None, ts: str = ""):
    """Проба по метке времени — лента проб показывает выбранную пробу в окне CV."""
    return cv_store.get_result(_cv_serial(serial), ts) or {"empty": True}


@app.get("/api/cv/thumb")
def cv_thumb(serial: str | None = None, ts: str = ""):
    """Миниатюра пробы (кадр 0) для ленты проб."""
    p = cv_store.thumb_path(_cv_serial(serial), ts)
    if not p:
        return Response(status_code=404)
    return FileResponse(str(p), media_type="image/jpeg")


@app.get("/api/cv/trend")
def cv_trend(serial: str | None = None, series: str | None = None, limit: int = 200,
             t_from: float | None = Query(None, alias="from"), t_to: float | None = Query(None, alias="to")):
    """Тренд. С from/to (epoch, сек) — по реальному времени из журнала по дням (вся история);
    без них — последние limit проб (вкладка «Разломы»)."""
    ser = [s.strip() for s in series.split(",")] if series else None
    if t_from is not None and t_to is not None:
        return cv_store.trend_range(_cv_serial(serial), t_from, t_to, series=ser,
                                    limit=max(10, min(limit, 5000)))
    return cv_store.trend(_cv_serial(serial), series=ser, limit=limit)


@app.get("/api/cv/trend/days")
def cv_trend_days(serial: str | None = None):
    """Дни, за которые есть пробы (для выбора даты в тренде)."""
    return {"days": cv_store.history_days(_cv_serial(serial))}


@app.get("/api/cv/boils")
def cv_boils(serial: str | None = None, limit: int = 6):
    """Последние варки (новая первой) со сводкой мелочи/сростков/объёма: для «Объём и мелочь» с листалкой по варкам.
    Варка идёт, пока последняя проба свежая; мелочь считается по пробам варки с СВ ≥ порога (финиш)."""
    return {"boils": cv_store.boils(_cv_serial(serial), limit=max(1, min(int(limit), 20)))}


@app.get("/api/cv/trend/export")
def cv_trend_export(serial: str | None = None,
                    t_from: float | None = Query(None, alias="from"), t_to: float | None = Query(None, alias="to")):
    """Журнал проб в CSV (для разбора и обучения): по строке на пробу — рассев, причины брака, режим варки
    из ПЛК (температура, уровень, ток, разрежение, время варки, время с заводки). Без from/to — вся история."""
    import csv
    import io
    import time as _time
    rows = cv_store.export_rows(_cv_serial(serial), t_from, t_to)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cv_store.EXPORT_COLUMNS)
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if v is None else v) for k, v in r.items()})
    name = "cv_probes_%s.csv" % _time.strftime("%Y%m%d_%H%M")
    # utf-8-sig: Excel открывает без «кракозябр»; pandas читает как обычный utf-8
    return Response(content=buf.getvalue().encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="%s"' % name})


@app.get("/api/cv/objects")
def cv_objects(serial: str | None = None, ts: str = "", idx: int = 0):
    """Объекты кадра (кристаллы) для наведения: bbox/size_um/area_um2/group."""
    return {"objects": cv_store.get_objects(_cv_serial(serial), ts, idx)}


@app.get("/api/cv/overlay")
def cv_overlay(serial: str | None = None, ts: str = "", idx: int = 0):
    p = cv_store.overlay_path(_cv_serial(serial), ts, idx)
    if not p:
        return Response(status_code=404)
    return FileResponse(str(p), media_type="image/jpeg" if p.suffix == ".jpg" else "image/png")


@app.get("/api/cv/analyze")
def cv_analyze():
    res = micro.analyze_last_probe()
    api_log("api.cv.analyze", "Ручной запуск CV-анализа пробы", payload=res)
    return res


# --- автозапуск вместе с Windows (Планировщик задач, см. autostart.py) ---

@app.get("/api/autostart/status")
def api_autostart_status():
    return autostart.status()


@app.get("/api/autostart/enable")
def api_autostart_enable():
    data = autostart.enable()
    api_log("api.autostart", "Включение автозапуска с Windows", payload=data)
    return data


@app.get("/api/autostart/disable")
def api_autostart_disable():
    data = autostart.disable()
    api_log("api.autostart", "Выключение автозапуска с Windows", payload=data)
    return data


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
def rtsp_save(url: str, label: str = "", ip: str = "", scale: int = 100, fps: float = 0,
              serial: str = "", autostart: int | None = None):
    # проверяем схему уже при сохранении, а не только при стриминге — иначе в базу
    # попадает мусор (напр. file://), который потом отклоняется при попытке смотреть
    if not _is_rtsp_scheme(url):
        api_log("api.rtsp.save", "Отклонён RTSP-URL с недопустимой схемой", "warn", {"url": url})
        return {"error": "invalid_rtsp_scheme"}
    entry = {"url": url, "label": label, "ip": ip, "scale": scale, "fps": fps, "serial": serial}
    # autostart передаём ТОЛЬКО если он указан явно: обычное перезаписывание камеры
    # (сменили масштаб/fps) не должно сбрасывать галочку автоподключения
    if autostart is not None:
        entry["autostart"] = bool(autostart)
    items = rtsp_store.save(entry)
    api_log("api.rtsp.save", "RTSP-камера сохранена в базу", payload={"url": url, "count": len(items)})
    return {"items": items}


@app.get("/api/rtsp/autostart")
def rtsp_autostart(url: str, enabled: int, serial_number: str = ""):
    items = rtsp_store.set_autostart(url, bool(enabled))
    # синхронизируем живой воркер: снятая галочка не должна держать захват,
    # поставленная — сразу поднимает камеру, не дожидаясь перезапуска
    if serial_number:
        worker = manager.get_rtsp(serial_number, url)
        if worker is not None:
            worker.autostart = bool(enabled)
            if enabled:
                worker.ensure_capture()
    api_log("api.rtsp.autostart", "Автоподключение RTSP-камеры изменено",
            payload={"url": url, "enabled": bool(enabled), "serial_number": serial_number})
    return {"items": items, "autostart": bool(enabled)}


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
    worker = manager.get(serial_number)
    # если ещё не читали (обычно так на старте) — читаем по SDK, без genicam (−1020)
    if not getattr(worker, "data_limit", None) and hasattr(worker, "sdk_read_data_limit"):
        worker.sdk_read_data_limit()
    return worker.data_limit


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
def on_save_photo(serial_number: str, interval: int, project: str = "", photo_format: str = ""):
    data = manager.get(serial_number).on_photo(interval, project, photo_format or None)
    api_log("api.camera.on_save_photo", "Включено сохранение фото",
            payload={"interval": interval, "project": project,
                     "photo_format": photo_format, "result": data})
    return data


@app.get("/api/camera/off_save_photo")
def off_save_photo(serial_number: str):
    data = manager.get(serial_number).off_photo()
    api_log("api.camera.off_save_photo", "Выключено сохранение фото", payload=data)
    return data


@app.get("/api/camera/snap")
def camera_snap(serial_number: str, project: str = ""):
    # одиночный снимок по триггеру (soft-trigger): сохранить следующий кадр как фото.
    # Это примитив, который авто-цикл микроскопа будет дёргать в нужный момент пробы.
    data = manager.get(serial_number).snap(project or None)
    api_log("api.camera.snap", "Снимок по триггеру", payload={"serial_number": serial_number, "result": data})
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


# хостовая цветокоррекция кадра (гибрид «как в MVS»): гамма/насыщ/оттенок/контраст/
# яркость/CCM/палитра — применяются к потоку ЖИВЬЁМ, без перезапуска (см. camera_core._apply_color).
@app.get("/api/camera/color")
def camera_color(
    serial_number: str,
    gamma: float | None = None,
    saturation: float | None = None,
    hue: float | None = None,
    contrast: float | None = None,
    brightness: float | None = None,
    sharpness: float | None = None,   # 0 — без резкости; 0..2 — сила unsharp mask
    clarity: float | None = None,     # 0 — выкл; локальный контраст (CLAHE)
    denoise: float | None = None,     # 0 — выкл; шумоподавление (bilateral)
    ccm: str | None = None,        # 9 чисел через запятую (BGR 3x3) или "" — снять CCM
    palette: str | None = None,    # имя палитры или "" — без псевдоцвета
    wb_auto: int | None = None,    # 1 — авто баланс белого (серый мир), 0 — снять
    wb_r: float | None = None,     # ручные гейны каналов (при wb_auto=0)
    wb_g: float | None = None,
    wb_b: float | None = None,
    reset: int | None = None,      # 1 — сбросить всю цветокоррекцию
):
    worker = manager.get(serial_number)
    if reset:
        worker.color = {}
        plate_config.save({"camera_color": {}})   # запомнить сброс (в конфиг, входит в «Дамп»)
        return {"color": worker.color}
    patch = {}
    if wb_auto is not None or wb_r is not None or wb_g is not None or wb_b is not None:
        if wb_auto:
            patch["wb"] = {"auto": 1}
        else:
            wb = {k: v for k, v in (("r", wb_r), ("g", wb_g), ("b", wb_b)) if v is not None}
            # добираем недостающие гейны из текущих, чтобы правка одного канала не сбросила прочие
            cur = worker.color.get("wb") or {}
            for k in ("r", "g", "b"):
                patch_k = wb.get(k, cur.get(k))
                if patch_k is not None:
                    wb[k] = patch_k
            patch["wb"] = wb or None
    if gamma is not None: patch["gamma"] = gamma
    if saturation is not None: patch["saturation"] = saturation
    if hue is not None: patch["hue"] = hue
    if contrast is not None: patch["contrast"] = contrast
    if brightness is not None: patch["brightness"] = brightness
    if sharpness is not None: patch["sharpness"] = sharpness
    if clarity is not None: patch["clarity"] = clarity
    if denoise is not None: patch["denoise"] = denoise
    if ccm is not None:
        s = ccm.strip()
        if s:
            try:
                patch["ccm"] = [float(x) for x in s.split(",")][:9]
            except ValueError:
                patch["ccm"] = None
        else:
            patch["ccm"] = None
    if palette is not None:
        patch["palette"] = palette.strip()
    worker.color.update(patch)
    plate_config.save({"camera_color": worker.color})   # persist в конфиг (входит в «Дамп»)
    return {"color": worker.color}


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
            "zoom_factor": worker.zoom_factor,
            # состояние фонового захвата и здоровья автосохранения (см. camera_core)
            "reconnecting": worker.reconnecting,
            "autostart": worker.autostart,
            "viewers": worker.viewer_count(),
            **worker.photo_status()}


@app.get("/api/rtsp/on_save_photo")
def rtsp_on_save_photo(serial_number: str, interval: int, project: str = "", photo_format: str = ""):
    worker = manager.get_rtsp(serial_number)
    if worker is None:
        return {"error": "rtsp_not_connected"}
    data = worker.on_photo(interval, project, photo_format or None)
    api_log("api.rtsp.on_save_photo", "Включено автосохранение фото (RTSP)",
            payload={"interval": interval, "project": project,
                     "photo_format": photo_format, "result": data})
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
    return {"video": worker.save_video,
            "photo": worker.save_photo,
            "photo_count": worker.photo_saved_count,
            "video_elapsed": worker.video_elapsed(),
            "reconnecting": worker.reconnecting,
            "autostart": worker.autostart,
            # «включено, но не пишется» + причина (см. BaseCameraWorker.photo_status)
            **worker.photo_status()}


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
def rtsp_optical_zoom(serial_number: str, direction: str, speed: int = 1):
    worker = manager.get_rtsp(serial_number)
    if worker is None:
        return {"error": "rtsp_not_connected"}
    data = worker.optical_zoom(direction, speed)
    api_log("api.rtsp.optical_zoom", "Оптический зум RTSP",
            payload={"serial_number": serial_number, "direction": direction, "speed": speed, "result": data})
    return data


@app.get("/api/rtsp/lens_status")
def rtsp_lens_status(serial_number: str):
    worker = manager.get_rtsp(serial_number)
    if worker is None:
        return {"error": "rtsp_not_connected"}
    return worker.lens_status()


@app.get("/api/rtsp/focus")
def rtsp_focus(serial_number: str, direction: str):
    worker = manager.get_rtsp(serial_number)
    if worker is None:
        return {"error": "rtsp_not_connected"}
    data = worker.focus(direction)
    api_log("api.rtsp.focus", "Ручной фокус RTSP",
            payload={"serial_number": serial_number, "direction": direction, "result": data})
    return data


@app.get("/api/rtsp/focus_abs")
def rtsp_focus_abs(serial_number: str, value: float):
    worker = manager.get_rtsp(serial_number)
    if worker is None:
        return {"error": "rtsp_not_connected"}
    data = worker.set_focus_abs(value)
    api_log("api.rtsp.focus_abs", "Абсолютный фокус RTSP",
            payload={"serial_number": serial_number, "value": value, "result": data})
    return data


@app.get("/api/rtsp/autofocus")
def rtsp_autofocus(serial_number: str):
    worker = manager.get_rtsp(serial_number)
    if worker is None:
        return {"error": "rtsp_not_connected"}
    data = worker.auto_focus()
    api_log("api.rtsp.autofocus", "Автофокус RTSP",
            payload={"serial_number": serial_number, "result": data})
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
