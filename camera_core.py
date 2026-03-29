from harvesters.core import Harvester

Harvester = Harvester()

Harvester.add_file(r"\api\MvProducerGEV.cti")
Harvester.update()

def scan_cams():
    for device in Harvester.device_info_list:
        print(device['access_status'], device['display_name'], device['serial_number'])
