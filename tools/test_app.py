"""End-to-end test of app/hub.py + app/openrgb_bridge.py + app/scan.py against
real hardware. The next step up from tools/probe.py: this exercises the
actual modules shyyctl will run, not one-off liquidctl/OpenRGB calls.

Run from the repo root:
    python tools/test_app.py
"""

import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml

logging.basicConfig(level=logging.DEBUG, format="%(name)s %(levelname)s: %(message)s")

from app.hub import Hub
from app.openrgb_bridge import OpenRgbBridge
from app.scan import Scan

config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))

hub = Hub(config["hub"]["match"])
rgb = OpenRgbBridge(**config["openrgb"])

print("Connecting to hub...")
if not hub.connect():
    print("FAILED:", hub.last_error)
    sys.exit(1)
print("Connected:", hub.description)
print("Channels:", hub.channels)

print("\n=== direct status() check, right after connect, before any scan ===")
raw = hub._run("status", "--verbose", match=hub.match)
print(f"raw stdout ({len(raw or '')} chars):")
print(repr(raw))
readings = hub.status()
print(f"\nparsed into {len(readings)} readings:")
for r in readings:
    print(" ", r)
print(f"\nhub.rpm('fan1') -> {hub.rpm('fan1')}")
input("\nEnter to continue to the scan.")

# OpenRGB is deliberately NOT connected here. scan.py connects it itself,
# only after fan probing is done (see _scan_openrgb) - connecting it early
# was ruled out as the cause of the all-empty fan results (same result with
# or without it), kept anyway since it's still the more correct order.

print("\nRunning scan (no curve measurement, quick)...")
scan = Scan(hub, rgb)
scan.start(measure_curves=False)
while scan.running:
    print(" ", scan.step)
    time.sleep(1)

print("\n=== scan result ===")
print(json.dumps(scan.state(), indent=2))

print("\n=== addressing test, path 1: liquidctl firmware fixed mode ===")
print("(channel 1 is expected to fail here - confirmed liquidctl driver bug, not new)")
for entry in scan.results:
    if not entry["present"] or not entry["led_channel"]:
        continue
    ch = entry["led_channel"]
    print(f"\n{ch}: setting fixed orange via liquidctl...")
    try:
        hub.set_color(ch, "fixed", ["ff6b3d"])
    except Exception as exc:
        print("  FAILED:", exc)
        continue
    input(f"  {ch} should be solid orange now. Enter to turn off + continue.")
    hub.set_color(ch, "fixed", ["000000"])

print("\n=== addressing test, path 2: OpenRGB (hub LEDs incl. channel 1, mobo, RAM) ===")
if not rgb.connected and not rgb.connect():
    print("Could not connect to OpenRGB:", rgb.last_error)
else:
    # Known-good counts from this session's manual verification in OpenRGB's
    # own GUI - this hub can't report them itself (see scan.py), so this is
    # a test-only stand-in for what the real setup screen will get from a
    # human picking the fan model.
    VERIFIED_COUNTS = {"led1": 36, "led3": 24, "led4": 36, "led5": 16}

    hub_devices = rgb.hub_leds()
    if not hub_devices:
        print("Hub not found in OpenRGB.")
    else:
        device = hub_devices[0]
        for entry in scan.results:
            ch = entry["led_channel"]
            if not ch or ch not in VERIFIED_COUNTS:
                continue
            count = VERIFIED_COUNTS[ch]
            print(f"\n{ch}: resizing to {count} LEDs, then orange via OpenRGB...")
            if not rgb.resize_zone(device, ch, count):
                print("  resize failed or zone not found")
                continue
            try:
                rgb.set_zone_color(device, ch, (255, 107, 61))
            except Exception as exc:
                print("  FAILED:", exc)
                continue
            input(f"  {ch} should be solid orange now (via OpenRGB). Enter to turn off + continue.")
            rgb.set_zone_color(device, ch, (0, 0, 0))

    mobo_devices = rgb.motherboard()
    if not mobo_devices:
        print("\nMotherboard not found in OpenRGB.")
    else:
        print("\nmobo: orange via OpenRGB...")
        rgb.set_color(mobo_devices, (255, 107, 61))
        input("  mobo should be solid orange now. Enter to turn off.")
        rgb.set_color(mobo_devices, (0, 0, 0))

    ram_devices = rgb.ram()
    if not ram_devices:
        print("\nRAM not found in OpenRGB.")
    else:
        print("\nram: orange via OpenRGB...")
        rgb.set_color(ram_devices, (255, 107, 61))
        input("  RAM should be solid orange now. Enter to turn off.")
        rgb.set_color(ram_devices, (0, 0, 0))

print("\nDone.")
