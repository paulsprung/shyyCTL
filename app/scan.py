"""Hardware discovery.

Finds what is electrically present, across every controllable surface: the
NZXT Control Hub's fan/LED channels via liquidctl, and the motherboard/RAM
via OpenRGB. Where a unit is bolted, which way it faces, which model it is -
none of that is guessable from software and is left for the human via the
webUI's setup screen.

Confirmed against real hardware, not assumed: this hub's LED channel count
can't be read via liquidctl's CLI or OpenRGB's protocol-4 view - the CLI's
'initialize' only ever prints "Unknown" for accessory type, and OpenRGB
(when spoken over openrgb-python, which hard-caps at protocol 4) reports
these zones as 0/1 LEDs unconditionally, regardless of what's actually
plugged in - see app/openrgb_raw.py's docstring: this hub's zones are
flagged effects-only/manually-configurable-size, which OpenRGB's own
server deliberately zeroes out for any client below protocol 5, as an
intentional backward-compat shim, not a bug on either side. The CLI's
"Unknown" turned out to be a labelling gap, not a missing measurement
though: the hub does report a real numeric accessory ID per channel
(hub.accessory_ids(), via liquidctl's Python API), and this session found
the actual IDs used by the X-series fans (see geometry.ACCESSORY-related
lookups). Where that ID is recognised, the model and LED count are
certain, straight from hardware - no picking required. An unrecognised ID
still falls back to confidence "unknown" and asks the setup screen for a
manual pick, same as before.

Runs in a background thread and reports progress, because the fan probing
needs real settling time and the UI should show what is happening.
"""

import logging
import threading

from . import geometry
from .openrgb_raw import OpenRgbRaw

log = logging.getLogger("scan")

SETTLE = 1.4          # seconds to wait for a fan to reach a new duty
PROBE_DUTY = 50        # duty used to check whether a channel has anything on it
START_STEP = 5         # percent per step when hunting for the start duty
RESTORE_DUTY = 40      # safe duty to leave every fan at once the scan ends


class Scan:
    def __init__(self, hub, openrgb):
        self.hub = hub
        self.openrgb = openrgb
        self.running = False
        self.step = ""
        self.progress = 0.0
        self.results: list[dict] = []
        self.mobo: dict | None = None
        self.ram: list[dict] = []
        self.absent: list[str] = []
        self.error: str | None = None
        self._thread = None
        self._cancel = threading.Event()
        self._skip_rgb_devices = False

    # -- control -------------------------------------------------------

    MODES = ("full", "quick", "rgb")

    def start(self, mode: str = "full", skip_rgb_devices: bool = False) -> bool:
        """mode:
        - "full": spins every fan up, measures start duty + max RPM, then
          resizes/reports RGB.
        - "quick": spins every fan up to confirm presence and read RPM,
          skips curve measurement, then resizes/reports RGB.
        - "rgb": never touches a single fan - reads the hub's per-channel
          accessory IDs (no spin-up needed for that) and goes straight to
          resizing OpenRGB hub zones + reading mobo/RAM. Silent and fast,
          but "present" can only be inferred from whether an LED accessory
          was detected, not confirmed via tachometer - see
          _identify_channel_only's docstring.

        skip_rgb_devices: skips the motherboard/RAM check entirely (the
        hub's own LED zones still get resized/reported regardless - this
        only concerns OpenRGB's separate motherboard/DRAM devices). An
        escape hatch for a system where OpenRGB genuinely can't detect
        them (confirmed by a real report: PawnIO, OpenRGB's SMBus access
        driver, failing to initialize) - the trimmed retry budget in
        OpenRgbBridge now fails in a few seconds instead of minutes, but
        a few seconds still isn't free when it happens on every single
        scan and is never going to succeed on this particular machine.
        """
        if mode not in self.MODES:
            raise ValueError(f"unknown scan mode {mode!r} - must be one of {self.MODES}")
        if self.running:
            return False
        self._cancel.clear()
        self.results = []
        self.mobo = None
        self.ram = []
        self.absent = []
        self.error = None
        self._skip_rgb_devices = skip_rgb_devices
        self._thread = threading.Thread(
            target=self._run, args=(mode,), daemon=True, name="scan")
        self._thread.start()
        return True

    def cancel(self):
        self._cancel.set()

    def state(self) -> dict:
        return {
            "running": self.running,
            "step": self.step,
            "progress": round(self.progress, 3),
            "results": self.results,
            "mobo": self.mobo,
            "ram": self.ram,
            "absent": self.absent,
            "error": self.error,
        }

    def _wait(self, seconds: float) -> bool:
        """Sleep unless cancelled. Returns False if the scan should stop."""
        return not self._cancel.wait(seconds)

    # -- the scan ------------------------------------------------------

    def _run(self, mode: str):
        self.running = True
        try:
            self._scan(mode)
        except Exception as exc:
            log.exception("Scan failed")
            self.error = str(exc)
        finally:
            self.running = False
            self.step = "done" if not self.error else "failed"
            self.progress = 1.0
            if mode != "rgb":
                self._restore()

    def _scan(self, mode: str):
        self.step = "Looking for the hub"
        self.progress = 0.02
        if not self.hub.connected and not self.hub.connect():
            raise RuntimeError(self.hub.last_error or "No hub found.")

        fans = self.hub.channels.get("fan", [])
        leds = self.hub.channels.get("led", [])
        if not fans:
            raise RuntimeError(
                "The driver reports no fan channels. Either the hub is not "
                "supported by this liquidctl build, or CAM/CAM Service still "
                "holds it open.")

        # Motherboard/RAM (OpenRGB's own separate devices, not the hub) go
        # first now, before any fan gets spun up - this doesn't depend on
        # anything the per-channel probing below produces, and checking it
        # first means a broken OpenRGB/PawnIO setup (see skip_rgb_devices'
        # docstring) fails fast in a few seconds instead of only being
        # discovered after ~2 minutes of fan probing already ran.
        if self._skip_rgb_devices:
            self.step = "Skipped motherboard/RAM check"
            self.progress = 0.05
        else:
            self.step = "Checking motherboard and RAM lighting"
            self.progress = 0.05
            self._scan_openrgb()
        if self._cancel.is_set():
            return

        self.step = f"Reading channel list ({len(fans)} fan, {len(leds)} led)"
        self.progress = 0.1

        try:
            accessory_ids = self.hub.accessory_ids()
        except Exception as exc:
            log.warning("Could not read accessory IDs (falling back to manual model pick): %s", exc)
            accessory_ids = {}

        if mode == "rgb":
            self.step = "Identifying channels (no fan spin-up)"
            self.progress = 0.4
            for channel in fans:
                if self._cancel.is_set():
                    return
                entry = self._identify_channel_only(channel, accessory_ids)
                # No LED accessory detected at all (not even an
                # unrecognised one) - this mode never spins a fan, so an
                # accessory ID is the only confirmation signal it has, and
                # there isn't one. Excluded entirely rather than shown as
                # a dashes-and-"not measured" row, same reasoning as the
                # full/quick path's speed-based exclusion below, and
                # recorded in self.absent so the Fans page hides it too.
                # A channel WITH an accessory ID but an unrecognised model
                # still shows - there's a real, confirmed accessory there,
                # it just needs a manual model pick in Setup.
                if entry["led_channel"] is not None:
                    self.results.append(entry)
                else:
                    self.absent.append(channel)
        else:
            measure_curves = mode == "full"
            total = len(fans)
            for index, channel in enumerate(fans):
                if self._cancel.is_set():
                    return
                base = 0.12 + 0.6 * (index / total)
                entry = self._probe_channel(channel, measure_curves, base, 0.6 / total, accessory_ids)
                # A channel that never answered a tachometer probe (no rpm
                # at all - see _probe_channel's "nothing responded" path)
                # is not confirmed to have anything attached, LED accessory
                # or not - excluded entirely rather than carried forward as
                # an "empty" entry, so Setup/RGB never get a fan unit for a
                # channel nothing ever confirmed responds to speed control.
                # Recorded in self.absent instead, purely so the Fans page
                # can hide a duty slider for something confirmed not there
                # (rather than every unscanned channel losing its slider,
                # which would be wrong for a channel nobody's checked yet).
                if entry["present"]:
                    self.results.append(entry)
                else:
                    self.absent.append(channel)

        # Only when OpenRGB drives the hub's LEDs: a DirectHub writes
        # exactly as many colours as the identified model has, there are
        # no OpenRGB zones to size (OpenRGB doesn't even see the hub then).
        if not getattr(self.hub, "drives_leds", False):
            self.step = "Sizing OpenRGB LED zones"
            self.progress = 0.75
            self._resize_hub_zones()

        self.step = "Done"
        self.progress = 1.0

    def _identify_channel_only(self, channel, accessory_ids) -> dict:
        """The "rgb" scan mode's per-channel step - identical model/LED
        identification as _probe_channel, but never calls hub.set_fan(),
        so it never spins anything up or makes noise. The tradeoff: unlike
        the full/quick paths, "present" here can't be confirmed by a
        tachometer reading - it's inferred from whether the hub reports an
        LED accessory on this channel at all. For NZXT's X-series fans
        that's a reliable stand-in (the accessory ID literally can't exist
        without a physical unit attached), but a channel with no LED
        accessory is genuinely unknown here (empty, or a fan without one),
        not confirmed empty the way a real tachometer check would be."""
        number = channel[3:] if channel.startswith("fan") else None
        led_channel = f"led{number}" if number else None
        has_led = led_channel in self.hub.channels.get("led", [])

        model = None
        if has_led and led_channel in accessory_ids:
            model = geometry.identify_by_accessory_id(accessory_ids[led_channel])

        entry = {
            "fan_channel": channel,
            "led_channel": led_channel if has_led else None,
            "leds": model.leds if model else None,
            "model": model.name if model else None,
            "present": True if has_led else None,
            "start_duty": None,
            "max_rpm": None,
            "rpm_at_probe": None,
            "confidence": "certain" if model else ("unknown" if has_led else "none"),
            "needs": [],
        }
        if has_led and not model:
            raw_id = accessory_ids.get(led_channel)
            entry["needs"].append(
                f"unrecognised accessory id {raw_id!r} - pick the fan model in "
                "setup so the real count can be sent to OpenRGB" if raw_id is not None
                else "LED count can't be auto-detected on this hub - pick the fan "
                     "model in setup so the real count can be sent to OpenRGB")
        elif not has_led:
            entry["needs"].append(
                "no LED accessory detected - run a full or quick scan to confirm "
                "whether anything is actually connected here")
        entry["needs"].extend(["position", "name"])
        return entry

    def _probe_channel(self, channel, measure_curves, base, budget, accessory_ids) -> dict:
        number = channel[3:] if channel.startswith("fan") else None
        led_channel = f"led{number}" if number else None
        has_led = led_channel in self.hub.channels.get("led", [])

        model = None
        if has_led and led_channel in accessory_ids:
            model = geometry.identify_by_accessory_id(accessory_ids[led_channel])

        entry = {
            "fan_channel": channel,
            "led_channel": led_channel if has_led else None,
            "leds": model.leds if model else None,
            "model": model.name if model else None,
            "present": False,
            "start_duty": None,
            "max_rpm": None,
            "rpm_at_probe": None,
            "confidence": "certain" if model else ("unknown" if has_led else "none"),
            "needs": [],
        }

        # 1. Is anything attached? Spin it up and look for a tachometer signal.
        self.step = f"Probing {channel}"
        self.progress = base
        try:
            self.hub.set_fan(channel, PROBE_DUTY)
        except Exception as exc:
            entry["needs"].append(f"could not drive {channel}: {exc}")
            return entry

        if not self._wait(SETTLE):
            return entry
        rpm = self.hub.rpm(channel)
        entry["rpm_at_probe"] = rpm

        if not rpm:
            entry["needs"].append("nothing responded - assumed empty")
            try:
                self.hub.set_fan(channel, 0)
            except Exception:
                pass
            return entry

        entry["present"] = True

        if has_led and not model:
            raw_id = accessory_ids.get(led_channel)
            entry["needs"].append(
                f"unrecognised accessory id {raw_id!r} - pick the fan model in "
                "setup so the real count can be sent to OpenRGB" if raw_id is not None
                else "LED count can't be auto-detected on this hub - pick the fan "
                     "model in setup so the real count can be sent to OpenRGB")
        elif not has_led:
            entry["needs"].append("hub did not detect an LED accessory on this channel")

        # 2. Curve measurement.
        if measure_curves:
            self.step = f"Finding start duty on {channel}"
            self.progress = base + budget * 0.35
            entry["start_duty"] = self._find_start(channel)

            self.step = f"Ramping {channel} to maximum"
            self.progress = base + budget * 0.75
            try:
                self.hub.set_fan(channel, 100)
                if self._wait(SETTLE * 2):
                    entry["max_rpm"] = self.hub.rpm(channel)
            except Exception:
                pass

        entry["needs"].extend(["position", "name"])
        try:
            self.hub.set_fan(channel, 0)
        except Exception:
            pass
        return entry

    def _find_start(self, channel) -> int | None:
        """Lowest duty at which the fan actually turns.

        Matters more than the datasheet: it sets min_duty and decides whether
        Zero RPM is usable on this channel.
        """
        try:
            self.hub.set_fan(channel, 0)
        except Exception:
            return None
        if not self._wait(SETTLE * 1.5):
            return None

        for duty in range(START_STEP, 61, START_STEP):
            if self._cancel.is_set():
                return None
            try:
                self.hub.set_fan(channel, duty)
            except Exception:
                return None
            if not self._wait(SETTLE):
                return None
            if self.hub.rpm(channel):
                return duty
        return None

    def _resize_hub_zones(self):
        """OpenRGB's hub LED zones report as 0/1 LEDs and silently do
        nothing when written to until resized to the real count - not a
        client-side bug, an intentional server-side backward-compat shim
        for this hub's effects-only zones that only lifts at SDK protocol
        5 (see app/openrgb_raw.py's docstring, sourced from OpenRGB's own
        RGBController.cpp). openrgb-python (app/openrgb_bridge.py) is
        hard-capped at protocol 4 and can never see or fix this - it's the
        wrong tool for this specific device, which is why every previous
        attempt at this from that side (timing retries, connection
        churn, a threading lock) treated real symptoms of the version
        mismatch as if they were the actual disease. openrgb_raw.py talks
        protocol 5 directly and sees/sets the real sizes. This is the one
        place that actually applies the counts this scan just identified,
        so hub RGB control works right after a scan instead of needing a
        separate manual step for every channel a model was confidently
        identified for. Channels left at "unknown" confidence still need a
        manual pick + POST /api/rgb/hub/{channel}/resize (no auto-detected
        count exists to apply yet)."""
        to_resize = [
            entry for entry in self.results
            if entry["confidence"] == "certain" and entry["led_channel"] and entry["leds"]
        ]
        if not to_resize:
            return
        try:
            raw = OpenRgbRaw(host=self.openrgb.host, port=self.openrgb.port)
            raw.connect()
        except Exception as exc:
            log.warning("Could not connect the raw OpenRGB v5 client for hub zone resize: %s", exc)
            for entry in to_resize:
                entry["needs"].append(f"could not resize OpenRGB zone: {exc}")
            return
        try:
            ctrl = raw.find_controller("NZXT")
            if ctrl is None:
                log.warning("No OpenRGB hub device found (raw v5 client), skipping zone resize")
                for entry in to_resize:
                    entry["needs"].append("no OpenRGB hub device found")
                return
            for entry in to_resize:
                zone = raw.zone_for_channel(ctrl, entry["led_channel"])
                if zone is None:
                    entry["needs"].append(
                        f"no OpenRGB zone matches {entry['led_channel']}")
                    continue
                if zone.leds_count == entry["leds"]:
                    continue
                try:
                    raw.resize_zone(ctrl.index, zone.index, entry["leds"])
                except Exception as exc:
                    entry["needs"].append(f"resize failed: {exc}")
            # Resizing invalidates the ControllerInfo snapshot's zone data -
            # re-fetch once to confirm every resize actually landed before
            # reporting success.
            ctrl = raw.controller_data(ctrl.index)
            for entry in to_resize:
                zone = raw.zone_for_channel(ctrl, entry["led_channel"])
                if zone is None or zone.leds_count != entry["leds"]:
                    got = zone.leds_count if zone else "no zone"
                    entry["needs"].append(
                        f"could not resize OpenRGB zone for {entry['led_channel']} "
                        f"(wanted {entry['leds']}, got {got})")
        finally:
            raw.close()

    def _scan_openrgb(self):
        # The settling-for-more-devices retry (OpenRGB's own SMBus/RAM SPD
        # detection can still be running well after this connection was
        # made) now lives in OpenRgbBridge.motherboard()/.ram() themselves
        # (see _settled_by_type there) - every caller gets it, not just a
        # scan. That retry is deliberately lightweight (client.update() on
        # the existing connection, not a reconnect - see its docstring for
        # why hammering reconnect() on every web request broke things), but
        # its ~6s budget only helps if OpenRGB is *actively* mid-detection
        # right now; it can't recover a device list that's been sitting
        # stale in this bridge's cache since server startup because
        # nothing has called update() on it since. A scan is a deliberate,
        # infrequent, user-initiated action (unlike a page load), so one
        # full reconnect() here - the same "once per rare action" pattern
        # already used for the hub in _resize_hub_zones - guarantees a
        # genuinely fresh read right before the result actually gets
        # reported to the user, on top of the lightweight retry below.
        if not self.openrgb.connected and not self.openrgb.connect():
            log.warning("OpenRGB not reachable during scan: %s", self.openrgb.last_error)
            return
        self.openrgb.reconnect()
        # Neither motherboard() nor ram() know anything about this scan's
        # cancel event - they're shared with every other caller in the app
        # and their retries are a plain blocking time.sleep(), not this
        # scan's own interruptible _wait(). Checking here can't interrupt
        # a retry loop already in flight, but it does mean a cancel that
        # arrives between the two calls (or before either starts) actually
        # takes effect instead of being silently ignored until both finish.
        if self._cancel.is_set():
            return
        try:
            mobo = self.openrgb.motherboard()
            # The real zone's LEDs, not the whole device's: on the X870-F
            # that's 3 ('Aura Mainboard') rather than 27 - the other 24 are
            # the empty ARGB headers, and the effect writes the real zone
            # only (see OpenRgbBridge.real_zone_index).
            if mobo:
                zone_index = self.openrgb.real_zone_index(mobo[0])
                count = (len(mobo[0].zones[zone_index].leds) if zone_index is not None
                         else len(mobo[0].leds))
                self.mobo = {"name": mobo[0].name, "leds": count}
            else:
                self.mobo = None
        except Exception as exc:
            log.warning("Could not read motherboard from OpenRGB: %s", exc)
        if self._cancel.is_set():
            return
        try:
            ram = self.openrgb.ram()
            self.ram = [{"name": d.name, "leds": len(d.leds)} for d in ram]
        except Exception as exc:
            log.warning("Could not read RAM from OpenRGB: %s", exc)

    def _restore(self):
        """Leave every fan somewhere safe rather than at whatever the scan left."""
        for channel in self.hub.channels.get("fan", []):
            try:
                self.hub.set_fan(channel, RESTORE_DUTY)
            except Exception:
                pass
