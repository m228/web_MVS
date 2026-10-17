"""Общая настройка pytest для web_MVS.

Главное — ПЕСОЧНИЦА: `paths.DATA_DIR` подменяется на временную папку ДО импорта любого
модуля проекта (они берут DATA_DIR при импорте), чтобы тесты не трогали боевые
plate_config.json / mvs.db / cv_results. Тот же приём, что в selfcheck.py.

Запуск из корня репозитория:  .venv\\Scripts\\python.exe -m pytest -q
"""
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# DATA_DIR на сессию — до импорта модулей проекта в тестах
import paths  # noqa: E402

_SANDBOX = Path(tempfile.mkdtemp(prefix="web_mvs_pytest_"))
paths.DATA_DIR = _SANDBOX


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Свой DATA_DIR на тест: переключаем paths и перезагруженные в модулях ссылки на него.

    Патчим ВСЕ модули, которые взяли DATA_DIR/пути при импорте (иначе cv_store/fracture_lab
    писали бы в общий сессионный sandbox и тесты протекали бы друг в друга). db.DB_PATH
    восстанавливаем после теста."""
    import cv_store
    import db
    import fracture_lab
    import plate_config
    import rtsp_store
    import save_settings

    old_db_path = db.DB_PATH
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(plate_config, "CONFIG_PATH", tmp_path / "plate_config.json")
    monkeypatch.setattr(plate_config, "BACKUP_PATH", tmp_path / "plate_config.backup.json")
    monkeypatch.setattr(plate_config, "LAST_GOOD_PATH", tmp_path / "plate_config.last_good.json")
    monkeypatch.setattr(plate_config, "CORRUPT_PATH", tmp_path / "plate_config.corrupt.json")
    monkeypatch.setattr(plate_config, "SNAP_DIR", tmp_path / "config_backups")
    monkeypatch.setattr(rtsp_store, "STORE", tmp_path / "rtsp_cameras.json")
    monkeypatch.setattr(save_settings, "STORE", tmp_path / "save_settings.json")
    monkeypatch.setattr(cv_store, "BASE", tmp_path / "cv_results")
    monkeypatch.setattr(cv_store, "HISTORY", tmp_path / "cv_history")
    monkeypatch.setattr(fracture_lab, "LAB_DIR", tmp_path / "fracture_lab")
    cv_store._HIST_FILLED.clear()
    cv_store._PHASE_CACHE.clear()
    db.set_path(tmp_path / "mvs.db")
    yield tmp_path
    db.close()
    db.set_path(old_db_path)
