"""Неоднородность размера в рассеве (19.11): CV = 100·σ/средний, где средний и σ взяты с весом объёма модели / площади.
Для пробы — из сумм кадров; для варки/фазы — из моментов проб (а не среднее их CV). Без железа и боевой базы."""
from types import SimpleNamespace as NS

import pytest

import cv_store
import cv_volume


def _m(d, group="whole", defect=None):
    return NS(group=group, defect=defect, size_um=float(d), length_um=float(d), width_um=float(d))


def _cfg():
    return {"volume": {"fines_side_mm": 0.2, "k_thick": 0.88}}


def test_het_equal_sizes_is_zero():
    p = cv_volume.percents(cv_volume.sums_for([_m(200), _m(200), _m(200)], _cfg()))
    for mod in ("m1", "m2", "m3", "area"):
        assert p["het_pct"][mod] == 0.0, mod


def test_het_matches_weighted_formula():
    ms = [_m(100), _m(300), _m(900, defect="aggregate"), _m(900, group="cut"), _m(900, group="bubble")]   # брак/обрезанные/пузыри не входят
    p = cv_volume.percents(cv_volume.sums_for(ms, _cfg()))
    for mod, k in (("m1", 3), ("area", 2)):                   # у квадратных кристаллов вес M1 = d³, площади = d²
        w = [100 ** k, 300 ** k]
        mean = (w[0] * 100 + w[1] * 300) / sum(w)
        var = (w[0] * 100 ** 2 + w[1] * 300 ** 2) / sum(w) - mean ** 2
        assert p["het_pct"][mod] == pytest.approx(100 * var ** 0.5 / mean, abs=0.1), mod


def test_frames_add_up():
    a, b = cv_volume.sums_for([_m(100)], _cfg()), cv_volume.sums_for([_m(300)], _cfg())
    both = cv_volume.percents(cv_volume.add_sums(a, b))
    one = cv_volume.percents(cv_volume.sums_for([_m(100), _m(300)], _cfg()))
    assert both["het_pct"] == one["het_pct"]                  # кадры складываются суммами; CV кадра с одним кристаллом = 0, но в сумме он не теряется
    assert both["het_pct"]["m1"] > 0


def test_empty_and_old_sums():
    assert cv_volume.percents(cv_volume.empty_sums())["het_pct"] == {"m1": None, "m2": None, "m3": None, "area": None}
    old = cv_volume.sums_for([_m(200), _m(300)], _cfg())
    del old["d2sum"]                                          # проба до v3.0.20 — без сумм квадратов
    assert cv_volume.percents(old)["het_pct"]["m1"] is None


def test_boil_block_pools_probes_not_averages_cv():
    """Две пробы, в каждой кристаллы одного размера (CV пробы = 0), но размеры разные: CV варки не ноль и равен CV объединённой массы."""
    def probe(ms):
        s = {"volume": cv_volume.sums_for(ms, _cfg())}
        cols = cv_store._volume_cols(s, cv_volume.percents(s["volume"]), 1)
        cols["fines_m3"], cols["ts"] = 1.0, "x"
        return cols
    r1, r2 = probe([_m(100), _m(100)]), probe([_m(300), _m(300)])
    assert cv_volume.het_pct(r1["msize_m1"], r1["msq_m1"]) == 0.0
    blk = cv_store._vol_block([r1, r2])
    pooled = cv_volume.percents(cv_volume.sums_for([_m(100)] * 2 + [_m(300)] * 2, _cfg()))["het_pct"]
    assert blk["het"]["m1"] == pytest.approx(pooled["m1"], abs=0.2)
    assert blk["het"]["m1"] > 10                     # по массе крупные весят в 27 раз больше: CV ~12 % (по штукам был бы 50 %)
    old = dict(r1); del old["msq_m1"]                          # старая проба без момента не ломает расчёт
    assert cv_store._vol_block([old])["het"]["m1"] is None
