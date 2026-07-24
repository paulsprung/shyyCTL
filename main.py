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
import subprocess
import tempfile
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.hub import Hub, HubError
from app.openrgb_bridge import OpenRgbBridge
from app.panorama import find_print_panel, find_usb_panel
from app.scan import Scan

ROOT = Path(__file__).resolve().parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
WEB_DIR = ROOT / "web"
CURVE_FILE = ROOT / "curve.json"

logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s: %(message)s")
log = logging.getLogger("shyyctl")

hub = Hub(match=CONFIG["hub"]["match"])
openrgb = OpenRgbBridge(host=CONFIG["openrgb"]["host"], port=CONFIG["openrgb"]["port"])
scan = Scan(hub, openrgb)

_panorama = None
_panorama_lock = asyncio.Lock()
_keepalive_task: asyncio.Task | None = None


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


async def get_panorama():
    """Lazily (re)connects to the panel on every call rather than trusting
    a connection made at server startup - it may not be plugged in yet, or
    KANALI may currently hold usbprint.sys open (see app/panorama.py)."""
    global _panorama
    async with _panorama_lock:
        if _panorama is not None and _panorama.connected:
            return _panorama
        _panorama = await asyncio.to_thread(_find_panorama)
        return _panorama


async def _keepalive_loop():
    """Confirmed against real hardware this session: the panel reverts to
    its default content a few seconds after the last message it sees -
    every real KANALI capture showed it pinging roughly every 2s
    continuously the whole time it ran. A failed ping here just means the
    panel is unplugged or something else (KANALI) currently has it; it's
    not treated as fatal, the loop keeps trying."""
    while True:
        try:
            panel = _panorama
            if panel is not None and panel.connected:
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

@app.get("/api/rgb/devices")
async def rgb_devices():
    if not openrgb.connected and not await asyncio.to_thread(openrgb.connect):
        raise HTTPException(503, f"OpenRGB not connected: {openrgb.last_error}")
    return await asyncio.to_thread(openrgb.describe_all)


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


class ResizeRequest(BaseModel):
    leds: int


@app.post("/api/rgb/hub/{channel}/resize")
async def resize_hub_channel(channel: str, body: ResizeRequest):
    """Needed once per channel before hub color control does anything -
    OpenRGB's zones default to 0 LEDs and silently no-op until sized to
    match the real fan (see app/openrgb_bridge.py's docstring). A scan
    applies this automatically for every channel it identifies with
    certainty (app/scan.py's _resize_hub_zones); call this directly for a
    channel scan left at 'unknown' confidence after picking its model by
    hand, or to redo one channel without a full rescan."""
    if not openrgb.connected and not await asyncio.to_thread(openrgb.connect):
        raise HTTPException(503, f"OpenRGB not connected: {openrgb.last_error}")
    devices = await asyncio.to_thread(openrgb.hub_leds)
    if not devices:
        raise HTTPException(404, "no hub RGB device found")
    ok = await asyncio.to_thread(openrgb.resize_zone, devices[0], channel, body.leds)
    if not ok:
        raise HTTPException(400, f"could not resize zone for '{channel}' to {body.leds} LEDs")
    return {"ok": True}


@app.post("/api/rgb/hub/{channel}/color")
async def set_hub_channel_color(channel: str, body: ColorRequest):
    """liquidctl LED control is confirmed broken for the 360mm units - hub
    RGB goes through OpenRGB exclusively, same as motherboard/RAM (see
    app/openrgb_bridge.py's docstring)."""
    if not openrgb.connected and not await asyncio.to_thread(openrgb.connect):
        raise HTTPException(503, f"OpenRGB not connected: {openrgb.last_error}")
    rgb = _hex_to_rgb(body.color)
    devices = await asyncio.to_thread(openrgb.hub_leds)
    if not devices:
        raise HTTPException(404, "no hub RGB device found")
    try:
        await asyncio.to_thread(openrgb.set_zone_color, devices[0], channel, rgb, body.persistent)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


# -- hardware discovery scan -------------------------------------------

@app.post("/api/scan/start")
async def scan_start():
    started = await asyncio.to_thread(scan.start)
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


# -- panorama display -----------------------------------------------------

@app.get("/api/panorama/status")
async def panorama_status():
    panel = await get_panorama()
    if panel is None or not panel.connected:
        return {"connected": False}
    info = await asyncio.to_thread(panel.handshake)
    return {"connected": True, "info": info}


@app.get("/api/panorama/media")
async def panorama_media():
    panel = await get_panorama()
    if panel is None or not panel.connected:
        raise HTTPException(503, "panel not connected")
    return await asyncio.to_thread(panel.list_media)


class BrightnessRequest(BaseModel):
    value: int


@app.post("/api/panorama/brightness")
async def panorama_brightness(body: BrightnessRequest):
    panel = await get_panorama()
    if panel is None or not panel.connected:
        raise HTTPException(503, "panel not connected")
    ok = await asyncio.to_thread(panel.set_brightness, body.value)
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
        remote_name = await asyncio.to_thread(panel.upload_media, tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    if remote_name is None:
        raise HTTPException(500, "upload failed (encode or transfer step) - check server logs")
    if apply_as_boot:
        ok = await asyncio.to_thread(panel.set_boot_media, remote_name)
    else:
        ok = await asyncio.to_thread(panel.set_media, remote_name)
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
