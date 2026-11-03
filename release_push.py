"""release_push — «толчок» о новой версии от приёмника мониторинга: клиенты не ждут своих 30 минут, а подхватывают релиз сразу.

Приёмник (monitor_server) раз в 3 минуты смотрит последний релиз на GitHub. Клиент держит у него длинный запрос GET /wait?known=<версия>
(Bearer-токен, как у пульса): приёмник отвечает, как только версия на GitHub изменилась. Новее установленной — просим автообновление
проверить GitHub сразу (auto_update.check_now, с разбросом до минуты) и дальше всё как обычно: скачать заранее, перезапуск в окне
(микроскоп — после пропарки, остальные — 20:00–08:00). Качает клиент сам с GitHub; приёмник релиз не раздаёт и ничего клиенту не навязывает.
Нет токена / отправка выключена / приёмник старый (нет /wait) — молчим и ждём обычных проверок раз в 30 минут.
"""
from __future__ import annotations

import json
import random
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

import sysmon
import updater
from logger import log_event, mask_secrets
from paths import read_version

WAIT_S = 25                  # столько приёмник держит запрос (см. monitor_app.WAIT_S)
HTTP_TIMEOUT_S = WAIT_S + 15
IDLE_S = 30                  # не настроено — заглянуть в настройки позже
REPOLL_S = 0.5               # ответ получен — спрашиваем снова
BACKOFF = (15, 30, 60, 120, 300, 600)
SPREAD_S = 60                # проверку GitHub клиенты начинают в течение этого окна, а не все в одну секунду


def _get_json(url: str, token: str, system_proxy: bool, timeout: float = HTTP_TIMEOUT_S) -> tuple[Optional[int], Optional[dict], str]:
    """GET JSON. (HTTP-код или None, ответ-словарь или None, текст ошибки)."""
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token, "User-Agent": "web_MVS-push/" + read_version()})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(None if system_proxy else {}))
    try:
        with opener.open(req, timeout=timeout) as r:
            data = json.loads(r.read(65536).decode("utf-8"))
            return r.status, data if isinstance(data, dict) else None, ""
    except urllib.error.HTTPError as e:
        return e.code, None, "HTTP %d" % e.code
    except urllib.error.URLError as e:
        return None, None, mask_secrets("Нет связи: %s" % (e.reason,))
    except Exception as e:
        return None, None, mask_secrets("Ошибка: %s" % e)


class ReleaseListener:
    """step() — один длинный запрос; возвращает, сколько секунд подождать до следующего. Поток только крутит step."""

    def __init__(self, on_release: Callable[[str], None], cfg_fn: Callable[[], dict] = sysmon.load_config, get: Callable = _get_json,
                 installed_fn: Callable[[], str] = read_version):
        self.on_release, self.cfg_fn, self.get, self.installed_fn = on_release, cfg_fn, get, installed_fn
        self.known = ""              # последняя версия, о которой нам сказал приёмник
        self.notified = ""           # о какой уже просили автообновление
        self.fails = 0
        self.status = {"ok": None, "latest": None, "error": ""}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def step(self) -> float:
        cfg = self.cfg_fn()
        if not sysmon.configured(cfg):
            return IDLE_S
        via_proxy = cfg["system_proxy"] and not sysmon.closed_network(cfg["url"], cfg["allow_http"])
        code, data, err = self.get("%s/wait?known=%s" % (cfg["url"], urllib.parse.quote(self.known)), cfg["token"], via_proxy)
        if code != 200 or not data:
            self.fails += 1
            self.status.update(ok=False, error="приёмник старый, нет /wait" if code == 404 else (err or "пустой ответ"))
            return BACKOFF[-1] if code == 404 else BACKOFF[min(self.fails - 1, len(BACKOFF) - 1)]
        self.fails = 0
        latest = str(data.get("latest") or "")
        self.status.update(ok=True, latest=latest or None, error="")
        if latest:
            self.known = latest
            if latest != self.notified and updater._version_tuple(latest) > updater._version_tuple(self.installed_fn()):
                self.notified = latest
                log_event("release_push", "Приёмник сообщил: вышла v%s — проверяю обновление сейчас" % latest, "info")
                self.on_release(latest)
        return REPOLL_S

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="release-push", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                delay = self.step()
            except Exception as e:                      # поток не должен умирать от случайной ошибки
                log_event("release_push", "Толчок о релизе: ошибка цикла: %s" % e, "error")
                delay = BACKOFF[-1]
            self._stop.wait(delay)


_listener: Optional[ReleaseListener] = None


def _check_updates(version: str) -> None:
    import auto_update
    ag = auto_update.agent()
    if ag is not None:
        ag.check_now(random.uniform(0, SPREAD_S))


def start(on_release: Callable[[str], None] = _check_updates) -> ReleaseListener:
    """Запустить поток (один на процесс). Вызывается из lifespan приложения."""
    global _listener
    if _listener is None:
        _listener = ReleaseListener(on_release)
        _listener.start()
    return _listener


def stop() -> None:
    if _listener is not None:
        _listener.stop()


def listener() -> Optional[ReleaseListener]:
    return _listener
