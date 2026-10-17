"""The startup animation's frames - played as soon as shyyctl has the hub,
before the saved effect or colours take over (main.py's
_run_startup_animation). The hub itself keeps nothing across a power-off
(confirmed on the real hardware: every LED comes up plain white), so this is
the first thing the rings show that shyyctl chose.

Pure functions only - no device access - so the frames can be checked
without hardware. Directions and starting points come from the LED
positions in the layout, not from strip indices, so every unit looks the
same whatever its strip's wiring.

Two spins share one frame function (spin_frame): the whole case turning
as one big ring - every fan LED placed by its angle around the case's
centre, so the light sweeps from fan to fan round the case - or each
ring turning on its own, the rings starting one after another in that
same order round the case.
"""

import colorsys
import math

STYLES = ("case_spin", "spin", "ambient")
PALETTES = ("solid", "gradient", "rainbow")

# spin: how far into its first turn one ring is when the next one starts
SPIN_STAGGER = 0.5
SPIN_TAIL_START_LEDS = 3   # each ring: its tail on the first turn, in LEDs
CASE_TAIL_START = 0.1      # whole case: its tail on the first turn, in turns
SPIN_FILL_FROM = 0.6       # share of the run after which everything fills in


def _centre(leds: list[dict]) -> tuple[float, float]:
    return (sum(l["x"] for l in leds) / len(leds), sum(l["y"] for l in leds) / len(leds))


def clockwise(leds: list[dict], reverse: bool = False) -> list[dict]:
    """A unit's LEDs in ring order, starting at the top of the unit:
    clockwise, or counter-clockwise with reverse. leds: the unit's entries
    from Layout.with_leds() ({"index", "x", "y"}); y grows downwards on
    the canvas, so atan2(dx, -dy) is 0 at the top and increases clockwise."""
    if not leds:
        return []
    cx, cy = _centre(leds)
    ring = sorted(leds, key=lambda l: math.atan2(l["x"] - cx, -(l["y"] - cy)) % (2 * math.pi))
    return ring[:1] + ring[:0:-1] if reverse else ring


def ring_order(leds: list[dict], reverse: bool = False) -> list[float]:
    """Each LED's place along its ring, 0..1, indexed by strip index.
    Ranked rather than taken from the angle itself, so the head moves one
    LED per step on the long sides of an F240X/F360X too, not just around
    the corners."""
    ranked = clockwise(leds, reverse)
    if not ranked:
        return []
    places = {l["index"]: rank / len(ranked) for rank, l in enumerate(ranked)}
    return [places.get(i, 0.0) for i in range(max(places) + 1)]


def case_turns(units: list[tuple[str, list[dict]]], start_unit: str,
               reverse: bool) -> dict[str, list[float]]:
    """Every unit's LEDs placed round the case as a whole, 0..1 of a turn,
    by strip index: the angle of each LED around the centre of all the
    fans, 0 at the leading edge of `start_unit` (the left-most unit if it
    isn't one of them), increasing clockwise on the canvas -
    counter-clockwise with reverse."""
    leds = [l for _, ls in units for l in ls]
    if not leds:
        return {}
    cx, cy = _centre(leds)

    def turn(x, y):
        a = math.atan2(x - cx, -(y - cy)) / (2 * math.pi)  # clockwise from the top
        return -a if reverse else a

    centres = {uid: _centre(ls) for uid, ls in units if ls}
    if start_unit not in centres:
        start_unit = min(centres, key=lambda uid: centres[uid][0])
    # 0 at the start unit's leading edge, so the whole of it comes first
    centre = turn(*centres[start_unit])
    lead = min(((turn(l["x"], l["y"]) - centre + 0.5) % 1.0 - 0.5) for l in dict(units)[start_unit])
    zero = centre + lead - 0.01
    out = {}
    for uid, ls in units:
        if not ls:
            out[uid] = []
            continue
        # each unit in one piece: a unit straddling the start is placed
        # just past the end of the turn, not partly at its very beginning
        mid = (turn(*centres[uid]) - zero) % 1.0
        by_index = {l["index"]: mid + ((turn(l["x"], l["y"]) - zero - mid + 0.5) % 1.0 - 0.5) for l in ls}
        out[uid] = [by_index.get(i, mid) for i in range(max(by_index) + 1)]
    return out


def unit_sequence(units: list[tuple[str, list[dict]]], start_unit: str, reverse: bool) -> list[str]:
    """Unit ids in the order a turn round the case reaches their centres."""
    centred = [(uid, [{"index": 0, "x": _centre(ls)[0], "y": _centre(ls)[1]}]) for uid, ls in units if ls]
    if not centred:
        return []
    # placed among all LEDs, so the centre of rotation is the case's
    turns = case_turns(units + [(f"_c_{uid}", c) for uid, c in centred], start_unit, reverse)
    return sorted((uid for uid, _ in centred), key=lambda uid: turns[f"_c_{uid}"][0])


def tail_level(behind: float, tail: float) -> float:
    """Brightness of an LED `behind` LEDs behind the head: 1 at the head,
    easing out over `tail` LEDs, 0 past the tail."""
    tail = max(tail, 1.0)
    if behind < 0 or behind >= tail:
        return 0.0
    return (1 - behind / tail) ** 2


def paint(palette: str, colors: list[tuple], u: float, t: float = 0.0) -> tuple[int, int, int]:
    """The colour at u (0..1: along a tail, or a place on a ring).
    solid: the first colour; gradient: first -> second along u; rainbow:
    hue running along u and drifting with time t."""
    if palette == "gradient" and len(colors) > 1:
        a, b = colors[0], colors[1]
        u = max(0.0, min(1.0, u))
        return tuple(round(a[c] + (b[c] - a[c]) * u) for c in range(3))
    if palette == "rainbow":
        r, g, b = colorsys.hsv_to_rgb((u + t * 0.25) % 1.0, 1.0, 1.0)
        return (round(r * 255), round(g * 255), round(b * 255))
    return tuple(colors[0])


def spin_duration(lap_s: float, laps: int) -> float:
    return lap_s * max(laps, 1)


def spin_frame(places: list[float], t: float, lap_s: float, laps: int, tail_start: float,
               palette: str, colors: list[tuple]) -> list[tuple[int, int, int]]:
    """LEDs t seconds into a spin, by strip index; places: each LED's
    position round whatever is turning, 0..1. A head goes round `laps`
    times, its tail growing from `tail_start` (a fraction of a turn) to
    the whole way round, and over the last part of the run everything
    fills in to full colour - fully lit at the end. Dark before it starts
    (t < 0)."""
    if not places or t < 0:
        return [(0, 0, 0)] * len(places)
    total = spin_duration(lap_s, laps)
    p = min(1.0, t / total)
    head = t / lap_s
    tail = tail_start + (1 - tail_start) * p ** 1.5
    fill = _smooth((p - SPIN_FILL_FROM) / (1 - SPIN_FILL_FROM))
    out = []
    for q in places:
        behind = head - q
        level = _tail(behind % 1.0, tail) if behind >= 0 else 0.0
        out.append(scale(paint(palette, colors, q, t), max(level, fill)))
    return out


def _tail(behind: float, tail: float) -> float:
    """tail_level in fractions of a turn."""
    if behind >= tail or tail <= 0:
        return 0.0
    return (1 - behind / tail) ** 2


def full_frame(places: list[float], palette: str, colors: list[tuple], t: float) -> list[tuple[int, int, int]]:
    """A ring fully lit - how every spin ends."""
    return [paint(palette, colors, q, t) for q in places]


def _smooth(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def scale(color: tuple, level: float) -> tuple[int, int, int]:
    return tuple(round(c * level) for c in color)


def blend(a: list[tuple], b: list[tuple], k: float) -> list[tuple[int, int, int]]:
    """a -> b, k from 0 (all a) to 1 (all b), smoothed at both ends."""
    k = max(0.0, min(1.0, k))
    k = (1 - math.cos(k * math.pi)) / 2
    return [tuple(round(x + (y - x) * k) for x, y in zip(ca, cb)) for ca, cb in zip(a, b)]
