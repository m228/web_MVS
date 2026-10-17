"""plate_config: сборка DEFAULTS+правки, атомарное сохранение, восстановление битого файла,
само-исцеление устаревшего sv_source. Боевой файл не трогаем (DATA_DIR → tmp)."""
import json

import plate_config


def test_defaults_merge_keys_present(data_dir):
    cfg = plate_config.load()
    for k in ("host", "port", "SP", "SVSP", "probe_cycle", "cv", "fracture", "sv_source"):
        assert k in cfg
    assert len(cfg["SP"]) == len(cfg["SVSP"])


def test_save_only_stores_overrides(data_dir):
    plate_config.save({"led_bright": 37})
    assert plate_config.load()["led_bright"] == 37
    # в файле — только правка, не весь слепок DEFAULTS
    on_disk = json.loads(plate_config.CONFIG_PATH.read_text(encoding="utf-8"))
    assert on_disk == {"led_bright": 37}


def test_deep_merge_nested(data_dir):
    plate_config.save({"probe_cycle": {"dwell_sec": 99}})
    cfg = plate_config.load()
    assert cfg["probe_cycle"]["dwell_sec"] == 99
    # прочие вложенные ключи probe_cycle остаются из DEFAULTS
    assert cfg["probe_cycle"]["retract_pos"] == plate_config.DEFAULTS["probe_cycle"]["retract_pos"]


def test_corrupt_file_recovered_from_last_good(data_dir):
    plate_config.save({"led_bright": 21})                       # пишет и last_good
    plate_config.CONFIG_PATH.write_text("{битый жсон", encoding="utf-8")
    cfg = plate_config.load()
    assert cfg["led_bright"] == 21                              # восстановлено из last_good
    assert plate_config.CORRUPT_PATH.is_file()                  # битый сохранён для разбора


def test_corrupt_without_last_good_falls_back_to_defaults(data_dir):
    plate_config.CONFIG_PATH.write_text("не json вовсе", encoding="utf-8")
    cfg = plate_config.load()
    assert cfg["led_bright"] == plate_config.DEFAULTS["led_bright"]


def test_stale_sv_source_dropped(data_dir):
    # старый слепок: sv_source с нулевыми reg → игнорируется, берётся свежий из DEFAULTS
    stale = {"sv_source": {"enabled": False, "fields": {"sv": {"reg": 0, "scale": 100}}, "sv_register": 0}}
    plate_config.CONFIG_PATH.write_text(json.dumps(stale), encoding="utf-8")
    cfg = plate_config.load()
    assert cfg["sv_source"]["enabled"] is True
    assert cfg["sv_source"]["fields"]["sv"]["reg"] == plate_config.DEFAULTS["sv_source"]["fields"]["sv"]["reg"]


def test_replace_all_replaces_not_merges(data_dir):
    plate_config.save({"led_bright": 5})
    plate_config.replace_all({"host": "10.0.0.1", "port": 502})
    # это ЗАМЕНА, а не merge: прежняя пользовательская правка led_bright должна исчезнуть из файла
    on_disk = json.loads(plate_config.CONFIG_PATH.read_text(encoding="utf-8"))
    assert on_disk == {"host": "10.0.0.1", "port": 502}
    assert plate_config.load()["host"] == "10.0.0.1"
    assert plate_config.BACKUP_PATH.is_file()                  # backup «before-import» сделан


def test_replace_all_rejects_empty(data_dir):
    import pytest
    for bad in ({}, [], "строка", None):
        with pytest.raises((ValueError, TypeError)):
            plate_config.replace_all(bad)


def test_read_snapshot_rejects_traversal(data_dir):
    import pytest
    for bad in ("../plate_config.json", "plate_config_2026.json", "..\\x", "plate_config_20261017_0900.txt"):
        with pytest.raises(ValueError):
            plate_config.read_snapshot(bad)
