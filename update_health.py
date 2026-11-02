"""Проверка «всё ли поднялось» после обновления: плата, ПЛК, камеры, сервисы, автомат, новые красные тревоги.

capture() — снимок «до перезапуска» (пишет автообновление перед запуском update.ps1); evaluate() — сверка после старта.
Обновили кнопкой и снимка нет — остаются абсолютные проверки (плата, ПЛК, сервисы): «было/стало» сравнивать не с чем.
Источники — функции без аргументов, только чтение; тесты подменяют их целиком (см. default_sources).
"""
from typing import Optional

GOOD_SERVICE = ("healthy",)
PENDING_SERVICE = ("started",)
BAD_SERVICE = ("gave_up", "spawn_error")
SERVICE_NAMES = {"cv": "CV", "video": "видео", "motion": "КЗ"}


def default_sources() -> dict:
    """Откуда берём данные (всё лениво: модуль можно импортировать без железа и приложения)."""
    import plate_config
    import services
    import sysmon
    from camera_core import manager
    from microscope_service import micro

    def rtsp_running() -> int:
        with manager._registry_lock:                       # копия реестра под замком: воркеры создаются из запросов
            ws = list(manager.rtsp_workers.values())
        return sum(1 for w in ws if getattr(w, "running", False))

    def camera_streaming() -> bool:
        serial = (plate_config.load().get("camera_serial") or "").strip()
        w = manager.workers.get(serial) if serial else None
        return bool(w is not None and w.running)

    def plc_connected() -> Optional[bool]:
        st = micro.sv_status()
        return bool(st.get("connected")) if st.get("enabled") else None

    def alerts() -> dict:
        ag = sysmon.agent()
        return {c: a for c, a in (ag.active.items() if ag is not None else [])}

    return {
        "micro_on": lambda: services.micro_enabled(plate_config.load()),
        "plate_connected": lambda: bool(micro.status().get("connected")),
        "plc_connected": plc_connected,
        "camera_streaming": camera_streaming,
        "rtsp_running": rtsp_running,
        "services": services.states,
        "automat": lambda: (bool(micro.state().get("cyclic")), bool(micro.state().get("manual"))),
        "alerts": alerts,
    }


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def capture(src: dict) -> dict:
    """Что работало ДО перезапуска (JSON-совместимо — уходит в auto_update.json)."""
    cyc, _man = _safe(src["automat"], (False, False))
    svc = _safe(src["services"], {}) or {}
    return {"micro_on": bool(_safe(src["micro_on"], False)),
            "camera_streaming": bool(_safe(src["camera_streaming"], False)),
            "rtsp_running": int(_safe(src["rtsp_running"], 0) or 0),
            "services_ok": sorted(k for k, v in svc.items() if v in GOOD_SERVICE),
            "cyclic": bool(cyc),
            "alerts": sorted(_safe(src["alerts"], {}) or {})}


def evaluate(src: dict, pre: Optional[dict]) -> dict:
    """Сверка после старта. Возвращает {"ok": [текст ✓...], "bad": [причины], "done": bool}.
    done — всё в порядке (можно сообщать сразу); bad не пуст — повторим позже: камеры подключаются со страницы, сервисы поднимаются не сразу."""
    ok, bad = [], []
    micro_on = bool(_safe(src["micro_on"], False))
    if micro_on:
        if _safe(src["plate_connected"], False):
            ok.append("плата ✓")
        else:
            bad.append("плата микроскопа не отвечает")
        plc = _safe(src["plc_connected"], None)
        if plc is True:
            ok.append("ПЛК ✓")
        elif plc is False:
            bad.append("ПЛК не отвечает")
    if pre:
        if pre.get("camera_streaming"):
            if _safe(src["camera_streaming"], False):
                ok.append("камера ✓")
            else:
                bad.append("камера микроскопа не стримит (до обновления стримила)")
        n = int(pre.get("rtsp_running") or 0)
        if n:
            now = int(_safe(src["rtsp_running"], 0) or 0)
            if now >= n:
                ok.append("RTSP %d из %d ✓" % (now, n))
            else:
                bad.append("RTSP-камер подключено %d из %d" % (now, n))
        if micro_on and pre.get("cyclic"):
            cyc, man = _safe(src["automat"], (False, False))
            if cyc and not man:
                ok.append("автомат ✓")
            else:
                bad.append("автомат не включён" if not cyc else "автомат в ручном режиме (до обновления был включён)")
    svc = _safe(src["services"], {}) or {}
    want = set(pre.get("services_ok") or []) if pre else {k for k, v in svc.items() if v in GOOD_SERVICE + PENDING_SERVICE + BAD_SERVICE}
    checked = 0
    for name in sorted(want):
        state = svc.get(name)
        label = SERVICE_NAMES.get(name, name)
        if state in GOOD_SERVICE:
            checked += 1
        else:
            bad.append("сервис %s %s" % (label, "ещё запускается" if state in PENDING_SERVICE else "не отвечает"))
    if checked and checked == len(want):
        ok.append("сервисы ✓")
    if pre:
        alerts = _safe(src["alerts"], {}) or {}
        new = [a.get("text", c) for c, a in alerts.items() if c not in (pre.get("alerts") or []) and a.get("level") in ("critical", "high")]
        if new:
            bad.append("новая красная тревога: %s" % "; ".join(str(t)[:80] for t in new[:2]))
        else:
            ok.append("тревог нет ✓")
    return {"ok": ok, "bad": bad, "done": not bad}


def verdict_text(frm: str, to: str, res: dict, final: bool) -> tuple:
    """(state, текст) итогового сообщения: ok — всё поднялось, warn — что-то нет."""
    head = "Обновлено %s → %s" % (frm, to) if frm else "Обновлено до v%s" % to
    if res["done"]:
        return "ok", head + (" · " + " · ".join(res["ok"]) if res["ok"] else "")
    text = head + ", но не всё поднялось: " + "; ".join(res["bad"])
    if res["ok"]:
        text += " · в порядке: " + ", ".join(x.replace(" ✓", "") for x in res["ok"])
    return "warn", text
