"""The NZXT Control Hub: DirectHub (bottom of this file, the default) keeps
one connection open and drives fans and per-LED colour itself; Hub is the
older liquidctl-CLI wrapper, used when config.yaml's hub.control is
"openrgb" (hub LEDs then go through OpenRGB, fans through liquidctl).

Hub - subprocess + CLI rather than liquidctl's Python API: the ControlHub driver
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

# liquidctl's Control Hub driver reads at most 12 HID reports while
# looking for the one it wants (status: the 0x67 0x02 fan report) and then
# fails with "missing messages (attempts=12, missing=1)". While a
# host-driven effect runs, OpenRGB streams LED frames to this same hub and
# the extra reports that traffic produces fill those 12 reads before the
# fan report comes round - fan readings failed almost every time an effect
# was on. Reading further is all it takes; each read returns as soon as a
# report arrives, so a larger budget costs nothing when the hub is quiet.
HUB_READ_ATTEMPTS = 96


def patch_liquidctl_reads() -> None:
    """Raise the Control Hub driver's read budget (see HUB_READ_ATTEMPTS).
    Applied in this process (accessory_ids) and in every bundled liquidctl
    child (main.py's --liquidctl entry) - not possible for a separately
    installed liquidctl on PATH, which is why the bundled copy is preferred
    in the installed app (see _liquidctl_command)."""
    try:
        from liquidctl.driver.control_hub import ControlHub
    except Exception:
        return
    if getattr(ControlHub, "_MAX_READ_ATTEMPTS", 0) < HUB_READ_ATTEMPTS:
        ControlHub._MAX_READ_ATTEMPTS = HUB_READ_ATTEMPTS


_liquidctl_cmd: list[str] | None = None


def _bundled_liquidctl_works(cmd: list[str]) -> bool:
    try:
        result = subprocess.run(cmd + ["--version"], capture_output=True, encoding="utf-8",
                                errors="replace", timeout=30, env=_ENV, creationflags=NO_WINDOW)
    except Exception as exc:
        log.warning("bundled liquidctl unusable (%s)", exc)
        return False
    if result.returncode != 0:
        log.warning("bundled liquidctl unusable (exit %s): %s",
                    result.returncode, (result.stderr or result.stdout).strip()[-300:])
        return False
    return True


def _liquidctl_command() -> list[str]:
    """Installed app: the copy bundled into this build - the exe
    re-invokes itself with --liquidctl (see the top of main.py) - as long
    as that actually runs, since only it carries patch_liquidctl_reads();
    a liquidctl on PATH is the fallback. Dev checkout: liquidctl on PATH
    first (whatever the developer installed), else `python main.py
    --liquidctl`. Before the bundled copy existed, the installed app
    silently depended on a separate Python + liquidctl install being on
    PATH - without one, every fan call failed with "liquidctl not found"."""
    global _liquidctl_cmd
    if _liquidctl_cmd is None:
        found = shutil.which("liquidctl")
        if getattr(sys, "frozen", False):
            bundled = [sys.executable, "--liquidctl"]
            if found and not _bundled_liquidctl_works(bundled):
                _liquidctl_cmd = [found]
            else:
                _liquidctl_cmd = bundled
        elif found:
            _liquidctl_cmd = [found]
        else:
            main_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
            _liquidctl_cmd = [sys.executable, main_py, "--liquidctl"]
        log.info("using liquidctl via: %s", " ".join(_liquidctl_cmd))
    return _liquidctl_cmd


class HubError(Exception):
    pass


class Hub:
    drives_leds = False  # hub LEDs go through OpenRGB in this mode

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
            if readings:
                self._status_cache = (done, gen, readings)
            elif cached is not None and cached[1] == gen and done - cached[0] < 10:
                # A failed read right after a good one: the last good
                # reading (same duties, a few seconds old) beats the Fans
                # page blanking every rpm to "--" over a single hiccup.
                readings = cached[2]
            self._status_last = (done, gen, readings)
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
        patch_liquidctl_reads()

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


# -- direct control ----------------------------------------------------------
#
# Hue 2 per-LED packets, exactly as OpenRGB sends them to this hub (USB PID
# 0x2022 - "NZXT RGB & Fan Controller 2024" in OpenRGB, "NZXT Control Hub"
# in liquidctl; see OpenRGB's Controllers/NZXTHue2Controller/
# NZXTHue2Controller.cpp, SendDirect/SendApply/SetChannelLEDs):
#   0x22 0x10|group, 1<<channel, 0x00, then up to 20 LEDs as G,R,B
#   (group 0 = LEDs 0-19, group 1 = LEDs 20-39), then
#   0x22 0xA0, 1<<channel, 0x00, 0x01, 0, 0, 0x28, 0, 0, 0x80, 0, 0x32, 0, 0, 0x01
# to latch the new colours. No mode switch is needed first - direct
# packets take over from whatever firmware effect was running.
_REPORT_LENGTH = 64
_LEDS_PER_PACKET = 20
MAX_LEDS_PER_CHANNEL = 40
_APPLY_TAIL = [0x00, 0x01, 0x00, 0x00, 0x28, 0x00, 0x00, 0x80, 0x00, 0x32, 0x00, 0x00, 0x01]
# The hub sends this report by itself every ~0.5s once initialized
# (liquidctl's initialize() sets the interval): per-channel control mode at
# byte 16+i, rpm as little-endian u16 at 24+2i, duty at 40+i, noise at 56.
_FAN_REPORT = (0x67, 0x02)
_FAN_MODES = {1: "DC", 2: "PWM"}


class DirectHub:
    """The hub driven by shyyctl alone, over one connection that stays
    open - fan duty, fan readings and per-LED colour all go through it,
    the way NZXT CAM does it. OpenRGB no longer touches the hub at all in
    this mode (main.py switches off OpenRGB's detector for it), so there
    is no second program on the device any more, and no liquidctl process
    per call either.

    liquidctl's ControlHub driver still does discovery, initialize() (fan
    report interval, accessory IDs) and the fan duty packet. Everything
    that reads afterwards is this class's own: a background thread listens
    to the reports the hub sends on its own and keeps the latest fan
    report, so status() never waits on the device and never competes with
    LED writes for replies - the cause of liquidctl's "missing messages".

    Same public interface as Hub (the CLI wrapper above), plus
    set_leds(); drives_leds tells main.py which one it has. A lost
    connection (hub unplugged, USB reset) is reopened by the next call,
    at most every few seconds."""

    drives_leds = True
    _RECONNECT_EVERY_S = 5.0

    def __init__(self, match: str):
        self.match = match
        self.connected = False
        self.last_error: str | None = None
        self.description: str | None = None
        self.channels: dict[str, list[str]] = {"fan": [], "led": []}
        self.modes = list(FIRMWARE_MODES)
        self.supports_per_led = True
        self._dev = None
        self._accessory_ids: dict[str, int] = {}
        # Lock order, wherever more than one is held: _conn_lock, then
        # _write_lock, then _read_lock - never _conn_lock while holding
        # either of the others. The reader thread only ever holds
        # _read_lock (one short timed read at a time), so closing or
        # re-initializing the device under both I/O locks is guaranteed
        # not to pull the handle out from under a read or write in progress.
        self._conn_lock = threading.RLock()
        self._write_lock = threading.RLock()
        self._read_lock = threading.Lock()
        self._reader_stop: threading.Event | None = None
        self._reader: threading.Thread | None = None
        self._readings: tuple[float, list[dict]] | None = None
        self._first_report = threading.Event()
        self._next_reconnect = 0.0

    # -- lifecycle -----------------------------------------------------

    def connect(self) -> bool:
        with self._conn_lock:
            self._close()
            try:
                from liquidctl.driver.control_hub import ControlHub
            except Exception as exc:
                self.last_error = f"liquidctl could not be loaded: {exc}"
                return False
            patch_liquidctl_reads()
            try:
                dev = next((d for d in ControlHub.find_supported_devices()
                            if self.match.lower() in d.description.lower()), None)
            except Exception as exc:
                self.last_error = f"Could not enumerate USB devices: {exc}"
                return False
            if dev is None:
                self.last_error = f"No device matching '{self.match}'."
                return False
            try:
                dev.connect()
            except Exception as exc:
                self.last_error = f"Found the hub but could not open it: {exc}"
                return False
            try:
                init = dev.initialize()
            except Exception as exc:
                self._safe_disconnect(dev)
                self.last_error = f"Found the hub but could not initialize it: {exc}"
                return False
            self._dev = dev
            self.description = dev.description
            speed = getattr(dev, "_speed_channels", None) or {f"fan{i + 1}": None for i in range(5)}
            self.channels = {
                "fan": sorted(speed, key=lambda c: int(c[3:])),
                "led": self._apply_init(init),
            }
            self._readings = None
            self._first_report.clear()
            stop = threading.Event()
            self._reader_stop = stop
            self._reader = threading.Thread(target=self._read_loop, args=(dev, stop),
                                            name="hub-reader", daemon=True)
            self._reader.start()
            self.connected = True
            self.last_error = None
            log.info("hub: direct connection to %s open (fans %s, LEDs %s)",
                     self.description, self.channels["fan"], self.channels["led"])
            return True

    def disconnect(self):
        with self._conn_lock:
            self._close()

    def _close(self):
        """Stops the reader and closes the handle - only once no read or
        write can still be using it (see the lock-order note above)."""
        dev, self._dev = self._dev, None
        self.connected = False
        if self._reader_stop is not None:
            self._reader_stop.set()
        reader, self._reader = self._reader, None
        if dev is None:
            return
        with self._write_lock, self._read_lock:
            self._safe_disconnect(dev)
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1.0)

    @staticmethod
    def _safe_disconnect(dev):
        try:
            dev.disconnect()
        except Exception as exc:
            log.debug("hub: closing handle failed: %s", exc)

    def _lost(self, dev, reason: str):
        with self._conn_lock:
            if dev is not self._dev:
                return  # an older connection failing late - already replaced
            log.warning("hub: connection lost (%s) - reopening", reason)
            self.last_error = f"connection lost: {reason}"
            self._close()
            self._next_reconnect = 0.0

    def ensure_connected(self) -> bool:
        """Reopens a lost connection - at most every _RECONNECT_EVERY_S."""
        if self.connected:
            return True
        with self._conn_lock:
            if self.connected:
                return True
            if time.monotonic() < self._next_reconnect:
                return False
            self._next_reconnect = time.monotonic() + self._RECONNECT_EVERY_S
            return self.connect()

    def _apply_init(self, init) -> list[str]:
        """initialize()'s accessory list -> populated LED channels, plus
        the raw accessory ID of each channel's first accessory (see
        geometry.py: 0x20/0x22/0x24 are F120X/F240X/F360X)."""
        ids: dict[str, int] = {}
        leds: set[int] = set()
        for key, value, _unit in init or []:
            found = re.match(r"LED (\d+) accessory (\d+)$", key)
            if not found:
                continue
            leds.add(int(found.group(1)))
            if found.group(2) == "1":
                ids[f"led{found.group(1)}"] = getattr(value, "value", value)
        self._accessory_ids = ids
        return [f"led{n}" for n in sorted(leds)]

    # -- reading -------------------------------------------------------

    def _read_loop(self, dev, stop: threading.Event):
        hiddev = dev.device.hiddev
        try:
            hiddev.set_nonblocking(False)
        except Exception:
            pass
        while not stop.is_set():
            try:
                with self._read_lock:
                    if stop.is_set():
                        return
                    data = hiddev.read(max_length=_REPORT_LENGTH, timeout_ms=250)
            except Exception as exc:
                if not stop.is_set():
                    self._lost(dev, f"read failed: {exc}")
                return
            if len(data) > 56 and data[0] == _FAN_REPORT[0] and data[1] == _FAN_REPORT[1]:
                self._readings = (time.monotonic(), self._parse_fan_report(data))
                self._first_report.set()

    def _parse_fan_report(self, msg) -> list[dict]:
        readings = []
        for i, _channel in enumerate(self.channels["fan"]):
            n = i + 1
            readings.append({"key": f"Fan {n} speed",
                             "value": float(msg[24 + 2 * i] | (msg[25 + 2 * i] << 8)), "unit": "rpm"})
            readings.append({"key": f"Fan {n} duty", "value": float(msg[40 + i]), "unit": "%"})
            readings.append({"key": f"Fan {n} control mode",
                             "value": _FAN_MODES.get(msg[16 + i], "N/A"), "unit": ""})
        readings.append({"key": "Noise level", "value": float(msg[56]), "unit": "dB"})
        return sorted(readings, key=lambda r: r["key"])

    def status(self) -> list[dict]:
        """The latest fan report the hub sent by itself (~every 0.5s) -
        nothing is asked of the device here. Waits briefly only for the
        very first report after (re)connecting. Readings older than 10s
        mean reports stopped arriving: reported as none, not as current."""
        if not self.ensure_connected():
            return []
        if self._readings is None:
            self._first_report.wait(timeout=2.0)
        current = self._readings
        if current is None or time.monotonic() - current[0] > 10:
            return []
        return [dict(r) for r in current[1]]

    def rpm(self, channel: str) -> int | None:
        if not channel.startswith("fan"):
            return None
        for reading in self.status():
            if reading["key"].lower() == f"fan {channel[3:]} speed":
                return int(reading["value"])
        return None

    # -- writing -------------------------------------------------------

    def _with_device(self, action: str, fn):
        if not self.ensure_connected():
            raise HubError(f"Hub not connected - could not {action}: {self.last_error}")
        with self._write_lock:
            dev = self._dev
            if dev is None:
                raise HubError(f"Hub not connected - could not {action}.")
            try:
                return fn(dev)
            except HubError:
                raise
            except Exception as exc:
                error = exc
        # Outside _write_lock: _lost takes _conn_lock, which connect()
        # holds while it waits for _write_lock - the other order deadlocks.
        self._lost(dev, f"{action}: {error}")
        raise HubError(f"Could not {action}: {error}") from error

    def set_fan(self, channel: str, duty: int) -> int:
        duty = max(0, min(100, int(duty)))
        if channel not in self.channels["fan"]:
            raise HubError(f"Unknown fan channel '{channel}'.")
        self._with_device(f"set {channel} to {duty}%",
                          lambda dev: dev.set_fixed_speed(channel, duty))
        return duty

    def set_leds(self, channel: str, colors) -> None:
        """Per-LED colours for one LED channel ("led1".."led5"), in strip
        order, (r, g, b) each - at most MAX_LEDS_PER_CHANNEL."""
        found = re.fullmatch(r"led(\d+)", channel)
        if not found:
            raise HubError(f"Unknown LED channel '{channel}'.")
        bit = 1 << (int(found.group(1)) - 1)
        data: list[int] = []
        for r, g, b in list(colors)[:MAX_LEDS_PER_CHANNEL]:
            data += [int(g) & 0xFF, int(r) & 0xFF, int(b) & 0xFF]
        if not data:
            return
        packets = []
        for group in range((len(data) + 3 * _LEDS_PER_PACKET - 1) // (3 * _LEDS_PER_PACKET)):
            chunk = data[group * 3 * _LEDS_PER_PACKET:(group + 1) * 3 * _LEDS_PER_PACKET]
            packets.append([0x22, 0x10 | group, bit, 0x00] + chunk)
        packets.append([0x22, 0xA0, bit] + _APPLY_TAIL)

        def write(dev):
            for packet in packets:
                dev.device.write(packet + [0x00] * (_REPORT_LENGTH - len(packet)))
        self._note_writer(channel)
        self._with_device(f"set LEDs on {channel}", write)

    _writers: dict[str, tuple[int, str, float]] = {}
    _writers_warned = 0.0

    def _note_writer(self, channel: str):
        """Logs when two threads keep writing the same LED channel within
        a moment of each other - two animations fighting over the LEDs,
        which shows as wild flickering, should be visible in the log."""
        me = threading.current_thread()
        now = time.monotonic()
        last = self._writers.get(channel)
        if (last is not None and last[0] != me.ident and now - last[2] < 0.3
                and now - DirectHub._writers_warned > 30):
            DirectHub._writers_warned = now
            log.warning("hub: %s written by two threads at once (%s and %s) - two animations "
                        "are fighting over the LEDs", channel, last[1], me.name)
        self._writers[channel] = (me.ident, me.name, now)

    def set_color(self, channel: str, mode: str, colors: list[str]):
        """The hub's own firmware effects (via liquidctl's ControlHub
        driver, which sends the 2023-style packets this hub expects)."""
        if mode not in FIRMWARE_MODES:
            raise HubError(f"'{mode}' isn't supported on this hub - only {FIRMWARE_MODES}.")
        rgb = [[int(c.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)] for c in colors]
        self._with_device(f"set {channel} to {mode}",
                          lambda dev: dev.set_color(channel, mode, rgb))

    def blackout(self, except_channel: str | None):
        for ch in self.channels.get("led", []):
            self.set_color(ch, "fixed", ["ff0000"] if ch == except_channel else ["000000"])

    def known_accessory_ids(self) -> dict[str, int]:
        """Accessory IDs as last read - no device access."""
        return dict(self._accessory_ids)

    def accessory_ids(self) -> dict[str, int]:
        """Re-reads the accessory list (initialize() again, on the open
        connection - the reader is paused meanwhile, since initialize()
        reads its own replies), so a fan plugged in since connecting shows
        up; falls back to what was read at connect time if that fails."""
        if not self.ensure_connected():
            return dict(self._accessory_ids)
        with self._write_lock, self._read_lock:
            dev = self._dev
            if dev is not None:
                try:
                    self.channels["led"] = self._apply_init(dev.initialize())
                except Exception as exc:
                    log.warning("hub: re-reading accessories failed: %s", exc)
        return dict(self._accessory_ids)
