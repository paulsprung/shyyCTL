"""Tryx Panorama display - USB control channel + ADB for media.

Protocol reverse-engineered by github.com/fadli0029/reed-tpse (MIT licence),
read directly from its C++ source this session and ported here. That project
verified its work against the Panorama SE (2160x1080) and 360 (2240x1080) -
this project's actual hardware is the 240mm 2-fan variant, which nobody has
walked yet. The firmware family is presumably shared, but treat every
command in this file as unverified until it's actually run against this
specific panel - the same rule as everywhere else in this codebase.

IMPORTANT, worked out from real hardware + KANALI's own driver install
this session (not from reed-tpse, which only documents Linux): on Windows
this panel (VID 0x391A / PID 0x1011, shows up as "RK PANO" - RK = Rockchip,
its SoC) is NOT a CDC-ACM serial device - there is no COM port for it, which
is why Device Manager shows nothing under Ports (COM & LPT). It also is NOT
meant to be a raw WinUSB device: KANALI's own installer sets it up with
Windows' built-in USB Printing Support class driver (usbprint.sys) -
confirmed directly against this hardware's Device Manager entry, correcting
an earlier assumption in this file that WinUSB was the intended driver
(that assumption was based on a different USB path seen in KANALI's logs,
which most likely belongs to a different Tryx USB component entirely, e.g.
the AIO's pump/fan controller, not this display).

Concretely, on Windows this panel is talked to via CreateFile/ReadFile/
WriteFile on its USBPRINT device interface (PanoramaPrint below) - not via
libusb/pyusb at all, which cannot open a usbprint.sys-bound device (see
usb_probe()'s "not supported"/NotImplementedError case, hit when trying
exactly that). PanoramaUSB (WinUSB via Zadig) is kept only as a fallback/
diagnostic path - using it requires replacing usbprint.sys, which breaks
KANALI's own access, and is not the vendor's intended setup.
reed-tpse's serial framing (find_device()/Panorama below) is confirmed
correct only for the Linux CDC-ACM case and never applies on Windows at
all for this panel - it's simply the wrong transport here.

Two separate channels, confirmed from reed-tpse's own README, not guessed:
- USB control channel - handshake, brightness, screen config. On Linux
  this is CDC-ACM (/dev/ttyACM*, 115200 baud 8N1). On Windows it's
  usbprint.sys (VID 0x391A:0x1011) instead of a COM port - see
  PanoramaPrint below.
- ADB (adb.exe on Windows, must be on PATH): file transfer only, media is
  pushed to /sdcard/pcMedia/ on the panel's own (Android-based) storage.
  liquidctl/OpenRGB are never involved here - this is a third, independent
  control surface.

Frame format, byte for byte from reed-tpse's protocol.cpp:

    0x5A <escaped payload> 0x5A

    payload before escaping:
        length (u16, big-endian) + header + '\r\n\r\n' + json_body + crc (u8)
    header:
        f"{request_state} {cmd_type} {version}\r\n"
        f"ContentType=json\r\nContentLength={len(json_body)}\r\nAckNumber={n}\r\n"
    length:
        len(header + '\r\n\r\n' + json_body) + 5   (5 = protocol overhead)
    crc:
        sum of every byte from the length prefix through the byte just
        before the crc itself, & 0xFF
    escaping (applied to length+header+body+crc, NOT the outer 0x5A markers):
        0x5A -> 0x5B 0x01
        0x5B -> 0x5B 0x02

Known commands (request_state is always "POST" for these):
    conn                 - handshake; response JSON has productId/OS/sn/version
    brightness            - {"value": 0-100}
    waterBlockScreenId    - full screen config (media list, mode, ratio, ...)
    mediaDelete           - {"include": [filenames]}

The device wants a keepalive roughly every 60s or it reverts to its default
screen (reed-tpse runs a background daemon for this) - not implemented here
yet; this module is the protocol plus one-shot commands.
"""

import json
import logging
import subprocess
import time

log = logging.getLogger("panorama")

FRAME_MARKER = 0x5A
ESCAPE_MARKER = 0x5B
MEDIA_PATH = "/sdcard/pcMedia/"
BAUD_RATE = 115200


class PanoramaError(Exception):
    pass


# -- wire protocol, ported field-for-field from reed-tpse's protocol.cpp ----

def _crc(data: bytes) -> int:
    return sum(data) & 0xFF


def _escape(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b == FRAME_MARKER:
            out += bytes((ESCAPE_MARKER, 0x01))
        elif b == ESCAPE_MARKER:
            out += bytes((ESCAPE_MARKER, 0x02))
        else:
            out.append(b)
    return bytes(out)


def _unescape(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        if data[i] == ESCAPE_MARKER and i + 1 < len(data) and data[i + 1] in (0x01, 0x02):
            out.append(FRAME_MARKER if data[i + 1] == 0x01 else ESCAPE_MARKER)
            i += 2
        else:
            out.append(data[i])
            i += 1
    return bytes(out)


def build_frame(request_state: str, cmd_type: str, content: str = "",
                 version: str = "1", ack_number: int = 0) -> bytes:
    header = (f"{request_state} {cmd_type} {version}\r\n"
              f"ContentType=json\r\n"
              f"ContentLength={len(content)}\r\n"
              f"AckNumber={ack_number}\r\n\r\n")
    body = (header + content).encode("utf-8")
    total_length = len(body) + 5
    data = total_length.to_bytes(2, "big") + body
    data += bytes((_crc(data),))
    return bytes((FRAME_MARKER,)) + _escape(data) + bytes((FRAME_MARKER,))


class Response:
    def __init__(self, raw: str, version: str, status: str, body: str, json_value):
        self.raw = raw
        self.version = version
        self.status = status
        self.body = body
        self.json = json_value

    def __repr__(self):
        return f"Response(version={self.version!r}, status={self.status!r}, json={self.json!r})"


def parse_response(data: bytes) -> Response | None:
    if len(data) < 4 or data[0] != FRAME_MARKER or data[-1] != FRAME_MARKER:
        return None
    payload = _unescape(data[1:-1])
    if len(payload) < 3:
        return None
    message = payload[2:-1].decode("utf-8", errors="replace")  # skip length(2) + trailing crc(1)
    separator = message.find("\r\n\r\n")
    if separator == -1:
        return Response(message, "", "", "", None)
    header_part = message[:separator]
    body = message[separator + 4:]
    json_value = None
    if body:
        try:
            json_value = json.loads(body)
        except json.JSONDecodeError:
            json_value = None
    first_line = header_part.split("\r\n", 1)[0]
    parts = first_line.split()
    version = parts[0] if len(parts) > 0 else ""
    status = parts[1] if len(parts) > 1 else ""
    return Response(message, version, status, body, json_value)


# -- shared command layer: identical over serial and raw USB ---------------
#
# The frame protocol (build_frame/parse_response above) doesn't care how the
# bytes get to the device - CDC-ACM serial and raw USB bulk endpoints both
# just move bytes. Only _write()/_read_frame()/connect()/disconnect() differ
# between transports; every actual command (handshake, brightness, screen
# config) is transport-agnostic and lives here once.

class _PanoramaBase:
    def __init__(self, timeout: float = 1.0):
        self.timeout = timeout
        self._seq = 0

    @property
    def connected(self) -> bool:
        raise NotImplementedError

    def connect(self) -> bool:
        raise NotImplementedError

    def disconnect(self):
        raise NotImplementedError

    def _write(self, frame: bytes):
        raise NotImplementedError

    def _read_frame(self, timeout_s: float) -> bytes:
        raise NotImplementedError

    def send_command(self, request_state: str, cmd_type: str, content: str = "",
                     wait_response: bool = True) -> Response | None:
        if not self.connected:
            raise PanoramaError("not connected")
        self._seq += 1
        frame = build_frame(request_state, cmd_type, content, "1", self._seq)
        self._write(frame)
        if not wait_response:
            return None
        time.sleep(0.5)  # matches reed-tpse's fixed pre-read delay
        raw = self._read_frame(timeout_s=2.0)
        if not raw:
            return None
        return parse_response(raw)

    def handshake(self) -> dict | None:
        response = self.send_command("POST", "conn")
        if response is None or response.json is None:
            return None
        j = response.json
        return {
            "product_id": j.get("productId", "unknown"),
            "os": j.get("OS", "unknown"),
            "serial": j.get("sn", "unknown"),
            "app_version": j.get("version", {}).get("app", "unknown"),
            "firmware": j.get("version", {}).get("firmware", "unknown"),
            "hardware": j.get("version", {}).get("hardware", "unknown"),
            "attributes": j.get("attribute", []),
        }

    def set_brightness(self, value: int) -> Response | None:
        value = max(0, min(100, int(value)))
        return self.send_command("POST", "brightness", json.dumps({"value": value}))

    def set_screen_config(self, media: list[str], screen_mode: str = "Full Screen",
                          ratio: str = "2:1", play_mode: str = "Single") -> Response | None:
        config = {
            "Type": "Custom",
            "id": "Customization",
            "screenMode": screen_mode,
            "ratio": ratio,
            "playMode": play_mode,
            "media": media,
            "settings": {
                "position": "Top", "color": "#FFFFFF", "align": "Center",
                "badges": [], "filter": {"value": "", "opacity": 0.0},
            },
            "sysinfoDisplay": [],
        }
        content = json.dumps(config)
        # reed-tpse sends this twice - the device otherwise sometimes keeps
        # showing a cached config from before the first apply.
        self.send_command("POST", "waterBlockScreenId", content)
        time.sleep(0.5)
        return self.send_command("POST", "waterBlockScreenId", content)

    def delete_media(self, filenames: list[str]) -> Response | None:
        return self.send_command("POST", "mediaDelete", json.dumps({"include": filenames}))


# -- serial (CDC-ACM) transport ---------------------------------------------
#
# Correct on Linux (/dev/ttyACM*). Confirmed WRONG for this panel on Windows
# this session - it has no COM port there at all (see PanoramaUSB below).
# Kept for Linux and in case some future firmware/driver combo exposes a COM
# port here after all.

class Panorama(_PanoramaBase):
    def __init__(self, port: str, timeout: float = 1.0):
        super().__init__(timeout)
        self.port_name = port
        self.ser = None

    def connect(self) -> bool:
        import serial
        try:
            self.ser = serial.Serial(
                port=self.port_name, baudrate=BAUD_RATE,
                bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE, rtscts=False, dsrdtr=False,
                xonxoff=False, timeout=self.timeout)
            return True
        except Exception as exc:
            log.warning("could not open %s: %s", self.port_name, exc)
            self.ser = None
            return False

    def disconnect(self):
        if self.ser is not None:
            self.ser.close()
            self.ser = None

    @property
    def connected(self) -> bool:
        return self.ser is not None and self.ser.is_open

    def _write(self, frame: bytes):
        self.ser.write(frame)
        self.ser.flush()

    def _read_frame(self, timeout_s: float) -> bytes:
        deadline = time.monotonic() + timeout_s
        buf = bytearray()
        while time.monotonic() < deadline:
            chunk = self.ser.read(256)
            if chunk:
                buf += chunk
                if len(buf) >= 2 and buf[0] == FRAME_MARKER and buf[-1] == FRAME_MARKER:
                    break
        return bytes(buf)


def find_device(timeout: float = 1.0) -> str | None:
    """Try every serial port on the system, handshake each, return the first
    one that answers with a real productId - mirrors reed-tpse's
    find_device(), which scans /dev/ttyACM* on Linux; ports on Windows have
    no equivalent fixed naming, so every port has to be tried.

    Confirmed this session to be the wrong transport for this panel on
    Windows (see usb_probe() below) - kept only because it's still correct
    on Linux, and in case some future firmware/driver combo does expose a
    COM port here after all."""
    import serial.tools.list_ports
    for info in serial.tools.list_ports.comports():
        panel = Panorama(info.device, timeout=timeout)
        if not panel.connect():
            continue
        try:
            info_dict = panel.handshake()
            if info_dict and info_dict["product_id"] not in ("", "unknown"):
                return info.device
        finally:
            panel.disconnect()
    return None


# -- raw USB visibility check (WinUSB, no COM port) -------------------------

TRYX_VENDOR_ID = 0x391A
PANORAMA_PRODUCT_ID = 0x1011


def usb_probe() -> dict:
    """Look for the panel at the raw USB bus level via pyusb/libusb - the one
    check that works no matter what's bound to the device: no driver, the
    WinUSB driver, KANALI running or not, COM port or not. This is what
    Device Manager's Ports list can't show, and it's the only way left to
    tell a real hardware/cabling problem apart from "something's just
    holding the device open" once find_device() (see above) comes back
    empty, since that function only ever looks at COM ports.

    Never raises for the ordinary failure cases (nothing plugged in,
    something else has it open) - those are exactly the cases this exists
    to tell apart, and each comes back as reason/likely_cause text instead
    of an exception. Only genuinely unexpected failures propagate.
    """
    try:
        import usb.core
        import usb.util
    except ImportError:
        return {
            "ok": False,
            "reason": "pyusb not installed (pip install pyusb libusb-package)",
        }

    backend = None
    try:
        import libusb_package
        backend = libusb_package.get_libusb1_backend()
    except Exception as exc:
        log.debug("libusb_package backend unavailable, falling back to pyusb's default search: %s", exc)

    try:
        find_kwargs = {"find_all": True, "idVendor": TRYX_VENDOR_ID}
        if backend is not None:
            find_kwargs["backend"] = backend
        devices = list(usb.core.find(**find_kwargs))
    except usb.core.NoBackendError:
        return {
            "ok": False,
            "reason": "no libusb backend found (pip install libusb-package, or "
                      "put libusb-1.0.dll next to python.exe)",
        }

    if not devices:
        return {
            "ok": False,
            "reason": (
                f"No USB device with vendor id 0x{TRYX_VENDOR_ID:04x} (Tryx) is visible "
                "on the bus at all. This is below any driver or software - Windows isn't "
                "seeing the device electrically. Points at the physical connection: the "
                "panel's internal USB 2.0 header cable to the motherboard (reseat it, try "
                "a different header), or its power connector, not KANALI/drivers/software."
            ),
            "devices": [],
        }

    result = {"ok": True, "devices": []}
    for dev in devices:
        info = {
            "vendor_id": f"0x{dev.idVendor:04x}",
            "product_id": f"0x{dev.idProduct:04x}",
            "is_panorama": dev.idProduct == PANORAMA_PRODUCT_ID,
            "bus": dev.bus,
            "address": dev.address,
        }
        try:
            info["manufacturer"] = usb.util.get_string(dev, dev.iManufacturer) if dev.iManufacturer else None
            info["product"] = usb.util.get_string(dev, dev.iProduct) if dev.iProduct else None
        except Exception as exc:
            info["manufacturer"] = None
            info["product"] = None
            info["string_read_error"] = str(exc)
        try:
            cfg = dev.get_active_configuration()
            info["accessible"] = True
            info["num_interfaces"] = cfg.bNumInterfaces
            info["endpoints"] = [
                f"0x{ep.bEndpointAddress:02x}" for intf in cfg for ep in intf
            ]
        except Exception as exc:
            # Broad except deliberately: libusb's Windows backend raises
            # NotImplementedError (not usb.core.USBError) when the bound
            # driver isn't one it can open (needs WinUSB/libusb0/libusbK
            # specifically), which is the expected failure mode here, not
            # a bug - it means the device enumerated but nothing suitable
            # is bound to talk to it.
            info["accessible"] = False
            info["access_error"] = f"{type(exc).__name__}: {exc}"
            lowered = str(exc).lower()
            if "access" in lowered or "denied" in lowered or "busy" in lowered:
                info["likely_cause"] = (
                    "Another process already has it open exclusively - most likely KANALI's "
                    "UDB.exe (even a stuck/background instance after 'closing' the app). Check "
                    "Task Manager and end it, then retry."
                )
            elif "not supported" in lowered or "not implemented" in lowered:
                info["likely_cause"] = (
                    "Device enumerates (Windows sees the VID/PID) but no WinUSB/libusb0/libusbK "
                    "compatible driver is currently bound to this interface, so libusb can't open "
                    "it - not a cabling problem. In Device Manager, find this device (search "
                    "hardware ID VID_391A&PID_1011 under View > Devices by container, or check "
                    "'Other devices'/'Universal Serial Bus devices'), then either: (a) right-click "
                    "> Uninstall device (tick 'delete the driver'), unplug/replug or reboot, and "
                    "let Windows/KANALI reinstall it fresh, or (b) use Zadig (zadig.akeo.ie) to "
                    "force-bind WinUSB to this specific device so this tool can open it directly "
                    "without KANALI at all. Worth trying (a) first since KANALI's own logs show "
                    "it failing the same way - the current driver binding may just be stuck."
                )
            else:
                info["likely_cause"] = (
                    "Device is visible but its configuration couldn't be read - may need the "
                    "WinUSB driver (re)bound via Zadig, or running this as Administrator."
                )
        result["devices"].append(info)
    return result


# -- raw USB (WinUSB bulk) transport -----------------------------------------
#
# The confirmed-correct transport for this panel on Windows. Requires WinUSB
# actually bound to the device's interface first (Windows binds its generic
# usbprint.sys class driver by default on this hardware, per this session's
# Device Manager check - use Zadig, https://zadig.akeo.ie, "List All
# Devices", target driver WinUSB, Replace Driver). usb_probe() reports which
# state a given machine is in.
#
# Endpoints confirmed against real hardware this session: one bulk OUT
# (0x01) and one bulk IN (0x81) on the panel's single interface - the same
# two-endpoint shape a CDC-ACM data interface would have, just addressed
# directly instead of through a COM port. The frame protocol riding on top
# is unverified against this specific panel until a real handshake succeeds.

class PanoramaUSB(_PanoramaBase):
    def __init__(self, timeout: float = 1.0):
        super().__init__(timeout)
        self.dev = None
        self.ep_out = None
        self.ep_in = None

    def connect(self) -> bool:
        import usb.core
        import usb.util

        backend = None
        try:
            import libusb_package
            backend = libusb_package.get_libusb1_backend()
        except Exception as exc:
            log.debug("libusb_package backend unavailable, falling back to pyusb's default search: %s", exc)

        find_kwargs = {"idVendor": TRYX_VENDOR_ID, "idProduct": PANORAMA_PRODUCT_ID}
        if backend is not None:
            find_kwargs["backend"] = backend
        dev = usb.core.find(**find_kwargs)
        if dev is None:
            log.warning("panel not found on the USB bus (VID 0x391A:0x1011) - "
                        "see usb_probe() for what's actually visible")
            return False

        try:
            dev.set_configuration()
        except usb.core.USBError as exc:
            log.debug("set_configuration (often harmless if already configured): %s", exc)

        cfg = dev.get_active_configuration()
        intf = cfg[(0, 0)]
        ep_out = usb.util.find_descriptor(
            intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
        ep_in = usb.util.find_descriptor(
            intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
        if ep_out is None or ep_in is None:
            log.warning("panel found on the bus but its interface has no bulk IN/OUT pair")
            return False

        self.dev = dev
        self.ep_out = ep_out
        self.ep_in = ep_in
        self._try_cdc_line_coding(intf.bInterfaceNumber)
        return True

    def _try_cdc_line_coding(self, interface_number: int):
        """Sends the two standard USB CDC control requests a real serial
        driver issues before any data flows - SET_LINE_CODING (115200 8N1)
        and SET_CONTROL_LINE_STATE asserting DTR+RTS.

        Reasoning, not a guess pulled from nowhere: reed-tpse's Linux build
        opens /dev/ttyACM0 through the OS's normal serial API, which sends
        exactly these two control requests automatically as a side effect
        of "opening the port" - before that happens, many USB-UART bridge
        firmwares simply refuse to send anything on the data pipe. Our raw
        WinUSB path skips that entirely by going straight to bulk transfers,
        which is one explanation (not yet confirmed) for the panel staying
        completely silent to both real requests and passive listening this
        session. Harmless if the device doesn't care: failure here is
        logged and swallowed, connect() proceeds regardless.
        """
        import usb.core
        line_coding = (115200).to_bytes(4, "little") + bytes((0, 0, 8))  # 1 stop bit, no parity, 8 data bits
        try:
            # bmRequestType 0x21 = host-to-device, class, interface
            self.dev.ctrl_transfer(0x21, 0x20, 0, interface_number, line_coding)  # SET_LINE_CODING
            self.dev.ctrl_transfer(0x21, 0x22, 0x0003, interface_number, None)     # SET_CONTROL_LINE_STATE, DTR+RTS
            log.debug("CDC line-coding control requests accepted")
        except usb.core.USBError as exc:
            log.debug("CDC line-coding control requests rejected/unsupported (may be harmless): %s", exc)

    def disconnect(self):
        if self.dev is not None:
            import usb.util
            usb.util.dispose_resources(self.dev)
        self.dev = None
        self.ep_out = None
        self.ep_in = None

    @property
    def connected(self) -> bool:
        return self.dev is not None

    def _write(self, frame: bytes):
        self.ep_out.write(frame, timeout=max(int(self.timeout * 1000), 1000))

    def _read_frame(self, timeout_s: float) -> bytes:
        import usb.core
        deadline = time.monotonic() + timeout_s
        buf = bytearray()
        packet_size = self.ep_in.wMaxPacketSize or 512
        while time.monotonic() < deadline:
            try:
                chunk = self.ep_in.read(packet_size, timeout=250)
            except usb.core.USBError as exc:
                if "timeout" in str(exc).lower() or "timed out" in str(exc).lower():
                    continue
                raise
            if chunk:
                buf += bytes(chunk)
                if len(buf) >= 2 and buf[0] == FRAME_MARKER and buf[-1] == FRAME_MARKER:
                    break
        return bytes(buf)


def find_usb_panel(timeout: float = 1.0) -> "PanoramaUSB | None":
    """Connect to the panel over WinUSB and return it ready to use, or None.

    Fallback/diagnostic path only - confirmed this session that KANALI's
    own installer sets up usbprint.sys for this device, not WinUSB, so
    using this requires replacing that driver via Zadig, which breaks
    KANALI's own access. Prefer find_print_panel() below, which uses the
    vendor-intended driver and doesn't require touching anything in Device
    Manager. Does not itself distinguish *why* it failed (nothing on the
    bus vs. wrong driver still bound) - call usb_probe() for that."""
    panel = PanoramaUSB(timeout=timeout)
    if not panel.connect():
        return None
    return panel


# -- usbprint.sys transport (CreateFile/ReadFile/WriteFile) -----------------
#
# The vendor-intended transport on Windows, confirmed this session: KANALI's
# own installer sets up Windows' built-in USB Printing Support class driver
# (usbprint.sys) for this device, not WinUSB - a known technique some
# vendors use to get raw bulk I/O without needing a signed custom driver,
# since usbprint.sys is inbox and exposes a plain CreateFile/ReadFile/
# WriteFile interface. libusb/pyusb (PanoramaUSB above) cannot open a
# device in this state at all - hence this separate, lower-level transport
# using ctypes directly against kernel32/setupapi.
#
# Unverified end to end against this panel - the device-path discovery
# (SetupAPI GUID_DEVINTERFACE_USBPRINT enumeration) follows the standard,
# widely-documented Windows pattern, but has not been run for real; treat
# this the same as everything else ported from documentation rather than
# a live test in this codebase.

import ctypes
from ctypes import wintypes

GUID_DEVINTERFACE_USBPRINT = "{28D78FAD-5A12-11D1-AE5B-0000F803A8C2}"

_DIGCF_PRESENT = 0x02
_DIGCF_DEVICEINTERFACE = 0x10
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x1
_FILE_SHARE_WRITE = 0x2
_OPEN_EXISTING = 3
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid_from_string(text: str) -> _GUID:
    """'{28D78FAD-5A12-11D1-AE5B-0000F803A8C2}' -> _GUID struct."""
    text = text.strip("{}")
    data1, data2, data3, data4a, data4b = text.split("-")
    data4_bytes = bytes.fromhex(data4a) + bytes.fromhex(data4b)
    return _GUID(int(data1, 16), int(data2, 16), int(data3, 16),
                 (ctypes.c_ubyte * 8)(*data4_bytes))


class _SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("InterfaceClassGuid", _GUID),
        ("Flags", wintypes.DWORD),
        ("Reserved", ctypes.POINTER(ctypes.c_ulong)),
    ]


def find_usbprint_path(vendor_id: int = TRYX_VENDOR_ID,
                       product_id: int = PANORAMA_PRODUCT_ID) -> str | None:
    """Find the Windows device path for a usbprint.sys-bound device with the
    given VID/PID, via the standard SetupAPI device-interface enumeration
    (SetupDiGetClassDevs + SetupDiEnumDeviceInterfaces +
    SetupDiGetDeviceInterfaceDetail against GUID_DEVINTERFACE_USBPRINT) -
    the same mechanism Windows itself uses to hand this path to any
    application, including presumably KANALI. Returns None if nothing
    matching is currently present (wrong driver bound, or not plugged in)."""
    setupapi = ctypes.WinDLL("setupapi")
    guid = _guid_from_string(GUID_DEVINTERFACE_USBPRINT)

    devinfo = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(guid), None, None, _DIGCF_PRESENT | _DIGCF_DEVICEINTERFACE)
    if devinfo == _INVALID_HANDLE_VALUE:
        log.warning("SetupDiGetClassDevsW failed (gle=%d)", ctypes.get_last_error())
        return None

    match_fragment = f"vid_{vendor_id:04x}&pid_{product_id:04x}".lower()
    found_path = None
    try:
        index = 0
        while True:
            did = _SP_DEVICE_INTERFACE_DATA()
            did.cbSize = ctypes.sizeof(did)
            if not setupapi.SetupDiEnumDeviceInterfaces(
                    devinfo, None, ctypes.byref(guid), index, ctypes.byref(did)):
                break  # ERROR_NO_MORE_ITEMS - done

            required = wintypes.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                devinfo, ctypes.byref(did), None, 0, ctypes.byref(required), None)
            buf = ctypes.create_string_buffer(required.value)
            # SP_DEVICE_INTERFACE_DETAIL_DATA_W: DWORD cbSize followed by a
            # WCHAR[] DevicePath. cbSize itself must be set to the size of
            # just the struct's fixed header (DWORD + one WCHAR) - 6 bytes
            # logically, but a long-documented Windows quirk requires 8 on
            # 64-bit and 5 (well, 6) on 32-bit for this call to succeed
            # regardless of the buffer's real total size. Hardcoding 8 since
            # this project only targets 64-bit Windows.
            ctypes.memmove(buf, ctypes.byref(wintypes.DWORD(8)), ctypes.sizeof(wintypes.DWORD))
            ok = setupapi.SetupDiGetDeviceInterfaceDetailW(
                devinfo, ctypes.byref(did), buf, required, None, None)
            if ok:
                path = ctypes.wstring_at(ctypes.addressof(buf) + ctypes.sizeof(wintypes.DWORD))
                if match_fragment in path.lower():
                    found_path = path
                    break
            index += 1
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(devinfo)

    return found_path


class PanoramaPrint(_PanoramaBase):
    """Talks to the panel via Windows' native CreateFile/ReadFile/WriteFile
    on its usbprint.sys device path - the same access model KANALI itself
    uses, so this does not require touching any driver in Device Manager
    (unlike PanoramaUSB), but does still conflict with KANALI if it's
    running at the same time, same as any single-open-handle device."""

    def __init__(self, timeout: float = 1.0):
        super().__init__(timeout)
        self._handle = None
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def connect(self) -> bool:
        path = find_usbprint_path()
        if path is None:
            log.warning("no usbprint.sys device found for VID 0x391A:0x1011 - "
                        "wrong driver bound, or not plugged in")
            return False
        handle = self._kernel32.CreateFileW(
            path, _GENERIC_READ | _GENERIC_WRITE,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE, None, _OPEN_EXISTING, 0, None)
        if handle == _INVALID_HANDLE_VALUE:
            log.warning("CreateFileW failed on %s (gle=%d) - probably still open "
                        "elsewhere (KANALI's UDB.exe?)", path, ctypes.get_last_error())
            return False
        self._handle = handle
        return True

    def disconnect(self):
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None

    @property
    def connected(self) -> bool:
        return self._handle is not None

    def _write(self, frame: bytes):
        written = wintypes.DWORD(0)
        ok = self._kernel32.WriteFile(self._handle, frame, len(frame), ctypes.byref(written), None)
        if not ok:
            raise PanoramaError(f"WriteFile failed (gle={ctypes.get_last_error()})")

    def _read_frame(self, timeout_s: float) -> bytes:
        deadline = time.monotonic() + timeout_s
        buf = bytearray()
        chunk = ctypes.create_string_buffer(512)
        read = wintypes.DWORD(0)
        while time.monotonic() < deadline:
            ok = self._kernel32.ReadFile(self._handle, chunk, 512, ctypes.byref(read), None)
            if ok and read.value:
                buf += chunk.raw[:read.value]
                if len(buf) >= 2 and buf[0] == FRAME_MARKER and buf[-1] == FRAME_MARKER:
                    break
            elif not ok:
                # A synchronous ReadFile with nothing available typically
                # just blocks rather than erroring on a real device; this
                # branch is here mainly so a genuine I/O error doesn't spin
                # silently for the full timeout.
                log.debug("ReadFile returned an error (gle=%d)", ctypes.get_last_error())
                break
        return bytes(buf)


def find_print_panel(timeout: float = 1.0) -> "PanoramaPrint | None":
    """Connect to the panel via its usbprint.sys device path and return it
    ready to use, or None - the transport to actually use on Windows, since
    it matches what KANALI's own installer sets up (see module docstring).
    """
    panel = PanoramaPrint(timeout=timeout)
    if not panel.connect():
        return None
    return panel


# -- media transfer over ADB -----------------------------------------------

class Media:
    """Wraps adb.exe - subprocess only, same pattern as hub.py's liquidctl
    wrapper. Requires adb on PATH and the panel already authorised as an ADB
    device (Windows may need Google's ADB driver if it doesn't enumerate on
    its own - unverified for this specific panel)."""

    def __init__(self, adb_path: str = "adb"):
        self.adb_path = adb_path

    def _run(self, *args: str) -> str | None:
        try:
            result = subprocess.run([self.adb_path, *args], capture_output=True,
                                     text=True, timeout=30)
        except FileNotFoundError:
            log.error("adb not found on PATH")
            return None
        except subprocess.TimeoutExpired:
            log.error("adb timed out: %s", " ".join(args))
            return None
        if result.returncode != 0:
            log.warning("adb failed (%s): %s", " ".join(args), result.stderr.strip())
        return result.stdout

    def is_connected(self) -> bool:
        out = self._run("devices")
        if out is None:
            return False
        return any("\tdevice" in line for line in out.splitlines())

    def push(self, local_path: str, remote_name: str) -> bool:
        out = self._run("push", local_path, MEDIA_PATH + remote_name)
        return out is not None and ("pushed" in out or "1 file" in out)

    def list_media(self) -> list[str]:
        out = self._run("shell", "ls", "-1", MEDIA_PATH)
        if out is None or "No such file" in out or "error:" in out:
            return []
        return [line.strip() for line in out.splitlines() if line.strip()]

    def delete(self, remote_name: str) -> bool:
        out = self._run("shell", "rm", MEDIA_PATH + remote_name)
        return out is not None
