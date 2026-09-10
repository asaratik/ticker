# PyInstaller spec -- the single source of truth for packaging.
#
# This file is tracked, and build.py builds *from* it. That matters: passing
# options on the PyInstaller command line makes it generate a .spec of its
# own and overwrite whatever is there, so anything added to a generated spec
# silently disappears on the next build. Everything packaging-related
# therefore lives here.
#
# onedir, not onefile. The onefile bootloader unpacks an
# embedded archive to a temp directory and executes from there, which is
# behaviourally identical to a dropper, and heuristic AV engines flag it on
# that basis alone -- signing does not fully fix it. A folder wrapped in an
# installer costs the user nothing and trips far fewer scanners.

import os
import sys

from PyInstaller.utils.hooks import collect_all

ROOT = os.path.dirname(SPECPATH)          # noqa: F821 -- injected by PyInstaller

datas = []
binaries = []
hiddenimports = []

# The migrations and the schema snapshot are read off disk at runtime, so
# PyInstaller -- which only follows imports -- cannot discover them. Without
# these, the first run of a packaged build finds no migrations and cannot
# create its schema.
datas += [
    (os.path.join(ROOT, "ticker", "db", "migrations", "*.sql"),
     os.path.join("ticker", "db", "migrations")),
    (os.path.join(ROOT, "ticker", "db", "schema.sql"),
     os.path.join("ticker", "db")),
]

if sys.platform == "win32":
    # bleak's Windows BLE backend uses the winrt bindings, which
    # PyInstaller's static analysis doesn't fully trace on its own -- without
    # this the packaged exe silently fails to connect.
    win_datas, win_binaries, win_hidden = collect_all("winrt")
    datas += win_datas
    binaries += win_binaries
    hiddenimports += win_hidden

a = Analysis(
    [os.path.join(ROOT, "ticker", "ui", "app.py")],
    pathex=[ROOT],
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

pyz = PYZ(a.pure)                          # noqa: F821

exe = EXE(                                 # noqa: F821
    pyz,
    a.scripts,
    [],
    # onedir: the binaries and data are collected alongside rather than
    # embedded in the executable.
    exclude_binaries=True,
    name="Ticker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX compresses the executable, and a packed executable is another thing
    # AV heuristics dislike. The few megabytes are not worth the false
    # positives (section 12.3).
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(                            # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Ticker",
)

if sys.platform == "darwin":
    app = BUNDLE(                          # noqa: F821
        coll,
        name="Ticker.app",
        icon=None,
        bundle_identifier="com.ticker.app",
        info_plist={
            # Without this key macOS refuses Bluetooth outright, signed or
            # not (section 12.5). The hardened runtime additionally needs the
            # com.apple.security.device.bluetooth entitlement, applied at
            # codesign time -- not set up yet, see packaging/README.md.
            "NSBluetoothAlwaysUsageDescription":
                "Ticker reads heart rate from your Bluetooth chest strap or watch.",
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
        },
    )
