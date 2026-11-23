"""Файловый журнал событий (logs\\events.log): запись, склейка повторов, и что в него попадают срабатывания и переходы тревог мониторинга."""
import logging

import pytest

import logger
from tests.test_sysmon import configured_agent, pos, sm  # noqa: F401  (sm — фикстура)


@pytest.fixture
def events_log(tmp_path):
    path = tmp_path / "logs" / "events.log"
    assert logger.enable_file(path)
    yield path
    logger.disable_file()


def _lines(path):
    for h in logging.getLogger("web_mvs.events").handlers:
        h.flush()
    return path.read_text(encoding="utf-8").splitlines()


def test_log_event_goes_to_file(events_log):
    logger.log_event("demo", "Привет", "warn", {"serial": "A1", "n": 3})
    (line,) = _lines(events_log)
    assert "[warn] demo: Привет" in line and '"serial": "A1"' in line and '"n": 3' in line


def test_secrets_are_masked_in_file(events_log):
    logger.log_event("rtsp", "открываю rtsp://admin:secret@10.0.0.5/ch1")
    assert "secret" not in _lines(events_log)[0]


def test_consecutive_repeats_are_collapsed(events_log):
    for _ in range(5):
        logger.log_event("demo", "то же самое")
    logger.log_event("demo", "другое")
    lines = _lines(events_log)
    assert len(lines) == 3 and "повторилась ещё 4 раз" in lines[1] and "другое" in lines[2]


def test_no_file_when_not_enabled(tmp_path):
    logger.disable_file()
    logger.log_event("demo", "ничего не пишем")           # не падает, файла нет
    assert not (tmp_path / "logs").exists()


def test_bad_path_does_not_break_app(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert logger.enable_file(blocker / "logs" / "events.log") is False
    logger.log_event("demo", "работает без файла")


def test_alert_open_and_close_are_logged(sm, events_log):
    ag = configured_agent(sm)
    ag.add_positions(pos("подвод", timeout=True))
    ag.tick(ag.clock())                                                         # открылось жёлтое предупреждение
    ag.add_positions(pos("подвод"))
    ag.add_positions(pos("подвод"))
    ag.add_positions(pos("подвод"))
    ag.tick(ag.clock())                                                         # хорошие пробы — снято
    text = "\n".join(_lines(events_log))
    assert "не доехал за отведённое время" in text           # сам подвод с таймаутом
    assert "Правило сработало [pos_approach]" in text
    assert "ТРЕВОГА открыта [pos_approach, medium]" in text
    assert "Тревога снята [pos_approach]" in text and '"stage"' in text
