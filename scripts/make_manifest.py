"""Манифест релиза для сборки (build.bat): у каждого компонента своя версия, обновляется и выкладывается только изменившееся.

Источники версий (все в git):
  VERSION                 — основное приложение (web_MVS);
  component_versions.json — сервисы: {"cv": "3.0.0", "video": ..., "motion": ..., "level": ...};
  RELEASE                 — номер релиза (тег v<RELEASE>); растёт на каждый релиз.

Релиз частичный: в него попадают только компоненты, версия которых выше, чем в прошлом манифесте. Архив назван по версии
компонента (motion_service_v3.0.5.zip, web_MVS_v3.0.10.zip). Манифест полный: у каждого компонента есть tag и asset —
в каком релизе лежит его актуальный архив (у не менявшихся — запись из прошлого манифеста).

  python scripts/make_manifest.py --write-versions                — записать VERSION в папки сервисов (до упаковки архивов)
  python scripts/make_manifest.py --plan plan.cmd [--prev p.json] — что собирать: plan.cmd (set BUILD_main=0/1, VER_<ключ>=версия)
  python scripts/make_manifest.py --verify [--dist dist]          — по GitHub (gh): архивы не менявшихся компонентов лежат в релизах из манифеста
  python scripts/make_manifest.py [--prev prev.json]              — собрать dist/components.json, список dist/release_files.txt
      (что выкладывать) и dist/release_notes.txt; лишние архивы из dist убрать. Проверки:
      * номер релиза больше прошлого;
      * хоть один компонент изменился;
      * файлы сервиса изменились, а его версия та же — СТОП (забыли поднять версию: клиенты не обновили бы сервис);
      * версия компонента не меньше прошлой.
  Хеш сервиса — по именам и CRC файлов в его архиве (без VERSION): одинаковый код — одинаковый хеш при любых датах файлов.
"""
import argparse
import hashlib
import json
import re
import subprocess
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


def asset_name(key: str, version: str) -> str:
    """Имя архива компонента в релизе: по версии компонента."""
    return "%s_v%s.zip" % ("web_MVS" if key == "main" else SERVICES[key], version)


def _prev_version(prev, key: str) -> str:
    old = ((prev or {}).get("components") or {}).get(key)
    return (old.get("version") if isinstance(old, dict) else old) or ""


def is_changed(key: str, version: str, prev) -> bool:
    """Версия компонента выше, чем в прошлом манифесте (нет прошлого манифеста или записи — считаем изменившимся)."""
    pv = _prev_version(prev, key)
    return not pv or _vt(version) > _vt(pv)


def make_plan(root: Path = ROOT, prev=None) -> dict:
    """{"main": True/False, "cv": ..., "versions": {...}}: что собирать и выкладывать."""
    ver = load_versions(root)
    plan = {key: is_changed(key, ver[key], prev) for key in ("main", *SERVICES)}
    plan["versions"] = {key: ver[key] for key in ("main", *SERVICES)}
    return plan


def write_plan_cmd(path: Path, plan: dict) -> None:
    """Файл для build.bat (call): VER_main, BUILD_main, VER_cv, BUILD_cv ... (BUILD_x=1 — компонент изменился)."""
    lines = []
    for key, v in plan["versions"].items():
        lines.append('set "VER_%s=%s"' % (key, v))
        lines.append('set "BUILD_%s=%d"' % (key, 1 if plan[key] else 0))
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode("ascii"))


def build_manifest(dist: Path, root: Path = ROOT) -> dict:
    """Кандидат: версии и хеши всех сервисов по их архивам (упакованы все — нужны для проверки). Основное — только версия."""
    ver = load_versions(root)
    comps = {"main": {"version": ver["main"]}}
    for key in SERVICES:
        z = dist / asset_name(key, ver[key])
        if not z.is_file():
            raise FileNotFoundError("нет архива %s — сначала упакуйте сервисы" % z)
        comps[key] = {"version": ver[key], "sha": zip_sha(z)}
    return {"release": ver["release"], "components": comps}


def _carry(prev: dict, key: str) -> dict:
    """Запись не менявшегося компонента из прошлого манифеста; нет tag/asset (манифест v3.0.0–3.0.4) — выводим из номера релиза."""
    old = prev["components"][key]
    entry = dict(old) if isinstance(old, dict) else {"version": str(old)}
    rel = str(prev.get("release") or "").strip().lstrip("vV")
    if rel:
        entry.setdefault("tag", "v" + rel)
        entry.setdefault("asset", "%s_v%s.zip" % ("web_MVS" if key == "main" else SERVICES[key], rel))
    return entry


def finalize_manifest(cand: dict, prev, dist: Path):
    """(манифест для публикации, список имён архивов для выкладки). Изменившимся — tag этого релиза и имя архива, остальным — прошлая запись."""
    release = cand["release"]
    comps, publish = {}, []
    for key, c in cand["components"].items():
        if prev is not None and key in (prev.get("components") or {}) and not is_changed(key, c["version"], prev):
            comps[key] = _carry(prev, key)
            continue
        name = asset_name(key, c["version"])
        if not (dist / name).is_file():
            raise FileNotFoundError("нет архива %s — компонент изменился, его надо собрать" % (dist / name))
        comps[key] = dict(c, tag="v" + release, asset=name)
        publish.append(name)
    return {"release": release, "components": comps}, publish


def release_notes(manifest: dict, publish: list) -> str:
    comps = manifest["components"]
    names = {"main": "приложение"}
    new = ["%s %s" % (names.get(k, k), comps[k]["version"]) for k in comps if comps[k].get("asset") in publish]
    old = ["%s %s" % (names.get(k, k), comps[k]["version"]) for k in comps if comps[k].get("asset") not in publish]
    out = "Изменилось: " + ", ".join(new) + "."
    if old:
        out += "\nБез изменений (в этот релиз не входят): " + ", ".join(old) + "."
    return out + "\n"


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


def gh_release_assets(tag: str) -> set:
    """Имена файлов релиза tag (gh release view). Бросает RuntimeError, если релиз не открылся."""
    p = subprocess.run(["gh", "release", "view", tag, "--json", "assets", "--jq", ".assets[].name"],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "gh вернул ошибку").strip())
    return set(p.stdout.split())


def verify_carried(manifest: dict, publish: list, lister=None) -> list:
    """Каждый компонент, чей архив лежит в прошлом релизе, обязан там быть: клиенты берут его по (tag, asset) из манифеста.
    Нет в релизе, релиз удалён или у записи нет tag/asset — ошибка (иначе неверная запись переезжала бы из манифеста в манифест)."""
    errs, cache = [], {}
    lister = lister or gh_release_assets
    for key, c in manifest["components"].items():
        tag, asset = c.get("tag"), c.get("asset")
        if asset in publish:
            continue
        if not tag or not asset:
            errs.append("%s: в манифесте нет tag/asset — не знаю, где лежит архив (в прошлом манифесте нет поля release?)" % key)
            continue
        if tag not in cache:
            try:
                cache[tag] = lister(tag)
            except Exception as exc:
                cache[tag] = exc
        names = cache[tag]
        if isinstance(names, Exception):
            errs.append("%s: релиз %s не открылся (%s) — релизы из манифеста нельзя удалять" % (key, tag, names))
        elif asset not in names:
            errs.append("%s: в релизе %s нет файла %s" % (key, tag, asset))
    return errs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-versions", action="store_true")
    ap.add_argument("--plan", default="")
    ap.add_argument("--verify", action="store_true", help="проверить по GitHub, что архивы не менявшихся компонентов лежат там, куда ссылается манифест")
    ap.add_argument("--dist", default=str(ROOT / "dist"))
    ap.add_argument("--prev", default="")
    a = ap.parse_args(argv)
    if a.write_versions:
        write_service_versions(ROOT)
        return 0
    if a.verify:
        dist = Path(a.dist)
        manifest = json.loads((dist / "components.json").read_text(encoding="utf-8-sig"))
        publish = [Path(line.strip()).name for line in (dist / "release_files.txt").read_text(encoding="utf-8").splitlines() if line.strip()]
        errs = verify_carried(manifest, publish)
        for e in errs:
            print("[manifest] STOP: " + e)
        if not errs:
            print("[manifest] архивы не менявшихся компонентов на месте (GitHub)")
        return 1 if errs else 0
    prev = None
    if a.prev and Path(a.prev).is_file():
        try:
            prev = json.loads(Path(a.prev).read_text(encoding="utf-8-sig"))
        except ValueError:
            print("[manifest] WARNING: прошлый манифест не читается — собираем всё, проверка изменений пропущена")
    else:
        print("[manifest] WARNING: прошлого манифеста нет (первый релиз с манифестом или нет связи) — собираем всё, проверка изменений пропущена")
    if prev is not None and not (isinstance(prev, dict) and isinstance(prev.get("components"), dict)):
        print("[manifest] WARNING: прошлый манифест не того формата — собираем всё, проверка изменений пропущена")
        prev = None
    if a.plan:
        plan = make_plan(ROOT, prev)
        write_plan_cmd(Path(a.plan), plan)
        print("[manifest] план: " + ", ".join("%s %s%s" % (k, v, "" if plan[k] else " (без изменений)") for k, v in plan["versions"].items()))
        return 0
    dist = Path(a.dist)
    cand = build_manifest(dist, ROOT)
    errs = check_against_prev(cand, prev)
    if prev is not None and not any(is_changed(k, c["version"], prev) for k, c in cand["components"].items()):
        errs.append("ни у одного компонента версия не выше прошлого релиза — выкладывать нечего (поднимите VERSION или component_versions.json)")
    if errs:
        for e in errs:
            print("[manifest] STOP: " + e)
        return 1
    manifest, publish = finalize_manifest(cand, prev, dist)
    for f in dist.glob("*.zip"):                        # собранное, но не выкладываемое (не менялось) — убрать, чтобы не уехало случайно
        if f.name not in publish:
            f.unlink()
    out = dist / "components.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (dist / "release_files.txt").write_text("\n".join(["dist\\" + n for n in publish] + ["dist\\components.json"]) + "\n", encoding="utf-8")
    (dist / "release_notes.txt").write_text(release_notes(manifest, publish), encoding="utf-8")
    print("[manifest] %s: release %s, " % (out.name, manifest["release"]) +
          ", ".join("%s %s" % (k, v["version"]) for k, v in manifest["components"].items()))
    print("[manifest] выкладываем: " + ", ".join(publish))
    return 0


if __name__ == "__main__":
    sys.exit(main())
