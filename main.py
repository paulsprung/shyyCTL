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

import sys

if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--liquidctl":
    # The installed exe doubles as the bundled liquidctl CLI (see
    # app/hub.py's _liquidctl_command): `shyyctl.exe --liquidctl <args>`
    # runs exactly what `liquidctl <args>` would, in its own short-lived
    # process like the real CLI. Handled before anything else in this
    # module runs - none of the server setup below (log redirection/
    # rotation, config, hardware objects) may happen in this child. The
    # frozen exe is a windowed build, so stdout/stderr can be None; they
    # are rebuilt from the pipes the parent passed in, forced to utf-8
    # (liquidctl prints box-drawing characters).
    import os

    def _std_stream(fd: int, handle_id: int):
        try:
            return os.fdopen(os.dup(fd), "w", encoding="utf-8", errors="replace", buffering=1)
        except OSError:
            pass
        try:
            import ctypes
            import msvcrt
            get_std_handle = ctypes.windll.kernel32.GetStdHandle
            get_std_handle.restype = ctypes.c_void_p
            handle = get_std_handle(handle_id)
            if handle not in (None, ctypes.c_void_p(-1).value):
                return open(msvcrt.open_osfhandle(handle, 0), "w", encoding="utf-8",
                            errors="replace", buffering=1)
        except Exception:
            pass
        return open(os.devnull, "w")

    for _name, _fd, _handle_id in (("stdout", 1, -11), ("stderr", 2, -12)):
        _stream = getattr(sys, _name)
        if _stream is not None and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
        else:
            setattr(sys, _name, _std_stream(_fd, _handle_id))

    try:
        from app.hub import patch_liquidctl_reads
        patch_liquidctl_reads(full=True)
    except Exception:
        pass

    from liquidctl.cli import main as _liquidctl_main
    sys.argv = ["liquidctl"] + sys.argv[2:]
    _code = _liquidctl_main()
    sys.stdout.flush()
    sys.stderr.flush()
    sys.exit(_code)

import asyncio
import colorsys
import contextlib
import functools
import hashlib
import io
import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
if "--lite" in sys.argv:
    # The background service serves no web UI - see app/web_stub.py: FastAPI
    # and its dependencies were ~25MB of RAM it only loaded to define routes.
    from app import web_stub
    web_stub.install()
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.conflicts import running_conflicts
from app.hub import DirectHub, Hub, HubError
from app.layout import Layout
from app.openrgb_bridge import OpenRgbBridge
from app.openrgb_raw import OpenRgbRaw
from app.ambient import SAMPLE_FPS as AMBIENT_FPS, AmbientSampler
from app.panorama import (DEFAULT_POWERON_MEDIA, KANALI_DEFAULT_POWERON_MEDIA, PanoramaError,
                          build_encoded_media_container, find_print_panel,
                          find_usb_panel, generate_thumbnail)
from app.procflags import NO_WINDOW
from app.scan import Scan
from app import geometry
from app import startup_anim
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
STARTUP_ANIM_FILE = ROOT / "startup_animation.json"
AMBIENT_FRAME_FILE = ROOT / "ambient_frame.json"
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
APP_VERSION = "1.0.10"
UPDATE_REPO = "paulsprung/shyyctl"

_log_file = None  # only ever opened below (frozen build) - export_log() flushes it before reading
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

# hub.control: "direct" (default) - shyyctl drives the hub's fans and LEDs
# itself over one open connection, OpenRGB only handles RAM + motherboard
# (see app/hub.py's DirectHub). "openrgb" - the previous split: hub LEDs via
# OpenRGB, fans via a liquidctl process per call.
HUB_DIRECT = str(CONFIG["hub"].get("control", "direct")).lower() != "openrgb"
hub = (DirectHub if HUB_DIRECT else Hub)(match=CONFIG["hub"]["match"])
openrgb = OpenRgbBridge(host=CONFIG["openrgb"]["host"], port=CONFIG["openrgb"]["port"])
scan = Scan(hub, openrgb)
layout = Layout(LAYOUT_FILE)
ambient_sampler = AmbientSampler()

_panorama = None
_panorama_info: dict | None = None
_panorama_lock = asyncio.Lock()
# Handshake retries for a connection whose first handshake got no device
# info back - capped and spaced out (see get_panorama), not one per request.
_panorama_handshake_retries = 0
_panorama_handshake_next = 0.0
_PANORAMA_HANDSHAKE_MAX_RETRIES = 3
_PANORAMA_HANDSHAKE_RETRY_S = 30.0


def _drop_panorama(panel) -> None:
    """Forget a connection that just failed AND close its OS handle.
    Dropping only the reference (as every failure path used to) left the
    usbprint.sys handle and its event object open for the rest of the
    process's life - one leaked handle pair per drop, and real logs show
    a drop every few minutes, so hundreds over a day, some still pointing
    at a device instance that had re-enumerated. Only clears the global if
    it's still this same object, so a newer connection is never thrown
    away by a late failure report about an older one."""
    global _panorama, _panorama_info
    if panel is not None:
        try:
            panel.disconnect()
        except Exception as exc:
            log.debug("closing dropped panel connection failed: %s", exc)
    if _panorama is panel:
        _panorama = None
        _panorama_info = None


_keepalive_task: asyncio.Task | None = None
_curve_task: asyncio.Task | None = None
_curve_status: dict = {"curves": []}
_curve_status_read_at = 0.0  # last time anyone asked for it - see _curve_loop

# Host-driven, position-aware hub effects (Advanced RGB view) - see
# _run_position_effect below. A plain threading.Event rather than an asyncio
# primitive because the effect body is a tight blocking-socket loop run
# on its own thread (_start_effect_thread), not a coroutine; setting the
# event from a request handler's own thread is safe without a lock, it's
# just a one-way "stop now" flag.
#
# Each run gets a fresh Event (see _new_effect_stop) instead of the one
# shared Event being cleared again: a previous run that didn't exit within
# _stop_effect's timeout (stuck in a slow write) used to see the cleared
# flag and carry on, two loops then fighting over the same LEDs.
_effect_stop = threading.Event()
_effect_task: asyncio.Future | None = None
_effect_status: dict = {"running": False, "kind": None, "error": None}


def _new_effect_stop() -> threading.Event:
    global _effect_stop
    _effect_stop = threading.Event()
    return _effect_stop


def _start_effect_thread(fn, *args) -> asyncio.Future:
    """Effect loops run for as long as the effect does, so each gets a
    thread of its own instead of permanently occupying a slot of the
    default pool (and having to wait for a free one to start at all).
    The returned future resolves once the loop has exited."""
    loop = asyncio.get_running_loop()
    done = loop.create_future()

    def finished():
        if not done.done():
            done.set_result(None)

    def run():
        try:
            fn(*args)
        finally:
            try:
                loop.call_soon_threadsafe(finished)
            except RuntimeError:
                pass  # loop already closed (shutdown) - nobody's waiting any more

    threading.Thread(target=run, name="effect", daemon=True).start()
    return done


# liquidctl calls (hub.*) run on their own threads rather than the default
# pool asyncio.to_thread shares with everything else. Each one can block for
# seconds (one liquidctl process at a time, retries on "missing messages"),
# and a pile of them waiting there once starved the effect loop, OpenRGB
# and Panorama calls of threads altogether.
_hub_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="liquidctl")


async def _hub_call(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(
        _hub_executor, functools.partial(fn, *args))


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
                          poweron_media: str | None = None, standby_media: str | None = None,
                          gallery_id: str | None = "__unset__",
                          boot_gallery_id: str | None = "__unset__"):
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
    real power cycle.

    gallery_id: the local gallery item id last applied, kept separately
    from `media` - `media` is the PANEL's own remote filename for it
    (default_media_name()'s generated timestamp name, confirmed against
    real hardware to never equal the gallery item's original uploaded
    filename), so nothing that wants "which gallery item is this" can
    match it back against _load_gallery()'s entries by name. Defaults to
    the sentinel "__unset__" (not None) so brightness-only calls that
    don't know or care about the current media (panorama_brightness) can
    omit it and leave whatever gallery_id was already saved alone,
    distinct from a real caller explicitly clearing it to None.

    boot_gallery_id: the same for the power-on slot - which gallery item
    the custom boot screen came from, None for the TRYX default."""
    saved = _load_panorama_state()
    if gallery_id == "__unset__":
        gallery_id = saved.get("gallery_id")
    if boot_gallery_id == "__unset__":
        boot_gallery_id = saved.get("boot_gallery_id")
    PANORAMA_STATE_FILE.write_text(
        json.dumps({"media": media, "brightness": brightness,
                   "poweron_media": poweron_media, "standby_media": standby_media,
                   "gallery_id": gallery_id, "boot_gallery_id": boot_gallery_id}),
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
    the gallery entry it names has since been deleted.

    Matched by gallery_id, not by the saved "media" name - that field is
    the PANEL's own generated remote filename for the upload (see
    _save_panorama_state's docstring), which on real hardware is never
    equal to the gallery item's original filename this used to compare
    it against, so the match never actually succeeded outside a demo
    where both were fabricated to line up."""
    gallery_id = _load_panorama_state().get("gallery_id")
    if not gallery_id:
        return None
    for item in _load_gallery():
        if item.get("id") != gallery_id:
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
    global _panorama, _panorama_info, _panorama_handshake_retries, _panorama_handshake_next
    async with _panorama_lock:
        if _panorama is not None and _panorama.connected:
            if (_panorama_info is None
                    and _panorama_handshake_retries < _PANORAMA_HANDSHAKE_MAX_RETRIES
                    and time.monotonic() >= _panorama_handshake_next):
                # The one handshake attempt for this connection didn't
                # stick yet (e.g. it raced the device's own startup, or a
                # reply timed out) - retry here instead of returning the
                # same cached None for the rest of this connection's
                # lifetime. Confirmed against a real run this was a
                # genuine regression: once _panorama_info was None, every
                # later call hit this fast path and never tried again.
                # Capped and spaced out though: this path runs on every
                # /api/status poll (every 3s per open tab), and each retry
                # re-sends the whole registration burst KANALI only ever
                # sends once per connection, while holding the lock the
                # keepalive ping needs for up to the handshake's timeout.
                _panorama_handshake_retries += 1
                _panorama_handshake_next = time.monotonic() + _PANORAMA_HANDSHAKE_RETRY_S
                try:
                    _panorama_info = await asyncio.to_thread(_panorama.handshake)
                except Exception as exc:
                    log.warning("panorama handshake retry failed: %s", exc)
            return _panorama
        if _panorama is not None:
            _drop_panorama(_panorama)
        _panorama = await asyncio.to_thread(_find_panorama)
        _panorama_info = None
        _panorama_handshake_retries = 0
        _panorama_handshake_next = time.monotonic() + _PANORAMA_HANDSHAKE_RETRY_S
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
    # Was this loop's last tick able to see a live connection - tracked
    # only to log state *transitions* (dropped / recovered), not every
    # single 2s tick, while still landing on INFO level (a plain
    # `python main.py` run, or the installed exe's shyyctl.log, both
    # default to INFO - see logging.basicConfig above) instead of DEBUG,
    # which never made it into an exported log at all. Direct user
    # report: the display goes black and recovers on its own repeatedly
    # (minutes on, minutes off, cycling) - this loop's own reasoning
    # already covers *that* a drop gets retried and recovered, but
    # without these two lines actually reaching the log file, there was
    # no way to see *when* each drop/recovery happened to tell a real
    # recurring USB-level dropout apart from a shyyctl-side bug from the
    # log alone.
    was_connected = False
    lost_at = None
    while True:
        try:
            panel = _panorama
            if panel is None or not panel.connected:
                await get_panorama()
                if _panorama is not None and _panorama.connected:
                    if lost_at is not None:
                        log.info("panorama keepalive: reconnected after %.0fs", time.monotonic() - lost_at)
                    else:
                        log.info("panorama keepalive: reconnected")
                    was_connected = True
                    lost_at = None
            else:
                async with _panorama_lock:
                    try:
                        await asyncio.to_thread(panel.ping)
                    except PanoramaError:
                        # A real protocol-level failure (not just "nothing
                        # plugged in yet") - the device went away under
                        # this handle (re-enumerated, or something else
                        # grabbed it). connected (app/panorama.py) is only
                        # a Python-side flag, never a live check, so it
                        # would otherwise stay True forever. Dropped (and
                        # its handle closed) while still holding the lock,
                        # so no other request can be mid-I/O on it; the
                        # next tick (~2s away) reconnects.
                        _drop_panorama(panel)
                        raise
                if not was_connected:
                    log.info("panorama keepalive: ping succeeding again")
                was_connected = True
        except Exception as exc:
            if was_connected:
                log.info("panorama keepalive: connection lost (%s) - retrying every 2s", exc)
                lost_at = time.monotonic()
            was_connected = False
        await asyncio.sleep(2)


# -- helpers started through scheduled tasks (no UAC prompt) ------------------
#
# OpenRGB (RAM RGB) and SensorHelper (temperatures) need admin rights. The
# default is a UAC prompt for each at every launch; once set up from
# Settings (tools/elevated_helpers.ps1, one prompt), protected copies of
# both in %ProgramFiles%\shyyctl\helpers are started by two scheduled
# tasks at the highest run level instead - see that script for why only
# that arrangement is safe to run elevated without asking. A copy that no
# longer matches the bundled helper (after an update) isn't used: back to
# the prompt, until set up again.

HELPER_TASK_DIR = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "shyyctl" / "helpers"
HELPER_TASKS = ("OpenRGB", "SensorHelper")
_HELPER_SCRIPT = (ASSETS_DIR / "elevated_helpers.ps1" if getattr(sys, "frozen", False)
                  else ROOT / "tools" / "elevated_helpers.ps1")
_digests: dict[tuple, str] = {}


def _sensor_helper_port() -> int:
    lhm_url = CONFIG.get("sensors", {}).get("librehardwaremonitor_url", "")
    try:
        return int(lhm_url.rsplit(":", 1)[1].split("/", 1)[0])
    except (IndexError, ValueError):
        return 8085


def _helper_exe(base: Path, name: str) -> Path | None:
    if name == "OpenRGB":
        folder = base / "OpenRGB"
        return next(folder.rglob("OpenRGB.exe"), None) if folder.is_dir() else None
    exe = base / "SensorHelper" / "SensorHelper.exe"
    return exe if exe.is_file() else None


def _file_digest(path: Path) -> str:
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key not in _digests:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        _digests[key] = h.hexdigest()
    return _digests[key]


def _task_exists(name: str) -> bool:
    try:
        return subprocess.run(["schtasks", "/query", "/tn", f"\\shyyctl\\{name}"], capture_output=True,
                              timeout=10, creationflags=NO_WINDOW).returncode == 0
    except Exception:
        return False


def _helper_task_state(name: str) -> str:
    """"missing" (not set up), "outdated" (the protected copy no longer
    matches the bundled helper, or the sensor port changed - set up
    again), or "ready"."""
    if sys.platform != "win32":
        return "missing"
    protected = _helper_exe(HELPER_TASK_DIR, name)
    if protected is None or not _task_exists(name):
        return "missing"
    bundled = _helper_exe(ROOT / "vendor", name)
    try:
        if bundled is not None and _file_digest(bundled) != _file_digest(protected):
            return "outdated"
    except OSError:
        return "outdated"
    if name == "SensorHelper":
        try:
            setup = json.loads((HELPER_TASK_DIR / "setup.json").read_text(encoding="utf-8-sig"))
            if int(setup.get("sensor_port", 0)) != _sensor_helper_port():
                return "outdated"
        except Exception:
            return "outdated"
    return "ready"


def _run_helper_task(name: str) -> bool:
    try:
        return subprocess.run(["schtasks", "/run", "/tn", f"\\shyyctl\\{name}"], capture_output=True,
                              timeout=15, creationflags=NO_WINDOW).returncode == 0
    except Exception as exc:
        log.warning("could not start scheduled task %s: %s", name, exc)
        return False


def _run_elevated_helpers_script(action: str) -> dict:
    """Runs tools/elevated_helpers.ps1 elevated (one UAC prompt) and
    waits for it. Blocking - call from a thread."""
    import ctypes
    from ctypes import wintypes
    import tempfile

    if not _HELPER_SCRIPT.is_file():
        return {"ok": False, "message": f"setup script missing: {_HELPER_SCRIPT}"}
    result_file = Path(tempfile.gettempdir()) / f"shyyctl-helpers-{os.getpid()}.json"
    result_file.unlink(missing_ok=True)
    args = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
            "-File", str(_HELPER_SCRIPT), "-Action", action, "-ResultFile", str(result_file)]
    if action == "install":
        args += ["-VendorDir", str(ROOT / "vendor"),
                 "-User", f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\"),
                 "-OpenRgbSettings", str(Path(os.environ.get("APPDATA", "")) / "OpenRGB" / "OpenRGB.json"),
                 "-SensorPort", str(_sensor_helper_port())]

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
                    ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
                    ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
                    ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040 | 0x00000100  # SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.lpVerb = "runas"
    info.lpFile = "powershell.exe"
    info.lpParameters = subprocess.list2cmdline(args)
    info.nShow = 0  # SW_HIDE
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        return {"ok": False, "message": "the administrator prompt was declined" if err == 1223
                else f"could not start the setup (error {err})"}
    try:
        kernel32.WaitForSingleObject(info.hProcess, 300_000)
    finally:
        kernel32.CloseHandle(info.hProcess)
    try:
        return json.loads(result_file.read_text(encoding="utf-8-sig"))
    except Exception:
        return {"ok": False, "message": "the setup didn't report back"}
    finally:
        result_file.unlink(missing_ok=True)


def _elevated_helpers_status() -> dict:
    states = {name: _helper_task_state(name) for name in HELPER_TASKS}
    return {
        "supported": sys.platform == "win32",
        "states": states,
        "ready": all(v == "ready" for v in states.values()),
        "any": any(v != "missing" for v in states.values()),
        "folder": str(HELPER_TASK_DIR),
    }


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
    if elevated and _helper_task_state("OpenRGB") == "ready":
        log.info("Starting OpenRGB through its scheduled task (protected copy, no UAC prompt)")
        if _run_helper_task("OpenRGB"):
            return True
        log.warning("OpenRGB's scheduled task didn't start - falling back to a UAC prompt")
    # --server alone: OpenRGB runs its SDK server without loading its
    # window and tray icon at all - about half the memory of the
    # (minimised) window it used to start with, which nobody opened.
    args = "--server"
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
        subprocess.Popen([str(exe), "--server"], cwd=str(exe.parent))
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

    if await asyncio.to_thread(_launch_vendor_openrgb):
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

    port = _sensor_helper_port()
    elevated = CONFIG.get("sensor_helper", {}).get("launch_elevated", True)
    if elevated and _helper_task_state("SensorHelper") == "ready":
        log.info("Starting SensorHelper through its scheduled task (protected copy, no UAC prompt)")
        if _run_helper_task("SensorHelper"):
            return True
        log.warning("SensorHelper's scheduled task didn't start - falling back to a UAC prompt")
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


# OpenRGB's own detector name for this hub (USB PID 0x2022 - see its
# Controllers/NZXTHue2Controller/NZXTHue2ControllerDetect.cpp).
OPENRGB_HUB_DETECTOR = "NZXT RGB & Fan Controller 2024"


def _set_openrgb_hub_detector(enabled: bool) -> bool:
    """Switches OpenRGB's detection of the hub on or off in its settings
    file (%APPDATA%\\OpenRGB\\OpenRGB.json, "Detectors" -> "detectors" ->
    name: bool - the same switch as OpenRGB's Settings > Supported Devices
    list). With hub.control: direct, shyyctl is the hub's only driver and
    OpenRGB is left with RAM + motherboard; it only reads this at startup.
    Every other setting in the file is kept as it is. Returns whether the
    file changed."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return False
    path = Path(appdata) / "OpenRGB" / "OpenRGB.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except Exception as exc:
        # Never rewrite a settings file that can't be read back - that
        # would throw away every other OpenRGB setting in it.
        log.warning("could not read %s (%s) - leaving OpenRGB's hub detection as it is", path, exc)
        return False
    if not isinstance(data, dict):
        return False
    section = data.get("Detectors")
    if not isinstance(section, dict):
        section = data["Detectors"] = {}
    detectors = section.get("detectors")
    if not isinstance(detectors, dict):
        detectors = section["detectors"] = {}
    if detectors.get(OPENRGB_HUB_DETECTOR, True) == enabled:
        return False
    detectors[OPENRGB_HUB_DETECTOR] = enabled
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=4), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:
        log.warning("could not update %s: %s", path, exc)
        return False
    log.info("OpenRGB hub detection %s (hub.control: %s)",
             "enabled" if enabled else "disabled", "direct" if HUB_DIRECT else "openrgb")
    return True


async def _sync_openrgb_hub_detection():
    """Before connecting to OpenRGB at startup: make its hub detection
    match hub.control, and restart an already running OpenRGB whose
    setting just changed (it only reads the file when it starts) - the
    usual startup path then launches it again."""
    if await asyncio.to_thread(_set_openrgb_hub_detector, not HUB_DIRECT):
        if await asyncio.to_thread(_process_running, "OpenRGB.exe"):
            log.info("restarting OpenRGB so its hub detection change takes effect")
            await asyncio.to_thread(_kill_process, "OpenRGB.exe")
            openrgb.disconnect()
            await asyncio.sleep(1.0)


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


async def _reapply_rgb_state(hub_only: bool = False, boards_only: bool = False):
    """Pushes every colour saved by _save_rgb_state_entry back to the
    hardware. Called unconditionally on every launch, same reasoning as
    the saved position effect and Panorama's last-applied media/
    brightness: OpenRGB gives no way to read a device's actual current
    colour back, so there's no "only if not already set" check possible -
    only "always re-apply, best effort, one target's failure doesn't
    block the rest". hub_only skips motherboard and RAM (see
    _reapply_hub_rgb_state_delayed); boards_only skips the hub (direct
    control re-applies it at launch without waiting for OpenRGB).

    Waits for the startup animation first (its finale lights RAM and the
    board too), and with a saved effect about to take over, leaves alone
    every unit that effect drives - a static colour in between would
    flash before the effect fades in from where the animation ended."""
    state = _load_rgb_state()
    if not state:
        return
    await _wait_startup_animation()
    if _startup_anim_taken_over() or _effect_status.get("running"):
        return  # something the user started has the LEDs now
    driven: set[str] = set()
    if HUB_DIRECT and EFFECT_STATE_FILE.is_file():
        units = layout.load().get("units", [])
        driven = {u.get("led_channel") for u in units if u.get("kind") == "fan"}
        driven |= {u["id"] for u in units if u.get("kind") in ("mobo", "ram")}

    mobo = None if hub_only or "mobo" in driven else state.get("mobo", {}).get("0")
    if mobo:
        try:
            devices = await asyncio.to_thread(openrgb.motherboard)
            if devices:
                await asyncio.to_thread(
                    openrgb.set_color, devices, _hex_to_rgb(mobo["color"]), mobo.get("persistent", True))
        except Exception as exc:
            log.warning("could not reapply saved motherboard colour: %s", exc)

    ram_state = {} if hub_only else {i: e for i, e in state.get("ram", {}).items()
                                     if f"ram-{i}" not in driven}
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

    hub_state = {} if boards_only else {ch: e for ch, e in state.get("hub", {}).items()
                                         if ch not in driven}
    for channel, entry in hub_state.items():
        try:
            rgb = _hex_to_rgb(entry["color"])
            persistent = entry.get("persistent", True)

            def _do(channel=channel, rgb=rgb, persistent=persistent):
                with _hub_leds() as leds:
                    # In OpenRGB mode a channel's zone size doesn't survive
                    # an OpenRGB restart until a scan re-sizes it (see the
                    # resize endpoint's docstring) - skip silently rather
                    # than writing colour data to a zone reporting 0 LEDs.
                    if not leds.channel_leds(channel):
                        return
                    leds.fill(channel, rgb, persistent)

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
    server startup on it.

    Hub channels only, and not at all while an effect runs or is about to
    be resumed: this used to re-apply motherboard and RAM too, right as a
    resumed effect started - switching both back to a static mode the
    effect never leaves again (it selects Direct once, at its start), so
    the board stayed dark for the whole effect. Direct hub control needs
    none of this - its channels are sized without a scan, so the first
    reapply already reached them."""
    if HUB_DIRECT:
        return
    await asyncio.sleep(10)
    if _effect_status.get("running") or EFFECT_STATE_FILE.is_file():
        return
    state = _load_rgb_state()
    if state.get("hub"):
        await _reapply_rgb_state(hub_only=True)


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
    at the cap either way rather than never running at all.

    With direct hub control nothing waits on the scan - channel sizes come
    from the layout - so the effect starts as soon as the startup
    animation is over, fading in from where it ended."""
    if not HUB_DIRECT:
        # the hub's zones live in OpenRGB here - it, then the scan sizing
        # them, has to come first
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_openrgb_bringup_done.wait(), timeout=120)
        await asyncio.sleep(0.5)
        for _ in range(150):
            if not scan.running:
                break
            await asyncio.sleep(0.2)
    played = await _wait_startup_animation()
    if _startup_anim_taken_over() or _effect_status.get("running"):
        return  # Stop, or something else started meanwhile - don't paint over it
    try:
        await _do_start_position_effect(saved_effect, persist=False, fade_in=played)
    except Exception as exc:
        log.warning("Could not resume saved effect: %s", exc)


# -- startup animation ------------------------------------------------------
#
# The hub keeps nothing across a power-off (every LED comes up white -
# confirmed on the real hardware), so the first thing the fan rings show
# that shyyctl chose is this: played on the hub right after shyyctl has it,
# optionally with RAM and motherboard joining for a finale once OpenRGB is
# up, then faded into the saved effect or the saved static colours.
# Frames: app/startup_anim.py.

class StartupAnimation(BaseModel):
    enabled: bool = True
    style: str = "case_spin"            # startup_anim.STYLES
    start_unit: str = ""                # fan unit the turn starts at; "" = the left-most
    reverse: bool = True                # counter-clockwise (on the canvas)
    palette: str = "solid"              # startup_anim.PALETTES
    color: str = "#ffffff"
    color2: str = "#4c79ff"             # gradient: the second colour
    brightness: int = 100               # %
    lap_s: float = 1.5                  # one turn
    laps: int = 2                       # turns before a ring is fully lit
    ambient_source: str = ""            # gallery item id; "" = the power-on video
    ambient_on_display: bool = True     # the panel plays that video meanwhile
    show_s: float = 8.0                 # ambient: how long
    finale: bool = True                 # RAM + motherboard join, everything lights up
    fade_s: float = 1.5                 # into the saved effect / colours


def _load_startup_animation() -> StartupAnimation:
    try:
        cfg = StartupAnimation.model_validate_json(STARTUP_ANIM_FILE.read_text(encoding="utf-8"))
        if cfg.style not in startup_anim.STYLES:
            cfg = cfg.model_copy(update={"style": "case_spin"})  # snake/cascade/chase/fill were dropped
        return cfg
    except FileNotFoundError:
        return StartupAnimation()
    except Exception as exc:
        log.warning("could not read %s (%s) - using the default startup animation",
                    STARTUP_ANIM_FILE.name, exc)
        return StartupAnimation()


def _checked_startup_animation(body: StartupAnimation) -> StartupAnimation:
    for field, allowed in (("style", startup_anim.STYLES), ("palette", startup_anim.PALETTES)):
        if getattr(body, field) not in allowed:
            raise HTTPException(400, f"unknown {field} {getattr(body, field)!r} - must be one of {allowed}")
    _hex_to_rgb(body.color)
    _hex_to_rgb(body.color2)
    if body.ambient_source and not any(i["id"] == body.ambient_source for i in _load_gallery()):
        raise HTTPException(400, "the ambient video is no longer in the gallery")
    return body.model_copy(update={
        "brightness": min(max(body.brightness, 5), 100),
        "lap_s": min(max(body.lap_s, 0.3), 10.0),
        "laps": min(max(body.laps, 1), 5),
        "show_s": min(max(body.show_s, 2.0), 30.0),
        "fade_s": min(max(body.fade_s, 0.0), 10.0),
    })


_startup_anim_task: asyncio.Future | None = None
_startup_anim_loop: asyncio.AbstractEventLoop | None = None
# unit id -> the colours it was left showing when the animation ended (by
# strip index) - where a following effect fades in from. None until an
# animation has played.
_startup_anim_end: dict[str, list[tuple[int, int, int]]] | None = None
_STARTUP_FRAME_S = 0.04
_FINALE_S = 1.2
_FINALE_OPENRGB_WAIT_S = 20.0


class _Show:
    """One run of the startup animation: settings, rings, colours, and the
    brightness every write goes out at."""

    def __init__(self, cfg: StartupAnimation, stop: threading.Event):
        self.cfg = cfg
        self.stop = stop
        self.colors = [_hex_to_rgb(cfg.color), _hex_to_rgb(cfg.color2)]
        self.level = cfg.brightness / 100
        self.rings = _startup_rings(cfg.reverse)
        self.t0 = time.monotonic()

    def t(self) -> float:
        return time.monotonic() - self.t0

    def paint(self, u: float) -> tuple[int, int, int]:
        return startup_anim.paint(self.cfg.palette, self.colors, u, self.t())

    def write(self, frames: dict[str, list[tuple]]):
        for ring in self.rings:
            hub.set_leds(ring["channel"], [startup_anim.scale(c, self.level) for c in frames[ring["id"]]])

    def tick(self):
        self.stop.wait(_STARTUP_FRAME_S)


def _startup_rings(reverse: bool = False) -> list[dict]:
    """Every placed fan unit the hub drives: {"id", "channel", "leds"
    (layout entries with positions), "places" (ring_order), "count"}.
    Units whose positions don't match their LED count run in strip order."""
    data = layout.with_leds()
    rings = []
    for u in data["units"]:
        channel = u.get("led_channel")
        if u.get("kind") != "fan" or not channel or channel not in hub.channels.get("led", []):
            continue
        count = _hub_channel_led_count(channel)
        if not count:
            continue
        leds = [l for l in data["leds"] if l["unit"] == u["id"]]
        places = startup_anim.ring_order(leds, reverse)
        if len(places) != count or len(leds) != count:
            leds = []
            places = [i / count for i in range(count)]
        rings.append({"id": u["id"], "channel": channel, "leds": leds, "places": places, "count": count})
    return rings


def _play_case_spin(show: _Show) -> dict:
    """The whole case turns as one ring: every fan LED placed by its angle
    round the centre of all the fans (startup_anim.case_turns), so the
    light sweeps from fan to fan round the case, `laps` times, its tail
    growing until everything is lit. Ends with every ring fully lit."""
    cfg, rings = show.cfg, show.rings
    turns = startup_anim.case_turns([(r["id"], r["leds"]) for r in rings], cfg.start_unit, cfg.reverse)
    places = {r["id"]: turns.get(r["id"]) or r["places"] for r in rings}
    total = startup_anim.spin_duration(cfg.lap_s, cfg.laps)
    start = time.monotonic()
    while not show.stop.is_set():
        t = time.monotonic() - start
        show.write({r["id"]: startup_anim.spin_frame(places[r["id"]], t, cfg.lap_s, cfg.laps,
                                                      startup_anim.CASE_TAIL_START, cfg.palette, show.colors)
                    for r in rings})
        if t >= total:
            break
        show.tick()
    return {r["id"]: startup_anim.full_frame(places[r["id"]], cfg.palette, show.colors, show.t()) for r in rings}


def _play_spin(show: _Show) -> dict:
    """Every ring spins `laps` times on its own, its tail growing until the
    whole ring is lit - the rings starting one after another in their
    order round the case (from start_unit, in the turn's direction), each
    next one half a turn later. Ends with every ring fully lit."""
    cfg, rings = show.cfg, show.rings
    order = startup_anim.unit_sequence([(r["id"], r["leds"]) for r in rings], cfg.start_unit, cfg.reverse)
    order += [r["id"] for r in rings if r["id"] not in order]
    delays = {uid: i * cfg.lap_s * startup_anim.SPIN_STAGGER for i, uid in enumerate(order)}
    total = max(delays.values()) + startup_anim.spin_duration(cfg.lap_s, cfg.laps)
    start = time.monotonic()
    while not show.stop.is_set():
        t = time.monotonic() - start
        show.write({r["id"]: startup_anim.spin_frame(
            r["places"], t - delays[r["id"]], cfg.lap_s, cfg.laps,
            startup_anim.SPIN_TAIL_START_LEDS / r["count"], cfg.palette, show.colors)
            for r in rings})
        if t >= total:
            break
        show.tick()
    return {r["id"]: startup_anim.full_frame(r["places"], cfg.palette, show.colors, show.t()) for r in rings}


def _call_on_loop(coro, timeout: float):
    """Runs a coroutine on the server's event loop from the animation's
    thread (the Panorama lock is an asyncio lock) and waits for it."""
    return asyncio.run_coroutine_threadsafe(coro, _startup_anim_loop).result(timeout)


async def _panel_show_media(media: str) -> str | None:
    """Switches the panel to `media` without remembering it as the applied
    media; returns what was showing before, or None if the panel isn't
    there or didn't confirm."""
    panel = _panorama
    if panel is None or not panel.connected:
        return None
    async with _panorama_lock:
        previous = panel.last_media
        ok = await _call_panel(panel.set_media, media)
    return previous if ok else None


def _ambient_source(cfg: StartupAnimation) -> tuple[Path, str | None] | None:
    """(local video, its name on the panel or None) for the ambient show:
    the chosen gallery item, or the custom power-on video by default. The
    TRYX default animations exist only on the panel, never locally, so
    they can't be followed."""
    state = _load_panorama_state()
    item_id = cfg.ambient_source or state.get("boot_gallery_id")
    if not item_id:
        return None
    item = next((i for i in _load_gallery() if i["id"] == item_id), None)
    original = next((GALLERY_DIR / item_id).glob("original.*"), None) if item else None
    if original is None:
        return None
    remote = item.get("remote_name")
    if item_id == state.get("boot_gallery_id"):
        remote = state.get("poweron_media") or remote
    return original, remote


def _play_ambient_show(show: _Show) -> dict | None:
    """The panel plays the chosen video (if on_display and it's on the
    panel) while the rings light up from that same video, decoded locally
    in step with it; then the panel goes back to the applied media. None
    if there's no local video to follow."""
    cfg, rings, stop = show.cfg, show.rings, show.stop
    source = _ambient_source(cfg)
    if source is None:
        log.info("startup animation: ambient needs a gallery video (or a custom power-on video) - "
                 "playing the case spin instead")
        return None
    original, remote = source
    previous = None
    if cfg.ambient_on_display and remote:
        # The panel connects on its own task at launch - give it a moment.
        deadline = time.monotonic() + 10
        while (_panorama is None or not _panorama.connected) and time.monotonic() < deadline and not stop.is_set():
            stop.wait(0.2)
        try:
            previous = _call_on_loop(_panel_show_media(remote), timeout=10)
        except Exception as exc:
            log.warning("startup animation: could not show the video on the panel: %s", exc)
    sampler = AmbientSampler()
    _configure_ambient(sampler)
    sampler.set_source(str(original), original.suffix.lower() in _VIDEO_SUFFIXES,
                       started_at=_panel_media_started_at() if previous is not None else None)
    dirs = _ambient_directions([l for r in rings for l in r["leds"]])
    frames = {}
    start = time.monotonic()
    try:
        while not stop.is_set():
            t = time.monotonic() - start
            fade = min(1.0, t / 0.5)
            for r in rings:
                frames[r["id"]] = [startup_anim.scale(sampler.edge_color(*dirs.get((r["id"], i), (0, -1))), fade)
                                   for i in range(r["count"])]
            show.write(frames)
            if t >= cfg.show_s:
                break
            show.tick()
    finally:
        sampler.stop()
        if previous is not None:
            try:
                _call_on_loop(_panel_show_media(previous), timeout=10)
            except Exception as exc:
                log.warning("startup animation: could not switch the panel back: %s", exc)
    return frames


def _finale_targets():
    """RAM sticks and the motherboard as the effect would drive them -
    [(unit, device, zone index, strip indices bottom-to-top)]."""
    leds_by_unit, _, ram_targets, mobo_target = _resolve_effect_targets()
    targets = []
    for u, device in ram_targets:
        order = [l["index"] for l in sorted(leds_by_unit.get(u["id"], []), key=lambda l: -l["y"])]
        targets.append((u, device, 0, order))
    if mobo_target is not None:
        u, device, zone_index = mobo_target
        targets.append((u, device, zone_index, []))
    for _, device, _, _ in targets:
        openrgb.select_mode(device, "direct")
    return targets


def _play_finale(show: _Show, end: dict) -> tuple[dict, list]:
    """RAM and motherboard join in once the rings are lit: as soon as
    OpenRGB is up (the rings hold meanwhile, at most
    _FINALE_OPENRGB_WAIT_S), the RAM sticks fill from the bottom and the
    board lights up. Returns the end frames of everything lit, and the
    RAM/board targets."""
    rings, stop = show.rings, show.stop
    full = {r["id"]: [show.paint(p) for p in r["places"]] for r in rings}
    deadline = time.monotonic() + _FINALE_OPENRGB_WAIT_S
    while not openrgb.connected and time.monotonic() < deadline and not stop.is_set():
        stop.wait(0.2)
    targets = []
    if openrgb.connected:
        try:
            targets = _finale_targets()
        except Exception as exc:
            log.warning("startup animation: RAM/motherboard unavailable for the finale: %s", exc)
    ring_start = {r["id"]: end[r["id"]] for r in rings}
    finale_color = startup_anim.scale(show.paint(0.0), show.level)
    start = time.monotonic()
    while not stop.is_set():
        k = min(1.0, (time.monotonic() - start) / _FINALE_S)
        show.write({r["id"]: startup_anim.blend(ring_start[r["id"]], full[r["id"]], k) for r in rings})
        for u, device, zone_index, order in targets:
            count = len(device.zones[zone_index].leds)
            if order and len(order) == count:
                lit = k * count
                frame = [(0, 0, 0)] * count
                for rank, index in enumerate(order):
                    frame[index] = startup_anim.scale(finale_color, max(0.0, min(1.0, lit - rank)))
            else:
                frame = [startup_anim.scale(finale_color, k)] * count
            try:
                openrgb.set_zone_colors_by_index(device, zone_index, frame)
            except Exception as exc:
                log.debug("startup animation: finale write failed for %s: %s", u["id"], exc)
        if k >= 1:
            break
        show.tick()
    ends = dict(full)
    for u, device, zone_index, _ in targets:
        ends[u["id"]] = [finale_color] * len(device.zones[zone_index].leds)
    return ends, targets


def _run_startup_animation(cfg: StartupAnimation, fade_to_saved_colours: bool,
                           stop: threading.Event):
    """Plays the animation (blocking, a few seconds), optionally the
    finale, then - when no effect follows - fades into the saved static
    colours. A following effect does its own fade from where this left
    everything (_startup_anim_end, see _run_position_effect)."""
    global _startup_anim_end
    show = _Show(cfg, stop)
    if not show.rings:
        return
    try:
        end = None
        if cfg.style == "ambient":
            end = _play_ambient_show(show)
        if end is None:
            end = _play_spin(show) if cfg.style == "spin" else _play_case_spin(show)
        targets = []
        if cfg.finale and not stop.is_set():
            end, targets = _play_finale(show, end)
        # what was really shown: rings went out at the chosen brightness
        ring_ids = {r["id"] for r in show.rings}
        shown = {uid: [startup_anim.scale(c, show.level) for c in f] if uid in ring_ids else f
                 for uid, f in end.items()}
        _startup_anim_end = shown
        if stop.is_set() or not fade_to_saved_colours:
            return
        saved = _load_rgb_state()
        hub_saved = saved.get("hub", {})
        ring_targets = {r["id"]: [_hex_to_rgb(hub_saved[r["channel"]]["color"]) if r["channel"] in hub_saved
                                  else (0, 0, 0)] * r["count"] for r in show.rings}

        def saved_colour(u):
            if u["kind"] == "mobo":
                entry = saved.get("mobo", {}).get("0")
            else:
                entry = saved.get("ram", {}).get(u["id"].rsplit("-", 1)[-1])
            return _hex_to_rgb(entry["color"]) if entry else (0, 0, 0)

        show.level = 1.0  # `shown` already carries the brightness
        fade_start = time.monotonic()
        while not stop.is_set():
            k = (time.monotonic() - fade_start) / cfg.fade_s if cfg.fade_s > 0 else 1.0
            show.write({r["id"]: startup_anim.blend(shown[r["id"]], ring_targets[r["id"]], k) for r in show.rings})
            for u, device, zone_index, _ in targets:
                count = len(device.zones[zone_index].leds)
                try:
                    openrgb.set_zone_colors_by_index(
                        device, zone_index, startup_anim.blend(shown[u["id"]], [saved_colour(u)] * count, k))
                except Exception as exc:
                    log.debug("startup animation: fade write failed for %s: %s", u["id"], exc)
            if k >= 1:
                break
            show.tick()
    except Exception as exc:
        log.warning("startup animation stopped: %s", exc)


def _start_startup_animation():
    """Called once, right after the hub connected at launch. Runs as the
    current "effect", so Stop or anything else started meanwhile ends it
    like any effect - it used to have a stop flag of its own nothing
    could reach, and kept writing alongside whatever was started next."""
    global _startup_anim_task, _startup_anim_loop, _startup_anim_stop, _effect_task, _effect_status
    if not (HUB_DIRECT and hub.connected):
        return
    cfg = _load_startup_animation()
    if not cfg.enabled:
        return
    _startup_anim_loop = asyncio.get_running_loop()
    stop = _new_effect_stop()
    _startup_anim_stop = stop
    _effect_status = {"running": True, "kind": "startup", "error": None}
    _effect_task = _start_effect_thread(_run_startup_animation_once, cfg,
                                        _load_effect_state() is None, stop)
    _startup_anim_task = _effect_task


def _run_startup_animation_once(cfg: StartupAnimation, fade_to_saved_colours: bool,
                                stop: threading.Event):
    _run_startup_animation(cfg, fade_to_saved_colours, stop)
    _end_effect(stop)


_startup_anim_stop: threading.Event | None = None


def _startup_anim_taken_over() -> bool:
    """Whether something else has the LEDs since the launch animation
    started (Stop, an effect, a test pattern, a static colour) - then
    nothing may be resumed or re-applied over it."""
    stop = _startup_anim_stop
    return stop is not None and (stop.is_set() or stop is not _effect_stop)


async def _wait_startup_animation() -> bool:
    """Waits for a running startup animation to finish; whether one ran
    to its end without anything taking over."""
    task = _startup_anim_task
    if task is None:
        return False
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=60)
    except Exception as exc:
        log.warning("startup animation did not finish cleanly: %s", exc)
    return _startup_anim_end is not None and not _startup_anim_taken_over()


async def _ensure_on_panel(item_id: str) -> str | None:
    """The panel-side name of a gallery item, uploading it once if it was
    never uploaded - so the ambient show can play it at launch without an
    encode + upload on the way. None if the panel isn't there."""
    item = next((i for i in _load_gallery() if i["id"] == item_id), None)
    if item is None:
        return None
    if item.get("remote_name"):
        return item["remote_name"]
    panel = await get_panorama()
    original = next((GALLERY_DIR / item_id).glob("original.*"), None)
    if panel is None or not panel.connected or original is None:
        return None
    container = await asyncio.to_thread(build_encoded_media_container, str(original))
    if container is None:
        return None
    async with _panorama_lock:
        remote_name = await _call_panel(panel.upload_media, str(original), None, 2.0, 15.0, container)
    if remote_name:
        _remember_gallery_remote(item_id, remote_name)
    return remote_name


def _remember_gallery_remote(item_id: str, remote_name: str):
    items = _load_gallery()
    for item in items:
        if item["id"] == item_id:
            item["remote_name"] = remote_name
    _save_gallery(items)


_openrgb_bringup_done = asyncio.Event()


async def _bring_up_openrgb():
    """OpenRGB in the background at launch (see lifespan): connect, or
    launch it and wait for it - however long a UAC prompt stays open -
    then what needs it: the quick scan (it reads RAM and board through
    OpenRGB; started after 45s regardless, so fans get mapped even while
    OpenRGB is still away) and the saved RAM/board colours (hub colours
    too when the hub goes through OpenRGB)."""
    connect = asyncio.create_task(_connect_openrgb_with_recovery())
    done, _ = await asyncio.wait({connect}, timeout=45)
    if hub.connected or openrgb.connected:
        scan.start("quick")
    try:
        ok = await asyncio.wait_for(connect, timeout=600) if not done else connect.result()
    except Exception as exc:
        log.warning("OpenRGB did not come up: %s", exc)
        ok = False
    _openrgb_bringup_done.set()
    if not ok:
        log.warning("OpenRGB still not connected after recovery attempts: %s", openrgb.last_error)
        return
    await _reapply_rgb_state_logged(boards_only=HUB_DIRECT)
    if not HUB_DIRECT:
        asyncio.create_task(_reapply_hub_rgb_state_delayed())


_sensor_helper_wanted_at = 0.0


def _want_sensor_helper():
    """Something needs temperatures now (the web UI asked, or a fan curve
    on CPU temperature): start SensorHelper in the background if it isn't
    answering - at most once a minute, so a declined UAC prompt isn't
    asked again on every poll."""
    global _sensor_helper_wanted_at
    if time.monotonic() - _sensor_helper_wanted_at < 60:
        return
    _sensor_helper_wanted_at = time.monotonic()
    asyncio.create_task(_bring_up_sensor_helper())


def _curves_need_cpu_temp() -> bool:
    try:
        curves = json.loads(CURVE_FILE.read_text(encoding="utf-8")).get("curves") or []
    except Exception:
        return False
    return any(c.get("source") in ("cpu", "cpu_gpu") and c.get("channels") for c in curves)


async def _bring_up_sensor_helper():
    """SensorHelper.exe (CPU/motherboard/RAM temperature - see
    app/sensors.py) in the background at launch, only if a URL is
    configured and nothing answers there yet - never a second instance."""
    lhm_url = CONFIG.get("sensors", {}).get("librehardwaremonitor_url")
    if lhm_url and not await asyncio.to_thread(_lhm_reachable, lhm_url):
        await asyncio.to_thread(_launch_vendor_sensor_helper)


async def _reapply_rgb_state_logged(**kwargs):
    try:
        await _reapply_rgb_state(**kwargs)
    except Exception as exc:
        log.warning("could not reapply saved RGB colours: %s", exc)


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

    if not await _hub_call(hub.connect):
        log.warning("hub not connected: %s", hub.last_error)
    _start_startup_animation()
    await _sync_openrgb_hub_detection()

    # OpenRGB and SensorHelper come up in the background: launching either
    # can mean a UAC prompt, and waiting for it here held the whole server
    # back - fans, display, effects - for as long as the prompt stayed
    # unanswered (2.5 minutes after a real boot). What needs OpenRGB
    # catches up once it answers (_bring_up_openrgb); a running effect
    # picks RAM and board up on its own.
    asyncio.create_task(_bring_up_openrgb())
    # SensorHelper (temperatures) only right away if a fan curve runs on
    # CPU temperature; otherwise once something asks for temperatures (the
    # web UI) - most of the time nothing does, and it's ~30MB plus, without
    # the prompt-free setup, a UAC prompt at every login.
    if _curves_need_cpu_temp():
        asyncio.create_task(_bring_up_sensor_helper())

    try:
        await panorama_task
    except Exception as exc:
        log.warning("panorama connect task failed: %s", exc)
    _keepalive_task = asyncio.create_task(_keepalive_loop())
    _curve_task = asyncio.create_task(_curve_loop())

    if HUB_DIRECT and hub.connected:
        # The hub's own saved colours don't need OpenRGB.
        asyncio.create_task(_reapply_rgb_state_logged(hub_only=True))

    # Resume whatever position effect was running when this process last
    # stopped - see _save_effect_state's docstring: it's host-driven, so
    # it goes dark on process exit with nothing to bring it back unless
    # this does. persist=False - don't touch the saved file just for
    # replaying it, and don't wipe it if this particular attempt fails.
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

    if not await _hub_call(hub.connect):
        log.warning("hub not connected: %s", hub.last_error)
    _start_startup_animation()

    saved_effect = _load_effect_state()
    rgb_state = _load_rgb_state()
    if saved_effect is not None or rgb_state:
        await _sync_openrgb_hub_detection()
        if not await asyncio.to_thread(openrgb.connect):
            log.warning("OpenRGB not connected: %s", openrgb.last_error)
            if await asyncio.to_thread(_launch_vendor_openrgb):
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
            await asyncio.to_thread(_launch_vendor_sensor_helper)

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


# -- local-only request guard ------------------------------------------------
#
# Binding to 127.0.0.1 keeps other machines out, but not other *websites*
# open in the same browser: any page can fire a cross-origin POST at
# http://127.0.0.1:<port>/... without the user noticing. Forms and
# multipart uploads never trigger a CORS preflight, so e.g. a hostile page
# could upload a gallery item whose filename carries markup (stored in the
# gallery manifest, rendered by the Display page), stop OpenRGB/
# SensorHelper, quit shyyctl mid fan-curve, or start a full scan - and via
# DNS rebinding (attacker's hostname re-pointed at 127.0.0.1) it could even
# read responses and drive /api/system/update-apply, which downloads and
# runs an installer. Two cheap checks close all of that: the Host header
# must name this machine (defeats rebinding), and state-changing requests
# must come from this app's own origin (defeats cross-site posts). Plain
# non-browser clients (curl, the single-instance probe below) send no
# Origin header at all and are unaffected.

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _allowed_hostnames() -> set[str]:
    names = {"127.0.0.1", "localhost", "::1"}
    configured = str(CONFIG.get("server", {}).get("host", "")).strip("[]").lower()
    if configured and configured not in ("0.0.0.0", "::"):
        names.add(configured)
    return names


_ALLOWED_HOSTNAMES = _allowed_hostnames()


def _hostname_of(netloc: str) -> str | None:
    try:
        return urllib.parse.urlsplit("//" + netloc).hostname
    except ValueError:
        return None


def _request_rejection(method: str, headers: dict[str, str]) -> str | None:
    host = _hostname_of(headers.get("host", ""))
    if host is not None and host not in _ALLOWED_HOSTNAMES:
        return f"unexpected Host header {headers.get('host')!r}"
    if method in _SAFE_METHODS:
        return None
    if headers.get("sec-fetch-site") == "cross-site":
        return "cross-site request refused"
    origin = headers.get("origin")
    if origin is not None:
        parts = urllib.parse.urlsplit(origin)
        if parts.scheme not in ("http", "https") or parts.hostname not in _ALLOWED_HOSTNAMES:
            return f"request from origin {origin!r} refused"
    return None


class _LocalOnlyMiddleware:
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            reason = _request_rejection(scope["method"], headers)
            if reason:
                log.warning("refused %s %s: %s", scope["method"], scope.get("path"), reason)
                await JSONResponse({"detail": reason}, status_code=403)(scope, receive, send)
                return
        await self.inner(scope, receive, send)


app.add_middleware(_LocalOnlyMiddleware)


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
            "control": "direct" if HUB_DIRECT else "openrgb",
        },
        "openrgb": {"connected": openrgb.connected, "error": openrgb.last_error},
        "panorama": {"connected": panel is not None and panel.connected},
    }


@app.get("/api/sensors")
async def sensors():
    """CPU/RAM load, GPU load/mem/temp, CPU temperature - see
    app/sensors.py for exactly where each one comes from and why each
    degrades to None independently rather than failing the whole call.
    The first ask starts SensorHelper if it isn't running (see
    _want_sensor_helper) - it isn't started at launch any more unless a
    fan curve needs it."""
    snap = await asyncio.to_thread(sensors_mod.snapshot, CONFIG)
    if snap.get("cpu_temp_c") is None:
        _want_sensor_helper()
    return snap


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
    hub_ok = await _hub_call(hub.connect)
    openrgb_ok = await asyncio.to_thread(openrgb.connect)
    async with _panorama_lock:
        _drop_panorama(_panorama)
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


@app.get("/api/system/elevated-helpers")
async def elevated_helpers_status():
    return await asyncio.to_thread(_elevated_helpers_status)


@app.post("/api/system/elevated-helpers/{action}")
async def elevated_helpers_action(action: str):
    """Set up (one UAC prompt) or remove starting OpenRGB and SensorHelper
    without a prompt - see tools/elevated_helpers.ps1. Setting up restarts
    both from their protected copies right away; removing starts them the
    old way again, with a prompt."""
    if action not in ("install", "remove"):
        raise HTTPException(404, "unknown action")
    if sys.platform != "win32":
        raise HTTPException(400, "only on Windows")
    result = await asyncio.to_thread(_run_elevated_helpers_script, action)
    if action == "remove" and result.get("ok"):
        asyncio.create_task(asyncio.to_thread(_start_openrgb_if_needed))
        asyncio.create_task(_bring_up_sensor_helper())
    return {**result, **await asyncio.to_thread(_elevated_helpers_status)}


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
    ("startup_animation.json", STARTUP_ANIM_FILE),
    ("ambient_frame.json", AMBIENT_FRAME_FILE),
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
    reported as 404 rather than an empty/missing file download.

    Reads the file's bytes directly (LOG_FILE.read_bytes()) rather than
    handing FileResponse the path to open a second handle itself - this
    file is also _log_file, held open for append for the app's entire
    lifetime as sys.stdout/sys.stderr, so it's still being written to
    while this request is in flight. flush() first so a line just
    written (e.g. logging this very request) isn't missing from what
    gets exported, and any read failure is caught and reported with
    real detail instead of surfacing as an opaque 500 the frontend can't
    show a useful message for - the whole point of this endpoint is
    giving a human something to send when something else already went
    wrong; failing silently here defeats that."""
    if not LOG_FILE.is_file():
        raise HTTPException(404, "no log file yet - only written when running as the installed app")
    try:
        if _log_file is not None:
            _log_file.flush()
        log_bytes = LOG_FILE.read_bytes()
        if not LOG_FILE_OLD.is_file():
            return Response(
                log_bytes, media_type="text/plain",
                headers={"Content-Disposition": 'attachment; filename="shyyctl.log"'})
        old_bytes = LOG_FILE_OLD.read_bytes()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("shyyctl.log", log_bytes)
            zf.writestr("shyyctl.log.old", old_bytes)
        return Response(
            buf.getvalue(), media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="shyyctl-logs.zip"'})
    except OSError as exc:
        log.warning("export_log: could not read log file(s): %s", exc)
        raise HTTPException(500, f"could not read the log file: {exc}")


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


def _is_release_asset_url(url: str) -> bool:
    """This endpoint downloads and silently runs whatever it's pointed at,
    so it only ever accepts what update-check itself hands out: an .exe
    attached to a release of UPDATE_REPO on github.com. Case-insensitive
    on owner/repo (the API answers with the repo's own casing, e.g.
    "shyyCTL"), no dot segments or percent-escapes that could walk the
    path into another repository."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    path = parts.path
    return (parts.scheme == "https" and parts.netloc.lower() == "github.com"
            and not parts.query and not parts.fragment
            and "%" not in path and "/../" not in path and "/./" not in path
            and path.lower().startswith(f"/{UPDATE_REPO.lower()}/releases/download/")
            and path.lower().endswith(".exe"))


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
    if not _is_release_asset_url(body.download_url):
        raise HTTPException(400, "download_url must be an .exe asset of this project's own GitHub releases")
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
    readings = await _hub_call(hub.status)
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
    _curve_written.pop(channel, None)
    try:
        applied = await _hub_call(hub.set_fan, channel, body.duty)
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
    scan and the RGB effects (/api/scan/status, /api/rgb/effects/status).
    Asking for it is also what keeps _curve_loop reading temperatures for
    curves no fan follows yet."""
    global _curve_status_read_at
    _curve_status_read_at = time.monotonic()
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


# channel -> (duty, monotonic time) of the last duty _curve_loop actually
# wrote; see the write-skipping in _curve_loop. Any other writer of a fan's
# duty (manual slider, scan) clears the entry so the curve re-asserts.
_curve_written: dict[str, tuple[int, float]] = {}

# Ryzen's Tctl (the 9950X3D's "Core (Tctl/Tdie)" this curve runs on) jumps
# 10-20°C for a second or two on any short burst of load - opening a
# program, a browser tab - and drops straight back. Fed into the curve
# unfiltered, every such blip spun the fans up and back down again within
# a few seconds, audibly, and cost a liquidctl write each way. The curve
# now follows a smoothed temperature: rises are taken in at 40% per 4s
# tick (a sustained load is fully reflected within ~15s, which a water-
# cooled CPU's thermal mass easily covers), falls at 20%, and fans spin
# down by at most _CURVE_STEP_DOWN percent per tick. At _CURVE_PANIC_C or
# above the raw reading is used directly - real heat is never smoothed away.
_CURVE_RISE = 0.4
_CURVE_FALL = 0.2
_CURVE_PANIC_C = 90.0
_CURVE_STEP_DOWN = 5
_curve_temp_smooth: dict[str, float] = {}


def _smoothed_temp(source: str, raw: float | None) -> float | None:
    if raw is None:
        _curve_temp_smooth.pop(source, None)
        return None
    prev = _curve_temp_smooth.get(source)
    if prev is None or raw >= _CURVE_PANIC_C:
        value = raw
    else:
        value = prev + (raw - prev) * (_CURVE_RISE if raw > prev else _CURVE_FALL)
    _curve_temp_smooth[source] = value
    return value


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
        # Temperatures only when they matter: a fan following a curve, or
        # someone looking at the curves (the Fans page polls the status).
        # Otherwise every tick cost an nvidia-smi process (~40ms) and a
        # SensorHelper round trip - about 1% of a core around the clock,
        # as much as the rest of shyyctl, for curves nothing followed.
        live_fans = hub.channels.get("fan", []) if hub.connected else []
        followed = [c for c in curves if any(ch in live_fans for ch in c.get("channels", []))]
        watched = time.monotonic() - _curve_status_read_at < 15
        if not followed and not watched:
            _curve_status = {"curves": [{"id": c.get("id"), "temp_c": None, "temp_raw_c": None, "duty": None}
                                        for c in curves]}
            await asyncio.sleep(4)
            continue
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
        if cpu_t is None and any(c.get("source") in ("cpu", "cpu_gpu") for c in (followed or curves)):
            _want_sensor_helper()
        raw_temps = {"cpu": cpu_t, "gpu": gpu_t, "cpu_gpu": _combine_temp(cpu_t, gpu_t)}
        temps = {source: _smoothed_temp(source, t) for source, t in raw_temps.items()}
        fan_channels = hub.channels.get("fan", []) if hub.connected else []
        # A running scan drives fans itself (0% to find the start duty,
        # 100% for max rpm) - writing curve duties in between used to
        # undo those probes within 4s and skew what the scan measured.
        # Once it's done, every curve channel gets re-asserted, since the
        # scan leaves them at its own restore duty.
        scanning = scan.running
        if scanning:
            _curve_written.clear()
        now = time.monotonic()
        for curve in curves:
            temp_c = temps.get(curve.get("source"))
            channels = [c for c in curve.get("channels", []) if c in fan_channels and c not in scan.absent]
            duty = None
            if temp_c is not None and channels:
                duty = _curve_duty_for_temp(
                    curve.get("points") or [], CONFIG["curve"].get("min_duty", 20), temp_c)
                if hub.connected and not scanning:
                    for channel in channels:
                        # The hub keeps a duty until told otherwise, so an
                        # unchanged value isn't re-sent every 4s - every
                        # write is a whole liquidctl process plus HID
                        # traffic. Still re-asserted every minute in case
                        # something else (a hub reset, another tool)
                        # changed it behind our back. A 1% wobble isn't
                        # worth a write either.
                        last = _curve_written.get(channel)
                        target = duty
                        if last is not None and target < last[0]:
                            target = max(target, last[0] - _CURVE_STEP_DOWN)
                        if last is not None and abs(target - last[0]) < 2 and now - last[1] < 60:
                            continue
                        try:
                            await _hub_call(hub.set_fan, channel, target)
                            _curve_written[channel] = (target, now)
                        except Exception as exc:
                            _curve_written.pop(channel, None)
                            log.warning("curve %s: could not set %s to %s%%: %s",
                                       curve.get("id"), channel, target, exc)
            raw_t = raw_temps.get(curve.get("source"))
            statuses.append({"id": curve.get("id"),
                             "temp_c": round(temp_c, 1) if temp_c is not None else None,
                             "temp_raw_c": raw_t, "duty": duty})
        _curve_status = {"curves": statuses}
        await asyncio.sleep(4)


# -- RGB: motherboard / RAM / hub -------------------------------------------

def _open_hub_raw(timeout: float = 5.0) -> OpenRgbRaw:
    return OpenRgbRaw(host=CONFIG["openrgb"]["host"], port=CONFIG["openrgb"]["port"], timeout=timeout)


MAX_HUB_LEDS = 40  # per channel - Hue 2 protocol limit (app/hub.py)


class HubLedsUnavailable(Exception):
    """A hub-LED action that can't happen: no hub (status 503/404), or a
    channel with no known LED count (400)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _hub_channel_led_count(channel: str) -> int | None:
    """Direct mode: LEDs on a hub channel. The hub itself never reports a
    count (see scan.py) - the placed unit's count (from a scan or a model
    picked in Setup) wins, else the model behind the channel's accessory ID
    (geometry.py's table)."""
    for unit in layout.load().get("units", []):
        if unit.get("kind") == "fan" and unit.get("led_channel") == channel and unit.get("leds"):
            return int(unit["leds"])
    accessory = hub.known_accessory_ids().get(channel) if HUB_DIRECT else None
    model = geometry.identify_by_accessory_id(accessory) if accessory is not None else None
    return model.leds if model else None


class _DirectHubLeds:
    """Hub LEDs written by shyyctl itself (DirectHub.set_leds)."""

    def channel_leds(self, channel: str) -> int | None:
        return _hub_channel_led_count(channel) if channel in hub.channels.get("led", []) else None

    def write(self, channel: str, colors: list[tuple]) -> None:
        hub.set_leds(channel, colors)

    def fill(self, channel: str, rgb: tuple, persistent: bool = True) -> None:
        # A direct colour stays on the hub until something else is sent,
        # and every saved colour is re-sent on startup anyway (see
        # _reapply_rgb_state) - "persistent" has nothing extra to do here.
        count = self.channel_leds(channel)
        if not count:
            raise HubLedsUnavailable(f"channel '{channel}' has no known LED count yet - run a scan or pick its model in Setup")
        hub.set_leds(channel, [rgb] * count)

    def reconnect(self) -> None:
        # DirectHub reopens a lost connection on its own (on the next
        # write) - this only reports whether it's back yet.
        if not hub.ensure_connected():
            raise HubLedsUnavailable(f"hub not connected: {hub.last_error}", 503)


class _OpenRgbHubLeds:
    """Hub LEDs via OpenRGB's protocol-5 SDK (hub.control: openrgb)."""

    def __init__(self, raw: OpenRgbRaw):
        self.raw = raw
        self.ctrl = raw.find_controller("NZXT")
        self._mode = None

    def _select(self, mode: str):
        if self._mode != mode:
            self.raw.select_mode(self.ctrl, mode)
            self._mode = mode

    def channel_leds(self, channel: str) -> int | None:
        zone = self.raw.zone_for_channel(self.ctrl, channel)
        return zone.leds_count if zone is not None and zone.leds_count else None

    def write(self, channel: str, colors: list[tuple]) -> None:
        zone = self.raw.zone_for_channel(self.ctrl, channel)
        if zone is None:
            raise HubLedsUnavailable(f"no zone on {self.ctrl.name} matches channel '{channel}'")
        self._select("direct")
        self.raw.set_zone_colors(self.ctrl.index, zone.index, colors)

    def fill(self, channel: str, rgb: tuple, persistent: bool = True) -> None:
        zone = self.raw.zone_for_channel(self.ctrl, channel)
        if zone is None or not zone.leds_count:
            raise HubLedsUnavailable(f"channel '{channel}' has no LEDs sized in OpenRGB yet")
        self._select("static" if persistent else "direct")
        self.raw.set_zone_color(self.ctrl.index, zone, rgb)

    def reconnect(self) -> None:
        self.raw.close()
        self.raw.connect()
        self.ctrl = self.raw.find_controller("NZXT")
        self._mode = None
        if self.ctrl is None:
            raise HubLedsUnavailable("no hub RGB device found", 404)


@contextlib.contextmanager
def _hub_leds(timeout: float = 5.0):
    """The one way every hub-LED feature (static colours, per-LED writes,
    effects, the LED-order test, Identify) gets at the hub's LEDs, whichever
    path hub.control selects. Raises HubLedsUnavailable if there's no hub."""
    if HUB_DIRECT:
        if not hub.connected and not hub.connect():
            raise HubLedsUnavailable(f"hub not connected: {hub.last_error}", 503)
        yield _DirectHubLeds()
        return
    with _open_hub_raw(timeout=timeout) as raw:
        leds = _OpenRgbHubLeds(raw)
        if leds.ctrl is None:
            raise HubLedsUnavailable("no hub RGB device found", 404)
        yield leds


def _hub_rgb_entry() -> dict | None:
    if not HUB_DIRECT:
        return _hub_via_raw()
    if not hub.connected:
        return None
    zones = [{"name": f"LED {ch[3:]}", "leds": _hub_channel_led_count(ch) or 0}
             for ch in hub.channels.get("led", [])]
    return {"name": hub.description or "NZXT Control Hub", "type": "LEDSTRIP",
            "leds": sum(z["leds"] for z in zones), "modes": ["Direct"], "zones": zones,
            "driven_by": "shyyctl"}


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
    if openrgb.connected or await asyncio.to_thread(openrgb.connect):
        devices = await asyncio.to_thread(openrgb.describe_all)
    elif HUB_DIRECT and hub.connected:
        devices = []  # RAM/board unavailable, the hub is still worth listing
    else:
        raise HTTPException(503, f"OpenRGB not connected: {openrgb.last_error}")
    hub_entry = await asyncio.to_thread(_hub_rgb_entry)
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


async def _take_over_leds():
    """A static colour about to be set: whatever effect, test pattern or
    startup animation is running would paint straight over it - or both
    would take turns, which flickers - so it stops first, and isn't
    resumed at the next launch either."""
    if _effect_status.get("running"):
        await _stop_effect()
        _clear_effect_state()


@app.post("/api/rgb/mobo/color")
async def set_mobo_color(body: ColorRequest):
    await _take_over_leds()
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
    await _take_over_leds()
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
    await _take_over_leds()
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
    await _take_over_leds()
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
    await _take_over_leds()
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
    rescan.

    Direct mode (hub.control: direct) has no zones to size - the count
    comes from the Setup layout / the fan model (_hub_channel_led_count) -
    so this only checks the number is one the hub can take."""
    if HUB_DIRECT:
        if not 0 < body.leds <= MAX_HUB_LEDS:
            raise HTTPException(400, f"a hub channel takes 1-{MAX_HUB_LEDS} LEDs")
        return {"ok": True}

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


async def _run_hub_leds(fn):
    """Runs a blocking hub-LED action, mapping its failures to HTTP errors
    the same way for every hub-LED endpoint."""
    try:
        await asyncio.to_thread(fn)
    except HubLedsUnavailable as exc:
        raise HTTPException(exc.status, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(503, f"hub LEDs unavailable: {exc}")


@app.post("/api/rgb/hub/{channel}/color")
async def set_hub_channel_color(channel: str, body: ColorRequest):
    """liquidctl LED control is confirmed broken for the 360mm units - hub
    RGB goes through OpenRGB's protocol-5 client (app/openrgb_raw.py),
    same reasoning as the resize endpoint above. The channel must already
    be sized (via a scan or the resize endpoint) - a 0-LED zone means
    nothing is known to be attached there, and is refused rather than
    silently falling back to a device-wide write."""
    await _take_over_leds()
    rgb = _hex_to_rgb(body.color)

    def _do():
        with _hub_leds() as leds:
            leds.fill(channel, rgb, body.persistent)

    await _run_hub_leds(_do)
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
    await _take_over_leds()
    rgb_list = [_hex_to_rgb(c) for c in body.colors]

    def _do():
        with _hub_leds() as leds:
            count = leds.channel_leds(channel)
            if count != len(rgb_list):
                raise ValueError(f"channel '{channel}' has {count or 0} LEDs, "
                                 f"but {len(rgb_list)} colours were sent")
            leds.write(channel, rgb_list)

    await _run_hub_leds(_do)
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


def _fit_frame(frame: list, count: int) -> list:
    """A frame resampled to the LED count of the zone it is written to.
    Layouts scanned before scan.py counted only the motherboard's real zone
    hold the whole device's LED count (27 on the X870-F, 3 of them real),
    and OpenRGB rejects a zone write whose length doesn't match - the board
    stayed dark through every effect."""
    if len(frame) == count or not frame:
        return frame
    return [frame[min(len(frame) - 1, int((i + 0.5) * len(frame) / count))] for i in range(count)]


# Layout x/y are fractions of the canvas, which is wider than tall (see
# web/index.html's CANVAS_W/CANVAS_H less its padding) - a step in x is
# this many times a step in y on screen.
_CANVAS_ASPECT = (900 - 48) / (600 - 48)


class AmbientFrame(BaseModel):
    """Where in the picture the ambient light takes its edge (see
    AmbientSampler.configure), in percent: how much is cut off each side,
    and how deep inwards from that frame the colour is averaged. offset_ms
    moves the light ahead of the panel's picture (negative: behind it)."""
    left: float = 0.0
    top: float = 0.0
    right: float = 0.0
    bottom: float = 0.0
    depth: float = 10.0
    offset_ms: int = 0


def _checked_ambient_frame(body: AmbientFrame) -> AmbientFrame:
    sides = {k: min(max(float(getattr(body, k)), 0.0), 45.0) for k in ("left", "top", "right", "bottom")}
    if sides["left"] + sides["right"] > 90 or sides["top"] + sides["bottom"] > 90:
        raise HTTPException(400, "the frame leaves nothing of the picture")
    return body.model_copy(update={**sides, "depth": min(max(float(body.depth), 2.0), 50.0),
                                   "offset_ms": min(max(int(body.offset_ms), -3000), 3000)})


def _load_ambient_frame() -> AmbientFrame:
    try:
        return _checked_ambient_frame(
            AmbientFrame.model_validate_json(AMBIENT_FRAME_FILE.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return AmbientFrame()
    except Exception as exc:
        log.warning("could not read %s (%s) - taking the ambient light from the whole picture",
                    AMBIENT_FRAME_FILE.name, exc)
        return AmbientFrame()


def _configure_ambient(sampler: AmbientSampler, frame: AmbientFrame | None = None):
    frame = frame or _load_ambient_frame()
    sampler.configure((frame.left / 100, frame.top / 100, frame.right / 100, frame.bottom / 100),
                      frame.depth / 100, frame.offset_ms / 1000)


_configure_ambient(ambient_sampler)


def _panel_media_started_at() -> float | None:
    """When the panel started what it's showing (see app/panorama.py's
    media_started_at) - the ambient timeline's zero."""
    panel = _panorama
    return panel.media_started_at if panel is not None and panel.connected else None


@app.get("/api/rgb/ambient-frame")
async def get_ambient_frame():
    return _load_ambient_frame().model_dump()


@app.post("/api/rgb/ambient-frame")
async def set_ambient_frame(body: AmbientFrame):
    """Saves where the ambient light takes its colours and applies it to a
    running ambient effect straight away."""
    frame = _checked_ambient_frame(body)
    AMBIENT_FRAME_FILE.write_text(frame.model_dump_json(), encoding="utf-8")
    _configure_ambient(ambient_sampler, frame)
    return frame.model_dump()


def _ambient_directions(leds: list[dict]) -> dict[tuple, tuple[float, float]]:
    """(unit id, LED index) -> the direction from the Panorama's centre
    to that LED, in the panel's own frame (its rotation in the layout
    taken out, y downwards) - what AmbientSampler.edge_color() takes.
    Without the panel placed, from the middle of all the LEDs."""
    display = next((u for u in layout.load().get("units", []) if u.get("kind") == "display"), None)
    if display is not None:
        cx, cy = display.get("x", 0.5), display.get("y", 0.5)
        rot = math.radians(display.get("rotation", 0) or 0)
    elif leds:
        cx = sum(l["x"] for l in leds) / len(leds)
        cy = sum(l["y"] for l in leds) / len(leds)
        rot = 0.0
    else:
        return {}
    cos_r, sin_r = math.cos(-rot), math.sin(-rot)
    out = {}
    for l in leds:
        dx, dy = (l["x"] - cx) * _CANVAS_ASPECT, l["y"] - cy
        out[(l["unit"], l["index"])] = (dx * cos_r - dy * sin_r, dx * sin_r + dy * cos_r)
    return out


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
    # Only with OpenRGB actually connected: asking a missing one retries
    # its way through several seconds per device type, and the effect (fans
    # included) would sit dark meanwhile. A running effect picks RAM and
    # board up once OpenRGB is there (see _run_position_effect).
    if ram_units and openrgb.connected:
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
    if mobo_units and openrgb.connected:
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


def _end_effect(stop: threading.Event, error: str | None = None):
    """Marks the effect finished - unless a newer run has already replaced
    this one (see _new_effect_stop): an old run exiting late must not
    report the new one as stopped or stop its ambient sampler."""
    global _effect_status
    if stop is not _effect_stop:
        return
    ambient_sampler.stop()
    _effect_status = {"running": False, "kind": None, "error": error}


def _run_position_effect(direction: str, palette: str, colors: list[tuple],
                         period_s: float, moving: bool, shape: str = "wave",
                         led_selection: dict[str, set[int]] | None = None,
                         stop: threading.Event | None = None,
                         fade_in: tuple[float, tuple[int, int, int] | None, set] | None = None):
    """Runs until `stop` is set, on its own thread (see
    _start_effect_thread) - one raw hub connection held for the whole effect
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
    if stop is None:
        stop = _effect_stop
    leds_by_unit, fan_units, ram_targets, mobo_target = _resolve_effect_targets()

    # Every placed unit counts for the effect's geometry, whether its
    # device is reachable yet or not - RAM and board can join later, and
    # the effect shouldn't shift on screen when they do.
    placed = layout.load().get("units", [])
    wants_ram = any(u.get("kind") == "ram" for u in placed)
    wants_mobo = any(u.get("kind") == "mobo" for u in placed)
    driven_ids = ({u["id"] for u in fan_units}
                  | {u["id"] for u in placed if u.get("kind") in ("ram", "mobo")})
    all_leds = [led for uid in driven_ids for led in leds_by_unit.get(uid, [])]
    if led_selection is not None:
        all_leds = [led for led in all_leds if led["index"] in led_selection.get(led["unit"], ())]
    if not all_leds:
        _end_effect(stop, "no LEDs selected" if led_selection is not None
                    else "no units placed in Setup yet")
        return
    ambient_dirs = _ambient_directions([l for ls in leds_by_unit.values() for l in ls]) if shape == "ambient" else {}
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
            # human picked: the picture's edge extended outwards from the
            # panel, each LED taking the edge colour in its direction.
            colors_out = [ambient_sampler.edge_color(*ambient_dirs.get((led["unit"], led["index"]), (0, -1)))
                          for led in unit_leds]

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

    if fade_in is not None:
        # (seconds, {unit id: colours it was left showing}) - after the
        # startup animation the effect blends in over the first few
        # seconds from wherever each unit was left, instead of cutting in;
        # units the animation never touched fade up from dark.
        fade_s, ends = fade_in
        effect_frame_colors = frame_colors
        fade_started = [None]

        def frame_colors(u):
            unit_leds, colors_out = effect_frame_colors(u)
            if fade_started[0] is None:
                fade_started[0] = time.monotonic()
            k = (time.monotonic() - fade_started[0]) / fade_s if fade_s > 0 else 1.0
            if k < 1:
                base = ends.get(u["id"]) or [(0, 0, 0)]
                if len(base) != len(colors_out):
                    base = [base[0]] * len(colors_out)  # e.g. the board: layout count vs real zone
                colors_out = startup_anim.blend(base, colors_out, k)
            return unit_leds, colors_out

    def refresh_ambient_source():
        media = _current_panorama_media_path()
        if media is not None:
            ambient_sampler.set_source(*media, started_at=_panel_media_started_at())
        else:
            ambient_sampler.stop()

    if shape == "ambient":
        refresh_ambient_source()
    last_ambient_check = time.monotonic()
    last_board_check = time.monotonic()

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
        with contextlib.ExitStack() as stack:
            leds = None
            if fan_units and HUB_DIRECT:
                # Not _hub_leds(): a hub that's away right now (USB reset)
                # comes back by itself (DirectHub reconnects on the next
                # write), fans included - no reason to sit this effect out.
                leds = _DirectHubLeds()
            elif fan_units:
                try:
                    leds = stack.enter_context(_hub_leds(timeout=2.0))
                except HubLedsUnavailable as exc:
                    log.warning("effect: %s, fan channels won't animate", exc)

            def channel_counts():
                return {u["led_channel"]: leds.channel_leds(u["led_channel"]) for u in fan_units}

            hub_counts = channel_counts() if leds is not None else {}
            start = time.monotonic()
            hub_failures = 0
            bridge_failures = 0
            # target -> (frame, monotonic time) of the last successful
            # write. A frame identical to the one already on the hardware
            # isn't sent again (static effects, breathing/strobe plateaus,
            # ambient between its 2fps samples, LEDs outside a selection)
            # - every write is real work for OpenRGB, RAM especially (each
            # one is a series of slow SMBus transactions), which showed up
            # as OpenRGB sitting near a full core while an effect ran. Still
            # re-sent every 2s so a device that reset or lost a write
            # comes back on its own.
            last_sent: dict[tuple, tuple[list, float]] = {}

            def unchanged(key, frame, now):
                prev = last_sent.get(key)
                return prev is not None and prev[0] == frame and now - prev[1] < 2.0

            # Ambient runs at the panel's frame rate, so the fans change
            # with every picture - but RAM and board keep the usual pace:
            # each of their writes is a series of slow SMBus/USB
            # transactions inside OpenRGB.
            frame_s = 1 / AMBIENT_FPS if shape == "ambient" else (0.08 if moving else 1.0)
            last_bridge_at = 0.0

            while not stop.is_set():
                if moving:
                    phase[0] = ((time.monotonic() - start) / period_s) % 1.0
                now = time.monotonic()

                if (now - last_board_check > 3.0 and openrgb.connected
                        and ((wants_ram and not ram_targets) or (wants_mobo and mobo_target is None))):
                    # OpenRGB came up after the effect started (a UAC
                    # prompt answered late, a restart) - take RAM and
                    # board on from here.
                    last_board_check = now
                    try:
                        _, _, ram_targets, mobo_target = _resolve_effect_targets()
                        for _, device in ram_targets:
                            openrgb.select_mode(device, "direct")
                        if mobo_target is not None:
                            openrgb.select_mode(mobo_target[1], "direct")
                        if ram_targets or mobo_target is not None:
                            log.info("effect: RAM/motherboard joined (OpenRGB is up now)")
                    except Exception as exc:
                        log.debug("effect: RAM/motherboard still unavailable: %s", exc)

                if shape == "ambient" and time.monotonic() - last_ambient_check > 2.0:
                    # Cheap enough to just do every couple seconds
                    # (one small JSON read + string compare) rather than
                    # needing the human to restart the effect after
                    # switching what's applied to the panel while
                    # ambient is already running.
                    refresh_ambient_source()
                    last_ambient_check = time.monotonic()

                if leds is not None:
                    for u in fan_units:
                        try:
                            count = hub_counts.get(u["led_channel"])
                            if not count:
                                continue
                            unit_leds, frame = frame_colors(u)
                            if len(unit_leds) != count:
                                continue
                            if unchanged(("hub", u["id"]), frame, now):
                                continue
                            leds.write(u["led_channel"], frame)
                            last_sent[("hub", u["id"])] = (frame, now)
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
                            leds.reconnect()
                            hub_counts = channel_counts()
                            log.info("effect: reconnected to the hub after repeated write failures")
                            hub_failures = 0
                            last_sent.clear()
                        except Exception as exc:
                            log.debug("effect: hub reconnect failed: %s", exc)
                            hub_failures = 2

                bridge_due = now - last_bridge_at >= 0.075
                if bridge_due:
                    last_bridge_at = now

                for u, device in (ram_targets if bridge_due else ()):
                    _, frame = frame_colors(u)
                    if unchanged(("ram", u["id"]), frame, now):
                        continue
                    try:
                        openrgb.set_zone_colors_by_index(device, 0, frame)
                        last_sent[("ram", u["id"])] = (frame, now)
                        bridge_failures = 0
                    except Exception as exc:
                        log.debug("effect: RAM write failed for %s: %s", u["id"], exc)
                        bridge_failures += 1

                if mobo_target is not None and bridge_due:
                    u, device, zone_index = mobo_target
                    _, frame = frame_colors(u)
                    frame = _fit_frame(frame, len(device.zones[zone_index].leds))
                    if not unchanged(("mobo", u["id"]), frame, now):
                        try:
                            openrgb.set_zone_colors_by_index(device, zone_index, frame)
                            last_sent[("mobo", u["id"])] = (frame, now)
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
                        last_sent.clear()
                    except Exception as exc:
                        log.debug("effect: OpenRGB reconnect failed: %s", exc)
                        bridge_failures = 2

                stop.wait(frame_s)
    except Exception as exc:
        log.warning("position effect stopped: %s", exc)
        _end_effect(stop, str(exc))
        return
    _end_effect(stop)


def _chase_level(back: int, window: int) -> tuple[int, int, int]:
    """White at full brightness for the leading LED (back == 0), halving
    for each LED behind it, off outside the window."""
    if back >= window:
        return (0, 0, 0)
    v = 255 >> back
    return (v, v, v)


def _run_chase_effect(channel: str, window: int, step_s: float,
                      stop: threading.Event | None = None):
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
    if stop is None:
        stop = _effect_stop
    try:
        with _hub_leds() as leds:
            n = leds.channel_leds(channel)
            if not n:
                _end_effect(stop, f"channel '{channel}' has no sized LEDs")
                return
            step = 0
            while not stop.is_set():
                # A bright leading LED with a dimming tail on black - the
                # canvas draws the same, so start point and direction read
                # by brightness alone, no colour needed (see
                # startLedOrderTest; hue cues are useless to a
                # colour-blind user, and all-white on the canvas's
                # light-grey off dots was barely visible).
                colors = [_chase_level((step - i) % n, window) for i in range(n)]
                leds.write(channel, colors)
                step = (step + 1) % n
                stop.wait(step_s)
    except Exception as exc:
        log.warning("LED order test stopped: %s", exc)
        _end_effect(stop, str(exc))
        return
    _end_effect(stop)


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
    stop = _new_effect_stop()
    _effect_status = {"running": True, "kind": "chase", "channel": channel, "error": None}
    _effect_task = _start_effect_thread(
        _run_chase_effect, channel, max(body.window, 1), max(body.step_s, 0.1), stop)
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


async def _do_start_position_effect(body: PositionEffectRequest, persist: bool = True,
                                     fade_in: bool = False):
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
    stop = _new_effect_stop()
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
    fade = None
    if fade_in and _startup_anim_end is not None:
        fade = (_load_startup_animation().fade_s, dict(_startup_anim_end))
    _effect_task = _start_effect_thread(
        _run_position_effect, body.direction, body.palette,
        rgb_colors, period_s, body.moving, body.shape, selection, stop, fade)
    if persist:
        _save_effect_state(body)


@app.get("/api/rgb/startup-animation")
async def get_startup_animation():
    return {**_load_startup_animation().model_dump(), "styles": list(startup_anim.STYLES),
            "available": HUB_DIRECT}


@app.post("/api/rgb/startup-animation")
async def set_startup_animation(body: StartupAnimation):
    """Saves the settings. An ambient show that should also play on the
    panel needs its video there already - uploaded once here, now, rather
    than at launch."""
    cfg = _checked_startup_animation(body)
    STARTUP_ANIM_FILE.write_text(cfg.model_dump_json(), encoding="utf-8")
    note = None
    if cfg.style == "ambient" and cfg.ambient_on_display and cfg.ambient_source:
        if await _ensure_on_panel(cfg.ambient_source) is None:
            note = "saved - but the video could not be put on the panel (not connected?); the LEDs still follow it"
    return {**cfg.model_dump(), "note": note}


@app.post("/api/rgb/startup-animation/preview")
async def preview_startup_animation(body: StartupAnimation):
    """Plays the given (not necessarily saved) animation now, the way it
    runs at launch: whatever was running stops, the animation plays, then
    the saved effect resumes with its fade - or the saved colours fade in."""
    global _effect_task, _effect_status, _startup_anim_end, _startup_anim_loop
    if not HUB_DIRECT:
        raise HTTPException(400, "the startup animation needs hub.control: direct")
    if not hub.ensure_connected():
        raise HTTPException(503, f"hub not connected: {hub.last_error}")
    cfg = _checked_startup_animation(body)
    if cfg.style == "ambient" and cfg.ambient_on_display and cfg.ambient_source:
        await _ensure_on_panel(cfg.ambient_source)
    saved = _load_effect_state()
    await _stop_effect()
    stop = _new_effect_stop()
    _startup_anim_end = None
    _startup_anim_loop = asyncio.get_running_loop()
    _effect_status = {"running": True, "kind": "startup", "error": None}
    _effect_task = _start_effect_thread(_run_startup_animation, cfg, saved is None, stop)
    await _effect_task
    if stop is not _effect_stop or stop.is_set():
        # Something else was started, or Stop / a test pattern stopped the
        # preview - resuming the effect now would paint over that.
        if stop is _effect_stop:
            _end_effect(stop)
        return {"ok": True, "interrupted": True}
    _end_effect(stop)
    if saved is not None:
        await _do_start_position_effect(saved, persist=False, fade_in=True)
    return {"ok": True}


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
            with _hub_leds() as leds:
                for u in units:
                    if u["kind"] != "fan" or not u.get("led_channel"):
                        continue
                    if not leds.channel_leds(u["led_channel"]):
                        continue
                    leds.fill(u["led_channel"], RED if u["id"] == unit_id else BLACK)
        await _run_hub_leds(_do)
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

async def _call_panel(fn, *args, **kwargs):
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
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except PanoramaError as exc:
        log.warning("panel command failed, dropping the cached connection: %s", exc)
        _drop_panorama(getattr(fn, "__self__", None) or _panorama)
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
        "last_applied": {
            "media": panel.last_media, "brightness": panel.last_brightness,
            # The gallery item id behind `media`, not `media` itself (the
            # panel's own generated remote filename) - see
            # _current_panorama_media_path's docstring for why matching by
            # name never worked. The frontend's currentPanoramaMediaItem()
            # matches on this instead of last_applied.media.
            "gallery_id": _load_panorama_state().get("gallery_id"),
        },
        "boot": {
            "media": panel.last_poweron_media,
            "gallery_id": _load_panorama_state().get("boot_gallery_id"),
            "is_default": panel.last_poweron_media in (DEFAULT_POWERON_MEDIA,
                                                       KANALI_DEFAULT_POWERON_MEDIA),
        },
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

_GALLERY_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _gallery_item_dir(item_id: str) -> Path:
    """Gallery ids are generated here (uuid hex) - anything else, in
    particular ".." (a legal single path segment), is refused before it
    gets joined onto GALLERY_DIR."""
    if not _GALLERY_ID_RE.fullmatch(item_id):
        raise HTTPException(404, "no such gallery item")
    return GALLERY_DIR / item_id


@app.get("/api/panorama/gallery")
async def gallery_list():
    return _load_gallery()


@app.post("/api/panorama/gallery")
async def gallery_upload(file: UploadFile = File(...)):
    GALLERY_DIR.mkdir(parents=True, exist_ok=True)
    item_id = uuid.uuid4().hex[:12]
    item_dir = GALLERY_DIR / item_id
    item_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "upload").suffix.lower() or ".png"
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        suffix = ".bin"
    original_path = item_dir / f"original{suffix}"
    # Streamed to disk in chunks rather than file.read() into one bytes
    # object - a gallery video can be hundreds of MB.
    with original_path.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            out.write(chunk)
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
    thumb_path = _gallery_item_dir(item_id) / "thumb.jpg"
    if not thumb_path.is_file():
        raise HTTPException(404, "no thumbnail")
    return FileResponse(thumb_path, media_type="image/jpeg")


@app.get("/api/panorama/gallery/{item_id}/original")
async def gallery_original(item_id: str):
    item_dir = _gallery_item_dir(item_id)
    if not item_dir.is_dir():
        raise HTTPException(404, "no such item")
    original = next(item_dir.glob("original.*"), None)
    if original is None:
        raise HTTPException(404, "no original file")
    suffix = original.suffix.lower()
    media_type = "video/mp4" if suffix == ".mp4" else "video/webm" if suffix == ".webm" else "video/quicktime" if suffix == ".mov" else "video/x-msvideo" if suffix == ".avi" else "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/png" if suffix == ".png" else "image/webp" if suffix == ".webp" else "application/octet-stream"
    return FileResponse(original, media_type=media_type)


async def _apply_gallery_item(item_id: str, as_boot: bool) -> dict:
    item_dir = _gallery_item_dir(item_id)
    original = next(item_dir.glob("original.*"), None) if item_dir.is_dir() else None
    if original is None:
        raise HTTPException(404, "no such gallery item")
    panel = await get_panorama()
    if panel is None or not panel.connected:
        raise HTTPException(503, "panel not connected")
    # Encode BEFORE taking _panorama_lock, not inside it - direct user
    # report: the panel's display went black and only recovered
    # "eventually", using shyyctl specifically, never KANALI. Traced to
    # this lock being held for the whole old panel.upload_media() call,
    # including its ffmpeg encode step - _keepalive_loop's ping needs
    # this same lock, so it couldn't fire for however long a real video's
    # encode took, easily longer than the panel's few-seconds idle
    # timeout (see _keepalive_loop's docstring). The encode itself
    # touches no device state at all, so it doesn't need the lock -
    # only the actual upload/apply protocol exchange below does.
    container = await asyncio.to_thread(build_encoded_media_container, str(original))
    if container is None:
        raise HTTPException(500, "media encode failed - check server logs")
    async with _panorama_lock:
        remote_name = await _call_panel(panel.upload_media, str(original), None, 2.0, 15.0, container)
        if remote_name is None:
            raise HTTPException(500, "upload failed (transfer step) - check server logs")
        _remember_gallery_remote(item_id, remote_name)
        if as_boot:
            ok = await _call_panel(panel.set_boot_media, remote_name)
        else:
            ok = await _call_panel(panel.set_media, remote_name)
        if ok:
            # Boot/standby media is a different slot from what's actively
            # showing now - only a non-boot apply changes which gallery
            # item is "current" (omitting gallery_id for the as_boot
            # branch leaves whatever was already saved alone, via
            # _save_panorama_state's "__unset__" sentinel default).
            if as_boot:
                _save_panorama_state(panel.last_media, panel.last_brightness,
                                     panel.last_poweron_media, panel.last_standby_media,
                                     boot_gallery_id=item_id)
            else:
                _save_panorama_state(panel.last_media, panel.last_brightness,
                                     panel.last_poweron_media, panel.last_standby_media,
                                     gallery_id=item_id)
    return {"filename": remote_name, "applied": ok}


@app.post("/api/panorama/gallery/{item_id}/apply")
async def gallery_apply(item_id: str):
    return await _apply_gallery_item(item_id, as_boot=False)


@app.post("/api/panorama/gallery/{item_id}/boot")
async def gallery_set_boot(item_id: str):
    return await _apply_gallery_item(item_id, as_boot=True)


@app.post("/api/panorama/boot/reset")
async def panorama_boot_reset():
    """Back to the TRYX power-on animation - the fallback for a custom
    boot screen that misbehaves: sends exactly what KANALI always sends
    in that slot."""
    panel = await get_panorama()
    if panel is None or not panel.connected:
        raise HTTPException(503, "panel not connected")
    async with _panorama_lock:
        ok = await _call_panel(panel.set_boot_media, KANALI_DEFAULT_POWERON_MEDIA)
        if ok:
            _save_panorama_state(panel.last_media, panel.last_brightness,
                                 panel.last_poweron_media, panel.last_standby_media,
                                 boot_gallery_id=None)
    if not ok:
        raise HTTPException(500, "the panel did not confirm the change")
    return {"ok": True}


@app.delete("/api/panorama/gallery/{item_id}")
async def gallery_delete(item_id: str):
    _gallery_item_dir(item_id)
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
    if "--remove-elevated-helpers" in sys.argv:
        # The uninstaller's way of undoing "Start without UAC prompts" (see
        # tools/build_installer/installer.nsi) - exactly what Settings'
        # "Undo" does, then exit.
        log.info("removing the prompt-free helper start: %s",
                 _run_elevated_helpers_script("remove").get("message"))
        sys.exit(0)

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
