"""camera — рабочий поток одной камеры: RTSP/файл → кадры → CameraEngine → состояние + запись в ПЛК.

Что делает сверх engine:
  • переподключение к потоку (пауза reconnect_delay_s), RTSP по tcp (убирает «bad cseq» и потери пакетов);
  • детектор зависшей камеры: картинка не меняется дольше freeze_timeout_s (или кадров нет столько же) → все биты и
    уровни = 0, status = 0 — в ПЛК уходят нули и «камера не жива», детекция сама оживает, когда кадры пойдут;
  • отдельный поток записи в ПЛК: недоступный контроллер не тормозит разбор кадров (запись — по изменению и раз в
    write_period_s);
  • сбой разбора кадра (исключение) пропускает кадр, а не роняет поток; нет свежего результата дольше порога (сбои подряд,
    поток завис или умер) — камера считается мёртвой: наружу и в ПЛК идёт то же, что при пропаже связи;
  • последний кадр для просмотра (snapshot), история метрик для графиков калибровки и запись в CSV.

Источник — rtsp://… или путь к видеофайлу (для проверки без камеры; файл крутится по кругу, темп — по FPS файла).
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np

import juice_base
from perf_stats import PERF
from motion_engine import GAUGE_TYPES, CameraEngine, TubeRT, scale_points
from motion_level import LevelInfer, nearest_trend_shot, post_infer, trend_shot_list
from motion_webview import WebView
from motion_store import store_for
from motion_trends import TrendLog
from motion_fill_trend import fill_trend_for
from motion_modbus import LinkRegistry

log = logging.getLogger("motion.camera")

HISTORY_LEN = 900                # точек истории на камеру (~5 мин при 3 fps)
LONG_STEP_S = 1.0                # длинная история: одна точка в секунду (максимум за секунду — всплеск не теряется)
LONG_LEN = 7200                  # ... на 2 часа назад
TIP_SEEN = 0.3                   # сила кончика (0..1), с которой отражатель конца считается видимым (как MV_TIP_SEEN в 10-motion.js)
STATS_STEP_S = 10                # статистика зон (среднее/мин/макс): корзина на 10 с ...
STATS_LEN = 8640                 # ... и сутки назад (8640 × 10 с)
_OPEN_LOCK = threading.Lock()    # OPENCV_FFMPEG_CAPTURE_OPTIONS — переменная окружения процесса, выставляем под замком


def _squeeze(rows: list, points: int, angle_ids=frozenset()) -> list:
    """[(ts, {зона: (flow, diff, bit)}, ok)] → не больше points строк: корзины по k штук, внутри максимум по зонам,
    ok — минимум (хоть один плохой кадр делает корзину «нет данных»), время — последней строки корзины.
    angle_ids — зоны-палки (flow = угол): угол максимумом не сжать (у вращающихся навстречу валов максимум берётся из разных
    моментов, сумма углов уезжала на десятки градусов), поэтому берём строку целиком — последнюю в корзине, оба вала из одного момента."""
    if len(rows) <= points:
        return rows
    k = -(-len(rows) // points)
    out = []
    for i in range(0, len(rows), k):
        chunk = rows[i:i + k]
        zones: dict = {}
        for _, z, _ok in chunk:
            for zid, (fl, df, bit) in z.items():
                old = zones.get(zid)
                if zid in angle_ids:
                    zones[zid] = (fl, df, bit)
                else:
                    zones[zid] = (fl, df, bit) if old is None else (max(old[0], fl), max(old[1], df), max(old[2], bit))
        out.append((chunk[-1][0], zones, min(r[2] for r in chunk)))
    return out


def is_stream_url(url: str) -> bool:
    return str(url).lower().startswith(("rtsp://", "rtsps://", "rtmp://", "http://", "https://"))


def garbage_ratio(gray: np.ndarray, bs: int = 16) -> float:
    """Доля блоков bs×bs, залитых ровным серым ~128: так декодер H.264 «стирает» кадр при потере пакетов (серая каша).
    Живой кадр (даже тёмный/засвеченный) такого не даёт: у него есть шум матрицы или яркость далеко от 128."""
    h, w = gray.shape[:2]
    h -= h % bs
    w -= w % bs
    if h < bs or w < bs:
        return 0.0
    b = gray[:h, :w].reshape(h // bs, bs, w // bs, bs).astype(np.float32).transpose(0, 2, 1, 3).reshape(-1, bs * bs)
    flat = (b.std(axis=1) < 1.5) & (np.abs(b.mean(axis=1) - 128.0) <= 10.0)
    return float(flat.mean())


def stream_url(url: str, substream: bool = False) -> str:
    """Адрес потока для анализа. substream — вместо основного потока взять дополнительный: у камер Dahua это subtype=1 вместо
    subtype=0 (картинка в разы меньше — декодер нагружается на порядок слабее). Адрес без subtype= не трогаем."""
    if substream and str(url).lower().startswith("rtsp") and re.search(r"subtype=\d+", url):
        return re.sub(r"subtype=\d+", "subtype=1", url, count=1)
    return url


def hw_params(hw_accel: str) -> list:
    """Параметры VideoCapture для аппаратного декодирования; [] — программное (или OpenCV без поддержки).
    CAP_PROP_HW_DEVICE не передаём: OpenCV отказывается открывать поток, если устройство указано без явного режима."""
    prop = getattr(cv2, "CAP_PROP_HW_ACCELERATION", None)
    mode = {"auto": getattr(cv2, "VIDEO_ACCELERATION_ANY", None), "d3d11": getattr(cv2, "VIDEO_ACCELERATION_D3D11", None)}.get(hw_accel)
    return [prop, mode] if prop is not None and mode is not None else []


def cfg_hw(cfg: dict) -> str:
    return cfg.get("hw_accel", "auto")


OPEN_GAP_S = 2.0
LAG_CATCHUP_S = 0.5              # очередь кадров длиннее — старые кадры выбрасываем (grab без обработки), пока не догоним
LAG_MAX_SKIP_S = 3.0             # но хоть один кадр в столько секунд обрабатываем: камера не должна выглядеть мёртвой, если декодер не догоняет
LAG_BASE_WIN_S = 600.0           # «нормальную» задержку потока ищем как минимум за столько секунд (уход часов камеры не копится)
LAG_BUCKET_S = 30.0              # минимум держим по корзинам такой длины
LAG_GOOD_N = 5                   # столько кадров подряд время потока должно расти, прежде чем ему верим (файл/заглушка его не дают)
LAG_JUMP_BACK_MS = 1000.0        # время потока ушло назад больше чем на это, или вперёд больше чем на LAG_JUMP_FWD_MS, — поток начался заново
LAG_JUMP_FWD_MS = 10000.0
LAG_MAX_WIN_S = 60.0             # «макс очереди» на панели — за последнюю минуту
PROC_TOLERANCE = 0.85           # кадр берём, если с прошлого прошло ≥ 85 % интервала proc_fps: джиттер камеры иначе «съедает» каждый второй (8 к/с и proc_fps 4 давали ≈3)
ROD_HIST_LEN = 600               # точек ряда на палку (≈ 3–5 минут при 2–3 к/с)
FREEZE_REOPEN_S = 30.0           # картинка стоит дольше (и не меньше 2·freeze_timeout_s) — поток открываем заново, потом повторяем так же
STALE_MIN_S = 5.0                # результат не бывает «старым» раньше этого (больше LAG_MAX_SKIP_S: догонялка очереди — не сбой)
STALE_REASON = "нет свежих результатов анализа"
FAIL_REASON = "сбой анализа кадра"
FAIL_LOG_S = 60.0                # повторяющийся сбой разбора кадра пишем в лог не чаще
_open_lock = threading.Lock()
_open_next = 0.0


def wait_open_turn(gap: Optional[float] = None, clock=time.monotonic, sleep=time.sleep) -> float:
    """Очередь на открытие потока: следующее открытие стартует не раньше чем через gap секунд после предыдущего.
    После перезапуска сервиса все камеры открывались разом (декодер, сеть, процессор) — теперь по одной. Держим лишь
    расписание, а не само открытие: мёртвая камера остальные не задерживает. -> сколько секунд ждали."""
    global _open_next
    gap = OPEN_GAP_S if gap is None else gap
    with _open_lock:
        now = clock()
        start = max(now, _open_next)
        _open_next = start + gap
    wait = start - now
    if wait > 0:
        sleep(wait)
    return max(wait, 0.0)


class QueueLag:
    """Отставание обработки от потока: на сколько секунд кадр, взятый сейчас, старше самого свежего, что уже был в этом потоке.

    Время потока кадра (CAP_PROP_POS_MSEC) идёт как часы камеры. Пока кадры берутся вовремя, «часы ПК − время потока» постоянно;
    очередь в сокете и декодере (обработка медленнее камеры, просадка процессора или диска) поднимает его на свою длину. Постоянную
    часть (задержку камеры и кодера) мы не знаем — берём минимум за окно: отставание = текущее − минимум. Минимум держим по корзинам
    (окно LAG_BASE_WIN_S), чтобы уход часов камеры на десятки ppm не копился. Нет времени потока (файл, заглушка: оно не растёт)
    или оно только что началось заново — None, и вызывающий ничего не пропускает и не считает."""

    def __init__(self, window_s: float = LAG_BASE_WIN_S, bucket_s: float = LAG_BUCKET_S):
        self.window_s, self.bucket_s = window_s, bucket_s
        self.reset()

    def reset(self) -> None:
        self._mins: deque = deque()            # [начало корзины (часы ПК), минимум «часы ПК − время потока»]
        self._last: Optional[float] = None     # время потока прошлого кадра, мс
        self._good = 0                         # сколько кадров подряд время потока росло
        self.lag: Optional[float] = None

    def update(self, wall: float, pos_ms: Optional[float]) -> Optional[float]:
        """wall — часы ПК в момент grab(), pos_ms — CAP_PROP_POS_MSEC после него. -> отставание, с (≥ 0) или None."""
        if pos_ms is None or pos_ms != pos_ms:
            self.lag = None
            return None
        pos_ms = float(pos_ms)
        last, self._last = self._last, pos_ms
        if last is None:
            self._good = 0
        elif pos_ms < last - LAG_JUMP_BACK_MS or pos_ms > last + LAG_JUMP_FWD_MS:
            self._mins.clear()                 # поток открылся заново / время потока перескочило: прежняя база не годится
            self._good = 0
        elif pos_ms > last:
            self._good = min(self._good + 1, LAG_GOOD_N)
        else:
            self._good = 0
        if self._good < LAG_GOOD_N:
            self.lag = None
            return None
        off = wall - pos_ms / 1000.0
        if self._mins and wall - self._mins[-1][0] < self.bucket_s:
            if off < self._mins[-1][1]:
                self._mins[-1][1] = off
        else:
            self._mins.append([wall, off])
        while self._mins and wall - self._mins[0][0] > self.window_s + self.bucket_s:
            self._mins.popleft()
        self.lag = max(0.0, off - min(m[1] for m in self._mins))
        return self.lag


def stream_pos_ms(cap) -> Optional[float]:
    """Время потока последнего кадра, мс (None — источник его не отдаёт)."""
    try:
        return float(cap.get(cv2.CAP_PROP_POS_MSEC))
    except Exception:
        return None


def decoder_kind(cap) -> str:
    """'gpu' — поток реально декодируется на видеоядре, 'cpu' — программно, '' — неизвестно (подмена в тестах)."""
    try:
        v = cap.get(cv2.CAP_PROP_HW_ACCELERATION)
    except Exception:
        return ""
    if v == 0:
        return "cpu"
    return "gpu" if 0 < v <= 5 else ""         # VIDEO_ACCELERATION_*: 1..5; иное (подмена) — неизвестно


def open_capture(url: str, transport: str = "tcp", hw_accel: str = "off"):
    """cv2.VideoCapture с таймаутами открытия/чтения (без них мёртвая камера вешает поток надолго).
    hw_accel: off — декодирует процессор; auto/d3d11 — видеоядро (Intel UHD и т.п.), нагрузка на процессор падает в разы."""
    with _OPEN_LOCK:
        if str(url).lower().startswith("rtsp") and transport in ("tcp", "udp"):
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;%s" % transport
        try:
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000,
                                                         cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000] + hw_params(hw_accel))
        except Exception:
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass
    return cap


class CameraWorker(threading.Thread):
    def __init__(self, cam_cfg: dict, modbus_cfg: dict, links: LinkRegistry, data_dir: Path,
                 capture_factory: Callable = open_capture, level_post: Optional[Callable] = None):
        super().__init__(daemon=True, name="cam-" + cam_cfg["id"])
        self.cfg = cam_cfg
        self.id = cam_cfg["id"]
        self.mb = cam_cfg["modbus"] or modbus_cfg
        self.links = links
        self.data_dir = Path(data_dir)
        self.capture_factory = capture_factory
        self.level_post = level_post or post_infer      # запрос к сервису уровня (в тестах подменяется)
        self.sound_states: Optional[Callable] = None    # (настройки камеры) -> записи групп «этапы по звуку»; задаёт менеджер
        self._hw_off = False                   # аппаратное декодирование не вышло — до остановки воркера декодируем процессором
        self._hw_fail = 0                      # сколько открытий подряд с аппаратным декодированием не дали ни одного кадра
        self._decoder = ""                     # 'gpu' | 'cpu' | '' — чем декодируется поток сейчас
        self.engine = CameraEngine(cam_cfg, self.data_dir / ("mirror_learn_%s.json" % re.sub(r"[^\w.-]", "_", str(cam_cfg["id"]))))
        self._halt = threading.Event()
        self._lock = threading.Lock()
        self._frame: Optional[np.ndarray] = None
        self._state: Optional[dict] = None
        self._pub_mono: Optional[float] = None    # когда (monotonic) поток камеры последний раз дал результат; None — анализ не запускался
        self._fail_n = 0                          # сколько раз разбор кадра упал с исключением
        self._fail_logged: Optional[float] = None
        self._connected = False
        self._conn_logged: Optional[bool] = None
        self._error = ""
        self._last_good = 0.0                 # когда последний раз пришёл кадр
        self._stamps: deque = deque(maxlen=12)
        self._qlag = QueueLag()                # отставание обработки от потока (очередь кадров), см. QueueLag
        self._lag_s: Optional[float] = None    # оно же на последнем обработанном кадре; None — время потока источник не отдаёт
        self._lag_hist: deque = deque()        # (время, отставание) обработанных кадров за последнюю минуту — «макс очереди» на панели
        self._skipped = 0                      # сколько кадров выброшено из очереди с запуска (догонялка)
        self._rod_hist: dict = {}                # зона-цикл → deque (время съёмки кадра, угол, valid, coast): ряд для сравнения валов по времени съёмки
        self._history: deque = deque(maxlen=HISTORY_LEN)
        self._long: deque = deque(maxlen=LONG_LEN)
        self._bucket: Optional[list] = None   # [ts, {зона: (flow, diff, bit)}, ok] — набирающаяся секунда длинной истории
        self._tipb: Optional[list] = None     # [секунда, {зона: [красный конец виден, синий виден]}] — набирающаяся секунда видимости отражателей
        self._stats: deque = deque(maxlen=STATS_LEN)   # корзины статистики: [номер 10-секундки, {зона: [n, Σflow, min, max, Σdiff, min, max]}]
        self._sbucket: Optional[list] = None  # набирающаяся корзина статистики
        # тренды признака «вид окна» (tex / lum / rel): только в памяти, живут через пересоздание камеры; см. motion_fill_trend
        self._fill = fill_trend_for((str(self.data_dir), self.id), {z["id"]: repr((z.get("points"), z["fill_metric"])) for z in cam_cfg["zones"]
                                                                    if z.get("fill_metric", "off") != "off"})
        # зоны-палки (flow — угол), прозрачность (flow — мутность) и уровнемеры (flow — %): значение не «максимум за секунду», а самое позднее хорошее
        self._angle_ids = frozenset(z["id"] for z in cam_cfg["zones"] if z["type"] in ("cycle", "clarity") + GAUGE_TYPES)
        self._mstats: deque = deque(maxlen=STATS_LEN)   # корзины статистики синхронности валов: [номер 10-секундки, {группа: [n, Σ delta, min, max, Σ поправка, min, max]}] (в памяти)
        self._mbucket: Optional[list] = None
        self._juice_last = 0.0                # база «Сок»: когда последний раз писали кадр и когда чистили старое
        self._juice_purged = 0.0
        self._writes: Optional[list] = None   # что должно лежать в ПЛК (последнее)
        self._writes_evt = threading.Event()
        self._store = store_for(self.data_dir)
        self._rec_run: Optional[int] = None   # идущий запуск записи в базе (data\motion.db)
        self._rec_name: Optional[str] = None
        self._rec_ids: list = []
        self._rec_fill: set = set()       # зоны записи с признаком вида окна: у них ещё четыре значения (tex, lum, rel, fill)
        self._rec_warned = False
        # тренды на диске (data\motion.db): переживают перезапуск; здесь же читаем их обратно, чтобы графики и статистика не обнулялись
        self._trend = TrendLog(self.data_dir, self.id)
        self._restore_trends()
        self._writer: Optional[threading.Thread] = None
        self.plc = {"enabled": bool(self.mb.get("enable")), "ok": None, "error": ""}
        self._web: Optional[WebView] = None   # картинка для SCADA и браузера (cam["webview"]); None — выключена

    def _open(self, url: str, cfg: dict):
        """Открыть поток: подпоток вместо основного (substream) и аппаратное декодирование (hw_accel). Не открылось с аппаратным —
        один раз пробуем программно: если так открылось, дальше этот воркер декодирует процессором (до перезапуска сервиса)."""
        url = stream_url(url, cfg.get("substream", False))
        if is_stream_url(url):
            wait_open_turn()                   # камеры открываются по одной, а не все сразу после перезапуска
        hw = "off" if self._hw_off else cfg.get("hw_accel", "auto")
        if hw == "off":
            return self.capture_factory(url, cfg["transport"])
        cap = self.capture_factory(url, cfg["transport"], hw)
        if not cap.isOpened():
            cap.release()
            cap = self.capture_factory(url, cfg["transport"])
            if cap.isOpened():
                self._hw_off = True
                log.warning("[%s] с аппаратным декодированием поток не открылся, а программно открылся — дальше декодирую процессором", self.id)
        return cap

    def _hw_giveup_check(self, got_frame: bool):
        """Поток с аппаратным декодированием оборвался, не дав ни одного кадра. Три раза подряд — видеоядро его не тянет, возвращаемся на процессор."""
        if self._hw_off or cfg_hw(self.cfg) == "off" or got_frame:
            return
        self._hw_fail += 1
        if self._hw_fail >= 3:
            self._hw_off = True
            log.warning("[%s] аппаратное декодирование не дало ни одного кадра за %d открытий — дальше декодирую процессором", self.id, self._hw_fail)

    # ─────────── внешний API ───────────
    def stop(self):
        self._halt.set()
        self._writes_evt.set()
        self.stop_record()
        with self._lock:
            self._trend.close()

    def _stale_after(self) -> float:
        """Сколько секунд без нового результата терпим: как без кадров (freeze_timeout_s), но не меньше времени, за которое
        исправный поток заведомо успевает дать результат (редкие кадры, набор буфера frame_gap)."""
        return max(self.cfg["freeze_timeout_s"], STALE_MIN_S, (self.cfg["frame_gap"] + 2) * self.engine.proc_interval)

    def _stale(self) -> bool:
        return self._pub_mono is not None and time.monotonic() - self._pub_mono > self._stale_after()

    def _current(self) -> tuple:
        """(состояние, записи в ПЛК, устарело) — единственное, что можно отдавать наружу. Поток камеры молчит дольше порога
        (завис, умер) — вместо застывшего последнего результата то же, что при пропаже связи: биты и status 0, уровнемеры
        держат значение свои «держать»."""
        with self._lock:
            st = dict(self._state) if self._state else None
            writes = list(self._writes) if self._writes is not None else None
            stale = self._stale()
        if not stale:
            return st, writes, False
        reason = st["reason"] if st and not st["alive"] else STALE_REASON      # уже мёртвая — причину не меняем
        st = self.engine.lost_state(time.time(), reason)
        return st, self.engine.compute_writes(st), True

    def state(self) -> dict:
        sound = self.sound_states(self.cfg) if self.sound_states else []   # этапы по звуку живут своим потоком, не от кадров
        st, _writes, stale = self._current()
        with self._lock:
            if sound:
                if st is None:
                    # reason пустой: мониторинг тогда покажет настоящую ошибку подключения камеры
                    st = {"ts": time.time(), "alive": False, "reason": "", "frame_wh": None, "zones": [], "groups": []}
                st["groups"] = list(st.get("groups") or []) + sound
            fps = 0.0
            if not stale and len(self._stamps) >= 2 and self._stamps[-1] > self._stamps[0]:
                fps = (len(self._stamps) - 1) / (self._stamps[-1] - self._stamps[0])
            error = self._error or (st["reason"] if stale else "")
            return {"id": self.id, "name": self.cfg["name"], "enabled": self.cfg["enabled"], "connected": self._connected,
                    "error": error, "decoder": self._decoder, "fps": round(fps, 2), "age_s": round(time.time() - self._last_good, 1) if self._last_good else None,
                    "lag_s": None if self._lag_s is None else round(self._lag_s, 2),
                    "recording": self._rec_name, "plc": dict(self.plc), "result": st,
                    "webview": self._web.status() if self._web else None}

    def rod_track(self, zone_id: str) -> list:
        """Ряд палки [(время прихода кадра, угол, valid, coast)] — копия, старые точки первыми."""
        with self._lock:
            return list(self._rod_hist.get(zone_id, ()))

    def frame_timing(self) -> dict:
        """Как идут обработанные кадры: частота, шаг (p50/p95, мс) и поправка задержки камеры — для панели «Время кадров»."""
        with self._lock:
            st = list(self._stamps)
            lag, lags, skipped = self._lag_s, [v for _, v in self._lag_hist], self._skipped
        out = {"fps": None, "dt_p50_ms": None, "dt_p95_ms": None, "latency_s": float(self.cfg.get("latency_s", 0.0)),
               "lag_s": None if lag is None else round(lag, 2), "lag_max_s": round(max(lags), 2) if lags else None, "skipped": skipped}
        if len(st) >= 3 and st[-1] > st[0]:
            dts = sorted(b - a for a, b in zip(st, st[1:]))
            out["fps"] = round((len(st) - 1) / (st[-1] - st[0]), 2)
            out["dt_p50_ms"] = round(dts[len(dts) // 2] * 1000.0)
            out["dt_p95_ms"] = round(dts[min(len(dts) - 1, int(round(0.95 * (len(dts) - 1))))] * 1000.0)
        return out

    def _web_source(self) -> Optional[tuple]:
        """Для картинки SCADA: (свежий кадр, данные разметки уровня) или None, если кадр старше 5 с / камера застыла."""
        f = self._level_frame()
        if f is None:
            return None
        lv = next(iter(self.engine.levels.values()), None)         # группа-уровень камеры (обычно одна — декантатор)
        return f, (lv.overlay() if lv else None)

    def _level_frame(self) -> Optional[np.ndarray]:
        """Свежий кадр для модели уровня; None — кадра нет, он старше 5 с или камера застыла/мертва (тогда модель не гоняем)."""
        with self._lock:
            f, st = self._frame, self._state
        if f is None or (st is not None and not st.get("alive")) or time.time() - self._last_good > 5.0:
            return None
        return f

    def level_shots_dir(self, gid: str) -> Path:
        """Папка снимков графика уровня группы gid: data\\level_shots\\<камера>\\<группа>."""
        safe = lambda s: re.sub(r"[^\w.-]", "_", str(s))     # noqa: E731
        return self.data_dir / "level_shots" / safe(self.id) / safe(gid)

    def level_shot(self, gid: str, ts: float, direction: int = 0) -> Optional[tuple]:
        """(jpeg-байты, время снимка) — снимок графика уровня, ближайший к ts (direction −1/+1 — соседний раньше/позже), или None.
        Группа — только из своего конфига."""
        g = next((x for x in self.cfg["groups"] if x["type"] == "level" and x["id"] == gid), None)
        if g is None:
            return None
        hit = nearest_trend_shot(self.level_shots_dir(gid), ts, max(90.0, float(g.get("shots_every_s") or 0) * 1.5),
                                 direction=(direction > 0) - (direction < 0))
        if hit is None:
            return None
        try:
            return hit[0].read_bytes(), hit[1]
        except OSError:                                # как раз стёрли по сроку
            return None

    def level_shot_list(self, gid: str, since: float = 0.0) -> Optional[list]:
        """[{"ts", "meta"}] снимков группы новее since — для ленты в окне камеры; None — нет такой группы-уровня."""
        if not any(x["type"] == "level" and x["id"] == gid for x in self.cfg["groups"]):
            return None
        return trend_shot_list(self.level_shots_dir(gid), since)

    def snapshot_jpeg(self, max_w: int = 0) -> Optional[tuple]:
        """(jpeg-байты, (ширина, высота) оригинала) последнего кадра или None."""
        with self._lock:
            f = self._frame
        if f is None:
            return None
        h, w = f.shape[:2]
        if max_w and w > max_w:
            f = cv2.resize(f, (max_w, int(round(h * max_w / w))), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return (buf.tobytes(), (w, h)) if ok else None

    def rod_mask_jpeg(self, zone_id: str, red=None, blue=None, max_w: int = 640, raw: bool = False, red_more=None, blue_more=None, red_not=None, blue_not=None,
                      tip=None, fill=None, empty=None, tol=None) -> Optional[bytes]:
        """JPEG «что видит детектор» по зоне-циклу или уровнемеру с пробными порогами цвета (None — кадра ещё нет); raw — сам кадр без масок.
        fill / empty / tol — пробные образцы цвета и допуск уровнемера-столбика. ValueError — нет такой зоны."""
        cyc = self.engine.cycles.get(zone_id) or self.engine.gauges.get(zone_id)
        if cyc is None:
            raise ValueError("нет зоны «%s» у камеры" % zone_id)
        with self._lock:
            f = self._frame
        if f is None or f.ndim != 3:
            return None
        if isinstance(cyc, TubeRT):
            img = cyc.preview(f, max_w, raw, fill, empty, tol)
        else:
            img = cyc.mask_preview(f, red, blue, max_w, raw, red_more, blue_more, red_not, blue_not, tip)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92 if raw and isinstance(cyc, TubeRT) else 85])
        return buf.tobytes() if ok else None

    def rod_pick(self, zone_id: str, u: float, v: float) -> Optional[dict]:
        """Пипетка: цвет палки в точке превью (u, v — доли картинки rod_mask). None — кадра ещё нет. ValueError — нет такой зоны."""
        cyc = self.engine.cycles.get(zone_id)
        if cyc is None:
            raise ValueError("нет зоны-цикла «%s» у камеры" % zone_id)
        with self._lock:
            f = self._frame
        return cyc.pick_color(f, u, v) if f is not None else None

    def autocal_frame(self) -> tuple:
        """(цветной кадр, метка его прихода) для записи подгонки валов. (None, 0.0) — кадра нет, он старше 5 с или камера застыла/мертва.
        Кадр после приёма не меняется (воркер кладёт новый массив), так что резать вырез можно без копии всего кадра."""
        with self._lock:
            f, st, stamp = self._frame, self._state, self._last_good
        if f is None or f.ndim != 3 or (st is not None and not st.get("alive")) or time.time() - stamp > 5.0:
            return None, 0.0
        return f, stamp

    def mirror_samples(self, zone_ids: list, mode: str = "mirror") -> list:
        """[(θ1, θ1±θ2)] по кадрам в норме, где найдены обе палки — из покадровой истории (последние ~5 мин). Для таблицы поправки зеркальности.
        Берём именно покадровую историю, а не секундную: там берётся максимум за секунду, а он у вращающихся навстречу палок смещён."""
        a, b = zone_ids
        with self._lock:
            rows = list(self._history)
        out = []
        for _ts, zones, ok in rows:
            za, zb = zones.get(a), zones.get(b)
            if ok and za and zb and za[2] and zb[2]:
                out.append((za[0], za[0] + zb[0] if mode == "mirror" else za[0] - zb[0]))
        return out

    def history(self, n: int = 300, window_s: float = 0, points: int = 600) -> dict:
        """Ряды для графиков. n — последние n кадров (калибровка); window_s > 0 — последние window_s секунд
        из длинной истории (до 2 ч), ужатые до ~points точек (максимум в корзине), правый край — «сейчас»."""
        with self._lock:
            if window_s and window_s > 0:
                rows = list(self._long)
                if self._bucket is not None:
                    rows.append(tuple(self._bucket))
                if rows:
                    edge = rows[-1][0] - float(window_s)
                    rows = [r for r in rows if r[0] >= edge]
                pts = _squeeze(rows, max(10, int(points)), self._angle_ids)
            else:
                pts = list(self._history)[-max(1, n):]
        zones: dict = {}
        for _, z, _ok in pts:
            for zid, (fl, df, bit) in z.items():
                d = zones.setdefault(zid, {"flow": [], "diff": [], "bit": []})
                d["flow"].append(fl)
                d["diff"].append(df)
                d["bit"].append(bit)
        out = {"t": [round(t, 2) for t, _, _ in pts], "ok": [ok for _, _, ok in pts], "zones": zones}
        if window_s and window_s > 0:
            fill = self._fill.history(window_s, points)       # признак «вид окна» — свой ряд времени: правый край тот же «сейчас»
            if fill:
                out["fill"] = fill
        return out

    def clarity_sample(self, zone_ids: list, seconds: float) -> dict:
        """Цвет кругов-прозрачности за последние seconds секунд (медиана) — для калибровки «чистый» / «грязный»; сохраняет интерфейс."""
        return self.engine.clarity_sample(list(zone_ids or []), max(2.0, min(float(seconds), 600.0)))

    VIS_STEPS = (30, 60, 120, 300, 600, 1200, 1800, 3600)       # шаг окна видимости, с: подбираем так, чтобы точек было ~70–140

    def visibility(self, window_s: float) -> dict:
        """Видимость палок (зоны-циклы): доля времени, когда палка найдена, окнами за последние window_s секунд (до 7 суток, из секундных
        трендов на диске, которые живут 60 дней). pct — 0..100 или None (в окне мало данных / камера молчала); avg — по всему периоду."""
        window_s = max(300.0, min(float(window_s), 7 * 86400.0))
        step = next((s for s in self.VIS_STEPS if window_s / s <= 140), self.VIS_STEPS[-1])
        now = time.time()
        k0, k1 = int((now - window_s) // step), int(now // step)
        zones = [z for z in self.cfg["zones"] if z["enabled"] and z["type"] == "cycle"]
        try:
            raw = self._store.visibility(self.id, {z["id"] for z in zones}, k0 * step, (k1 + 1) * step, step)
        except Exception as e:  # noqa: BLE001 — диск не должен ронять запрос
            return {"error": "тренды не прочитались: %s" % e}
        series: dict = {}
        avg: dict = {}
        for z in zones:
            rows = raw.get(z["id"], {})
            vals, tot_n, tot_f = [], 0, 0
            for k in range(k0, k1 + 1):
                n, f = rows.get(k, (0, 0))
                tot_n += n
                tot_f += f
                vals.append(round(100.0 * f / n, 1) if n >= max(5, step * 0.2) else None)
            series[z["id"]] = vals
            avg[z["id"]] = round(100.0 * tot_f / tot_n, 1) if tot_n else None
        out = {"window_s": window_s, "step_s": step, "t": [k * step for k in range(k0, k1 + 1)],
               "zones": series, "avg": avg, "names": {z["id"]: z["name"] for z in zones}}
        # отражатели: доля секунд, когда виден отражатель красного (r) и синего (b) конца; только у зон, где отражатели заданы
        tz = [z for z in zones if z.get("tip_hsv") and float(z.get("tip_w", 1.0)) > 0]
        tips: dict = {}
        tavg: dict = {}
        if tz:
            try:
                traw = self._store.tip_visibility(self.id, {z["id"] for z in tz}, k0 * step, (k1 + 1) * step, step)
            except Exception as e:  # noqa: BLE001
                traw = {}
                log.warning("[%s] видимость отражателей не прочиталась: %s", self.id, e)
            for z in tz:
                rows = traw.get(z["id"], {})
                sr, sb, tot = [], [], [0, 0, 0]
                for k in range(k0, k1 + 1):
                    n, r, b = rows.get(k, (0, 0, 0))
                    tot[0] += n; tot[1] += r; tot[2] += b
                    ok = n >= max(5, step * 0.2)
                    sr.append(round(100.0 * r / n, 1) if ok else None)
                    sb.append(round(100.0 * b / n, 1) if ok else None)
                tips[z["id"]] = {"r": sr, "b": sb}
                tavg[z["id"]] = {"r": round(100.0 * tot[1] / tot[0], 1) if tot[0] else None, "b": round(100.0 * tot[2] / tot[0], 1) if tot[0] else None}
            out["tips"] = tips
            out["tips_avg"] = tavg
        return out

    LOSS_SECTOR_DEG = 15

    def loss_map(self, window_s: float) -> dict:
        """Карта потерь палок (зоны-циклы) за последние window_s секунд (до 14 суток, секундные тренды на диске): где и когда палка пропадает.
        sector — по углу красного конца, секторов 360/sector_deg (с 0° по часовой): n — секунд с данными, f — секунд «найдена».
        hour — по часам суток (местное время), сюда входят и секунды полной потери угла. Время без картинки (ok=0) не считается."""
        window_s = max(3600.0, min(float(window_s), 14 * 86400.0))
        now = time.time()
        zones = [z for z in self.cfg["zones"] if z["enabled"] and z["type"] == "cycle"]
        nsec = 360 // self.LOSS_SECTOR_DEG
        try:
            raw = self._store.loss_map(self.id, {z["id"] for z in zones}, now - window_s, now + 1, self.LOSS_SECTOR_DEG)
        except Exception as e:  # noqa: BLE001 — диск не должен ронять запрос
            return {"error": "тренды не прочитались: %s" % e}
        out: dict = {}
        for z in zones:
            sn, sf, hn, hf = [0] * nsec, [0] * nsec, [0] * 24, [0] * 24
            for (h, s), (n, f) in raw.get(z["id"], {}).items():
                if 0 <= h < 24:
                    hn[h] += n
                    hf[h] += f
                if 0 <= s < nsec:
                    sn[s] += n
                    sf[s] += f
            tot_n, tot_f = sum(hn), sum(hf)
            out[z["id"]] = {"name": z["name"], "sector": {"n": sn, "f": sf}, "hour": {"n": hn, "f": hf},
                            "avg": round(100.0 * tot_f / tot_n, 1) if tot_n else None, "seconds": tot_n}
        return {"window_s": window_s, "sector_deg": self.LOSS_SECTOR_DEG, "zones": out}

    def stats(self, window_s: float) -> dict:
        """Среднее / минимум / максимум flow и diff по каждой зоне за последние window_s секунд (до суток), только по кадрам в норме
        (битые, застывшие и «каша» не считаются). Значения — те же, что в графиках и CSV (сглаженные по `smooth` кадров).
        span_s — сколько реально накоплено (меньше window_s, если сервис недавно стартовал или камера пересоздавалась)."""
        window_s = max(float(STATS_STEP_S), min(float(window_s), STATS_STEP_S * STATS_LEN))
        edge = int((time.time() - window_s) // STATS_STEP_S)
        with self._lock:
            rows = [r for r in self._stats if r[0] >= edge]
            if self._sbucket is not None and self._sbucket[0] >= edge:      # набирающуюся корзину копируем: её правит поток камеры
                rows.append([self._sbucket[0], {k: list(v) for k, v in self._sbucket[1].items()}])
            first = rows[0][0] if rows else None
            mrows = [r for r in self._mstats if r[0] >= edge]
            if self._mbucket is not None and self._mbucket[0] >= edge and self._mbucket[1]:
                mrows.append([self._mbucket[0], {k: list(v) for k, v in self._mbucket[1].items()}])
        macc: dict = {}
        mfirst: dict = {}
        for _key, gs in mrows:
            for gid, v in gs.items():
                mfirst.setdefault(gid, _key)
                a = macc.get(gid)
                if a is None:
                    macc[gid] = list(v)
                else:
                    a[0] += v[0]; a[1] += v[1]; a[2] = min(a[2], v[2]); a[3] = max(a[3], v[3])
                    a[4] += v[4]; a[5] = min(a[5], v[5]); a[6] = max(a[6], v[6])
        groups = {gid: {"n": a[0], "span_s": round(max(0.0, min(window_s, time.time() - mfirst[gid] * STATS_STEP_S)), 1),
                        "delta": {"avg": round(a[1] / a[0], 2), "min": round(a[2], 2), "max": round(a[3], 2)},
                        "corr": {"avg": round(a[4] / a[0], 2), "min": round(a[5], 2), "max": round(a[6], 2)}}
                  for gid, a in macc.items() if a[0]}
        acc: dict = {}
        for _key, zs in rows:
            for zid, v in zs.items():
                a = acc.get(zid)
                if a is None:
                    acc[zid] = list(v)
                else:
                    a[0] += v[0]; a[1] += v[1]; a[2] = min(a[2], v[2]); a[3] = max(a[3], v[3])
                    a[4] += v[4]; a[5] = min(a[5], v[5]); a[6] = max(a[6], v[6])
        zones = {zid: {"n": a[0],
                       "flow": {"avg": round(a[1] / a[0], 4), "min": round(a[2], 4), "max": round(a[3], 4)},
                       "diff": {"avg": round(a[4] / a[0], 4), "min": round(a[5], 4), "max": round(a[6], 4)}}
                 for zid, a in acc.items() if a[0]}
        for zid, f in self._fill.stats(window_s).items():       # вид окна: tex / lum / rel — ср / мин / макс
            if zid in zones:
                zones[zid]["fill"] = f
        span = round(min(window_s, time.time() - first * STATS_STEP_S), 1) if first is not None else 0.0
        return {"window_s": window_s, "span_s": max(0.0, span), "zones": zones, "groups": groups}

    def _restore_trends(self):
        """Длинная история (2 ч) и корзины статистики (сутки) — из data\trends: после обновления сервиса графики не пустые."""
        try:
            zids = {z["id"] for z in self.cfg["zones"] if z["enabled"]}
            longs, stats = self._trend.load(time.time(), LONG_LEN * LONG_STEP_S, STATS_LEN * STATS_STEP_S, zids)
        except Exception as e:  # noqa: BLE001 — битый файл трендов не должен мешать запуску камеры
            log.warning("[%s] тренды с диска не восстановлены: %s", self.id, e)
            return
        self._long.extend(longs[-LONG_LEN:])
        self._stats.extend(stats[-STATS_LEN:])
        if longs or stats:
            log.info("[%s] тренды восстановлены с диска: %d точек истории, %d корзин статистики", self.id, len(longs), len(stats))

    def shot(self, max_w: int = 1280) -> dict:
        """Снимок «в один момент» для data\\shots: {"jpeg": байты|None, "info": {связь, статус, значения зон}}. На кадре нарисованы зоны:
        зелёная — «движется» (бит 1), красная — «стоит», рядом diff / flow."""
        st, _writes, stale = self._current()
        with self._lock:
            f = None if self._frame is None else self._frame.copy()
            fps = 0.0
            if not stale and len(self._stamps) >= 2 and self._stamps[-1] > self._stamps[0]:
                fps = (len(self._stamps) - 1) / (self._stamps[-1] - self._stamps[0])
            conn, err, dec = self._connected, self._error, self._decoder
        zones = {z["id"]: {"flow": round(z["flow"], 3), "diff": round(z["diff"], 3), "bit": int(z["bit"]),
                           **({"motion": False} if z.get("motion") is False else {}),
                           **({"tex": z["tex"], "lum": z["lum"], "rel": z["rel"], "state": z["state"]} if "tex" in z else {})} for z in (st or {}).get("zones", [])}
        info = {"name": self.cfg["name"], "connected": bool(conn), "alive": bool(st and st.get("alive")),
                "status": "ok" if (st and st.get("alive")) else ((st or {}).get("reason") or err or "нет данных"),
                "fps": round(fps, 2), "decoder": dec, "zones": zones}
        if f is None:
            return {"jpeg": None, "info": info}
        if f.ndim == 2:
            f = cv2.cvtColor(f, cv2.COLOR_GRAY2BGR)
        h, w = f.shape[:2]
        if max_w and w > max_w:
            k = max_w / float(w)
            f = cv2.resize(f, (max_w, int(round(h * k))), interpolation=cv2.INTER_AREA)
        else:
            k = 1.0
        fh, fw = f.shape[:2]
        size = self.cfg.get("frame_size")
        for zc in self.cfg["zones"]:
            z = zones.get(zc["id"])
            if not z or not zc.get("points") or zc["type"] == "tube":
                continue
            pts = np.round(np.array(scale_points(zc["points"], size, (w, h))) * k).astype(np.int32)
            color = (60, 200, 60) if z["bit"] else (50, 50, 230)
            cv2.polylines(f, [pts], True, color, 2)
            moved = "" if z.get("motion") is False else " d=%.2f f=%.2f" % (z["diff"], z["flow"])
            cv2.putText(f, zc["id"] + moved + (" t=%.1f L=%.0f r=%.2f" % (z["tex"], z["lum"], z["rel"]) if "tex" in z else ""),
                        (int(pts[:, 0].min()), max(14, int(pts[:, 1].min()) - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, max(0.4, fw / 1800.0), color, 1, cv2.LINE_AA)
        for zc in self.cfg["zones"]:                # уровнемеры: шкала с процентами (линейка стрелки или разметка столбика; зону указателя нарисовал общий цикл выше)
            if zc["type"] not in GAUGE_TYPES:
                continue
            zs = next((z for z in (st or {}).get("zones", []) if z["id"] == zc["id"]), None)
            rl = np.round(np.array(scale_points(zc["ruler" if zc["type"] == "gate" else "points"], size, (w, h))) * k).astype(np.int32)
            if zc["type"] == "tube" and len(rl) == 4:
                rl = cv2.convexHull(rl).reshape(-1, 2)
            color = (60, 200, 60) if zs and zs["valid"] else (150, 150, 150)
            cv2.polylines(f, [rl], True, color, 2)
            cv2.putText(f, "%s %s" % (zc["id"], "%.0f%%" % zs["pct"] if zs and zs["valid"] else "--"), (int(rl[:, 0].min()), max(14, int(rl[:, 1].min()) - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, max(0.4, fw / 1800.0), color, 1, cv2.LINE_AA)
        for zc in self.cfg["zones"]:                # круги прозрачности: цвет по шкале зелёный → красный и число, нет значения — серый
            if zc["type"] != "clarity":
                continue
            zs = next((z for z in (st or {}).get("zones", []) if z["id"] == zc["id"]), None)
            if not zs:
                continue
            sx, sy = (w / float(size[0]), h / float(size[1])) if size else (1.0, 1.0)
            cx, cy, rr = int(zc["center"][0] * sx * k), int(zc["center"][1] * sy * k), max(3, int(zc["radius"] * (sx + sy) / 2.0 * k))
            t = zs["turb"]
            color = (150, 150, 150) if not zs["valid"] else (0, int(200 * (1 - t / 100.0) + 55), int(60 + 170 * t / 100.0))
            cv2.circle(f, (cx, cy), rr, color, 2)
            cv2.putText(f, "%s %s" % (zc["name"], "pusto" if zs.get("empty") else ("%.0f" % t) if zs["valid"] else "--"), (max(0, cx - rr), max(14, cy - rr - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, max(0.4, fw / 1800.0), color, 1, cv2.LINE_AA)
        cv2.putText(f, "%s %s" % (self.id, time.strftime("%Y-%m-%d %H:%M:%S")), (8, fh - 10), cv2.FONT_HERSHEY_SIMPLEX,
                    max(0.45, fw / 1600.0), (255, 255, 255), 1, cv2.LINE_AA)
        ok, buf = cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 82])
        return {"jpeg": buf.tobytes() if ok else None, "info": info}

    def start_record(self) -> Optional[str]:
        """Писать метрики каждого кадра в базу (data\\motion.db) — для калибровки порогов по реальной ленте. -> метка запуска."""
        with self._lock:
            if self._rec_run:
                return self._rec_name
            self._rec_ids = [z["id"] for z in self.cfg["zones"] if z["enabled"]]
            self._rec_fill = {z["id"] for z in self.cfg["zones"] if z["enabled"] and z.get("fill_metric", "off") != "off"}
            try:
                self._rec_run = self._store.rec_start(self.id, self._rec_ids, self._rec_fill, time.time())
            except (OSError, sqlite3.Error) as e:                # диск полон / база недоступна: запись не включается, камера работает дальше
                log.warning("[%s] запись метрик не включилась: %s", self.id, e)
                return None
            self._rec_name = "%s_%s_r%d" % (self.id, time.strftime("%Y%m%d_%H%M%S"), self._rec_run)
            return self._rec_name

    def flush_record(self):
        """Дописать буфер на диск (перед выдачей архива: иначе последние секунды лежат в памяти)."""
        self._store.flush()

    def _rec_write(self, ts: float, byid: dict, status: str, event: bool = False):
        """Строка записи кадра. Звать под self._lock. status — «ok» или причина (картинка застыла, каша…); event — разовое
        событие связи (alive пустой, значения зон пустые): объясняет дыру в строках, когда поток оборвался и кадров не было."""
        if not self._rec_run:
            return
        zones: dict = {}
        if not event:
            for zid in self._rec_ids:
                z = byid.get(zid) or {}
                v = {"flow": z.get("flow", 0), "diff": z.get("diff", 0), "bit": z.get("bit", 0)}
                if zid in self._rec_fill:           # признак вида окна (зернистость, яркость, жом есть) — для подбора порогов по записи
                    v.update(tex=z.get("tex"), lum=z.get("lum"), rel=z.get("rel"), fill=z.get("fill"))
                zones[zid] = v
        try:
            self._store.rec_add(self.id, ts, None if event else int(status == "ok"), status, zones)
            self._store.maybe_flush(ts, force=status != "ok")      # сбой — сразу в базу: переживёт падение процесса (при отключении питания WAL без fsync теряет последние секунды)
        except (OSError, sqlite3.Error) as e:
            if not self._rec_warned:
                self._rec_warned = True
                log.warning("[%s] запись метрик не пишется: %s", self.id, e)

    def stop_record(self):
        with self._lock:
            if self._rec_run:
                try:
                    self._store.rec_stop(self._rec_run, time.time())
                except (OSError, sqlite3.Error):
                    pass
            self._rec_run = None
            self._rec_name = None

    # ─────────── внутреннее ───────────
    def _push_history(self, ts: float, zones: dict, ok: int):
        """Точка в обычную (по кадрам) и в длинную (по секундам) историю. Звать под self._lock."""
        self._history.append((ts, zones, ok))
        b = self._bucket
        if b is None:
            self._bucket = [ts, dict(zones), ok]
        elif ts - b[0] >= LONG_STEP_S:
            self._long.append((b[0], b[1], b[2]))
            self._trend.add_long(b[0], b[1], b[2])
            self._bucket = [ts, dict(zones), ok]
        else:
            for zid, (fl, df, bit) in zones.items():
                old = b[1].get(zid)
                if old is None:
                    b[1][zid] = (fl, df, bit)
                elif zid in self._angle_ids:
                    # угол не «максимум»: берём самый поздний кадр секунды, где палка найдена (бит секунды — «найдена хотя бы раз»)
                    b[1][zid] = (fl, df, 1) if bit else (old if old[2] else (fl, df, 0))
                else:
                    b[1][zid] = (max(old[0], fl), max(old[1], df), max(old[2], bit))
            b[2] = min(b[2], ok)

    def _tip_add(self, ts: float, zones: list):
        """Живой кадр → секунда видимости отражателей (зоны с настроенными отражателями): конец «виден», если хоть в одном кадре секунды
        сила кончика ≥ TIP_SEEN и угол не достроен по скорости. Звать под self._lock."""
        seen = {z["id"]: (not z.get("coast") and float(z.get("lvl_tr") or 0.0) >= TIP_SEEN,
                          not z.get("coast") and float(z.get("lvl_tb") or 0.0) >= TIP_SEEN)
                for z in zones if z.get("type") == "cycle" and z.get("tip_on")}
        if not seen:
            return
        sec = int(ts)
        b = self._tipb
        if b is not None and b[0] != sec:
            self._trend.add_tip(b[0], {k: tuple(v) for k, v in b[1].items()})
            b = None
        if b is None:
            self._tipb = [sec, {k: list(v) for k, v in seen.items()}]
            return
        for k, v in seen.items():
            o = b[1].setdefault(k, [False, False])
            o[0] = o[0] or v[0]
            o[1] = o[1] or v[1]

    @staticmethod
    def _fill_zones(zones: list) -> dict:
        """Зоны с включённым признаком «вид окна» (у остальных в состоянии нет tex): {зона: (tex, lum, rel, жом есть|None)}."""
        return {z["id"]: (z["tex"], z["lum"], z["rel"], z.get("fill")) for z in zones if z.get("tex") is not None}

    def _stats_add(self, ts: float, zones: dict):
        """Кадр в норме — в 10-секундную корзину статистики. Звать под self._lock."""
        key = int(ts // STATS_STEP_S)
        b = self._sbucket
        if b is None or b[0] != key:
            if b is not None:
                self._stats.append(b)
                self._trend.add_stats(b)
            b = self._sbucket = [key, {}]
        for zid, (fl, df, _bit) in zones.items():
            a = b[1].get(zid)
            if a is None:
                b[1][zid] = [1, fl, fl, fl, df, df, df]
            else:
                a[0] += 1; a[1] += fl; a[2] = min(a[2], fl); a[3] = max(a[3], fl)
                a[4] += df; a[5] = min(a[5], df); a[6] = max(a[6], df)

    def _mirror_stats_add(self, ts: float, groups: list):
        """Δ группы синхронности (уже С коррекцией) и сама поправка — в 10-секундную корзину. Только кадры, где видны обе палки и угол не достроен
        (в удержании Δ ненадёжна — её нет и в шапке). Звать под self._lock."""
        key = int(ts // STATS_STEP_S)
        b = self._mbucket
        if b is None or b[0] != key:
            if b is not None and b[1]:
                self._mstats.append(b)
            b = self._mbucket = [key, {}]
        for g in groups:
            if g.get("type") != "mirror" or not g.get("valid") or g.get("hold") or g.get("delta") is None:
                continue
            d, cr = float(g["delta"]), float(g.get("corr") or 0.0)
            a = b[1].get(g["id"])
            if a is None:
                b[1][g["id"]] = [1, d, d, d, cr, cr, cr]
            else:
                a[0] += 1; a[1] += d; a[2] = min(a[2], d); a[3] = max(a[3], d)
                a[4] += cr; a[5] = min(a[5], cr); a[6] = max(a[6], cr)

    def _publish(self, state: dict, frame: Optional[np.ndarray], count_frame: bool, heartbeat: bool = True):
        """heartbeat=False — это не результат анализа (сбой): отсчёт свежести не сдвигаем."""
        writes = self.engine.compute_writes(state)
        with self._lock:
            self._state = state
            self._writes = writes            # вместе с состоянием: сбой ниже (история, запись на диск) не оставит в ПЛК старое
            if heartbeat:
                self._pub_mono = time.monotonic()
            if frame is not None:
                self._frame = frame
            if count_frame:
                self._stamps.append(state["ts"])
                lag = self._lag_s or 0.0                  # время съёмки = когда взяли − сколько кадр простоял в очереди (время потока монотонно)
                if self._lag_s is not None:
                    self._lag_hist.append((state["ts"], self._lag_s))
                while self._lag_hist and state["ts"] - self._lag_hist[0][0] > LAG_MAX_WIN_S:
                    self._lag_hist.popleft()
                if state["alive"]:
                    for z in state["zones"]:
                        if z.get("type") == "cycle":
                            h = self._rod_hist.get(z["id"])
                            if h is None:
                                h = self._rod_hist[z["id"]] = deque(maxlen=ROD_HIST_LEN)
                            t_cap = state["ts"] - lag
                            if h and t_cap <= h[-1][0]:        # минимум базы мог сдвинуться больше шага кадра: ряд для интерполяции обязан идти строго вперёд
                                t_cap = h[-1][0] + 1e-3
                            h.append((t_cap, z["angle"], bool(z["valid"]), bool(z["coast"])))
                zones = {z["id"]: (z["flow"], z["diff"], z["bit"]) for z in state["zones"]}
                self._push_history(state["ts"], zones, 1 if state["alive"] else 0)
                if state["alive"]:
                    self._tip_add(state["ts"], state["zones"])
                self._fill.add(state["ts"], self._fill_zones(state["zones"]), bool(state["alive"]))
                if state["alive"]:
                    # мутность без значения (нет эталонов, цвет не на шкале) и уровнемер без значения в среднее / мин / макс не идут:
                    # ноль там означал бы «чистый» / «0 %»
                    bad = {z["id"] for z in state["zones"] if z["type"] in ("clarity",) + GAUGE_TYPES and not z["valid"]}
                    self._stats_add(state["ts"], {k: v for k, v in zones.items() if k not in bad} if bad else zones)
                    self._mirror_stats_add(state["ts"], state.get("groups") or [])
            self._rec_write(state["ts"], {z["id"]: z for z in state["zones"]}, "ok" if state["alive"] else (state["reason"] or "нет данных"))
        self._writes_evt.set()
        self._juice_tick(state, frame, count_frame)

    def _juice_tick(self, state: dict, frame: Optional[np.ndarray], count_frame: bool):
        """База «Сок»: раз в every_min минут кадр и значения стаканов на диск (если оператор включил). Только живые кадры."""
        jb = self.cfg.get("juice_base") or {}
        if not (jb.get("on") and count_frame and frame is not None and state.get("alive")):
            return
        ts = state["ts"]
        if ts - self._juice_last < jb["every_min"] * 60 - 1:
            return
        rows = juice_base.rows_for(state, self.cfg["zones"])
        if not juice_base.worth_saving(rows):                # цвета нет ни у одного стакана (нет кадра) — снимок бесполезен; без эталонов цвет есть, пишем
            return
        self._juice_last = ts
        if juice_base.save(self.data_dir, self.id, ts, frame, rows) and ts - self._juice_purged > 86400:
            self._juice_purged = ts
            juice_base.purge(self.data_dir, self.id, ts)

    def _publish_held(self, now: float, frame: np.ndarray):
        """Каша: показываем последнее состояние с отметкой corrupt, историю и запись в ПЛК не двигаем."""
        with self._lock:
            st = dict(self._state) if self._state else None
            self._frame = frame
        if st is not None:
            st["corrupt"] = True
            st["ts"] = now
            st["zones"] = self.engine.held_zones(st["zones"], now)
            with self._lock:
                self._state = st
                self._pub_mono = time.monotonic()
                self._stamps.append(now)
                self._push_history(now, {z["id"]: (z["flow"], z["diff"], z["bit"]) for z in st["zones"]}, 0)
                self._fill.add(now, self._fill_zones(st["zones"]), False)
                self._rec_write(now, {z["id"]: z for z in st["zones"]}, "битый кадр (серая каша)")

    def _plc_loop(self):
        """Поток записи: пишет, когда значения изменились или прошло write_period_s. Недоступный ПЛК тормозит только его."""
        link = None
        sent: Optional[list] = None
        last = 0.0
        period = float(self.mb.get("write_period_s", 1.0))
        while not self._halt.is_set():
            self._writes_evt.wait(timeout=min(period, 1.0))      # не реже раза в секунду: замолчавший поток камеры события не пришлёт
            self._writes_evt.clear()
            if self._halt.is_set():
                break
            if not self.mb.get("enable"):
                self.plc.update(enabled=False, ok=None, error="")
                continue
            try:
                writes = self._current()[1]
            except Exception:  # noqa: BLE001 — поток записи не должен умирать
                log.exception("[%s] записи в ПЛК не посчитаны", self.id)
                self._halt.wait(FAIL_LOG_S)
                continue
            if not writes:
                continue
            now = time.time()
            if writes == sent and now - last < period:
                continue
            if link is None:
                link = self.links.get(self.mb["host"], self.mb["port"], self.mb["device_id"])
            ok = link.write(writes)
            last = now
            if ok:
                sent = writes
            self.plc.update(enabled=True, ok=ok, error="" if ok else link.last_error)

    def run(self):
        cfg = self.cfg
        url = cfg["url"]
        if not cfg["enabled"] or not url:
            self._error = "камера выключена" if not cfg["enabled"] else "не задан адрес потока"
            return
        self._writer = threading.Thread(target=self._plc_loop, daemon=True, name="plc-" + self.id)
        self._writer.start()
        for gid, lv in self.engine.levels.items():     # группы-уровни: свой поток модели на каждую (обычно одна — декантатор)
            LevelInfer(lv, self._level_frame, self._halt, self.level_post, save_dir=self.data_dir / "level_frames",
                       shots_dir=self.level_shots_dir(gid)).start()
        wv = cfg.get("webview")
        if wv and wv["enabled"]:
            self._web = WebView(self.id, wv, self._web_source, self._halt)
            self._web.start()
        file_src = not is_stream_url(url)
        interval = self.engine.proc_interval
        timeout = cfg["freeze_timeout_s"]
        cap = None
        last_proc = 0.0
        last_gray = None
        freeze_time = 0.0
        last_diff = 0.0                        # последняя разница соседних кадров (в лог при «застыла»)
        frozen = False
        stuck_gray = None                      # последний кадр застывшей камеры (None — не застыла); переживает переоткрытие потока
        bad_since = 0.0                       # когда началась «серая каша» (0 — кадр нормальный)
        bad_until = 0.0                        # пока не истечёт — кадры после каши не считаем
        was_bad = False
        got_frame = False                      # с этого открытия потока уже пришёл хоть один кадр
        next_t = 0.0                           # темп чтения файла
        file_dt = 0.04
        self._pub_mono = time.monotonic()      # отсчёт свежести: не будет первого результата дольше порога — тоже «мёртвая»
        while not self._halt.is_set():
            try:
                now = time.time()
                if cap is None or not cap.isOpened():
                    if cap is not None:
                        cap.release()
                    cap = self._open(url, cfg)
                    if not cap.isOpened():
                        self._note_conn(False, "нет связи с источником")
                        self._dead(now, timeout, "нет кадров")
                        self._halt.wait(cfg["reconnect_delay_s"])
                        continue
                    self._note_conn(True, "")
                    self._decoder = decoder_kind(cap)
                    self._qlag.reset()                 # поток открыт заново: время потока и очередь начинаются с нуля
                    self._lag_s = None
                    got_frame = False
                    if self._last_good and time.time() - self._last_good < timeout:
                        self.engine.buf.clear()    # короткий обрыв (короче freeze_timeout_s): сравнивать со старым кадром нельзя, а биты зон держим
                    else:
                        self.engine.reset()
                    # last_gray и stuck_gray остаются: залипший захват после переоткрытия отдаёт тот же кадр
                    last_proc, freeze_time, frozen = 0.0, 0.0, stuck_gray is not None
                    if file_src:
                        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
                        file_dt = 1.0 / max(1.0, fps)
                        next_t = time.time()
                if not cap.grab():
                    if file_src and cfg["loop"]:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        self.engine.reset()
                        last_gray = None
                        continue
                    self._note_conn(False, "поток оборвался")
                    self._hw_giveup_check(got_frame)
                    cap.release()
                    cap = None
                    self._dead(time.time(), timeout, "поток оборвался")
                    self._halt.wait(cfg["reconnect_delay_s"])
                    continue
                if file_src:                       # без паузы файл читался бы со скоростью диска
                    next_t += file_dt
                    self._halt.wait(max(0.0, next_t - time.time()))
                now = time.time()
                # Очередь кадров: цикл медленнее камеры (или просел процессор/диск) — кадры копятся в сокете и декодере, и обрабатываются
                # ВСЕ по порядку, ничего не пропуская: отставание растёт и тает лишь на запасе скорости (минуты). Берём время потока кадра и,
                # пока очередь длиннее LAG_CATCHUP_S, выбрасываем кадры без разбора: grab() дёшев, очередь уходит за секунду-две.
                lag = None if file_src else self._qlag.update(now, stream_pos_ms(cap))
                if lag is not None and lag > LAG_CATCHUP_S and now - last_proc < LAG_MAX_SKIP_S:
                    self._skipped += 1
                    self._last_good = now
                    continue
                if now - last_proc < interval * PROC_TOLERANCE:
                    continue
                self._lag_s = lag
                t_dec = time.perf_counter()
                ok, frame = cap.retrieve()
                if not ok or frame is None:
                    self._dead(now, timeout, "нет кадров")
                    continue
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
                PERF.record(self.id + ":frame", (time.perf_counter() - t_dec) * 1000.0, budget_ms=interval * 1000.0,
                            note="%d×%d%s" % (frame.shape[1], frame.shape[0], " · " + self._decoder if self._decoder else ""))
                dt = now - last_proc if last_proc else interval
                last_proc = now
                self._last_good = now
                got_frame = True
                self._hw_fail = 0
                # детектор зависшей картинки: живой поток всегда чуть «дышит» (шум матрицы)
                still = False                      # картинка не изменилась с прошлого кадра (в том числе кадра до переоткрытия потока)
                if last_gray is not None and last_gray.shape == gray.shape:
                    last_diff = float(cv2.absdiff(last_gray, gray).mean())
                    if last_diff < cfg["freeze_eps"]:
                        freeze_time += dt
                        still = True
                    else:
                        freeze_time = 0.0
                last_gray = gray
                # серая каша: результаты не берём, держим последнее состояние; если затянулось — как мёртвая камера
                cr = cfg["corrupt_ratio"]
                if cr > 0 and garbage_ratio(gray) >= cr:
                    if not bad_since:
                        bad_since = now
                        log.warning("[%s] битый кадр (серая каша) — результаты не учитываются", self.id)
                    bad_until = now + cfg["corrupt_hold_s"]
                elif bad_since and now >= bad_until:
                    log.info("[%s] кадры в норме (каша %.1f с)", self.id, now - bad_since)
                    bad_since = 0.0
                if bad_since:
                    was_bad = True
                    if now - bad_since >= timeout:
                        frozen = True              # после выхода из каши — состояния с нуля (как после фриза)
                        self._publish(self.engine.dead_state(now, "битый поток"), frame, True)
                    else:
                        self._publish_held(now, frame)
                    continue
                if was_bad:                        # после каши первый кадр сравнивать не с чем — буфер с нуля, гистерезис не трогаем
                    was_bad = False
                    self.engine.buf.clear()
                # застывшую камеру оживляет только изменившийся кадр: счётчик freeze_time обнуляют и переоткрытие потока,
                # и каша между одинаковыми кадрами, поэтому сверяем ещё и с последним кадром застывшей камеры
                now_frozen = freeze_time >= timeout or (stuck_gray is not None and stuck_gray.shape == gray.shape
                                                        and float(cv2.absdiff(stuck_gray, gray).mean()) < cfg["freeze_eps"])
                if now_frozen:
                    stuck_gray = gray
                    if not frozen:
                        log.warning("[%s] картинка застыла: разница соседних кадров %.3f < %.3f уже %.0f с", self.id, last_diff, cfg["freeze_eps"], freeze_time)
                    frozen = True
                    if not file_src and freeze_time >= max(2 * timeout, FREEZE_REOPEN_S):
                        # залипший захват (декодер отдаёт один и тот же кадр, а grab() успешен) сам не оживает — открываем поток заново
                        log.warning("[%s] картинка стоит %.0f с — переоткрываю поток", self.id, freeze_time)
                        with self._lock:
                            self._rec_write(now, {}, "событие: картинка стоит, переоткрываю поток", event=True)
                        cap.release()
                        cap = None
                        self._publish(self.engine.dead_state(now, "картинка застыла"), frame, True)
                        self._halt.wait(cfg["reconnect_delay_s"])
                        continue
                    self._publish(self.engine.dead_state(now, "картинка застыла"), frame, True)
                    continue
                stuck_gray = None
                if frozen:                         # камера ожила — состояния с нуля
                    frozen = False
                    self.engine.reset()
                st = self.engine.process(gray, now, frame, fresh=not still)
                if st is not None:
                    self._publish(st, frame, True)
                else:
                    with self._lock:
                        self._frame = frame
            except Exception:  # noqa: BLE001 — сбой разбора кадра (например, нет памяти в OpenCV) не должен убивать камеру
                self._note_failure()
                if self._stale():          # сбои подряд дольше порога: камера мертва, состояния с нуля, поток открываем заново
                    if cap is not None:
                        cap.release()
                        cap = None
                    try:
                        self.engine.reset()
                        self._publish(self.engine.dead_state(time.time(), FAIL_REASON), None, False, heartbeat=False)
                    except Exception:  # noqa: BLE001 — не вышло и это: наружу всё равно отдаст «мёртвую» _current()
                        self._note_failure()
                self._halt.wait(cfg["reconnect_delay_s"] if cap is None else min(interval, 1.0))
        if cap is not None:
            cap.release()

    def _note_failure(self):
        """Разбор кадра упал (звать из except): в лог — первый раз и дальше не чаще FAIL_LOG_S."""
        self._fail_n += 1
        now = time.monotonic()
        if self._fail_logged is None or now - self._fail_logged >= FAIL_LOG_S:
            self._fail_logged = now
            log.exception("[%s] %s (всего сбоев: %d) — кадр пропущен", self.id, FAIL_REASON, self._fail_n)

    def _note_conn(self, ok: bool, err: str):
        """Состояние подключения + запись в журнал ТОЛЬКО при смене (мёртвая камера не засоряет журнал каждую попытку)."""
        if ok != self._connected or (not ok and self._conn_logged is None):
            src = self.cfg["url"] if not is_stream_url(self.cfg["url"]) else self.cfg["url"].split("@")[-1]
            if ok:
                log.info("[%s] источник открыт: %s", self.id, src)
            else:
                log.warning("[%s] %s: %s", self.id, err, src)
            with self._lock:
                self._rec_write(time.time(), {}, "событие: " + ("источник открыт" if ok else err), event=True)
        self._conn_logged = ok
        self._connected, self._error = ok, err

    def _dead(self, now: float, timeout: float, reason: str):
        """Источника нет: после freeze_timeout_s без кадров выдаём нули и status=0."""
        if self._last_good and now - self._last_good < timeout:
            return
        self._publish(self.engine.dead_state(now, reason), None, False)
