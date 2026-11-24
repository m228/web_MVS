"""Оценка расхождения камер по Δ углов левых/правых валов двух рядов (motion_shafts.camera_offset и панель «Время кадров»).

Цифры взяты со скринов 24.11: НЛВ–ВЛВ Δ −9,1°, НПВ–ВПВ Δ +7,7° при ≈ 0,7 об/мин (4,2°/с), левые валы ↺, правые ↻ — кадры val_Up на ≈ 2 с старше.
"""
import sys
from pathlib import Path

MS = Path(__file__).resolve().parent.parent / "motion_service"
sys.path.insert(0, str(MS))

import motion_shafts as ms  # noqa: E402
from test_motion_shafts import cfg_two_rows, runner, states  # noqa: E402

RATE = 4.2                                   # °/с


def pts(delta, rate, n=ms.OFFSET_MIN_N, t0=1000.0):
    return [(t0 + i, delta, rate) for i in range(n)]


def test_offset_sign_and_value_from_screen_numbers():
    now = 1000.0 + ms.OFFSET_MIN_N
    s_left = ms.camera_offset(pts(-9.1, -RATE), now)        # левые ↺ (rate −), Δ −9,1° → первая камера (нижняя) новее на 2,17 с
    s_right = ms.camera_offset(pts(7.7, +RATE), now)        # правые ↻ (rate +), Δ +7,7° → то же самое
    assert abs(s_left - (-2.17)) < 0.01 and abs(s_right - (-1.83)) < 0.01
    assert s_left < 0 and s_right < 0                       # задержка камеры даёт одинаковый знак «τA − τB» у обеих пар


def test_offset_needs_enough_points_and_a_moving_shaft():
    now = 1000.0 + ms.OFFSET_MIN_N
    assert ms.camera_offset(pts(-9.1, -RATE, n=ms.OFFSET_MIN_N - 1), now) is None
    assert ms.camera_offset(pts(-9.1, -0.1 * 6), now) is None          # вал почти стоит — секунды не получить
    assert ms.camera_offset(pts(-9.1, -RATE), now + 1000.0) is None    # точки старше окна
    assert ms.camera_offset(pts(-9.1, -RATE), now) is not None


def test_offset_averages_around_360():
    now = 1000.0 + ms.OFFSET_MIN_N
    rows = [(1000.0 + i, (179.0 if i % 2 else -179.0), RATE) for i in range(ms.OFFSET_MIN_N)]   # ±179 — рядом через 180, не «ноль»
    s = ms.camera_offset(rows, now)
    assert abs(abs(s) - 180.0 / RATE) < 1.0


def run_ticks(r, st, n, t0=1000.0):
    for i in range(n):
        r.tick(t0 + i)
    return r.snap["timing"]


def test_runner_reports_that_val_up_is_older_when_both_pairs_agree():
    # dn = нижний ряд (val_Down), up = верхний (val_Up); rpm левых −0,7, правых +0,7; Δ(НЛВ–ВЛВ) = −9,1°, Δ(НПВ–ВПВ) = +7,7°
    st = states(dn=(100.0, 267.7, -0.7, 0.7), up=(109.1, 260.0, -0.7, 0.7))
    cfg = cfg_two_rows()
    cfg["cameras"][1]["latency_s"] = 0.5
    r, _, _ = runner(cfg, st)
    t = run_ticks(r, st, ms.OFFSET_MIN_N + 2)
    off = t["offset"]
    assert off and off["cam"] == "up" and off["agree"] is True and 1.9 < off["s"] < 2.1
    assert off["latency_s"] is None or off["latency_s"] >= 0           # текущая задержка камеры отдана панели
    assert t["pairs"]["lnv"]["offset_s"] < 0 and t["pairs"]["rnv"]["offset_s"] < 0


def test_runner_says_pairs_disagree_when_signs_are_the_same():
    # ошибка эталона/ракурса: обе пары сдвинуты в одну сторону — это не задержка камеры
    st = states(dn=(100.0, 262.0, -0.7, 0.7), up=(109.1, 260.0, -0.7, 0.7))      # lnv −9,1°, rnv +2,0° → камеры «старше» разные
    r, _, _ = runner(cfg_two_rows(), st)
    off = run_ticks(r, st, ms.OFFSET_MIN_N + 2)["offset"]
    assert off and off["agree"] is False


def test_runner_no_estimate_without_enough_history_or_when_shafts_stand():
    st = states(dn=(100.0, 267.7, -0.7, 0.7), up=(109.1, 260.0, -0.7, 0.7))
    r, _, _ = runner(cfg_two_rows(), st)
    assert run_ticks(r, st, 10)["offset"] is None
    still = states(dn=(100.0, 267.7, 0.0, 0.0), up=(109.1, 260.0, 0.0, 0.0))
    r, _, _ = runner(cfg_two_rows(), still)
    assert run_ticks(r, still, ms.OFFSET_MIN_N + 2)["offset"] is None
