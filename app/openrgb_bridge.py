"""RAM and motherboard RGB via OpenRGB's SDK (openrgb-python).

Hub RGB is NOT handled here anymore - see app/openrgb_raw.py. This bridge
is hard-capped at SDK protocol 4 (a limit in the openrgb-python library
itself, not something a setting here can lift), and the hub's zones need
protocol 5 to report/accept their real LED counts at all. Mobo/RAM don't
use that effects-only zone flag, so protocol 4 sees their real sizes fine
and they stay on this simpler client.

shyyctl never touches SMBus itself - OpenRGB carries that privilege, same
separation as LibreHardwareMonitor for CPU temperature (see PROMPT.md).

Facts confirmed against real hardware this session, not assumptions:
- A zone with 0 LEDs must never be targeted directly - writing to one falls
  back to a device-wide write instead of doing nothing (confirmed: doing so
  visibly changed every other channel at once).
- Devices can transiently come back as None from the SDK client right after
  OpenRGB (re)starts - retry rather than giving up on the first attempt.
- An empty devices list is its own failure mode, distinct from the above:
  openrgb-python only populates client.devices when something calls
  client.update() - it does that once inside OpenRGBClient.__init__ and
  never again on its own. If our SDK connection lands while OpenRGB's own
  (slow - SMBus/RAM SPD probing has been observed taking well past 30s)
  internal hardware detection hasn't finished yet, __init__'s one update()
  call catches a device count of zero and nothing ever re-asks - the SDK
  socket just sits there holding a stale empty list forever. Confirmed
  against a real run: /api/status showed openrgb.connected: true (the TCP
  handshake and initial update() both genuinely succeeded) while
  /api/rgb/devices stayed [] indefinitely. Retrying here has to actively
  call client.update() again, not just re-read the same cached attribute.
- 'Static' mode is what survives closing OpenRGB/shyyctl (the controller IC
  holds it); 'Direct' mode is for host-driven per-LED effects and needs
  shyyctl running continuously.
- This bridge is shared, mutable, and was NOT thread-safe - confirmed as the
  real cause of a whole chain of symptoms that looked like OpenRGB-side
  instability (a device's zones list coming back doubled, its reported
  total LED count turning to garbage across runs, one device permanently
  stuck as None). What was actually happening: FastAPI serves each request
  on its own thread (asyncio.to_thread), and the scan runs on its own
  background thread too - both were calling into this same object's
  self.client/socket concurrently with zero locking. reconnect() replacing
  self.client (and closing its old socket) while another thread was
  mid-read on it is exactly what OpenRGB's own server log
  ("recv_select failed receiving magic, closing listener") was reporting.
  Every method that touches self.client is now serialised with one
  reentrant lock.
"""

import logging
import re
import threading
import time

log = logging.getLogger("openrgb")

# OpenRGB's own RAM/SMBus detection has been observed taking well past 30s
# on real hardware right after shyyctl launches it - that's what this
# budget was originally sized for (a one-time wait during server startup,
# see main.py's lifespan). _devices() is now also called from every live
# request through motherboard()/ram(), though (RGB tab, Advanced, a
# scan...), and on a system where OpenRGB's SMBus access is failing
# outright (confirmed by a real user's server log: "PawnIO module
# initialization aborted", OpenRGB's modern WinRing0 replacement, right
# before repeated "device(s) came back None" warnings) every retry just
# reproduces the identical failure - there's nothing to wait out. The
# previous 20x2s=40s budget turned each of those calls into a real, felt
# multi-second-to-a-minute hang with no way to cancel out of it
# (confirmed by direct user report: RAM/mobo requests "extremely
# delayed", a scan stuck on this step with Cancel doing nothing - Cancel
# only takes effect between scan steps, and this retry loop is a single
# uninterruptible step). Cut down significantly - still enough to ride
# out a brief real hiccup, not to spend a minute retrying a hard failure.
RECONNECT_RETRIES = 6
RETRY_DELAY = 1.0


class OpenRgbBridge:
    def __init__(self, host: str = "127.0.0.1", port: int = 6742,
                 protocol_version: int | None = None):
        self.host = host
        self.port = port
        # openrgb-python has been seen negotiating protocol 4 even against a
        # server that advertises 5 (confirmed via OpenRGB's own SDK Client
        # tab this session) - v5 added new device/zone fields, which lines
        # up with exactly one (newly-added) device failing to parse. Forcing
        # this explicitly is a diagnostic, not a permanent fix.
        self.protocol_version = protocol_version
        self.client = None
        self.connected = False
        self.last_error: str | None = None
        # Reentrant: connect()/_devices() call each other from the same
        # thread, and a plain Lock would deadlock on that. See the class
        # docstring for why every method below acquires this.
        self._lock = threading.RLock()

    def connect(self) -> bool:
        with self._lock:
            try:
                from openrgb import OpenRGBClient
            except ImportError:
                self.last_error = "openrgb-python not installed"
                self.connected = False
                return False
            try:
                kwargs = {"address": self.host, "port": self.port, "name": "shyyctl"}
                if self.protocol_version is not None:
                    kwargs["protocol_version"] = self.protocol_version
                self.client = OpenRGBClient(**kwargs)
                self.connected = True
                self.last_error = None
                return True
            except Exception as exc:
                self.last_error = str(exc)
                self.connected = False
                return False

    def reconnect(self) -> bool:
        """Fully drops and re-establishes the SDK connection instead of
        reusing the cached client - see resize_zone's docstring and
        scan.py's _resize_hub_zones, which calls this once per channel
        rather than issuing several resize calls over one long-lived
        connection. Closes the old socket first so OpenRGB doesn't
        accumulate stale client entries in its SDK tab across a scan."""
        with self._lock:
            if self.client is not None:
                try:
                    self.client.disconnect()
                except Exception:
                    pass
            self.connected = False
            return self.connect()

    def disconnect(self) -> None:
        """Drops the SDK connection without reconnecting - used when the
        OpenRGB process itself is being stopped (see main.py's service-mode
        control), so this object doesn't keep reporting connected: true
        against a socket whose other end no longer exists."""
        with self._lock:
            if self.client is not None:
                try:
                    self.client.disconnect()
                except Exception:
                    pass
            self.client = None
            self.connected = False

    def debug_info(self) -> dict:
        """Raw introspection of the SDK connection itself, not the usual
        cleaned-up device lists - for pinning down exactly which device
        index fails to come through and why. Surfaces the negotiated
        protocol version explicitly: openrgb-python 0.3.6 (the latest
        release on PyPI as of this session) hard-caps itself at protocol 4
        (network.py's OPENRGB_PROTOCOL_VERSION = 4 is a literal constant,
        not just an unset default) even if the OpenRGB server itself
        offers 5 - a real candidate for why one specific (likely the
        newest/most complex) device keeps coming back None with no
        exception ever raised: a v4 client silently misreading a v5-shaped
        payload wouldn't necessarily hit a clean struct/parsing error, it
        could just desync which bytes belong to which field."""
        with self._lock:
            if not self.connected and not self.connect():
                return {"connected": False, "error": self.last_error}
            update_error = None
            try:
                self.client.update()
            except Exception as exc:
                update_error = f"{type(exc).__name__}: {exc}"
            comms = self.client.comms
            return {
                "connected": True,
                "update_error": update_error,
                "negotiated_protocol_version": comms._protocol_version,
                "client_max_protocol_version": comms.max_protocol_version,
                "device_count": self.client.device_num,
                "devices": [
                    {
                        "index": i,
                        "name": d.name if d is not None else None,
                        "type": d.type.name if d is not None else None,
                    }
                    for i, d in enumerate(self.client.devices)
                ],
            }

    def report(self) -> dict:
        with self._lock:
            if not self.connected:
                return {"connected": False, "sticks": [], "error": self.last_error}
            try:
                sticks = self.ram()
            except Exception as exc:
                return {"connected": False, "sticks": [], "error": str(exc)}
            return {"connected": True, "sticks": [{"leds": len(d.leds)} for d in sticks], "error": None}

    # -- device groups ---------------------------------------------------

    def _devices(self, retries: int = RECONNECT_RETRIES) -> list:
        with self._lock:
            if not self.connected and not self.connect():
                return []
            clean: list = []
            for attempt in range(retries):
                all_devices = self.client.devices
                clean = [d for d in all_devices if d is not None]
                if all_devices and len(clean) == len(all_devices):
                    return clean
                if not all_devices:
                    log.warning("OpenRGB reports 0 devices (detection may still be running), "
                                "re-querying, retry %d/%d", attempt + 1, retries)
                else:
                    log.warning("%d device(s) came back None from OpenRGB, re-querying, "
                                "retry %d/%d", len(all_devices) - len(clean), attempt + 1, retries)
                # Both cases need an actual re-query, not just re-reading the
                # same cached attribute - client.devices only ever changes
                # when something calls client.update() (see class docstring).
                # A previous version of this only did that for the fully-
                # empty case, which left a partially-None list retrying for
                # the full budget without ever actually asking OpenRGB again.
                try:
                    self.client.update()
                except Exception as exc:
                    log.warning("OpenRGB update() failed: %s", exc)
                time.sleep(RETRY_DELAY)
            log.warning("gave up waiting for OpenRGB to settle, proceeding with %d device(s)",
                        len(clean))
            return clean

    def hub_leds(self) -> list:
        """Not used by the real app anymore - main.py and scan.py talk to
        the hub through app/openrgb_raw.py's protocol-5 client instead
        (see its docstring for why: this hub's zones are flagged
        effects-only/manually-configurable-size, and OpenRGB's server
        deliberately reports those as 0/1 LEDs to any protocol-4 client
        like this one, as an intentional backward-compat shim). Kept for
        tools/test_app.py's side-by-side comparison test only."""
        with self._lock:
            return [d for d in self._devices() if "NZXT" in d.name]

    def _settled_by_type(self, device_type, retries: int = 2, delay: float = 1.0) -> list:
        """A *stable* device list - no Nones, non-empty - can still be
        genuinely short of the real hardware if OpenRGB's own SMBus/RAM
        SPD detection just hasn't found every device yet (confirmed
        against a real run: a second RAM stick and the motherboard both
        intermittently missing from these endpoints when called shortly
        after startup, despite both showing up fine in OpenRGB's own
        client moments later). _devices()'s own retry loop doesn't help
        here - its fast path returns the instant a list has no Nones,
        without ever calling client.update() again, and client.devices is
        a plain cached attribute that openrgb-python only repopulates on
        an explicit update() call (see class docstring) - just re-reading
        it in a loop returns the exact same frozen list forever.

        REVERTED a first attempt at this that called self.reconnect()
        between polls: this method runs on essentially every call to
        motherboard()/ram(), and the RGB tab hits both on every visit, so
        that meant tearing down and rebuilding the *entire* SDK connection
        (fresh TCP handshake, protocol renegotiation, full device
        re-enumeration) at least once per page load, sometimes twice
        back-to-back on concurrent requests. That's exactly the "hammering
        the connection" pattern this class's own docstring already
        documents as triggering real OpenRGB server-side instability
        (garbage LED counts, doubled zone lists) - confirmed as a real
        regression, not a hypothetical one, by a direct user report that
        this broke previously-working RAM/mobo detection outright.
        self.client.update() is the actually-lightweight equivalent: it
        re-asks the *same* connection for the current device count/data
        (this is exactly what _devices()'s own already-safe retry uses
        for the empty/None case), with no socket teardown and no new SDK
        client entry created in OpenRGB's own client list each time.

        retries/delay trimmed down from an earlier 4/1.5s: this runs on
        every live request, and on a system where OpenRGB genuinely can't
        detect a device (not just slow to), more retries just spend more
        real seconds reproducing the same failure - see RECONNECT_RETRIES's
        comment above for the concrete report that motivated this."""
        best: list = []
        for attempt in range(retries):
            current = [d for d in self._devices() if d.type == device_type]
            if len(current) <= len(best) and best:
                break
            best = current
            if attempt < retries - 1:
                log.debug("%d device(s) of type %s so far, re-querying to check for more",
                           len(best), device_type)
                time.sleep(delay)
                try:
                    self.client.update()
                except Exception as exc:
                    log.warning("OpenRGB update() failed while settling: %s", exc)
        return best

    def motherboard(self) -> list:
        from openrgb.utils import DeviceType
        with self._lock:
            return self._settled_by_type(DeviceType.MOTHERBOARD)

    def ram(self) -> list:
        from openrgb.utils import DeviceType
        with self._lock:
            return self._settled_by_type(DeviceType.DRAM)

    def describe_all(self) -> list[dict]:
        """Raw device/zone breakdown, exactly what OpenRGB itself reports -
        every controllable device on the system, not just the three groups
        shyyctl cares about (that also includes keyboards/mice etc.)."""
        with self._lock:
            return [
                {
                    "name": d.name,
                    "type": d.type.name,
                    "leds": len(d.leds),
                    "modes": [m.name for m in d.modes],
                    "zones": [{"index": i, "name": z.name, "leds": len(z.leds)}
                              for i, z in enumerate(d.zones)],
                }
                for d in self._devices()
            ]

    # -- hub channel <-> zone mapping -------------------------------------

    @staticmethod
    def zone_for_channel(device, channel: str):
        """liquidctl calls a channel 'led3'; OpenRGB names the matching zone
        something like 'Hue 2 Channel 3' - match on the trailing number
        rather than assuming an exact name, since that's the only part
        confirmed stable."""
        m = re.search(r"(\d+)$", channel)
        if not m:
            return None
        number = m.group(1)
        for zone in device.zones:
            zm = re.search(r"(\d+)$", zone.name.strip())
            if zm and zm.group(1) == number:
                return zone
        return None

    def resize_zone(self, device, channel: str, led_count: int) -> bool:
        """Never call this with a guessed count - only with a number that
        came from the user picking a real fan model (geometry.expected_leds)
        or from a source that actually verified it against the hardware.

        REVERTED this session: an earlier version of this retried
        zone.update() several times against a slow read-back, on the
        theory that the hub just needed longer to settle over USB. Real
        evidence disproved that - after adding the retry, repeated re-reads
        of this specific device ("NZXT RGB & Fan Controller 2024") started
        coming back with its zones list literally doubled (12 entries for
        6 real channels) and a garbage, ever-growing total LED count across
        runs. That's a server-side OpenRGB driver instability triggered by
        hammering REQUEST_CONTROLLER_DATA at this device repeatedly, not a
        client-side timing gap - retrying harder was actively corrupting
        OpenRGB's own in-memory state for it. Back to one resize, one
        read-back, no polling loop; see _resize_hub_zones in scan.py for
        the real fix (a fresh reconnect per channel instead of hammering
        one cached connection)."""
        with self._lock:
            zone = self.zone_for_channel(device, channel)
            if zone is None:
                return False
            if len(zone.leds) == led_count:
                return True
            zone.resize(led_count)
            return len(zone.leds) == led_count

    # -- colour -----------------------------------------------------------

    def set_color(self, devices: list, color: tuple[int, int, int], persistent: bool = True):
        """Whole-device colour (every non-empty zone). Empty (0-LED) zones
        contribute nothing to the write, which is safe - it's writing
        *through* a zone-scoped call on an empty zone that misbehaves."""
        from openrgb.utils import RGBColor
        mode_name = "static" if persistent else "direct"
        with self._lock:
            for device in devices:
                self._select_mode(device, mode_name)
                device.set_color(RGBColor(*color))

    def set_zone_color(self, device, channel: str, color: tuple[int, int, int],
                        persistent: bool = True):
        with self._lock:
            zone = self.zone_for_channel(device, channel)
            if zone is None:
                raise ValueError(f"no zone on {device.name} matches channel '{channel}'")
            if len(zone.leds) == 0:
                raise ValueError(f"channel '{channel}' has no LEDs - it's an empty combo header, "
                                  "do not address it")
            from openrgb.utils import RGBColor
            mode_name = "static" if persistent else "direct"
            self._select_mode(device, mode_name)
            zone.set_color(RGBColor(*color))

    @staticmethod
    def real_zone_index(device) -> int | None:
        """The one zone on a device that actually has LEDs attached - for
        the X870-F's motherboard controller, that's 'Aura Mainboard' (3
        onboard LEDs); its other 3 zones ('Aura Addressable N') are
        unconnected combo headers with 0 LEDs each and are never worth
        addressing. First match only: a board with more than one populated
        zone would need picking a specific one, not just the first, but
        this board only has one, and guessing further isn't warranted."""
        for i, zone in enumerate(device.zones):
            if len(zone.leds) > 0:
                return i
        return None

    def set_led_color_by_index(self, device, zone_index: int, led_index: int,
                                color: tuple[int, int, int], persistent: bool = True):
        """Addresses exactly one physical LED within a zone - confirmed via
        openrgb-python's own source (orgb.py's LED.set_color) that this
        sends RGBCONTROLLER_UPDATESINGLELED with that LED's real
        device-wide id, not a zone-wide write. Only meaningful for a
        protocol-4-visible device like the motherboard; the hub's
        effects-only zones need the protocol-5 raw client instead (see
        openrgb_raw.py), same reasoning as everywhere else in this file."""
        with self._lock:
            if zone_index < 0 or zone_index >= len(device.zones):
                raise ValueError(f"{device.name} has no zone at index {zone_index}")
            zone = device.zones[zone_index]
            if led_index < 0 or led_index >= len(zone.leds):
                raise ValueError(f"zone '{zone.name}' has no LED at index {led_index}")
            from openrgb.utils import RGBColor
            mode_name = "static" if persistent else "direct"
            self._select_mode(device, mode_name)
            zone.leds[led_index].set_color(RGBColor(*color))

    def set_device_led_color(self, device, led_index: int, color: tuple[int, int, int],
                              persistent: bool = True):
        """Same real per-LED write as set_led_color_by_index above, but
        straight through the device's own top-level LED list rather than
        a zone - each RAM stick is its own single-zone OpenRGB device, so
        there's no need to know a zone index first, unlike the
        multi-zone motherboard controller."""
        with self._lock:
            if led_index < 0 or led_index >= len(device.leds):
                raise ValueError(f"{device.name} has no LED at index {led_index}")
            from openrgb.utils import RGBColor
            mode_name = "static" if persistent else "direct"
            self._select_mode(device, mode_name)
            device.leds[led_index].set_color(RGBColor(*color))

    def select_mode(self, device, mode_name: str):
        """Public entry point to _select_mode - for a caller (main.py's
        wave effect) that resolves a device once before a continuous
        animation loop starts and needs to select 'direct' mode itself,
        the same way set_color()/set_zone_color() etc already do
        internally on every call."""
        with self._lock:
            self._select_mode(device, mode_name)

    def set_zone_colors_by_index(self, device, zone_index: int, colors: list[tuple[int, int, int]]):
        """Multi-LED write to one zone with no mode reselection - for a
        continuous animation loop (main.py's wave effect) that already
        called select_mode() once before starting; reselecting the mode
        on every single frame would double the round trips for zero
        benefit, the same reasoning set_zone_colors_by_index's siblings
        above apply to a one-off write."""
        with self._lock:
            if zone_index < 0 or zone_index >= len(device.zones):
                raise ValueError(f"{device.name} has no zone at index {zone_index}")
            from openrgb.utils import RGBColor
            device.zones[zone_index].set_colors([RGBColor(*c) for c in colors])

    @staticmethod
    def _select_mode(device, mode_name: str):
        for i, mode in enumerate(device.modes):
            if mode.name.lower() == mode_name:
                device.set_mode(i)
                return
        log.warning("%s has no '%s' mode - leaving whatever mode is currently active",
                    device.name, mode_name)
