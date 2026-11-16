"""Чёрный ящик М1: «Поиск 0» не даёт ложной тревоги, настоящий уход в ноль — даёт."""
import m1_blackbox as bb


def _raw(calc, enc=None):
    r = [0] * 16
    r[4] = calc
    r[15] = calc if enc is None else enc
    return r


def _box(tmp_path):
    events = []
    return bb.M1BlackBox(tmp_path / "m1.log", on_event=events.append), events


def _run(box, calc, pc, ticks):
    for _ in range(ticks):
        box.feed(_raw(calc), {}, pc)


def test_real_drop_to_glass_alarms(tmp_path):
    box, ev = _box(tmp_path)
    _run(box, 200, 200, 5)               # стоит на отводе
    _run(box, 0, 200, bb.GLASS_HOLD_TICKS + 2)   # ушла в ноль, цель 200
    assert len(ev) == 1 and "у стекла при цели ПК 200" in ev[0]


def test_short_touch_does_not_alarm(tmp_path):
    box, ev = _box(tmp_path)
    _run(box, 200, 200, 5)
    _run(box, 0, 200, bb.GLASS_HOLD_TICKS - 5)   # меньше 3 с
    assert ev == []


def test_find_zero_is_not_alarm_and_rearms(tmp_path):
    box, ev = _box(tmp_path)
    _run(box, 200, 200, 5)
    box.expect_glass()                   # ПК послал «Поиск 0»
    _run(box, 0, 200, bb.GLASS_HOLD_TICKS + 20)  # голова долго у стекла, в цели прежний отвод
    assert ev == []
    _run(box, 200, 200, 5)               # отвели -> перевзвод
    _run(box, 0, 200, bb.GLASS_HOLD_TICKS + 2)   # настоящий уход после этого — снова ловим
    assert len(ev) == 1
