"""The startup animation's frames - played on the hub's fan rings as soon as
shyyctl has the hub, before the saved effect or colours take over (main.py's
_play_startup_animation). The hub itself keeps nothing across a power-off
(confirmed on the real hardware: every LED comes up plain white), so this is
the first thing the rings show that shyyctl chose.

Pure functions only - no device access - so the frames can be checked
without hardware. Every unit runs in the same visual direction (clockwise,
starting at its top) whatever its strip's wiring: the order comes from the
LED positions in the layout, not from strip indices.
"""

import math

STYLES = ("chase", "fill")


def ring_order(leds: list[dict]) -> list[float]:
    """Each LED's place along the ring, 0..1, indexed by strip index:
    clockwise from the top of the unit. Ranked by angle around the unit's
    centre rather than taken from the angle itself, so the head moves one
    LED per step on the long straight runs of an F240X/F360X too, not just
    around the round end caps. leds: the unit's entries from
    Layout.with_leds() ({"index", "x", "y"})."""
    if not leds:
        return []
    cx = sum(l["x"] for l in leds) / len(leds)
    cy = sum(l["y"] for l in leds) / len(leds)

    def angle(l):
        # y grows downwards on the canvas: atan2(dx, -dy) is 0 at the top
        # and increases clockwise.
        return math.atan2(l["x"] - cx, -(l["y"] - cy)) % (2 * math.pi)

    ranked = sorted(leds, key=angle)
    places = {l["index"]: rank / len(ranked) for rank, l in enumerate(ranked)}
    count = max(places) + 1
    return [places.get(i, 0.0) for i in range(count)]


def duration(style: str, lap_s: float, tail: int, count: int) -> float:
    """Seconds the animation itself runs, before the fade: one lap, plus -
    for the chase - the time its tail needs to run out after the head is
    back at the start."""
    if style == "chase" and count:
        return lap_s * (1 + max(tail, 1) / count)
    return lap_s


def frame(style: str, places: list[float], t: float, lap_s: float, tail: int,
          color: tuple[int, int, int]) -> list[tuple[int, int, int]]:
    """The ring's colours t seconds into the animation, by strip index.
    chase: a full-brightness head with a tail fading over `tail` LEDs,
    once around; fill: the ring lights up LED by LED and stays lit."""
    n = len(places)
    if not n:
        return []
    head = t / lap_s if lap_s > 0 else 1.0
    out = []
    for p in places:
        behind = (head - p) * n  # LEDs the head is past this one
        if style == "fill":
            level = 1.0 if behind >= 0 else 0.0
        elif behind < 0 or behind >= max(tail, 1):
            level = 0.0
        else:
            level = (1 - behind / max(tail, 1)) ** 2
        out.append(tuple(round(c * level) for c in color))
    return out


def end_frame(style: str, count: int, color: tuple[int, int, int]) -> list[tuple[int, int, int]]:
    """What the ring shows once the animation is over - what the fade
    starts from: dark after a chase, fully lit after a fill."""
    return [color if style == "fill" else (0, 0, 0)] * count


def blend(a: list[tuple], b: list[tuple], k: float) -> list[tuple[int, int, int]]:
    """a -> b, k from 0 (all a) to 1 (all b), smoothed at both ends."""
    k = max(0.0, min(1.0, k))
    k = (1 - math.cos(k * math.pi)) / 2
    return [tuple(round(x + (y - x) * k) for x, y in zip(ca, cb)) for ca, cb in zip(a, b)]
