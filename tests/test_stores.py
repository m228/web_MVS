"""rtsp_store / save_settings: строгое чтение перед записью не даёт затереть базу при сбое,
атомарная запись. Боевые файлы не трогаем (DATA_DIR → tmp)."""
import rtsp_store
import save_settings


def test_rtsp_save_load_autostart_remove(data_dir):
    url = "rtsp://admin:x@127.0.0.9:554/cam"
    rtsp_store.save({"url": url, "label": "t", "ip": "127.0.0.9"})
    assert any(i["url"] == url for i in rtsp_store.load())
    rtsp_store.set_autostart(url, True)
    assert any(i["url"] == url and i.get("autostart") for i in rtsp_store.load())
    rtsp_store.remove(url)
    assert not any(i["url"] == url for i in rtsp_store.load())


def test_rtsp_save_keeps_other_cameras_on_corrupt_file(data_dir):
    # две камеры в базе, затем файл побит: новая запись НЕ должна стереть остальные
    rtsp_store.save({"url": "rtsp://127.0.0.1/a", "ip": "127.0.0.1"})
    rtsp_store.save({"url": "rtsp://127.0.0.2/b", "ip": "127.0.0.2"})
    rtsp_store.STORE.write_text("{битый", encoding="utf-8")
    before = rtsp_store.save({"url": "rtsp://127.0.0.3/c", "ip": "127.0.0.3"})
    # строгое чтение не прошло → запись отменена, база (битая) не перезаписана одной камерой
    assert not any(i.get("url") == "rtsp://127.0.0.3/c" for i in before)


def test_rtsp_second_save_preserves_autostart(data_dir):
    url = "rtsp://127.0.0.1/x"
    rtsp_store.save({"url": url, "ip": "127.0.0.1", "autostart": True})
    rtsp_store.save({"url": url, "ip": "127.0.0.1", "scale": 50})   # autostart не передан
    item = next(i for i in rtsp_store.load() if i["url"] == url)
    assert item["autostart"] is True and item["scale"] == 50


def test_save_settings_update_get(data_dir):
    save_settings.update("CAM1", photo_project="p1", photo_interval=5)
    got = save_settings.get("CAM1")
    assert got["photo_project"] == "p1" and got["photo_interval"] == 5


def test_save_settings_per_camera_isolation(data_dir):
    save_settings.update("CAM1", photo_project="a")
    save_settings.update("CAM2", photo_project="b")
    assert save_settings.get("CAM1")["photo_project"] == "a"
    assert save_settings.get("CAM2")["photo_project"] == "b"


def test_ip_change_keeps_serial_and_autostart(data_dir):
    # регрессия: после смены IP камера теряла serial (ключ воркера/проекта) и autostart.
    # app._update_rtsp_store_url переносит запись на новый url — проверяем, что оба поля сохраняются.
    import app
    old = "rtsp://admin:x@192.168.1.50:554/cam"
    rtsp_store.save({"url": old, "label": "цех", "ip": "192.168.1.50",
                     "serial": "SER123", "autostart": True, "scale": 50})
    new = "rtsp://admin:x@192.168.1.77:554/cam"
    app._update_rtsp_store_url(old, new, "192.168.1.77")
    items = rtsp_store.load()
    assert not any(i["url"] == old for i in items), "старый url должен исчезнуть"
    moved = next(i for i in items if i["url"] == new)
    assert moved["serial"] == "SER123", "serial потерян при смене IP"
    assert moved["autostart"] is True, "autostart сброшен при смене IP"
    assert moved["label"] == "цех" and moved["ip"] == "192.168.1.77"
