"""Карта стадий варки: код стадии чужого контроллера -> НАША стадия (M.mode 1..21).

Вся программа (автомат пробы, отчёт, журнал, sysmon, видео) живёт на наших кодах: проба при 3..9, подкачка = 5, рост = 7 …
Поэтому чужой код переводится в наш ОДИН раз, на входе (sv_source), а дальше всё работает как есть.

Настройка — plate_config.sv_source.stage_map:
  profile = "ours"   — контроллер наш, код стадии идёт как есть (по умолчанию);
  profile = "custom" — контроллер чужой, переводим по таблице rows [{code, name, to}] (пусто — DEFAULT_ROWS).
Кода нет в таблице — стадии нет (None), не угадываем.
"""
from __future__ import annotations

PROFILES = ("ours", "custom")
OUR_MIN, OUR_MAX = 1, 21
ROWS_MAX = 200

# Чужая схема (дал Макс 2026-11-10): код -> (название у них, наша стадия).
# 50 КРИСТАЛЛИЗАЦИЯ -> 5 подкачка, 60..62 ВАРКА -> 7 рост, 70/71 ИСТОЩЕНИЕ -> 8 уваривание; всё до набора -> 1 останов.
_DEFAULT = (
    ((1,), "СТАРТ ШАГ", 1),
    ((10, 11), "ПРЕДВ. ВАКУУМ", 1),
    ((20, 21, 22), "НАБОР", 2),
    ((30, 31, 32), "СГУЩЕНИЕ", 3),
    ((40,), "ЗАТРАВКА СЛАРРИ", 4),
    ((44,), "ЗАТРАВКА МУ", 4),
    ((50,), "КРИСТАЛЛИЗАЦИЯ", 5),
    ((60, 61, 62), "ВАРКА", 7),
    ((70, 71), "ИСТОЩЕНИЕ", 8),
    ((80, 81, 82, 83), "ВЫГРУЗКА", 10),
    ((90, 91, 92, 93), "ПРОПАРКА", 11),
)
DEFAULT_ROWS = [{"code": c, "name": n, "to": to} for codes, n, to in _DEFAULT for c in codes]


def profile_of(cfg: dict | None) -> str:
    """Профиль из раздела sv_source (мусор — «ours»)."""
    p = str(((cfg or {}).get("stage_map") or {}).get("profile") or "ours").lower()
    return p if p in PROFILES else "ours"


def rows_of(cfg: dict | None) -> list[dict]:
    """Строки таблицы: сохранённые, иначе умолчательные. Битые строки пропускаем."""
    saved = ((cfg or {}).get("stage_map") or {}).get("rows")
    out = []
    for r in saved if isinstance(saved, list) else []:
        try:
            out.append({"code": int(r["code"]), "name": str(r.get("name") or "")[:40], "to": int(r["to"])})
        except (KeyError, TypeError, ValueError):
            continue
    return out or [dict(r) for r in DEFAULT_ROWS]


class StageMap:
    """Готовый переводчик для SvSource: translate(code) -> (наша стадия | None, название у них | None)."""

    def __init__(self, cfg: dict | None):
        self.custom = profile_of(cfg) == "custom"
        self._by_code = {r["code"]: r for r in rows_of(cfg)} if self.custom else {}

    def translate(self, code):
        if not self.custom or code is None:
            return code, None
        r = self._by_code.get(int(code))
        return (r["to"], r["name"] or None) if r else (None, None)


def normalize(body) -> dict:
    """Тело со страницы -> раздел stage_map для plate_config. Ошибка -> ValueError (текст по-русски)."""
    if not isinstance(body, dict):
        raise ValueError("карта стадий: ожидался объект")
    profile = str(body.get("profile") or "ours").lower()
    if profile not in PROFILES:
        raise ValueError("карта стадий: наша или другая")
    out: dict = {"profile": profile}
    if "rows" in body:
        rows, seen = [], set()
        for r in body["rows"] if isinstance(body["rows"], list) else []:
            if not isinstance(r, dict):
                continue
            try:
                code, to = int(r.get("code")), int(r.get("to"))
            except (TypeError, ValueError):
                raise ValueError("карта стадий: код и наша стадия — целые числа")
            if code < 0 or code > 65535:
                raise ValueError("карта стадий: код %d вне 0…65535" % code)
            if not OUR_MIN <= to <= OUR_MAX:
                raise ValueError("карта стадий: наша стадия для кода %d — от %d до %d" % (code, OUR_MIN, OUR_MAX))
            if code in seen:
                raise ValueError("карта стадий: код %d повторяется" % code)
            seen.add(code)
            rows.append({"code": code, "name": str(r.get("name") or "").strip()[:40], "to": to})
        if len(rows) > ROWS_MAX:
            raise ValueError("карта стадий: не больше %d строк" % ROWS_MAX)
        out["rows"] = sorted(rows, key=lambda r: r["code"])
    return out


def public(cfg: dict | None) -> dict:
    """Для страницы: профиль, строки (сохранённые или умолчательные), умолчательные для кнопки «сбросить»."""
    return {"profile": profile_of(cfg), "rows": rows_of(cfg), "default_rows": [dict(r) for r in DEFAULT_ROWS]}
