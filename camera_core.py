from harvesters.core import Harvester

cti_path = "Driver/MvProducerGEV.cti"
H = Harvester()

# статус всех камер
cam_online = {}
# состояние загрузки драйвера
Driver = False


# загрузка драйвера для работы
def load_driver():
    global Driver,cti_path
    try:
        H.add_file(cti_path)
        H.update()
        Driver = True
    except:
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