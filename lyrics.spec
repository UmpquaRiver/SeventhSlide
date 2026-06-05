# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all

block_cipher = None

# uvicorn discovers its HTTP/WebSocket/loop implementations dynamically at
# runtime (Config defaults to http/ws/loop="auto"), so PyInstaller's static
# analysis can't see them. collect_all sweeps up every submodule, data file,
# and binary for these packages — bundling the whole optional-dependency tree
# (websockets, httptools, h11, ...) instead of hand-listing modules one by one.
datas = [('templates', 'templates')]
binaries = []
hiddenimports = []
for _pkg in ('uvicorn', 'websockets', 'httptools', 'multipart'):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

a = Analysis(
    ['lyrics.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='lyrics-slideshow',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # Set to False for a GUI-only app (no console window)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icons/seventhslide.ico',  # regenerate from the logo with `python icons/make_icons.py`
)
