# -*- mode: python ; coding: utf-8 -*-
#
# Builds the frozen shyyctl.exe this project's Windows installer wraps.
#
# Run from the repo root:
#   pyinstaller --noconfirm tools/build_installer/shyyctl.spec
#
# --onedir (the default this spec produces, via COLLECT below - NOT
# --onefile) is deliberate: main.py's ROOT now resolves to
# Path(sys.executable).resolve().parent when frozen, which only gives a
# stable, permanent location for config.yaml/curve.json/layout.json/etc.
# under --onedir. --onefile re-extracts its bundle to a fresh temp
# directory on every single launch (sys._MEIPASS), which would make every
# bit of this app's persistent state vanish the moment it restarts -
# --onedir avoids ever needing a separate "bundled read-only asset path"
# vs "persistent writable state path" distinction in main.py at all.
#
# vendor/ (ffmpeg.exe, OpenRGB/, SensorHelper/ - built by
# tools/setup_vendor.py) is deliberately NOT bundled here. It's tens to a
# couple hundred MB of third-party binaries that the release workflow
# fetches once, straight into the installer's staging directory,
# alongside this PyInstaller output - see .github/workflows/release.yml
# and tools/build_installer/installer.nsi.

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

block_cipher = None

REPO_ROOT = Path(SPECPATH).resolve().parent.parent

# Optional: Use a custom icon if it exists at tools/build_installer/shyyctl.ico
# To create from an image, run: python tools/build_installer/create_icon.py your_image.png
# SPECPATH is this spec's own folder (tools/build_installer) - the icon sits
# right in it. Looking one level up (tools/) found nothing, and every build
# silently fell back to PyInstaller's default icon.
ICON_PATH = Path(SPECPATH).resolve() / "shyyctl.ico"
ICON = str(ICON_PATH) if ICON_PATH.exists() else None

extra_datas = [
    (str(REPO_ROOT / "web"), "web"),
]
extra_binaries = []
extra_hiddenimports = [
    # uvicorn resolves its actual event-loop/protocol implementations
    # via importlib at runtime (see uvicorn.loops.auto/
    # uvicorn.protocols.http.auto/uvicorn.protocols.websockets.auto) -
    # PyInstaller's static import scan can't see through that, so
    # these need to be listed explicitly or the frozen exe fails at
    # startup with "ImportError: no module named ..." the first time
    # uvicorn actually tries to pick a loop/protocol.
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
]

# Collected wholesale rather than as individual hiddenimports: both wrap a
# native USB backend (libusb_package ships libusb-1.0.dll itself; liquidctl
# depends on it through pyusb) that PyInstaller's import scan can't
# discover on its own, since it's loaded via ctypes/find_library at
# runtime, not a plain Python import. Must run BEFORE Analysis() - its
# 2-tuple (src, dest) hook-style results are the format Analysis's own
# datas/binaries parameters expect, not the 3-tuple TOC format Analysis
# produces internally (appending these to a.datas/a.binaries *after*
# Analysis has already normalized them to TOC entries mismatches the
# tuple arity and breaks COLLECT later).
for pkg in ("libusb_package", "liquidctl", "usb"):
    try:
        datas, binaries, hiddenimports = collect_all(pkg)
        extra_datas += datas
        extra_binaries += binaries
        extra_hiddenimports += hiddenimports
    except Exception:
        # Best-effort - if a package isn't installed in this build
        # environment at all, collect_all has nothing to collect and
        # would only raise; the real failure (missing dependency) is
        # already obvious from requirements.txt not being honoured, no
        # need to also crash spec evaluation over it.
        pass

a = Analysis(
    [str(REPO_ROOT / "main.py")],
    pathex=[str(REPO_ROOT)],
    binaries=extra_binaries,
    datas=extra_datas,
    hiddenimports=extra_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# config.yaml is bundled under a different name than it has in the repo -
# renamed to config.example.yaml so a future installer upgrade never ships
# a file that could overwrite a user's already-tuned live config.yaml.
# main.py's startup copies this to config.yaml itself, once, only if
# config.yaml doesn't already exist next to the exe. Appended here (post-
# Analysis, TOC triple format) rather than via extra_datas above since
# this one genuinely does need the rename Analysis's datas param can't do.
a.datas += [("config.example.yaml", str(REPO_ROOT / "config.yaml"), "DATA")]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="shyyctl",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="shyyctl",
)
