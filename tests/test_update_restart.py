"""update.ps1: слежение за приложением после обновления, повторный запуск и откат — на заглушке web_MVS.exe.

Заглушка — маленький exe на C# (компилируется Add-Type из исходника ниже): слушает порт и отвечает {"version": ...} из
_internal\\VERSION, а поведение по номеру запуска берёт из stub.cfg (ok / die:N — упасть с кодом 0xC0000005 через N с после первого
ответа / hang:N — через N с перестать отвечать, оставаясь живым). Так проверяется то, что на заводе случалось после обновления:
приложение ответило и тут же пропало или зависло.

ВНИМАНИЕ: update.ps1 останавливает приложение по ИМЕНИ процесса (web_MVS), поэтому тест не запускается, если на машине уже
работает настоящий web_MVS.exe, — чужой процесс он трогать не должен.
"""
import shutil
import socket
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="update.ps1 — только Windows PowerShell")

STUB_CS = r'''
using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
class Stub {
    static int Main(string[] args) {
        string dir = AppDomain.CurrentDomain.BaseDirectory;
        var cfg = new Dictionary<string, string>();
        foreach (var l in File.ReadAllLines(Path.Combine(dir, "stub.cfg"))) {
            int i = l.IndexOf('=');
            if (i > 0) cfg[l.Substring(0, i).Trim()] = l.Substring(i + 1).Trim();
        }
        int port = int.Parse(cfg["port"]);
        string ver = File.ReadAllText(Path.Combine(dir, "_internal", "VERSION")).Trim();
        string runsFile = Path.Combine(dir, "stub_runs.txt");
        int run = (File.Exists(runsFile) ? File.ReadAllLines(runsFile).Length : 0) + 1;
        File.AppendAllText(runsFile, ver + "\r\n");
        Directory.CreateDirectory(Path.Combine(dir, "logs"));
        string log = Path.Combine(dir, "logs", "app.log");
        File.AppendAllText(log, "=== stub " + ver + " run " + run + " args " + string.Join(" ", args) + " ===\r\n");
        string mode = cfg.ContainsKey("run" + run) ? cfg["run" + run] : "ok";
        if (mode == "dead") { File.AppendAllText(log, "RuntimeError: stub died at start\r\n"); return 1; }
        int after = mode.Contains(":") ? int.Parse(mode.Split(':')[1]) : 0;
        var l2 = new TcpListener(IPAddress.Loopback, port);
        l2.Start();
        DateTime? firstAnswer = null;
        while (true) {
            if (firstAnswer.HasValue && (DateTime.UtcNow - firstAnswer.Value).TotalSeconds >= after) {
                if (mode.StartsWith("die")) {
                    File.AppendAllText(log, "Traceback (most recent call last): stub crash\r\n");
                    Environment.Exit(unchecked((int)0xC0000005));
                }
                if (mode.StartsWith("hang")) { Thread.Sleep(Timeout.Infinite); }
            }
            if (!l2.Pending()) { Thread.Sleep(50); continue; }
            using (var c = l2.AcceptTcpClient()) {
                c.ReceiveTimeout = 2000;
                var s = c.GetStream();
                var buf = new byte[4096];
                try { s.Read(buf, 0, buf.Length); } catch { }
                string body = "{\"version\":\"" + ver + "\"}";
                string resp = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + Encoding.UTF8.GetByteCount(body) + "\r\nConnection: close\r\n\r\n" + body;
                var b = Encoding.UTF8.GetBytes(resp);
                s.Write(b, 0, b.Length);
                if (!firstAnswer.HasValue) firstAnswer = DateTime.UtcNow;
            }
        }
    }
}
'''


def _ps(script: str, timeout=120):
    return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)


def _web_mvs_running() -> bool:
    return bool(_ps("@(Get-Process -Name web_MVS -ErrorAction SilentlyContinue).Count").stdout.strip() not in ("", "0"))


@pytest.fixture(scope="module")
def stub_exe(tmp_path_factory):
    if _web_mvs_running():
        pytest.skip("на машине уже работает web_MVS.exe — update.ps1 остановил бы и его")
    d = tmp_path_factory.mktemp("stub")
    (d / "Stub.cs").write_text(STUB_CS, encoding="utf-8")
    exe = d / "web_MVS.exe"
    r = _ps("Add-Type -TypeDefinition (Get-Content -LiteralPath '%s' -Raw) -OutputAssembly '%s' -OutputType ConsoleApplication" % (d / "Stub.cs", exe))
    if not exe.is_file():
        pytest.skip("не удалось скомпилировать заглушку exe: %s" % (r.stdout + r.stderr))
    return exe


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _install(tmp_path: Path, stub: Path, port: int, cfg: str):
    root = tmp_path / "каталог установки"
    (root / "_internal").mkdir(parents=True)
    shutil.copy(stub, root / "web_MVS.exe")
    (root / "_internal" / "VERSION").write_text("2.0.0\r\n", encoding="utf-8")
    (root / "update.ps1").write_bytes((ROOT / "update.ps1").read_bytes())
    (root / "stub.cfg").write_text("port=%d\r\n%s" % (port, cfg), encoding="utf-8")
    zp = tmp_path / "web_MVS_v2.0.1.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.write(stub, "web_MVS.exe")
        z.writestr("_internal/VERSION", "2.0.1\r\n")
        z.writestr("_internal/data.txt", "x")
    return root, zp


def _run_update(root: Path, zp: Path, port: int, stable: int, *extra):
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(root / "update.ps1"), "-Root", str(root), "-NoElevate",
           "-NoMenu", "-Components", "main", "-ZipPath", str(zp), "-Port", str(port), "-StableSec", str(stable), "-NoBrowser", *extra]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=200, stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout + p.stderr
    finally:
        _ps("Get-Process -Name web_MVS -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '%s*' } | Stop-Process -Force" % root)


def _runs(root: Path):
    f = root / "stub_runs.txt"
    return f.read_text(encoding="utf-8").split() if f.is_file() else []


def test_stable_app_passes_watch(tmp_path, stub_exe):
    port = _free_port()
    root, zp = _install(tmp_path, stub_exe, port, "run1=ok\r\n")
    code, out = _run_update(root, zp, port, 6)
    assert code == 0, out
    assert "слежу за приложением 6 с" in out and "приложение стабильно" in out and "ГОТОВО: было 2.0.0  →  стало 2.0.1" in out, out
    assert _runs(root) == ["2.0.1"], "один запуск, без перезапусков"
    assert "--no-open" in (root / "logs" / "app.log").read_text(encoding="utf-8"), "-NoBrowser передан приложению"
    assert (root / "_internal" / "VERSION").read_text().strip() == "2.0.1"


def test_crash_right_after_answer_restarts_once_with_diagnostics(tmp_path, stub_exe):
    """Ответило и упало через 2 с (как на заводе после обновления): код выхода и хвост журнала — в update.log, повторный запуск, версия новая."""
    port = _free_port()
    root, zp = _install(tmp_path, stub_exe, port, "run1=die:2\r\nrun2=ok\r\n")
    code, out = _run_update(root, zp, port, 6)
    assert code == 0, out
    assert "завершилось через" in out, out
    assert "C0000005" in out, "код выхода записан"
    assert "stub crash" in out, "хвост logs\\app.log попал в update.log"
    assert "запускаю ещё раз" in out and "приложение стабильно" in out, out
    assert _runs(root) == ["2.0.1", "2.0.1"], "ровно один повторный запуск"
    assert (root / "_internal" / "VERSION").read_text().strip() == "2.0.1", "новая версия осталась"
    assert not (root / ".update_backup").exists(), "бэкап удалён только после удачного запуска"


def test_crash_twice_rolls_back_to_previous_version(tmp_path, stub_exe):
    port = _free_port()
    root, zp = _install(tmp_path, stub_exe, port, "run1=die:2\r\nrun2=die:2\r\nrun3=ok\r\n")
    code, out = _run_update(root, zp, port, 6)
    assert code == 1, out
    assert "запускаю ещё раз" in out and "новая версия не поднялась — возвращаю прежнюю" in out and "откат выполнен, снова версия 2.0.0" in out, out
    assert (root / "_internal" / "VERSION").read_text().strip() == "2.0.0", "прежняя версия возвращена"
    assert _runs(root) == ["2.0.1", "2.0.1", "2.0.0"], "новая — дважды, потом прежняя"


def test_hung_app_is_detected_and_restarted(tmp_path, stub_exe):
    """Жив, но молчит (как при заморозке консоли): три проверки подряд без ответа — перезапуск."""
    port = _free_port()
    root, zp = _install(tmp_path, stub_exe, port, "run1=hang:1\r\nrun2=ok\r\n")
    code, out = _run_update(root, zp, port, 30)
    assert code == 0, out
    assert "не отвечает 3 проверки подряд" in out and "запускаю ещё раз" in out and "приложение стабильно" in out, out
    assert _runs(root) == ["2.0.1", "2.0.1"]


def test_stable_sec_zero_skips_watch(tmp_path, stub_exe):
    """-StableSec 0 — прежнее поведение: ответило — готово, без слежения."""
    port = _free_port()
    root, zp = _install(tmp_path, stub_exe, port, "run1=die:2\r\n")
    code, out = _run_update(root, zp, port, 0)
    assert code == 0, out
    assert "слежу за приложением" not in out and "приложение запущено, версия 2.0.1" in out, out
