"""Manual hardware probe - no shyyctl app involved, just liquidctl/OpenRGB directly.

liquidctl docs for the NZXT Smart Device family (V1/V2, HUE 2, and presumably
Control Hub) are explicit: after power-on or any hardware change, the device
needs 'initialize' once before fan control and lighting are reliable. Earlier
runs of this script never called it before touching LEDs, which is the
leading suspect for why channel 1 looked broken - so it's now called first,
every time, right before the commands that depend on it, in the same
liquidctl session.

RGB is back on liquidctl for the hub (all channels, tested individually) -
OpenRGB couldn't drive any of the hub's channels even after resizing, but is
solid for mobo/RAM, so it stays for those.

Close NZXT CAM and Armoury Crate/Aura Creator first - they hold devices open
or fight over who's driving the lighting.

Usage:
    python tools/probe.py fans    # each fan channel off, one at a time (initializes first)
    python tools/probe.py hubrgb  # all LEDs off, then each channel tested y/n, summary at the end
    python tools/probe.py debug1  # compare working vs broken channel with --debug + candidate fixes
    python tools/probe.py mobo    # mobo LED via OpenRGB
    python tools/probe.py ramrgb  # RAM via OpenRGB
    python tools/probe.py all     # fans, then hubrgb, then mobo, then ramrgb
    python tools/probe.py hub     # liquidctl list/status/initialize - info only
    python tools/probe.py aio     # look for the Panorama pump as its own liquidctl device
    python tools/probe.py conflicts  # processes/services that might be fighting over lighting
"""

import os
import subprocess
import sys
import time

# Windows consoles default to cp1252, which can't encode the box-drawing
# characters liquidctl's human-readable output uses. Forcing the child
# process's own stdout to utf-8 avoids it crashing mid-print.
_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}

FAN_CHANNELS = [f"fan{i}" for i in range(1, 6)]
LED_CHANNELS = [f"led{i}" for i in range(1, 6)]
ORANGE_HEX = "ff6b3d"
ORANGE_RGB = (255, 107, 61)
SETTLE = 0.5  # give the device/SDK server time to apply a write before checking


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


def _init_hub():
    print("=== initialize (required once per power-on before fans/LEDs are reliable) ===")
    run("initialize", match="control hub")


def verify_fans():
    m = "control hub"
    _init_hub()
    print("\n=== fans: one channel at a time ===")
    for ch in FAN_CHANNELS:
        run("set", ch, "speed", "0", match=m)
        input(f"{ch} at 0% now - note whether it stopped (or was already empty). Enter to restore.")
        run("set", ch, "speed", "40", match=m)


def verify_hub_rgb():
    m = "control hub"
    _init_hub()

    print("\n=== clean baseline: all hub LED channels off ===")
    for ch in LED_CHANNELS:
        run("set", ch, "color", "off", match=m)
    time.sleep(SETTLE)

    print("\n=== testing each channel, one at a time - just answer y/n ===")
    results = {}
    for ch in LED_CHANNELS:
        run("set", ch, "color", "fixed", ORANGE_HEX, match=m)
        time.sleep(SETTLE)
        answer = input(f"Did {ch} light up orange? [y/N] ").strip().lower()
        results[ch] = answer == "y"
        run("set", ch, "color", "off", match=m)
        time.sleep(SETTLE)

    print("\n=== summary ===")
    for ch, ok in results.items():
        print(f"  {ch}: {'reacted' if ok else 'NO REACTION'}")


def debug_channel1():
    """Compare the raw USB report for a known-working channel against the
    broken one, plus a couple of cheap candidate fixes (resend, other mode).
    Paste the full output back - if the reports differ by an obvious byte,
    hub.py can send a corrected report directly over HID, bypassing whatever
    is wrong in liquidctl's driver for this one channel."""
    m = "control hub"
    _init_hub()

    print("\n=== led5 (known working) with --debug ===")
    run("set", "led5", "color", "fixed", ORANGE_HEX, "--debug", match=m)
    input("Enter to continue (led5 should be orange now).")
    run("set", "led5", "color", "off", match=m)

    print("\n=== led1 (broken) with --debug, same command ===")
    run("set", "led1", "color", "fixed", ORANGE_HEX, "--debug", match=m)
    input("Enter to continue (led1 - note if anything happened).")

    print("\n=== led1 again, sent twice in a row (in case the first write gets dropped) ===")
    run("set", "led1", "color", "fixed", ORANGE_HEX, match=m)
    run("set", "led1", "color", "fixed", ORANGE_HEX, match=m)
    input("Enter to continue (note if anything happened this time).")
    run("set", "led1", "color", "off", match=m)

    print("\n=== led1 with a different mode (fixed might specifically be broken, not the channel) ===")
    run("set", "led1", "color", "spectrum-wave", match=m)
    input("Enter to continue (note if led1 does ANYTHING - even the wrong colour/animation counts).")
    run("set", "led1", "color", "off", match=m)


def _openrgb_group(device_type_name: str, name_contains: str | None = None):
    from openrgb import OpenRGBClient
    from openrgb.utils import DeviceType
    client = OpenRGBClient(address="127.0.0.1", port=6742, name="shyyctl-probe")
    devices = [d for d in client.devices if d is not None]
    if len(devices) != len(client.devices):
        print(f"({len(client.devices) - len(devices)} device(s) came back as None from "
              "OpenRGB - probably still settling after a restart)")
    dtype = getattr(DeviceType, device_type_name)
    return [d for d in devices
            if d.type == dtype and (name_contains is None or name_contains in d.name)]


def _verify_openrgb(label, devices):
    if not devices:
        print(f"\n{label}: nothing found in OpenRGB.")
        return
    from openrgb.utils import RGBColor

    def set_static(color):
        for device in devices:
            for i, mode in enumerate(device.modes):
                if mode.name.lower() == "static":
                    device.set_mode(i)
                    break
        for device in devices:
            device.set_color(RGBColor(*color))
        time.sleep(SETTLE)
        for device in devices:
            active = device.modes[device.active_mode].name if device.modes else "?"
            print(f"  {device.name}: active mode right after this write is '{active}'")

    print(f"\n=== {label}: off, then orange, then off ===")
    set_static((0, 0, 0))
    input(f"{label} should be off now. Enter to light it up.")
    set_static(ORANGE_RGB)
    print("  waiting 4s, then re-checking the mode - if it changed on its own, "
          "something else is overwriting this (see 'python tools/probe.py conflicts')")
    time.sleep(4)
    for device in devices:
        try:
            active = device.modes[device.active_mode].name if device.modes else "?"
        except Exception as exc:
            active = f"<could not re-read: {exc}>"
        print(f"  {device.name}: mode after the wait is '{active}'")
    input(f"{label} - still solid orange, or did it change/blink off on its own? "
          "Enter to turn off + continue.")
    set_static((0, 0, 0))


def verify_mobo():
    try:
        devices = _openrgb_group("MOTHERBOARD")
    except ImportError:
        print("pip install openrgb-python")
        return
    except OSError as exc:
        print("Could not reach OpenRGB SDK server:", exc)
        return
    _verify_openrgb("mobo", devices)


def verify_ram_rgb():
    try:
        devices = _openrgb_group("DRAM")
    except ImportError:
        print("pip install openrgb-python")
        return
    except OSError as exc:
        print("Could not reach OpenRGB SDK server:", exc)
        return
    _verify_openrgb("ram", devices)


SUSPECT_KEYWORDS = [
    "nzxt", "armoury crate", "auracreator", "lightingservice",
    "icue", "corsair", "mysticlight", "signalrgb", "fanxpert",
    "ai suite", "kanali",
]


def check_conflicts():
    """Anything below marked Running is a candidate for fighting over who
    controls the lighting - close the process / stop the service, then
    retest. A blink-then-revert-to-off pattern is the classic symptom."""
    print("=== running processes matching known RGB/lighting software ===")
    try:
        result = subprocess.run(["tasklist", "/fo", "csv", "/nh"],
                                 capture_output=True, text=True, timeout=15)
        hits = [line for line in result.stdout.splitlines()
                if any(k in line.lower() for k in SUSPECT_KEYWORDS)]
        print("\n".join(hits) if hits else "  none of the known suspects found running.")
    except Exception as exc:
        print("Could not list processes:", exc)

    print("\n=== Windows services matching the same list ===")
    pattern = "|".join(SUSPECT_KEYWORDS)
    ps_cmd = (
        f"Get-Service | Where-Object {{ $_.DisplayName -match '{pattern}' -or "
        f"$_.Name -match '{pattern}' }} | Select-Object Name,DisplayName,Status,StartType "
        "| Format-Table -AutoSize"
    )
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd],
                                 capture_output=True, text=True, timeout=20, env=_ENV)
        print(result.stdout.strip() or "  (no matching services)")
        if result.stderr.strip():
            print("stderr:", result.stderr.strip())
    except Exception as exc:
        print("Could not query services:", exc)


def verify_all():
    verify_fans()
    verify_hub_rgb()
    verify_mobo()
    verify_ram_rgb()


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


COMMANDS = {"fans": verify_fans, "hubrgb": verify_hub_rgb, "debug1": debug_channel1,
            "mobo": verify_mobo, "ramrgb": verify_ram_rgb, "all": verify_all,
            "hub": probe_hub, "aio": probe_aio, "conflicts": check_conflicts}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]]()
