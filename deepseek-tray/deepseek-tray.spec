# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['run.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=['pystray._win32'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pystray._xorg', 'pystray._gtk', 'pystray._appindicator', 'pystray._darwin', 'yaml', 'numpy', 'pandas', 'matplotlib', 'scipy', 'cv2', 'onnxruntime', 'rapidocr', 'playwright', 'mss', 'pyzbar', 'shapely', 'lxml', 'PyPDF2', 'docx', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'setuptools', 'pip'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='deepseek-tray',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets\\app.ico'],
)
