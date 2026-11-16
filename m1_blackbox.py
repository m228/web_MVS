"""«Чёрный ящик» мотора М1: короткая запись регистров вокруг подозрительных моментов.

Зачем: на варке голова иногда «уходит в ноль» (к стеклу). Разбор прошивки показал несколько
возможных причин (цель 1253 = 0 после перезагрузки платы, нулевой максимум хода, перезапись
расчётной по сырому датчику на ходу, висящий флаг поиска нуля). Различить их можно только по
значениям регистров в самый момент события — их и пишем.

Работает ТОЛЬКО на чтение: ничего в плату не пишет, в такт опроса почти ничего не стоит и
никогда не бросает исключений наружу. Файл — m1_blackbox.log в каталоге данных (рядом с
approach_fails.log), больше ~1 МБ -> переименовывается в .old.

Все значения — СЫРЫЕ слова регистров (1 ед. = 10 мкм для 1253/1271/1274/1285), как в плате.
"""
import collections
import time
from datetime import datetime

PRE_SAMPLES = 40        # сколько тактов ДО события сохраняем (≈4 с при такте 100 мс)
POST_SAMPLES = 30       # сколько тактов ПОСЛЕ события дописываем
COOLDOWN_SEC = 20.0     # не чаще одного события в 20 с (одна и та же авария не должна заливать файл)
MAX_BYTES = 1_000_000

JUMP_CALC = 30          # скачок расчётной 1274 за такт, ед. (= 300 мкм)
STILL_ENC = 10          # энкодер 1285 при этом почти не сдвинулся, ед. (= 100 мкм)
GLASS_POS = 5           # расчётная у стекла, ед. (= 50 мкм)
FAR_TARGET = 30         # цель ПК, которая «далеко» от стекла, ед. (= 300 мкм)
GLASS_HOLD_TICKS = 30   # «у стекла при далёкой цели» должно держаться столько тактов подряд (≈3 с)
EXPECT_QUIET_SEC = 60.0  # после команды «Поиск 0/Ноль/Дом» скачок расчётной — штатный, не событие

_HEADER = "t\tcalc1274\tenc1285\tsens1271\tpc1253\tboard1253\tmaxtravel1231\tlocks1535\tcalc_steps1280"


def _s16(v):
    v = int(v)
    return v - 0x10000 if v >= 0x8000 else v


class M1BlackBox:
    def __init__(self, path, on_event=None):
        self._path = path
        self._on_event = on_event       # on_event(reason) — сообщить наружу (мониторинг); ошибки колбэка глотаем
        self._ring = collections.deque(maxlen=PRE_SAMPLES)
        self._prev = None
        self._post_left = 0
        self._last_event = 0.0
        self._glass_armed = True        # событие «у стекла при далёкой цели» — раз на заход
        self._alert = False             # последний найденный _detect повод — тревога (True) или только журнал (False)
        self._glass_ticks = 0           # сколько тактов подряд голова у стекла при далёкой цели
        self._jump_quiet_until = 0.0    # до этого момента скачок расчётной не считаем событием
        self._board_zero_armed = True   # событие «плата потеряла цель» — раз на потерю

    def feed(self, raw, ext, pc_target):
        """raw — сырой блок IN (1270..1285), ext — снимок ext-регистров (по именам ext_map),
        pc_target — что ПК записал в OUT[3] (1253). Возвращает None; ошибки глотает."""
        try:
            self._feed(raw, ext or {}, pc_target)
        except Exception:
            pass

    def expect_glass(self):
        """ПК сам послал мотору 1 «Поиск 0 / Ноль / Дом»: голова законно едет к стеклу (в 1253 при этом
        лежит прежняя цель, например отвод), а расчётная законно скачет. Не считаем это уходом в ноль:
        тревога C молчит, пока голова не отойдёт от стекла (штатный перевзвод при calc > 20)."""
        try:
            self._glass_armed = False
            self._glass_ticks = 0
            self._jump_quiet_until = time.time() + EXPECT_QUIET_SEC
        except Exception:
            pass

    def _feed(self, raw, ext, pc_target):
        if raw is None or len(raw) < 16:
            return
        now = time.time()
        row = (
            now,
            _s16(raw[4]),                       # 1274 расчётная
            raw[15],                            # 1285 энкодер (без знака, как в плате)
            _s16(raw[1]),                       # 1271 датчик (сырой)
            None if pc_target is None else int(pc_target) & 0xFFFF,
            ext.get("m1_sp_board_raw"),         # 1253 как её видит плата
            ext.get("m1_maxtravel"),            # 1231
            ext.get("locks"),                   # 1535
            ext.get("m1_steps"),                # 1280
        )
        self._ring.append(row)
        if self._post_left > 0:
            self._post_left -= 1
            self._write_rows([row])

        reason = self._detect(row)
        alert = self._alert
        self._prev = row
        # событие только для журнала (alert=False) охлаждение не расходует: оно раз на потерю цели (_board_zero_armed)
        if reason and (not alert or now - self._last_event >= COOLDOWN_SEC):
            if alert:
                self._last_event = now
            self._post_left = POST_SAMPLES
            self._write_event(reason, alert)

    def _detect(self, row):
        calc, enc, pc, board = row[1], row[2], row[4], row[5]
        prev = self._prev
        reason = None
        board_reason = None
        self._alert = False
        # A. расчётная скакнула, а энкодер на месте — её переписала прошивка (датчик/ноль), а не ход
        if (prev is not None and time.time() >= self._jump_quiet_until
                and abs(calc - prev[1]) >= JUMP_CALC and abs(enc - prev[2]) < STILL_ENC):
            reason = "скачок расчётной %s -> %s при неподвижном энкодере" % (prev[1], calc)
        # B. ПК записал цель, а плата её не помнит (после перезагрузки 1253 = 0 -> повтор goto уйдёт к стеклу)
        if board is not None and pc:
            if board == 0 and self._board_zero_armed:
                self._board_zero_armed = False
                # Только в журнал: 1253 пишут двое (write_m1_sp и motor_goto), «Поиск 0» прошивка обнуляет цель сама,
                # так что «плата 0, а в буфере ПК цель» — не поломка. Вред ловят A и C (голова реально ушла).
                board_reason = "плата видит цель 1253 = 0, а в буфере ПК %s (только журнал, без тревоги)" % pc
            elif board != 0:
                self._board_zero_armed = True
        # C. голова у стекла, хотя цель ПК далеко — именно «ушла в ноль»
        if pc is not None and pc >= FAR_TARGET and calc <= GLASS_POS:
            self._glass_ticks += 1
            if self._glass_armed and self._glass_ticks >= GLASS_HOLD_TICKS:
                self._glass_armed = False
                reason = reason or "расчётная %s у стекла при цели ПК %s" % (calc, pc)
        else:
            self._glass_ticks = 0
            if calc > GLASS_POS * 4:
                self._glass_armed = True
        self._alert = reason is not None
        return reason or board_reason

    def _write_event(self, reason, alert=True):
        stamp = datetime.now().isoformat(timespec="seconds")
        lines = ["== %s\t%s" % (stamp, reason), _HEADER]
        lines.extend(self._fmt(r) for r in self._ring)
        self._append("\n".join(lines) + "\n")
        if alert and self._on_event is not None:
            try:
                self._on_event(reason)
            except Exception:
                pass

    def _write_rows(self, rows):
        self._append("\n".join(self._fmt(r) for r in rows) + "\n")

    @staticmethod
    def _fmt(r):
        return "\t".join([datetime.fromtimestamp(r[0]).strftime("%H:%M:%S.%f")[:-3]]
                         + ["" if v is None else str(v) for v in r[1:]])

    def _append(self, text):
        p = self._path
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            if p.exists() and p.stat().st_size > MAX_BYTES:
                old = p.with_suffix(".log.old")
                if old.exists():
                    old.unlink()
                p.rename(old)
        except OSError:
            pass
        with open(p, "a", encoding="utf-8") as f:
            f.write(text)
