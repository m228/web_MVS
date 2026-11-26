"""CV-сайдкар сообщает, на чём считает модель (вопрос № 13 учебника: Усп на AMD — GPU или CPU?). torch подменяем: в основном
окружении его нет, он живёт только в venv сайдкара."""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

DETECTOR = Path(__file__).resolve().parent.parent / "cv_service" / "detector.py"


@pytest.fixture
def det(monkeypatch):
    spec = importlib.util.spec_from_file_location("cv_detector_under_test", DETECTOR)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, mod)         # dataclass ищет свой модуль в sys.modules
    spec.loader.exec_module(mod)
    return mod.UltralyticsDetector


def fake_torch(name="NVIDIA GeForce GTX 1050 Ti", broken=False):
    def get_device_name(i):
        if broken:
            raise RuntimeError("no device")
        return "%s #%d" % (name, i) if i else name
    return types.SimpleNamespace(cuda=types.SimpleNamespace(get_device_name=get_device_name, is_available=lambda: True))


def test_cpu_is_named_cpu(det):
    assert det._device_name("cpu") == "CPU"


def test_gpu_name_comes_from_torch(det, monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", fake_torch())
    assert det._device_name("0") == "GPU NVIDIA GeForce GTX 1050 Ti"
    assert det._device_name("cuda:1") == "GPU NVIDIA GeForce GTX 1050 Ti #1"


def test_amd_rocm_build_shows_radeon(det, monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", fake_torch("AMD Radeon RX 6600"))
    assert det._device_name("0") == "GPU AMD Radeon RX 6600"


def test_unknown_gpu_still_says_gpu(det, monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", fake_torch(broken=True))
    assert det._device_name("0") == "GPU 0"
