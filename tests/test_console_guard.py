"""console_guard: файловый журнал собранного exe (logs\\app.log) и защита консоли.

Без Windows-консоли и без включения faulthandler/atexit: проверяется то, что можно проверить в песочнице, —
метки времени, фильтр успешных запросов, ротация, Tee (файл раньше консоли, ошибки консоли не мешают), install().
"""
import io
import os
import re
import sys

import console_guard as cg


def _lines(path):
    return path.read_text(encoding="utf-8").splitlines()


def test_logfile_timestamps_ansi_and_print_style_writes(tmp_path):
    """print() пишет текст и перевод строки отдельными write — метка времени должна встать один раз, в начало строки; цвета uvicorn вырезаются."""
    p = tmp_path / "app.log"
    lf = cg.LogFile(p)
    lf.write("\x1b[32mINFO\x1b[0m:     started\n")
    lf.write("part one")
    lf.write(" part two")
    lf.write("\n")
    lf.write("a\nb\n")
    lf.close()
    rows = _lines(p)
    stamp = r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d "
    assert len(rows) == 4
    assert re.match(stamp + r"INFO:     started$", rows[0])
    assert re.match(stamp + r"part one part two$", rows[1])
    assert re.match(stamp + r"a$", rows[2]) and re.match(stamp + r"b$", rows[3])


def test_logfile_skips_ok_requests_keeps_errors_and_prints(tmp_path):
    """Страницы опрашивают сервер каждую секунду: успешные запросы в файл не пишем, ошибки и обычный текст — пишем."""
    p = tmp_path / "app.log"
    lf = cg.LogFile(p)
    lf.write('INFO:     127.0.0.1:50122 - "GET /api/micro/telemetry HTTP/1.1" 200 OK\n')
    lf.write('INFO:     127.0.0.1:50123 - "GET /static/app.js HTTP/1.1" 304 Not Modified\n')
    lf.write('INFO:     127.0.0.1:50124 - "GET /api/nope HTTP/1.1" 404 Not Found\n')
    lf.write('INFO:     127.0.0.1:50125 - "POST /api/x HTTP/1.1" 500 Internal Server Error\n')
    lf.write('print: "GET отправлен" и всё\n')
    lf.close()
    text = p.read_text(encoding="utf-8")
    assert "200 OK" not in text and "304" not in text
    assert "404 Not Found" in text and "500 Internal Server Error" in text and "GET отправлен" in text


def test_logfile_rotates_at_limit(tmp_path):
    p = tmp_path / "app.log"
    lf = cg.LogFile(p, max_bytes=300)
    for i in range(30):
        lf.write("строка номер %d\n" % i)
    lf.close()
    assert (tmp_path / "app.log.1").is_file(), "старый журнал ушёл в архивный файл"
    assert p.stat().st_size <= 300 + 60, "текущий файл не растёт без предела"
    assert "строка номер 29" in p.read_text(encoding="utf-8"), "свежие строки — в текущем файле"


def test_logfile_never_raises(tmp_path):
    lf = cg.LogFile(tmp_path / "app.log")
    lf.close()
    lf.write("после закрытия\n")            # не исключение: журнал не должен ронять приложение
    lf.flush()


class _BrokenConsole:
    def write(self, data):
        raise OSError("консоль закрыта")

    def flush(self):
        raise OSError("консоль закрыта")

    def isatty(self):
        return True


def test_tee_writes_file_first_and_swallows_console_errors(tmp_path):
    lf = cg.LogFile(tmp_path / "app.log")
    t = cg.Tee(lf, _BrokenConsole())
    t.write("до падения консоли\n")
    t.flush()
    lf.close()
    assert "до падения консоли" in (tmp_path / "app.log").read_text(encoding="utf-8")
    assert t.isatty() is True, "isatty — от консоли: по нему uvicorn решает, красить ли вывод"


def test_tee_without_console(tmp_path):
    """Нет консоли (sys.stdout is None): uvicorn спрашивает isatty() и не должен упасть."""
    lf = cg.LogFile(tmp_path / "app.log")
    t = cg.Tee(lf, None)
    assert t.write("текст\n") == len("текст\n")
    assert t.isatty() is False
    t.flush()
    lf.close()
    assert "текст" in (tmp_path / "app.log").read_text(encoding="utf-8")


def test_tee_delegates_other_attributes():
    sio = io.StringIO()
    t = cg.Tee(cg.LogFile(os.devnull), sio)
    assert t.getvalue() == "" and t.closed is False


def test_install_redirects_output_to_app_log(tmp_path, monkeypatch):
    monkeypatch.setattr(cg, "disable_quick_edit", lambda: True)       # настоящий режим консоли разработчика не трогаем
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    path = cg.install(tmp_path, "9.9.9", crash_dump=False, register_exit=False)
    try:
        print("обычный вывод")
        print("ошибка", file=sys.stderr)
        sys.stdout.flush()
        logged = sys.stdout._log
    finally:
        logged.close()
    assert path == tmp_path / "logs" / "app.log"
    text = path.read_text(encoding="utf-8")
    assert "web_MVS 9.9.9: запуск" in text and "обычный вывод" in text and "ошибка" in text
    assert "обычный вывод" in out.getvalue() and "ошибка" in err.getvalue(), "консоль продолжает получать вывод"


def test_install_survives_unwritable_log_dir(tmp_path, monkeypatch):
    """Папку журнала создать нельзя (на её месте файл) — приложение работает как раньше, вывод не подменяется."""
    monkeypatch.setattr(cg, "disable_quick_edit", lambda: True)
    (tmp_path / "logs").write_text("я файл, а не папка", encoding="utf-8")
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert cg.install(tmp_path, "1.0", crash_dump=False, register_exit=False) is None
    assert sys.stdout is out


def test_disable_quick_edit_is_noop_outside_windows(monkeypatch):
    monkeypatch.setattr(cg.os, "name", "posix")
    assert cg.disable_quick_edit() is False
