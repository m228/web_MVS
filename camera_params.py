"""Сохранённые параметры камеры микроскопа (plate_config.camera_params).

Камера держит экспозицию и усиление только в оперативной памяти: после отключения питания она
грузится из своего UserSet, а там другие значения (так вышла экспозиция 5000 вместо 170 и белые
кадры варки). Раньше web_MVS их нигде не хранил — страница читала текущее значение с камеры и
отправляла его же обратно. Теперь: по кнопке «Применить» фактический конфиг камеры (то, что она
приняла, включая усиление) сохраняется сюда, и при КАЖДОМ открытии потока пишется в камеру.
Автоподключение, сторож и перезапуски этих значений не меняют.
"""
import plate_config
from logger import log_event

# что храним и пишем в камеру
KEYS = ("pixel_format", "width", "height", "offset_x", "offset_y", "fps",
        "exposure_auto", "exposure_time", "gain_auto", "gain")
# что сверяем обратным чтением (fps камера сама урезает по каналу — его не сверяем)
VERIFY = ("pixel_format", "width", "height", "offset_x", "offset_y",
          "exposure_auto", "exposure_time", "gain_auto", "gain")
APPLY_TRIES = 3          # запись + обратное чтение: столько попыток, потом ошибка в журнал


def _filled(d):
    return {k: d.get(k) for k in KEYS if d.get(k) not in (None, "")}


def is_microscope(serial):
    """Это камера микроскопа (serial == camera_serial)?"""
    return bool(serial) and serial == (plate_config.load().get("camera_serial") or "").strip()


def saved(serial):
    """Сохранённые параметры для камеры микроскопа (serial == camera_serial) или None."""
    if not serial:
        return None
    cfg = plate_config.load()
    if serial != (cfg.get("camera_serial") or "").strip():
        return None
    p = cfg.get("camera_params") or {}
    if p.get("serial") != serial:            # камеру заменили — чужие значения не пишем
        return None
    return _filled(p) or None


def resolve(serial, requested, save=False):
    """Что писать в камеру. save (нажали «Применить») — то, что прислала страница.
    Иначе сохранённое главнее присланного: автоподключение после перезапуска может прислать
    значения, прочитанные с камеры, которая загрузилась со своими. Недостающее — из запроса."""
    req = _filled(requested or {})
    if save:
        return req
    s = saved(serial)
    return {**req, **s} if s else req


def store(serial, actual):
    """Запомнить фактический конфиг камеры (прочитан с неё после применения)."""
    vals = _filled(actual or {})
    if not serial or not vals:
        return None
    plate_config.save({"camera_params": {"serial": serial, **vals}})
    log_event("camera_params", "Параметры камеры сохранены (кнопка «Применить»)", "info",
              {"serial_number": serial, **vals})
    return vals


def _is_manual(mode):
    return str(mode or "Off").lower() == "off"


def mismatches(expected, actual):
    """{ключ: [ждали, стоит]} для значений, которые камера не приняла. Выдержку и усиление
    сверяем только в ручном режиме (в авто их ставит сама камера)."""
    bad = {}
    for k in VERIFY:
        e = expected.get(k)
        if e in (None, ""):
            continue
        if k == "exposure_time" and not _is_manual(expected.get("exposure_auto")):
            continue
        if k == "gain" and not _is_manual(expected.get("gain_auto")):
            continue
        a = actual.get(k)
        try:
            if a is None:
                ok = False
            elif k == "exposure_time":       # камера округляет выдержку до своего шага
                ok = abs(float(a) - float(e)) <= max(1.0, abs(float(e)) * 0.02)
            elif k == "gain":
                ok = abs(float(a) - float(e)) <= max(0.05, abs(float(e)) * 0.02)
            elif k in ("pixel_format", "exposure_auto", "gain_auto"):
                ok = str(a).lower() == str(e).lower()
            else:
                ok = int(a) == int(e)
        except (TypeError, ValueError):
            ok = False
        if not ok:
            bad[k] = [e, a]
    return bad


# пересвет кадра пробы: белое поле, видны только силуэты кристаллов (экспозиция не та).
# Обычные кадры Кира: медиана яркости 74–118, насыщенных пикселей 0 % (200 кадров, 2026-10-16)
OVER_MEDIAN = 230          # медиана яркости (0–255) от этого — пересвет
OVER_SAT_SHARE = 0.5       # или от этой доли пикселей ≥ 250


def overexposure(img):
    """(пересвечен ли кадр, медиана яркости, доля насыщенных пикселей). Считается по прореженному
    серому кадру — быстро и для 5 МП."""
    import cv2
    import numpy as np
    g = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    step = max(1, min(g.shape[:2]) // 256)
    g = g[::step, ::step]
    med = float(np.median(g))
    sat = float((g >= 250).mean())
    return (med >= OVER_MEDIAN or sat >= OVER_SAT_SHARE), med, sat


# чёрный кадр: свет не горит или не доходит (плата зависла, 12V упало, шторка) — шума и картинки нет совсем.
# Рабочие кадры: медиана 74–118, 99-й процентиль выше 100; порог взят с большим запасом, чтобы тусклый, но живой кадр не пугал.
BLACK_MEDIAN = 8           # медиана яркости (0–255) не выше этого
BLACK_P99 = 40             # и 99-й процентиль не выше этого — тогда кадр чёрный
BLACK_SECONDS = 120        # столько непрерывно чёрного живого потока — тревога (решение Макса 03.11)
BLACK_SAMPLE_S = 1.0       # живой поток проверяем раз в секунду
BLACK_GAP_S = 10.0         # кадров не было дольше этого — отсчёт чёрного начинается заново


def blackness(img):
    """(чёрный ли кадр, медиана яркости, 99-й процентиль). Считается по прореженному серому кадру, как overexposure."""
    import cv2
    import numpy as np
    g = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    step = max(1, min(g.shape[:2]) // 256)
    g = g[::step, ::step]
    med = float(np.median(g))
    p99 = float(np.percentile(g, 99))
    return (med <= BLACK_MEDIAN and p99 <= BLACK_P99), med, p99


class BlackWatch:
    """Сколько секунд подряд живой поток чёрный. Кормим кадрами из потока; сам проверяет раз в BLACK_SAMPLE_S."""

    def __init__(self, clock=None):
        import time
        self._clock = clock or time.time
        self.since = None          # с какого момента кадры чёрные подряд
        self.last = None           # когда был последний проверенный кадр
        self.med = self.p99 = None

    def feed(self, img):
        now = self._clock()
        if self.last is not None and now - self.last < BLACK_SAMPLE_S:
            return
        if self.last is not None and now - self.last > BLACK_GAP_S:
            self.since = None      # поток прерывался: чёрное до паузы не считаем
        self.last = now
        black, self.med, self.p99 = blackness(img)
        if not black:
            self.since = None
        elif self.since is None:
            self.since = now

    def seconds(self):
        """Секунд непрерывно чёрного потока; 0 — кадр живой или поток молчит."""
        if self.since is None or self.last is None or self._clock() - self.last > BLACK_GAP_S:
            return 0.0
        return self._clock() - self.since

    def live(self):
        """Поток идёт и последний проверенный кадр не чёрный."""
        return self.since is None and self.last is not None and self._clock() - self.last <= BLACK_GAP_S


def black_alarm(secs, streaming, led_on, led_bright, exposure):
    """Чёрный кадр — повод для тревоги: поток идёт, чёрный не меньше BLACK_SECONDS, а свет включён и экспозиция не нулевая
    (иначе чернота ожидаема). exposure None — не знаем, не мешает."""
    if not (streaming and secs >= BLACK_SECONDS):
        return False
    if not led_on or not (led_bright or 0) > 0:
        return False
    return exposure is None or exposure > 0
