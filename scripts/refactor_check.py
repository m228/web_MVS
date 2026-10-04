"""refactor_check — доказывает, что механический перенос кода при рефакторинге ничего не потерял и не изменил.

Эталон берётся прямо из git (тег `pre-refactor-1.7.28`), отдельный снимок хранить не нужно.

    python scripts/refactor_check.py            # проверить все цели
    python scripts/refactor_check.py multi      # только multi.js
    python scripts/refactor_check.py --ref <тег/коммит>

Что проверяется для каждой цели (старый файл из эталона  vs  новые файлы рабочего дерева):
  1. СТРОКИ: каждая значимая строка (strip, без пустых) старого кода есть в новом столько же раз.
     «Потерянные» строки = FAIL (что-то выкинули/переписали). «Добавленные» — печатаются для ручного
     просмотра: это должны быть только оболочки (импорты, <script>-обёртка, namespace).
  2. Python: AST-отпечаток КАЖДОЙ функции/класса/метода/константы верхнего уровня совпадает с эталоном
     (тело функции не изменилось ни на символ логики), py_compile всех новых файлов.
  3. JS: множество объявленных функций совпадает; `node --check` всех новых файлов (если есть node).
Код возврата: 0 — всё совпало, 1 — есть расхождения.
"""
import ast
import hashlib
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REF = "pre-refactor-1.7.28"

# имя цели -> (старые файлы в эталоне, glob новых файлов в рабочем дереве, вид)
TARGETS = {
    "multi": (["page/static/js/multi.js"], ["page/static/js/multi/*.js"], "js"),
    "microscope": (["page/static/js/microscope.js"], ["page/static/js/microscope*.js", "page/static/js/microscope/*.js"], "js"),
    "camera_core": (["camera_core.py"], ["camera_core/*.py"], "py"),
}


def git_show(ref, path):
    out = subprocess.run(["git", "show", "%s:%s" % (ref, path)], cwd=ROOT, capture_output=True)
    if out.returncode != 0:
        raise SystemExit("не нашёл %s в %s: %s" % (path, ref, out.stderr.decode("utf-8", "replace")))
    return out.stdout.decode("utf-8")


def sig_lines(text):
    return Counter(ln.strip() for ln in text.splitlines() if ln.strip())


def py_fingerprint(text):
    """{квалифицированное имя -> sha1(ast.dump)} для def/class/методов и констант верхнего уровня."""
    tree = ast.parse(text)
    fp = {}

    def put(name, node):
        key, i = name, 1
        while key in fp:
            i += 1
            key = "%s#%d" % (name, i)
        fp[key] = hashlib.sha1(ast.dump(node).encode("utf-8")).hexdigest()[:12]

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            put(node.name, node)
        elif isinstance(node, ast.ClassDef):
            put(node.name + " (class-header)", ast.ClassDef(node.name, node.bases, node.keywords, [], node.decorator_list))
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    put("%s.%s" % (node.name, sub.name), sub)
                else:
                    put("%s.<%s>" % (node.name, type(sub).__name__), sub)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = ",".join(ast.unparse(t) for t in targets)
            put("=" + names, node)
    return fp


JS_FUNC = re.compile(r"\bfunction\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(")


def js_funcs(text):
    return Counter(JS_FUNC.findall(text))


def node_check(path):
    r = subprocess.run(["node", "--check", str(path)], capture_output=True)
    return r.returncode == 0, r.stderr.decode("utf-8", "replace").strip()


def have_node():
    try:
        return subprocess.run(["node", "--version"], capture_output=True).returncode == 0
    except OSError:
        return False


def check_target(name, ref):
    olds, new_globs, kind = TARGETS[name]
    new_files = []
    for g in new_globs:
        new_files += sorted(ROOT.glob(g))
    new_files = sorted(set(new_files))
    print("\n=== %s (%s) ===" % (name, kind))
    if not new_files:
        print("  новых файлов нет — рефакторинг этой цели ещё не делался (старый файл на месте)")
        return True
    old_text = "\n".join(git_show(ref, p) for p in olds)
    new_texts = {f: f.read_text(encoding="utf-8") for f in new_files}
    new_text = "\n".join(new_texts.values())
    ok = True

    # 1. строки
    a, b = sig_lines(old_text), sig_lines(new_text)
    lost = a - b
    added = b - a
    print("  файлов: %d, строк: было %d → стало %d" % (len(new_files), sum(a.values()), sum(b.values())))
    if lost:
        ok = False
        print("  FAIL потеряно/изменено строк: %d" % sum(lost.values()))
        for ln, n in list(lost.items())[:40]:
            print("     - %s%s" % (ln[:140], (" ×%d" % n) if n > 1 else ""))
    else:
        print("  OK   ни одна строка эталона не потеряна")
    if added:
        print("  добавлено строк: %d (проверь глазами — должны быть только оболочки):" % sum(added.values()))
        for ln, n in list(added.items())[:60]:
            print("     + %s%s" % (ln[:140], (" ×%d" % n) if n > 1 else ""))

    if kind == "py":
        import py_compile
        for f in new_files:
            try:
                py_compile.compile(str(f), doraise=True)
            except py_compile.PyCompileError as e:
                ok = False
                print("  FAIL py_compile %s: %s" % (f.name, e))
        fo = py_fingerprint(old_text)
        fn = {}
        for f, t in new_texts.items():
            for k, v in py_fingerprint(t).items():
                key, i = k, 1
                while key in fn:
                    i += 1
                    key = "%s#%d" % (k, i)
                fn[key] = v
        missing = sorted(set(fo) - set(fn))
        changed = sorted(k for k in fo if k in fn and fo[k] != fn[k])
        extra = sorted(set(fn) - set(fo))
        if missing or changed:
            ok = False
        print("  %s AST: определений в эталоне %d, найдено %d; потеряно %d, изменено %d, новых %d" % (
            "OK  " if not (missing or changed) else "FAIL", len(fo), len(fn) - len(extra), len(missing), len(changed), len(extra)))
        for k in missing[:30]:
            print("     потеряно: %s" % k)
        for k in changed[:30]:
            print("     ИЗМЕНЕНО: %s" % k)
        for k in extra[:30]:
            print("     новое:    %s" % k)
    else:
        fo, fn = js_funcs(old_text), js_funcs(new_text)
        if fo != fn:
            ok = False
            print("  FAIL множество функций расходится: потеряно %s, лишних %s" % (list((fo - fn).keys())[:20], list((fn - fo).keys())[:20]))
        else:
            print("  OK   функций: %d, имена и количество совпали" % sum(fo.values()))
        if have_node():
            bad = []
            for f in new_files:
                good, err = node_check(f)
                if not good:
                    bad.append((f.name, err[:300]))
            if bad:
                ok = False
                for n, e in bad:
                    print("  FAIL node --check %s: %s" % (n, e))
            else:
                print("  OK   node --check: %d файлов" % len(new_files))
        else:
            print("  --   node не найден, синтаксис JS не проверен")
    return ok


def main():
    args = sys.argv[1:]
    ref = DEFAULT_REF
    if "--ref" in args:
        i = args.index("--ref")
        ref = args[i + 1]
        del args[i:i + 2]
    names = args or list(TARGETS)
    bad = [n for n in names if n not in TARGETS]
    if bad:
        raise SystemExit("неизвестная цель: %s (есть: %s)" % (bad, list(TARGETS)))
    print("эталон: %s" % ref)
    results = {n: check_target(n, ref) for n in names}
    print("\n" + "=" * 60)
    for n, r in results.items():
        print("  %-12s %s" % (n, "OK" if r else "FAIL"))
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
