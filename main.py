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

logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s: %(message)s")
log = logging.getLogger("shyyctl")

hub = Hub(match=CONFIG["hub"]["match"])
openrgb = OpenRgbBridge(host=CONFIG["openrgb"]["host"], port=CONFIG["openrgb"]["port"])
scan = Scan(hub, openrgb)
layout = Layout(LAYOUT_FILE)

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


# -- hardware discovery scan -------------------------------------------

class ScanRequest(BaseModel):
    mode: str = "full"  # "full" | "quick" | "rgb"


@app.post("/api/scan/start")
async def scan_start(body: ScanRequest = ScanRequest()):
    if body.mode not in scan.MODES:
        raise HTTPException(400, f"unknown scan mode {body.mode!r} - must be one of {scan.MODES}")
    started = await asyncio.to_thread(scan.start, body.mode)
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
    panel = await get_panorama()
    if panel is None or not panel.connected:
        return {"connected": False}
    async with _panorama_lock:
        info = await asyncio.to_thread(panel.handshake)
    return {"connected": True, "info": info}


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
