"""Manual test of app/panorama.py against the real Tryx Panorama - the
display equivalent of tools/test_app.py.

Needs:
    pip install pyserial
    adb.exe on PATH (Android platform-tools - just the zip, no Android
    Studio: https://developer.android.com/tools/releases/platform-tools)

Nothing here is verified yet - the protocol was ported from reed-tpse,
which tested against the Panorama SE/360, not this project's 240mm/2-fan
unit. Run 'find' first; if handshake fails, the protocol may need
adjusting for this specific panel before anything else will work.

Usage:
    python tools/test_panorama.py
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import logging
logging.basicConfig(level=logging.DEBUG, format="%(name)s %(levelname)s: %(message)s")

from app.panorama import Panorama, Media, find_device

_panel: Panorama | None = None


def panel() -> Panorama:
    global _panel
    if _panel is not None and _panel.connected:
        return _panel
    print("Searching for the panel on every serial port (tries handshake on each)...")
    port = find_device()
    if port is None:
        print("No panel found. Is it plugged in? Close KANALI if it's running "
              "(likely holds the port open, same story as NZXT CAM).")
        sys.exit(1)
    print(f"Found on {port}")
    _panel = Panorama(port)
    _panel.connect()
    return _panel


def test_list_ports():
    import serial.tools.list_ports
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("No serial ports detected at all - Windows may not have a driver "
              "for the panel (check Device Manager > Ports (COM & LPT); if it's "
              "sitting under 'Other devices' with a warning icon instead, that's "
              "the problem, not this script).")
        return
    for p in ports:
        print(f"  {p.device}: {p.description}  (hwid={p.hwid})")


def test_handshake_on_port():
    port_name = input("COM port to test directly (e.g. COM5): ").strip()
    p = Panorama(port_name)
    if not p.connect():
        print("Could not open that port.")
        return
    try:
        print("Sending handshake...")
        info = p.handshake()
        print("Result:", info if info is not None else "no/invalid response")
    finally:
        p.disconnect()


def test_find():
    p = panel()
    print("Connected:", p.connected, "on", p.port_name)


def test_handshake():
    p = panel()
    info = p.handshake()
    if info is None:
        print("No/invalid response to handshake.")
        return
    for key, value in info.items():
        print(f"  {key}: {value}")


def test_brightness():
    p = panel()
    for value in (20, 100, 60):
        print(f"Setting brightness to {value}...")
        resp = p.set_brightness(value)
        print("  response:", resp)
        time.sleep(1)


def test_media_list():
    media = Media()
    if not media.is_connected():
        print("adb doesn't see the panel as a device. `adb devices` to check manually - "
              "may need to accept a USB debugging prompt on first connect, if the "
              "panel's Android side shows one, or a Windows ADB driver.")
        return
    for name in media.list_media():
        print(" ", name)


def test_set_default_image():
    """Uploads the image and sets it as the persistent boot/default screen -
    single image, full screen, looping. This is the one CAM/KANALI-free
    thing this whole project exists to prove out for the display."""
    image_path = input("Path to the image to upload (local file): ").strip().strip('"')
    if not Path(image_path).is_file():
        print("File not found:", image_path)
        return
    remote_name = Path(image_path).name

    media = Media()
    if not media.is_connected():
        print("adb doesn't see the panel - see option 4 for details.")
        return
    print(f"Pushing {image_path} -> /sdcard/pcMedia/{remote_name} ...")
    if not media.push(image_path, remote_name):
        print("Push failed.")
        return
    print("Pushed. Setting screen config...")

    p = panel()
    resp = p.set_screen_config([remote_name], screen_mode="Full Screen",
                               ratio="2:1", play_mode="Single")
    print("response:", resp)
    print("Should be showing now - and should persist across a reboot, per reed-tpse's README.")


MENU = {
    "1": ("Find + connect (handshake test)", test_find),
    "2": ("Full handshake info", test_handshake),
    "3": ("Brightness sweep (20 -> 100 -> 60)", test_brightness),
    "4": ("List media on the panel (via adb)", test_media_list),
    "5": ("Upload an image and set it as the default/boot screen", test_set_default_image),
    "6": ("List every serial port Windows/pyserial can see (no handshake)", test_list_ports),
    "7": ("Handshake on a specific COM port you name", test_handshake_on_port),
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
