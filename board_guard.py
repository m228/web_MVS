"""Защита от чёрного кадра: поток камеры микроскопа ровно чёрный BLACK_SECONDS при включённом свете и ненулевой
экспозиции -> ОДИН перезапуск платы (решение Макса 03.11: в его случае плата зависала, одного перезапуска хватило).

Не поднялась или не приняла команду -> результат уходит в sysmon (тревога high, Telegram сразу). Кадр остался чёрным -> его же
тревога «чёрный кадр» с пометкой, что плату уже перезапускали. Второй раз плату не трогаем, пока не появится живой кадр;
между перезапусками не меньше COOLDOWN_S.

Источники (src) — функции без аргументов; тесты подменяют их и часы.
  camera() -> {serial, streaming, current, black_s, live}   state() -> состояние автомата
  status() -> статус связи PlateClient   ext() -> доп. телеметрия платы   reboot() -> послать команду
  light() -> {} | {что расходилось}: сверить яркость/частоту LED в плате с сохранёнными и, если разошлись, записать заново

Порядок при чёрном кадре: сначала проверка света (после «Сохранить»/перезапуска частота или яркость в плате могли слететь,
а на странице всё как было) — расходятся -> пишем сохранённые заново и ждём LIGHT_WAIT_S; кадр остался чёрным — тогда ребут платы.
"""
import threading
import time

import camera_params
from logger import log_event

TICK_S = 5.0
COOLDOWN_S = 1800.0       # между перезапусками платы минимум столько
DOWN_WAIT_S = 15.0        # после команды плата должна пропасть за это время, иначе команду не приняла
RETURN_WAIT_S = 45.0      # и вернуться за это время (на деле ~5 с)
LIGHT_WAIT_S = 20.0       # после повторной записи света ждём живой кадр столько, потом уже ребут платы
POLL_S = 0.5

RESULT_TEXT = {
    "returned": "плата вернулась через %d с",
    "no_return": "плата не поднялась за %d с",
    "no_reboot": "плата не перезапустилась по команде за %d с",
}


class BoardGuard:
    def __init__(self, src, clock=time.time, sleep=time.sleep):
        self.src = src
        self.clock = clock
        self.sleep = sleep
        self.armed = True            # после перезапуска false, пока не появится живой кадр
        self.last = None             # {"t", "result", "took_s", "text"} — последний перезапуск
        self.light = None            # {"t", "diff", "text"} — последний раз, когда свет в плате разошёлся с сохранённым
        self.light_at = None         # когда при этом чёрном кадре уже переписывали свет (None — ещё нет)
        self.busy = False
        self._stop = threading.Event()
        self._thread = None

    # ---- фон
    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="board-guard", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(TICK_S):
            try:
                self.tick()
            except Exception as e:                      # защита не должна падать сама
                log_event("board_guard", "Ошибка защиты платы: %s" % e, "warn")

    # ---- решение
    def _blocked(self):
        """Почему сейчас нельзя трогать плату ('' — можно): идёт шаг автомата (подвод, проба, отвод), калибровка нуля,
        или плата и так не отвечает (с ней разбирается тревога «плата не отвечает»)."""
        st = self.src["state"]() or {}
        if st.get("mode"):
            return "идёт шаг автомата"
        if (st.get("autocal") or {}).get("active"):
            return "идёт калибровка нуля"
        if (self.src["status"]() or {}).get("connected") is False:
            return "плата не отвечает"
        return ""

    def tick(self):
        """Один проход. Возвращает результат перезапуска или None (ничего не делали)."""
        cam = self.src["camera"]() or {}
        if cam.get("live"):
            self.armed = True
            self.light_at = None
        if self.last and self.last["result"] == "no_return" and (self.src["status"]() or {}).get("connected"):
            self.last.update(result="late_return", text="плата вернулась только позже")   # тревога «не поднялась» снимается
        if not self.armed or self.busy:
            return None
        st = self.src["state"]() or {}
        if not camera_params.black_alarm(cam.get("black_s") or 0, cam.get("streaming"), st.get("led_on"), st.get("led_bright"),
                                         (cam.get("current") or {}).get("exposure_time")):
            return None
        now = self.clock()
        if self.last and now - self.last["t"] < COOLDOWN_S:
            return None
        if self._blocked():
            return None
        chk = self._light_check(now)
        if chk:
            return "light" if chk == "fixed" else None       # "wait" — ждём живой кадр после повторной записи света
        return self.reboot(now, cam.get("black_s"))

    def _light_check(self, now):
        """"fixed" — свет только что переписали, "wait" — ждём живой кадр (плату пока не трогаем). False — ребут."""
        fn = self.src.get("light")
        if fn is None:
            return False
        if self.light_at is not None:
            return "wait" if now - self.light_at < LIGHT_WAIT_S else False
        try:
            diff = fn() or {}
        except Exception as e:
            log_event("board_guard", "Не удалось сверить свет с платой: %s" % e, "warn")
            return False
        if not diff:
            return False
        text = ", ".join("%s: должно %s, в плате %s" % (k, v[0], v[1]) for k, v in sorted(diff.items()))
        self.light_at = now
        self.light = {"t": now, "diff": diff, "text": text}
        log_event("board_guard", "Кадр чёрный, свет в плате не тот (%s) — записал сохранённый заново" % text, "warn", {"diff": diff})
        return "fixed"

    def reboot(self, now, black_s=None):
        self.busy = True
        self.armed = False
        try:
            up0 = self._uptime()
            log_event("board_guard", "Кадр чёрный %s с при включённом свете — перезапускаю плату (одна попытка)" % int(black_s or 0),
                      "warn", {"black_s": black_s})
            self.src["reboot"]()
            result, took = self._wait_back(now, up0)
            text = RESULT_TEXT[result] % took
            self.last = {"t": now, "result": result, "took_s": took, "text": text}
            log_event("board_guard", "Перезапуск платы: " + text, "success" if result == "returned" else "error", {"result": result})
            return result
        finally:
            self.busy = False

    def _uptime(self):
        v = (self.src["ext"]() or {}).get("uptime_s")
        return float(v) if isinstance(v, (int, float)) else None

    def _wait_back(self, t0, up0):
        """Ждём: плата пропала (связь или аптайм сбросился), потом ответила. -> (результат, секунд)."""
        down_at = None
        while True:
            now = self.clock()
            took = int(round(now - t0))
            st = self.src["status"]() or {}
            ok_t = st.get("last_ok")
            fresh = bool(st.get("connected")) and ok_t is not None and now - ok_t < 1.0
            up = self._uptime()
            if up0 is not None and up is not None and up + 10 < up0 and fresh:
                return "returned", took                  # аптайм обнулился — перезапустилась и уже отвечает
            if down_at is None and not fresh:
                down_at = now
            elif down_at is not None and fresh and ok_t > down_at:
                return "returned", took
            if down_at is None and now - t0 > DOWN_WAIT_S:
                return "no_reboot", took
            if now - t0 > RETURN_WAIT_S:
                return "no_return", took
            self.sleep(POLL_S)
