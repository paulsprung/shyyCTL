"""Minimal, hand-written OpenRGB SDK client, protocol version 5 aware.

Written after reading OpenRGB's own server source (RGBController.cpp,
NetworkServer.cpp on the project's GitLab) because openrgb-python - even
its latest GitHub master, not just the PyPI release - hard-caps at
protocol 4 (its own network.py: `OPENRGB_PROTOCOL_VERSION = 4`).

Why that matters for this specific hub: its channels are flagged
ZONE_FLAG_MANUALLY_CONFIGURABLE_SIZE_EFFECTS_ONLY - a v5-only concept
("resizable effects-only zones", per NetworkProtocol.h's version history).
For any client that negotiates protocol < 5, RGBController::
GetZoneDescriptionData deliberately overwrites the zone's real LED count
with 0/1 before sending it:

    if((zone.flags & ZONE_FLAG_MANUALLY_CONFIGURABLE_SIZE_EFFECTS_ONLY)
        && (protocol_version < 5))
    {
        // report leds_count as 0 or 1 regardless of the real size
    }

That's not a bug on either side - it's an intentional backward-compat
shim for clients that don't understand the concept, and it's exactly why
this hub's zones always looked empty through openrgb-python: it can never
ask for anything above protocol 4, so it can never see this hub's real
sizes. Speaking protocol 5 properly here gets the real numbers directly,
no manual pre-configuration in OpenRGB's own GUI required.

Scope: only what shyyctl needs for the hub - list controllers/zones,
resize a zone, set a zone's or a whole device's colours, switch mode.
Mobo/RAM stay on openrgb-python (openrgb_bridge.py) - they don't use
effects-only zones, protocol 4 sees their real sizes just fine.

Wire format (from NetworkServer.cpp / RGBController.cpp, cross-checked
against openrgb-python's network.py/utils.py which implements the same
framing for versions <= 4):

    header: b'ORGB' + device_id(u32) + packet_type(u32) + payload_size(u32)
    strings: length(u16, includes the trailing NUL) + bytes (NUL-terminated)
    RGBColor: R(u8) G(u8) B(u8) pad(u8)
"""

from __future__ import annotations

import logging
import socket
import struct
from dataclasses import dataclass, field

log = logging.getLogger("openrgb_raw")

HEADER_FMT = "4sIII"
HEADER_SIZE = struct.calcsize(HEADER_FMT)
MAGIC = b"ORGB"

PKT_REQUEST_CONTROLLER_COUNT = 0
PKT_REQUEST_CONTROLLER_DATA = 1
PKT_REQUEST_PROTOCOL_VERSION = 40
PKT_SET_CLIENT_NAME = 50
PKT_RGBCONTROLLER_RESIZEZONE = 1000
PKT_RGBCONTROLLER_UPDATELEDS = 1050
PKT_RGBCONTROLLER_UPDATEZONELEDS = 1051
PKT_RGBCONTROLLER_UPDATEMODE = 1101

PROTOCOL_VERSION = 5  # what we ask for; server may negotiate us down


def hex_window(data: bytes, around: int, before: int = 96, after: int = 64) -> str:
    """A small, pasteable hex+ascii dump centred on a byte offset - for
    reporting exactly where parsing broke down without dumping the whole
    (often multi-KB) payload."""
    start = max(0, around - before)
    end = min(len(data), around + after)
    lines = []
    for row in range(start - start % 16, end, 16):
        chunk = data[row:row + 16]
        hexed = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        marker = " <== failure here" if row <= around < row + 16 else ""
        lines.append(f"{row:6d}: {hexed:<48} {asc}{marker}")
    return "\n".join(lines)


class OpenRgbRawError(Exception):
    pass


class ReadOverrunError(OpenRgbRawError):
    """Raised with enough context (the full payload + where reading broke
    down) to print a hex dump around the failure point for debugging,
    instead of just a message."""

    def __init__(self, message: str, data: bytes, offset: int):
        super().__init__(message)
        self.data = data
        self.offset = offset


@dataclass
class ModeInfo:
    index: int
    name: str
    value: int
    flags: int
    speed_min: int
    speed_max: int
    brightness_min: int
    brightness_max: int
    colors_min: int
    colors_max: int
    speed: int
    brightness: int
    direction: int
    color_mode: int
    colors: list[tuple[int, int, int]] = field(default_factory=list)


@dataclass
class ZoneInfo:
    index: int
    name: str
    zone_type: int
    leds_min: int
    leds_max: int
    leds_count: int


@dataclass
class ControllerInfo:
    index: int
    name: str
    device_type: int
    zones: list[ZoneInfo]
    num_leds: int
    modes: list[ModeInfo]
    active_mode: int


class _Reader:
    """Sequential reader over a bytes buffer - mirrors the pointer walk in
    RGBController.cpp's Get*DescriptionData functions, field for field, so
    the parsing code here can be checked line-by-line against that source."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def bytes_left(self) -> int:
        return len(self.data) - self.pos

    def read(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise ReadOverrunError(
                f"tried to read {n} bytes at offset {self.pos} with only "
                f"{self.bytes_left()} left ({len(self.data)} total)",
                self.data, self.pos)
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def u8(self) -> int:
        return struct.unpack("B", self.read(1))[0]

    def u16(self) -> int:
        return struct.unpack("H", self.read(2))[0]

    def u32(self) -> int:
        return struct.unpack("I", self.read(4))[0]

    def i32(self) -> int:
        return struct.unpack("i", self.read(4))[0]

    def string(self) -> str:
        length = self.u16()
        raw = self.read(length)
        return raw.rstrip(b"\x00").decode("utf-8", errors="replace")

    def rgbcolor(self) -> tuple[int, int, int]:
        r, g, b = self.u8(), self.u8(), self.u8()
        self.u8()  # padding byte
        return (r, g, b)


class OpenRgbRaw:
    def __init__(self, host: str = "127.0.0.1", port: int = 6742,
                 name: str = "shyyctl-raw", protocol_version: int = PROTOCOL_VERSION,
                 timeout: float = 5.0):
        self.host = host
        self.port = port
        self.name = name
        self.requested_version = protocol_version
        self.negotiated_version = 0
        self.timeout = timeout
        self.sock: socket.socket | None = None

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect((self.host, self.port))

        self._send(0, PKT_REQUEST_PROTOCOL_VERSION, struct.pack("I", self.requested_version))
        _, ptype, payload = self._recv()
        if ptype == PKT_REQUEST_PROTOCOL_VERSION and len(payload) >= 4:
            server_version = struct.unpack("I", payload[:4])[0]
            self.negotiated_version = min(server_version, self.requested_version)
        else:
            self.negotiated_version = 0
        log.info("negotiated OpenRGB protocol version %d (requested %d)",
                 self.negotiated_version, self.requested_version)

        self._send(0, PKT_SET_CLIENT_NAME, self.name.encode("utf-8") + b"\x00")

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    # -- wire helpers --------------------------------------------------

    def _send(self, device_id: int, packet_type: int, payload: bytes = b""):
        if self.sock is None:
            raise OpenRgbRawError("not connected")
        header = struct.pack(HEADER_FMT, MAGIC, device_id, packet_type, len(payload))
        self.sock.sendall(header + payload)

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise OpenRgbRawError("connection closed while reading")
            buf += chunk
        return buf

    def _recv(self):
        header = self._recv_exact(HEADER_SIZE)
        magic, device_id, packet_type, packet_size = struct.unpack(HEADER_FMT, header)
        if magic != MAGIC:
            raise OpenRgbRawError(f"bad magic in response header: {magic!r}")
        payload = self._recv_exact(packet_size) if packet_size else b""
        return device_id, packet_type, payload

    # -- device enumeration ----------------------------------------------

    def controller_count(self) -> int:
        self._send(0, PKT_REQUEST_CONTROLLER_COUNT)
        _, _, payload = self._recv()
        return struct.unpack("I", payload)[0]

    def controller_data(self, index: int) -> ControllerInfo:
        self._send(index, PKT_REQUEST_CONTROLLER_DATA, struct.pack("I", self.negotiated_version))
        _, _, payload = self._recv()
        try:
            return self._parse_controller(index, payload)
        except ReadOverrunError as exc:
            log.error("controller #%d: %s\n%s", index, exc, hex_window(exc.data, exc.offset))
            raise

    def all_controllers(self) -> list[ControllerInfo]:
        controllers = []
        for i in range(self.controller_count()):
            try:
                controllers.append(self.controller_data(i))
            except OpenRgbRawError as exc:
                log.warning("controller #%d failed to parse: %s", i, exc)
        return controllers

    def find_controller(self, name_contains: str) -> ControllerInfo | None:
        for ctrl in self.all_controllers():
            if name_contains.lower() in ctrl.name.lower():
                return ctrl
        return None

    @staticmethod
    def find_mode(controller: ControllerInfo, name: str) -> ModeInfo | None:
        for mode in controller.modes:
            if mode.name.lower() == name.lower():
                return mode
        return None

    @staticmethod
    def zone_for_channel(controller: ControllerInfo, channel: str) -> ZoneInfo | None:
        """liquidctl calls a channel 'led3'; OpenRGB names the matching zone
        something like 'Hue 2 Channel 3' - match on the trailing number, the
        only part confirmed stable (same approach as openrgb_bridge.py)."""
        import re
        m = re.search(r"(\d+)$", channel)
        if not m:
            return None
        number = m.group(1)
        for zone in controller.zones:
            zm = re.search(r"(\d+)$", zone.name.strip())
            if zm and zm.group(1) == number:
                return zone
        return None

    # -- parsing, field-for-field against RGBController.cpp ---------------

    def _parse_controller(self, index: int, payload: bytes) -> ControllerInfo:
        r = _Reader(payload)
        version = self.negotiated_version

        r.u32()  # total size - redundant once we already have the full payload
        device_type = r.i32()
        name = r.string()
        if version >= 1:
            r.string()  # vendor
        r.string()  # description
        r.string()  # firmware version
        r.string()  # serial
        r.string()  # location

        log.debug("[%s] name=%r pos=%d after metadata", index, name, r.pos)
        num_modes = r.u16()
        active_mode = r.i32()
        log.debug("[%s] num_modes=%d active_mode=%d pos=%d", index, num_modes, active_mode, r.pos)
        modes = []
        for i in range(num_modes):
            mode = self._parse_mode(r, version, i)
            log.debug("[%s]   mode %d: name=%r flags=%d colors=%d pos=%d",
                      index, i, mode.name, mode.flags, len(mode.colors), r.pos)
            modes.append(mode)

        num_zones = r.u16()
        log.debug("[%s] num_zones=%d pos=%d", index, num_zones, r.pos)
        zones = []
        for i in range(num_zones):
            zone = self._parse_zone(r, version, i)
            log.debug("[%s]   zone %d: name=%r leds_count=%d pos=%d",
                      index, i, zone.name, zone.leds_count, r.pos)
            zones.append(zone)

        num_leds = r.u16()
        log.debug("[%s] num_leds=%d pos=%d payload_len=%d", index, num_leds, r.pos, len(payload))
        for _ in range(num_leds):
            self._skip_led(r, version)

        num_colors = r.u16()
        log.debug("[%s] num_colors=%d pos=%d", index, num_colors, r.pos)
        for _ in range(num_colors):
            r.rgbcolor()

        if version >= 5:
            num_led_names = r.u16()
            log.debug("[%s] num_led_names=%d pos=%d", index, num_led_names, r.pos)
            for _ in range(num_led_names):
                r.string()
            r.u32()  # controller flags
        log.debug("[%s] finished at pos=%d of %d", index, r.pos, len(payload))

        return ControllerInfo(index, name, device_type, zones, num_leds, modes, active_mode)

    def _parse_mode(self, r: _Reader, version: int, index: int) -> ModeInfo:
        name = r.string()
        value = r.i32() if version < 6 else 0
        flags = r.u32()
        speed_min = r.u32()
        speed_max = r.u32()
        brightness_min = r.u32() if version >= 3 else 0
        brightness_max = r.u32() if version >= 3 else 0
        colors_min = r.u32()
        colors_max = r.u32()
        speed = r.u32()
        brightness = r.u32() if version >= 3 else 0
        direction = r.u32()
        color_mode = r.u32()
        num_colors = r.u16()
        colors = [r.rgbcolor() for _ in range(num_colors)]
        return ModeInfo(index, name, value, flags, speed_min, speed_max,
                         brightness_min, brightness_max, colors_min, colors_max,
                         speed, brightness, direction, color_mode, colors)

    def _parse_zone(self, r: _Reader, version: int, index: int) -> ZoneInfo:
        name = r.string()
        zone_type = r.i32()
        leds_min = r.u32()
        leds_max = r.u32()
        leds_count = r.u32()
        matrix_size = r.u16()
        if matrix_size:
            r.read(matrix_size)
        if version >= 4:
            num_segments = r.u16()
            for _ in range(num_segments):
                self._skip_segment(r, version)
        if version >= 5:
            r.u32()  # zone flags
        return ZoneInfo(index, name, zone_type, leds_min, leds_max, leds_count)

    @staticmethod
    def _skip_led(r: _Reader, version: int):
        r.string()
        if version < 6:
            r.u32()

    @staticmethod
    def _skip_segment(r: _Reader, version: int):
        # Confirmed against RGBController.cpp's GetSegmentDescriptionData:
        # the matrix map + flags fields are gated on protocol >= 6, NOT >= 5
        # (unlike the zone-level and controller-level flags, which really
        # are >= 5). Getting this one wrong shifted every read after the
        # first zone that had any segments.
        r.string()  # name
        r.i32()     # zone type
        r.u32()     # start_idx
        r.u32()     # leds_count
        if version >= 6:
            matrix_size = r.u16()
            if matrix_size:
                r.read(matrix_size)
            r.u32()  # segment flags

    # -- control -----------------------------------------------------------

    def resize_zone(self, device_index: int, zone_index: int, led_count: int):
        payload = struct.pack("ii", zone_index, led_count)
        self._send(device_index, PKT_RGBCONTROLLER_RESIZEZONE, payload)

    def set_zone_colors(self, device_index: int, zone_index: int,
                        colors: list[tuple[int, int, int]]):
        body = struct.pack("iH", zone_index, len(colors))
        for red, green, blue in colors:
            body += struct.pack("BBBx", red, green, blue)
        payload = struct.pack("I", len(body) + 4) + body
        self._send(device_index, PKT_RGBCONTROLLER_UPDATEZONELEDS, payload)

    def set_device_colors(self, device_index: int, colors: list[tuple[int, int, int]]):
        body = struct.pack("H", len(colors))
        for red, green, blue in colors:
            body += struct.pack("BBBx", red, green, blue)
        payload = struct.pack("I", len(body) + 4) + body
        self._send(device_index, PKT_RGBCONTROLLER_UPDATELEDS, payload)

    def set_mode(self, device_index: int, mode: ModeInfo):
        """Re-sends a mode (as returned by controller_data) to select it as
        active - id must be the mode's own index for the server to pick it."""
        version = self.negotiated_version
        data = struct.pack("i", mode.index)
        name_bytes = mode.name.encode("ascii", errors="replace")
        data += struct.pack("H", len(name_bytes) + 1) + name_bytes + b"\x00"
        if version < 6:
            data += struct.pack("i", mode.value)
        data += struct.pack("I", mode.flags)
        data += struct.pack("I", mode.speed_min)
        data += struct.pack("I", mode.speed_max)
        if version >= 3:
            data += struct.pack("I", mode.brightness_min)
            data += struct.pack("I", mode.brightness_max)
        data += struct.pack("I", mode.colors_min)
        data += struct.pack("I", mode.colors_max)
        data += struct.pack("I", mode.speed)
        if version >= 3:
            data += struct.pack("I", mode.brightness)
        data += struct.pack("I", mode.direction)
        data += struct.pack("I", mode.color_mode)
        data += struct.pack("H", len(mode.colors))
        for red, green, blue in mode.colors:
            data += struct.pack("BBBx", red, green, blue)
        data = struct.pack("I", len(data) + 4) + data
        self._send(device_index, PKT_RGBCONTROLLER_UPDATEMODE, data)
