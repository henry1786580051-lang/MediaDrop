# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for MediaDrop Flask server
# Uses onedir mode (COLLECT) to avoid temp-directory DLL loading issues on Windows.
# Usage: pyinstaller mediadrop-server.spec --distpath ../bin-dist

import sys

block_cipher = None

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('templates', 'templates'),
        ('static', 'static'),
        ('fonts', 'fonts'),
    ],
    hiddenimports=[
        'flask',
        'jinja2',
        'werkzeug',
        'werkzeug.middleware.proxy_fix',
        'markupsafe',
        'itsdangerous',
        'click',
        'blinker',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'numpy', 'pandas', 'matplotlib'],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# OneDir: EXE with exclude_binaries=True creates a minimal bootloader;
# all binaries/zipfiles/datas are collected alongside it by COLLECT.
exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name='mediadrop-server',
    debug=False,
    bootloader_ignore_signals=False,
    strip=sys.platform != 'win32',
    upx=False,
    console=True,
    target_arch=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=sys.platform != 'win32',
    upx=False,
    name='mediadrop-server',
)
