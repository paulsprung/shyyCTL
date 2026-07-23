"""End-to-end test of app/hub.py + app/openrgb_bridge.py + app/scan.py against
real hardware. The next step up from tools/probe.py: this exercises the
actual modules shyyctl will run, not one-off liquidctl/OpenRGB calls.

Run from the repo root:
    python tools/test_app.py
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml

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

print("\nConnecting to OpenRGB...")
if not rgb.connect():
    print("FAILED (not fatal - scan will note it):", rgb.last_error)
else:
    print("Connected.")

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
