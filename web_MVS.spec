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
# * pymodbus (страница /microscope) копируется в _internal\pymodbus как datas
#   напрямую (import pymodbus + путь к папке пакета). Пакет чистый python (нативных
#   .pyd нет), поэтому копия папкой проще collect_all и вдобавок fail-fast: если
#   pymodbus не установлен в сборочном venv — сборка падает с ModuleNotFoundError
#   прямо тут, а не выпускает молча-битый бандл (как раньше падал старт микроскопа).
#   ВАЖНО: pymodbus должен стоять в venv, которым собирает PyInstaller (см. requirements).
# * uvicorn выбирает loop/protocol-реализации динамически по строке "app:app",
#   поэтому тянем все его подмодули + локальные модули как hiddenimports.
# * рядом с web_MVS.exe собирается ОТДЕЛЬНЫЙ SelfCheck.exe (selfcheck.py) — проверка всех модулей
#   ТЕМ ЖЕ бандлом (общие _internal\): оба exe в одном COLLECT, зависимости не дублируются (MERGE).
# * PySide6/shiboken6 исключены — в коде не используются (лишние ~150-200 МБ).

import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [('page', 'page'), ('Driver', 'Driver'), ('VERSION', '.')]
binaries = []
hiddenimports = ['app', 'camera_core', 'rtsp_store', 'net_tools', 'paths', 'logger', 'updater', 'diag',
                 'dahua_control', 'sdk_gige',
                 # микроскоп (страница /microscope): плата по Modbus TCP + автомат
                 'microscope_service', 'microscope_plc', 'microscope_fsm', 'plate_config', 'sv_source']
# selfcheck.py грузит модули по имени строкой (importlib) — PyInstaller их не увидит сам
selfcheck_imports = ['selfcheck', 'cv_analyzer', 'cv_fracture', 'cv_client', 'cv_store', 'fracture_lab',
                     'autostart', 'multipart', 'starlette', 'fastapi', 'pydantic']
hiddenimports += collect_submodules('uvicorn')
# вложенная обёртка MVS SDK (mvsdk/) — динамические импорты, тянем все подмодули
hiddenimports += collect_submodules('mvsdk')
# camera_core — пакет (бывший camera_core.py): подмодули тоже явно, чтобы ничего не потерялось
hiddenimports += collect_submodules('camera_core')

for pkg in ('genicam', 'harvesters'):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# pymodbus — копируем всю папку пакета в _internal\pymodbus (см. шапку файла)
import pymodbus as _pymodbus
datas += [(os.path.dirname(_pymodbus.__file__), 'pymodbus')]

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

sc = Analysis(
    ['selfcheck.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports + selfcheck_imports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        'PySide6', 'PySide6_Addons', 'PySide6_Essentials', 'shiboken6',
        'PyQt5', 'PyQt6', 'tkinter', 'matplotlib',
    ],
    noarchive=False,
)

# общие зависимости обоих exe лежат в одном _internal\ и не дублируются
MERGE((a, 'run', 'web_MVS'), (sc, 'selfcheck', 'SelfCheck'))

pyz = PYZ(a.pure)
pyz_sc = PYZ(sc.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='web_MVS',
    console=True,
    strip=False,
    upx=False,
    # встраиваем манифест requireAdministrator: exe всегда запускается от админа
    # (нужно для сетевых функций: jumbo, фильтр GigE). UAC-запрос при старте — один раз,
    # без ручной настройки «Запуск от имени администратора».
    uac_admin=True,
)

# SelfCheck.exe — без UAC: проверка только читает и работает в песочнице
exe_sc = EXE(
    pyz_sc,
    sc.scripts,
    [],
    exclude_binaries=True,
    name='SelfCheck',
    console=True,
    strip=False,
    upx=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    exe_sc,
    sc.binaries,
    sc.datas,
    strip=False,
    upx=False,
    name='web_MVS',
)
