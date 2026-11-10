"""«Плюс» стадии цикла пробы (вкладка «Цикл»): по умолчанию 20 УНВ, правится списком; пустой список — только диапазон «от…до»."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import microscope_fsm as F  # noqa: E402


def test_parse_stage_list():
    assert F.parse_stage_list(None, (20,)) == (20,)                         # ключа нет — умолчание
    assert F.parse_stage_list("") == () and F.parse_stage_list([]) == ()   # пусто — без «плюс»
    assert F.parse_stage_list("21, 20;20 99 0 x") == (20, 21)               # мусор и вне 1..21 — мимо, повторы убраны
    assert F.parse_stage_list([20, "21"]) == (20, 21)
    assert F.parse_stage_list(8) == (8,)


def test_default_extra_is_unv_and_api_param():
    import plate_config
    assert plate_config.DEFAULTS["probe_cycle"]["stage_extra"] == [20] and F.CYCLE_EXTRA_STAGES == (20,)


def test_stage_in_uses_range_and_extra():
    f = object.__new__(F.MicroscopeFSM)
    f._stage_min, f._stage_max, f._stage_extra = 3, 9, (20, 21)
    ok = lambda st: (setattr(f, "stage", st), f._stage_in())[1]
    assert [ok(s) for s in (2, 3, 9, 10, 20, 21)] == [False, True, True, False, True, True]
    f._stage_extra = ()                                                      # «плюс» выключен — УНВ в цикл не идёт
    assert [ok(s) for s in (9, 20, 21)] == [True, False, False]
