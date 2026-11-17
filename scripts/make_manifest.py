"""Манифест релиза для сборки (build.bat): у каждого компонента своя версия, обновляется только изменившееся.

Источники версий (все в git):
  VERSION                 — основное приложение (web_MVS);
  component_versions.json — сервисы: {"cv": "3.0.0", "video": ..., "motion": ..., "level": ...};
  RELEASE                 — номер релиза (тег v<RELEASE>, имена архивов); растёт на каждый релиз.

  python scripts/make_manifest.py --write-versions          — записать VERSION в папки сервисов (до упаковки архивов)
  python scripts/make_manifest.py [--prev prev.json]        — собрать dist/components.json из готовых архивов и проверить:
      * номер релиза больше прошлого;
      * файлы сервиса изменились, а его версия та же — СТОП (забыли поднять версию: клиенты не обновили бы сервис);
      * версия компонента не меньше прошлой.
  Хеш сервиса — по именам и CRC файлов в его архиве (без VERSION): одинаковый код — одинаковый хеш при любых датах файлов.
"""
import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICES = {"cv": "cv_service", "video": "video_service", "motion": "motion_service", "level": "level_service"}
VER_RE = re.compile(r"^\d+(\.\d+){1,3}$")


def _vt(v: str) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", v or "")) or (0,)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig").strip().lstrip("vV")


def load_versions(root: Path = ROOT) -> dict:
    """{"release", "main", "cv", ...} из файлов репозитория; неверный формат — ValueError."""
    out = {"release": read_text(root / "RELEASE"), "main": read_text(root / "VERSION")}
    svc = json.loads((root / "component_versions.json").read_text(encoding="utf-8-sig"))
    for key in SERVICES:
        out[key] = str(svc.get(key, "")).strip().lstrip("vV")
    for key, v in out.items():
        if not VER_RE.match(v):
            raise ValueError("версия %s некорректна: %r" % (key, v))
    return out


def write_service_versions(root: Path = ROOT) -> None:
    """VERSION в папки сервисов (файлы в .gitignore, попадают в архивы)."""
    ver = load_versions(root)
    for key, d in SERVICES.items():
        if (root / d).is_dir():
            (root / d / "VERSION").write_text(ver[key] + "\n", encoding="utf-8")


def zip_sha(path: Path) -> str:
    """Хеш содержимого архива сервиса: имена + CRC файлов, кроме VERSION."""
    h = hashlib.sha256()
    with zipfile.ZipFile(path) as zf:
        for info in sorted(zf.infolist(), key=lambda i: i.filename.replace("\\", "/")):
            name = info.filename.replace("\\", "/")
            if info.is_dir() or name == "VERSION":
                continue
            h.update(("%s:%08x\n" % (name, info.CRC)).encode("utf-8"))
    return h.hexdigest()


def build_manifest(dist: Path, root: Path = ROOT) -> dict:
    ver = load_versions(root)
    comps = {"main": {"version": ver["main"]}}
    for key, d in SERVICES.items():
        z = dist / ("%s_v%s.zip" % (d, ver["release"]))
        if not z.is_file():
            raise FileNotFoundError("нет архива %s — сначала упакуйте сервисы" % z)
        comps[key] = {"version": ver[key], "sha": zip_sha(z)}
    return {"release": ver["release"], "components": comps}


def check_against_prev(new: dict, prev) -> list:
    """Список проблем (пусто — всё хорошо). prev — манифест прошлого релиза или None (первый релиз с манифестом)."""
    if not prev:
        return []
    errs = []
    if _vt(new["release"]) <= _vt(prev.get("release", "")):
        errs.append("RELEASE %s не больше прошлого (%s) — поднимите файл RELEASE" % (new["release"], prev.get("release")))
    for key, cur in new["components"].items():
        old = (prev.get("components") or {}).get(key)
        if not old:
            continue
        ov, nv = (old.get("version") if isinstance(old, dict) else old), cur["version"]
        if _vt(nv) < _vt(ov):
            errs.append("%s: версия %s ниже прошлой %s" % (key, nv, ov))
        elif key != "main" and nv == ov and isinstance(old, dict) and old.get("sha") and old["sha"] != cur["sha"]:
            errs.append("%s: файлы сервиса изменились, а версия та же (%s) — поднимите её в component_versions.json" % (key, nv))
    return errs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-versions", action="store_true")
    ap.add_argument("--dist", default=str(ROOT / "dist"))
    ap.add_argument("--prev", default="")
    a = ap.parse_args(argv)
    if a.write_versions:
        write_service_versions(ROOT)
        return 0
    manifest = build_manifest(Path(a.dist), ROOT)
    prev = None
    if a.prev and Path(a.prev).is_file():
        try:
            prev = json.loads(Path(a.prev).read_text(encoding="utf-8-sig"))
        except ValueError:
            print("[manifest] WARNING: прошлый манифест не читается — проверка изменений пропущена")
    else:
        print("[manifest] WARNING: прошлого манифеста нет (первый релиз с манифестом или нет связи) — проверка изменений пропущена")
    errs = check_against_prev(manifest, prev)
    if errs:
        for e in errs:
            print("[manifest] STOP: " + e)
        return 1
    out = Path(a.dist) / "components.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("[manifest] %s: release %s, " % (out.name, manifest["release"]) +
          ", ".join("%s %s" % (k, v["version"]) for k, v in manifest["components"].items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
