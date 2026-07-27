"""Physical placement of every controllable unit - fans, motherboard, RAM,
the Panorama display - plus the LED coordinate map derived from it.

Where a unit is bolted, which way it faces, which side of the case it's on:
none of that is guessable from software (same reasoning as scan.py's model
identification) - this module persists the answer once a human places each
unit on the Setup screen, and turns that placement into real per-LED
coordinates for anything that needs them (a case preview now, host-driven
effects like a top-to-bottom wave later).

adopt() never overwrites a unit's placement - only the fields a fresh scan
actually re-measures (model/leds/channel) get replaced; name, surface,
position, rotation, facing and LED order are only ever touched by the human
via save(), and survive being re-adopted after a rescan.
"""

import json
from pathlib import Path

from . import geometry

SURFACES = ("front", "top", "side", "rear")

# Fraction of a surface canvas's span that one radius-1 fan's LED ring
# occupies - not a physical measurement, just enough spread that a unit's
# individual LEDs are visible as a cluster rather than a single point.
# Kept small deliberately (smaller than an early value of 0.12) so units
# placed close together don't visually overlap.
UNIT_SCALE = 0.085


class Layout:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        if self.path.is_file():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return {"units": []}

    def save(self, units: list[dict]) -> list[dict]:
        self.path.write_text(json.dumps({"units": units}), encoding="utf-8")
        return units

    def adopt(self, scan_state: dict) -> list[dict]:
        """Rebuilds the unit list from the latest scan (fans, motherboard,
        RAM) plus a standing Panorama display unit - the panel isn't part
        of a hardware scan (see scan.py; it's found independently via
        app/panorama.py) but still needs a place in the layout."""
        current = {u["id"]: u for u in self.load()["units"]}
        units = []

        fan_entries = [r for r in scan_state.get("results", []) if r.get("led_channel")]
        for i, r in enumerate(fan_entries):
            units.append(self._merge(current, {
                "id": f"fan-{r['led_channel']}",
                "kind": "fan",
                "name": r["led_channel"],
                "fan_channel": r["fan_channel"],
                "led_channel": r["led_channel"],
                "model": r.get("model"),
                "leds": r.get("leds"),
            }, i, len(fan_entries)))

        mobo = scan_state.get("mobo")
        if mobo:
            units.append(self._merge(current, {
                "id": "mobo", "kind": "mobo", "name": mobo["name"], "leds": mobo["leds"],
            }, 0, 1))

        ram = scan_state.get("ram") or []
        for i, r in enumerate(ram):
            # Every stick on a kit reports the identical OpenRGB device
            # name (confirmed against real hardware: two sticks, both
            # "ENE DRAM") - appending the stick number here is the only
            # thing that keeps them visually distinguishable in Setup,
            # Advanced and Status. Only used as a first-adopt default (see
            # _merge below) - a name the human already edited always wins.
            units.append(self._merge(current, {
                "id": f"ram-{i}", "kind": "ram", "name": f"{r['name']} ({i + 1})", "leds": r["leds"],
            }, i, len(ram)))

        units.append(self._merge(current, {
            "id": "display", "kind": "display", "name": "Panorama", "leds": None,
        }, 0, 1))

        return self.save(units)

    @staticmethod
    def _merge(current: dict, fresh: dict, index: int, total: int) -> dict:
        """fresh carries this adopt's just-measured fields (kind, name,
        channel, model, leds); a prior placement for the same id (if any)
        wins for everything placement-related, so re-adopting after a
        rescan can't silently discard where the human already put this
        unit."""
        prior = current.get(fresh["id"])
        # Confirmed by direct comparison against real F120X/F240X/F360X
        # hardware using the split (red/blue) and chase tests: F120X's
        # index order matches as-is; F240X needs -4 and F360X needs -3 to
        # line up (an *earlier* pass through this same comparison found
        # +4 for both - these replace that, measured after F360X's real
        # per-seam LED count also turned out to be 4 rather than the
        # previously assumed 5, see geometry.py's module docstring; -3 on
        # the new 32-LED/4-per-seam shape hasn't been independently
        # re-confirmed since that geometry change, only carried over from
        # what worked against the old 36-LED shape). Only a first-adopt
        # default (see the loop below) - if it's off for a given unit, the
        # LED offset field in Setup corrects it live, no rescan needed.
        default_led_offset = {"F240X": -4, "F360X": -3}.get(fresh.get("model"), 0)
        placement_defaults = {
            "name": fresh["name"],
            "surface": "front",
            "x": 0.5 if total <= 1 else 0.12 + 0.76 * (index / (total - 1)),
            "y": 0.5,
            "rotation": 0,
            "facing": "front",
            "reversed_leds": False,
            "led_offset": default_led_offset,
        }
        merged = dict(fresh)
        for key, default in placement_defaults.items():
            merged[key] = prior[key] if prior and key in prior else default
        return merged

    # -- LED coordinate map -------------------------------------------------

    def with_leds(self, units: list[dict] | None = None) -> dict:
        if units is None:
            units = self.load()["units"]
        leds = []
        for unit in units:
            for i, (px, py) in enumerate(self._unit_points(unit)):
                leds.append({
                    "unit": unit["id"],
                    "surface": unit.get("surface", "front"),
                    "x": unit.get("x", 0.5) + px * UNIT_SCALE,
                    "y": unit.get("y", 0.5) + py * UNIT_SCALE,
                    "index": i,
                })
        return {"surfaces": list(SURFACES), "units": units, "leds": leds}

    @staticmethod
    def _unit_points(unit: dict) -> list[tuple[float, float]]:
        """Per-LED coordinates in unit-space (a radius-1 fan is 1 unit
        across), before placement. Real capsule geometry for an identified
        fan model (app/geometry.py, unit-tested); a simple evenly-spaced
        line for anything else - an unidentified fan, RAM, motherboard, or
        the display - which is not physically accurate but keeps the LED
        count and cluster-of-dots look consistent everywhere.

        rotation and reversed_leds apply regardless of kind - a RAM stick
        or motherboard header is just as physically rotatable as a fan, it
        only lacks the fan-specific capsule shape and front/edge facing
        concept (facing only means anything for an actual fan).

        led_offset is the escape hatch for a wrong *wiring order*
        assumption rather than a wrong shape or facing - F360X's cap/side
        split (see geometry.py's module docstring) is an inferred
        placeholder, never walked on real hardware, and a mismatch there
        looks like "the LED that lights up isn't the one highlighted here"
        even though the shape itself is otherwise correct. Rather than
        guess a new formula blind, a cyclic shift of the point order lets
        a human correct it empirically: click each LED in Advanced (which
        doubles as an identify tool - it lights up exactly that one LED
        and nothing else) and nudge this until the highlighted position
        matches the one that's actually lit."""
        model = None
        if unit.get("kind") == "fan" and unit.get("model"):
            model = next((m for m in geometry.MODELS if m.name == unit["model"]), None)
        if model is not None:
            pts = geometry.led_positions(
                model, radius=1.0, vertical=unit.get("facing", "front") == "front")
        else:
            n = unit.get("leds") or 1
            pts = [(0.0, 0.0)] if n <= 1 else [(-1.0 + 2.0 * i / (n - 1), 0.0) for i in range(n)]
        if unit.get("rotation"):
            pts = geometry.rotate(pts, unit["rotation"])
        if unit.get("reversed_leds"):
            pts = list(reversed(pts))
        offset = unit.get("led_offset") or 0
        if offset and pts:
            offset %= len(pts)
            pts = pts[offset:] + pts[:offset]
        return pts
