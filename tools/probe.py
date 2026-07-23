"""Manual hardware probe - no shyyctl app involved, just liquidctl/OpenRGB directly.

Run this against the real hardware before building anything on top of it. It
only runs commands and prints what comes back; it does not decide anything.
Close NZXT CAM first - it holds the HID handle open and liquidctl will not
see the device while CAM is running.

Usage:
    python tools/probe.py hub    # NZXT Control Hub: list + status
    python tools/probe.py verify # does control actually work? fans + fan LEDs, all at once
    python tools/probe.py aura   # ASUS Aura LED Controller (mobo LED), all channels at once
    python tools/probe.py aio    # look for the Panorama pump as its own device
    python tools/probe.py ram    # G.Skill RAM via a running OpenRGB (SDK server on)

None of this cares which physical fan/LED is which channel - that mapping
happens later, in the webUI's own setup screen. This just confirms every
piece of hardware actually responds to a command.

Two liquidctl devices are now on this system (Control Hub + Aura) and both
name their channels led1/led2/... - every call below is scoped with --match
so liquidctl never has to guess which device a channel name belongs to.
"""

import os
import subprocess
import sys

# Windows consoles default to cp1252, which can't encode the box-drawing
# characters liquidctl's human-readable output uses. Forcing the child
# process's own stdout to utf-8 avoids it crashing mid-print.
_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


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
        return
    print(result.stdout.strip() or "(no stdout)")
    if result.stderr.strip():
        print("stderr:", result.stderr.strip())


FAN_CHANNELS = [f"fan{i}" for i in range(1, 6)]
LED_CHANNELS = [f"led{i}" for i in range(1, 6)]


def probe_hub():
    m = "control hub"
    print("=== everything liquidctl can see ===")
    run("list", "--verbose")

    print("\n=== Control Hub status ===")
    run("status", "--verbose", match=m)

    print("\nRun 'python tools/probe.py verify' next to confirm control actually works.")


def verify_hub():
    """All fan channels down together, all LED channels green together. One look, done."""
    m = "control hub"

    print("=== fans: all channels to 0% at once ===")
    for ch in FAN_CHANNELS:
        run("set", ch, "speed", "0", match=m)
    input("Fans should be noticeably slower/off now. Enter to restore to 40%.")
    for ch in FAN_CHANNELS:
        run("set", ch, "speed", "40", match=m)

    print("\n=== fan LEDs: all channels to green at once ===")
    for ch in LED_CHANNELS:
        run("set", ch, "color", "fixed", "00ff00", match=m)
    input("Fan LEDs should be green now. Enter to turn off.")
    for ch in LED_CHANNELS:
        run("set", ch, "color", "off", match=m)

    print("\nDid fans + fan LEDs both react? That's all this checks.")


def probe_aura():
    m = "aura"
    print("=== ASUS Aura LED Controller (mobo LED) - all 4 channels at once ===")
    run("status", "--verbose", match=m)

    for ch in ("led1", "led2", "led3", "led4"):
        run("set", ch, "color", "static", "ff6b3d", match=m)
    input("\nMobo LED should be solid orange now. Enter to turn off.")
    for ch in ("led1", "led2", "led3", "led4"):
        run("set", ch, "color", "off", match=m)

    print("\nDid the mobo LED react? That's all this checks.")


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


def probe_ram():
    try:
        from openrgb import OpenRGBClient
        from openrgb.utils import RGBColor
    except ImportError:
        print("pip install openrgb-python")
        return
    print("Requires OpenRGB running with SDK Server enabled (Settings > SDK Server, port 6742).")
    try:
        client = OpenRGBClient(address="127.0.0.1", port=6742, name="shyyctl-probe")
    except Exception as exc:
        print("Could not reach OpenRGB SDK server:", exc)
        return
    for device in client.devices:
        print(f"\n{device.name}  type={device.type}  leds={len(device.leds)}")
        for zone in device.zones:
            print(f"  zone: {zone.name}  leds={zone.leds_count}")

    if not client.devices:
        print("\nNo devices - is the RAM detected inside OpenRGB itself?")
        return
    for device in client.devices:
        device.set_color(RGBColor(0, 255, 0))
    input("\nRAM should be solid green now. Enter to turn off.")
    for device in client.devices:
        device.set_color(RGBColor(0, 0, 0))

    print("\nDid the RAM react? That's all this checks.")


COMMANDS = {"hub": probe_hub, "verify": verify_hub, "aura": probe_aura,
            "aio": probe_aio, "ram": probe_ram}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]]()
