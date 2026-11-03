"""monitor_release — какая версия web_MVS последняя на GitHub: чтобы панель и бот сообщали «завод на старой версии».

Раз в REFRESH_S (3 минуты) спрашиваем у GitHub последний релиз: один запрос от приёмника вместо запросов от каждого клиента, а клиенты
узнают о новой версии через GET /wait (monitor_app) за секунды и сами качают её с GitHub. Лимит GitHub без ключа — 60 запросов в час, 20 в час нам хватает. Нет связи или GitHub не ответил — прошлое значение остаётся, а если его не было, метки нет
(версию не додумываем). Адрес можно заменить переменной MONITOR_RELEASE_URL; пустое значение отключает проверку.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.request
from typing import Callable, Optional

log = logging.getLogger("monitor.release")

LATEST_URL = "https://api.github.com/repos/m228/web_MVS/releases/latest"
REFRESH_S = 180
RETRY_S = 600
HTTP_TIMEOUT_S = 10
_VER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def parse_version(v) -> Optional[tuple]:
    """«2.0.50» / «v2.0.50» -> (2, 0, 50); всё остальное («dev», пусто) -> None."""
    m = _VER.match(str(v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def is_older(version, latest) -> bool:
    """Версия завода строго ниже последней. Не удалось разобрать любую из двух — не утверждаем."""
    a, b = parse_version(version), parse_version(latest)
    return a is not None and b is not None and a < b


def fetch_latest(url: str) -> str:
    """Номер последнего релиза без «v». Через системный прокси, как и остальные внешние запросы приёмника."""
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "web_MVS-monitor"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
        tag = str(json.loads(r.read().decode("utf-8")).get("tag_name") or "")
    ver = parse_version(tag)
    if ver is None:
        raise ValueError("у релиза необычный тег: %r" % tag[:40])
    return ".".join(str(x) for x in ver)


class Releases:
    def __init__(self, url: Optional[str] = None, fetch: Optional[Callable[[str], str]] = None, clock: Callable[[], float] = time.time):
        env = os.environ.get("MONITOR_RELEASE_URL")
        self.url = url if url is not None else (LATEST_URL if env is None else env)
        self.fetch, self.clock = fetch or fetch_latest, clock
        self.latest: Optional[str] = None
        self.checked_t: Optional[float] = None
        self.error = ""
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def refresh(self) -> bool:
        """Один запрос. True — номер получен."""
        if not self.url:
            return False
        try:
            self.latest = self.fetch(self.url)
            self.checked_t, self.error = self.clock(), ""
            return True
        except Exception as e:
            self.error = "%s: %s" % (type(e).__name__, e)
            return False

    def start(self) -> None:
        if self._thread is None and self.url:
            self._thread = threading.Thread(target=self._loop, name="monitor-release", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        warned = False
        while not self._stop.is_set():
            ok = self.refresh()
            if not ok and not warned:
                log.warning("не удалось узнать последнюю версию на GitHub (%s); метки «старая версия» пока не будет", self.error)
            warned = not ok
            self._stop.wait(REFRESH_S if ok else RETRY_S)
