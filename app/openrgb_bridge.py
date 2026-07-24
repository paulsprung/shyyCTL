"""RAM, motherboard and hub RGB via OpenRGB's SDK.

shyyctl never touches SMBus itself - OpenRGB carries that privilege, same
separation as LibreHardwareMonitor for CPU temperature (see PROMPT.md).

Facts confirmed against real hardware this session, not assumptions:
- The NZXT hub's own LED zones report 0 LEDs by default and do nothing when
  written to until explicitly resized with the real LED count - this turned
  out to be the actual root cause of what first looked like a liquidctl
  channel-specific bug. Once sized correctly, every populated channel works
  through OpenRGB.
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
"""

import logging
import re
import time

log = logging.getLogger("openrgb")

# OpenRGB's own RAM/SMBus detection has been observed taking well past 30s
# on real hardware (see _devices() below) - the retry budget has to cover
# that, not just a brief SDK-side hiccup.
RECONNECT_RETRIES = 20
RETRY_DELAY = 2.0


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

    def connect(self) -> bool:
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

    def report(self) -> dict:
        if not self.connected:
            return {"connected": False, "sticks": [], "error": self.last_error}
        try:
            sticks = self.ram()
        except Exception as exc:
            return {"connected": False, "sticks": [], "error": str(exc)}
        return {"connected": True, "sticks": [{"leds": len(d.leds)} for d in sticks], "error": None}

    # -- device groups ---------------------------------------------------

    def _devices(self, retries: int = RECONNECT_RETRIES) -> list:
        if not self.connected and not self.connect():
            return []
        clean: list = []
        for attempt in range(retries):
            all_devices = self.client.devices
            clean = [d for d in all_devices if d is not None]
            if all_devices and len(clean) == len(all_devices):
                return clean
            if not all_devices:
                # Nothing cached yet - re-query rather than re-reading the
                # same stale (possibly permanently empty) attribute; see
                # the class docstring for why this is necessary.
                log.warning("OpenRGB reports 0 devices (detection may still be running), "
                            "re-querying, retry %d/%d", attempt + 1, retries)
                try:
                    self.client.update()
                except Exception as exc:
                    log.warning("OpenRGB update() failed: %s", exc)
            else:
                log.warning("%d device(s) came back None from OpenRGB, retry %d/%d",
                            len(all_devices) - len(clean), attempt + 1, retries)
            time.sleep(RETRY_DELAY)
        log.warning("gave up waiting for OpenRGB to settle, proceeding with %d device(s)",
                    len(clean))
        return clean

    def hub_leds(self) -> list:
        return [d for d in self._devices() if "NZXT" in d.name]

    def motherboard(self) -> list:
        from openrgb.utils import DeviceType
        return [d for d in self._devices() if d.type == DeviceType.MOTHERBOARD]

    def ram(self) -> list:
        from openrgb.utils import DeviceType
        return [d for d in self._devices() if d.type == DeviceType.DRAM]

    def describe_all(self) -> list[dict]:
        """Raw device/zone breakdown, exactly what OpenRGB itself reports -
        every controllable device on the system, not just the three groups
        shyyctl cares about (that also includes keyboards/mice etc.)."""
        return [
            {
                "name": d.name,
                "type": d.type.name,
                "leds": len(d.leds),
                "modes": [m.name for m in d.modes],
                "zones": [{"name": z.name, "leds": len(z.leds)} for z in d.zones],
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
        or from a source that actually verified it against the hardware."""
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
        for device in devices:
            self._select_mode(device, mode_name)
            device.set_color(RGBColor(*color))

    def set_zone_color(self, device, channel: str, color: tuple[int, int, int],
                        persistent: bool = True):
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
    def _select_mode(device, mode_name: str):
        for i, mode in enumerate(device.modes):
            if mode.name.lower() == mode_name:
                device.set_mode(i)
                return
        log.warning("%s has no '%s' mode - leaving whatever mode is currently active",
                    device.name, mode_name)
