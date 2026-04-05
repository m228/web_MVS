from harvesters.core import Harvester
import numpy as np
import socket
import struct


cti_path = "Driver/MvProducerGEV.cti"
H = Harvester()

# статус всех камер
cam_online = {}
# состояние загрузки драйвера
Driver = False

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
    cam_online = {}

    if check():
        for device in H.device_info_list:
            cam_online [device.serial_number] = device.access_status
    return cam_online

# подключение к камере и получение nodemap
def get_node_map_cam(serial_number):
    global H
    ia = H.create({'serial_number': f'{serial_number}'})
    node_map = ia.remote_device.node_map
    return node_map, ia


# получение айпи камеры по серийнику
def get_ip(serial_number: str):
    ia = None
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
    print(len(cam_online))
    return {"count": len(cam_online)}


# получение данных с камеры, текущие + лимиты
def get_camera_settings(node_map):
    if check():
        data = {
            "width": {
                "value": node_map.Width.value,
                "min": node_map.Width.min,
                "max": node_map.Width.max,
            },
            "height": {
                "value": node_map.Height.value,
                "min": node_map.Height.min,
                "max": node_map.Height.max,
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


# нужна для проверки в submit_settings_camera
def check_value(value,min,max) -> bool:
    if value is None:
        return False
    return min <= value <= max

def apply_settings_camera(node_map, data_limit, width=None, height=None, offset_x=None, offset_y=None, fps=None, exposure_auto=None, exposure_time=None):
    try:
        if check_value(width, data_limit["width"]["min"], data_limit["width"]["max"]):
            print("set Width", width)
            node_map.Width.value = int(width)

        if check_value(height, data_limit["height"]["min"], data_limit["height"]["max"]):
            print("set Height", height)
            node_map.Height.value = int(height)

        if check_value(offset_x, data_limit["offset_x"]["min"], data_limit["offset_x"]["max"]):
            print("set OffsetX", offset_x)
            node_map.OffsetX.value = int(offset_x)

        if check_value(offset_y, data_limit["offset_y"]["min"], data_limit["offset_y"]["max"]):
            print("set OffsetY", offset_y)
            node_map.OffsetY.value = int(offset_y)

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
        return img

def connect_camera(serial_number, width=None, height=None, offset_x=None, offset_y=None, fps=None, exposure_auto=None, exposure_time=None):
    ia = None
    if check():
        try:
            node_map, ia = get_node_map_cam(serial_number)
            data_limit = get_camera_settings(node_map)

            if apply_settings_camera(node_map,data_limit,width=width,height=height,offset_x=offset_x,offset_y=offset_y,fps=fps,exposure_auto=exposure_auto,exposure_time=exposure_time):
                ia.start()
                img = get_frame(ia, node_map)
                ia.stop()
                return img
            else:
                return None
        finally:
            if ia is not None:
                ia.destroy()


# сначала вбиваются настройки и потом кнопка подключиться(она меняется, потом на применить) и камера подключается и запускается с этими настройками

"""
будем от этого плясать
def get_frame():
    global ia

    node_map = ia.remote_device.node_map
    width = node_map.Width.value
    height = node_map.Height.value

    with ia.fetch() as buffer:
        data = buffer.payload.components[0].data
        img = np.array(data, dtype=np.uint8).reshape(height, width, 3)
        return img


def get_jpeg():
    frame = get_frame()
    success, jpeg = cv2.imencode(".jpg", frame)

    if not success:
        return None

    return jpeg.tobytes()
"""