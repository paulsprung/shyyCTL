"""Tryx Panorama display - USB CDC-ACM serial control channel + ADB for media.

Protocol reverse-engineered by github.com/fadli0029/reed-tpse (MIT licence),
read directly from its C++ source this session and ported here. That project
verified its work against the Panorama SE (2160x1080) and 360 (2240x1080) -
this project's actual hardware is the 240mm 2-fan variant, which nobody has
walked yet. The firmware family is presumably shared, but treat every
command in this file as unverified until it's actually run against this
specific panel - the same rule as everywhere else in this codebase.

Two separate channels, confirmed from reed-tpse's own README, not guessed:
- USB CDC-ACM serial (a COM port on Windows, /dev/ttyACM* on Linux):
  control commands - handshake, brightness, screen config. 115200 baud,
  8N1, no flow control.
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


# -- serial control channel ------------------------------------------------

class Panorama:
    def __init__(self, port: str, timeout: float = 1.0):
        self.port_name = port
        self.timeout = timeout
        self.ser = None
        self._seq = 0

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

    def send_command(self, request_state: str, cmd_type: str, content: str = "",
                     wait_response: bool = True) -> Response | None:
        if not self.connected:
            raise PanoramaError("not connected")
        self._seq += 1
        frame = build_frame(request_state, cmd_type, content, "1", self._seq)
        self.ser.write(frame)
        self.ser.flush()
        if not wait_response:
            return None
        time.sleep(0.5)  # matches reed-tpse's fixed pre-read delay
        raw = self._read_frame(timeout_s=1.0)
        if not raw:
            return None
        return parse_response(raw)

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


def find_device(timeout: float = 1.0) -> str | None:
    """Try every serial port on the system, handshake each, return the first
    one that answers with a real productId - mirrors reed-tpse's
    find_device(), which scans /dev/ttyACM* on Linux; ports on Windows have
    no equivalent fixed naming, so every port has to be tried."""
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
