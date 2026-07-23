"""Manual hardware probe - no shyyctl app involved, just liquidctl/OpenRGB directly.

liquidctl's Control Hub LED driver has a confirmed bug (channel 1 doesn't
apply colour even though OpenRGB's independent driver reaches it fine), so
RGB now goes exclusively through OpenRGB - for the hub's own LEDs too, not
just RAM/mobo. liquidctl is only used here for fan speed.

Close NZXT CAM and Armoury Crate/Aura Creator first - they hold devices open
or fight over who's driving the lighting.

Usage:
    python tools/probe.py fans   # each fan channel off, one at a time
    python tools/probe.py rgb    # hub LEDs, mobo, RAM - each off/orange/off in turn
    python tools/probe.py all    # fans then rgb
    python tools/probe.py hub    # liquidctl list/status/initialize - info only
    python tools/probe.py aio    # look for the Panorama pump as its own liquidctl device
"""

import os
import re
import subprocess
import sys

# Windows consoles default to cp1252, which can't encode the box-drawing
# characters liquidctl's human-readable output uses. Forcing the child
# process's own stdout to utf-8 avoids it crashing mid-print.
_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}

FAN_CHANNELS = [f"fan{i}" for i in range(1, 6)]
ORANGE = (255, 107, 61)


def run(*args: str, match: str | None = None):
    full = (["-m", match] if match else []) + list(args)
    print(f"\n$ liquidctl {' '.join(full)}")
    try:
        result = subprocess.run(["liquidctl", *full], capture_output=True, text=True,
                                 timeout=30, env=_ENV)
    except FileNotFoundError:
        print("liquidctl not found on PATH - pip install liquidctl")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("(timed out)")
        return ""
    print(result.stdout.strip() or "(no stdout)")
    if result.stderr.strip():
        print("stderr:", result.stderr.strip())
    return result.stdout


def verify_fans():
    m = "control hub"
    print("=== fans: one channel at a time ===")
    for ch in FAN_CHANNELS:
        run("set", ch, "speed", "0", match=m)
        input(f"{ch} at 0% now - note whether it stopped (or was already empty). Enter to restore.")
        run("set", ch, "speed", "40", match=m)


def _openrgb_groups():
    from openrgb import OpenRGBClient
    from openrgb.utils import DeviceType
    client = OpenRGBClient(address="127.0.0.1", port=6742, name="shyyctl-probe")
    for device in client.devices:
        print(f"{device.name}  type={device.type}  leds={len(device.leds)}  "
              f"modes={[m.name for m in device.modes]}")
    return {
        "hub (fan LEDs)": [d for d in client.devices if "NZXT" in d.name],
        "mobo": [d for d in client.devices if d.type == DeviceType.MOTHERBOARD],
        "ram": [d for d in client.devices if d.type == DeviceType.DRAM],
    }


def _set_static(devices, color):
    from openrgb.utils import RGBColor
    for device in devices:
        for i, mode in enumerate(device.modes):
            if mode.name.lower() == "static":
                device.set_mode(i)
                break
    for device in devices:
        device.set_color(RGBColor(*color))


def _hub_led_counts():
    """Ask the hub itself how many LEDs are on each channel - liquidctl's
    'initialize' is where NZXT's own accessory detection surfaces (same thing
    CAM reads to know it's an F120X vs F240X). Never hardcode/guess this."""
    output = run("initialize", match="control hub")
    counts = [int(n) for n in re.findall(r"(\d+)\s*LEDs?", output, re.IGNORECASE)]
    if not counts:
        print("Could not parse LED counts from 'initialize' output - falling back to a guess of 40.")
        return None
    print(f"Parsed LED counts per channel from the hub itself: {counts}")
    return counts


def _ensure_sized(devices, counts=None, default=40):
    """Resizable zones (addressable strips OpenRGB can't measure on its own,
    which is what the hub's leds=0 zones are) need an explicit length before
    set_color has anything to write into. Uses the hub's own reported counts
    where we have them, in channel order - falls back to a flat guess
    otherwise, only to keep this probe usable before that's confirmed."""
    i = 0
    for device in devices:
        for zone in device.zones:
            before = len(zone.leds)
            if before != 0:
                print(f"  zone '{zone.name}' already has {before} LEDs, leaving as is")
                continue
            size = counts[i] if counts and i < len(counts) else default
            i += 1
            try:
                zone.resize(size)
            except Exception as exc:
                print(f"  resize FAILED on zone '{zone.name}' (requested {size}): {exc}")
                continue
            after = len(zone.leds)
            print(f"  zone '{zone.name}': resize({size}) called, now reports {after} LEDs"
                  + ("" if after else "  <- still 0, resize had no effect"))


def verify_rgb():
    try:
        groups = _openrgb_groups()
    except ImportError:
        print("pip install openrgb-python")
        return
    except Exception as exc:
        print("Could not reach OpenRGB SDK server:", exc)
        return

    counts = _hub_led_counts()
    _ensure_sized(groups["hub (fan LEDs)"], counts=counts)

    for label, devices in groups.items():
        if not devices:
            print(f"\n{label}: nothing found in OpenRGB.")
            continue
        print(f"\n=== {label}: off, then orange, then off ===")
        _set_static(devices, (0, 0, 0))
        input(f"{label} should be off now. Enter to light it up.")
        _set_static(devices, ORANGE)
        input(f"{label} should be solid orange now (Static mode - stays even if you close "
              "OpenRGB/this script). Enter to turn off + continue.")
        _set_static(devices, (0, 0, 0))

    print("\nWhich of the three groups actually reacted?")


def verify_all():
    verify_fans()
    verify_rgb()


def probe_hub():
    m = "control hub"
    print("=== everything liquidctl can see ===")
    run("list", "--verbose")
    print("\n=== Control Hub status ===")
    run("status", "--verbose", match=m)
    print("\n=== LED/accessory detection (status can't show this - write-only) ===")
    run("initialize", match=m)


def probe_aio():
    print("=== looking for the Panorama pump as its own liquidctl device ===")
    print("(Control Hub and pump are usually separate USB devices)")
    run("list", "--verbose")
    run("status", "--verbose")
    print(
        "\nIf a second device shows up: does its status include a temperature "
        "field (e.g. 'Liquid temperature')? If nothing shows up beyond the "
        "Control Hub, the pump likely isn't liquidctl-recognised at all - "
        "check Windows Device Manager for an unrecognised USB device."
    )


COMMANDS = {"fans": verify_fans, "rgb": verify_rgb, "all": verify_all,
            "hub": probe_hub, "aio": probe_aio}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]]()
