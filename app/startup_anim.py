"""The startup animation's frames - played as soon as shyyctl has the hub,
before the saved effect or colours take over (main.py's
_run_startup_animation). The hub itself keeps nothing across a power-off
(confirmed on the real hardware: every LED comes up plain white), so this is
the first thing the rings show that shyyctl chose.

Pure functions only - no device access - so the frames can be checked
without hardware. Directions and starting points come from the LED
positions in the layout, not from strip indices, so every unit looks the
same whatever its strip's wiring: rings run clockwise from their top (or
counter-clockwise), and the snake/cascade travel through the case in the
chosen direction.
"""

import colorsys
import math

STYLES = ("snake", "cascade", "chase", "fill", "ambient")
DIRECTIONS = ("bottom_to_top", "top_to_bottom", "left_to_right", "right_to_left")
PALETTES = ("solid", "gradient", "rainbow")

# cascade: how far into one ring's lap the next ring starts, and how
# bright a ring stays once its head has passed
CASCADE_STAGGER = 0.35
CASCADE_REST_LEVEL = 0.3

GAP = None  # a snake path step between two rings: nothing to light


def _centre(leds: list[dict]) -> tuple[float, float]:
    return (sum(l["x"] for l in leds) / len(leds), sum(l["y"] for l in leds) / len(leds))


def _dist(a: dict, b: dict) -> float:
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"])


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
    LED per step on the long straight runs of an F240X/F360X too, not just
    around the round end caps."""
    ranked = clockwise(leds, reverse)
    if not ranked:
        return []
    places = {l["index"]: rank / len(ranked) for rank, l in enumerate(ranked)}
    return [places.get(i, 0.0) for i in range(max(places) + 1)]


def _travel_key(direction: str):
    """Sort key for 'which comes first' along a direction through the case."""
    return {
        "bottom_to_top": lambda p: -p[1],
        "top_to_bottom": lambda p: p[1],
        "left_to_right": lambda p: p[0],
        "right_to_left": lambda p: -p[0],
    }.get(direction, lambda p: -p[1])


def unit_order(units: list[tuple[str, list[dict]]], direction: str) -> list[str]:
    """Unit ids in the order a snake or cascade reaches them.
    units: (unit id, its LEDs)."""
    key = _travel_key(direction)
    return [uid for uid, leds in sorted(units, key=lambda u: key(_centre(u[1])) if u[1] else 0)]


def snake_path(units: list[tuple[str, list[dict]]], direction: str,
               reverse: bool = False) -> list[tuple[str, int] | None]:
    """One continuous path through every ring, in travel order:
    (unit id, strip index) per step, GAP for a step between two rings.

    Each ring is entered at its LED closest to the ring before and left at
    its LED closest to the ring after, going the long way round between
    the two, so the snake visibly leaves each ring towards the next one
    instead of finishing a full lap next to where it came in. The last
    ring gets a full lap. The jump between two rings becomes GAP steps,
    as many as LEDs would fit in that distance, so the head takes as long
    to cross a gap as it would to run it - the tail keeps fading in the
    ring behind meanwhile."""
    by_id = dict(units)
    order = [uid for uid in unit_order(units, direction) if by_id[uid]]
    key = _travel_key(direction)
    spacing = _led_spacing([by_id[uid] for uid in order])
    path: list[tuple[str, int] | None] = []
    prev_exit = None
    for k, uid in enumerate(order):
        ring = clockwise(by_id[uid], reverse)
        n = len(ring)
        if prev_exit is None:
            entry = min(range(n), key=lambda i: key((ring[i]["x"], ring[i]["y"])))
        else:
            entry = min(range(n), key=lambda i: _dist(ring[i], prev_exit))
            gap = _dist(ring[entry], prev_exit)
            path += [GAP] * max(0, round(gap / spacing) - 1)
        if k == len(order) - 1:
            steps = list(range(n))                      # full lap
        else:
            nxt = by_id[order[k + 1]]
            exit_ = min(range(n), key=lambda i: min(_dist(ring[i], l) for l in nxt))
            forward = (exit_ - entry) % n
            steps = list(range(forward + 1)) if forward >= n / 2 else \
                [-s for s in range(n - forward + 1)]    # the long way round, backwards
        visited = [ring[(entry + s) % n] for s in steps]
        path += [(uid, l["index"]) for l in visited]
        prev_exit = visited[-1]
    return path


def _led_spacing(rings: list[list[dict]]) -> float:
    """Typical distance between neighbouring LEDs of a ring."""
    gaps = []
    for leds in rings:
        ring = clockwise(leds)
        gaps += [_dist(ring[i], ring[(i + 1) % len(ring)]) for i in range(len(ring))]
    gaps.sort()
    return gaps[len(gaps) // 2] if gaps else 0.01


def tail_level(behind: float, tail: int) -> float:
    """Brightness of an LED `behind` LEDs behind the head: 1 at the head,
    easing out over `tail` LEDs, 0 ahead of the head and past the tail."""
    tail = max(tail, 1)
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


def duration(style: str, lap_s: float, tail: int, count: int, laps: int = 1) -> float:
    """Seconds a single ring's chase/fill/cascade runs: its laps, plus -
    for the chase - the time its tail needs to run out after the head is
    back at the start."""
    if style == "chase" and count:
        return lap_s * (laps + max(tail, 1) / count)
    return lap_s * (laps if style == "cascade" else 1)


def frame(style: str, places: list[float], t: float, lap_s: float, tail: int,
          palette: str, colors: list[tuple], laps: int = 1) -> list[tuple[int, int, int]]:
    """One ring's colours t seconds into its run, by strip index.
    chase: a full-brightness head with a tail fading over `tail` LEDs,
    `laps` times around; fill: the ring lights up LED by LED and stays
    lit; cascade: a head with a tail that leaves the ring dimly lit."""
    n = len(places)
    if not n:
        return []
    head = t / lap_s if lap_s > 0 else float(laps)
    out = []
    for p in places:
        if style == "fill":
            level = 1.0 if head >= p else 0.0
            out.append(scale(paint(palette, colors, p, t), level))
            continue
        # every lap the head is past this LED; the most recent pass counts
        behind = (head - p) * n
        if behind >= 0 and head < laps:
            behind %= n
        if style == "cascade":
            level = 0.0 if head < p else max(tail_level(behind, tail), CASCADE_REST_LEVEL)
        else:
            level = tail_level(behind, tail)
        out.append(scale(paint(palette, colors, behind / max(tail, 1), t), level))
    return out


def end_frame(style: str, places: list[float], palette: str, colors: list[tuple],
              t: float) -> list[tuple[int, int, int]]:
    """What a ring shows once its chase/fill/cascade is over."""
    level = {"fill": 1.0, "cascade": CASCADE_REST_LEVEL}.get(style, 0.0)
    return [scale(paint(palette, colors, p if style == "fill" else 1.0, t), level) for p in places]


def scale(color: tuple, level: float) -> tuple[int, int, int]:
    return tuple(round(c * level) for c in color)


def ambient_color(zones: dict, v: float, h: float) -> tuple[int, int, int]:
    """An LED's colour from a video's four sampled edge zones, by its
    position in the case (v: 0 top..1 bottom, h: 0 left..1 right) - the
    same blend as the "ambient" effect in main.py."""
    top, bottom, left, right = zones["top"], zones["bottom"], zones["left"], zones["right"]
    return tuple(round((top[c] * (1 - v) + bottom[c] * v + left[c] * (1 - h) + right[c] * h) / 2)
                 for c in range(3))


def blend(a: list[tuple], b: list[tuple], k: float) -> list[tuple[int, int, int]]:
    """a -> b, k from 0 (all a) to 1 (all b), smoothed at both ends."""
    k = max(0.0, min(1.0, k))
    k = (1 - math.cos(k * math.pi)) / 2
    return [tuple(round(x + (y - x) * k) for x, y in zip(ca, cb)) for ca, cb in zip(a, b)]
