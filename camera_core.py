from harvesters.core import Harvester

Harvester = Harvester()

Harvester.add_file("MvProducerGEV.cti")
Harvester.update()

status = {}

def scan_cams():
    for device in Harvester.device_info_list:
        status [device.serial_number] = device.access_status
        return status
