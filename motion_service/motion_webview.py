"""motion_webview — HTTP-картинка камеры для SCADA и браузера: перенос блока `[webview]` из LVLDec (его модуль httpstream).

Это НЕ видеопоток: на каждый запрос отдаётся один готовый JPEG, страницы сами перезагружают картинку по таймеру.
Кадр готовится не по запросу, а раз в update_sec — читатели берут уже сжатый JPEG из хранилища, сколько бы их ни было.

Адреса (как в LVLDec):
  /, /index.html            страница с картинкой и переключателем «Сырой / С разметкой»
  /scada.html, /scada.htm   минимальная страница для SCADA (старый IE-компонент): картинка на весь экран + обновление по таймеру
  /scada.jpg                адрес для SCADA: кадр с разметкой, если annotate = true, иначе тот же чистый кадр, что /frame.jpg
  /frame.jpg                последний сырой кадр: обрезка → размер → метка времени → JPEG
  /frame_annotated.jpg      кадр с разметкой (маска жидкости, рамки, Total / g1 / g2); только при annotate = true
  /healthz                  JSON: возраст последних JPEG в секундах (для мониторинга)

Настройки на камеру — cam["webview"], имена как в config.ini LVLDec: enabled, host, port, update_sec, jpeg_quality, annotate,
crop_x1..crop_y2 (пиксели ОРИГИНАЛЬНОГО кадра, 0 = без обрезки), output_w / output_h (0 = без изменения размера).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

import cv2
import numpy as np

log = logging.getLogger("motion")

COL_LIQUID = (60, 210, 60)       # BGR, как в отладчике LVLDec
COL_GLASS = (0, 170, 230)
COL_TEXT = (60, 255, 60)
FONT = cv2.FONT_HERSHEY_SIMPLEX
# что рисуется на размеченном кадре для SCADA (в LVLDec это web_show_mask): маски жидкости, рамки и подписи классов; стекло скрыто
SHOW = {"glass": False, "liquid": True, "boxes": True, "labels": True}
# порт занят — пауза перед следующей попыткой, дальше каждые 30 с. Первая неудача тихая: правка настроек пересоздаёт камеру, и прежний сервер ещё
# закрывает порт (до 0,25 с) — это не ошибка. Вторая подряд — уже настоящая занятость (старый LVLDec.exe), о ней говорим в журнале и в состоянии.
BIND_RETRY_S = (0.5, 2, 5, 15, 30)


def stamp_time(img: np.ndarray, ts: float) -> None:
    """Метка времени снизу слева (не перекрывает уровень сверху). Мутирует img."""
    txt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    y = img.shape[0] - 12
    cv2.putText(img, txt, (12, y), FONT, 0.7, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(img, txt, (12, y), FONT, 0.7, (255, 255, 255), 1, cv2.LINE_AA)


def _put(img, line: str, y: int):
    cv2.putText(img, line, (12, y), FONT, 0.7, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(img, line, (12, y), FONT, 0.7, COL_TEXT, 1, cv2.LINE_AA)


def draw_overlay(frame: np.ndarray, objs: list, wh: Optional[list], liquid_class: str, lv: Optional[dict]) -> np.ndarray:
    """Размеченный кадр, как рисует отладчик LVLDec для веба: полупрозрачная маска найденной жидкости, рамка с подписью «класс уверенность»
    и две строки сверху — Total / g1 / g2 и статус слоёв. objs — объекты сервиса уровня [{name, conf, bbox, polygons}] в пикселях кадра wh;
    lv — {total, glass1, glass2} или None (уровня нет → «levels: n/a»). Исходный кадр не меняется."""
    H, W = frame.shape[:2]
    k = (W / float(wh[0]), H / float(wh[1])) if wh and (wh[0], wh[1]) != (W, H) and wh[0] and wh[1] else (1.0, 1.0)
    shown = [o for o in objs if not (o.get("name") == "glass" and not SHOW["glass"]) and not (o.get("name") == liquid_class and not SHOW["liquid"])]
    out = frame.copy()
    overlay = frame.copy()
    drew = False
    for o in shown:
        col = COL_LIQUID if o.get("name") == liquid_class else COL_GLASS
        for poly in o.get("polygons") or []:
            if len(poly) >= 3:
                pts = np.round(np.array(poly, float).reshape(-1, 2) * k).astype(np.int32)
                cv2.fillPoly(overlay, [pts.reshape(-1, 1, 2)], col)
                drew = True
    if drew:
        out = cv2.addWeighted(overlay, 0.45, out, 0.55, 0)
    if SHOW["boxes"]:
        for o in shown:
            bb = o.get("bbox")
            if not bb or len(bb) < 4:
                continue
            col = COL_LIQUID if o.get("name") == liquid_class else COL_GLASS
            x1, y1, x2, y2 = int(bb[0] * k[0]), int(bb[1] * k[1]), int(bb[2] * k[0]), int(bb[3] * k[1])
            cv2.rectangle(out, (x1, y1), (x2, y2), col, 2)
            if SHOW["labels"]:
                txt = "%s %.2f" % (o.get("name", ""), float(o.get("conf", 0)))
                cv2.putText(out, txt, (x1, max(16, y1 - 6)), FONT, 0.6, (0, 0, 0), 3, cv2.LINE_AA)
                cv2.putText(out, txt, (x1, max(16, y1 - 6)), FONT, 0.6, col, 1, cv2.LINE_AA)
    lvl = ("Total=%.1f%%  g1(low)=%.1f  g2(up)=%.1f" % (lv["total"], lv["glass1"], lv["glass2"])) if lv else "levels: n/a"
    on = lambda key: "ON" if SHOW[key] else "off"  # noqa: E731
    toggles = "G glass:%s  L liquid:%s  B box:%s  T text:%s" % (on("glass"), on("liquid"), on("boxes"), on("labels"))
    y = 26
    for line in (lvl, toggles):
        _put(out, line, y)
        y += 30
    return out


class FrameStore:
    """Потокобезопасное хранилище последних JPEG (сырой и размеченный). set_raw / set_annotated принимают кадр BGR любого размера:
    обрезка → размер → метка времени → JPEG.

    crop — (x1, y1, x2, y2) в пикселях ОРИГИНАЛА или None; значения зажимаются в кадр, пустая область — кадр как есть.
    output_size — (w, h) после обрезки или None; работает и без crop."""

    def __init__(self, jpeg_quality: int = 85, crop: Optional[tuple] = None, output_size: Optional[tuple] = None):
        self.jpeg_quality = int(jpeg_quality)
        self.crop = crop
        self.output_size = output_size
        self._params = [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality]
        self._lock = threading.Lock()
        self._raw, self._raw_ts = b"", 0.0
        self._ann, self._ann_ts = b"", 0.0

    def _prepare(self, frame: np.ndarray, ts: float) -> bytes:
        img = frame.copy()
        if self.crop:
            x1, y1, x2, y2 = self.crop
            H, W = img.shape[:2]
            x1, x2 = max(0, min(int(x1), W)), max(0, min(int(x2), W))
            y1, y2 = max(0, min(int(y1), H)), max(0, min(int(y2), H))
            if x2 > x1 and y2 > y1:
                img = img[y1:y2, x1:x2]
        if self.output_size:
            tw, th = self.output_size
            if tw > 0 and th > 0:
                H, W = img.shape[:2]
                interp = cv2.INTER_AREA if tw * th < W * H else cv2.INTER_LINEAR     # уменьшаем — по площади, увеличиваем — линейно
                img = cv2.resize(img, (int(tw), int(th)), interpolation=interp)
        stamp_time(img, ts)
        ok, buf = cv2.imencode(".jpg", img, self._params)
        return buf.tobytes() if ok else b""

    def set_raw(self, frame: np.ndarray, ts: float):
        data = self._prepare(frame, ts)
        if data:
            with self._lock:
                self._raw, self._raw_ts = data, ts

    def set_annotated(self, frame: np.ndarray, ts: float):
        data = self._prepare(frame, ts)
        if data:
            with self._lock:
                self._ann, self._ann_ts = data, ts

    def get_raw(self) -> tuple:
        with self._lock:
            return self._raw, self._raw_ts

    def get_annotated(self) -> tuple:
        with self._lock:
            return self._ann, self._ann_ts


INDEX_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>LVLDec — камера</title>
<style>
 body {{ font-family: system-ui, sans-serif; background:#111; color:#eee; margin:0; padding:12px; }}
 h1 {{ font-size: 18px; margin: 0 0 10px 0; }}
 .bar {{ margin-bottom: 10px; font-size: 14px; }}
 .bar button, .bar a {{ margin-right: 8px; color:#4fc3f7; background:none; border:1px solid #4fc3f7;
                         padding:4px 10px; border-radius:4px; cursor:pointer; text-decoration:none; font-size:14px; }}
 .bar button.active {{ background:#4fc3f7; color:#111; }}
 .bar button:hover, .bar a:hover {{ background:#4fc3f7; color:#111; }}
 img#cam {{ max-width:100%; border:1px solid #333; display:block; }}
 .hint {{ font-size:12px; color:#888; margin-top:6px; }}
</style></head>
<body>
<h1>LVLDec — камера (обновление раз в {refresh_sec:g} с)</h1>
<div class="bar">
  <button id="btnRaw" onclick="setMode('raw')">📷 Сырой</button>
  <button id="btnAnn" onclick="setMode('ann')">🎯 С разметкой</button>
  <a href="/healthz" target="_blank">healthz</a>
</div>
<img id="cam" src="/frame.jpg?ts=0" alt="камера">
<div class="hint">Прямые URL для SCADA: <code>/scada.jpg</code> (с разметкой, если она включена) · <code>/frame.jpg</code> (всегда сырой)</div>
<script>
 const img = document.getElementById('cam');
 const btnRaw = document.getElementById('btnRaw');
 const btnAnn = document.getElementById('btnAnn');
 let mode = localStorage.getItem('lvldec_mode') || '{default_mode}';
 function setMode(m) {{
   mode = m; localStorage.setItem('lvldec_mode', m);
   btnRaw.classList.toggle('active', m === 'raw');
   btnAnn.classList.toggle('active', m === 'ann');
 }}
 setMode(mode);
 function refresh() {{
   const url = (mode === 'ann') ? '/scada.jpg' : '/frame.jpg';
   img.src = url + '?ts=' + Date.now();
 }}
 setInterval(refresh, {refresh_ms});
 refresh();
</script>
</body></html>
"""

SCADA_HTML = """<!-- saved from url=(0014)about:internet -->
<!doctype html>
<html><head>
<meta charset="utf-8">
<meta http-equiv="X-UA-Compatible" content="IE=edge">
<meta http-equiv="refresh" content="{refresh_sec}">
<title>SCADA</title>
<style>
 html, body {{ margin:0; padding:0; width:100%; height:100%;
               background:#000; overflow:hidden; }}
 img {{ display:block; width:100%; height:100%; border:0; }}
</style></head>
<body><img id="x" src="{src}?t=0" alt="">
<script>
 var img = document.getElementById('x');
 setInterval(function() {{ img.src = '{src}?t=' + (+new Date); }}, {refresh_ms});
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    """Разбор GET. store / refresh_sec / annotate_on подставляет make_handler() — у каждой камеры свои."""
    store: FrameStore
    refresh_sec: float = 5.0
    annotate_on: bool = False

    def log_message(self, fmt, *args):
        if any(p in self.path for p in ("/frame.jpg", "/frame_annotated.jpg")):      # картинку просят постоянно — в журнал не пишем
            return
        log.debug("[webview] " + fmt, *args)

    def _send(self, code: int, ctype: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass                                              # зритель закрыл страницу, не дождавшись картинки

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            body = INDEX_HTML.format(refresh_sec=self.refresh_sec, refresh_ms=int(self.refresh_sec * 1000),
                                     default_mode="ann" if self.annotate_on else "raw").encode("utf-8")
            self._send(200, "text/html; charset=utf-8", body)
        elif path in ("/scada.html", "/scada.htm"):
            src = "/scada.jpg" if self.annotate_on else "/frame.jpg"
            body = SCADA_HTML.format(src=src, refresh_ms=int(self.refresh_sec * 1000),
                                     refresh_sec=max(1, int(round(self.refresh_sec)))).encode("utf-8")
            self._send(200, "text/html; charset=utf-8", body)
        elif path == "/frame.jpg":
            jpeg, _ = self.store.get_raw()
            if not jpeg:
                self._send(503, "text/plain; charset=utf-8", "нет кадра — камера ещё не готова".encode("utf-8"))
            else:
                self._send(200, "image/jpeg", jpeg)
        elif path in ("/frame_annotated.jpg", "/scada.jpg"):
            # /scada.jpg — адрес, на который смотрит SCADA: с разметкой, если annotate включён, иначе тот же чистый кадр, что и /frame.jpg
            # (в LVLDec без annotate он отвечал 503 — а SCADA на нём стоит и при выключенной разметке картинки не видела).
            # /frame_annotated.jpg — только размеченная, как в LVLDec.
            plain = path == "/scada.jpg" and not self.annotate_on
            jpeg, _ = self.store.get_raw() if plain else self.store.get_annotated()
            if not jpeg:
                msg = "нет кадра — камера ещё не готова" if plain else "аннотированный кадр ещё не готов"
                self._send(503, "text/plain; charset=utf-8", msg.encode("utf-8"))
            else:
                self._send(200, "image/jpeg", jpeg)
        elif path == "/healthz":
            _, raw_ts = self.store.get_raw()
            _, ann_ts = self.store.get_annotated()
            now = time.time()
            raw_age = now - raw_ts if raw_ts else -1
            ann_age = now - ann_ts if ann_ts else -1
            body = '{"ok":true,"raw_age_sec":%.2f,"annotated_age_sec":%.2f,"annotate":%s}' % (raw_age, ann_age, str(self.annotate_on).lower())
            self._send(200, "application/json", body.encode("utf-8"))
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    timeout = 0.25                    # handle_request() просыпается 4 раза в секунду проверить флаг остановки
    # На Windows SO_REUSEADDR даёт занять порт, который уже слушает другой процесс, — «порт занят» мы бы не заметили, а старый LVLDec.exe и наш
    # сервис молча делили бы его. Поэтому там повторное использование выключено (после остановки порт свободен сразу).
    allow_reuse_address = os.name != "nt"

    def __init__(self, *a, **kw):
        self._halt = threading.Event()
        super().__init__(*a, **kw)

    def serve(self):
        # свой цикл вместо serve_forever(): его shutdown() виснет навсегда, если позвать раньше, чем поток начал работать
        while not self._halt.is_set():
            self.handle_request()

    def shutdown(self):
        self._halt.set()


def make_handler(store: FrameStore, refresh_sec: float, annotate_on: bool) -> type:
    """Свой класс обработчика на каждый сервер: у камер разные хранилища и периоды."""
    return type("WebviewHandler", (Handler,), {"store": store, "refresh_sec": float(refresh_sec), "annotate_on": bool(annotate_on)})


def start_server(host: str, port: int, store: FrameStore, refresh_sec: float, annotate_on: bool) -> tuple:
    """Поднять HTTP в потоке-демоне. (server, thread); OSError — порт занят или нет прав. Остановка: stop_server(server, thread)."""
    srv = _Server((host, int(port)), make_handler(store, refresh_sec, annotate_on))
    t = threading.Thread(target=srv.serve, name="webview-%s" % port, daemon=True)
    t.start()
    return srv, t


def stop_server(srv: ThreadingHTTPServer, thread: Optional[threading.Thread]):
    """Остановить сервер и освободить порт."""
    srv.shutdown()
    if thread is not None:
        thread.join(2.0)
    srv.server_close()


def crop_of(cfg: dict) -> Optional[tuple]:
    c = (cfg["crop_x1"], cfg["crop_y1"], cfg["crop_x2"], cfg["crop_y2"])
    return c if c[2] > c[0] and c[3] > c[1] else None


def output_of(cfg: dict) -> Optional[tuple]:
    return (cfg["output_w"], cfg["output_h"]) if cfg["output_w"] > 0 and cfg["output_h"] > 0 else None


class WebView(threading.Thread):
    """Раз в update_sec кладёт свежий кадр камеры в хранилище и держит HTTP-сервер этой камеры.

    get_source() -> (кадр, данные для разметки | None) или None, если свежего кадра нет (камера молчит дольше 5 с — картинка остаётся
    последней хорошей, её метка времени и /healthz показывают, что она старая). Данные разметки: {objs, wh, liquid_class, lv}.
    halt — общий стоп камеры. Порт занят → предупреждение и повторы (BIND_RETRY_S): пока старый LVLDec.exe держит порт, мы ждём."""

    def __init__(self, cam_id: str, cfg: dict, get_source: Callable[[], Optional[tuple]], halt: threading.Event):
        super().__init__(daemon=True, name="webview-" + cam_id)
        self.cam_id, self.cfg, self.get_source, self.halt = cam_id, cfg, get_source, halt
        self.store = FrameStore(cfg["jpeg_quality"], crop_of(cfg), output_of(cfg))
        self.srv: Optional[ThreadingHTTPServer] = None
        self._srv_thread: Optional[threading.Thread] = None
        self.error = ""
        self._tries = 0
        self._next_bind = 0.0
        self._warned = 0.0

    def status(self) -> dict:
        _, ts = self.store.get_raw()
        return {"enabled": True, "port": self.cfg["port"], "listening": self.srv is not None, "error": self.error,
                "age_s": round(time.time() - ts, 1) if ts else None}

    def _bind(self, now: float):
        if self.srv is not None or now < self._next_bind:
            return
        c = self.cfg
        try:
            self.srv, self._srv_thread = start_server(c["host"], c["port"], self.store, c["update_sec"], c["annotate"])
            self.error, self._tries = "", 0
            log.info("[%s] картинка для SCADA: http://%s:%d/  (кадр раз в %g с)", self.cam_id, c["host"], c["port"], c["update_sec"])
        except OSError as e:
            self._next_bind = now + BIND_RETRY_S[min(self._tries, len(BIND_RETRY_S) - 1)]
            self._tries += 1
            if self._tries >= 2:                              # первая неудача тихая (см. BIND_RETRY_S)
                if not self.error:
                    log.warning("[%s] порт %d для картинки не открылся (%s) — повторю", self.cam_id, c["port"], e)
                self.error = "порт %d не открылся: %s" % (c["port"], e)

    def step(self, now: Optional[float] = None) -> bool:
        """Один круг: (при необходимости) открыть порт и положить свежий кадр. True — кадр обновлён."""
        now = time.time() if now is None else now
        self._bind(now)
        src = self.get_source()
        if src is None:
            return False
        frame, extra = src
        if frame.ndim == 2:
            frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        self.store.set_raw(frame, now)
        if self.cfg["annotate"]:
            ex = extra or {}
            lv = ex.get("lv")
            self.store.set_annotated(draw_overlay(frame, ex.get("objs") or [], ex.get("wh"), ex.get("liquid_class") or "liquid", lv), now)
        return True

    def run(self):
        while not self.halt.is_set():
            ok = False
            try:
                ok = self.step()
            except Exception as e:                           # поток не должен умирать от одного плохого кадра
                if time.time() - self._warned > 60:
                    self._warned = time.time()
                    log.warning("[%s] картинка для SCADA: сбой при обновлении JPEG: %s", self.cam_id, e)
            self.halt.wait(self.cfg["update_sec"] if ok else 1.0)      # кадра нет — смотрим чаще, чтобы первая картинка появилась сразу
        self.close()

    def close(self):
        srv, self.srv = self.srv, None
        if srv is not None:
            try:
                stop_server(srv, self._srv_thread)
            except Exception:                                # noqa: BLE001 — при остановке сервера ошибки не важны
                pass
