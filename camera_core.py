from pickle import GLOBAL

from harvesters.core import Harvester
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
    global Driver,cti_path
    try:
        H.add_file(cti_path)
        H.update()
        Driver = True
    except Exception:
        Driver = False


# проверка состояния загрузки драйвера
def check():
    return Driver

# сканирование всех сетевых камер
# вывод серийник: статус камеры
def scan_cams():
    if check():
        for device in H.device_info_list:
            cam_online [device.serial_number] = device.access_status
    return cam_online

# подключение к камере и получение nodemap
def get_node_map_cam(H,serial_number):
    ia = H.create({'serial_number': f'{serial_number}'})
    node_map = ia.remote_device.node_map
    return node_map, ia


# получение айпи камеры по серийнику
def get_ip(serial_number: str):
    global H
    ia = None
    if check():
        try:
            node_map, ia = get_node_map_cam(H,serial_number)
            ip_ = node_map.GevCurrentIPAddress.value
            ip = int_to_ip(ip_)
            return {"ip":ip}
        finally:
            if ia is not None:
                ia.destroy()






