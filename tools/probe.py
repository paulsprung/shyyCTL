"""Manual hardware probe - no shyyctl app involved, just liquidctl/OpenRGB directly.

Run this against the real hardware before building anything on top of it. It
only runs commands and prints what comes back; it does not decide anything.
Close NZXT CAM first - it holds the HID handle open and liquidctl will not
see the device while CAM is running.

Usage:
    python tools/probe.py hub     # NZXT Control Hub: channels, fans, LEDs
    python tools/probe.py aio     # look for the Panorama pump as its own device
    python tools/probe.py ram     # G.Skill RAM via a running OpenRGB (SDK server on)
"""

import subprocess
import sys
import time


def run(*args: str):
    print(f"\n$ liquidctl {' '.join(args)}")
    try:
        result = subprocess.run(["liquidctl", *args], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        print("liquidctl not found on PATH - pip install liquidctl")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("(timed out)")
        return
    print(result.stdout.strip() or "(no stdout)")
    if result.stderr.strip():
        print("stderr:", result.stderr.strip())


def confirm(prompt: str) -> bool:
    return input(f"{prompt} [y/N] ").strip().lower() == "y"


def probe_hub():
    print("=== everything liquidctl can see ===")
    run("list")
    run("list", "--verbose")

    print("\n=== full status ===")
    run("status")
    run("status", "--verbose")

    if confirm("\nDrive fan1 to 50% for 5s (confirms control + tach response)?"):
        run("set", "fan1", "speed", "50")
        time.sleep(5)
        run("status")
        run("set", "fan1", "speed", "40")

    if confirm("\nTest super-fixed (per-LED) on led1 with two different colours?"):
        run("set", "led1", "color", "super-fixed", "ff0000", "0000ff")
        input("Look at the strip: did only the first LED/segment go red, "
              "rest something else? Enter to continue.")
        run("set", "led1", "color", "fixed", "000000")

    print("\nPaste everything above back, plus your answers to the two questions.")


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


COMMANDS = {"hub": probe_hub, "aio": probe_aio, "ram": probe_ram}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]]()
