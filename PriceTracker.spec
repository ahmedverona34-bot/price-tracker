# -*- mode: python ; coding: utf-8 -*-
#
# Playwright is deliberately NOT bundled.
#
# It ships a private copy of node (~89 MB) and every configured site fetches
# over plain HTTP, so it weighs ~100 MB onto a build that is otherwise ~26 MB.
# price_tracker.py degrades gracefully when it is missing: a site asking for
# "use_playwright" reports a plain Arabic notice instead of crashing. To build
# WITH browser support, delete the excludes below and add 'playwright' to
# hiddenimports.
#
# pythonnet and clr_loader MUST stay. They are not Playwright's own
# dependencies: pywebview loads the Edge WebView2 control through pythonnet's
# .NET bridge, so excluding them makes the window fail to open at all with
# "You must have pythonnet installed in order to use pywebview".
EXCLUDES = [
    'tkinter',
    'playwright',
    'greenlet',
    'pyee',
]

a = Analysis(
    ['price_tracker.py'],
    pathex=[],
    binaries=[],
    datas=[('ui', 'ui'), ('sites.json', '.')],
    hiddenimports=['pythonnet', 'clr_loader'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PriceTracker',
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
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='PriceTracker',
)
