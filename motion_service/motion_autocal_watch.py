"""Сторож видимости палок: сам запускает автоподгонку валов, когда палки стали находиться хуже порога, и сам откатывает её, если стало хуже.

Зачем: после сдвига камеры или пересоздания зон цвета сбрасываются на заводские, и палка находится в 50–65 % времени — пока кто-то не нажмёт
«Подогнать валы». Хорошо подогнанные настройки держат 95–100 % сутки напролёт, так что подгонка нужна не по часам, а по факту просадки.

Как: раз в check_min минут (30) считаем видимость каждой палки камеры за последнее окно (секундные тренды на диске). Ниже порога (95 %) и
кулдаун прошёл → запускаем обычное задание подгонки (motion_autocal_job) на просевших палках. Результат применяем сами, только если на кадрах,
не участвовавших в подборе, найденность выросла минимум на 2 п.п. (поверх проверки подгонки). После применения ждём окно и сверяем: если палка
находится хуже, чем ДО подгонки, возвращаем прежние настройки и на 6 часов оставляем камеру в покое. Если настройки палки меняли руками
(во время подбора или до отката), их не трогаем.

Всё, что произошло, — в событиях: память (отдаются в /state и идут в журнал приложения), data\\autocal_watch.jsonl (для отчёта день/ночь) и журнал сервиса.
Состояние (фаза, кулдаун, что применено) лежит в data\\autocal_watch.json и переживает перезапуск сервиса.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("motion")

TICK_S = 30.0
WARMUP_S = 90.0                  # после старта сервиса не проверяем: камеры поднимаются, тренд ещё не пишется
SEED_SECONDS = 150               # длина записи вращения для подгонки (≈2 оборота)
MIN_DATA_FRAC = 0.6              # в окне проверки должно быть не меньше 60 % секунд с картинкой, иначе судить рано
MIN_FOUND_GAIN = 0.02            # найденность на отложенных кадрах должна вырасти минимум на 2 п.п., чтобы применить
VERIFY_SETTLE_S = 120.0          # после применения камера перезапускается — первые минуты в сверку не берём
VERIFY_GIVE_UP_S = 7200.0        # данных для сверки так и нет — оставляем применённое, откат не делаем
ROLLBACK_MARGIN_PP = 1.0         # откат, если после подгонки видимость ниже прежней больше чем на столько п.п.
ROLLBACK_COOLDOWN_H = 6.0
ERROR_COOLDOWN_S = 1800.0
JOB_LIMIT_S = 1800.0             # подгонка идёт дольше — прерываем
ZONE_KEYS = ("center", "radius", "red_hsv", "blue_hsv", "thin", "blue_alone")
TIP_ZONE_KEYS = ("tip_hsv", "tip_len", "tip_r0", "tip_r1", "tip_thin")     # отражатели: учитываем только у зон, где они заданы (иначе [] против «нет» — ложная «ручная правка»)
EVENTS_KEEP = 200
JSONL_KEEP = 3000                # строк в data\autocal_watch.jsonl (при переполнении режем до половины)
JOURNAL_KINDS = ("start", "apply", "skip", "rollback", "confirm", "error")   # «check» — тихая проверка: в журнал приложения не идёт


def default_watch() -> dict:
    return {"on": False, "min_pct": 95.0, "check_min": 30.0, "cooldown_h": 2.0}


def zone_params(z: dict) -> dict:
    """Настройки зоны, которые меняет подгонка, — копией."""
    keys = ZONE_KEYS + (TIP_ZONE_KEYS if z.get("tip_hsv") else ())
    return json.loads(json.dumps({k: z.get(k) for k in keys}))


class AutocalWatch:
    def __init__(self, mgr, clock: Callable[[], float] = time.time, vis_fn: Optional[Callable] = None, stand_fn: Optional[Callable] = None):
        self.mgr = mgr
        self.clock = clock
        self.vis_fn = vis_fn or self._store_vis          # (камера, [зоны], t0, t1) -> {зона: (секунд с данными, секунд «найдена»)}
        self.stand_fn = stand_fn or self._standing       # камера -> стоит ли сейчас хоть один её вал
        self._stand_at: dict = {}                        # камера -> когда в последний раз видели стоящий вал (только в памяти: после перезапуска первый же проход заново)
        self.path = Path(mgr.dir) / "autocal_watch.json"
        self.log_path = Path(mgr.dir) / "autocal_watch.jsonl"
        self.st: dict = {}
        self.events: deque = deque(maxlen=EVENTS_KEEP)
        self._seq = 0
        self._appended = 0
        self._saved = ""
        self._started = clock()
        self._lock = threading.RLock()
        self._halt = threading.Event()
        self._th: Optional[threading.Thread] = None
        self._load()

    # --- состояние и события ---
    def _load(self):
        try:
            self.st = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.st = {}
        for s in self.st.values():
            if s.get("phase") == "running":              # подгонка жила в памяти сервиса и пропала с перезапуском
                s["phase"] = "idle"

    def _save(self):
        try:
            text = json.dumps(self.st, ensure_ascii=False)
            if text == self._saved:
                return                                    # проход раз в 30 с, состояние чаще всего то же — диск не трогаем
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            self._saved = text
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("состояние сторожа подгонки не записано: %s", e)

    def _cam_state(self, cid: str) -> dict:
        return self.st.setdefault(cid, {"phase": "idle", "next_check": 0.0, "cooldown_until": 0.0, "pct": {}, "pct_at": 0.0})

    def _event(self, cid: str, kind: str, msg: str, level: str = "info", **data):
        now = self.clock()
        self._seq = max(self._seq + 1, int(now * 1000))
        ev = {"id": self._seq, "ts": round(now, 1), "cam": cid, "kind": kind, "level": level, "msg": msg, "data": data}
        self.events.append(ev)
        if kind != "check":
            (log.warning if level == "warn" else log.info)("[%s] автоподгонка: %s", cid, msg)
        try:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            self._appended += 1
            if self._appended >= 500:
                self._appended = 0
                self._trim_log()
        except OSError as e:
            log.warning("журнал сторожа подгонки не записан: %s", e)

    def _trim_log(self):
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
            if len(lines) > JSONL_KEEP:
                self.log_path.write_text("\n".join(lines[-JSONL_KEEP // 2:]) + "\n", encoding="utf-8")
        except OSError:
            pass

    # recent() и snapshot() без замка: tick() держит его, пока применяет/откатывает настройки (перезапуск потока камеры — секунды),
    # а /state опрашивается каждую секунду и не должен на это время вставать. Копии берутся целиком одним вызовом (list/json в C), без перебора на ходу.
    def recent(self, since_id: int = 0) -> list:
        """События для журнала приложения (без тихих проверок), новее since_id."""
        return [dict(e) for e in list(self.events) if e["id"] > since_id and e["kind"] in JOURNAL_KINDS]

    def snapshot(self) -> dict:
        try:
            cams = json.loads(json.dumps(self.st))
        except (RuntimeError, ValueError):
            cams = {}
        return {"cameras": cams, "events": [dict(e) for e in list(self.events)[-60:]]}

    # --- данные ---
    def _store_vis(self, cid: str, zids: list, t0: float, t1: float) -> dict:
        import motion_store
        raw = motion_store.store_for(self.mgr.dir).visibility(cid, set(zids), t0, t1, int(t1 - t0) + 1)
        return {z: (sum(n for n, _ in rows.values()), sum(f for _, f in rows.values())) for z, rows in raw.items()}

    def _pct(self, cid: str, zids: list, t0: float, t1: float, min_frac: float) -> dict:
        """{зона: видимость %} за [t0, t1]; зоны, где секунд с картинкой меньше min_frac окна, — без значения."""
        need = min_frac * (t1 - t0)
        return {z: round(100.0 * f / n, 1) for z, (n, f) in self.vis_fn(cid, zids, t0, t1).items() if n and n >= need}

    @staticmethod
    def _zones(cam: dict) -> list:
        return [z for z in cam["zones"] if z["type"] == "cycle" and z["enabled"]]

    def _standing(self, cid: str) -> bool:
        """Хоть один вал камеры сейчас стоит (по скорости: за 10 с угол сдвинулся меньше 5°). Неизвестно (палку не видно) — не считаем стоящим."""
        w = (getattr(self.mgr, "workers", None) or {}).get(cid)
        if w is None:
            return False
        zs = (((w.state() or {}).get("result")) or {}).get("zones") or []
        return any(z.get("type") == "cycle" and z.get("moving") is False for z in zs)

    def _stood_within(self, cid: str, since: float) -> bool:
        """Вал камеры стоял после момента since. На остановке палки не находятся (видимость падает сама), и по ней судить нельзя."""
        t = self._stand_at.get(cid)
        return t is not None and t > since

    # --- ход ---
    def tick(self, now: Optional[float] = None):
        now = self.clock() if now is None else now
        with self._lock:
            for cam in list(self.mgr.cfg["cameras"]):
                cw = cam.get("autocal_watch") or {}
                if not cw.get("on"):
                    continue
                cid = cam["id"]
                st = self._cam_state(cid)
                try:
                    if self.stand_fn(cid):
                        self._stand_at[cid] = now
                    getattr(self, "_" + st["phase"])(cam, cw, st, now)
                except Exception as e:  # noqa: BLE001 — сторож вспомогательный: любая поломка не должна ронять сервис
                    log.exception("[%s] сторож подгонки: сбой", cid)
                    st["phase"], st["cooldown_until"] = "idle", now + ERROR_COOLDOWN_S
                    self._event(cid, "error", "сбой сторожа: %s" % (str(e) or e.__class__.__name__), "warn")
                self._save()

    def _idle(self, cam: dict, cw: dict, st: dict, now: float):
        if now - self._started < WARMUP_S or now < st["next_check"]:
            return
        cid, window = cam["id"], float(cw["check_min"]) * 60.0
        st["next_check"] = now + window
        zones = self._zones(cam)
        if not zones:
            return
        if self._stood_within(cid, now - window):              # вал стоял или стоит: видимость в окне просела из-за остановки, а не из-за цветов
            st["next_check"] = max(now + 300.0, self._stand_at[cid] + window)
            self._event(cid, "check", "вал стоял в последние %d мин — по видимости не сужу, подгонку не запускаю" % round(window / 60.0), "info")
            return
        pct = self._pct(cid, [z["id"] for z in zones], now - window, now, MIN_DATA_FRAC)
        st["pct"], st["pct_at"] = pct, now
        if not pct:
            self._event(cid, "check", "мало данных для проверки", "info")
            return
        bad = [zid for zid, p in pct.items() if p < float(cw["min_pct"])]
        if not bad:
            self._event(cid, "check", "палки находятся в норме", "info", pct=pct)
            return
        if now < st["cooldown_until"]:
            self._event(cid, "check", "видимость ниже порога, но ещё кулдаун", "info", pct=pct, until=st["cooldown_until"])
            return
        try:
            self.mgr.autocal_start(cid, bad, SEED_SECONDS)
        except (KeyError, ValueError, RuntimeError) as e:     # нет камеры / нечего подгонять / другая подгонка идёт
            self._event(cid, "check", "подгонку не запустил: %s" % e, "info", pct=pct)
            st["next_check"] = now + 300.0
            return
        names = {z["id"]: z["name"] for z in zones}
        st.update(phase="running", started=now, zones=bad, before={z: pct[z] for z in bad}, hour=time.localtime(now).tm_hour)
        self._event(cid, "start", "видимость ниже %g %% (%s) — запускаю подгонку, запись %d с" % (
            float(cw["min_pct"]), ", ".join("%s %g %%" % (names.get(z, z), pct[z]) for z in bad), SEED_SECONDS), "info", pct=pct, zones=bad)

    def _running(self, cam: dict, cw: dict, st: dict, now: float):
        cid = cam["id"]
        job = self.mgr.autocal.get(cid)
        d = job.status() if job else None
        if d is None or d["started"] < st["started"] - 5:                        # заданье пропало (перезапуск) или это чужое, старое
            st["phase"] = "idle"
            return
        if d["state"] in ("starting", "recording", "tuning"):
            if now - st["started"] > JOB_LIMIT_S:
                job.cancel()
                st.update(phase="idle", cooldown_until=now + ERROR_COOLDOWN_S)
                self._event(cid, "error", "подгонка шла дольше %d мин — прервал" % (JOB_LIMIT_S // 60), "warn")
            return
        if d["state"] != "done":
            st.update(phase="idle", cooldown_until=now + ERROR_COOLDOWN_S)
            self._event(cid, "error", "подгонка не вышла: %s" % (d.get("error") or d["state"]), "warn")
            return
        cooldown = now + float(cw["cooldown_h"]) * 3600.0
        zones = {z["id"]: z for z in cam["zones"]}
        cand, notes = [], []
        for zid in st["zones"]:
            r = d["results"].get(zid)
            if not r:
                continue
            gain = r["after_val"]["found"] - r["before_val"]["found"]
            name = r.get("name") or zid
            if not (r["improved"] and gain >= MIN_FOUND_GAIN):
                notes.append("%s: лучше не стало (найдена %d → %d %%)" % (name, round(100 * r["before_val"]["found"]), round(100 * r["after_val"]["found"])))
            elif zone_params(zones.get(zid, {})) != zone_params(r["old"]):
                notes.append("%s: настройки меняли вручную во время подбора — не трогаю" % name)
            else:
                cand.append(zid)
        if not cand:
            st.update(phase="idle", cooldown_until=cooldown)
            self._event(cid, "skip", "подгонка ничего не улучшила, настройки не меняю. " + "; ".join(notes), "info")
            return
        res = self.mgr.autocal_apply(cid, cand)
        applied = res["applied"]
        if not applied:
            st.update(phase="idle", cooldown_until=cooldown)
            self._event(cid, "skip", "результат применить не вышло", "warn")
            return
        cam_now = next(c for c in self.mgr.cfg["cameras"] if c["id"] == cid)
        now_z = {z["id"]: z for z in cam_now["zones"]}
        st.update(phase="verifying", applied_at=now, applied=applied, cooldown_until=cooldown, next_verify=0.0,
                  old={z: d["results"][z]["old"] for z in applied}, new={z: zone_params(now_z[z]) for z in applied})
        parts = ["%s: найдена %d → %d %%" % (d["results"][z].get("name") or z, round(100 * d["results"][z]["before_val"]["found"]),
                                             round(100 * d["results"][z]["after_val"]["found"])) for z in applied]
        self._event(cid, "apply", "применил новые настройки палок (%s); через %d мин сверю" % ("; ".join(parts), int(float(cw["check_min"]))), "info",
                    zones=applied, old=st["old"], new=st["new"], before=st["before"], notes=notes, hour=st.get("hour"))

    def _verifying(self, cam: dict, cw: dict, st: dict, now: float):
        cid, window = cam["id"], float(cw["check_min"]) * 60.0
        t0 = max(st["applied_at"] + VERIFY_SETTLE_S, self._stand_at.get(cid, 0.0))     # остановка: на ней палки не находятся, «после» просело бы само и откат был бы ложным — окно сверки начинаем заново
        if now < t0 + window or now < st.get("next_verify", 0.0):
            return
        applied = st["applied"]
        after = self._pct(cid, applied, t0, now, MIN_DATA_FRAC * window / max(window, now - t0))
        if len(after) < len(applied) and now - st["applied_at"] < VERIFY_GIVE_UP_S:
            st["next_verify"] = now + 300.0                      # картинки в окне мало (обрыв?) — подождём ещё
            return
        before = st.get("before", {})
        worse = [z for z, p in after.items() if z in before and p < before[z] - ROLLBACK_MARGIN_PP]
        st["phase"], st["next_check"] = "idle", now + window
        if not after:
            self._event(cid, "confirm", "для сверки не набралось данных — оставляю новые настройки", "info")
            return
        if not worse:
            st["pct"], st["pct_at"] = {**st.get("pct", {}), **after}, now
            self._event(cid, "confirm", "сверка прошла: " + ", ".join("%s %g → %g %%" % (z, before.get(z, float("nan")), p) for z, p in after.items()),
                        "info", before=before, after=after)
            return
        cfg = json.loads(json.dumps(self.mgr.cfg))
        cam_cfg = next(c for c in cfg["cameras"] if c["id"] == cid)
        done, kept = [], []
        for z in cam_cfg["zones"]:
            if z["id"] in worse:
                if zone_params(z) == st["new"][z["id"]]:
                    z.update(st["old"][z["id"]])
                    done.append(z["id"])
                else:
                    kept.append(z["id"])
        if done:
            self.mgr.apply(cfg)
        st["cooldown_until"] = now + ROLLBACK_COOLDOWN_H * 3600.0
        msg = "после подгонки хуже, чем до (%s) — " % ", ".join("%s %g → %g %%" % (z, before[z], after[z]) for z in worse)
        self._event(cid, "rollback", msg + ("вернул прежние настройки" if done else "") + ("; %s правили вручную — не трогаю" % ", ".join(kept) if kept else ""),
                    "warn", before=before, after=after, zones=done, kept=kept)

    # --- поток ---
    def start(self):
        if self._th and self._th.is_alive():
            return
        self._halt.clear()
        self._th = threading.Thread(target=self._run, daemon=True, name="autocal-watch")
        self._th.start()

    def stop(self):
        self._halt.set()
        if self._th and self._th.is_alive() and self._th is not threading.current_thread():
            self._th.join(5)

    def _run(self):
        while not self._halt.wait(TICK_S):
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("сторож подгонки: сбой прохода")
