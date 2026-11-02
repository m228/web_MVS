"""Фоновое автообновление: само проверяет релизы GitHub, скачивает и перезапускает приложение, когда варки нет.

Раз в interval_min (по умолчанию 30) проверяем последний релиз; вышла новая версия — докачиваем в .update (как кнопка «скачать»:
дельта, иначе архив целиком, sha256). Дальше людям остаётся только «перезапустить» на странице — либо приложение делает это само,
когда окно открыто (MicroscopeFSM.restart_window: пропарка/после неё, калибровка нуля прошла, автомат свободен) и оно держится
HOLD_S секунд подряд: за warn_s секунд до запуска update.ps1 страницы показывают плашку «перезапуск через N с · Отмена».
Решает сервер, а не страница, поэтому работает и без открытых вкладок.

Защиты: только собранный exe; «Отмена» гасит перезапуск до следующего захода в окно; на версию не больше MAX_ATTEMPTS автозапусков
(откат обновления не должен гонять завод по кругу); сбой связи — пауза растёт до 60 минут, в журнал не чаще раза в час.
Заводы без микроскопа/связи с ПЛК: окна нет, перезапуск только кнопкой.
"""
import json
import math
import threading
import time
from typing import Callable, Optional

import plate_config
import sysmon
import update_health
import updater
from logger import log_event
from paths import DATA_DIR, read_version

TICK_S = 5                 # шаг потока: окно перезапуска и обратный отсчёт проверяются часто, GitHub — раз в interval_min
FIRST_CHECK_S = 120        # первая проверка после старта: не мешаем подъёму камер и сервисов
HOLD_S = 30                # окно должно держаться столько подряд, прежде чем показать плашку (стадия не мигает)
MAX_BACKOFF_S = 3600       # пауза после сбоя связи растёт до часа
ERROR_LOG_EVERY_S = 3600   # одну и ту же ошибку в журнал не чаще
MAX_ATTEMPTS = 2           # автозапусков обновления на одну версию
APP_PORT = 8000            # как run.PORT: update.ps1 ждёт ответа приложения на этом порту
STATE_FILE = DATA_DIR / "auto_update.json"   # попытки автозапуска по версиям и метка «обновляюсь»; ВНЕ .update — её апдейтер сносит
HEALTH_FIRST_S = 180       # первая проверка «всё поднялось» — через столько после старта нового приложения (камеры подключаются со страницы)
HEALTH_RETRY_S = 120       # при замечаниях — повтор через столько
HEALTH_MAX_S = 600         # дольше не ждём: вердикт «не всё поднялось»
PENDING_MAX_S = 3600       # метка «обновляюсь» старше часа — устаревшая, не считаем её итогом запуска

DEFAULT_CFG = {"enabled": True, "interval_min": 30, "auto_restart": True, "warn_s": 10}


def load_config() -> dict:
    """Настройки из plate_config (auto_update) с границами: интервал 1…1440 мин, отсчёт 3…120 с."""
    try:
        raw = plate_config.load().get("auto_update") or {}
    except Exception:
        raw = {}
    cfg = dict(DEFAULT_CFG)
    cfg.update({k: raw[k] for k in DEFAULT_CFG if k in raw})
    try:
        cfg["interval_min"] = min(1440.0, max(1.0, float(cfg["interval_min"])))
        cfg["warn_s"] = min(120, max(3, int(cfg["warn_s"])))
    except (TypeError, ValueError):
        cfg["interval_min"], cfg["warn_s"] = DEFAULT_CFG["interval_min"], DEFAULT_CFG["warn_s"]
    cfg["enabled"], cfg["auto_restart"] = bool(cfg["enabled"]), bool(cfg["auto_restart"])
    return cfg


def _read_state(path) -> dict:
    """auto_update.json: {"attempts": {версия: сколько раз запускали}, "pending": {from, to, t, pre} — запуск обновления в полёте}."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log_event("auto_update", "Не удалось записать auto_update.json: %s" % e, "warn")


def _read_attempts(path) -> dict:
    try:
        return {str(k): int(v) for k, v in (_read_state(path).get("attempts") or {}).items()}
    except (AttributeError, TypeError, ValueError):
        return {}


class AutoUpdater:
    def __init__(self, window_fn: Callable[[], tuple] = None, cfg_fn: Callable[[], dict] = load_config,
                 clock: Callable[[], float] = time.monotonic, frozen_fn: Callable[[], bool] = updater.is_frozen,
                 state_file=STATE_FILE, report_fn: Callable = sysmon.report, wall: Callable[[], float] = time.time,
                 health_src=None, start_reason_fn: Callable = sysmon.start_reason):
        self._window_fn = window_fn or (lambda: (False, "микроскоп не подключён"))
        self._cfg_fn = cfg_fn
        self._clock = clock
        self._frozen_fn = frozen_fn
        self._state_file = state_file
        self._report_fn = report_fn
        self._wall = wall
        self._health_src = health_src            # None — настоящие источники (update_health.default_sources), берём лениво
        self._start_reason_fn = start_reason_fn
        self._health = None                      # идёт проверка «всё поднялось»: {pre, from, to, next, deadline}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._cfg = dict(DEFAULT_CFG)
        t0 = clock()
        self._next_check = t0 + FIRST_CHECK_S
        self._fails = 0
        self._err_logged_at = None
        # кэш последней проверки — его и отдаёт /api/update/state (без сети)
        self._info = {"latest": None, "staged": None, "downloading": False, "last_check": None, "error": ""}
        # окно перезапуска
        self._window_since = None
        self._restart_at = None
        self._cancelled = False        # «Отмена» на этом заходе в окно; сбрасывается, когда окно закрылось
        self._waiting = ""
        self._launched = False

    # ---- жизненный цикл
    def start(self) -> None:
        if self._thread is not None:
            return
        try:
            self.resume()
        except Exception as e:                          # итог прошлого запуска не должен мешать запуску потока
            log_event("auto_update", "Автообновление: не разобрал итог прошлого запуска: %s" % e, "warn")
        self._thread = threading.Thread(target=self._loop, name="auto-update", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=3)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:                      # поток не должен умирать от случайной ошибки
                log_event("auto_update", "Автообновление: ошибка цикла: %s" % e, "error")
            self._stop.wait(TICK_S)

    # ---- один шаг
    def tick(self) -> None:
        cfg = self._cfg_fn()
        self._cfg = cfg
        if not self._frozen_fn():
            return
        now = self._clock()
        if self._health is not None:                    # проверка после обновления идёт и при выключенном автообновлении (обновили кнопкой)
            self._health_step(now)
        if not cfg["enabled"]:
            return
        if now >= self._next_check:
            self._check(now, cfg)
        self._restart_step(now, cfg)

    def _backoff_error(self, now: float, cfg: dict, message: str) -> None:
        """Сбой проверки/скачивания: пауза ×2 (до часа); в журнал — не чаще раза в час."""
        self._fails += 1
        self._next_check = now + min(MAX_BACKOFF_S, cfg["interval_min"] * 60 * 2 ** (self._fails - 1))
        with self._lock:
            self._info["error"] = message
        if self._err_logged_at is None or now - self._err_logged_at >= ERROR_LOG_EVERY_S:
            self._err_logged_at = now
            log_event("auto_update", "Автообновление: " + message, "warn", {"next_try_min": round((self._next_check - now) / 60)})

    def _check(self, now: float, cfg: dict) -> None:
        res = updater.check_latest(quiet=True)
        if res.get("error"):
            self._backoff_error(now, cfg, res["error"])
            return
        self._next_check = now + cfg["interval_min"] * 60
        staged, downloading = res.get("staged"), bool(res.get("downloading"))
        with self._lock:
            self._info.update(latest=res.get("latest"), staged=staged, downloading=downloading, error="",
                              last_check=time.time())
        if not res.get("update_available") or downloading or staged == res.get("latest"):
            self._fails = 0                             # делать нечего — цикл прошёл чисто
            return
        r = updater.download_latest(extract=False)
        if r.get("busy"):                               # кто-то уже качает (кнопка на странице) — посмотрим на следующей проверке
            self._next_check = now + 60
            return
        if not r.get("ok"):
            self._backoff_error(now, cfg, r.get("error") or "не удалось скачать обновление")
            return
        self._fails = 0
        ver = r.get("version") or res.get("latest")
        with self._lock:
            self._info["staged"] = ver
        log_event("auto_update", "Обновление v%s скачано заранее — перезапуск по кнопке или после варки" % ver,
                  "success", {"auto_restart": cfg["auto_restart"]})
        if not r.get("already"):                        # «уже скачано» — об этом сказали раньше (например, кнопка на странице)
            self._notify("staged", "info", "Скачана v%s, %s" % (ver, "перезапущусь после варки" if cfg["auto_restart"] else "жду перезапуска по кнопке"),
                         {"to": ver})

    # ---- перезапуск, когда варки нет
    def _restart_step(self, now: float, cfg: dict) -> None:
        with self._lock:
            staged = self._info.get("staged")
        if self._launched or not staged or updater._version_tuple(staged) <= updater._version_tuple(read_version()):
            self._close_window("")
            return
        if not cfg["auto_restart"]:
            self._close_window("автоперезапуск выключен в настройках")
            return
        ok, reason = self._window_fn()
        if not ok:
            self._close_window(reason)
            return
        if self._window_since is None:
            self._window_since = now
        if self._cancelled:
            self._waiting = "отменено — до следующей пропарки"
            return
        if _read_attempts(self._state_file).get(staged, 0) >= MAX_ATTEMPTS:
            self._waiting = "автоперезапуск на v%s отключён: прошлые попытки не удались, нажмите «Перезапустить»" % staged
            return
        if self._restart_at is None:
            held = now - self._window_since
            if held < HOLD_S:
                self._waiting = "варки нет, жду %d с" % math.ceil(HOLD_S - held)
                return
            self._restart_at = now + cfg["warn_s"]
            self._waiting = ""
            log_event("auto_update", "Варки нет — перезапуск на v%s через %d с" % (staged, cfg["warn_s"]), "warn")
            return
        if now >= self._restart_at:
            self._launch(staged)

    def _close_window(self, waiting: str) -> None:
        """Окно закрылось (или перезапуск не нужен): отсчёт и «Отмена» сбрасываются, следующий заход в окно — с чистого листа."""
        if self._restart_at is not None:
            log_event("auto_update", "Перезапуск отложен: " + (waiting or "обновление не требуется"), "info")
        self._window_since = None
        self._restart_at = None
        self._cancelled = False
        self._waiting = waiting

    def _launch(self, staged: str) -> None:
        self._restart_at = None
        self._cancelled = True                          # один запуск на заход в окно: не вышло — не долбим
        state = _read_state(self._state_file)
        attempts = _read_attempts(self._state_file)
        attempts[staged] = attempts.get(staged, 0) + 1
        state["attempts"] = attempts
        # что работало до перезапуска: по этому снимку после старта проверяем, что всё вернулось (update_health)
        try:
            pre = update_health.capture(self._sources())
        except Exception as e:                          # без снимка проверка после старта пойдёт по абсолютным признакам
            pre = None
            log_event("auto_update", "Не удалось снять состояние до перезапуска: %s" % e, "warn")
        state["pending"] = {"from": read_version(), "to": staged, "t": self._wall(), "pre": pre}
        _write_state(self._state_file, state)
        comps = ["main"] + [c["key"] for c in updater.components_info()["components"] if c["key"] != "main" and c.get("installed")]
        r = updater.run_update_script(APP_PORT, staged=True, components=",".join(comps), no_browser=True)
        if r.get("ok"):
            self._launched = True
            self._waiting = "перезапускаюсь"
            log_event("auto_update", "Автоперезапуск на v%s запущен (попытка %d)" % (staged, attempts[staged]), "warn", {"components": comps})
        else:
            state.pop("pending", None)                  # запуска не было — итога ждать нечего
            _write_state(self._state_file, state)
            self._waiting = "не запустилось: %s" % r.get("error")
            log_event("auto_update", "Автоперезапуск не запустился: %s" % r.get("error"), "error")

    # ---- после перезапуска: итог обновления и проверка «всё поднялось»
    def _sources(self) -> dict:
        if self._health_src is None:
            self._health_src = update_health.default_sources()
        return self._health_src

    def _notify(self, state: str, level: str, text: str, data: dict) -> None:
        """Сообщение об обновлении: в журнал приложения и на приёмник (Telegram: «скачана» и «обновлено» тихо, «не всё поднялось» со звуком)."""
        log_event("auto_update", text, "warn" if state in ("warn", "failed") else "info", {"state": state, **data})
        try:
            self._report_fn("update", level, text, {"state": state, **data})
        except Exception as e:
            log_event("auto_update", "Не удалось отправить сообщение об обновлении: %s" % e, "warn")

    def resume(self) -> None:
        """Вызывается при старте приложения. Метка «обновляюсь» с прошлого запуска: версия сменилась — идёт проверка «всё поднялось»;
        осталась прежней — обновление не удалось (откат). Обновили кнопкой (метки нет, но старт «app_update») — проверка без снимка «до»."""
        if not self._frozen_fn():
            return
        state = _read_state(self._state_file)
        pending = state.pop("pending", None)
        current = read_version()
        now = self._clock()
        if isinstance(pending, dict) and self._wall() - float(pending.get("t") or 0) <= PENDING_MAX_S:
            _write_state(self._state_file, state)
            if pending.get("to") == current:
                self._start_health(now, pending.get("from") or "", current, pending.get("pre") if isinstance(pending.get("pre"), dict) else None)
            elif pending.get("from") == current:
                self._notify("failed", "info", "Обновление до v%s не удалось, работаю на v%s" % (pending.get("to"), current),
                             {"from": pending.get("from"), "to": pending.get("to")})
            return
        if pending is not None:
            _write_state(self._state_file, state)
        if self._start_reason_fn() == "app_update":
            self._start_health(now, "", current, None)

    def _start_health(self, now: float, frm: str, to: str, pre: Optional[dict]) -> None:
        self._health = {"pre": pre, "from": frm, "to": to, "next": now + HEALTH_FIRST_S, "deadline": now + HEALTH_MAX_S}

    def _health_step(self, now: float) -> None:
        h = self._health
        if h is None or now < h["next"]:
            return
        try:
            res = update_health.evaluate(self._sources(), h["pre"])
        except Exception as e:
            res = {"ok": [], "bad": ["проверка не выполнилась: %s" % e], "done": False}
        final = now >= h["deadline"]
        if not res["done"] and not final:
            h["next"] = now + HEALTH_RETRY_S           # что-то ещё поднимается — смотрим снова
            return
        self._health = None
        state, text = update_health.verdict_text(h["from"], h["to"], res, final)
        self._notify(state, "info" if state == "ok" else "medium", text,
                     {"from": h["from"], "to": h["to"], "ok": res["ok"], "bad": res["bad"]})

    # ---- снаружи
    def cancel(self) -> dict:
        """«Отмена» на плашке: перезапуск гасится до следующего захода в окно (пропарка следующей варки)."""
        if self._restart_at is not None:
            self._restart_at = None
            self._cancelled = True
            self._waiting = "отменено — до следующей пропарки"
            log_event("auto_update", "Автоперезапуск отменён оператором", "info")
        return self.state()

    def state(self) -> dict:
        """Снимок для /api/update/state — из кэша, без сети и без записи в журнал."""
        with self._lock:
            info = dict(self._info)
        ra = self._restart_at
        left = None if ra is None else max(0, math.ceil(ra - self._clock()))
        cfg = self._cfg
        return {"current": read_version(), **info,
                "auto": {"enabled": bool(cfg["enabled"]), "restart": bool(cfg["auto_restart"]), "interval_min": cfg["interval_min"],
                         "restart_in_s": left, "launched": self._launched, "waiting_for": self._waiting}}


# ------------------------------------------------------------------------------------------------------------------ синглтон

_agent: Optional[AutoUpdater] = None


def start(window_fn: Callable[[], tuple] = None) -> AutoUpdater:
    """Запустить поток (один на процесс). Вызывается из lifespan приложения; window_fn — micro.restart_window."""
    global _agent
    if _agent is None:
        _agent = AutoUpdater(window_fn=window_fn)
        _agent.start()
    return _agent


def stop() -> None:
    if _agent is not None:
        _agent.stop()


def agent() -> Optional[AutoUpdater]:
    return _agent
