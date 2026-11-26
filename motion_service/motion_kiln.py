"""kiln — этапы загрузки известковой печи по зонам камеры (чистая логика: на входе «движется» и «вид окна», на выходе этап).

Цикл идёт строго по кругу:
    ожидание сброса → сброс → ожидание засыпки → засыпка → ожидание прокрутки → прокрутка → ожидание сброса …

Как узнаём этапы:
    сброс      движение в зонах, после которого зона потемнела (камни ушли) относительно того, что было ДО движения.
               Сравнение с «до», а не с постоянным порогом: солнце и тень за день меняют яркость сильнее, чем разница «пусто / полно»;
    засыпка    следующее движение после сброса;
    прокрутка  следующее движение после засыпки, а если оно не прерывалось дольше load_max_s — его продолжение.
Движение без потемнения (свет, человек, бункер рядом) сбросом не считается. Этап, затянувшийся дольше своего предела, — сбой
порядка (пропустили событие, сервис запустили посреди цикла): возвращаемся к ожиданию сброса и встаём в цикл со следующего.
"""
from __future__ import annotations

import logging
from collections import deque

log = logging.getLogger("motion.kiln")

STAGES = ("wait_drop", "drop", "wait_load", "load", "wait_rot", "rot")
STAGE_NAMES = {"wait_drop": "ожидание сброса", "drop": "сброс", "wait_load": "ожидание засыпки", "load": "засыпка",
               "wait_rot": "ожидание прокрутки", "rot": "прокрутка"}
DEFAULT_CODES = {"wait_drop": 0, "drop": 1, "wait_load": 0, "load": 2, "wait_rot": 0, "rot": 3}
BASE_S = 10.0          # «до движения» — медиана вида окна за столько секунд покоя
CONFIRM_S = 15.0       # вид окна сглажен и отстаёт: столько секунд после конца движения ещё ждём потемнения
SHOW_MIN_S = 2.0       # сброс и прокрутку держим не меньше этого: ПЛК и сценарий (раз в секунду) должны их увидеть
GAP_S = 8.0            # кадров не было дольше (камера застыла, переоткрывалась): вид окна «до» и «после» разрыва не сравниваем
DROP_MAX_S = 20.0      # движение сброса дольше этого — это уже засыпка пошла без паузы
WAIT_LOAD_MAX_S = 120.0
ROT_MAX_S = 180.0


class KilnRT:
    def __init__(self, drop_pct: float, load_max_s: float, rot_wait_s: float, codes: dict, name: str = ""):
        self.name = name
        self.drop = drop_pct / 100.0
        self.load_max_s, self.rot_wait_s = load_max_s, rot_wait_s
        self.codes = dict(codes)
        self.reset()

    def reset(self):
        """Камеры не было / кадры с нуля: где мы в цикле, неизвестно — ждём сброс."""
        self.stage = "wait_drop"
        self.since = None               # когда начался этап
        self.quiet: deque = deque()     # (время, {зона: вид окна}) — кадры покоя за последние BASE_S
        self.cand = None                # движение, которое может оказаться сбросом: {"t0", "end", "base"}
        self.moved = False              # было ли движение на прошлом кадре
        self.last = None                # время прошлого кадра
        self.cycles = 0                # сколько сбросов увидели с запуска
        self.base: dict = {}            # вид окна до последнего сброса
        self.dark = 0.0                 # на сколько всего потемнело при последнем сбросе, % — по нему подбирают порог

    def _go(self, stage: str, now: float):
        log.info("%s: %s -> %s (через %.0f с, потемнение %.0f %%)", self.name or "печь", STAGE_NAMES[self.stage], STAGE_NAMES[stage],
                 now - (self.since if self.since is not None else now), self.dark)
        self.stage, self.since, self.cand = stage, now, None

    def _darkening(self, look: dict, base: dict) -> float:
        return max((1.0 - look[z] / base[z] for z in look if base.get(z, 0) > 0), default=0.0)

    def step(self, now: float, moving: bool, look: dict) -> int:
        """Кадр: moving — движется хоть одна зона; look — {зона: значение вида окна}. Возвращает код этапа для ПЛК."""
        if self.since is None:
            self.since = now
        if self.last is not None and now - self.last > GAP_S:
            self.cand, self.moved = None, False
            self.quiet.clear()
        self.last = now
        start = moving and not self.moved
        if self.stage == "wait_drop":
            self._watch_drop(now, moving, start, look)
        elif self.stage == "drop":
            if now - self.since >= SHOW_MIN_S and not moving:
                self._go("wait_load", now)
            elif moving and now - self.since >= DROP_MAX_S:
                self._go("load", now)
        elif self.stage == "wait_load":
            if start:
                self._go("load", now)
            elif now - self.since > WAIT_LOAD_MAX_S:
                self._go("wait_drop", now)
        elif self.stage == "load":
            if not moving:
                self._go("wait_rot", now)
            elif now - self.since >= self.load_max_s:
                self._go("rot", now)
        elif self.stage == "wait_rot":
            if moving:
                self._go("rot", now)
            elif now - self.since > self.rot_wait_s:
                self._go("wait_drop", now)
        elif (not moving and now - self.since >= SHOW_MIN_S) or now - self.since > ROT_MAX_S:       # rot
            self._go("wait_drop", now)
        if self.stage in ("drop", "wait_load"):                     # вид окна сглажен: воронка темнеет ещё и после подтверждения
            self.dark = max(self.dark, round(self._darkening(look, self.base) * 100.0, 1))
        if not moving and look:
            self.quiet.append((now, dict(look)))
        while self.quiet and now - self.quiet[0][0] > BASE_S + CONFIRM_S + DROP_MAX_S:
            self.quiet.popleft()
        self.moved = moving
        return self.codes[self.stage]

    def _watch_drop(self, now: float, moving: bool, start: bool, look: dict):
        if start and self.cand is None:
            last = self.quiet[-1][0] if self.quiet else None
            frames = [v for t, v in self.quiet if last - t <= BASE_S] if self.quiet else [look]
            base = {z: sorted(f[z] for f in frames if z in f)[len([f for f in frames if z in f]) // 2]
                    for z in look if any(z in f for f in frames)}
            self.cand = {"t0": now, "end": None, "base": base}
        c = self.cand
        if c is None:
            return
        if moving:
            c["end"] = None
        elif c["end"] is None:
            c["end"] = now
        dark = self._darkening(look, c["base"])
        if dark >= self.drop:
            self.cycles += 1
            self.base, self.dark = c["base"], round(dark * 100.0, 1)
            self._go("drop", now)
        elif (c["end"] is not None and now - c["end"] > CONFIRM_S) or (moving and now - c["t0"] > DROP_MAX_S + CONFIRM_S):
            self.cand = None                    # не потемнело — это был не сброс

    def snapshot(self, now: float) -> dict:
        return {"stage": self.stage, "stage_name": STAGE_NAMES[self.stage], "value": self.codes[self.stage],
                "since_s": 0 if self.since is None else int(max(0.0, now - self.since)), "cycles": self.cycles, "dark": self.dark}
