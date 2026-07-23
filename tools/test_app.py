"""End-to-end test of app/hub.py + app/openrgb_bridge.py + app/scan.py against
real hardware. The next step up from tools/probe.py: this exercises the
actual modules shyyctl will run, not one-off liquidctl/OpenRGB calls.

Run from the repo root and pick what to test:
    python tools/test_app.py

liquidctl LED colour control is NOT exercised here anymore - confirmed this
session that the 360mm units don't come out right through it. RGB is
OpenRGB-only now (hub, mobo, RAM); liquidctl is fan PWM/RPM only.
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

CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
ORANGE = (255, 107, 61)

# Known-good LED counts from this session's manual verification in OpenRGB's
# own GUI - this hub can't report them itself (see scan.py). Test-only
# stand-in for what the real setup screen will get from a human picking the
# fan model.
VERIFIED_COUNTS = {"led1": 36, "led3": 24, "led4": 36, "led5": 16}

_hub: Hub | None = None
_rgb: OpenRgbBridge | None = None


def hub() -> Hub:
    global _hub
    if _hub is None:
        _hub = Hub(CONFIG["hub"]["match"])
    if not _hub.connected and not _hub.connect():
        print("Could not connect to hub:", _hub.last_error)
        sys.exit(1)
    return _hub


def rgb() -> OpenRgbBridge:
    global _rgb
    if _rgb is None:
        _rgb = OpenRgbBridge(**CONFIG["openrgb"])
    if not _rgb.connected and not _rgb.connect():
        print("Could not connect to OpenRGB:", _rgb.last_error)
    return _rgb


def test_fans():
    h = hub()
    print("Channels:", h.channels)
    raw = h._run("status", "--verbose", match=h.match)
    print(f"\nraw stdout:\n{raw}")
    for r in h.status():
        print(" ", r)
    for ch in h.channels.get("fan", []):
        print(f"{ch} rpm ->", h.rpm(ch))


def test_scan():
    h = hub()
    r = rgb()
    scan = Scan(h, r)
    scan.start(measure_curves=False)
    while scan.running:
        print(" ", scan.step)
        time.sleep(1)
    print(json.dumps(scan.state(), indent=2))


def test_list_openrgb():
    r = rgb()
    print(json.dumps(r.describe_all(), indent=2))


def test_hub_leds_openrgb():
    r = rgb()
    devices = r.hub_leds()
    if not devices:
        print("Hub not found in OpenRGB.")
        return
    device = devices[0]
    for ch, count in VERIFIED_COUNTS.items():
        print(f"\n{ch}: resizing to {count} LEDs, then orange...")
        if not r.resize_zone(device, ch, count):
            print("  resize failed or zone not found")
            continue
        try:
            r.set_zone_color(device, ch, ORANGE)
        except Exception as exc:
            print("  FAILED:", exc)
            continue
        input(f"  {ch} should be solid orange now. Enter to turn off + continue.")
        r.set_zone_color(device, ch, (0, 0, 0))


def test_mobo():
    r = rgb()
    devices = r.motherboard()
    if not devices:
        print("Motherboard not found in OpenRGB.")
        return
    r.set_color(devices, ORANGE)
    input("mobo should be solid orange now. Enter to turn off.")
    r.set_color(devices, (0, 0, 0))


def test_ram():
    r = rgb()
    devices = r.ram()
    if not devices:
        print("RAM not found in OpenRGB.")
        return
    r.set_color(devices, ORANGE)
    input("RAM should be solid orange now. Enter to turn off.")
    r.set_color(devices, (0, 0, 0))


MENU = {
    "1": ("List every OpenRGB device + zone (raw)", test_list_openrgb),
    "2": ("Fan PWM/RPM via liquidctl", test_fans),
    "3": ("Full hardware scan (fans + LED presence + mobo/ram)", test_scan),
    "4": ("Hub LEDs via OpenRGB (resized + coloured)", test_hub_leds_openrgb),
    "5": ("Motherboard via OpenRGB", test_mobo),
    "6": ("RAM via OpenRGB", test_ram),
}

if __name__ == "__main__":
    print("What do you want to test?")
    for key, (label, _) in MENU.items():
        print(f"  {key}. {label}")
    choice = input("> number(s), comma-separated: ").strip()
    picks = [c.strip() for c in choice.split(",") if c.strip()]
    unknown = [c for c in picks if c not in MENU]
    if not picks or unknown:
        print("Unknown choice(s):", unknown or "(nothing entered)")
        sys.exit(1)
    for pick in picks:
        label, fn = MENU[pick]
        print(f"\n=== {label} ===")
        fn()
    print("\nDone.")
