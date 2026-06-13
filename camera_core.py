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

from pathlib import Path

class CameraWorker:
    def __init__(self,serial_number):
        self.serial_number = serial_number
        self.ia = None
        self.node_map = None
        self.running = False

        self.save_photo = False
        self.photo_interval = None
        self.last_photo = None

        self.save_video = 0
        self.video_duration = None
        self.video_start = None
        self.video_writer = None

        self.advanced_settings = False

        self.metrics = {
            "fps": 0.0,
            "image_number": 0,
            "bandwidth_mbps": 0.0,
            "width": 0,
            "height": 0,
            "errors": 0,
        }


workers = {}

# создать объект камеры если нет
def get_CameraWorker(serial_number):
    global workers
    if serial_number not in workers:
        workers[serial_number] = CameraWorker(serial_number)
    return workers[serial_number]










# Путь до директории и имя файла для поиска
program_dir = Path(__file__).resolve().parent
filename = "MvProducerGEV.cti"

# создание объекта для работы
H = Harvester()

# статус всех камер
cam_online = {}
# data limit камер
data_limit = {}

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

## глобальные переменные потоки стрима
#current_ia = None
#
#stream_running = False
#stream_closed = True
## сохранение фото и видео
#photo_enabled = False
#photo_interval = None
#last_save = None
#
#video_enabled = 0
#video_duration = None
#video_start = None
#video_writer = None
#
## расширенные настройки в смене айпи
#advanced_network_settings = False

# из ip в int для записи в камеру
def ip_to_int(ip):
    return struct.unpack("!I", socket.inet_aton(ip))[0]

# обратно для показа на списке
def int_to_ip(n):
    return socket.inet_ntoa(struct.pack("!I", n))

# загрузка драйвера для работы
def load_driver():
    global Driver, H, filename
    try:
        cti_path = next(program_dir.rglob(filename), None)
        if cti_path is not None:
            cti_path = str(cti_path)
            H.add_file(cti_path)
            H.update()
            Driver = True
            log_event("camera_core.load_driver", "Драйвер загружен", "success", {"cti_path": cti_path})
        else:
            log_event("camera_core.load_driver", "Драйвер отсутствует в папке программы", "error")
            Driver = False
    except Exception as e:
        Driver = False
        log_event("camera_core.load_driver", "Ошибка загрузки драйвера", "error", {"error": str(e)})

# проверка состояния загрузки драйвера
def check():
    if not Driver:
        load_driver()
        log_event("camera_core.load_driver", "Ошибка загрузки драйвера", "error", )
        if Driver:
            return Driver
    return Driver

# сканирование всех сетевых камер
# вывод серийник: статус камеры
def scan_cams():
    global cam_online
    # пробные данные(потом убрать)
    cam_online = {"DA123123":1}
    log_event("camera_core.scan_cams", "Запущено сканирование камер")
    H.update()
    load_driver()
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

def get_metrics(serial_number):
    cam = get_CameraWorker(serial_number)
    return cam.metrics

def get_stream_state(serial_number):
    cam = get_CameraWorker(serial_number)
    return {
        "serial_number": serial_number,
        "running": cam.running
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

def get_frame(serial_number, ia, node_map):
    cam = get_CameraWorker(serial_number)
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
        if not cam.running:
            log_event("camera_core.get_frame", "Ошибка получения кадра", "error",{"error": str(e)})
            return None, None
        raise

def generate_stream(serial_number, width=None, height=None, offset_x=None, offset_y=None, fps=None, exposure_auto=None, exposure_time=None):
    cam = get_CameraWorker(serial_number)



   #global stream_running, stream_closed, stream_metrics, current_ia, photo_enabled, photo_interval, last_save, video_writer, video_enabled, video_duration, video_start, data_limit
    ia = None
    last_frame_time = None


    if not check():
        return

    if cam.running:
        log_event("camera_core.generate_stream", "Старый поток открыт, принудительно закрытие", "warn")
        close_stream_force(serial_number)
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



        cam.ia = ia
        cam.running = True

        ia.start()
        log_event("camera_core.generate_stream", "Поток камеры запущен", "success", {"serial_number": serial_number})


        while cam.running:

            try:
                img, frame = get_frame(serial_number, ia, node_map)

                if frame is None or img is None:
                    cam.metrics["errors"] += 1
                    continue

                now = time.time()

                cam.metrics["image_number"] += 1
                cam.metrics["width"] = img.shape[1]
                cam.metrics["height"] = img.shape[0]

                if last_frame_time is not None:
                    dt = now - last_frame_time
                    if dt > 0:
                        cam.metrics["fps"] = 1.0 / dt

                if cam.metrics["fps"] > 0:
                    cam.metrics["bandwidth_mbps"] = (len(frame) * 8 * cam.metrics["fps"]) / 1_000_000

                last_frame_time = now


            except Exception as e:
                if not cam.running:
                    break
                log_event("camera_core.generate_stream", "Ошибка получения потока", "error", {"error": repr(e)})
                break



            # для фото проверка
            if cam.save_photo and check_save_photo(serial_number,cam.photo_interval):
                save_photo(img)

            check_video_enabled(serial_number)
            if cam.save_video == 1:
               writer_video(serial_number,img,fps)

            if cam.save_video == 2 and cam.video_writer is not None:
                cam.video_writer.release()
                cam.video_writer = None
                cam.save_video = 0
                cam.video_duration = None
                cam.video_start = None

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            )

    except Exception as e:
        if not cam.running:
            log_event("camera_core.generate_stream", "Поток остановлен", "error", {"error": repr(e)})
        else:
            log_event("camera_core.generate_stream", "Ошибка потока", "error", {"error": repr(e)})
            cam.metrics["errors"] += 1

    finally:
        log_event("camera_core.generate_stream", "Поток камеры закрыт", "info", {"serial_number": serial_number})

        cam.running = False

        if cam.ia is not None:

            try:
                cam.ia.stop()
            except:
                pass

            try:
                cam.ia.destroy()
            except:
                pass

            if cam.ia is ia:
                cam.ia = None

            cam.save_photo = False
            cam.photo_interval = None
            cam.last_photo = None

            if cam.video_writer is not None:
                cam.video_writer.release()
                cam.video_writer = None
                cam.save_video = 0
                cam.video_duration = None
                cam.video_start = None



def close_stream(serial_number):
    cam = get_CameraWorker(serial_number)
    cam.running = False
    log_event("camera_core.close_stream", "Запрошена мягкая остановка потока")
    return {"status": "stopping"}

def close_stream_force(serial_number):
    cam = get_CameraWorker(serial_number)
    cam.running = False

    if cam.ia is not None:
        try:
            cam.ia.stop()
        except Exception as e:
            log_event("camera_core.close_stream_force", "Ошибка force stop", "warn",{"error": e})

        try:
            cam.ia.destroy()
        except Exception as e:
            log_event("camera_core.close_stream_force", "Ошибка force destroy", "warn", {"error": e})

        cam.ia = None

    log_event("camera_core.close_stream_force", "Запрошена принудительная остановка потока", "warn")
    return {"status": "force_stopped"}

# Для фото
def on_photo(serial_number, interval):
    cam = get_CameraWorker(serial_number)

    cam.save_photo = True
    cam.photo_interval = interval

    log_event("camera_core.on_save", "Вкл. автосохранение фото c интервалом", "info", {"interval": interval})
    return {"status": "ok", "photo_enabled": True, "interval": cam.photo_interval}

def off_photo(serial_number):
    cam = get_CameraWorker(serial_number)

    cam.save_photo = False
    cam.photo_interval = None
    cam.last_photo = None

    log_event("camera_core.off_save", "Выкл. автосохранение фото")
    return {"status": "ok", "photo_enabled": False}

def check_save_photo(serial_number, interval):
    current_time = time.time()
    cam = get_CameraWorker(serial_number)

    if interval is None:
        return False

    if cam.last_photo is None:
        cam.last_photo = current_time
        return True

    else:
        if current_time - cam.last_photo >= interval:
            cam.last_photo = current_time
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

def on_video(serial_number, duration):
    cam = get_CameraWorker(serial_number)


    if duration is None:
        cam.video_duration = None
    elif cam.save_video == 0:
        cam.video_duration = duration

    if cam.save_video == 0:
        cam.save_video = 1
        cam.video_start = time.time()
    log_event("camera_core.on_video", "Вкл. автосохранение видео с длительностью: ", "info", {"video_duration": cam.video_duration})
    return {"status": "ok", "video_enabled": "1"}


def off_video(serial_number):
    cam = get_CameraWorker(serial_number)
    if cam.save_video == 1:
        cam.save_video = 2
    log_event("camera_core.off_video", "Выкл. автосохранение видео")
    return {"status": "ok", "video_enabled": "2"}


def check_video_enabled(serial_number):
    cam = get_CameraWorker(serial_number)
    if cam.save_video == 1:
        current_time = time.time()
        if cam.video_duration is not None:
            if current_time - cam.video_start >= cam.video_duration:
                cam.save_video = 2


def writer_video(serial_number,img,fps):
    cam = get_CameraWorker(serial_number)

    if cam.video_writer is None:
        folder = Path("Videos")
        folder.mkdir(parents=True, exist_ok=True)
        filename = f"Video{datetime.now().strftime('%d_%m_%H_%M_%S')}.avi"
        path = os.path.join(folder, filename)
        fourcc = cv2.VideoWriter_fourcc(*"MJPG")
        cam.video_writer = cv2.VideoWriter(path,fourcc, fps, (img.shape[1],img.shape[0]))
        cam.video_writer.write(img)
    else:
        cam.video_writer.write(img)

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

def set_advanced_network_settings(serial_number):
    cam = get_CameraWorker(serial_number)
    cam.advanced_settings = True
    return {"advanced_network_settings": cam.advanced_settings}

def change_ip(serial_number, ip, mask="", gateway=""):
    cam = get_CameraWorker(serial_number)
    node_map, ia = None, None

    log_event("camera_core.change_ip","Запрошено изменение ip-mask-gateway","info",
              {"serial_number": serial_number, "ip": ip, "mask": mask, "gateway": gateway, "advanced": cam.advanced_settings})

    try:
        if check():
            if not cam.running:
                old_ip, old_mask, old_gateway, dhcp_enabled = get_network_settings(serial_number)

                if old_ip is not None and old_mask is not None and old_gateway is not None:

                    log_event("camera_core.change_ip", "Запрошены старые сетевые настройки", "info",
                              {"serial_number": serial_number, "ip": ip, "mask": mask, "gateway": gateway,"advanced": dhcp_enabled})

                    if mask == "" and gateway == "":
                        cam.advanced_settings = False

                    if not cam.advanced_settings:
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
                        node_map, ia = get_node_map_cam(serial_number)
                        if node_map is None or ia is None:
                            log_event("camera_core.change_ip", "не доступен node_map", "warn")
                            return {"ip": "node_map_not_available"}

                        if dhcp_enabled:
                            node_map.GevCurrentIPConfigurationPersistentIP.value = True
                            node_map.GevCurrentIPConfigurationDHCP.value = False

                        if ip_changed:
                            node_map.GevPersistentIPAddress.value = ip_to_int(ip)

                        if cam.advanced_settings:
                            if mask_changed:
                                node_map.GevPersistentSubnetMask.value = ip_to_int(mask)
                            if gateway_changed:
                                node_map.GevPersistentDefaultGateway.value = ip_to_int(gateway)
                        else:
                            log_event("camera_core.change_ip", "mask-gateway нет изменени, т.к. не прожата кнопка", "warn")
                            return {"ip": "mask_gateway_not_changed_advanced_off"}

                        time.sleep(1)
                        node_map.DeviceReset.execute()

                        if ip_changed and cam.advanced_settings and advanced_changed:
                            log_event("camera_core.change_ip", "Ip mask gateway успешно поменяны","info")
                            return {"ip": "ip_mask_gateway_changed"}
                        elif ip_changed:
                            log_event("camera_core.change_ip", "Ip успешно поменян", "info")
                            return {"ip": "ip_changed"}
                        elif cam.advanced_settings and advanced_changed:
                            log_event("camera_core.change_ip", "Mask gateway успешно поменяны", "info")
                            return {"ip": "mask_gateway_changed"}

                        log_event("camera_core.change_ip", "неизвестная ошибка ip", "warn")
                        return {"ip": "unknown"}

                    except Exception as e:
                        log_event("camera_core.change_ip", "Ошибка изменения ip-mask-gateway", "warn")
                        return {"error": "Ошибка изменения ip-mask-gateway"}
                    finally:
                        if ia is not None:
                            try:
                                cam.ia.stop()
                            except Exception as e:
                                log_event("camera_core.close_stream_force", "Ошибка force stop", "warn", {"error": e})

                            try:
                                cam.ia.destroy()
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
        cam.advanced_settings = False