"""Микроскоп по умолчанию включается только там, где рядом стоит CV-сервис; явная галочка главнее."""
import services


def _install_cv(base):
    d = base / "cv_service"
    (d / ".venv" / "Scripts").mkdir(parents=True)
    (d / ".venv" / "Scripts" / "python.exe").write_bytes(b"")
    (d / "server.py").write_text("")


def test_micro_off_without_cv_service(tmp_path):
    assert services.installed("cv", tmp_path) is False
    assert services.micro_enabled({"micro_enabled": None}, tmp_path) is False
    assert services.micro_enabled({}, tmp_path) is False


def test_micro_on_when_cv_service_installed(tmp_path):
    _install_cv(tmp_path)
    assert services.installed("cv", tmp_path) is True
    assert services.micro_enabled({"micro_enabled": None}, tmp_path) is True


def test_explicit_checkbox_beats_auto(tmp_path):
    assert services.micro_enabled({"micro_enabled": True}, tmp_path) is True      # включили руками без сервиса
    _install_cv(tmp_path)
    assert services.micro_enabled({"micro_enabled": False}, tmp_path) is False    # сервис есть, но выключили галочкой


def test_default_is_auto():
    import plate_config
    assert plate_config.DEFAULTS["micro_enabled"] is None
