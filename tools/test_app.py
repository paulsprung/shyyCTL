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

print("\n=== addressing test: each present LED channel, firmware fixed mode ===")
for entry in scan.results:
    if not entry["present"] or not entry["led_channel"]:
        continue
    ch = entry["led_channel"]
    print(f"\n{ch}: setting fixed orange...")
    try:
        hub.set_color(ch, "fixed", ["ff6b3d"])
    except Exception as exc:
        print("  FAILED:", exc)
        continue
    input(f"  {ch} should be solid orange now. Enter to turn off + continue.")
    hub.set_color(ch, "fixed", ["000000"])

print("\nDone.")
