# PyInstaller spec for the MED17.7.5 Flash Tool desktop app.
# Build:  pyinstaller packaging/med17flasher.spec
# Output: dist/med17flasher-desktop  (a single downloadable executable)
#
# Bundles the built React UI (webui/dist) and the ECU profiles (config/) as
# data so the one-file executable serves the UI and loads profiles offline.

import os
import sys

block_cipher = None

ROOT = os.path.abspath(os.getcwd())

app_icon = None
_ico = os.path.join(ROOT, "packaging", "icon.ico")
if sys.platform == "win32" and os.path.isfile(_ico):
    app_icon = _ico

datas = [
    (os.path.join(ROOT, "webui", "dist"), "webui/dist"),
    (os.path.join(ROOT, "config"), "config"),
    (os.path.join(ROOT, "packaging", "icon.png"), "."),
    # A plain-source copy of the package for the 32-bit helper processes.
    # Vendor seed/key DLLs and J2534 PassThru drivers (Tactrix's op20pt32.dll)
    # are 32-bit, so this 64-bit build drives them through a separate 32-bit
    # Python - which cannot import out of PyInstaller's archive and needs real
    # .py files. Path must match core.procbridge.BRIDGE_SRC_DIR.
    (os.path.join(ROOT, "med17flasher"), "bridge_src/med17flasher"),
]
binaries = []

# Optional integrations light up only if installed; don't hard-require them.
hiddenimports = []
for opt in ("can", "serial", "webview", "yaml"):
    try:
        __import__(opt)
        hiddenimports.append(opt)
    except Exception:
        pass

# Bundle pywebview (native window) and its platform backend when installed on
# the build machine. collect_all pulls the backend submodules + data the
# PyInstaller hook needs; guarded so a build host without it still works.
for _pkg in ("webview", "clr_loader", "pythonnet"):
    try:
        from PyInstaller.utils.hooks import collect_all

        _d, _b, _h = collect_all(_pkg)
        datas += _d
        binaries += _b
        hiddenimports += _h
    except Exception:
        pass

# A windowed app (no console) on Windows/macOS so it feels like a real desktop
# app; keep a console on Linux where a native webview backend is less certain.
console_flag = sys.platform not in ("win32", "darwin")

a = Analysis(
    [os.path.join(ROOT, "packaging", "desktop_entry.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest"],
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
    name="med17flasher-desktop",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=console_flag,  # windowed (no terminal) on Windows/macOS
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=app_icon,   # Windows .ico when building on win32, else None
)
