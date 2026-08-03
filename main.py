"""shyyctl backend - a local FastAPI server tying together everything this
project confirmed working against real hardware this session: fan PWM/RPM
and hub accessory IDs via liquidctl (app/hub.py), motherboard/RAM/hub RGB
via OpenRGB (app/openrgb_bridge.py), hardware discovery (app/scan.py), and
the Tryx Panorama display's real reverse-engineered protocol
(app/panorama.py) - handshake, brightness, media upload, boot image.

Binds to 127.0.0.1 only (see config.yaml) - this is a local control panel
for hardware physically attached to this machine, never meant to be
reachable over the network.

Run:
    python main.py
"""

import asyncio
import colorsys
import contextlib
import io
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import webbrowser
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.conflicts import running_conflicts
from app.hub import Hub, HubError
from app.layout import Layout
from app.openrgb_bridge import OpenRgbBridge
from app.openrgb_raw import OpenRgbRaw
from app.ambient import AmbientSampler
from app.panorama import PanoramaError, find_print_panel, find_usb_panel, generate_thumbnail
from app.procflags import NO_WINDOW
from app.scan import Scan
from app import sensors as sensors_mod

# ROOT is where persistent, writable state lives (config.yaml, curve.json,
# layout.json, ... and vendor/, which the installer stages here directly,
# not PyInstaller) - sys.executable's parent, the stable --onedir folder
# the installer places everything in, so nothing here gets wiped or
# re-extracted between launches.
#
# ASSETS_DIR is where read-only bundled files PyInstaller itself packed
# live (web/, config.example.yaml). Confirmed empirically this session
# (PyInstaller 6.x): in --onedir mode, COLLECT puts bundled datas/binaries
# under an "_internal" subfolder next to the exe, not flat alongside it -
# so ASSETS_DIR must be sys._MEIPASS, not sys.executable's parent, or
# WEB_DIR/config.example.yaml simply wouldn't be found. sys._MEIPASS is
# still the right (and only) choice despite --onefile's usual "fresh temp
# dir every launch" caveat: in --onedir mode it points at that same stable
# _internal folder every time, it's just --onefile where it's ephemeral -
# so this never revisits the "one process, one folder" simplicity ROOT
# above relies on for actual persistent state.
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
    ASSETS_DIR = Path(sys._MEIPASS)  # type: ignore[attr-defined]
else:
    ROOT = Path(__file__).resolve().parent
    ASSETS_DIR = ROOT

_CONFIG_FILE = ROOT / "config.yaml"
_CONFIG_EXAMPLE = ASSETS_DIR / "config.example.yaml"
if not _CONFIG_FILE.is_file() and _CONFIG_EXAMPLE.is_file():
    # First run of a frozen build: config.yaml itself isn't shipped (only
    # config.example.yaml is bundled - see the PyInstaller spec) so that a
    # future installer upgrade never has a bundled file to overwrite a
    # user's already-tuned config.yaml with. Seed it once, here, rather
    # than at install time, since this is the one place that already knows
    # "config.yaml is genuinely missing" vs "the installer just hasn't run
    # yet".
    shutil.copyfile(_CONFIG_EXAMPLE, _CONFIG_FILE)

CONFIG = yaml.safe_load(_CONFIG_FILE.read_text(encoding="utf-8"))
WEB_DIR = ASSETS_DIR / "web"
CURVE_FILE = ROOT / "curve.json"
LAYOUT_FILE = ROOT / "layout.json"
PANORAMA_STATE_FILE = ROOT / "panorama_state.json"
EFFECT_STATE_FILE = ROOT / "effect_state.json"
RGB_STATE_FILE = ROOT / "rgb_state.json"
EFFECT_PRESETS_FILE = ROOT / "effect_presets.json"
GALLERY_DIR = ROOT / "media_gallery"
GALLERY_MANIFEST = GALLERY_DIR / "manifest.json"

LOG_FILE = ROOT / "shyyctl.log"
LOG_FILE_OLD = ROOT / "shyyctl.log.old"

# Bumped by hand alongside each release tag (see .github/workflows/release.yml,
# which takes the version from the tag/workflow_dispatch input, not from
# here - this is just this running process's own idea of its version, for
# the Settings > Updates check below to compare against GitHub's latest
# release tag). Nothing here ever calls out on its own; it only runs when
# a human clicks "Check for updates".
APP_VERSION = "1.0.6"
UPDATE_REPO = "paulsprung/shyyctl"

_log_handlers: list = []
if getattr(sys, "frozen", False):
    # The frozen exe is built with console=False (see
    # tools/build_installer/shyyctl.spec) - no console subsystem at all, so
    # sys.stdout/sys.stderr are actually None (not just unread), not merely
    # a stderr handler writing into the void. That's worse than the
    # "SensorHelper.exe silently doing nothing" problem sensor-helper/
    # Program.cs's own file logging was built to avoid: uvicorn.run()
    # below installs its own default logging config pointed at
    # sys.stderr, and any other library that does a bare print() would
    # raise AttributeError on a None stream and crash startup outright.
    # Redirecting both to the same log file - rather than just adding a
    # FileHandler alongside untouched None streams - fixes both problems
    # at once with one file, and keeps uvicorn's own startup banner in
    # the same place as this app's own log lines.
    #
    # Rotated once here at startup, not via logging.handlers.
    # RotatingFileHandler - this file is also sys.stdout/sys.stderr
    # themselves (not just a logging handler's target), and a
    # size-triggered mid-run rotation would close and reopen that
    # shared file object out from under the redirect above, leaving
    # both pointed at a stale/closed handle. A background service can
    # run for weeks; capping growth to "this run plus one prior run"
    # at process start is enough for a human to export and attach to a
    # bug report, without touching anything mid-run.
    if LOG_FILE.is_file() and LOG_FILE.stat().st_size > 10 * 1024 * 1024:
        LOG_FILE_OLD.unlink(missing_ok=True)
        LOG_FILE.rename(LOG_FILE_OLD)
    _log_file = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    sys.stdout = _log_file
    sys.stderr = _log_file
    _log_handlers.append(logging.StreamHandler(_log_file))
else:
    _log_handlers.append(logging.StreamHandler())
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s",
                     handlers=_log_handlers)
log = logging.getLogger("shyyctl")

hub = Hub(match=CONFIG["hub"]["match"])
openrgb = OpenRgbBridge(host=CONFIG["openrgb"]["host"], port=CONFIG["openrgb"]["port"])
scan = Scan(hub, openrgb)
layout = Layout(LAYOUT_FILE)
ambient_sampler = AmbientSampler()

_panorama = None
_panorama_info: dict | None = None
_panorama_lock = asyncio.Lock()
_keepalive_task: asyncio.Task | None = None
_curve_task: asyncio.Task | None = None
_curve_status: dict = {"curves": []}

# Host-driven, position-aware hub effects (Advanced RGB view) - see
# _run_position_effect below. A plain threading.Event rather than an asyncio
# primitive because the effect body is a tight blocking-socket loop run
# via asyncio.to_thread on a worker thread, not a coroutine; setting the
# event from a request handler's own thread is safe without a lock, it's
# just a one-way "stop now" flag.
_effect_stop = threading.Event()
_effect_task: asyncio.Task | None = None
_effect_status: dict = {"running": False, "kind": None, "error": None}


def _find_panorama():
    try:
        return find_print_panel() or find_usb_panel()
    except Exception as exc:
        # Broad except deliberately: this whole module talks to Windows-only
        # APIs (ctypes.WinDLL, pyusb) - any failure here (missing driver,
        # device unplugged, an unexpected exception from either transport)
        # should degrade to "no panel" for the rest of the server, never
        # take down startup or a request handler.
        log.warning("panorama connect attempt failed: %s", exc)
        return None


def _load_panorama_state() -> dict:
    if PANORAMA_STATE_FILE.is_file():
        try:
            return json.loads(PANORAMA_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_panorama_state(media: str | None, brightness: int | None,
                          poweron_media: str | None = None, standby_media: str | None = None):
    """Persists what shyyctl itself last applied - not a read of the
    device's real current state, which the reverse-engineered protocol has
    no command for (checked: neither the handshake's device-info reply nor
    the media listing carries a "currently active" field - confirmed
    against real captures, every field of both was dumped and inspected).
    KANALI itself almost certainly does the same thing (remembers its own
    last action rather than querying it back). This at least survives a
    shyyctl restart or panel reconnect, instead of resetting to a guessed
    default every time a fresh connection object is created.

    poweron_media/standby_media added alongside media/brightness for the
    same reason set_screen_config() now tracks them on the panel object
    (see its docstring) - without restoring these too on reconnect, a
    fresh connection's panel.last_poweron_media starts back at the
    factory DEFAULT_POWERON_MEDIA, and the very next set_media()/
    set_brightness() call would silently re-send that default and wipe
    out a custom boot image across a mere shyyctl restart, not just a
    real power cycle."""
    PANORAMA_STATE_FILE.write_text(
        json.dumps({"media": media, "brightness": brightness,
                   "poweron_media": poweron_media, "standby_media": standby_media}),
        encoding="utf-8")


def _save_effect_state(body: "PositionEffectRequest"):
    """A position effect (unlike a native hub firmware mode, or a static
    colour applied with persistent=True) is host-driven - main.py's own
    Python loop is what's actually pushing colours to the hardware, so it
    goes dark the instant this process stops, and nothing brings it back
    on the next launch without this. Only the effect's *config* is saved,
    not started_at - a resumed effect gets a fresh start time (see
    lifespan below), same as clicking Apply again would."""
    EFFECT_STATE_FILE.write_text(body.model_dump_json(), encoding="utf-8")


def _clear_effect_state():
    """Called whenever a position effect stops being the thing actually
    driving the hardware - an explicit Stop, a chase/LED-order test
    overriding it, or a native hub mode taking over - so a later restart
    doesn't surprise a human by resuming something they deliberately
    turned off or replaced."""
    EFFECT_STATE_FILE.unlink(missing_ok=True)


def _load_rgb_state() -> dict:
    if not RGB_STATE_FILE.is_file():
        return {}
    try:
        return json.loads(RGB_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("could not load saved RGB state: %s", exc)
        return {}


def _save_rgb_state_entry(group: str, key: str, color: str, persistent: bool):
    """Records the last colour explicitly applied to one target (the
    motherboard, one RAM stick, or one hub channel) so it can be pushed
    back to the hardware on the next launch. Same reasoning as
    _save_panorama_state - no OpenRGB call here has a way to read back a
    device's current colour, so there's no way to check "is the hardware
    already showing this" on startup, only to unconditionally re-apply
    (see _reapply_rgb_state)."""
    state = _load_rgb_state()
    state.setdefault(group, {})[key] = {"color": color, "persistent": persistent}
    RGB_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def _load_gallery() -> list[dict]:
    if GALLERY_MANIFEST.is_file():
        try:
            return json.loads(GALLERY_MANIFEST.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _save_gallery(items: list[dict]):
    GALLERY_DIR.mkdir(parents=True, exist_ok=True)
    GALLERY_MANIFEST.write_text(json.dumps(items), encoding="utf-8")


_VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".avi"}  # mirrors web/index.html's VIDEO_SUFFIXES


def _current_panorama_media_path() -> tuple[str, bool] | None:
    """The local file backing whatever's currently applied to the
    Panorama panel (see _save_panorama_state) - what the "ambient" RGB
    effect decodes locally (app/ambient.py), never the live screen.
    Returns (path, is_video), or None if nothing's been applied yet or
    the gallery entry it names has since been deleted."""
    media_name = _load_panorama_state().get("media")
    if not media_name:
        return None
    for item in _load_gallery():
        if item.get("name") != media_name:
            continue
        original = next((GALLERY_DIR / item["id"]).glob("original.*"), None)
        if original is None:
            return None
        return str(original), original.suffix.lower() in _VIDEO_SUFFIXES
    return None


def _load_effect_state() -> "PositionEffectRequest | None":
    if not EFFECT_STATE_FILE.is_file():
        return None
    try:
        return PositionEffectRequest.model_validate_json(EFFECT_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("could not load saved effect state: %s", exc)
        return None


async def get_panorama():
    """Lazily (re)connects to the panel on every call rather than trusting
    a connection made at server startup - it may not be plugged in yet, or
    KANALI may currently hold usbprint.sys open (see app/panorama.py).

    Also handshakes exactly once per fresh connection, right here, and
    caches the result in _panorama_info - build_registration_burst()'s own
    docstring says its three frames are "sent once after connecting" to
    make the device volunteer its info unprompted. /api/panorama/status
    used to call handshake() itself on every single request, resending
    that same burst against an already-registered, long-lived connection
    every time - if the device only replies with real info the first time
    after connecting (which is exactly what that docstring says), every
    repeat call after the first was always going to come back empty. Not a
    concurrency bug (already fixed separately with _panorama_lock around
    every panel call) - this was a protocol misuse bug underneath it."""
    global _panorama, _panorama_info
    async with _panorama_lock:
        if _panorama is not None and _panorama.connected:
            if _panorama_info is None:
                # The one handshake attempt for this connection didn't
                # stick yet (e.g. it raced the device's own startup, or a
                # reply timed out) - retry here instead of returning the
                # same cached None for the rest of this connection's
                # lifetime. Confirmed against a real run this was a
                # genuine regression: once _panorama_info was None, every
                # later call hit this fast path and never tried again.
                try:
                    _panorama_info = await asyncio.to_thread(_panorama.handshake)
                except Exception as exc:
                    log.warning("panorama handshake retry failed: %s", exc)
            return _panorama
        _panorama = await asyncio.to_thread(_find_panorama)
        _panorama_info = None
        if _panorama is not None:
            try:
                _panorama_info = await asyncio.to_thread(_panorama.handshake)
            except Exception as exc:
                log.warning("panorama handshake failed: %s", exc)
            # A fresh connection object's last_media/last_brightness/
            # last_poweron_media/last_standby_media all start at hardcoded
            # defaults (see app/panorama.py) - reload whatever shyyctl
            # itself last actually applied instead of forgetting it on
            # every reconnect. Restoring the last two matters as much as
            # the first two now: set_screen_config() defaults poweron/
            # standby media to these tracked values on every call, so an
            # un-restored fresh connection would silently re-send the
            # factory boot/standby default on the very next set_media()/
            # set_brightness() call after a shyyctl restart - the same
            # bug that made a custom boot image not survive an unrelated
            # Apply, just triggered by a restart instead.
            saved = _load_panorama_state()
            if saved.get("media"):
                _panorama.last_media = saved["media"]
            if saved.get("brightness") is not None:
                _panorama.last_brightness = saved["brightness"]
            if saved.get("poweron_media"):
                _panorama.last_poweron_media = saved["poweron_media"]
            if saved.get("standby_media"):
                _panorama.last_standby_media = saved["standby_media"]
            # Restoring the fields above only fixes what THIS process
            # remembers - it does not, by itself, put anything back on
            # the panel's own screen. Confirmed by _keepalive_loop's own
            # docstring: the panel reverts to its default content a few
            # seconds after the last message it sees, with or without a
            # USB-level disconnect - so a transient USB dropout (reported
            # directly this session: a working connection went dark on
            # its own after ~15 minutes, panel absent from usb_probe()
            # entirely, not just "something else has the handle") leaves
            # the panel showing nothing/its own default until a fresh
            # screen-config message reaches it again. Every reconnect
            # (this fresh-_panorama branch, whether it's the first
            # connection at server launch or recovery from a mid-session
            # drop) now resends the saved config unconditionally - same
            # "no readback, so always reapply" reasoning as the RGB
            # colour/position-effect resume elsewhere in this file, not
            # a guess that it's needed.
            if saved.get("media"):
                try:
                    await asyncio.to_thread(
                        _panorama.set_screen_config, _panorama.last_media, _panorama.last_brightness)
                except Exception as exc:
                    log.warning("could not reapply saved screen config after (re)connect: %s", exc)
        return _panorama


async def _keepalive_loop():
    """Confirmed against real hardware this session: the panel reverts to
    its default content a few seconds after the last message it sees -
    every real KANALI capture showed it pinging roughly every 2s
    continuously the whole time it ran. A failed ping here just means the
    panel is unplugged or something else (KANALI) currently has it; it's
    not treated as fatal, the loop keeps trying.

    Holds _panorama_lock for the ping itself, not just around
    get_panorama() - this is a single USB connection with one message in
    flight at a time (see app/panorama.py), and this loop runs
    continuously in the background regardless of what else is talking to
    the panel. Without the lock, a status/media/upload request's own read
    can consume this loop's ping reply (or vice versa), leaving the
    request's real reply looking like it never arrived - confirmed as the
    actual cause of /api/panorama/status intermittently coming back with
    empty device info despite a healthy connection.

    Also actively retries get_panorama() itself whenever _panorama is
    None, not just pinging an already-live one - direct user report: the
    display sometimes stayed black for "several minutes" after dropping.
    The previous version only dropped the cached connection here and left
    reconnecting entirely to whatever HTTP request happened to call
    get_panorama() next; if the browser tab wasn't sitting on the Display
    page (or nothing else touched it), that could be arbitrarily far in
    the future - matching the report exactly. get_panorama() is cheap to
    call when nothing's plugged in (find_usbprint_path's SetupAPI
    enumeration, not a blocking retry loop - see app/panorama.py), so
    retrying it here every 2s is safe even while genuinely disconnected."""
    global _panorama
    while True:
        try:
            panel = _panorama
            if panel is None:
                await get_panorama()
            elif panel.connected:
                async with _panorama_lock:
                    await asyncio.to_thread(panel.ping)
        except PanoramaError as exc:
            # A real protocol-level failure (not just "nothing plugged in
            # yet") - most likely something else (KANALI) grabbed the
            # device out from under this already-open handle. connected
            # (app/panorama.py) is only a Python-side flag, never a live
            # check, so it would otherwise stay True forever and every
            # future request would keep reusing and re-breaking the same
            # handle. Dropping it here means this same loop's own next
            # tick (~2s away) retries get_panorama() itself, on top of
            # whatever a real request would trigger anyway.
            log.debug("panorama keepalive ping failed, dropping the cached connection: %s", exc)
            _panorama = None
        except Exception as exc:
            log.debug("panorama keepalive ping failed: %s", exc)
        await asyncio.sleep(2)


def _launch_vendor_openrgb() -> bool:
    """Starts the bundled portable OpenRGB (see tools/setup_vendor.py) with
    its SDK server enabled, only called when nothing is already listening
    on the configured port - never launches a second instance alongside
    one the user is already running themselves. --server enables the SDK
    server; --startminimized starts it minimized to tray (implies --gui) -
    confirmed against a real run this session that the flag is spelled
    --startminimized, not --minimize-to-tray (that guess made OpenRGB
    reject it as an invalid option and exit immediately without ever
    starting the SDK server - exactly why the very first real test of
    this saw a connection timeout with no server ever listening).

    Elevated via a UAC prompt (openrgb.launch_elevated in config.yaml,
    default on) - confirmed by direct user report that OpenRGB's RAM
    SPD/SMBus access silently doesn't work non-elevated on Windows. This
    elevates only the OpenRGB subprocess, not shyyctl itself - nothing
    else this project does (liquidctl/WinUSB access) needs admin rights,
    so the backend keeps running as a normal user."""
    vendor_dir = ROOT / "vendor" / "OpenRGB"
    # Confirmed against a real run: the downloaded zip's own internal
    # folder structure is preserved (e.g. "OpenRGB Windows 64-bit/
    # OpenRGB.exe"), not a flat vendor/OpenRGB/OpenRGB.exe - search for it
    # rather than assuming a fixed depth.
    exe = next(vendor_dir.rglob("OpenRGB.exe"), None)
    if exe is None:
        return False

    elevated = CONFIG.get("openrgb", {}).get("launch_elevated", True)
    args = "--server --startminimized"
    log.info("Starting bundled OpenRGB (%s) with its SDK server%s...",
            exe, " (elevated - expect a UAC prompt)" if elevated else "")
    if elevated:
        try:
            import ctypes
            # ShellExecuteW's "runas" verb elevates just this one process
            # via the normal UAC prompt. Return value > 32 means success;
            # <= 32 covers both real failures and the user declining the
            # prompt - either way, fall through to report it as such.
            result = ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
                None, "runas", str(exe), args, str(exe.parent), 1)
            if int(result) <= 32:
                log.warning("Elevated OpenRGB launch failed or was declined (code %s)", result)
                return False
        except Exception as exc:
            log.warning("Could not launch bundled OpenRGB elevated: %s", exc)
            return False
        return True

    try:
        subprocess.Popen([str(exe), "--server", "--startminimized"], cwd=str(exe.parent))
    except OSError as exc:
        log.warning("Could not launch bundled OpenRGB: %s", exc)
        return False
    return True


async def _connect_openrgb_with_recovery() -> bool:
    """Called once from lifespan instead of a bare openrgb.connect() +
    unconditional _launch_vendor_openrgb() on failure - direct user report:
    RGB "not detected properly" after restarting shyyctl, matching a real
    failure mode this didn't previously guard against. OpenRGB.exe is a
    long-lived process shyyctl doesn't own the lifetime of; if the
    previous shyyctl run (or a Windows shutdown, or a crash) left it
    running but its SDK server wedged - not exited, just not answering -
    the old code's openrgb.connect() would ride out however long
    OpenRGBClient's internal per-device read timeouts take (see
    openrgb_bridge.py's module docstring: observed >30s in the wild) and,
    on failure, launch a *second* OpenRGB.exe that then can't bind the
    already-held SDK port - compounding the exact symptom being fixed.

    Deliberately NOT a tight timeout here, even though a process that's
    already running should usually answer fast if it's healthy: this
    same module's own docstring on openrgb_bridge.py documents OpenRGB's
    SMBus/RAM detection taking "well past 30s" on real hardware right
    after launch, and connect() itself walks every device sequentially
    (see this function's own docstring above). A short window here would
    misdiagnose an instance that's merely still warming up - e.g. Windows
    autostarted it at login at roughly the same moment as shyyctl - as
    "stuck", and kill it: actively counterproductive (worse than doing
    nothing) against a healthy process, and user-hostile against one the
    human is running themselves, not something shyyctl launched at all.
    20s costs nothing extra here that _launch_vendor_openrgb's own
    warm-up wait below wouldn't already cost anyway in the failure case,
    and the outer 50s budget in lifespan is the actual backstop against
    a genuinely wedged instance eating the whole startup.

    Killing it also unblocks openrgb_bridge's own connect() if this wait
    timed out while a background thread was still mid-read inside it
    (asyncio can cancel *this* coroutine's wait, never the underlying
    blocking call that thread is running) - the socket read that thread
    is stuck in ends almost immediately once the process backing it is
    gone, rather than only after its own multi-second read timeout."""
    running = await asyncio.to_thread(_process_running, "OpenRGB.exe")
    if running:
        try:
            if await asyncio.wait_for(asyncio.to_thread(openrgb.connect), timeout=20):
                return True
        except asyncio.TimeoutError:
            pass
        log.warning("OpenRGB.exe is running but not answering - killing the stale process and relaunching")
        await asyncio.to_thread(_kill_process, "OpenRGB.exe")
    elif await asyncio.to_thread(openrgb.connect):
        return True
    else:
        log.warning("OpenRGB not connected: %s", openrgb.last_error)

    if _launch_vendor_openrgb():
        # A single fixed short wait wasn't enough against real hardware:
        # OpenRGB's own device scan (SMBus/PawnIO/RAM enumeration) was
        # still visibly running in its console output well past 3s, long
        # before its SDK server was ready to accept connections. Poll for
        # up to ~20s instead of one fixed-delay retry.
        for _ in range(10):
            await asyncio.sleep(2)
            if await asyncio.to_thread(openrgb.connect):
                return True
        log.warning("Still not connected after starting bundled OpenRGB (waited ~20s): %s",
                   openrgb.last_error)
    return False


def _lhm_reachable(url: str, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except Exception:
        return False


def _launch_vendor_sensor_helper() -> bool:
    """Starts the bundled SensorHelper.exe (see tools/setup_vendor.py,
    which builds it from sensor-helper/Program.cs via `dotnet publish` -
    this project's own tiny background HTTP server wrapping
    LibreHardwareMonitorLib directly, not the full LibreHardwareMonitor
    GUI app), only called when its configured URL isn't already
    reachable - never launches a second instance alongside one already
    running.

    Elevated via a UAC prompt (sensor_helper.launch_elevated in
    config.yaml, default on) - full sensor access (CPU package
    temperature via MSR, motherboard SuperIO chip access) needs
    administrator rights on Windows, same reasoning as OpenRGB's RAM
    SPD/SMBus access above.

    Unlike the full LibreHardwareMonitor GUI app this replaced, there is
    no separate "Remote Web Server" toggle to click through by hand -
    SensorHelper.exe's own Main() starts its HTTP listener immediately
    on launch (see Program.cs), and it has no window at all (built as
    WinExe with no UI), so this is genuinely a one-shot "launch it and
    it's just working" background helper. The port is passed as its
    one command-line argument so it always matches config.yaml's
    sensors.librehardwaremonitor_url regardless of what port that's
    set to."""
    exe = ROOT / "vendor" / "SensorHelper" / "SensorHelper.exe"
    if not exe.exists():
        return False

    port = 8085
    lhm_url = CONFIG.get("sensors", {}).get("librehardwaremonitor_url", "")
    try:
        port = int(lhm_url.rsplit(":", 1)[1].split("/", 1)[0])
    except (IndexError, ValueError):
        pass

    elevated = CONFIG.get("sensor_helper", {}).get("launch_elevated", True)
    log.info("Starting bundled SensorHelper (%s) on port %d%s...",
            exe, port, " (elevated - expect a UAC prompt)" if elevated else "")
    if elevated:
        try:
            import ctypes
            result = ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
                None, "runas", str(exe), str(port), str(exe.parent), 0)
            if int(result) <= 32:
                log.warning("Elevated SensorHelper launch failed or was declined (code %s)", result)
                return False
        except Exception as exc:
            log.warning("Could not launch bundled SensorHelper elevated: %s", exc)
            return False
        return True

    try:
        subprocess.Popen([str(exe), str(port)], cwd=str(exe.parent))
    except OSError as exc:
        log.warning("Could not launch bundled SensorHelper: %s", exc)
        return False
    return True


# -- background-services mode control ---------------------------------------
#
# Direct user request: with only RGB effects wanted, there's no reason to
# also keep SensorHelper running (it exists purely for CPU/RAM/mobo
# temperature, which effects never read); with only fans/curves and the
# display wanted, there's no reason to keep OpenRGB running (RGB effects are
# the only thing that talks to it - hub fan control is direct liquidctl,
# and the Panorama display is direct USB, neither goes through OpenRGB).
# "Stop everything" additionally needs to actually free the two vendored
# .exe's, not just stop shyyctl from using them - matches the same
# uninstall-blocking problem the NSIS uninstaller's taskkill fix addresses,
# just triggered by hand instead of by the installer.
#
# Detection/killing is by process name (tasklist/taskkill), not by tracked
# PIDs - the elevated launch path (ShellExecuteW "runas", see
# _launch_vendor_openrgb/_launch_vendor_sensor_helper above) doesn't hand
# back a PID at all, so this is the only approach that works uniformly
# regardless of how - or whether shyyctl itself - started the process.
# Only ever touches shyyctl's own bundled vendor/ copies by construction
# (start_* below only launches those); a same-named process the user
# started independently would also be caught by stop_*, which is the
# expected, documented behaviour of an "OpenRGB off" button, not a bug.

def _process_running(name: str) -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}"],
            capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW,
        )
    except Exception:
        return False
    return name.lower() in result.stdout.lower()


def _kill_process(name: str) -> None:
    try:
        subprocess.run(["taskkill", "/F", "/IM", name], capture_output=True,
                        timeout=5, creationflags=NO_WINDOW)
    except Exception as exc:
        log.warning("could not stop %s: %s", name, exc)


def _stop_openrgb():
    _kill_process("OpenRGB.exe")
    openrgb.disconnect()


def _stop_sensor_helper():
    _kill_process("SensorHelper.exe")


def _start_openrgb_if_needed():
    if not _process_running("OpenRGB.exe"):
        _launch_vendor_openrgb()


def _start_sensor_helper_if_needed():
    lhm_url = CONFIG.get("sensors", {}).get("librehardwaremonitor_url")
    if lhm_url and not _lhm_reachable(lhm_url):
        _launch_vendor_sensor_helper()


_SERVICE_MODES = {
    # (want OpenRGB running, want SensorHelper running)
    "all": (True, True),
    "effects": (True, False),
    "fans_display": (False, True),
    "stop": (False, False),
}


def _apply_service_mode(mode: str):
    want_openrgb, want_sensor_helper = _SERVICE_MODES[mode]
    if want_openrgb:
        _start_openrgb_if_needed()
    else:
        _stop_openrgb()
    if want_sensor_helper:
        _start_sensor_helper_if_needed()
    else:
        _stop_sensor_helper()


# -- autostart (HKCU Run key) ------------------------------------------------
#
# Only meaningful for the installed/frozen build - the installer is what
# sets this key up in the first place (see tools/build_installer/
# installer.nsi), and "start shyyctl.exe --background at login" makes no
# sense pointed at a dev checkout's `python main.py`. Exposed as a toggle
# here (Settings) rather than only at install time, direct user request:
# install-time is a one-shot decision, this is one a human may want to
# flip later without reinstalling.

_AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_AUTOSTART_VALUE = "shyyctl"


def _autostart_available() -> bool:
    return getattr(sys, "frozen", False) and sys.platform == "win32"


def _autostart_enabled() -> bool:
    if not _autostart_available():
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY) as key:
            winreg.QueryValueEx(key, _AUTOSTART_VALUE)
            return True
    except FileNotFoundError:
        return False


def _set_autostart(enabled: bool):
    if not _autostart_available():
        raise HTTPException(400, "autostart is only available in the installed build")
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            command = f'"{sys.executable}" --background'
            winreg.SetValueEx(key, _AUTOSTART_VALUE, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, _AUTOSTART_VALUE)
            except FileNotFoundError:
                pass


async def _reapply_rgb_state():
    """Pushes every colour saved by _save_rgb_state_entry back to the
    hardware. Called unconditionally on every launch, same reasoning as
    the saved position effect and Panorama's last-applied media/
    brightness: OpenRGB gives no way to read a device's actual current
    colour back, so there's no "only if not already set" check possible -
    only "always re-apply, best effort, one target's failure doesn't
    block the rest"."""
    state = _load_rgb_state()
    if not state:
        return

    mobo = state.get("mobo", {}).get("0")
    if mobo:
        try:
            devices = await asyncio.to_thread(openrgb.motherboard)
            if devices:
                await asyncio.to_thread(
                    openrgb.set_color, devices, _hex_to_rgb(mobo["color"]), mobo.get("persistent", True))
        except Exception as exc:
            log.warning("could not reapply saved motherboard colour: %s", exc)

    ram_state = state.get("ram", {})
    if ram_state:
        try:
            devices = await asyncio.to_thread(openrgb.ram)
            for idx_str, entry in ram_state.items():
                idx = int(idx_str)
                if 0 <= idx < len(devices):
                    await asyncio.to_thread(
                        openrgb.set_color, [devices[idx]], _hex_to_rgb(entry["color"]),
                        entry.get("persistent", True))
        except Exception as exc:
            log.warning("could not reapply saved RAM colours: %s", exc)

    hub_state = state.get("hub", {})
    for channel, entry in hub_state.items():
        try:
            rgb = _hex_to_rgb(entry["color"])
            persistent = entry.get("persistent", True)

            def _do(channel=channel, rgb=rgb, persistent=persistent):
                with _open_hub_raw() as raw:
                    ctrl = raw.find_controller("NZXT")
                    if ctrl is None:
                        return
                    zone = raw.zone_for_channel(ctrl, channel)
                    # A channel's zone size doesn't survive an OpenRGB
                    # restart until a scan re-sizes it (see the resize
                    # endpoint's docstring) - skip silently rather than
                    # writing colour data to a zone reporting 0 LEDs.
                    if zone is None or zone.leds_count == 0:
                        return
                    raw.select_mode(ctrl, "static" if persistent else "direct")
                    raw.set_zone_color(ctrl.index, zone, rgb)

            await asyncio.to_thread(_do)
        except Exception as exc:
            log.warning("could not reapply saved hub channel %s colour: %s", channel, exc)


async def _reapply_hub_rgb_state_delayed():
    """A freshly (re)started OpenRGB reports every hub channel's zone as
    0 LEDs until the startup quick-scan resizes it (scan.py's
    _resize_hub_zones, kicked off fire-and-forget in lifespan below) -
    reapplying hub channel colours immediately after connecting would
    silently skip all of them. Waits long enough for a "quick" scan to
    realistically have finished before trying, rather than blocking
    server startup on it."""
    await asyncio.sleep(10)
    state = _load_rgb_state()
    if state.get("hub"):
        await _reapply_rgb_state()


async def _resume_effect_after_scan(saved_effect: "PositionEffectRequest"):
    """Waits for the startup quick-scan to actually finish before
    resuming a saved position effect, instead of starting it right
    alongside the scan. _run_position_effect silently skips every fan
    channel whose hub zone isn't sized yet (0 LEDs, see its own loop) -
    confirmed as a real, visible symptom: on restart, RAM/motherboard
    animated immediately but every fan sat out the first several seconds
    of a resumed effect while the scan was still resizing their zones.

    Polls scan.running rather than blocking the whole lifespan/server
    startup on it - the web UI should stay usable immediately even if
    the scan takes a while, this is just about not racing the effect
    against it. Capped at ~30s in case the scan never finishes (nothing
    to scan, or something's genuinely stuck) - the effect still starts
    at the cap either way rather than never running at all."""
    for _ in range(150):
        if not scan.running:
            break
        await asyncio.sleep(0.2)
    try:
        await _do_start_position_effect(saved_effect, persist=False)
    except Exception as exc:
        log.warning("Could not resume saved effect: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keepalive_task, _curve_task
    # Direct user report: the display would sometimes stay disconnected
    # for minutes after a restart, and it "felt like" RGB hanging was to
    # blame - it was: get_panorama() used to run strictly after the
    # hub/OpenRGB block below, so a slow OpenRGB (see
    # _connect_openrgb_with_recovery's docstring) delayed even *attempting*
    # the display connection by however long that took, despite the two
    # having nothing to do with each other. Starting it as its own task
    # here lets it connect while hub/OpenRGB are still working, instead of
    # queued behind them.
    panorama_task = asyncio.create_task(get_panorama())

    if not await asyncio.to_thread(hub.connect):
        log.warning("hub not connected: %s", hub.last_error)

    try:
        # Overall budget on top of _connect_openrgb_with_recovery's own
        # internal waits (20s zombie check + ~20s relaunch poll = ~40s
        # already, see its docstring on why 20s and not something
        # tighter) - a final backstop so a worse-than-expected case (e.g.
        # the kill not releasing openrgb_bridge's lock as quickly as
        # usual) still can't hold up the rest of startup indefinitely.
        if not await asyncio.wait_for(_connect_openrgb_with_recovery(), timeout=50):
            log.warning("OpenRGB still not connected after recovery attempts: %s", openrgb.last_error)
    except asyncio.TimeoutError:
        log.warning("OpenRGB connection attempt exceeded the 50s startup budget - "
                   "continuing without it for this launch")

    # Same auto-launch idea as OpenRGB above, for SensorHelper.exe (CPU/
    # motherboard/RAM temperature - see app/sensors.py and
    # sensor-helper/Program.cs). Only attempts this if a URL is
    # configured at all and it isn't already reachable (a previous
    # launch already left it up) - never launches a second instance.
    # Fire-and-forget: unlike OpenRGB, nothing else in startup needs this
    # to be up synchronously, sensors are polled independently and
    # asynchronously later.
    lhm_url = CONFIG.get("sensors", {}).get("librehardwaremonitor_url")
    if lhm_url and not await asyncio.to_thread(_lhm_reachable, lhm_url):
        _launch_vendor_sensor_helper()

    try:
        await panorama_task
    except Exception as exc:
        log.warning("panorama connect task failed: %s", exc)
    _keepalive_task = asyncio.create_task(_keepalive_loop())
    _curve_task = asyncio.create_task(_curve_loop())

    # Auto-discover on every launch rather than waiting for a human to open
    # Scan and click a button - "quick" spins each fan just long enough to
    # confirm it's there and read its RPM (no curve calibration, unlike
    # "full") and, unless skipped, reads motherboard/RAM through OpenRGB
    # too (see scan.py - that check now runs before any fan spins up).
    # Only worth starting if there's something to scan; on a box with
    # neither the hub nor OpenRGB reachable this would just spawn a thread
    # that immediately fails with "no hub found".
    if hub.connected or openrgb.connected:
        scan.start("quick")

    # Push back whatever static RGB colours shyyctl itself last applied -
    # see _reapply_rgb_state's docstring, same "no readback, so always
    # re-apply" reasoning as the saved effect below. Mobo/RAM colours
    # don't depend on the scan above; hub channel colours do (a channel's
    # zone reports 0 LEDs until the scan's resize step runs), so that
    # part is retried again after a delay by the background task.
    if openrgb.connected:
        try:
            await _reapply_rgb_state()
        except Exception as exc:
            log.warning("could not reapply saved RGB colours: %s", exc)
        asyncio.create_task(_reapply_hub_rgb_state_delayed())

    # Resume whatever position effect was running when this process last
    # stopped - see _save_effect_state's docstring: it's host-driven, so
    # it goes dark on process exit with nothing to bring it back unless
    # this does. persist=False - don't touch the saved file just for
    # replaying it, and don't wipe it if this particular attempt fails
    # (a device that isn't connected *yet* this early shouldn't cost a
    # human their saved effect for every future launch). Runs as a
    # background task (see _resume_effect_after_scan) so it can wait for
    # the quick-scan above to actually finish sizing the hub's zones
    # first, without delaying server startup itself.
    saved_effect = _load_effect_state()
    if saved_effect is not None:
        asyncio.create_task(_resume_effect_after_scan(saved_effect))

    # --background (only ever passed by the installer's silent autostart
    # shortcut - see tools/build_installer/installer.nsi's Registry Run
    # entry) means "keep curves/effects/display alive, don't pop a browser
    # tab at every login" - the Start Menu shortcut launches with no flag,
    # so a human deliberately opening shyyctl still gets config.yaml's
    # normal open_browser behaviour (default on).
    if "--background" not in sys.argv and CONFIG.get("server", {}).get("open_browser"):
        url = f"http://{CONFIG['server']['host']}:{CONFIG['server']['port']}"
        webbrowser.open(url)

    yield

    _keepalive_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _keepalive_task
    _curve_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _curve_task
    await _stop_effect()
    hub.disconnect()
    if _panorama is not None:
        _panorama.disconnect()


async def _lite_service_main():
    """Entry point for `--lite` (see the __main__ block below) - RGB
    effects, fan curves, and the Panorama display, with no web server
    ever bound, no browser tab, and no gallery involvement at all. Built
    for the direct user request of running shyyctl on a second,
    leaned-down Windows install (a dual-boot "gaming" OS) purely as a
    background helper: the CPU/RAM lifespan() normally spends on
    FastAPI/uvicorn's own request-handling stack and the startup
    quick-scan is exactly what a machine like that has no use for, on top
    of what OpenRGB/SensorHelper themselves already cost. The Panorama
    keepalive ping is kept, not dropped alongside those - it's one small
    USB message every ~2s (see _keepalive_loop's own docstring for why it
    can't be skipped: the panel silently reverts to its own default
    content a few seconds after the last message it sees), nowhere near
    what OpenRGB/SensorHelper/uvicorn cost, and dropping it would mean a
    display that goes dark within seconds of --lite starting.

    Reuses every real hardware/state function lifespan() itself uses
    (same hub/openrgb connect calls, same _reapply_rgb_state/
    _resume_effect_after_scan resume logic, same get_panorama/
    _keepalive_loop, same _curve_loop) rather than reimplementing any of
    it - this is a trimmed-down startup order, not a second
    implementation of RGB effects, fan curves, or the display. Config for
    it is whatever's already sitting next to the exe (config.yaml,
    curve.json, layout.json, rgb_state.json, effect_state.json,
    panorama_state.json) - see the Settings page's "Export config" button
    (/api/system/export-config), which bundles exactly those files for
    copying into another install.

    OpenRGB is only launched if there's actually an effect or a saved
    static colour to show - no point paying for a Qt GUI app to sit idle
    with nothing to display. SensorHelper is only launched if a saved
    curve both has a channel assigned AND reads a cpu/cpu_gpu
    temperature - a GPU-only curve needs nothing from it (nvidia-smi
    alone covers GPU temp), and an unassigned curve isn't driving
    anything regardless of its source. The Panorama panel, unlike those
    two, isn't a launched process at all (get_panorama() just opens a USB
    handle directly), so there's no equivalent "only if needed" gate for
    it - connecting costs nothing extra to skip checking for."""
    global _curve_task, _keepalive_task
    log.info("Starting in --lite mode: RGB effects + fan curves + Panorama display, no web UI.")

    if not await asyncio.to_thread(hub.connect):
        log.warning("hub not connected: %s", hub.last_error)

    saved_effect = _load_effect_state()
    rgb_state = _load_rgb_state()
    if saved_effect is not None or rgb_state:
        if not await asyncio.to_thread(openrgb.connect):
            log.warning("OpenRGB not connected: %s", openrgb.last_error)
            if _launch_vendor_openrgb():
                for _ in range(10):
                    await asyncio.sleep(2)
                    if await asyncio.to_thread(openrgb.connect):
                        break
                else:
                    log.warning("Still not connected after starting bundled OpenRGB "
                               "(waited ~20s): %s", openrgb.last_error)

    try:
        curve_state = json.loads(CURVE_FILE.read_text(encoding="utf-8")) if CURVE_FILE.is_file() else {}
    except Exception:
        curve_state = {}
    curves = curve_state.get("curves") or []
    needs_lhm = any(c.get("source") in ("cpu", "cpu_gpu") and c.get("channels") for c in curves)
    if needs_lhm:
        lhm_url = CONFIG.get("sensors", {}).get("librehardwaremonitor_url")
        if lhm_url and not await asyncio.to_thread(_lhm_reachable, lhm_url):
            _launch_vendor_sensor_helper()

    if hub.connected and rgb_state.get("hub"):
        scan.start("quick")
        asyncio.create_task(_reapply_hub_rgb_state_delayed())
    if openrgb.connected and rgb_state:
        try:
            await _reapply_rgb_state()
        except Exception as exc:
            log.warning("could not reapply saved RGB colours: %s", exc)
    if saved_effect is not None:
        asyncio.create_task(_resume_effect_after_scan(saved_effect))

    # get_panorama() connects, handshakes, and reapplies the last saved
    # media/brightness in one call - same as lifespan's own startup, and
    # same reasoning as everything above: no readback exists, so the only
    # option is unconditionally reapplying whatever was last saved.
    await get_panorama()
    _keepalive_task = asyncio.create_task(_keepalive_loop())

    _curve_task = asyncio.create_task(_curve_loop())
    try:
        await _curve_task
    except asyncio.CancelledError:
        pass
    finally:
        _keepalive_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _keepalive_task
        await _stop_effect()
        hub.disconnect()
        if _panorama is not None:
            _panorama.disconnect()


app = FastAPI(title="shyyctl", lifespan=lifespan)


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    if len(value) != 6:
        raise HTTPException(400, f"invalid color {value!r} - expected #rrggbb")
    try:
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        raise HTTPException(400, f"invalid color {value!r} - expected #rrggbb")


# -- status -------------------------------------------------------------

@app.get("/api/status")
async def status():
    panel = await get_panorama()
    return {
        "hub": {
            "connected": hub.connected, "error": hub.last_error,
            "description": hub.description, "channels": hub.channels,
        },
        "openrgb": {"connected": openrgb.connected, "error": openrgb.last_error},
        "panorama": {"connected": panel is not None and panel.connected},
    }


@app.get("/api/sensors")
async def sensors():
    """CPU/RAM load, GPU load/mem/temp, CPU temperature - see
    app/sensors.py for exactly where each one comes from and why each
    degrades to None independently rather than failing the whole call."""
    return await asyncio.to_thread(sensors_mod.snapshot, CONFIG)


@app.get("/api/system/resources")
async def system_resources():
    """CPU/RAM cost of shyyctl itself plus OpenRGB.exe/SensorHelper.exe,
    for the Settings resource viewer - see app/sensors.py's
    process_resources for how each reading degrades to None (psutil
    missing, or that particular process not running)."""
    return await asyncio.to_thread(sensors_mod.process_resources)


@app.get("/api/conflicts")
async def conflicts():
    """Other RGB/fan-controlling software running right now (see
    app/conflicts.py) - polled far less often than /api/status since
    listing every running process isn't free and this doesn't change
    on any interesting timescale shorter than that."""
    return {"running": await asyncio.to_thread(running_conflicts)}


@app.post("/api/reconnect")
async def reconnect_all():
    """Manual retry for all three hardware surfaces - e.g. after plugging
    something in, restarting OpenRGB, or the panel dropping off USB -
    without restarting shyyctl itself. /api/status only ever reports
    cached state; this is what actually re-probes."""
    hub_ok = await asyncio.to_thread(hub.connect)
    openrgb_ok = await asyncio.to_thread(openrgb.connect)
    global _panorama
    async with _panorama_lock:
        _panorama = None
    panel = await get_panorama()
    return {
        "hub": {"connected": hub_ok, "error": hub.last_error},
        "openrgb": {"connected": openrgb_ok, "error": openrgb.last_error},
        "panorama": {"connected": panel is not None and panel.connected},
    }


# -- system control (background services / autostart) ---------------------

class ServiceModeRequest(BaseModel):
    mode: str


@app.get("/api/system/services")
async def system_services():
    """Live running state of the two vendored background helpers - not
    shyyctl's own connection state (openrgb.connected can be false with
    the process still up, e.g. right after a reconnect failure), the
    process itself, since that's what the mode buttons below actually
    start/stop."""
    return {
        "openrgb": await asyncio.to_thread(_process_running, "OpenRGB.exe"),
        "sensor_helper": await asyncio.to_thread(_process_running, "SensorHelper.exe"),
    }


@app.post("/api/system/services")
async def set_system_services(body: ServiceModeRequest):
    """Direct user request: rather than a single all-or-nothing toggle,
    four modes matching what each background helper is actually for -
    "effects" keeps OpenRGB (RGB effects need it) but drops SensorHelper
    (effects never read temperature); "fans_display" is the reverse (fan
    curves need SensorHelper's temperature, but nothing about fan control
    or the Panorama display goes through OpenRGB); "all" is today's
    default behaviour; "stop" frees both .exe's entirely, e.g. right
    before uninstalling by hand rather than trusting the installer's own
    taskkill step."""
    if body.mode not in _SERVICE_MODES:
        raise HTTPException(400, f"unknown mode {body.mode!r} - expected one of {sorted(_SERVICE_MODES)}")
    await asyncio.to_thread(_apply_service_mode, body.mode)
    return {
        "openrgb": await asyncio.to_thread(_process_running, "OpenRGB.exe"),
        "sensor_helper": await asyncio.to_thread(_process_running, "SensorHelper.exe"),
    }


class AutostartRequest(BaseModel):
    enabled: bool


@app.get("/api/system/autostart")
async def get_autostart():
    return {"available": _autostart_available(), "enabled": _autostart_enabled()}


@app.post("/api/system/autostart")
async def set_autostart(body: AutostartRequest):
    await asyncio.to_thread(_set_autostart, body.enabled)
    return {"available": _autostart_available(), "enabled": _autostart_enabled()}


@app.post("/api/system/quit")
async def quit_shyyctl():
    """The "full close" the Stop-services modes above don't cover on
    their own - shyyctl.exe itself keeps running under all four modes,
    including "stop" (which only frees OpenRGB/SensorHelper). This is
    the one control that also ends the web server process, for a human
    who wants nothing at all left running - most usefully right before
    an uninstall the installer's own taskkill step (see installer.nsi)
    would otherwise need to do on their behalf.

    os._exit rather than a graceful uvicorn shutdown: this request's own
    response has to make it back to the browser first, and any orderly
    shutdown path here would race the HTTP response against tearing down
    the very server sending it. A short delay via a background task lets
    the response flush before the process actually disappears."""
    await asyncio.to_thread(_apply_service_mode, "stop")

    async def _delayed_exit():
        await asyncio.sleep(0.5)
        os._exit(0)

    asyncio.create_task(_delayed_exit())
    return {"ok": True}


# Every ROOT-relative state file a `--lite` instance (see _lite_service_main
# below) reads on its own - RGB effects, fan curves, and the Panorama
# display's last-applied media, deliberately nothing gallery-related since
# --lite never touches the gallery itself (only whatever media was already
# applied before export). config.yaml first since it's the one file that's
# mandatory (hub/openrgb host/port); the rest are included only if they
# actually exist - a fresh Setup with nothing saved yet shouldn't make this
# endpoint 404, it should just export a smaller zip.
_LITE_EXPORT_FILES: list[tuple[str, "Path"]] = [
    ("config.yaml", _CONFIG_FILE),
    ("curve.json", CURVE_FILE),
    ("layout.json", LAYOUT_FILE),
    ("rgb_state.json", RGB_STATE_FILE),
    ("effect_state.json", EFFECT_STATE_FILE),
    ("effect_presets.json", EFFECT_PRESETS_FILE),
    ("panorama_state.json", PANORAMA_STATE_FILE),
]


@app.get("/api/system/export-config")
async def export_config():
    """Zips up everything a `--lite` instance needs (see
    _LITE_EXPORT_FILES/_lite_service_main) so it can be copied straight
    into another shyyctl install's folder - e.g. a leaned-down dual-boot
    gaming Windows where the point is running RGB effects, fan curves,
    and the Panorama display for the least possible CPU/RAM, not opening
    the web UI there at all. panorama_state.json only references the
    media by the name already uploaded onto the panel's own flash storage
    (see _apply_gallery_item - shyyctl never re-uploads it from this
    zip), which is exactly why this works for the dual-boot case: it's
    the same physical panel either way, just driven from a different OS
    install, so a name it already has on-device is still good. Built in
    memory (io.BytesIO) rather than a temp file - these are all small
    JSON/YAML files, nothing here is big enough to justify touching disk
    twice."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, path in _LITE_EXPORT_FILES:
            if path.is_file():
                zf.write(path, arcname)
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=shyyctl-config.zip"})


@app.get("/api/system/export-log")
async def export_log():
    """Downloads shyyctl.log (and shyyctl.log.old too, if a rotation has
    happened - see LOG_FILE_OLD above) - direct user request: something
    to attach to a bug report instead of describing a failure from
    memory after the fact. Only ever written when running as the
    installed exe (see the frozen-only sys.stdout/stderr redirect this
    module starts with) - a dev `python main.py` run has nothing here,
    reported as 404 rather than an empty/missing file download."""
    if not LOG_FILE.is_file():
        raise HTTPException(404, "no log file yet - only written when running as the installed app")
    if not LOG_FILE_OLD.is_file():
        return FileResponse(LOG_FILE, media_type="text/plain", filename="shyyctl.log")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(LOG_FILE, "shyyctl.log")
        zf.write(LOG_FILE_OLD, "shyyctl.log.old")
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=shyyctl-logs.zip"})


def _version_tuple(v: str) -> tuple:
    """"1.0.10" -> (1, 0, 10) so a >= comparison works numerically instead
    of comparing "10" < "9" as strings. Any non-numeric component (a
    "-dev"/"-rc1" suffix, say) contributes 0 rather than raising, since
    this only ever needs to answer "is the release newer", not fully
    parse arbitrary version strings."""
    parts = []
    for p in v.split("."):
        m = re.match(r"\d+", p)
        parts.append(int(m.group()) if m else 0)
    return tuple(parts)


@app.get("/api/system/update-check")
async def check_for_update():
    """Compares APP_VERSION against the shyyctl repo's latest GitHub
    release - only ever runs when a human opens Settings or clicks
    "Check for updates" (see Settings > Updates in the web UI), never on
    a background timer. Unauthenticated GitHub API call, same as any
    browser hitting this repo's public releases page would make."""
    url = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"
    req = urllib.request.Request(url, headers={
        "User-Agent": "shyyctl-update-check",
        "Accept": "application/vnd.github+json",
    })
    try:
        data = await asyncio.to_thread(lambda: json.loads(urllib.request.urlopen(req, timeout=6).read()))
    except Exception as exc:
        raise HTTPException(503, f"could not reach GitHub: {exc}")
    latest_tag = data.get("tag_name", "") or ""
    latest = latest_tag[1:] if latest_tag.startswith("v") else latest_tag
    asset = next((a for a in data.get("assets", []) if a.get("name", "").endswith(".exe")), None)
    return {
        "current": APP_VERSION,
        "latest": latest,
        "available": bool(latest) and _version_tuple(latest) > _version_tuple(APP_VERSION),
        "notes_url": data.get("html_url"),
        "download_url": asset["browser_download_url"] if asset else None,
        "asset_name": asset["name"] if asset else None,
    }


class UpdateApplyRequest(BaseModel):
    download_url: str


@app.post("/api/system/update-apply")
async def apply_update(body: UpdateApplyRequest):
    """Downloads the installer .exe an update-check response pointed at
    and launches it silently (NSIS's own /S flag). installer.nsi's
    Section already kills this exact running process and copies fresh
    files over it (see that script's own docstring on why), then
    relaunches shyyctl.exe itself when done - this endpoint's job ends
    the moment that installer process starts, same as a human
    double-clicking the downloaded installer themselves would, just
    without the extra manual download/uninstall-first dance that was
    silently wiping curve.json/layout.json/rgb_state.json/the media
    gallery whenever someone uninstalled the old version first instead
    of just running the new installer over it.

    Only meaningful for the installed exe - a `python main.py` dev run
    has no installer.exe concept for this to drive."""
    if not getattr(sys, "frozen", False):
        raise HTTPException(400, "updating only works for the installed app, not a dev run")
    tmp_path = Path(tempfile.gettempdir()) / "shyyctl-update-setup.exe"
    try:
        await asyncio.to_thread(urllib.request.urlretrieve, body.download_url, tmp_path)
    except Exception as exc:
        raise HTTPException(502, f"download failed: {exc}")
    try:
        subprocess.Popen([str(tmp_path), "/S"], creationflags=NO_WINDOW)
    except Exception as exc:
        raise HTTPException(500, f"could not launch installer: {exc}")
    return {"ok": True}


# -- fans -----------------------------------------------------------------

@app.get("/api/fans")
async def list_fans():
    if not hub.connected:
        raise HTTPException(503, "hub not connected")
    readings = await asyncio.to_thread(hub.status)
    # A channel a full/quick scan actually confirmed nothing responds to
    # (scan.absent - see scan.py's _scan()) is left out here entirely,
    # not just hidden from Setup - there's no point offering a duty
    # slider for a header confirmed empty. A channel never scanned at
    # all still shows, since nothing's confirmed either way for it yet.
    channels = [c for c in hub.channels.get("fan", []) if c not in scan.absent]
    # liquidctl's own status() reports every physical channel regardless -
    # it has no concept of scan.absent. Filtering readings to the same
    # allowed set here means a confirmed-empty fan's "0 rpm" reading can't
    # leak into a page that only looks at readings directly (the Status
    # overview's stat tiles), the way the Fans page's own channel list
    # already excludes it.
    allowed_numbers = {c[3:] for c in channels if c.startswith("fan")}
    def _reading_channel_ok(r):
        m = re.match(r"fan (\d+)", str(r.get("key", "")).lower())
        return not m or m.group(1) in allowed_numbers
    readings = [r for r in readings if _reading_channel_ok(r)]
    return {"channels": channels, "readings": readings}


class DutyRequest(BaseModel):
    duty: int


@app.post("/api/fans/{channel}/duty")
async def set_fan_duty(channel: str, body: DutyRequest):
    if not hub.connected:
        raise HTTPException(503, "hub not connected")
    try:
        applied = await asyncio.to_thread(hub.set_fan, channel, body.duty)
    except HubError as exc:
        raise HTTPException(500, str(exc))
    return {"channel": channel, "duty": applied}


# -- fan curve --------------------------------------------------------
# A fan is either "Fixed" (plain manual duty, /api/fans/{channel}/duty)
# or a member of exactly one named curve profile - CPU, GPU, and CPU/GPU
# combined ship as defaults (matching the three sources a human would
# actually want to react to), and a human can add more of their own.
# Membership lives on the curve, not the fan (curve.channels), so "which
# group is this fan in" is just "which curve's channels list contains
# it" - a fan absent from every curve's list is Fixed by definition,
# nothing extra to track.

CURVE_SOURCES = ("cpu", "gpu", "cpu_gpu")


class Curve(BaseModel):
    id: str
    name: str
    source: str  # "cpu" | "gpu" | "cpu_gpu"
    points: list[list[int]]  # [[temp_c, duty_pct], ...]
    channels: list[str] = []


class CurvesRequest(BaseModel):
    curves: list[Curve]


def _default_curves() -> list[dict]:
    points = CONFIG["curve"]["default"]
    return [
        {"id": "cpu", "name": "CPU", "source": "cpu", "points": points, "channels": []},
        {"id": "gpu", "name": "GPU", "source": "gpu", "points": points, "channels": []},
        {"id": "cpu_gpu", "name": "CPU / GPU", "source": "cpu_gpu", "points": points, "channels": []},
    ]


@app.get("/api/curve")
async def get_curve():
    if CURVE_FILE.is_file():
        try:
            data = json.loads(CURVE_FILE.read_text(encoding="utf-8"))
            if data.get("curves"):
                return data
        except Exception:
            pass
    return {"curves": _default_curves()}


@app.post("/api/curve")
async def set_curve(body: CurvesRequest):
    for curve in body.curves:
        if curve.source not in CURVE_SOURCES:
            raise HTTPException(400, f"unknown curve source {curve.source!r} - must be one of {CURVE_SOURCES}")
    CURVE_FILE.write_text(
        json.dumps({"curves": [c.model_dump() for c in body.curves]}), encoding="utf-8")
    return {"ok": True}


@app.get("/api/curve/status")
async def curve_status():
    """What _curve_loop is actually doing right now, per curve - separate
    from the stored config above, same GET-config/GET-status split as
    scan and the RGB effects (/api/scan/status, /api/rgb/effects/status)."""
    return _curve_status


def _curve_duty_for_temp(points: list[list[int]], min_duty: int, temp_c: float) -> int:
    """Linear interpolation between the two curve points bracketing
    temp_c, clamped below by min_duty - a fan stalling near 0% is worse
    than running a little faster than the curve technically asked for."""
    pts = sorted(points)
    if not pts:
        return min_duty
    if temp_c <= pts[0][0]:
        return max(min_duty, pts[0][1])
    if temp_c >= pts[-1][0]:
        return max(min_duty, pts[-1][1])
    for (t0, d0), (t1, d1) in zip(pts, pts[1:]):
        if t0 <= temp_c <= t1:
            frac = (temp_c - t0) / (t1 - t0) if t1 != t0 else 0
            return max(min_duty, round(d0 + (d1 - d0) * frac))
    return max(min_duty, pts[-1][1])


def _combine_temp(cpu_t: float | None, gpu_t: float | None) -> float | None:
    """The "CPU / GPU" combined source: whichever component is hotter
    drives the fan, the same reasoning AIO pumps/case fans usually use a
    combined curve for - being generous to the louder answer is the
    whole point, not an average that could mask one side running hot."""
    vals = [t for t in (cpu_t, gpu_t) if t is not None]
    return max(vals) if vals else None


async def _curve_loop():
    """Polls every few seconds rather than reacting to a temp change
    event - there isn't one to react to (CPU/GPU temp are only ever read
    by polling, see app/sensors.py), and a fan curve doesn't need
    sub-second response time anyway. Reads curve.json fresh each cycle
    instead of caching it in memory, so a change from the Fans page takes
    effect on the very next tick without needing to signal this loop
    directly. Every curve is evaluated every cycle (not just ones with
    channels assigned) so /api/curve/status can always show what a curve
    *would* apply, useful feedback while still deciding what to assign
    it - only a curve with at least one assigned channel actually writes
    to the hub."""
    global _curve_status
    while True:
        try:
            state = json.loads(CURVE_FILE.read_text(encoding="utf-8")) if CURVE_FILE.is_file() else {}
        except Exception:
            state = {}
        curves = state.get("curves") or _default_curves()
        statuses = []
        # CPU/GPU temp reads have nothing to do with the hub - gating the
        # whole block on hub.connected (as this used to) meant every
        # single curve, including a GPU-only one, went blank the instant
        # the hub connection flickered, since liquidctl's own USB read is
        # a separate, independently flaky thing (real report: recurring
        # "missing messages" AssertionErrors from the hub's own HID
        # comms). Confirmed as the actual cause of curves looking like
        # they "lag/flicker weirdly" - it wasn't the curve logic itself,
        # it was losing every reading in sync with an unrelated hub
        # hiccup. Only the actual fan-setting step below genuinely needs
        # the hub, so only that part is still gated on it.
        sensors_cfg = CONFIG.get("sensors", {})
        cpu_t = await asyncio.to_thread(
            sensors_mod.lhm_temp,
            sensors_cfg.get("librehardwaremonitor_url", ""),
            sensors_cfg.get("cpu_sensor_match", "CPU Package"),
        )
        gpu_t = (await asyncio.to_thread(sensors_mod.gpu)).get("temp_c")
        temps = {"cpu": cpu_t, "gpu": gpu_t, "cpu_gpu": _combine_temp(cpu_t, gpu_t)}
        fan_channels = hub.channels.get("fan", []) if hub.connected else []
        for curve in curves:
            temp_c = temps.get(curve.get("source"))
            channels = [c for c in curve.get("channels", []) if c in fan_channels and c not in scan.absent]
            duty = None
            if temp_c is not None and channels:
                duty = _curve_duty_for_temp(
                    curve.get("points") or [], CONFIG["curve"].get("min_duty", 20), temp_c)
                if hub.connected:
                    for channel in channels:
                        try:
                            await asyncio.to_thread(hub.set_fan, channel, duty)
                        except Exception as exc:
                            log.warning("curve %s: could not set %s to %s%%: %s",
                                       curve.get("id"), channel, duty, exc)
            statuses.append({"id": curve.get("id"), "temp_c": temp_c, "duty": duty})
        _curve_status = {"curves": statuses}
        await asyncio.sleep(4)


# -- RGB: motherboard / RAM / hub -------------------------------------------

def _open_hub_raw(timeout: float = 5.0) -> OpenRgbRaw:
    return OpenRgbRaw(host=CONFIG["openrgb"]["host"], port=CONFIG["openrgb"]["port"], timeout=timeout)


def _hub_via_raw() -> dict | None:
    """The hub's real zone sizes, straight from the protocol-5 client -
    see app/openrgb_raw.py's docstring. openrgb_bridge.py's describe_all()
    can never show these correctly: OpenRGB's server deliberately reports
    this hub's effects-only zones as 0/1 LEDs to any protocol-4 client,
    by design, no matter what the real size is. Returns None on any
    failure so the caller can fall back to whatever the v4 list already
    has rather than losing the whole response over the hub specifically."""
    try:
        with _open_hub_raw() as raw:
            ctrl = raw.find_controller("NZXT")
            if ctrl is None:
                return None
            return {
                "name": ctrl.name,
                "type": "LEDSTRIP",
                # ctrl.num_leds (the controller-level LED array size, not
                # the zones) doesn't reliably track zone resizes for this
                # device - confirmed it can read back as 0 right after a
                # zone was correctly resized to a real, verified count.
                # The zones themselves are the source of truth here.
                "leds": sum(z.leds_count for z in ctrl.zones),
                "modes": [m.name for m in ctrl.modes],
                "zones": [{"name": z.name, "leds": z.leds_count} for z in ctrl.zones],
            }
    except Exception as exc:
        log.warning("Could not fetch hub zones via the raw v5 client: %s", exc)
        return None


@app.get("/api/rgb/state")
async def rgb_state():
    """The last colour shyyctl itself applied to each target (see
    _save_rgb_state_entry) - lets the UI pre-fill colour pickers and show
    a "currently applied" swatch on load, instead of every control
    resetting to a generic default the moment the page is refreshed."""
    return _load_rgb_state()


@app.get("/api/rgb/devices")
async def rgb_devices():
    if not openrgb.connected and not await asyncio.to_thread(openrgb.connect):
        raise HTTPException(503, f"OpenRGB not connected: {openrgb.last_error}")
    devices = await asyncio.to_thread(openrgb.describe_all)
    hub_entry = await asyncio.to_thread(_hub_via_raw)
    if hub_entry is not None:
        devices = [d for d in devices if "NZXT" not in d["name"]]
        devices.append(hub_entry)
    return devices


@app.get("/api/rgb/debug")
async def rgb_debug():
    """Raw SDK connection introspection (negotiated protocol version, a
    per-index device breakdown) - see OpenRgbBridge.debug_info's
    docstring. Not for normal use, only for diagnosing a device that
    won't show up via /api/rgb/devices."""
    if not openrgb.connected and not await asyncio.to_thread(openrgb.connect):
        raise HTTPException(503, f"OpenRGB not connected: {openrgb.last_error}")
    return await asyncio.to_thread(openrgb.debug_info)


class ColorRequest(BaseModel):
    color: str  # "#rrggbb"
    persistent: bool = True


@app.post("/api/rgb/mobo/color")
async def set_mobo_color(body: ColorRequest):
    if not openrgb.connected and not await asyncio.to_thread(openrgb.connect):
        raise HTTPException(503, f"OpenRGB not connected: {openrgb.last_error}")
    rgb = _hex_to_rgb(body.color)
    devices = await asyncio.to_thread(openrgb.motherboard)
    if not devices:
        raise HTTPException(404, "no motherboard RGB device found")
    await asyncio.to_thread(openrgb.set_color, devices, rgb, body.persistent)
    _save_rgb_state_entry("mobo", "0", body.color, body.persistent)
    return {"ok": True}


@app.get("/api/rgb/mobo/leds")
async def mobo_leds():
    """The X870-F's motherboard controller is one OpenRGB device with 4
    zones: 'Aura Mainboard' (3 real onboard LEDs) and 3x 'Aura Addressable
    N' (8 LEDs each - unconnected combo headers on this board, confirmed
    against real hardware). Only the real zone is ever worth exposing -
    this returns its LED count directly (not a zone list) so the UI can
    address the 3 real LEDs individually without ever seeing the
    unconnected placeholders."""
    if not openrgb.connected and not await asyncio.to_thread(openrgb.connect):
        raise HTTPException(503, f"OpenRGB not connected: {openrgb.last_error}")
    devices = await asyncio.to_thread(openrgb.motherboard)
    if not devices:
        raise HTTPException(404, "no motherboard RGB device found")
    device = devices[0]
    zone_index = openrgb.real_zone_index(device)
    if zone_index is None:
        return {"name": device.name, "zone_index": None, "leds": 0}
    return {"name": device.name, "zone_index": zone_index, "leds": len(device.zones[zone_index].leds)}


@app.post("/api/rgb/mobo/led/{index}/color")
async def set_mobo_led_color(index: int, body: ColorRequest):
    if not openrgb.connected and not await asyncio.to_thread(openrgb.connect):
        raise HTTPException(503, f"OpenRGB not connected: {openrgb.last_error}")
    rgb = _hex_to_rgb(body.color)
    devices = await asyncio.to_thread(openrgb.motherboard)
    if not devices:
        raise HTTPException(404, "no motherboard RGB device found")
    device = devices[0]
    zone_index = openrgb.real_zone_index(device)
    if zone_index is None:
        raise HTTPException(404, "motherboard has no populated RGB zone")
    try:
        await asyncio.to_thread(
            openrgb.set_led_color_by_index, device, zone_index, index, rgb, body.persistent)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


@app.post("/api/rgb/ram/color")
async def set_ram_color(body: ColorRequest):
    if not openrgb.connected and not await asyncio.to_thread(openrgb.connect):
        raise HTTPException(503, f"OpenRGB not connected: {openrgb.last_error}")
    rgb = _hex_to_rgb(body.color)
    devices = await asyncio.to_thread(openrgb.ram)
    if not devices:
        raise HTTPException(404, "no RAM RGB device found")
    await asyncio.to_thread(openrgb.set_color, devices, rgb, body.persistent)
    for i in range(len(devices)):
        _save_rgb_state_entry("ram", str(i), body.color, body.persistent)
    return {"ok": True}


@app.get("/api/rgb/ram/sticks")
async def ram_sticks():
    """Each stick is its own separate OpenRGB DRAM device (confirmed this
    project's earlier RAM-detection fix in scan.py) - this lists them so
    the UI can address stick 1 and stick 2 independently instead of
    /api/rgb/ram/color's whole-set write."""
    if not openrgb.connected and not await asyncio.to_thread(openrgb.connect):
        raise HTTPException(503, f"OpenRGB not connected: {openrgb.last_error}")
    devices = await asyncio.to_thread(openrgb.ram)
    return [{"index": i, "name": d.name, "leds": len(d.leds)} for i, d in enumerate(devices)]


@app.post("/api/rgb/ram/stick/{index}/color")
async def set_ram_stick_color(index: int, body: ColorRequest):
    if not openrgb.connected and not await asyncio.to_thread(openrgb.connect):
        raise HTTPException(503, f"OpenRGB not connected: {openrgb.last_error}")
    rgb = _hex_to_rgb(body.color)
    devices = await asyncio.to_thread(openrgb.ram)
    if index < 0 or index >= len(devices):
        raise HTTPException(404, f"no RAM stick at index {index}")
    await asyncio.to_thread(openrgb.set_color, [devices[index]], rgb, body.persistent)
    _save_rgb_state_entry("ram", str(index), body.color, body.persistent)
    return {"ok": True}


@app.post("/api/rgb/ram/stick/{index}/led/{led_index}/color")
async def set_ram_stick_led_color(index: int, led_index: int, body: ColorRequest):
    """Individual LED on one stick - each stick is its own single-zone
    device, so this goes straight through the device's LED list (see
    OpenRgbBridge.set_device_led_color) rather than needing a zone index
    first, unlike the motherboard's multi-zone controller."""
    if not openrgb.connected and not await asyncio.to_thread(openrgb.connect):
        raise HTTPException(503, f"OpenRGB not connected: {openrgb.last_error}")
    rgb = _hex_to_rgb(body.color)
    devices = await asyncio.to_thread(openrgb.ram)
    if index < 0 or index >= len(devices):
        raise HTTPException(404, f"no RAM stick at index {index}")
    try:
        await asyncio.to_thread(
            openrgb.set_device_led_color, devices[index], led_index, rgb, body.persistent)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


class ResizeRequest(BaseModel):
    leds: int


@app.post("/api/rgb/hub/{channel}/resize")
async def resize_hub_channel(channel: str, body: ResizeRequest):
    """Needed once per channel before hub color control does anything.
    This hub's zones are flagged effects-only/manually-configurable-size -
    OpenRGB's server deliberately reports them as 0/1 LEDs to any client
    below SDK protocol 5, as an intentional backward-compat shim (see
    app/openrgb_raw.py's docstring, sourced from OpenRGB's own
    RGBController.cpp). openrgb-python (app/openrgb_bridge.py, used for
    mobo/RAM below) hard-caps at protocol 4 and can never see or resize
    these zones - this hub goes through our own protocol-5 client
    instead. A scan applies this automatically for every channel it
    identifies with certainty (app/scan.py's _resize_hub_zones); call
    this directly for a channel scan left at 'unknown' confidence after
    picking its model by hand, or to redo one channel without a full
    rescan."""
    def _do():
        with _open_hub_raw() as raw:
            ctrl = raw.find_controller("NZXT")
            if ctrl is None:
                return "no_device"
            zone = raw.zone_for_channel(ctrl, channel)
            if zone is None:
                return f"no zone on {ctrl.name} matches channel '{channel}'"
            raw.resize_zone(ctrl.index, zone.index, body.leds)
            ctrl = raw.controller_data(ctrl.index)
            zone = raw.zone_for_channel(ctrl, channel)
            if zone is None or zone.leds_count != body.leds:
                got = zone.leds_count if zone else "no zone"
                return f"could not resize '{channel}' to {body.leds} LEDs (got {got})"
            return None

    try:
        error = await asyncio.to_thread(_do)
    except Exception as exc:
        raise HTTPException(503, f"OpenRGB not connected: {exc}")
    if error == "no_device":
        raise HTTPException(404, "no hub RGB device found")
    if error:
        raise HTTPException(400, error)
    return {"ok": True}


@app.post("/api/rgb/hub/{channel}/color")
async def set_hub_channel_color(channel: str, body: ColorRequest):
    """liquidctl LED control is confirmed broken for the 360mm units - hub
    RGB goes through OpenRGB's protocol-5 client (app/openrgb_raw.py),
    same reasoning as the resize endpoint above. The channel must already
    be sized (via a scan or the resize endpoint) - a 0-LED zone means
    nothing is known to be attached there, and is refused rather than
    silently falling back to a device-wide write."""
    rgb = _hex_to_rgb(body.color)

    def _do():
        with _open_hub_raw() as raw:
            ctrl = raw.find_controller("NZXT")
            if ctrl is None:
                return "no_device"
            zone = raw.zone_for_channel(ctrl, channel)
            if zone is None:
                return f"no zone on {ctrl.name} matches channel '{channel}'"
            if zone.leds_count == 0:
                return (f"channel '{channel}' has no LEDs sized yet - "
                        f"resize it first (POST /api/rgb/hub/{channel}/resize)")
            raw.select_mode(ctrl, "static" if body.persistent else "direct")
            raw.set_zone_color(ctrl.index, zone, rgb)
            return None

    try:
        error = await asyncio.to_thread(_do)
    except Exception as exc:
        raise HTTPException(503, f"OpenRGB not connected: {exc}")
    if error == "no_device":
        raise HTTPException(404, "no hub RGB device found")
    if error:
        raise HTTPException(400, error)
    _save_rgb_state_entry("hub", channel, body.color, body.persistent)
    return {"ok": True}


class LedColorsRequest(BaseModel):
    colors: list[str]  # ["#rrggbb", ...], one per LED, in strip order


@app.post("/api/rgb/hub/{channel}/leds")
async def set_hub_channel_leds(channel: str, body: LedColorsRequest):
    """Per-LED control for a single hub channel - the "advanced" tier
    above the whole-channel colour endpoint. The channel must already be
    sized (a scan does this, or POST .../resize) and the number of
    colours sent must match that size exactly - no padding or truncating
    guesses, same reasoning as resize_zone's docstring about not guessing
    LED counts."""
    rgb_list = [_hex_to_rgb(c) for c in body.colors]

    def _do():
        with _open_hub_raw() as raw:
            ctrl = raw.find_controller("NZXT")
            if ctrl is None:
                return "no_device"
            zone = raw.zone_for_channel(ctrl, channel)
            if zone is None:
                return f"no zone on {ctrl.name} matches channel '{channel}'"
            if zone.leds_count != len(rgb_list):
                return (f"channel '{channel}' has {zone.leds_count} LEDs, "
                        f"but {len(rgb_list)} colours were sent")
            raw.select_mode(ctrl, "static")
            raw.set_zone_colors(ctrl.index, zone.index, rgb_list)
            return None

    try:
        error = await asyncio.to_thread(_do)
    except Exception as exc:
        raise HTTPException(503, f"OpenRGB not connected: {exc}")
    if error == "no_device":
        raise HTTPException(404, "no hub RGB device found")
    if error:
        raise HTTPException(400, error)
    return {"ok": True}


DIRECTIONS = ("top_to_bottom", "bottom_to_top", "left_to_right", "right_to_left", "outward", "inward")
PALETTES = ("solid", "two_color", "multicolor", "rainbow")
# "wave" is the original behaviour (colour sampled by each LED's own
# position along `direction`, unchanged). "breathing" is a uniform colour
# across every driven LED with brightness pulsing (sine 0..1..0).
# "strobe" is rapid flashing between full brightness and off (ignore direction).
# "pulse" is brightness pulsing while cycling through palette colors.
# "sweep" is like wave but faster movement (2x speed). "sparkle" is random
# LED twinkling with a base color (ignore direction). "fire" is organic
# per-LED flicker in both brightness and colour (ignore direction).
# "confetti" is sparkle with a different palette colour per flash instead
# of one shared base colour (ignore direction). "comet" is a bright band
# with a fading tail bouncing between the two ends of `direction`'s axis,
# instead of wave/sweep's continuous wrap-around travel. "wipe" is a hard
# on/off edge sweeping across `direction`'s axis rather than wave's
# smooth gradient. "pulse_ring" is one narrow band travelling across
# `direction`'s axis and wrapping at the edge (a genuine ring when
# direction is outward/inward). "meteor" is several comets at once,
# staggered in phase. "ambient" ignores palette/colours entirely and
# colours each LED from the Panorama display's own currently-applied
# media (see app/ambient.py) - a local file shyyctl already manages, not
# a live screen/desktop capture.
SHAPES = ("wave", "breathing", "strobe", "pulse", "sweep", "sparkle", "fire", "confetti", "comet",
          "wipe", "pulse_ring", "meteor", "ambient")


def _resolve_effect_targets():
    """Every real per-LED write target the position effect can drive,
    resolved once (not per-frame - re-resolving RAM/mobo every frame
    would reintroduce exactly the per-request OpenRGB latency fixed
    elsewhere, see RECONNECT_RETRIES's comment). Fan channels are
    resolved separately inside the effect loop itself since they go
    through the hub's raw v5 connection, which needs to be held open for
    the whole effect (see openrgb_raw.py's docstring on this hub's
    server-side instability under repeated reconnects) rather than
    fetched here alongside RAM/mobo's protocol-4 bridge."""
    data = layout.with_leds()
    leds_by_unit: dict[str, list[dict]] = {}
    for led in data["leds"]:
        leds_by_unit.setdefault(led["unit"], []).append(led)
    fan_units = [u for u in data["units"] if u.get("kind") == "fan" and u.get("led_channel")]
    ram_units = [u for u in data["units"] if u.get("kind") == "ram"]
    mobo_units = [u for u in data["units"] if u.get("kind") == "mobo"]

    ram_targets: list[tuple[dict, object]] = []
    if ram_units:
        try:
            ram_devices = openrgb.ram()
            for u in ram_units:
                m = re.search(r"-(\d+)$", u["id"])
                idx = int(m.group(1)) if m else None
                if idx is not None and 0 <= idx < len(ram_devices):
                    ram_targets.append((u, ram_devices[idx]))
        except Exception as exc:
            log.warning("effect: could not resolve RAM sticks: %s", exc)

    mobo_target = None
    if mobo_units:
        try:
            mobo_devices = openrgb.motherboard()
            if mobo_devices:
                device = mobo_devices[0]
                zone_index = openrgb.real_zone_index(device)
                if zone_index is not None:
                    mobo_target = (mobo_units[0], device, zone_index)
        except Exception as exc:
            log.warning("effect: could not resolve motherboard: %s", exc)

    return leds_by_unit, fan_units, ram_targets, mobo_target


def _position_value(direction: str, led: dict, bounds: dict) -> float:
    """Normalised 0..1 position of one LED along whichever axis
    `direction` cares about - the single thing every effect (wave,
    rainbow, whatever) samples its colour from. outward/inward are
    radial distance from the bounding box's centre, not a straight axis,
    so a fan in the middle of the case and one at a corner get genuinely
    different positions, not just a flat top/bottom or left/right split."""
    x, y = led["x"], led["y"]
    if direction == "top_to_bottom":
        return (y - bounds["y_min"]) / bounds["y_span"]
    if direction == "bottom_to_top":
        return 1.0 - (y - bounds["y_min"]) / bounds["y_span"]
    if direction == "left_to_right":
        return (x - bounds["x_min"]) / bounds["x_span"]
    if direction == "right_to_left":
        return 1.0 - (x - bounds["x_min"]) / bounds["x_span"]
    # outward / inward
    dx = (x - bounds["cx"]) / bounds["r_max"]
    dy = (y - bounds["cy"]) / bounds["r_max"]
    dist = min(1.0, math.hypot(dx, dy))
    return dist if direction == "outward" else 1.0 - dist


def _sample_palette(palette: str, colors: list[tuple], t: float, cyclic: bool) -> tuple:
    """The colour at position/phase `t` for whichever palette was picked.

    cyclic matters for two_color/multicolor specifically: a *moving*
    effect needs the gradient to loop back on itself smoothly as phase
    advances (n stops, wrapping from the last back to the first), but a
    *static* one-way gradient (moving=False) must NOT wrap - confirmed as
    a real bug by a direct test: with cyclic wrapping always on, the LED
    at position exactly 1.0 folds back to t%1.0==0.0 and renders the
    *start* colour again, so the far end of a static left-to-right
    gradient snapped back to the near end's colour instead of ending on
    the last stop. Non-cyclic clamps t to 0..1 and spans it across n-1
    segments instead, so position 0 is exactly the first colour and
    position 1 is exactly the last, no wraparound. Rainbow is always
    cyclic regardless - a hue wheel wrapping at 360°/0° is the whole
    point of a rainbow gradient, static or moving. solid ignores t
    entirely, cyclic or not."""
    if palette == "solid":
        return colors[0]
    if palette == "rainbow":
        r, g, b = colorsys.hsv_to_rgb(t % 1.0, 1.0, 1.0)
        return (round(r * 255), round(g * 255), round(b * 255))
    n = len(colors)
    if n <= 1:
        return colors[0] if colors else (255, 255, 255)
    if cyclic:
        t = t % 1.0
        seg = t * n
        i = int(seg) % n
        frac = seg - int(seg)
        a, b = colors[i], colors[(i + 1) % n]
    else:
        t = max(0.0, min(1.0, t))
        seg = t * (n - 1)
        i = min(int(seg), n - 2)
        frac = seg - i
        a, b = colors[i], colors[i + 1]
    w = (1 - math.cos(frac * math.pi)) / 2
    return tuple(round(a[k] + (b[k] - a[k]) * w) for k in range(3))


def _run_position_effect(direction: str, palette: str, colors: list[tuple],
                         period_s: float, moving: bool, shape: str = "wave",
                         led_selection: dict[str, set[int]] | None = None):
    """Runs until _effect_stop is set, on its own worker thread (via
    asyncio.to_thread) - one raw hub connection held for the whole effect
    rather than reopened per frame (see openrgb_raw.py's docstring on
    this hub's server-side instability under repeated
    REQUEST_CONTROLLER_DATA hammering). Drives every fan channel, RAM
    stick and the motherboard's real zone that's actually been placed in
    Setup, each LED coloured by _sample_palette() at its _position_value()
    - a single generalised effect covering directional wipes, radial
    in/out, solid colour, multi-stop gradients and a moving rainbow,
    rather than one hardcoded top-to-bottom two-colour wave.

    led_selection (unit id -> set of LED indices), when given, restricts
    which LEDs actually show the effect - direction-based shapes also
    compute their position bounds only from the selected LEDs, so e.g. a
    wave across a five-LED selection in the middle of a 36-LED fan still
    spans that selection's own full range rather than a sliver of the
    whole fan's range. Every LED outside the selection still gets written
    every frame forced to (0,0,0) - see PositionEffectRequest's docstring
    for why there's no way to instead just leave it alone.

    shape="breathing" is a second effect added alongside removing the
    broken standalone hub-mode feature: every driven LED shows the same
    colour (direction/position is meaningless here, so it's ignored) -
    while moving, that colour cycles through the palette exactly like
    the wave shape's gradient does (phase drives _sample_palette the
    same way), and brightness pulses through one full sine envelope per
    period_s, in sync with that same phase - a classic "breathe" rather
    than a travelling wipe. Static (moving=False) breathing shows a
    fixed full-brightness colour rather than freezing mid-pulse, which
    would otherwise land on an arbitrary (possibly near-zero)
    brightness depending on where the phase happened to stop.

    A non-moving (static) effect still runs this same loop rather than
    writing once and exiting - phase just never advances - so Stop keeps
    working the same way for every effect kind, at the cost of a few
    harmless redundant identical writes a second instead of one."""
    global _effect_status
    leds_by_unit, fan_units, ram_targets, mobo_target = _resolve_effect_targets()

    driven_ids = {u["id"] for u in fan_units} | {u["id"] for u, _ in ram_targets}
    if mobo_target is not None:
        driven_ids.add(mobo_target[0]["id"])
    all_leds = [led for uid in driven_ids for led in leds_by_unit.get(uid, [])]
    if led_selection is not None:
        all_leds = [led for led in all_leds if led["index"] in led_selection.get(led["unit"], ())]
    if not all_leds:
        _effect_status = {"running": False, "kind": None,
                           "error": "no LEDs selected" if led_selection is not None
                           else "no units placed in Setup yet"}
        return
    xs = [l["x"] for l in all_leds]
    ys = [l["y"] for l in all_leds]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    cx, cy = (x_min + x_max) / 2, (y_min + y_max) / 2
    r_max = max((math.hypot(l["x"] - cx, l["y"] - cy) for l in all_leds), default=0) or 1.0
    bounds = {
        "x_min": x_min, "x_span": (x_max - x_min) or 1.0,
        "y_min": y_min, "y_span": (y_max - y_min) or 1.0,
        "cx": cx, "cy": cy, "r_max": r_max,
    }

    phase = [0.0]  # mutable cell frame_colors closes over

    cyclic = moving or palette == "rainbow"

    def frame_colors(u):
        unit_leds = sorted(leds_by_unit.get(u["id"], []), key=lambda l: l["index"])

        if shape == "breathing":
            if moving:
                color = _sample_palette(palette, colors, phase[0], True)
                brightness = (1 - math.cos(phase[0] * 2 * math.pi)) / 2
                final = tuple(round(c * brightness) for c in color)
            else:
                final = _sample_palette(palette, colors, 0.0, False)
            colors_out = [final] * len(unit_leds)

        elif shape == "strobe":
            # Rapid flashing: full brightness / off, alternates based on phase
            strobe_freq = 4.0  # flashes per period
            is_on = (phase[0] * strobe_freq) % 1.0 < 0.5
            base_color = _sample_palette(palette, colors, 0.0, False)
            final = base_color if is_on else (0, 0, 0)
            colors_out = [final] * len(unit_leds)

        elif shape == "pulse":
            # Brightness pulsing while cycling through palette
            brightness = (1 - math.cos(phase[0] * 2 * math.pi)) / 2
            color = _sample_palette(palette, colors, phase[0], True)
            final = tuple(round(c * brightness) for c in color)
            colors_out = [final] * len(unit_leds)

        elif shape == "sweep":
            # Like wave but 2x speed
            sweep_phase = (phase[0] * 2.0) % 1.0
            colors_out = [
                _sample_palette(palette, colors,
                                _position_value(direction, led, bounds) - sweep_phase, cyclic)
                for led in unit_leds
            ]

        elif shape == "sparkle":
            # Twinkling with a base colour - each LED's twinkle offset is a
            # deterministic function of its own index (golden-ratio
            # multiplication, a cheap standard trick for spreading integer
            # indices evenly across 0..1 without a lookup table), not
            # random.random(). A real random per-LED offset would only
            # ever exist on this side - the frontend's local Preview and
            # the synced canvas both replay this same shape client-side to
            # show exactly what Apply put on real hardware (see
            # effectFrameColor's docstring), and have no way to learn a
            # value this process rolled privately. Same offset formula on
            # both sides (see effectFrameColor's "sparkle" branch) keeps
            # that guarantee intact for this shape too.
            base_color = _sample_palette(palette, colors, 0.0, False)

            def get_twinkle_brightness(led_index):
                offset = (led_index % 256) * 0.618 % 1.0
                t = (phase[0] + offset) % 1.0
                triangle = t * 2 if t < 0.5 else 2 * (1 - t)  # 0->1->0 per cycle
                # Direct user feedback: the plain triangle above spends as
                # much time near full brightness as it does near zero, so
                # a whole strip of LEDs reads as "everyone dimly lit" at
                # any instant instead of "mostly dark, a few sharp
                # twinkles" - raising it to a power pushes the low/mid
                # values toward zero while leaving the peak (1**5 == 1)
                # untouched, without changing the offset spacing or cycle
                # timing at all.
                return triangle ** 5

            colors_out = [
                tuple(round(c * get_twinkle_brightness(led["index"])) for c in base_color)
                for led in unit_leds
            ]

        elif shape == "fire":
            # Organic flicker, not a clean geometric wave/pulse - each
            # LED's own brightness is the sum of three sine waves at
            # incommensurate frequencies (3.1/5.3/7.9 Hz-equivalent, no
            # common factor so their sum never repeats on a short, visibly
            # periodic beat) rather than one sine, which is what actually
            # makes this look irregular instead of just "pulse but
            # dimmer". Same golden-ratio per-LED offset as sparkle so
            # neighbouring LEDs don't flicker in lockstep. Colour also
            # drifts slowly across the palette per LED (not just a shared
            # base colour flickering in brightness like pulse) for the
            # "plasma" half of the look on a multi-colour/rainbow palette.
            def flicker_brightness(led_index):
                offset = (led_index % 256) * 0.618 % 1.0
                t = phase[0] + offset
                n = (math.sin(t * 2 * math.pi * 3.1)
                     + math.sin(t * 2 * math.pi * 5.3 + 1.7)
                     + math.sin(t * 2 * math.pi * 7.9 + 4.2)) / 3.0  # -1..1
                return max(0.0, min(1.0, 0.55 + 0.45 * n))

            colors_out = []
            for led in unit_leds:
                brightness = flicker_brightness(led["index"])
                color_t = (phase[0] * 0.3 + (led["index"] % 256) * 0.618 % 1.0) % 1.0
                color = _sample_palette(palette, colors, color_t, True)
                colors_out.append(tuple(round(c * brightness) for c in color))

        elif shape == "confetti":
            # Sparkle's exact brightness envelope (see its own branch
            # above), but each flash is a different palette colour instead
            # of the one shared base colour - the actual visual
            # difference from sparkle, not a renamed copy of it. The
            # colour offset uses 0.382 (1 - 0.618, the golden ratio's
            # other conjugate) rather than sparkle's 0.618, plus its own
            # slow phase drift, so a given LED's flash colour and flash
            # timing don't stay locked together cycle after cycle - two
            # per-LED sequences derived from the same index but not
            # correlated, still fully deterministic (see sparkle's
            # docstring on why: the frontend preview replays this exact
            # formula and has no way to learn a value rolled with
            # random.random() instead).
            def confetti_brightness(led_index):
                offset = (led_index % 256) * 0.618 % 1.0
                t = (phase[0] + offset) % 1.0
                triangle = t * 2 if t < 0.5 else 2 * (1 - t)
                return triangle ** 5

            colors_out = []
            for led in unit_leds:
                brightness = confetti_brightness(led["index"])
                color_t = ((led["index"] % 256) * 0.382 + phase[0] * 0.15) % 1.0
                color = _sample_palette(palette, colors, color_t, True)
                colors_out.append(tuple(round(c * brightness) for c in color))

        elif shape == "comet":
            # A bright band with a fading tail bouncing between the two
            # ends of direction's axis - the same 0->1->0 ping-pong
            # triangle breathing/pulse already use for their brightness
            # envelope, applied to *position* here instead so the bright
            # point reverses at each end rather than wrapping around
            # continuously like wave/sweep.
            comet_pos = phase[0] * 2 if phase[0] < 0.5 else 2 * (1 - phase[0])
            tail_width = 0.35  # fraction of the axis the fading trail spans
            color = _sample_palette(palette, colors, phase[0], True)
            colors_out = []
            for led in unit_leds:
                dist = abs(_position_value(direction, led, bounds) - comet_pos)
                brightness = max(0.0, 1.0 - dist / tail_width)
                colors_out.append(tuple(round(c * brightness) for c in color))

        elif shape == "wipe":
            # A hard on/off edge sweeping across direction's axis, not
            # wave's smoothly interpolated gradient - the lit half is a
            # single solid colour (cycling through the palette if it's
            # moving/rainbow), the rest is off, with a sharp boundary
            # between them rather than a blend.
            base_color = _sample_palette(palette, colors, phase[0], True)
            colors_out = [
                base_color if (_position_value(direction, led, bounds) - phase[0]) % 1.0 < 0.5 else (0, 0, 0)
                for led in unit_leds
            ]

        elif shape == "pulse_ring":
            # One narrow band travelling continuously across direction's
            # axis and wrapping back around at the edge (unlike Comet,
            # which bounces) - a genuine ring when direction is outward/
            # inward, a travelling band on any other axis, same as wave/
            # sweep generalise to all six directions.
            ring_width = 0.18
            pos_phase = phase[0] % 1.0
            color = _sample_palette(palette, colors, phase[0], True)
            colors_out = []
            for led in unit_leds:
                dist = abs(_position_value(direction, led, bounds) - pos_phase)
                dist = min(dist, 1.0 - dist)  # wrap distance, so the band re-enters smoothly at the far edge
                brightness = max(0.0, 1.0 - dist / ring_width)
                colors_out.append(tuple(round(c * brightness) for c in color))

        elif shape == "meteor":
            # Several comets at once instead of Comet's single one, each
            # offset in phase so they don't move in lockstep - same
            # ping-pong triangle, repeated at a few staggered starting
            # points for a "shower" instead of one travelling band.
            tail_width = 0.22
            meteor_count = 3
            color = _sample_palette(palette, colors, phase[0], True)
            colors_out = []
            for led in unit_leds:
                pos = _position_value(direction, led, bounds)
                brightness = 0.0
                for i in range(meteor_count):
                    meteor_phase = (phase[0] + i / meteor_count) % 1.0
                    meteor_pos = meteor_phase * 2 if meteor_phase < 0.5 else 2 * (1 - meteor_phase)
                    dist = abs(pos - meteor_pos)
                    brightness = max(brightness, 1.0 - dist / tail_width)
                colors_out.append(tuple(round(c * max(0.0, brightness)) for c in color))

        elif shape == "ambient":
            # Colours come from the Panorama's own currently-applied
            # media (see app/ambient.py), not the palette/colours the
            # human picked - each LED blends the video's four sampled
            # edge zones by its own normalised top/bottom and left/right
            # position, so the rig mirrors roughly what's on the panel's
            # edges the way a TV ambilight mirrors screen edges, just
            # from a locally decoded file instead of a live feed.
            zones = ambient_sampler.zones()
            top, bottom, left, right = zones["top"], zones["bottom"], zones["left"], zones["right"]
            colors_out = []
            for led in unit_leds:
                v = _position_value("top_to_bottom", led, bounds)
                h = _position_value("left_to_right", led, bounds)
                colors_out.append(tuple(
                    round((top[c] * (1 - v) + bottom[c] * v + left[c] * (1 - h) + right[c] * h) / 2)
                    for c in range(3)
                ))

        else:
            # Default wave behavior
            colors_out = [
                _sample_palette(palette, colors,
                                _position_value(direction, led, bounds) - phase[0], cyclic)
                for led in unit_leds
            ]

        if led_selection is not None:
            allowed = led_selection.get(u["id"], ())
            colors_out = [c if led["index"] in allowed else (0, 0, 0)
                          for led, c in zip(unit_leds, colors_out)]

        return unit_leds, colors_out

    def refresh_ambient_source():
        media = _current_panorama_media_path()
        if media is not None:
            ambient_sampler.set_source(*media)
        else:
            ambient_sampler.stop()

    if shape == "ambient":
        refresh_ambient_source()
    last_ambient_check = time.monotonic()

    for _, device in ram_targets:
        openrgb.select_mode(device, "direct")
    if mobo_target is not None:
        openrgb.select_mode(mobo_target[1], "direct")

    try:
        # A short timeout here (vs. _open_hub_raw's normal 5s default,
        # fine for a one-off admin request) - direct user report: effects
        # sometimes "feel like they hang" after an unrelated RGB hiccup,
        # e.g. OpenRGB restarting mid-effect. This connection is held for
        # the whole effect (see docstring above), so if it goes bad every
        # frame's write would otherwise ride out the full 5s before even
        # counting as a failure - at the intended ~80ms cadence that's
        # every other visible thing (RAM/mobo write after this one in the
        # same frame) stalling behind it too, on a loop with no upper
        # bound on how many frames this repeats for.
        with _open_hub_raw(timeout=2.0) as raw:
            ctrl = raw.find_controller("NZXT") if fan_units else None
            if fan_units and ctrl is None:
                log.warning("effect: no hub RGB device found, fan channels won't animate")
            elif ctrl is not None:
                raw.select_mode(ctrl, "direct")
            start = time.monotonic()
            hub_failures = 0
            bridge_failures = 0
            while not _effect_stop.is_set():
                if moving:
                    phase[0] = ((time.monotonic() - start) / period_s) % 1.0

                if shape == "ambient" and time.monotonic() - last_ambient_check > 2.0:
                    # Cheap enough to just do every couple seconds
                    # (one small JSON read + string compare) rather than
                    # needing the human to restart the effect after
                    # switching what's applied to the panel while
                    # ambient is already running.
                    refresh_ambient_source()
                    last_ambient_check = time.monotonic()

                if ctrl is not None:
                    for u in fan_units:
                        try:
                            zone = raw.zone_for_channel(ctrl, u["led_channel"])
                            if zone is None or zone.leds_count == 0:
                                continue
                            unit_leds, frame = frame_colors(u)
                            if len(unit_leds) != zone.leds_count:
                                continue
                            raw.set_zone_colors(ctrl.index, zone.index, frame)
                            hub_failures = 0
                        except Exception as exc:
                            # A single bad write here (e.g. a transient
                            # socket hiccup talking to OpenRGB) used to
                            # propagate up to the outer try/except below
                            # and kill the WHOLE effect - RAM and mobo
                            # already only skip the one write that failed
                            # and keep going (see below); fan channels
                            # didn't, so one glitch here froze every LED
                            # (fans, RAM, mobo alike) until a human
                            # noticed and manually restarted it. Matches
                            # a real report: "effect hanging" after an
                            # unrelated USB hiccup elsewhere on the same
                            # machine.
                            log.debug("effect: hub write failed for %s: %s", u["id"], exc)
                            hub_failures += 1
                    if hub_failures >= 3:
                        # A handful of failures in a row (not just one -
                        # see above) means this specific long-lived
                        # connection is genuinely dead, most likely
                        # OpenRGB itself was restarted mid-effect - every
                        # future frame would otherwise keep retrying the
                        # identical broken socket forever, since nothing
                        # else ever reopens it. One reconnect attempt;
                        # failing that, only back off to 2 (not reset to
                        # 0) so it takes just one more write failure to
                        # try again, not three - a full reset here meant a
                        # reconnect attempt that itself failed (the hub
                        # genuinely still gone, say) waited through two
                        # extra needless failure cycles before retrying,
                        # on top of the one this branch just handled.
                        try:
                            raw.close()
                            raw.connect()
                            ctrl = raw.find_controller("NZXT")
                            if ctrl is not None:
                                raw.select_mode(ctrl, "direct")
                            log.info("effect: reconnected to the hub after repeated write failures")
                            hub_failures = 0
                        except Exception as exc:
                            log.debug("effect: hub reconnect failed: %s", exc)
                            hub_failures = 2

                for u, device in ram_targets:
                    _, frame = frame_colors(u)
                    try:
                        openrgb.set_zone_colors_by_index(device, 0, frame)
                        bridge_failures = 0
                    except Exception as exc:
                        log.debug("effect: RAM write failed for %s: %s", u["id"], exc)
                        bridge_failures += 1

                if mobo_target is not None:
                    u, device, zone_index = mobo_target
                    _, frame = frame_colors(u)
                    try:
                        openrgb.set_zone_colors_by_index(device, zone_index, frame)
                        bridge_failures = 0
                    except Exception as exc:
                        log.debug("effect: motherboard write failed: %s", exc)
                        bridge_failures += 1

                if bridge_failures >= 3 and (ram_targets or mobo_target is not None):
                    # Same reasoning as the hub reconnect above, for
                    # OpenRGB's own SDK connection (RAM/mobo) - device
                    # references resolved once at effect start (see
                    # _resolve_effect_targets' docstring on why not
                    # per-frame) go stale the same way if OpenRGB itself
                    # restarts mid-effect, and set_zone_colors_by_index
                    # has no retry/reconnect of its own (openrgb_bridge.py
                    # - it's a hot per-frame write path by design).
                    # openrgb.reconnect() drops and recreates the whole
                    # SDK client; re-resolving targets after gets fresh
                    # device objects rather than continuing to write
                    # through the old (now-replaced) ones.
                    try:
                        openrgb.reconnect()
                        _, _, ram_targets, mobo_target = _resolve_effect_targets()
                        for _, device in ram_targets:
                            openrgb.select_mode(device, "direct")
                        if mobo_target is not None:
                            openrgb.select_mode(mobo_target[1], "direct")
                        log.info("effect: reconnected to OpenRGB after repeated RAM/mobo write failures")
                        bridge_failures = 0
                    except Exception as exc:
                        log.debug("effect: OpenRGB reconnect failed: %s", exc)
                        bridge_failures = 2

                _effect_stop.wait(0.08 if moving else 1.0)
    except Exception as exc:
        log.warning("position effect stopped: %s", exc)
        _effect_status = {"running": False, "kind": None, "error": str(exc)}
        ambient_sampler.stop()
        return
    ambient_sampler.stop()
    _effect_status = {"running": False, "kind": None, "error": None}


def _run_chase_effect(channel: str, window: int, step_s: float):
    """A calibration tool, not a look - lights a sliding window of `window`
    consecutive LEDs (in the strip's own wiring order, index 0..n-1) and
    advances it one step at a time: LED 0 on, LED 1 on, ... once the
    window is full the trailing LED turns off as the next one lights, so
    with window=4 that's "LED 0 on, 1 on, 2 on, 3 on, then at step 4 LED 0
    turns off while 4 turns on" - wrapping continuously around the strip.
    Setup's canvas runs the same sequence on the same channel's LEDs at
    the same step timing (see startLedOrderTest in the frontend) so a
    human can watch the on-screen dot and the real fan side by side and
    see directly whether shyyctl's assumed wiring order (app/geometry.py
    - flagged there as an inferred, never-walked placeholder for F360X)
    actually matches the hardware, rather than guessing from a static
    colour."""
    global _effect_status
    try:
        with _open_hub_raw() as raw:
            ctrl = raw.find_controller("NZXT")
            if ctrl is None:
                _effect_status = {"running": False, "kind": None,
                                   "error": "no hub RGB device found"}
                return
            zone = raw.zone_for_channel(ctrl, channel)
            if zone is None or zone.leds_count == 0:
                _effect_status = {"running": False, "kind": None,
                                   "error": f"channel '{channel}' has no sized LEDs"}
                return
            n = zone.leds_count
            raw.select_mode(ctrl, "direct")
            step = 0
            while not _effect_stop.is_set():
                colors = [
                    (255, 255, 255) if (step - i) % n < window else (0, 0, 0)
                    for i in range(n)
                ]
                raw.set_zone_colors(ctrl.index, zone.index, colors)
                step = (step + 1) % n
                _effect_stop.wait(step_s)
    except Exception as exc:
        log.warning("LED order test stopped: %s", exc)
        _effect_status = {"running": False, "kind": None, "error": str(exc)}
        return
    _effect_status = {"running": False, "kind": None, "error": None}


async def _stop_effect():
    global _effect_task
    _effect_stop.set()
    if _effect_task is not None:
        try:
            await asyncio.wait_for(_effect_task, timeout=3.0)
        except Exception:
            pass
        _effect_task = None


class ChaseEffectRequest(BaseModel):
    window: int = 4
    step_s: float = 0.7


@app.post("/api/rgb/hub/{channel}/chase/start")
async def start_chase_effect(channel: str, body: ChaseEffectRequest):
    """Starting this (or a position effect, or a native hub mode) always
    stops whatever's currently running first, rather than rejecting the
    request - only one thing should ever be driving the hardware at
    once, and making the caller stop the old one first was just an extra
    round trip for no benefit."""
    global _effect_task, _effect_status
    await _stop_effect()
    _clear_effect_state()
    _effect_stop.clear()
    _effect_status = {"running": True, "kind": "chase", "channel": channel, "error": None}
    _effect_task = asyncio.create_task(
        asyncio.to_thread(_run_chase_effect, channel, max(body.window, 1), max(body.step_s, 0.1)))
    return {"ok": True}


class PositionEffectRequest(BaseModel):
    direction: str = "top_to_bottom"
    palette: str = "two_color"
    colors: list[str] = ["#4c79ff", "#ff7a30"]
    period_s: float = 4.0
    moving: bool = True
    shape: str = "wave"
    # {unit_id: [led_index, ...]} - restricts the effect to only these
    # LEDs (built from the Advanced canvas's click-drag multi-select, see
    # web/index.html's selectedLeds), or None for "every placed unit" (the
    # long-standing default). Every LED outside the selection still gets
    # written every frame (writes are always whole-zone, see
    # _run_position_effect - there's no way to leave a subset of a zone
    # untouched), just forced to (0,0,0)/off instead of the effect's own
    # colour, rather than left showing whatever static colour it had
    # before Apply.
    led_selection: dict[str, list[int]] | None = None


async def _do_start_position_effect(body: PositionEffectRequest, persist: bool = True):
    """Shared by the route below and lifespan's on-launch resume - both
    need to validate, stop whatever's running, and kick off the same
    background loop. persist=False on the resume path: the state file
    we're resuming *from* shouldn't be immediately rewritten with the
    same content, and definitely shouldn't be cleared if something in
    here fails part way (the saved config should survive a resume
    attempt that didn't pan out, in case the failure is transient - a
    hub that isn't connected yet this early in startup, say)."""
    global _effect_task, _effect_status
    if body.direction not in DIRECTIONS:
        raise HTTPException(400, f"unknown direction {body.direction!r} - must be one of {DIRECTIONS}")
    if body.palette not in PALETTES:
        raise HTTPException(400, f"unknown palette {body.palette!r} - must be one of {PALETTES}")
    if body.shape not in SHAPES:
        raise HTTPException(400, f"unknown shape {body.shape!r} - must be one of {SHAPES}")
    rgb_colors = [_hex_to_rgb(c) for c in body.colors] or [(255, 255, 255)]
    period_s = max(body.period_s, 0.5)
    await _stop_effect()
    _effect_stop.clear()
    selection = ({uid: set(idxs) for uid, idxs in body.led_selection.items()}
                 if body.led_selection else None)
    # started_at (wall-clock, not the loop's own time.monotonic() reference)
    # is what lets the browser reconstruct the *same* phase the backend is
    # currently showing - see startSyncedPreview() in the frontend - so the
    # Advanced canvas can display the effect continuously, kept in step
    # with the real hardware, rather than only during an explicit local
    # Preview that drifts out of phase with whatever's actually running.
    _effect_status = {
        "running": True, "kind": "position", "direction": body.direction,
        "palette": body.palette, "colors": body.colors, "period_s": period_s,
        "moving": body.moving, "shape": body.shape, "led_selection": body.led_selection,
        "started_at": time.time(), "error": None,
    }
    _effect_task = asyncio.create_task(
        asyncio.to_thread(_run_position_effect, body.direction, body.palette,
                          rgb_colors, period_s, body.moving, body.shape, selection))
    if persist:
        _save_effect_state(body)


@app.post("/api/rgb/effects/position/start")
async def start_position_effect(body: PositionEffectRequest):
    """See start_chase_effect's docstring re: always stopping whatever
    was running first instead of rejecting the request."""
    await _do_start_position_effect(body)
    return {"ok": True}


@app.post("/api/rgb/effects/stop")
async def stop_effect_endpoint():
    await _stop_effect()
    _clear_effect_state()
    return {"ok": True}


@app.get("/api/rgb/effects/status")
async def effect_status():
    return _effect_status


class EffectPreset(BaseModel):
    id: str
    name: str
    direction: str = "top_to_bottom"
    palette: str = "two_color"
    colors: list[str] = ["#4c79ff", "#ff7a30"]
    period_s: float = 4.0
    moving: bool = True
    shape: str = "wave"


class EffectPresetsRequest(BaseModel):
    presets: list[EffectPreset]


@app.get("/api/rgb/effects/presets")
async def get_effect_presets():
    """Saved position-effect configs (direction/palette/colours/speed) a
    human can name and recall instead of re-entering the same settings
    every time - same whole-list GET/POST shape as /api/curve, so the
    frontend can add/rename/delete locally and just POST the resulting
    array back, same as renameCurve/deleteCurve already do for curves."""
    if EFFECT_PRESETS_FILE.is_file():
        try:
            data = json.loads(EFFECT_PRESETS_FILE.read_text(encoding="utf-8"))
            if isinstance(data.get("presets"), list):
                return data
        except Exception:
            pass
    return {"presets": []}


@app.post("/api/rgb/effects/presets")
async def set_effect_presets(body: EffectPresetsRequest):
    EFFECT_PRESETS_FILE.write_text(
        json.dumps({"presets": [p.model_dump() for p in body.presets]}), encoding="utf-8")
    return {"ok": True}


# -- hardware discovery scan -------------------------------------------

class ScanRequest(BaseModel):
    mode: str = "full"  # "full" | "quick" | "rgb"
    skip_rgb_devices: bool = False


@app.post("/api/scan/start")
async def scan_start(body: ScanRequest = ScanRequest()):
    if body.mode not in scan.MODES:
        raise HTTPException(400, f"unknown scan mode {body.mode!r} - must be one of {scan.MODES}")
    started = await asyncio.to_thread(scan.start, body.mode, body.skip_rgb_devices)
    if not started:
        raise HTTPException(409, "scan already running")
    return {"started": True}


@app.get("/api/scan/status")
async def scan_status():
    return scan.state()


@app.post("/api/scan/cancel")
async def scan_cancel():
    scan.cancel()
    return {"ok": True}


# -- layout: where every unit physically is -------------------------------

@app.get("/api/layout")
async def get_layout():
    return layout.with_leds()


@app.post("/api/layout")
async def save_layout(units: list[dict]):
    return layout.with_leds(layout.save(units))


@app.post("/api/layout/adopt")
async def adopt_layout():
    """Builds/refreshes the unit list from the latest scan (fans,
    motherboard, RAM) plus a standing Panorama unit - safe to call again
    after a rescan, since Layout.adopt() never overwrites a unit's
    existing placement (see its docstring), only its measured fields."""
    return layout.with_leds(layout.adopt(scan.state()))


@app.post("/api/layout/identify/{unit_id}")
async def identify_unit(unit_id: str):
    """Flashes the given unit's real hardware red so it's visible while
    placing it on the Setup canvas - not a general colour setter. Fan
    channels go through the raw v5 hub client and blank every other known
    fan channel at the same time, since they're all zones on one shared
    controller and would otherwise just stay whatever colour they already
    were; motherboard/RAM are separate physical devices, so only the
    target one needs to change."""
    units = layout.load()["units"]
    unit = next((u for u in units if u["id"] == unit_id), None)
    if unit is None:
        raise HTTPException(404, f"no unit '{unit_id}' in the layout")
    RED, BLACK = (255, 0, 0), (0, 0, 0)

    if unit["kind"] == "fan":
        def _do():
            with _open_hub_raw() as raw:
                ctrl = raw.find_controller("NZXT")
                if ctrl is None:
                    return "no_device"
                raw.select_mode(ctrl, "static")
                for u in units:
                    if u["kind"] != "fan" or not u.get("led_channel"):
                        continue
                    zone = raw.zone_for_channel(ctrl, u["led_channel"])
                    if zone is None or zone.leds_count == 0:
                        continue
                    raw.set_zone_color(ctrl.index, zone, RED if u["id"] == unit_id else BLACK)
                return None
        try:
            error = await asyncio.to_thread(_do)
        except Exception as exc:
            raise HTTPException(503, f"OpenRGB not connected: {exc}")
        if error == "no_device":
            raise HTTPException(404, "no hub RGB device found")
    elif unit["kind"] in ("mobo", "ram"):
        if not openrgb.connected and not await asyncio.to_thread(openrgb.connect):
            raise HTTPException(503, f"OpenRGB not connected: {openrgb.last_error}")
        devices = await asyncio.to_thread(openrgb.motherboard if unit["kind"] == "mobo" else openrgb.ram)
        if unit["kind"] == "ram":
            idx = int(unit_id.split("-")[1]) if "-" in unit_id else 0
            devices = devices[idx:idx + 1]
        if not devices:
            raise HTTPException(404, f"no {unit['kind']} RGB device found")
        await asyncio.to_thread(openrgb.set_color, devices, RED, False)
    else:
        raise HTTPException(400, f"nothing to identify for a '{unit['kind']}' unit")
    return {"ok": True}


# -- panorama display -----------------------------------------------------

async def _call_panel(fn, *args):
    """Runs a blocking panel.<method> call in a thread and treats a write/
    read failure on the underlying handle as "this connection is dead"
    rather than an unhandled 500. panel.connected (app/panorama.py) is
    only a Python-side flag (self._handle is not None), never a live
    check - it stays True even after another program grabs the device
    out from under an already-open handle (KANALI is the documented real
    case - see PanoramaPrint.connect()'s own comment). Confirmed against
    a real crash: the very first write of an upload (the small announce
    frame, not the payload) failed with gle=5 (ACCESS_DENIED) on a handle
    every earlier call in the same session had used successfully.
    Dropping the cached _panorama here means the *next* call goes through
    get_panorama()'s normal reconnect path instead of reusing the same
    broken handle and crashing identically every time until the whole
    server restarts."""
    global _panorama
    try:
        return await asyncio.to_thread(fn, *args)
    except PanoramaError as exc:
        log.warning("panel command failed, dropping the cached connection: %s", exc)
        _panorama = None
        raise HTTPException(503, f"lost the panel connection mid-command ({exc}) - "
                                  "another program (KANALI?) may have grabbed it; try again")


@app.get("/api/panorama/status")
async def panorama_status():
    """Returns the info handshaked once when this connection was
    established (see get_panorama's docstring) - does NOT re-handshake on
    every call, since the device only replies with real info the first
    time after connecting.

    last_applied is what shyyctl itself last set (persisted across
    reconnects/restarts - see _save_panorama_state), NOT a live read of
    what's actually showing on the panel right now - the reverse-
    engineered protocol has no command for that (checked; see
    _save_panorama_state's docstring). If something else changed the
    screen after shyyctl last touched it (KANALI, for instance), this can
    be stale."""
    panel = await get_panorama()
    if panel is None or not panel.connected:
        return {"connected": False}
    return {
        "connected": True,
        "info": _panorama_info,
        "last_applied": {"media": panel.last_media, "brightness": panel.last_brightness},
    }


@app.get("/api/panorama/media")
async def panorama_media():
    panel = await get_panorama()
    if panel is None or not panel.connected:
        raise HTTPException(503, "panel not connected")
    async with _panorama_lock:
        return await _call_panel(panel.list_media)


class BrightnessRequest(BaseModel):
    value: int


@app.post("/api/panorama/brightness")
async def panorama_brightness(body: BrightnessRequest):
    panel = await get_panorama()
    if panel is None or not panel.connected:
        raise HTTPException(503, "panel not connected")
    async with _panorama_lock:
        ok = await _call_panel(panel.set_brightness, body.value)
    if ok:
        _save_panorama_state(panel.last_media, panel.last_brightness,
                             panel.last_poweron_media, panel.last_standby_media)
    return {"ok": ok}


# -- panorama gallery ---------------------------------------------------
# A local cache of everything ever uploaded through shyyctl, replacing the
# old one-shot "pick a file, apply it, forget it" flow. The device itself
# can't hand back a thumbnail or the original file for something already
# on it - list_media() only reports path/suffix/size (app/panorama.py) -
# so re-showing what's been uploaded before means keeping our own copy,
# not trying to pull one back off the panel. Each entry keeps the exact
# original file (so Apply/Set boot can re-run the real upload path
# without asking for the file a second time) plus a small ffmpeg-
# generated thumbnail for the grid. Uploading only ever caches - it does
# not touch the panel by itself; Apply/Set boot are separate, explicit
# actions on a gallery tile, same as they were as separate buttons
# before, just scoped to one cached item instead of one ad-hoc upload.

@app.get("/api/panorama/gallery")
async def gallery_list():
    return _load_gallery()


@app.post("/api/panorama/gallery")
async def gallery_upload(file: UploadFile = File(...)):
    GALLERY_DIR.mkdir(parents=True, exist_ok=True)
    item_id = uuid.uuid4().hex[:12]
    item_dir = GALLERY_DIR / item_id
    item_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "upload").suffix or ".png"
    original_path = item_dir / f"original{suffix}"
    original_path.write_bytes(await file.read())
    has_thumb = await asyncio.to_thread(
        generate_thumbnail, str(original_path), str(item_dir / "thumb.jpg"))
    entry = {
        "id": item_id, "name": file.filename or item_id, "suffix": suffix,
        "has_thumb": has_thumb, "uploaded_at": time.time(),
    }
    items = _load_gallery()
    items.append(entry)
    _save_gallery(items)
    return entry


@app.get("/api/panorama/gallery/{item_id}/thumb")
async def gallery_thumb(item_id: str):
    thumb_path = GALLERY_DIR / item_id / "thumb.jpg"
    if not thumb_path.is_file():
        raise HTTPException(404, "no thumbnail")
    return FileResponse(thumb_path, media_type="image/jpeg")


@app.get("/api/panorama/gallery/{item_id}/original")
async def gallery_original(item_id: str):
    item_dir = GALLERY_DIR / item_id
    if not item_dir.is_dir():
        raise HTTPException(404, "no such item")
    original = next(item_dir.glob("original.*"), None)
    if original is None:
        raise HTTPException(404, "no original file")
    suffix = original.suffix.lower()
    media_type = "video/mp4" if suffix == ".mp4" else "video/webm" if suffix == ".webm" else "video/quicktime" if suffix == ".mov" else "video/x-msvideo" if suffix == ".avi" else "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/png" if suffix == ".png" else "image/webp" if suffix == ".webp" else "application/octet-stream"
    return FileResponse(original, media_type=media_type)


async def _apply_gallery_item(item_id: str, as_boot: bool) -> dict:
    item_dir = GALLERY_DIR / item_id
    original = next(item_dir.glob("original.*"), None) if item_dir.is_dir() else None
    if original is None:
        raise HTTPException(404, "no such gallery item")
    panel = await get_panorama()
    if panel is None or not panel.connected:
        raise HTTPException(503, "panel not connected")
    async with _panorama_lock:
        remote_name = await _call_panel(panel.upload_media, str(original))
        if remote_name is None:
            raise HTTPException(500, "upload failed (encode or transfer step) - check server logs")
        if as_boot:
            ok = await _call_panel(panel.set_boot_media, remote_name)
        else:
            ok = await _call_panel(panel.set_media, remote_name)
        if ok:
            _save_panorama_state(panel.last_media, panel.last_brightness,
                                 panel.last_poweron_media, panel.last_standby_media)
    return {"filename": remote_name, "applied": ok}


@app.post("/api/panorama/gallery/{item_id}/apply")
async def gallery_apply(item_id: str):
    return await _apply_gallery_item(item_id, as_boot=False)


@app.post("/api/panorama/gallery/{item_id}/boot")
async def gallery_set_boot(item_id: str):
    return await _apply_gallery_item(item_id, as_boot=True)


@app.delete("/api/panorama/gallery/{item_id}")
async def gallery_delete(item_id: str):
    items = _load_gallery()
    remaining = [i for i in items if i["id"] != item_id]
    if len(remaining) == len(items):
        raise HTTPException(404, "no such gallery item")
    _save_gallery(remaining)
    shutil.rmtree(GALLERY_DIR / item_id, ignore_errors=True)
    return {"ok": True}


# -- frontend ---------------------------------------------------------------

if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")


def _already_running(host: str, port: int) -> bool:
    """True if something is already answering /api/status on our own port -
    i.e. shyyctl itself is already running. Only used for the frozen/
    installed build: the Registry Run key autostart launches this exe
    silently at every login, but a human can also open it again from the
    Start Menu shortcut at any time - without this check, that second
    launch would just crash trying to bind a port the first instance
    already holds, instead of doing the one useful thing a second launch
    should do (open a browser tab onto the instance that's already up)."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/status", timeout=1) as resp:
            return resp.status == 200
    except Exception:
        return False


if __name__ == "__main__":
    _host = CONFIG["server"]["host"]
    _port = CONFIG["server"]["port"]

    if "--lite" in sys.argv:
        # Deliberately still checked against the *normal* instance's port
        # even though --lite never binds one itself - running both at once
        # against the same hardware would just mean two processes racing
        # each other to write the same fan curve / RGB effect, not two
        # independent capabilities.
        if _already_running(_host, _port):
            log.info("shyyctl is already running (full instance) on %s:%d - not starting "
                     "a second, --lite copy alongside it.", _host, _port)
            sys.exit(0)
        try:
            asyncio.run(_lite_service_main())
        except KeyboardInterrupt:
            pass
        sys.exit(0)

    import uvicorn

    if getattr(sys, "frozen", False) and _already_running(_host, _port):
        if "--background" in sys.argv:
            log.info("shyyctl is already running on %s:%d - nothing to do for a "
                     "silent autostart launch.", _host, _port)
        else:
            log.info("shyyctl is already running on %s:%d - opening it in a browser "
                     "instead of starting a second instance.", _host, _port)
            webbrowser.open(f"http://{_host}:{_port}/")
        sys.exit(0)
    uvicorn.run(app, host=_host, port=_port)
