"""Средний размер кристалла в рассеве (13.11): по каждой модели объёма (M1–M3) и по площади — средний диаметр с весом объёма / площади
(средний кристалл «по массе», как по ситам в лаборатории). Считается по хорошим кристаллам; брак, обрезанные и пузыри не входят.

Без железа и боевой базы."""
from types import SimpleNamespace as NS

import pytest

import cv_store
import cv_volume


def _m(d, group="whole", defect=None):
    return NS(group=group, defect=defect, size_um=float(d), length_um=float(d), width_um=float(d))


def _cfg():
    return {"volume": {"fines_side_mm": 0.2, "k_thick": 0.88}}


def test_mean_weighted_by_model_volume_and_area():
    ms = [_m(100), _m(300), _m(900, defect="aggregate"), _m(900, group="cut"), _m(900, group="bubble")]
    p = cv_volume.percents(cv_volume.sums_for(ms, _cfg()))
    # M1 — вес d³: (100·1e6 + 300·27e6) / 28e6; у квадратных кристаллов (L = W = d) M2 и M3 тоже ~ d³ — тот же ответ
    want_v = (100 * 100 ** 3 + 300 * 300 ** 3) / (100 ** 3 + 300 ** 3)
    for mod in ("m1", "m2", "m3"):
        assert p["mean_um"][mod] == pytest.approx(want_v, abs=0.1), mod
    assert p["mean_um"]["area"] == pytest.approx((100 * 100 ** 2 + 300 * 300 ** 2) / (100 ** 2 + 300 ** 2), abs=0.1)
    assert p["mean_um"]["area"] < p["mean_um"]["m1"]          # по площади мелкие весят больше, чем по объёму


def test_frames_add_up_and_fines_gate_does_not_hide_size():
    a, b = cv_volume.sums_for([_m(100)], _cfg(), count_fines=False), cv_volume.sums_for([_m(300)], _cfg(), count_fines=False)
    both = cv_volume.percents(cv_volume.add_sums(a, b))
    one = cv_volume.percents(cv_volume.sums_for([_m(100), _m(300)], _cfg()))
    assert both["mean_um"] == one["mean_um"]                  # кадры пробы складываются суммами, а не усредняются
    assert both["m1"]["fines"] is None and both["mean_um"]["m1"] is not None     # СВ ниже порога муки — мука прочерк, размер есть


def test_empty_and_old_sums():
    assert cv_volume.percents(cv_volume.empty_sums())["mean_um"] == {"m1": None, "m2": None, "m3": None, "area": None}
    old = cv_volume.sums_for([_m(200)], _cfg())
    del old["dsum"]                                           # проба, посчитанная до 2.1.75 — без сумм размера
    assert cv_volume.percents(old)["mean_um"]["m1"] is None


def test_probe_columns_and_boil_block():
    s = {"volume": cv_volume.sums_for([_m(100), _m(300)], _cfg())}
    cols = cv_store._volume_cols(s, cv_volume.percents(s["volume"]), 1)
    assert cols["msize_m1"] == pytest.approx(292.9, abs=0.1) and cols["msize_area"] == pytest.approx(280.0, abs=0.1)
    # по набору проб: M1–M3 — с весом общего объёма пробы (как рассев), площадь — простое среднее
    rows = [{"ts": "a", "fines_m3": 1.0, "msize_m1": 200.0, "vtot_m1": 1.0, "msize_area": 200.0},
            {"ts": "b", "fines_m3": 1.0, "msize_m1": 400.0, "vtot_m1": 3.0, "msize_area": 400.0},
            {"ts": "c", "fines_m3": 1.0, "vtot_m1": 5.0}]                       # старая проба без размера — не входит
    blk = cv_store._vol_block(rows)
    assert blk["msize"]["m1"] == 350.0 and blk["msize"]["area"] == 300.0 and blk["msize"]["m2"] is None
