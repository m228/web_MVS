from datetime import datetime
import os
import time
from lib2to3.pgen2 import driver
from pathlib import Path

from harvesters.core import Harvester
import numpy as np
import socket
import struct
import cv2

from logger import log_event

import subprocess


cti_path = "Driver/MvProducerGEV.cti"
H = Harvester()

# статус всех камер
cam_online = {}
# data limit камер
data_limit = {}
# метрики стрима
stream_metrics = {
    "fps": 0.0,
    "image_number": 0,
    "bandwidth_mbps": 0.0,
    "width": 0,
    "height": 0,
    "errors": 0,
}
# статусы доступа к камерам
access_status = {
    0: "Неизвестно",
    1: "Ok",
    2: "Только чтение",
    3: "Нет доступа",
    4: "Занята",
    5: "OpenReadWrite",
    6: "OpenReadOnly",
}


# состояние загрузки драйвера
Driver = False
# глобальные переменные потоки стрима
current_ia = None

stream_running = False
stream_closed = True
# сохранение фото и видео
photo_enabled = False
photo_interval = None
last_save = None

video_enabled = 0
video_duration = None
video_start = None
video_writer = None

# расширенные настройки в смене айпи
advanced_network_settings = False


# из ip в int для записи в камеру
def ip_to_int(ip):
    return struct.unpack("!I", socket.inet_aton(ip))[0]

# обратно для показа на списке
def int_to_ip(n):
    return socket.inet_ntoa(struct.pack("!I", n))



# загрузка драйвера для работы
def load_driver():
    global Driver,cti_path, H
    try:
        H.add_file(cti_path)
        H.update()
        Driver = True
        log_event("camera_core.load_driver", "Драйвер загружен", "success", {"cti_path": cti_path})
    except Exception as e:
        Driver = False
        log_event("camera_core.load_driver", "Ошибка загрузки драйвера", "error", {"error": str(e)})

# проверка состояния загрузки драйвера
def check():
    if not Driver:
        log_event("camera_core.load_driver", "Ошибка загрузки драйвера", "error", )
    return Driver

# сканирование всех сетевых камер
# вывод серийник: статус камеры
def scan_cams():
    global cam_online
    # пробные данные(потом убрать)
    cam_online = {"DA123123":1}
    log_event("camera_core.scan_cams", "Запущено сканирование камер")
    H.update()
    if check():
        for device in H.device_info_list:
            cam_online [device.serial_number] = int(device.access_status)
            if device.serial_number not in data_limit:
                data_limit[device.serial_number] = None
    return cam_online

# подключение к камере и получение nodemap
def get_node_map_cam(serial_number):
    global H, data_limit, cam_online

    status = cam_online.get(serial_number)
    if status != 1:
        log_event("camera_core.get_node_map_cam", "Ошибка статуса камеры", "error", {"status_camera": str(status)})
        return None, None
    try:

        ia = H.create({'serial_number': f'{serial_number}'})
        node_map = ia.remote_device.node_map
        # получение лимитов
        data_limit[serial_number] = get_camera_settings(node_map)
        log_event("camera_core.get_node_map_cam", "Получен nodemap камеры", "success", {"status_camera": str(node_map)})
        return node_map, ia
    except Exception as e:
        log_event("camera_core.get_node_map_cam", "Ошибка статуса камеры", "error", {"status_camera": e})
        return None, None



# получение айпи камеры по серийнику
def get_ip(serial_number: str):
    global cam_online
    ia = None
    status = cam_online.get(serial_number)
    if status != 1:
        log_event("camera_core.get_ip", "Ошибка получения ip камеры", "error", {"status_camera": str(status)})
        return None

    # пробные данные(потом убрать)
    if serial_number == "DA123123":
        return {"ip": "192.168.2.10"}

    if check():
        try:
            node_map, ia = get_node_map_cam(serial_number)
            ip_ = node_map.GevCurrentIPAddress.value
            ip = int_to_ip(ip_)
            log_event("camera_core.get_node_map_cam", "Получен ip камеры", "success",{"ip": str(ip)})
            return {"ip":ip}
        finally:
            if ia is not None:
                ia.destroy()


def count_cams():
    global cam_online
    return {"count": len(cam_online)}


# получение данных с камеры, текущие + лимиты
def get_camera_settings(node_map):
    if check():
        data = {
            "width": {
                "value": node_map.Width.value,
                "min": node_map.Width.min,
                "max": node_map.Width.max,
                "step": node_map.Width.inc,
            },
            "height": {
                "value": node_map.Height.value,
                "min": node_map.Height.min,
                "max": node_map.Height.max,
                "step": node_map.Height.inc,
            },
            "offset_x": {
                "value": node_map.OffsetX.value,
                "min": node_map.OffsetX.min,
                "max": node_map.OffsetX.max,
            },
            "offset_y": {
                "value": node_map.OffsetY.value,
                "min": node_map.OffsetY.min,
                "max": node_map.OffsetY.max,
            },
            "exposure_time": {
                "value": node_map.ExposureTime.value,
                "min": node_map.ExposureTime.min,
                "max": node_map.ExposureTime.max,
            },
            "exposure_auto": {
                "value": node_map.ExposureAuto.value,
                "options": node_map.ExposureAuto.symbolics,
            }
        }
        return data

def get_data_limit(serial_number):
    global data_limit
    return data_limit.get(serial_number)

def get_metrics():
    global stream_metrics
    return stream_metrics

def get_stream_state():
    global stream_running, stream_closed
    return {
        "running": stream_running,
        "closed": stream_closed,
    }

# нужна для проверки в submit_settings_camera
def check_value(value,min,max) -> bool:
    if value is None:
        return False
    return min <= value <= max

def apply_settings_camera(node_map, data_limit, width=None, height=None, offset_x=None, offset_y=None, fps=None, exposure_auto=None, exposure_time=None):
    try:
        if check_value(width, data_limit["width"]["min"], data_limit["width"]["max"]):
            node_map.Width.value = int(width)

        if check_value(height, data_limit["height"]["min"], data_limit["height"]["max"]):
            node_map.Height.value = int(height)

        if check_value(fps, 0.1, 30):
           node_map.AcquisitionFrameRate.value = int(fps)

        if check_value(exposure_time, data_limit["exposure_time"]["min"], data_limit["exposure_time"]["max"]):
            node_map.ExposureTime.value = int(exposure_time)
        log_event("camera_core.apply_settings_camera", "Применение всех параметров камеры", "success")
        return True, None

    except Exception as e:
        log_event("camera_core.apply_settings_camera", "Ошибка применение параметров камеры", "error", {"error": str(e)})
        return False, e

def get_frame(ia, node_map):
    try:
        with ia.fetch() as buffer:
            data = buffer.payload.components[0].data
            real_width = node_map.Width.value
            real_height = node_map.Height.value
            img = np.array(data, dtype=np.uint8).reshape(real_height, real_width, 3)
            ok, encoded = cv2.imencode(".jpg", img)

            if not ok:
                log_event("camera_core.get_frame", "Ошибка получения кадра", "error", {"ok": str(ok)})
                return None, None

            return img, encoded.tobytes()
    except Exception as e:
        if not stream_running:
            log_event("camera_core.get_frame", "Ошибка получения кадра", "error",{"error": str(e)})
            return None, None
        raise

def generate_stream(serial_number, width=None, height=None, offset_x=None, offset_y=None, fps=None, exposure_auto=None, exposure_time=None):
    global stream_running, stream_closed, stream_metrics, current_ia, photo_enabled, photo_interval, last_save, video_writer, video_enabled, video_duration, video_start, data_limit
    ia = None
    last_frame_time = None
    stream_metrics = {
        "fps": 0.0,
        "image_number": 0,
        "bandwidth_mbps": 0.0,
        "width": 0,
        "height": 0,
        "errors": 0,
    }

    if not check():
        return

    if stream_running or not stream_closed:
        log_event("camera_core.generate_stream", "Старый поток открыт, принудительно закрытие", "warn")
        close_stream_force()
        time.sleep(0.3)

    try:

        log_event(
            "camera_core.generate_stream",
            "Запрошен старт потока",
            "info",
            {
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

        node_map, ia = get_node_map_cam(serial_number)
        data_limit[serial_number] = get_camera_settings(node_map)

        ok, err = apply_settings_camera(
            node_map,
            data_limit[serial_number],
            width=width,
            height=height,
            offset_x=offset_x,
            offset_y=offset_y,
            fps=fps,
            exposure_auto=exposure_auto,
            exposure_time=exposure_time
        )

        if not ok:
            log_event("camera_core.generate_stream", "ошибка принятия настроек камеры", "warn", {"error": err, "ok": ok})
            return

        current_ia = ia
        stream_running = True
        stream_closed = False

        ia.start()
        log_event("camera_core.generate_stream", "Поток камеры запущен", "success", {"serial_number": serial_number})

        while stream_running:

            try:
                img, frame = get_frame(ia, node_map)

                if frame is None or img is None:
                    stream_metrics["errors"] += 1
                    continue

                now = time.time()

                stream_metrics["image_number"] += 1
                stream_metrics["width"] = img.shape[1]
                stream_metrics["height"] = img.shape[0]

                if last_frame_time is not None:
                    dt = now - last_frame_time
                    if dt > 0:
                        stream_metrics["fps"] = 1.0 / dt

                if stream_metrics["fps"] > 0:
                    stream_metrics["bandwidth_mbps"] = (len(frame) * 8 * stream_metrics["fps"]) / 1_000_000

                last_frame_time = now


            except Exception as e:
                if not stream_running:
                    break
                log_event("camera_core.generate_stream", "Ошибка получения потока", "error", {"error": repr(e)})
                break



            # для фото проверка
            if photo_enabled and check_save_photo(photo_interval):
                save_photo(img)

            check_video_enabled()
            if video_enabled == 1:
               writer_video(img,fps)

            if video_enabled == 2 and video_writer is not None:
                video_writer.release()
                video_writer = None
                video_enabled = 0
                video_duration = None
                video_start = None

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            )

    except Exception as e:
        if not stream_running:
            log_event("camera_core.generate_stream", "Поток остановлен", "error", {"error": repr(e)})
        else:
            log_event("camera_core.generate_stream", "Ошибка потока", "error", {"error": repr(e)})
            stream_metrics["errors"] += 1

    finally:
        log_event("camera_core.generate_stream", "Поток камеры закрыт", "info", {"serial_number": serial_number})

        stream_running = False
        stream_closed = True

        if ia is not None:

            try:
                ia.stop()
            except:
                pass

            try:
                ia.destroy()
            except:
                pass

            if current_ia is ia:
                current_ia = None

            photo_enabled = False
            photo_interval = None
            last_save = None

            if video_writer is not None:
                video_writer.release()
                video_writer = None
                video_enabled = 0
                video_duration = None
                video_start = None



def close_stream():
    global stream_running
    stream_running = False
    log_event("camera_core.close_stream", "Запрошена мягкая остановка потока")
    return {"status": "stopping"}

def close_stream_force():
    global stream_running, current_ia, stream_closed


    stream_running = False

    if current_ia is not None:
        try:
            current_ia.stop()
        except Exception as e:
            log_event("camera_core.close_stream_force", "Ошибка force stop", "warn",{"error": e})

        try:
            current_ia.destroy()
        except Exception as e:
            log_event("camera_core.close_stream_force", "Ошибка force destroy", "warn", {"error": e})

        current_ia = None

    stream_closed = True

    log_event("camera_core.close_stream_force", "Запрошена принудительная остановка потока", "warn")
    return {"status": "force_stopped"}

# Для фото
def on_photo(interval):
    global photo_interval, photo_enabled
    photo_enabled = True
    photo_interval = interval
    log_event("camera_core.on_save", "Вкл. автосохранение фото c интервалом", "info", {"interval": interval})
    return {"status": "ok", "photo_enabled": True, "interval": photo_interval}

def off_photo():
    global photo_enabled, photo_interval, last_save
    photo_enabled = False
    photo_interval = None
    last_save = None
    log_event("camera_core.off_save", "Выкл. автосохранение фото")
    return {"status": "ok", "photo_enabled": False}

def check_save_photo(interval):
    current_time = time.time()
    global last_save

    if interval is None:
        return False

    if last_save is None:
        last_save = current_time
        return True

    else:
        if current_time - last_save >= interval:
            last_save = current_time
            return True

    return False


def save_photo(img):
    folder = Path("dataset")
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"frame_{datetime.now().strftime('%d_%m_%H_%M_%S')}.jpg"
    path = os.path.join(folder, filename)
    cv2.imwrite(path, img)


# для видео
# video_enabled
# 0 нет автосохранение видео
# 1 идет автосохранение видео
# 2 завершение автосохранение видео

def on_video(duration):
    global video_enabled, video_duration, video_start, video_writer

    if duration is None:
        video_duration = None
    elif video_enabled == 0:
        video_duration = duration

    if video_enabled == 0:
        video_enabled = 1
        video_start = time.time()
    log_event("camera_core.on_video", "Вкл. автосохранение видео с длительностью: ", "info", {"video_duration": video_duration})
    return {"status": "ok", "video_enabled": "1"}


def off_video():
    global video_enabled, video_duration, video_start, video_writer
    if video_enabled == 1:
        video_enabled = 2
    log_event("camera_core.off_video", "Выкл. автосохранение видео")
    return {"status": "ok", "video_enabled": "2"}


def check_video_enabled():
    global video_enabled, video_duration, video_start
    if video_enabled == 1:
        current_time = time.time()
        if video_duration is not None:
            if current_time - video_start >= video_duration:
                video_enabled = 2


def writer_video(img,fps):
    global video_writer, video_fps

    if video_writer is None:
        folder = Path("Videos")
        folder.mkdir(parents=True, exist_ok=True)
        filename = f"Video{datetime.now().strftime('%d_%m_%H_%M_%S')}.avi"
        path = os.path.join(folder, filename)
        fourcc = cv2.VideoWriter_fourcc(*"MJPG")
        video_writer = cv2.VideoWriter(path,fourcc, fps, (img.shape[1],img.shape[0]))
        video_writer.write(img)
    else:
        video_writer.write(img)

def get_network_settings(serial_number):
    global cam_online
    ia = None
    status = cam_online.get(serial_number)
    if status != 1:
        return None, None, None, None

    if check():
        try:
            node_map, ia = get_node_map_cam(serial_number)

            ip = int_to_ip(node_map.GevCurrentIPAddress.value)
            mask = int_to_ip(node_map.GevCurrentSubnetMask.value)
            gateway = int_to_ip(node_map.GevCurrentDefaultGateway.value)
            dhcp_enabled = node_map.GevCurrentIPConfigurationDHCP.value
            log_event("camera_core.get_network_settings", "Успешное получение всех сетевых настроек камеры", "success", {"serial_number": serial_number})
            return ip, mask, gateway, dhcp_enabled
        except Exception as e:
            log_event("camera_core.get_network_settings", "Ошибка получение сетевых настроек", "error", {"error": e})
            return None, None, None, None

        finally:
            if ia is not None:
                ia.destroy()



def ping_device(ip: str) -> bool:
    result = subprocess.run(
        ["ping", "-n", "1", ip],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    log_event("camera_core.change_ip", "пинг до устройства", "info", {"result": result.returncode})
    return result.returncode == 0

def set_advanced_network_settings():
    global advanced_network_settings
    advanced_network_settings = True
    return {"advanced_network_settings": advanced_network_settings}

def change_ip(serial_number, ip, mask="", gateway=""):
    global advanced_network_settings, current_ia

    log_event("camera_core.change_ip","Запрошено изменение ip-mask-gateway","info",
              {"serial_number": serial_number, "ip": ip, "mask": mask, "gateway": gateway, "advanced": advanced_network_settings})

    try:
        if check():
            if stream_closed:
                old_ip, old_mask, old_gateway, dhcp_enabled = get_network_settings(serial_number)

                if old_ip is not None and old_mask is not None and old_gateway is not None:

                    log_event("camera_core.change_ip", "Запрошены старые сетевые настройки", "info",
                              {"serial_number": serial_number, "ip": ip, "mask": mask, "gateway": gateway,"advanced": dhcp_enabled})

                    if mask == "" and gateway == "":
                        advanced_network_settings = False

                    if not advanced_network_settings:
                        if not mask:
                            mask = old_mask
                        if not gateway:
                            gateway = old_gateway

                    ip_changed = old_ip != ip
                    mask_changed = old_mask != mask
                    gateway_changed = old_gateway != gateway
                    advanced_changed = mask_changed or gateway_changed

                    if not ip_changed and not advanced_changed:
                        log_event("camera_core.change_ip", "нет изменений ip-mask-gateway","warn")
                        return {"ip": "no_changes"}

                    if ip == gateway:
                        log_event("camera_core.change_ip", "ip совпадает с gateway", "warn")
                        return {"ip": "gateway==ip"}

                    if ip_changed:
                        device_busy = ping_device(ip)
                        if not device_busy:
                            log_event("camera_core.change_ip", "данный IP занят", "warn")
                            return {"ip": "ip_busy"}

                    try:
                        node_map, current_ia = get_node_map_cam(serial_number)
                        if node_map is None or current_ia is None:
                            log_event("camera_core.change_ip", "не доступен node_map", "warn")
                            return {"ip": "node_map_not_available"}

                        if dhcp_enabled:
                            node_map.GevCurrentIPConfigurationPersistentIP.value = True
                            node_map.GevCurrentIPConfigurationDHCP.value = False

                        if ip_changed:
                            node_map.GevPersistentIPAddress.value = ip_to_int(ip)

                        if advanced_network_settings:
                            if mask_changed:
                                node_map.GevPersistentSubnetMask.value = ip_to_int(mask)
                            if gateway_changed:
                                node_map.GevPersistentDefaultGateway.value = ip_to_int(gateway)
                        else:
                            log_event("camera_core.change_ip", "mask-gateway нет изменени, т.к. не прожата кнопка", "warn")
                            return {"ip": "mask_gateway_not_changed_advanced_off"}

                        time.sleep(1)
                        node_map.DeviceReset.execute()

                        if ip_changed and advanced_network_settings and advanced_changed:
                            log_event("camera_core.change_ip", "Ip mask gateway успешно поменяны","info")
                            return {"ip": "ip_mask_gateway_changed"}
                        elif ip_changed:
                            log_event("camera_core.change_ip", "Ip успешно поменян", "info")
                            return {"ip": "ip_changed"}
                        elif advanced_network_settings and advanced_changed:
                            log_event("camera_core.change_ip", "Mask gateway успешно поменяны", "info")
                            return {"ip": "mask_gateway_changed"}

                        log_event("camera_core.change_ip", "неизвестная ошибка ip", "warn")
                        return {"ip": "unknown"}

                    except Exception as e:
                        log_event("camera_core.change_ip", "Ошибка изменения ip-mask-gateway", "warn")
                        return {"error": "Ошибка изменения ip-mask-gateway"}
                    finally:
                        if current_ia is not None:
                            try:
                                current_ia.stop()
                            except Exception as e:
                                log_event("camera_core.close_stream_force", "Ошибка force stop", "warn", {"error": e})

                            try:
                                current_ia.destroy()
                            except Exception as e:
                                log_event("camera_core.close_stream_force", "Ошибка force destroy", "warn",{"error": e})
                else:
                    log_event("camera_core.change_ip", "Ip не получен", "warn")
                    return {"ip": "ip_not_received"}
            else:
                log_event("camera_core.change_ip", "Поток видео не закрыт", "warn")
                return {"ip": "stream_not_closed"}
        else:
            log_event("camera_core.change_ip", "не загружен драйвер", "warn")
            return {"ip": "not_driver"}
    finally:
        advanced_network_settings = False