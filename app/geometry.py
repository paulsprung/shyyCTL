"""Physical geometry of NZXT X-series fan units.

The LED strip follows the fan apertures, not the square frame: an arc around
each outer opening, straight runs where two fans meet. The outline is a capsule.

The published/assumed LED counts were meant to decompose exactly:

    F120X   16 = 8 + 8            two end caps, no straight section
    F240X   24 = 16 caps + 8      one fan gap  -> 4 LEDs per side, confirmed
                                                   against real hardware
    F360X   36 = 16 caps + 20     two fan gaps -> 5 LEDs per side, INFERRED

F360X's real LED count is 36, confirmed against real hardware this project
targets - not the 32 a uniform "4 per side per gap" rule would predict (that
rule is only confirmed for F240X). The 5-per-side value above is derived
just to make F360X's total reconcile with 8 LEDs/cap; it has NOT been walked
on real F360X hardware, so its exact per-LED positions are a best-effort
placeholder, not confirmed fact. Run the setup screen's LED walk on a real
F360X unit before trusting `led_positions()` for anything beyond an
approximate preview on that model.

Because the per-gap density isn't the same across models, LED count no
longer safely inverts to a fan count for an arbitrary total (see
`fans_from_leds`) - only exact-match lookup against a known model is safe.

The hub can't report an LED count directly (see scan.py) - but it does
report a raw numeric accessory ID per channel via liquidctl's Python API
(hub.accessory_ids(); the CLI discards this, only printing "Unknown" since
these IDs aren't in liquidctl's Hue2Accessory name table yet). Confirmed
against real hardware this session:

    0x20 (32)  ->  F120X  (1 fan,  16 LEDs)
    0x22 (34)  ->  F240X  (2 fans, 24 LEDs)
    0x24 (36)  ->  F360X  (3 fans, 36 LEDs)

Sequential IDs, +2 per additional fan in the frame - looks like NZXT's own
internal model numbering, not something coincidental. This makes automatic
per-channel model detection possible via `identify_by_accessory_id()`,
without asking a human every time - falling back to manual model choice
in the setup screen only for an ID that isn't in this table yet.

UNVERIFIED: whether the strip is one continuous loop or has breaks. The setup
screen's LED walk answers that empirically instead of guessing from a product
photo. `gaps` on a Model carries the answer once known.
"""

from dataclasses import dataclass, field
from math import cos, sin, pi

CAP_LEDS = 8  # per semicircular end cap - consistent across all three models


@dataclass(frozen=True)
class Model:
    name: str
    fans: int
    leds: int
    size_mm: int
    single_frame: bool
    # LED indices known to be dark or absent. Filled in by the LED walk.
    gaps: tuple[int, ...] = field(default=())
    # False means the cap/side split below is inferred from the confirmed
    # total, not independently walked on real hardware for this model.
    geometry_walked: bool = True
    # Raw accessory ID from hub.accessory_ids() - None until confirmed
    # against real hardware for that model (see module docstring).
    accessory_id: int | None = None


MODELS = (
    Model("F120X", 1, 16, 120, False, accessory_id=0x20),
    Model("F240X", 2, 24, 240, True, accessory_id=0x22),
    Model("F360X", 3, 36, 360, True, geometry_walked=False, accessory_id=0x24),
)

BY_LEDS = {m.leds: m for m in MODELS}
BY_ACCESSORY_ID = {m.accessory_id: m for m in MODELS if m.accessory_id is not None}


def _side_leds_per_gap(model: Model) -> int:
    """LEDs per straight side per fan-gap, derived from the model's
    confirmed total rather than a single global constant - F240X and F360X
    do not share the same per-gap density (4 vs an inferred 5)."""
    gaps = model.fans - 1
    if gaps == 0:
        return 0
    extra = model.leds - CAP_LEDS * 2
    per_gap = extra / (2 * gaps)
    return round(per_gap)


def fans_from_leds(leds: int) -> int | None:
    """Exact-match only: with per-model density no longer uniform, a bare
    LED count can't be safely inverted to a fan count for anything that
    isn't one of the known totals (16/24/36)."""
    model = BY_LEDS.get(leds)
    return model.fans if model else None


def identify(leds: int) -> Model | None:
    """Map a reported LED count onto a known model."""
    return BY_LEDS.get(leds)


def identify_by_accessory_id(accessory_id: int) -> Model | None:
    """Map the hub's raw per-channel accessory ID onto a known model - the
    actual auto-detection path, since the hub never reports an LED count
    directly. Returns None for an ID not yet in the table, which should
    fall back to asking the human once (and is worth adding here afterwards)."""
    return BY_ACCESSORY_ID.get(accessory_id)


def expected_leds(fans: int) -> int | None:
    """The real LED count for a given fan count, looked up from MODELS -
    not computed from a formula, since the per-gap density isn't uniform."""
    for model in MODELS:
        if model.fans == fans:
            return model.leds
    return None


def led_positions(model: Model, radius: float = 1.0,
                  vertical: bool = True) -> list[tuple[float, float]]:
    """LED coordinates in unit space, centred on (0, 0).

    Walks the capsule outline: end cap, straight side, other end cap, other
    straight side. Order is the assumed physical wiring order; the setup
    screen can reverse it if the LED walk shows the strip runs the other way.

    `radius` is the fan radius. A 120 mm fan is 120 mm across, so in
    millimetres radius = 60.
    """
    gaps = model.fans - 1
    span = gaps * 2 * radius          # first fan centre to last fan centre
    side_per_gap = _side_leds_per_gap(model)
    n_side = side_per_gap * gaps
    pts: list[tuple[float, float]] = []

    def arc(ox: float, oy: float, a0: float, a1: float):
        for i in range(CAP_LEDS):
            t = a0 + (a1 - a0) * ((i + 0.5) / CAP_LEDS)
            pts.append((ox + cos(t) * radius, oy + sin(t) * radius))

    def line(x0: float, y0: float, x1: float, y1: float):
        for i in range(n_side):
            t = (i + 0.5) / n_side
            pts.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))

    if vertical:
        top, bottom = -span / 2, span / 2
        arc(0, bottom, 0, pi)                  # bottom cap
        line(-radius, bottom, -radius, top)    # left side, upwards
        arc(0, top, pi, 2 * pi)                # top cap
        line(radius, top, radius, bottom)      # right side, downwards
    else:
        left, right = -span / 2, span / 2
        arc(right, 0, -pi / 2, pi / 2)         # right cap
        line(right, radius, left, radius)      # bottom side, leftwards
        arc(left, 0, pi / 2, 3 * pi / 2)       # left cap
        line(left, -radius, right, -radius)    # top side, rightwards

    return pts


def rotate(points, degrees: float):
    """Rotate LED coordinates about the unit's centre."""
    r = degrees * pi / 180
    c, s = cos(r), sin(r)
    return [(x * c - y * s, x * s + y * c) for x, y in points]


def path_lengths(points) -> list[float]:
    """Cumulative distance along the strip, normalised to 0..1.

    This is what a 'travelling' effect should use. Index-based stepping looks
    wrong because the arcs are denser than the straights.
    """
    if not points:
        return []
    acc, total = [0.0], 0.0
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        total += ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
        acc.append(total)
    return [a / total for a in acc] if total else [0.0] * len(points)
