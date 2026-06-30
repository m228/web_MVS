# PyInstaller spec для web_MVS (one-folder бандл).
#
# Сборка:  pyinstaller --noconfirm web_MVS.spec   (или просто build.bat)
# Результат: dist\web_MVS\  — самодостаточная папка с web_MVS.exe.
#
# Особенности:
# * ассеты (page\, Driver\, VERSION) PyInstaller кладёт в подпапку _internal\;
#   код находит их через sys._MEIPASS (см. paths.py: BUNDLE_DIR).
# * collect_all('genicam'/'harvesters') — у них нативные .pyd/DLL, без явного
#   сбора PyInstaller их пропустит и камера не заведётся.
# * uvicorn выбирает loop/protocol-реализации динамически по строке "app:app",
#   поэтому тянем все его подмодули + локальные модули как hiddenimports.
# * PySide6/shiboken6 исключены — в коде не используются (лишние ~150-200 МБ).

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [('page', 'page'), ('Driver', 'Driver'), ('VERSION', '.')]
binaries = []
hiddenimports = ['app', 'camera_core', 'rtsp_store', 'net_tools', 'paths', 'logger', 'updater']
hiddenimports += collect_submodules('uvicorn')

for pkg in ('genicam', 'harvesters'):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        'PySide6', 'PySide6_Addons', 'PySide6_Essentials', 'shiboken6',
        'PyQt5', 'PyQt6', 'tkinter', 'matplotlib',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='web_MVS',
    console=True,
    strip=False,
    upx=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='web_MVS',
)
