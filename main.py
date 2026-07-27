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
import contextlib
import json
import logging
import math
import re
import subprocess
import tempfile
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.conflicts import running_conflicts
from app.hub import Hub, HubError
from app.layout import Layout
from app.openrgb_bridge import OpenRgbBridge
from app.openrgb_raw import OpenRgbRaw
from app.panorama import find_print_panel, find_usb_panel
from app.scan import Scan

ROOT = Path(__file__).resolve().parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
WEB_DIR = ROOT / "web"
CURVE_FILE = ROOT / "curve.json"
LAYOUT_FILE = ROOT / "layout.json"
PANORAMA_STATE_FILE = ROOT / "panorama_state.json"

logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s: %(message)s")
log = logging.getLogger("shyyctl")

hub = Hub(match=CONFIG["hub"]["match"])
openrgb = OpenRgbBridge(host=CONFIG["openrgb"]["host"], port=CONFIG["openrgb"]["port"])
scan = Scan(hub, openrgb)
layout = Layout(LAYOUT_FILE)

_panorama = None
_panorama_info: dict | None = None
_panorama_lock = asyncio.Lock()
_keepalive_task: asyncio.Task | None = None

# Host-driven, position-aware hub effects (Advanced RGB view) - see
# _run_wave_effect below. A plain threading.Event rather than an asyncio
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


def _save_panorama_state(media: str | None, brightness: int | None):
    """Persists what shyyctl itself last applied - not a read of the
    device's real current state, which the reverse-engineered protocol has
    no command for (checked: neither the handshake's device-info reply nor
    the media listing carries a "currently active" field - confirmed
    against real captures, every field of both was dumped and inspected).
    KANALI itself almost certainly does the same thing (remembers its own
    last action rather than querying it back). This at least survives a
    shyyctl restart or panel reconnect, instead of resetting to a guessed
    default every time a fresh connection object is created."""
    PANORAMA_STATE_FILE.write_text(
        json.dumps({"media": media, "brightness": brightness}), encoding="utf-8")


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
            # A fresh connection object's last_media/last_brightness start
            # at hardcoded defaults (see app/panorama.py) - reload whatever
            # shyyctl itself last actually applied instead of forgetting it
            # on every reconnect.
            saved = _load_panorama_state()
            if saved.get("media"):
                _panorama.last_media = saved["media"]
            if saved.get("brightness") is not None:
                _panorama.last_brightness = saved["brightness"]
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
    empty device info despite a healthy connection."""
    while True:
        try:
            panel = _panorama
            if panel is not None and panel.connected:
                async with _panorama_lock:
                    await asyncio.to_thread(panel.ping)
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keepalive_task
    if not await asyncio.to_thread(hub.connect):
        log.warning("hub not connected: %s", hub.last_error)
    if not await asyncio.to_thread(openrgb.connect):
        log.warning("OpenRGB not connected: %s", openrgb.last_error)
        if _launch_vendor_openrgb():
            # A single fixed short wait wasn't enough against real hardware:
            # OpenRGB's own device scan (SMBus/PawnIO/RAM enumeration) was
            # still visibly running in its console output well past 3s,
            # long before its SDK server was ready to accept connections.
            # Poll for up to ~20s instead of one fixed-delay retry.
            for _ in range(10):
                await asyncio.sleep(2)
                if await asyncio.to_thread(openrgb.connect):
                    break
            else:
                log.warning("Still not connected after starting bundled OpenRGB (waited ~20s): %s",
                           openrgb.last_error)
    await get_panorama()
    _keepalive_task = asyncio.create_task(_keepalive_loop())

    if CONFIG.get("server", {}).get("open_browser"):
        url = f"http://{CONFIG['server']['host']}:{CONFIG['server']['port']}"
        webbrowser.open(url)

    yield

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


# -- fans -----------------------------------------------------------------

@app.get("/api/fans")
async def list_fans():
    if not hub.connected:
        raise HTTPException(503, "hub not connected")
    readings = await asyncio.to_thread(hub.status)
    return {"channels": hub.channels.get("fan", []), "readings": readings}


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


# -- fan curve (storage only for now - no automatic control loop yet) -----

class CurveRequest(BaseModel):
    points: list[list[int]]  # [[temp_c, duty_pct], ...]


@app.get("/api/curve")
async def get_curve():
    if CURVE_FILE.is_file():
        return json.loads(CURVE_FILE.read_text(encoding="utf-8"))
    return {"points": CONFIG["curve"]["default"]}


@app.post("/api/curve")
async def set_curve(body: CurveRequest):
    CURVE_FILE.write_text(json.dumps({"points": body.points}), encoding="utf-8")
    return {"ok": True}


# -- RGB: motherboard / RAM / hub -------------------------------------------

def _open_hub_raw() -> OpenRgbRaw:
    return OpenRgbRaw(host=CONFIG["openrgb"]["host"], port=CONFIG["openrgb"]["port"])


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


class HubModeRequest(BaseModel):
    name: str


@app.post("/api/rgb/hub/mode")
async def set_hub_mode(body: HubModeRequest):
    """Selects one of the hub's own native firmware modes (Static, Direct,
    Fading, Spectrum Cycle, Rainbow Wave, etc - whatever ctrl.modes lists,
    read straight off the hub itself and surfaced via /api/rgb/devices'
    merged hub entry) - the 'standalone animation' tier of the Advanced RGB
    view: rainbow-style effects the hub's own IC drives on its own,
    distinct from the position-aware wave effect below, which is
    host-driven and needs shyyctl running continuously. Stops that wave
    effect first if one is active, so the two don't fight over the zone's
    active mode."""
    await _stop_effect()

    def _do():
        with _open_hub_raw() as raw:
            ctrl = raw.find_controller("NZXT")
            if ctrl is None:
                return "no_device"
            if not raw.select_mode(ctrl, body.name):
                return f"no mode named '{body.name}' on {ctrl.name}"
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


def _run_wave_effect(color_a: tuple, color_b: tuple, period_s: float):
    """Runs until _effect_stop is set, on its own worker thread (via
    asyncio.to_thread) - one raw connection held for the whole effect
    rather than reopened per frame (see openrgb_raw.py's docstring on this
    hub's server-side instability under repeated REQUEST_CONTROLLER_DATA
    hammering). Drives every fan channel, RAM stick and the motherboard's
    real zone - whatever's actually been placed in Setup - from its real
    per-LED y-coordinate (app/layout.py), blended against a moving phase:
    a wave that visibly runs top-to-bottom across the whole case, not
    just the fans.

    RAM/mobo go through the separate openrgb-python bridge (protocol 4 is
    fine for them, unlike the hub's effects-only zones - see
    openrgb_bridge.py's own docstring), resolved and mode-selected once
    here before the loop starts, not on every frame - re-resolving them
    per frame would reintroduce exactly the per-request OpenRGB latency
    just fixed elsewhere (see RECONNECT_RETRIES's comment). If they can't
    be resolved (not placed, or OpenRGB can't currently see them), the
    fan channels still animate - RAM/mobo just sit out silently rather
    than failing the whole effect."""
    global _effect_status
    data = layout.with_leds()
    leds_by_unit: dict[str, list[dict]] = {}
    for led in data["leds"]:
        leds_by_unit.setdefault(led["unit"], []).append(led)
    fan_units = [u for u in data["units"] if u.get("kind") == "fan" and u.get("led_channel")]
    ram_units = [u for u in data["units"] if u.get("kind") == "ram"]
    mobo_units = [u for u in data["units"] if u.get("kind") == "mobo"]

    all_units = fan_units + ram_units + mobo_units
    all_ys = [led["y"] for u in all_units for led in leds_by_unit.get(u["id"], [])]
    if not all_ys:
        _effect_status = {"running": False, "kind": None,
                           "error": "no units placed in Setup yet"}
        return
    y_min, y_max = min(all_ys), max(all_ys)
    y_span = (y_max - y_min) or 1.0

    def blend(a, b, t):
        # Cosine ease instead of a linear lerp - a linear wave has a
        # visible seam where it wraps from b back to a; cosine keeps the
        # loop smooth all the way around.
        w = (1 - math.cos(t * 2 * math.pi)) / 2
        return tuple(round(a[i] + (b[i] - a[i]) * w) for i in range(3))

    def frame_colors(u):
        unit_leds = sorted(leds_by_unit.get(u["id"], []), key=lambda l: l["index"])
        return unit_leds, [
            blend(color_a, color_b, ((led["y"] - y_min) / y_span - phase[0]) % 1.0)
            for led in unit_leds
        ]
    phase = [0.0]  # mutable cell frame_colors closes over

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
            log.warning("wave effect: could not resolve RAM sticks: %s", exc)

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
            log.warning("wave effect: could not resolve motherboard: %s", exc)

    for _, device in ram_targets:
        openrgb.select_mode(device, "direct")
    if mobo_target is not None:
        openrgb.select_mode(mobo_target[1], "direct")

    try:
        with _open_hub_raw() as raw:
            ctrl = raw.find_controller("NZXT") if fan_units else None
            if fan_units and ctrl is None:
                log.warning("wave effect: no hub RGB device found, fan channels won't animate")
            elif ctrl is not None:
                raw.select_mode(ctrl, "direct")
            start = time.monotonic()
            while not _effect_stop.is_set():
                phase[0] = ((time.monotonic() - start) / period_s) % 1.0

                if ctrl is not None:
                    for u in fan_units:
                        zone = raw.zone_for_channel(ctrl, u["led_channel"])
                        if zone is None or zone.leds_count == 0:
                            continue
                        unit_leds, colors = frame_colors(u)
                        if len(unit_leds) != zone.leds_count:
                            continue
                        raw.set_zone_colors(ctrl.index, zone.index, colors)

                for u, device in ram_targets:
                    _, colors = frame_colors(u)
                    try:
                        openrgb.set_zone_colors_by_index(device, 0, colors)
                    except Exception as exc:
                        log.debug("wave effect: RAM write failed for %s: %s", u["id"], exc)

                if mobo_target is not None:
                    u, device, zone_index = mobo_target
                    _, colors = frame_colors(u)
                    try:
                        openrgb.set_zone_colors_by_index(device, zone_index, colors)
                    except Exception as exc:
                        log.debug("wave effect: motherboard write failed: %s", exc)

                _effect_stop.wait(0.08)
    except Exception as exc:
        log.warning("wave effect stopped: %s", exc)
        _effect_status = {"running": False, "kind": None, "error": str(exc)}
        return
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
    global _effect_task, _effect_status
    if _effect_status.get("running"):
        raise HTTPException(409, "an effect is already running - stop it first")
    _effect_stop.clear()
    _effect_status = {"running": True, "kind": "chase", "channel": channel, "error": None}
    _effect_task = asyncio.create_task(
        asyncio.to_thread(_run_chase_effect, channel, max(body.window, 1), max(body.step_s, 0.1)))
    return {"ok": True}


class WaveEffectRequest(BaseModel):
    color_a: str = "#4c79ff"
    color_b: str = "#ff7a30"
    period_s: float = 4.0


@app.post("/api/rgb/effects/wave/start")
async def start_wave_effect(body: WaveEffectRequest):
    global _effect_task, _effect_status
    if _effect_status.get("running"):
        raise HTTPException(409, "an effect is already running - stop it first")
    _effect_stop.clear()
    _effect_status = {"running": True, "kind": "wave", "error": None}
    color_a = _hex_to_rgb(body.color_a)
    color_b = _hex_to_rgb(body.color_b)
    _effect_task = asyncio.create_task(
        asyncio.to_thread(_run_wave_effect, color_a, color_b, max(body.period_s, 0.5)))
    return {"ok": True}


@app.post("/api/rgb/effects/stop")
async def stop_effect_endpoint():
    await _stop_effect()
    return {"ok": True}


@app.get("/api/rgb/effects/status")
async def effect_status():
    return _effect_status


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
        return await asyncio.to_thread(panel.list_media)


class BrightnessRequest(BaseModel):
    value: int


@app.post("/api/panorama/brightness")
async def panorama_brightness(body: BrightnessRequest):
    panel = await get_panorama()
    if panel is None or not panel.connected:
        raise HTTPException(503, "panel not connected")
    async with _panorama_lock:
        ok = await asyncio.to_thread(panel.set_brightness, body.value)
    if ok:
        _save_panorama_state(panel.last_media, panel.last_brightness)
    return {"ok": ok}


async def _handle_media_upload(file: UploadFile, apply_as_boot: bool) -> dict:
    panel = await get_panorama()
    if panel is None or not panel.connected:
        raise HTTPException(503, "panel not connected")
    suffix = Path(file.filename or "upload").suffix or ".png"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name
    try:
        async with _panorama_lock:
            remote_name = await asyncio.to_thread(panel.upload_media, tmp_path)
            if remote_name is None:
                raise HTTPException(500, "upload failed (encode or transfer step) - check server logs")
            if apply_as_boot:
                ok = await asyncio.to_thread(panel.set_boot_media, remote_name)
            else:
                ok = await asyncio.to_thread(panel.set_media, remote_name)
                if ok:
                    _save_panorama_state(panel.last_media, panel.last_brightness)
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    return {"filename": remote_name, "applied": ok}


@app.post("/api/panorama/media")
async def panorama_upload_media(file: UploadFile = File(...)):
    return await _handle_media_upload(file, apply_as_boot=False)


@app.post("/api/panorama/boot-image")
async def panorama_upload_boot_image(file: UploadFile = File(...)):
    return await _handle_media_upload(file, apply_as_boot=True)


# -- frontend ---------------------------------------------------------------

if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=CONFIG["server"]["host"], port=CONFIG["server"]["port"])
