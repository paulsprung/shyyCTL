"""Wrapper around the liquidctl CLI for the NZXT Control Hub.

Subprocess + CLI rather than liquidctl's Python API: the ControlHub driver
only landed in liquidctl 1.16.0, and the CLI is what actually got exercised
against the real hardware. PYTHONIOENCODING is forced to utf-8 because
Windows consoles default to cp1252, which can't encode the box-drawing
characters in liquidctl's human-readable output and crashes mid-print
otherwise.

Facts confirmed against real hardware, not assumptions:
- 'initialize' must run once per power-on before fan control or lighting is
  reliable (this is documented liquidctl behaviour for the whole NZXT Smart
  Device family, not specific to this hub).
- 'initialize's accessory detection reports "Unknown", never an LED count -
  it's only useful here to learn which LED channels have anything at all.
- super-fixed (per-LED colour) is NOT supported by this driver's mode list:
  only off / fixed / fading / spectrum-wave / covering-marquee /
  super-rainbow - all whole-channel firmware effects, all persist after
  shyyctl closes.
- 'status' never reflects a previously-set colour - RGB is write-only. Any
  "current effect" the UI shows is only ever what shyyctl itself last sent.
"""

import logging
import os
import re
import subprocess

log = logging.getLogger("hub")

FIRMWARE_MODES = ("off", "fixed", "fading", "spectrum-wave",
                   "covering-marquee", "super-rainbow")

# Windows consoles default to cp1252; forcing utf-8 on the child avoids a
# UnicodeEncodeError crash when liquidctl prints box-drawing characters.
_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


class HubError(Exception):
    pass


class Hub:
    def __init__(self, match: str):
        self.match = match
        self.connected = False
        self.last_error: str | None = None
        self.description: str | None = None
        self.channels: dict[str, list[str]] = {"fan": [], "led": []}
        self.modes = list(FIRMWARE_MODES)
        self.supports_per_led = False  # confirmed: super-fixed unsupported

    # -- lifecycle -----------------------------------------------------

    def connect(self) -> bool:
        listing = self._run("list", "--verbose")
        if listing is None:
            self.last_error = "liquidctl not found - is it installed and on PATH?"
            self.connected = False
            return False
        if self.match.lower() not in listing.lower():
            self.last_error = f"No device matching '{self.match}'."
            self.connected = False
            return False
        self.description = self._parse_description(listing, self.match)

        init_out = self._run("initialize", match=self.match)
        if init_out is None:
            self.last_error = "Found the hub but could not initialize it."
            self.connected = False
            return False

        status_out = self._run("status", "--verbose", match=self.match) or ""
        self.channels = {
            "fan": self._parse_fan_channels(status_out),
            "led": self._parse_led_channels(init_out),
        }
        self.connected = True
        self.last_error = None
        return True

    def disconnect(self):
        self.connected = False

    # -- fans ------------------------------------------------------------

    def set_fan(self, channel: str, duty: int) -> int:
        duty = max(0, min(100, int(duty)))
        if self._run("set", channel, "speed", str(duty), match=self.match) is None:
            raise HubError(f"Could not set {channel} to {duty}%.")
        return duty

    def status(self) -> list[dict]:
        out = self._run("status", "--verbose", match=self.match)
        return self._parse_status(out) if out is not None else []

    def rpm(self, channel: str) -> int | None:
        if not channel.startswith("fan"):
            return None
        n = channel[3:]
        for reading in self.status():
            if reading["key"].lower() == f"fan {n} speed" and isinstance(reading["value"], float):
                return int(reading["value"])
        return None

    # -- lighting ----------------------------------------------------------

    def set_color(self, channel: str, mode: str, colors: list[str]):
        if mode not in FIRMWARE_MODES:
            raise HubError(f"'{mode}' isn't supported on this hub - only {FIRMWARE_MODES}.")
        if self._run("set", channel, "color", mode, *colors, match=self.match) is None:
            raise HubError(f"Could not set {channel} to {mode}.")

    def blackout(self, except_channel: str | None):
        """No true per-LED addressing (super-fixed is unsupported): this can
        only isolate a whole channel, never a single LED within one."""
        for ch in self.channels.get("led", []):
            self.set_color(ch, "fixed", ["ff0000"] if ch == except_channel else ["000000"])

    # -- internals -----------------------------------------------------

    def _run(self, *args: str, match: str | None = None) -> str | None:
        cmd = ["liquidctl"] + (["-m", match] if match else []) + list(args)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                     timeout=30, env=_ENV)
        except FileNotFoundError:
            log.error("liquidctl not found on PATH")
            return None
        except subprocess.TimeoutExpired:
            log.error("liquidctl timed out: %s", " ".join(cmd))
            return None
        if result.returncode != 0:
            log.warning("liquidctl failed (%s): %s", " ".join(cmd), result.stderr.strip())
        return result.stdout

    @staticmethod
    def _parse_description(listing: str, match: str) -> str | None:
        for line in listing.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("device") and ":" in stripped:
                name = stripped.split(":", 1)[1].strip()
                if match.lower() in name.lower():
                    return name
        return None

    @staticmethod
    def _parse_fan_channels(status_output: str) -> list[str]:
        """liquidctl always lists every physical fan channel in status,
        populated or not (confirmed: an empty channel shows "N/A" control
        mode and 0 rpm, but is still listed) - safer than hardcoding a count."""
        numbers = sorted(set(int(n) for n in re.findall(r"[Ff]an (\d+)\s", status_output)))
        return [f"fan{n}" for n in numbers]

    @staticmethod
    def _parse_led_channels(init_output: str) -> list[str]:
        """Only channels with a detected accessory appear here - empty
        channels are silently absent, which is exactly the signal used to
        tell a populated combo header from an empty one."""
        numbers = sorted(set(int(n) for n in re.findall(r"LED (\d+) accessory", init_output)))
        return [f"led{n}" for n in numbers]

    @staticmethod
    def _parse_status(status_output: str) -> list[dict]:
        readings = []
        for raw in status_output.splitlines():
            line = re.sub(r"^[\s├└│─]+", "", raw.rstrip())
            parts = re.split(r"\s{2,}", line.strip())
            if len(parts) < 2 or not parts[0]:
                continue
            key, value = parts[0], parts[1]
            unit = parts[2] if len(parts) > 2 else ""
            try:
                value = float(value)
            except ValueError:
                pass
            readings.append({"key": key, "value": value, "unit": unit})
        return readings
