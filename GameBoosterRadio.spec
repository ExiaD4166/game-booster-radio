# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for the desktop app (app.py).

Build with: pyinstaller GameBoosterRadio.spec
Produces a onedir build in dist/GameBoosterRadio/ — the .exe plus all its
bundled DLLs and data sitting alongside it (chosen over a single-file build
since VLC's plugin folder alone is 130MB+; re-extracting that from a onefile
archive on every launch would be slow and pointless).

See LEARNING_CONCEPTS.md (Phase 10) for why this bundles VLC's DLLs/plugins
under vlc_runtime/, and what radio_player.py does with that folder at runtime.

VLC_DIR below assumes VLC is installed at the default location on the
machine doing the build (only needed at build time, not by end users).
"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

VLC_DIR = r"C:\Program Files\VideoLAN\VLC"

binaries = [
    (VLC_DIR + r"\libvlc.dll", "vlc_runtime"),
    (VLC_DIR + r"\libvlccore.dll", "vlc_runtime"),
]

datas = [
    (VLC_DIR + r"\plugins", "vlc_runtime/plugins"),
]
datas += collect_data_files("customtkinter")

hiddenimports = collect_submodules("yt_dlp")

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='GameBoosterRadio',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets/app_icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='GameBoosterRadio',
)
