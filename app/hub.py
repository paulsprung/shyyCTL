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
- 'initialize's accessory detection reports "Unknown" via the CLI, never an
  LED count - the CLI only prints Hue2Accessory's name, and this hub's
  F120X/F240X/F360X units use IDs liquidctl doesn't have names for yet.
  The raw numeric ID is still there via the Python API (accessory_ids()) -
  once a real ID is matched to a model by hand, it's a genuine
  auto-detection key, not a permanent dead end.
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
import shutil
import subprocess
import sys
import threading
import time

from app.procflags import NO_WINDOW

log = logging.getLogger("hub")

FIRMWARE_MODES = ("off", "fixed", "fading", "spectrum-wave",
                   "covering-marquee", "super-rainbow")

# Windows consoles default to cp1252; forcing utf-8 on the child avoids a
# UnicodeEncodeError crash when liquidctl prints box-drawing characters.
_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}

# One liquidctl process at a time, ever. Every call here is a separate
# liquidctl subprocess opening the hub's HID interface on its own, and
# several callers run concurrently (the Fans/Status poll, the fan-curve
# loop setting duties, a scan, a manual slider, more than one browser
# tab) - two processes talking to the same HID device at once read each
# other's replies, which is exactly liquidctl's "missing messages
# (attempts=12, missing=1)" failure seen hundreds of times in real logs.
_LIQUIDCTL_LOCK = threading.Lock()

_liquidctl_cmd: list[str] | None = None


def _liquidctl_command() -> list[str]:
    """liquidctl on PATH if there is one (unchanged behaviour for anyone
    who installed it themselves), otherwise the copy bundled into this
    build: the installed exe re-invokes itself with --liquidctl (see the
    top of main.py), a dev checkout uses `python -m liquidctl`. Before
    this, the installed app silently depended on a separate Python +
    liquidctl install being on PATH - without one, every fan call failed
    with "liquidctl not found" even though liquidctl is bundled."""
    global _liquidctl_cmd
    if _liquidctl_cmd is None:
        found = shutil.which("liquidctl")
        if found:
            _liquidctl_cmd = [found]
        elif getattr(sys, "frozen", False):
            _liquidctl_cmd = [sys.executable, "--liquidctl"]
        else:
            _liquidctl_cmd = [sys.executable, "-m", "liquidctl"]
        log.info("using liquidctl via: %s", " ".join(_liquidctl_cmd))
    return _liquidctl_cmd


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
        # status() bookkeeping - see status(). _status_gen is bumped by every
        # set_fan(), so no reading taken before a duty change is ever
        # handed out after it.
        self._status_lock = threading.Lock()
        self._status_gen = 0
        self._status_cache: tuple[float, int, list[dict]] | None = None
        self._status_last: tuple[float, int, list[dict]] | None = None

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
        self._status_gen += 1  # the next status()/rpm() must see the new speed
        self._status_cache = None
        if self._run("set", channel, "speed", str(duty), match=self.match) is None:
            raise HubError(f"Could not set {channel} to {duty}%.")
        return duty

    def status(self) -> list[dict]:
        """Confirmed against a real run: liquidctl's own control_hub driver
        can fail outright with "missing messages (attempts=12, missing=1)"
        - a single dropped HID reply that its own 12-attempt internal read
        never recovers from - and this was happening on essentially every
        poll, not as an occasional blip. A fresh subprocess re-opens the
        HID handle from scratch each time, which is a much bigger reset
        than anything liquidctl retries internally, so a couple of retries
        here (not just liquidctl's own) are worth it before actually
        reporting no readings."""
        # Several pollers ask for this every few seconds (the Fans/Status
        # pages, one per open tab) - each answer used to cost a whole
        # liquidctl process. A reading a second or two old is still current
        # for a fan, and set_fan() clears this so a fresh duty is never
        # masked (scan.py's start-duty probing depends on that).
        #
        # Callers that arrive while a read is already running wait for it
        # and share its answer - failed reads included - instead of each
        # queueing a full read of their own. While an effect runs, OpenRGB
        # keeps the hub busy and reads fail with "missing messages", each
        # one taking several seconds of retries; with every poll queueing
        # its own read, waiting calls piled up without bound and tied up
        # the worker threads everything else (effect start, OpenRGB,
        # Panorama) runs on - effects showed "running" but never moved.
        asked = time.monotonic()
        with self._status_lock:
            gen = self._status_gen
            cached = self._status_cache
            if cached is not None and cached[1] == gen and time.monotonic() - cached[0] < 1.5:
                return [dict(r) for r in cached[2]]
            last = self._status_last
            if last is not None and last[1] == gen and last[0] >= asked:
                return [dict(r) for r in last[2]]
            out = self._run("status", "--verbose", match=self.match, retries=3, retry_delay=0.3)
            readings = self._parse_status(out) if out is not None else []
            done = time.monotonic()
            self._status_last = (done, gen, readings)
            if readings:
                self._status_cache = (done, gen, readings)
            return [dict(r) for r in readings]

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

    # -- accessory identification -------------------------------------------

    def accessory_ids(self) -> dict[str, int]:
        """Real numeric accessory ID per populated LED channel, straight
        from liquidctl's Python API rather than the CLI.

        The CLI only ever prints Hue2Accessory's *name* ('Unknown' for any
        ID not in its table - confirmed this is exactly what our F120X/
        F240X/F360X units hit, since liquidctl's known IDs are for older
        RGB/RGB Core fans, not the X series). The underlying numeric ID is
        still read from the hardware and is model-specific - once a real ID
        here is matched to a known fan (once, by hand, via Identify), every
        future scan can recognise that same model automatically, without
        asking again. This is the actual auto-detection path; the CLI never
        exposed the piece needed for it.
        """
        from liquidctl.driver.control_hub import ControlHub

        ids: dict[str, int] = {}
        with _LIQUIDCTL_LOCK:
            for device in ControlHub.find_supported_devices():
                if self.match.lower() not in device.description.lower():
                    continue
                device.connect()
                try:
                    for key, value, _unit in device.initialize():
                        found = re.match(r"LED (\d+) accessory 1$", key)
                        if found:
                            ids[f"led{found.group(1)}"] = getattr(value, "value", value)
                finally:
                    device.disconnect()
                break
        return ids

    # -- internals -----------------------------------------------------

    def _run(self, *args: str, match: str | None = None,
             retries: int = 1, retry_delay: float = 0.3) -> str | None:
        """retries>1 re-runs the whole subprocess (not just something
        internal to liquidctl) on a non-zero exit - a fresh process
        re-opens the HID handle from scratch, a bigger reset than
        anything liquidctl retries on its own. Only status() asks for
        more than the default 1 (see its docstring for why); every other
        caller keeps the original one-shot behaviour.

        Returns None on a non-zero exit (after exhausting retries), not
        just on FileNotFoundError/TimeoutExpired - every caller already
        treats None as "this failed" (set_fan/set_color raise HubError,
        connect() reports last_error), so silently returning a failed
        command's stdout instead of None meant those checks could never
        actually catch a real liquidctl error, only a missing binary or a
        hang."""
        cmd = _liquidctl_command() + (["-m", match] if match else []) + list(args)
        for attempt in range(retries):
            try:
                # encoding="utf-8" explicitly, not just text=True: the child
                # is forced to write utf-8 via PYTHONIOENCODING below, but
                # without this the parent decodes its stdout using the
                # platform default (cp1252 on Windows) and mangles every
                # box-drawing character, which then breaks every
                # downstream regex silently.
                with _LIQUIDCTL_LOCK:
                    result = subprocess.run(cmd, capture_output=True, encoding="utf-8",
                                             errors="replace", timeout=30, env=_ENV,
                                             creationflags=NO_WINDOW)
            except FileNotFoundError:
                log.error("liquidctl not found (%s)", cmd[0])
                return None
            except subprocess.TimeoutExpired:
                log.error("liquidctl timed out: %s", " ".join(cmd))
                return None
            if result.returncode == 0:
                return result.stdout
            log.warning("liquidctl failed (%s): %s", " ".join(cmd), result.stderr.strip())
            if attempt < retries - 1:
                time.sleep(retry_delay)
        return None

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
