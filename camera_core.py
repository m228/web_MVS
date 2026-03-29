from harvesters.core import Harvester


Harvester = Harvester()
# статус всех камер
cam_online = {}

# подгрузка драйвера для работы
def main(H):
    try:
        H.add_file("MvProducerGEV.cti")
        H.update()
    except:
        return False
    finally:
        return True

# проверка подгрузки драйвера
def check():
    return main(Harvester)

# сканирование всех сетевых камер
# вывод серийник: статус камеры
def scan_cams():
    if main(Harvester):
        for device in Harvester.device_info_list:
            cam_online [device.serial_number] = device.access_status
            return cam_online
