from datetime import datetime
import os
import time
from os import mkdir
from pathlib import Path

from harvesters.core import Harvester
import numpy as np
import socket
import struct
import cv2


cti_path = "Driver/MvProducerGEV.cti"
H = Harvester()

# статус всех камер
cam_online = {}
# data limit камер
data_limit = {}

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
    except Exception as e:
        print("Ошибка загрузки драйвера:", e)
        Driver = False

# проверка состояния загрузки драйвера
def check():
    return Driver

# сканирование всех сетевых камер
# вывод серийник: статус камеры
def scan_cams():
    global cam_online
    # пробные данные(потом убрать)
    cam_online = {"DA123123":"1"}

    if check():
        for device in H.device_info_list:
            cam_online [device.serial_number] = device.access_status
            if device.serial_number not in data_limit:
                data_limit[device.serial_number] = None
    return cam_online

# подключение к камере и получение nodemap
def get_node_map_cam(serial_number):
    global H, data_limit
    ia = H.create({'serial_number': f'{serial_number}'})
    node_map = ia.remote_device.node_map
    # получение лимитов
    data_limit[serial_number] = get_camera_settings(node_map)
    return node_map, ia


# получение айпи камеры по серийнику
def get_ip(serial_number: str):
    ia = None
    # пробные данные(потом убрать)
    if serial_number == "DA123123":
        return {"ip": "192.168.2.10"}

    if check():
        try:
            node_map, ia = get_node_map_cam(serial_number)
            ip_ = node_map.GevCurrentIPAddress.value
            ip = int_to_ip(ip_)
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

        return True

    except Exception as e:
        print("Ошибка применение настроек камеры:", e)
        return False

    # разобрать с fps exposure_auto exposure_time чуть позже


def get_frame(ia,node_map):
    with ia.fetch() as buffer:
        data = buffer.payload.components[0].data
        real_width = node_map.Width.value
        real_height = node_map.Height.value
        img = np.array(data, dtype=np.uint8).reshape(real_height, real_width, 3)
        ok, buffer = cv2.imencode(".jpg", img)

        if not ok:
            return None

        return img, buffer.tobytes()

def generate_stream(serial_number, width=None, height=None, offset_x=None, offset_y=None, fps=None, exposure_auto=None, exposure_time=None):
    global stream_running, stream_closed, current_ia, photo_enabled, photo_interval, last_save, video_writer, video_enabled, video_duration, video_start, data_limit
    ia = None

    if not check():
        return

    if stream_running and not stream_closed:
        print("Старый поток еще не закрыт, force stop")
        close_stream_force()
        time.sleep(0.3)

    try:
        node_map, ia = get_node_map_cam(serial_number)
        data_limit[serial_number] = get_camera_settings(node_map)



        ok = apply_settings_camera(
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
            return

        current_ia = ia
        stream_running = True
        stream_closed = False

        ia.start()

        while stream_running:

            try:
                img, frame = get_frame(ia, node_map)
            except Exception as e:
                if not stream_running:
                    break
                print("Ошибка получения кадра:", repr(e))
                break

            if frame is None or img is None:
                continue

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
            print("Поток остановлен")
        else:
            print("Ошибка потока:", repr(e))

    finally:
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
    return {"status": "stopping"}

def close_stream_force():
    global stream_running, current_ia, stream_closed

    stream_running = False

    if current_ia is not None:
        try:
            current_ia.stop()
        except Exception as e:
            print("Ошибка force stop():", repr(e))

        try:
            current_ia.destroy()
        except Exception as e:
            print("Ошибка force destroy():", repr(e))

        current_ia = None

    stream_closed = True
    return {"status": "force_stopped"}

# Для фото
def on_save(interval):
    global photo_interval, photo_enabled
    photo_enabled = True
    photo_interval = interval
    return {"status": "ok", "photo_enabled": True, "interval": photo_interval}

def off_save():
    global photo_enabled, photo_interval, last_save
    photo_enabled = False
    photo_interval = None
    last_save = None
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
    return {"status": "ok", "video_enabled": "1"}


def off_video():
    global video_enabled, video_duration, video_start, video_writer
    if video_enabled == 1:
        video_enabled = 2
    return {"status": "ok", "video_enabled": "2"}


def check_video_enabled():
    global video_enabled, video_duration, video_start
    if video_enabled == 1:
        current_time = time.time()
        print("current_time: ", current_time, ", video_start: ", video_start, ", video_duration", video_duration)
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


def change_ip():

    return None







