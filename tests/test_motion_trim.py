"""Нарезка записи вокруг событий (motion_service/motion_trim.py): отрезки, звук, движение, настоящий ffmpeg и расписание."""
import os
import re
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "motion_service"))

import motion_engine as me  # noqa: E402
import motion_store  # noqa: E402
import motion_trim as mt  # noqa: E402
import motion_videorec as vr  # noqa: E402

FFMPEG = vr.find_ffmpeg()
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="нет ffmpeg (задайте MOTION_FFMPEG)")
CFG = {"pre_s": 5, "post_s": 5, "sound_db": 20, "full_h": 24}
T0 = mt.start_time("20260101_120000.mkv")


# ─── чистая логика ───
def test_plan_pads_clips_and_merges():
    assert mt.plan([(20, 22), (40, 41), (95, 99)], 100, 5, 5) == [(15, 27), (35, 46), (90, 100)]
    assert mt.plan([(20, 22), (35, 36)], 100, 5, 5) == [(15, 41)]                 # зазор меньше MERGE_GAP_S — один отрезок
    assert mt.plan([(-8, -6), (106, 107)], 100, 5, 5) == []                       # события соседнего куска, не достающие до этого
    assert mt.plan([(-3, -1), (101, 102)], 100, 5, 5) == [(0, 4), (96, 100)]      # а эти достают


def noise(seconds, loud=(), amp=0.2, base=0.002, seed=0):
    rng = np.random.default_rng(seed)
    x = rng.normal(0, base, int(seconds * mt.SR))
    for a, dur in loud:
        part = x[int(a * mt.SR):int((a + dur) * mt.SR)]      # за концом куска — нечего добавлять
        part += rng.normal(0, amp, len(part))
    return x.astype(np.float32)


def test_sound_events_finds_loud_and_ignores_quiet():
    events, silent = mt.sound_events(noise(60, [(20, 3), (40, 2)]), 20)
    assert silent == [] and len(events) == 2
    assert abs(events[0][0] - 20) < 0.6 and abs(events[0][1] - 23) < 0.6 and abs(events[1][0] - 40) < 0.6
    assert mt.sound_events(noise(60), 20) == ([], [])
    assert mt.sound_events(noise(60, [(20, 3)], amp=0.01), 20)[0] == []           # +14 дБ — тише порога


def test_sound_events_digital_silence_is_unknown():
    x = noise(60)
    x[int(10 * mt.SR):int(30 * mt.SR)] = 0
    events, silent = mt.sound_events(x, 20)
    assert events == [] and len(silent) == 1 and abs(silent[0][0] - 10) < 0.2 and abs(silent[0][1] - 30) < 0.2
    assert mt.sound_events(np.zeros(60 * mt.SR, np.float32), 20) == ([], [(0.0, 60.0)])
    assert mt.sound_events(np.zeros(100, np.float32), 20) == ([], [])


def test_motion_events_and_gaps():
    rows = [(T0 + s, s in (30, 31), True) for s in range(0, 100) if not 60 <= s < 80]
    events, unknown = mt.motion_events(rows, T0, 0.0, 100.0)
    assert events == [(30, 31.5 + mt.SKEW_S), (31, 32.5 + mt.SKEW_S)]
    assert len(unknown) == 1 and unknown[0][0] == pytest.approx(60.5) and unknown[0][1] == pytest.approx(80)
    assert mt.motion_events([], T0, 0.0, 100.0) == ([], [(0.0, 100.0)])
    bad = [(T0 + s, False, False) for s in range(100)]                            # камера мертва — это «не знаем», а не «тихо»
    assert mt.motion_events(bad, T0, 0.0, 100.0)[1] == [(0.0, 100.0)]


def test_motion_zones_and_inverted_bit():
    cam = {"zones": [{"id": "a", "type": "motion"}, {"id": "b", "type": "presence"}, {"id": "c", "type": "cycle"}, {"id": "d", "type": "motion", "motion": False},
                     {"id": "e", "type": "motion", "enabled": False}, {"id": "f", "type": "gate"}, {"id": "g", "type": "motion", "invert": True},
                     {"id": "h", "type": "presence", "invert": False}]}
    assert me.motion_zones(cam) == {"a": False, "b": True, "g": True, "h": False}
    assert me.motion_zones(me.normalize_camera({"id": "c1", "url": "x", "zones": [{"id": "p", "type": "presence", "points": [[0, 0], [9, 0], [9, 9]]}]})) == {"p": True}


def test_store_activity(tmp_path):
    st = motion_store.store_for(tmp_path)
    for s in range(5):
        st.add_trend("c1", T0 + s, {"z1": (0, 0, int(s == 2)), "z2": (0, 0, 0), "other": (0, 0, 1)}, int(s != 4))
    rows = st.activity("c1", {"z1": False, "z2": False}, T0 + 1, T0 + 10)
    assert rows == [(T0 + 1, False, True), (T0 + 2, True, True), (T0 + 3, False, True), (T0 + 4, False, False)]
    assert st.activity("c1", {}, T0, T0 + 10) == []
    # окно с перевёрнутым битом: «стоит» (бит 1) — не событие, а бит 0 — движение
    assert [r[1] for r in st.activity("c1", {"z1": True}, T0 + 1, T0 + 3)] == [True, False, True]


# ─── настройки ───
def test_settings_validate_and_trim_from(tmp_path):
    rec = vr.VideoRec(tmp_path, lambda: [{"id": "c1", "url": ""}], ffmpeg=lambda: None)
    assert "trim_from" not in rec.set_camera("c1", {"fps": 3})["cameras"]["c1"]
    before = time.time()
    st = rec.set_camera("c1", {"keep": "events", "trim_from": 0})["cameras"]["c1"]      # момент включения ставит только сервис
    assert st["keep"] == "events" and st["trim_from"] >= before
    assert rec.set_camera("c1", {"post_s": 30})["cameras"]["c1"]["trim_from"] == st["trim_from"]
    again = vr.VideoRec(tmp_path, lambda: [{"id": "c1", "url": ""}], ffmpeg=lambda: None)
    again.load()
    assert again.settings["c1"]["trim_from"] == st["trim_from"] and again.settings["c1"]["post_s"] == 30
    assert "trim_from" not in again.set_camera("c1", {"keep": "all"})["cameras"]["c1"]
    for bad in ({"keep": "x"}, {"pre_s": -1}, {"post_s": 1000}, {"sound_db": 90}, {"full_h": "x"}):
        with pytest.raises(ValueError):
            again.set_camera("c1", bad)


def test_trim_settings_do_not_restart_recorder(tmp_path, monkeypatch):
    rec = vr.VideoRec(tmp_path, lambda: [{"id": "c1", "url": "rtsp://h/x"}], ffmpeg=lambda: "ffmpeg")
    started = []
    monkeypatch.setattr(vr.CamRecorder, "start", lambda self: started.append(self.cid))
    monkeypatch.setattr(vr.CamRecorder, "join", lambda self, timeout=None: None)
    monkeypatch.setattr(vr.CamRecorder, "is_alive", lambda self: True)
    monkeypatch.setattr(rec, "_free", lambda: 1 << 40)
    rec.set_camera("c1", {"on": True})
    rec.set_camera("c1", {"keep": "events"})
    rec.set_camera("c1", {"pre_s": 3, "full_h": 0})
    assert started == ["c1"]
    rec.set_camera("c1", {"fps": 3})
    assert started == ["c1", "c1"]


def _touch(rec, name, age=1000.0, size=100):
    f = rec.cam_dir("c1") / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"x" * size)
    os.utime(f, (time.time() - age, time.time() - age))
    return f


def test_next_piece_skips_old_fresh_and_writing(tmp_path):
    rec = vr.VideoRec(tmp_path, lambda: [])
    cfg = {"trim_from": mt.start_time("20260101_001000.mkv")}
    _touch(rec, "20260101_000000.mkv")                        # записан до включения нарезки
    assert rec._next_piece("c1", cfg) is None
    _touch(rec, "20260101_000000_audio.mka")
    _touch(rec, "20260101_000500_clip.mkv")
    a = _touch(rec, "20260101_001000.mkv", age=5)             # только что дописан
    assert rec._next_piece("c1", cfg) is None
    os.utime(a, (time.time() - 100, time.time() - 100))
    b = _touch(rec, "20260101_002000.mkv", age=100)
    piece, prev, nxt = rec._next_piece("c1", cfg)
    assert piece == a and nxt == b and prev.name == "20260101_000000_audio.mka"
    rec.recorders["c1"] = object()                            # идёт запись: самый новый кусок — пишущийся
    a.unlink()
    assert rec._next_piece("c1", cfg) is None
    os.utime(b, (time.time() - 700, time.time() - 700))       # камера пропала: кусок уже не вырастет
    assert rec._next_piece("c1", cfg)[0] == b


def test_purge_takes_full_copies_first(tmp_path):
    rec = vr.VideoRec(tmp_path, lambda: [])
    for name in ("20260101_000000_clip.mkv", "20260101_000000_audio.mka", "20260101_001000_full.mkv", "20260101_002000_full.mkv"):
        _touch(rec, name, size=1000)
    rec.limit_gb = 3500 / (1 << 30)
    assert rec.purge() == 1
    assert [n for n, _, _ in rec.files()] == ["20260101_000000_audio.mka", "20260101_000000_clip.mkv", "20260101_002000_full.mkv"]
    rec.limit_gb = 1500 / (1 << 30)
    assert rec.purge() == 2 and [n for n, _, _ in rec.files()] == ["20260101_000000_clip.mkv"]


def test_expire_full(tmp_path):
    rec = vr.VideoRec(tmp_path, lambda: [])
    old = _touch(rec, "20260101_000000_full.mkv", age=2 * 3600)
    new = _touch(rec, "20260101_001000_full.mkv", age=60)
    kept = _touch(rec, "20260101_002000_kept.mkv", age=99999)
    rec._expire_full("c1", {"full_h": 1})
    assert not old.exists() and new.exists() and kept.exists()


def test_failed_piece_is_kept_after_three_tries(tmp_path, monkeypatch):
    rec = vr.VideoRec(tmp_path, lambda: [], ffmpeg=lambda: "ffmpeg", settle_s=0)
    rec.settings["c1"] = vr.normalize_camera({"keep": "events", "trim_from": 0})
    f = _touch(rec, "20260101_000000.mkv")

    def boom(*a, **k):
        raise RuntimeError("rtsp://admin:secret@h/x сломалось")

    monkeypatch.setattr(vr.trim, "trim_piece", boom)
    for _ in range(vr.TRIM_TRIES - 1):
        assert rec.trim_once() == 0 and f.exists()
    assert "secret" not in rec.trim_info["c1"]["error"] and "сломалось" in rec.trim_info["c1"]["error"]
    rec.trim_once()
    assert [n for n, _, _ in rec.files()] == ["20260101_000000_kept.mkv"]


# ─── настоящий ffmpeg ───
def make_piece(path: Path, seconds=120, loud=((60, 4),), audio=True):
    """Кусок как из записи: H.264 с опорным кадром каждые 2 с и звук; «камни» — на заданных секундах."""
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=5:duration=%d" % seconds]
    wav = path.with_suffix(".wav")
    if audio:
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(mt.SR)
            w.writeframes((np.clip(noise(seconds, loud), -1, 1) * 32767).astype("<i2").tobytes())
        cmd += ["-i", str(wav), "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-force_key_frames", "expr:gte(t,n_forced*2)", "-f", "matroska", str(path)]
    subprocess.run(cmd, check=True, timeout=120)
    wav.unlink(missing_ok=True)
    return path


def media():
    return mt.Media(FFMPEG, threading.Event())


def quiet_rows(t0, t1, moving=()):
    return [(float(t), any(a <= t - T0 < b for a, b in moving), True) for t in range(int(t0), int(t1) + 1)]


def length(path):
    return media().probe(path)[0]


def describe(path):
    return subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=30).stderr


@needs_ffmpeg
def test_cut_around_sound_and_motion(tmp_path):
    piece = make_piece(tmp_path / "20260101_120000.mkv")
    r = mt.trim_piece(media(), piece, CFG, lambda a, b: quiet_rows(a, b, [(20, 23)]))
    assert r["result"] == "clips", r
    assert r["clips"] == ["20260101_120015_clip.mkv", "20260101_120055_clip.mkv"] or len(r["clips"]) == 2, r
    names = sorted(p.name for p in tmp_path.iterdir())
    assert "20260101_120000_full.mkv" in names and "20260101_120000_audio.mka" in names and not piece.exists()
    assert not [n for n in names if n.endswith(".part")]
    first, second = (tmp_path / n for n in r["clips"])
    # событие 3–4 с и по 5 с запаса (+ до 2 с до опорного кадра); у отрезка движения «после» ещё SKEW_S — имя куска позже содержимого
    assert 17 <= length(first) <= 22 and 12 <= length(second) <= 17
    assert media().probe(first)[1:] == (True, True)
    assert length(tmp_path / "20260101_120000_audio.mka") >= 119 and media().probe(tmp_path / "20260101_120000_audio.mka")[1:] == (True, False)
    # в отрезке вокруг «камней» они есть
    assert mt.sound_events(np.concatenate((noise(15, seed=3), media().audio(second))), 20)[0]


@needs_ffmpeg
@pytest.mark.parametrize("late", [2, 3, 6])
def test_audio_starting_late_keeps_its_place(tmp_path, late):
    """Звук начался позже картинки (переподключение камеры): в отдельном файле звука события стоят там же, нарезка проходит."""
    base = make_piece(tmp_path / "base.mkv", 60, loud=((30, 3),))
    piece = tmp_path / "20260101_120000.mkv"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(base), "-itsoffset", str(late), "-i", str(base), "-map", "0:v:0", "-map", "1:a:0",
                    "-c", "copy", "-t", "60", "-f", "matroska", str(piece)], check=True, timeout=60)
    base.unlink()
    events = mt.sound_events(media().audio(piece), 20)[0]
    assert abs(events[0][0] - (30 + late)) < 0.8, events          # разбор видит событие там, где оно на картинке
    r = mt.trim_piece(media(), piece, CFG, None)
    assert r["result"] == "clips" and r["clips"], r                # и нарезка не срывается
    text = describe(tmp_path / "20260101_120000_audio.mka")
    start = float(re.search(r"start: (\d+\.\d+)", text).group(1))
    assert abs(start - late) < 0.5, text                           # место звука в куске записано в метке начала файла


@needs_ffmpeg
def test_full_h_zero_deletes_piece_and_quiet_piece_leaves_only_sound(tmp_path):
    piece = make_piece(tmp_path / "20260101_120000.mkv", 60, loud=())
    r = mt.trim_piece(media(), piece, dict(CFG, full_h=0), lambda a, b: quiet_rows(a, b))
    assert r["result"] == "clips" and r["clips"] == [] and r["left_s"] == 0
    assert [p.name for p in tmp_path.iterdir()] == ["20260101_120000_audio.mka"]


@needs_ffmpeg
@pytest.mark.parametrize("cfg,activity,audio,why", [
    (CFG, lambda a, b: [], True, "почти на всё"),                        # камера не анализировалась — не режем
    (CFG, None, False, "почти на всё"),                                  # звук просили слушать, а его нет
    (dict(CFG, sound_db=0), None, True, "судить нечем"),
    (dict(CFG, sound_db=0), lambda a, b: None, True, "судить нечем"),    # окон движения у камеры нет
])
def test_keeps_piece_when_nothing_to_judge_by(tmp_path, cfg, activity, audio, why):
    piece = make_piece(tmp_path / "20260101_120000.mkv", 40, loud=(), audio=audio)
    size = piece.stat().st_size
    r = mt.trim_piece(media(), piece, cfg, activity)
    assert r["result"] == "kept" and why in r["reason"], r
    assert [p.name for p in tmp_path.iterdir()] == ["20260101_120000_kept.mkv"] and next(tmp_path.iterdir()).stat().st_size == size


@needs_ffmpeg
def test_events_across_piece_boundary(tmp_path):
    a = make_piece(tmp_path / "20260101_120000.mkv", 60, loud=((57, 2),))       # событие в конце прошлого куска
    b = make_piece(tmp_path / "20260101_120100.mkv", 60, loud=())
    c = make_piece(tmp_path / "20260101_120200.mkv", 60, loud=((1, 2),))        # и в начале следующего
    r = mt.trim_piece(media(), b, dict(CFG, pre_s=8, post_s=8), None, prev=a, nxt=c)
    assert r["result"] == "clips" and len(r["clips"]) == 2, r
    assert r["clips"][0] == "20260101_120100_clip.mkv"                          # хвост «после» с начала куска
    assert 5 <= length(tmp_path / r["clips"][0]) <= 9 and 5 <= length(tmp_path / r["clips"][1]) <= 11


@needs_ffmpeg
def test_failure_leaves_piece_untouched(tmp_path, monkeypatch):
    piece = make_piece(tmp_path / "20260101_120000.mkv", 60, loud=((30, 3),))
    real = mt.Media.copy

    def broken(self, src, dst, *a, **k):
        real(self, src, dst, *a, **k)
        if k.get("audio_only"):
            raise RuntimeError("диск полон")

    monkeypatch.setattr(mt.Media, "copy", broken)
    size = piece.stat().st_size
    with pytest.raises(RuntimeError):
        mt.trim_piece(media(), piece, CFG, None)
    assert [p.name for p in tmp_path.iterdir()] == [piece.name] and piece.stat().st_size == size


@needs_ffmpeg
def test_locked_piece_makes_no_copies(tmp_path, monkeypatch):
    """Кусок открыт плеером: переименовать нельзя — никаких отрезков и звука рядом не появляется, сколько ни пробуй."""
    piece = make_piece(tmp_path / "20260101_120000.mkv", 60, loud=((30, 3),))
    real = os.replace

    def locked(src, dst):
        if Path(src) == piece:
            raise PermissionError("занят")
        real(src, dst)

    monkeypatch.setattr(mt.os, "replace", locked)
    for _ in range(2):
        with pytest.raises(PermissionError):
            mt.trim_piece(media(), piece, CFG, None)
    assert [p.name for p in tmp_path.iterdir()] == [piece.name]


@needs_ffmpeg
def test_failed_publish_is_rolled_back(tmp_path, monkeypatch):
    piece = make_piece(tmp_path / "20260101_120000.mkv", 60, loud=((30, 3),))
    real = os.replace

    def broken(src, dst):
        if str(dst).endswith("_audio.mka"):
            raise OSError("диск отвалился")
        real(src, dst)

    monkeypatch.setattr(mt.os, "replace", broken)
    size = piece.stat().st_size
    with pytest.raises(OSError):
        mt.trim_piece(media(), piece, dict(CFG, full_h=0), None)
    assert [p.name for p in tmp_path.iterdir()] == [piece.name] and piece.stat().st_size == size


@needs_ffmpeg
@pytest.mark.parametrize("full_h", [0, 24])
def test_double_failure_never_leaves_piece_deletable(tmp_path, monkeypatch, full_h):
    """Выкладка сорвалась и откат тоже: исходник не должен выглядеть лишней копией — потом он возвращается целым куском."""
    rec = vr.VideoRec(tmp_path, lambda: [])
    folder = rec.cam_dir("c1")
    folder.mkdir(parents=True)
    piece = make_piece(folder / "20260101_120000.mkv", 60, loud=((30, 3),))
    size = piece.stat().st_size
    real = os.replace

    def broken(src, dst):
        if str(dst).endswith("_audio.mka") or (str(src).endswith(mt.STAGED) and Path(dst) == piece) or str(src).endswith("_clip.mkv"):
            raise OSError("файл взял антивирус")
        real(src, dst)

    monkeypatch.setattr(mt.os, "replace", broken)
    with pytest.raises(OSError):
        mt.trim_piece(media(), piece, dict(CFG, full_h=full_h), None)
    monkeypatch.setattr(mt.os, "replace", real)
    rec.limit_gb = 0.0                                       # лимит и срок полных копий его не берут
    rec.purge()
    rec._expire_full("c1", {"full_h": 0})
    left = folder / ("20260101_120000.mkv" + mt.STAGED)
    assert left.exists() and left.stat().st_size == size
    rec.limit_gb = 20.0
    rec.trim_once()
    kept = list(folder.glob("*_kept.mkv"))
    assert len(kept) == 1 and kept[0].stat().st_size == size and not left.exists()


@needs_ffmpeg
def test_power_cut_during_publish_keeps_the_piece(tmp_path, monkeypatch):
    """«Выключили питание» сразу после переименования исходника, полные куски не хранятся: после запуска кусок цел."""
    rec = vr.VideoRec(tmp_path, lambda: [], ffmpeg=lambda: FFMPEG)
    folder = rec.cam_dir("c1")
    folder.mkdir(parents=True)
    piece = make_piece(folder / "20260101_120000.mkv", 60, loud=((30, 3),))
    size = piece.stat().st_size
    real = os.replace

    class PowerCut(BaseException):
        pass

    def cut(src, dst):
        if str(src).endswith(".part"):
            raise PowerCut()
        real(src, dst)

    monkeypatch.setattr(mt.os, "replace", cut)
    with pytest.raises(PowerCut):
        mt.trim_piece(media(), piece, dict(CFG, full_h=0), None)
    monkeypatch.setattr(mt.os, "replace", real)
    rec.settings["c1"] = vr.normalize_camera({"keep": "events", "trim_from": 0, "full_h": 0})
    rec.trim_once()
    assert [p.stat().st_size for p in folder.glob("*_kept.mkv")] == [size]


@needs_ffmpeg
def test_existing_files_are_not_overwritten(tmp_path):
    """Часы перевели назад: имена совпали со старыми — старые файлы остаются нетронутыми."""
    for suffix in ("_full.mkv", "_audio.mka", "_kept.mkv"):
        (tmp_path / ("20260101_120000" + suffix)).write_bytes(b"old" + suffix.encode())
    piece = make_piece(tmp_path / "20260101_120000.mkv", 60, loud=())
    assert mt.trim_piece(media(), piece, CFG, None)["result"] == "clips"
    make_piece(tmp_path / "20260101_120000.mkv", 30, loud=())
    assert mt.trim_piece(media(), piece, dict(CFG, sound_db=0), None)["result"] == "kept"
    for suffix in ("_full.mkv", "_audio.mka", "_kept.mkv"):
        assert (tmp_path / ("20260101_120000" + suffix)).read_bytes() == b"old" + suffix.encode()
    assert len(list(tmp_path.glob("*_full.mkv"))) == 2 and len(list(tmp_path.glob("*_kept.mkv"))) == 2


@needs_ffmpeg
def test_mode_switched_off_during_work_leaves_piece(tmp_path):
    piece = make_piece(tmp_path / "20260101_120000.mkv", 60, loud=((30, 3),))
    with pytest.raises(mt.Aborted):
        mt.trim_piece(media(), piece, CFG, None, current=lambda: {"keep": "all"})
    assert [p.name for p in tmp_path.iterdir()] == [piece.name]


@needs_ffmpeg
def test_unfinished_file_is_kept(tmp_path):
    """Запись убили: в файле нет длительности — сколько там картинки, неизвестно, не режем."""
    piece = tmp_path / "20260101_120000.mkv"
    # бесконечный «живой» источник и убитый ffmpeg — ровно то, что бывает при обрыве записи: длину в заголовок дописать некому
    p = subprocess.Popen([FFMPEG, "-v", "error", "-re", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=5", "-re", "-f", "lavfi", "-i", "sine=sample_rate=16000",
                          "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-c:a", "aac", "-f", "matroska", "-flush_packets", "1", str(piece)])
    try:
        t0 = time.monotonic()
        while time.monotonic() - t0 < 20 and (not piece.exists() or piece.stat().st_size < 30000):
            time.sleep(0.2)
        time.sleep(3)
    finally:
        p.kill()
        p.wait(10)
    assert mt.Media(FFMPEG, threading.Event()).probe(piece)[0] is None
    r = mt.trim_piece(media(), piece, CFG, None)
    assert r["result"] == "kept" and "не закрыт" in r["reason"], r


def test_locked_piece_leaves_the_queue(tmp_path, monkeypatch):
    rec = vr.VideoRec(tmp_path, lambda: [], ffmpeg=lambda: "ffmpeg", settle_s=0)
    rec.settings["c1"] = vr.normalize_camera({"keep": "events", "trim_from": 0, "pre_s": 0})
    first = _touch(rec, "20260101_000000.mkv")
    second = _touch(rec, "20260101_001000.mkv")

    def boom(*a, **k):
        raise PermissionError("занят")

    monkeypatch.setattr(vr.trim, "trim_piece", boom)
    monkeypatch.setattr(vr.trim, "keep_whole", boom)
    for _ in range(vr.TRIM_TRIES):
        rec.trim_once()
    assert first.exists() and rec._next_piece("c1", rec.settings["c1"])[0] == second


def test_big_pre_roll_waits_for_next_piece(tmp_path):
    rec = vr.VideoRec(tmp_path, lambda: [])
    _touch(rec, "20260101_000000.mkv", age=90)
    _touch(rec, "20260101_001000.mkv", age=10)
    assert rec._next_piece("c1", {"trim_from": 0, "pre_s": 120}) is None
    assert rec._next_piece("c1", {"trim_from": 0, "pre_s": 10}) is not None


@needs_ffmpeg
def test_stopping_service_aborts_without_touching(tmp_path):
    piece = make_piece(tmp_path / "20260101_120000.mkv", 40)
    halt = threading.Event()
    halt.set()
    with pytest.raises(mt.Aborted):
        mt.trim_piece(mt.Media(FFMPEG, halt), piece, CFG, None)
    assert [p.name for p in tmp_path.iterdir()] == [piece.name]


@needs_ffmpeg
def test_broken_file_is_kept(tmp_path):
    f = tmp_path / "20260101_120000.mkv"
    f.write_bytes(b"not a video" * 100)
    assert mt.trim_piece(media(), f, CFG, None)["result"] == "kept"
    assert (tmp_path / "20260101_120000_kept.mkv").exists()


@needs_ffmpeg
def test_recording_then_trimming_live(tmp_path):
    """Настоящая запись из файла кусками по 20 с и нарезка потоком сервиса: старые куски не тронуты, новые разобраны."""
    src = make_piece(tmp_path / "src.mkv", 90, loud=((8, 3), (33, 3), (58, 3)))
    rec = vr.VideoRec(tmp_path / "data", lambda: [{"id": "c1", "url": str(src)}], ffmpeg=lambda: FFMPEG, segment_s=20, settle_s=3)
    old = rec.cam_dir("c1") / "20200101_000000.mkv"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old")
    old_stat = old.stat().st_size
    rec.start()
    try:
        time.sleep(0.2)
        rec.set_camera("c1", {"on": True, "keep": "events", "pre_s": 2, "post_s": 2})
        t0 = time.monotonic()
        while time.monotonic() - t0 < 100:
            names = [n for n, _, _ in rec.files()]
            if sum(n.endswith("_audio.mka") for n in names) >= 2:
                break
            time.sleep(1)
        names = [n for n, _, _ in rec.files()]
        assert sum(n.endswith("_audio.mka") for n in names) >= 2, (names, rec.trim_info)
        st = rec.status()["cameras"]["c1"]
        assert st["clips"] >= 1 and st["trim"] and not st["trim_error"], st
    finally:
        rec.shutdown()
    assert old.exists() and old.stat().st_size == old_stat
    assert not list(rec.root.glob("*/*.part"))
