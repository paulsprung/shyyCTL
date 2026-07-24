"""Manual test of app/panorama.py against the real Tryx Panorama - the
display equivalent of tools/test_app.py.

Needs:
    pip install pyusb libusb-package
    adb.exe on PATH (Android platform-tools - just the zip, no Android
    Studio: https://developer.android.com/tools/releases/platform-tools)

The protocol implemented here (handshake/ping/screen config, all in
app/panorama.py) is confirmed against a real captured KANALI session this
project recorded - Wireshark + USBPcap, decoded byte-for-byte, correlated
against a precise documented action timeline. Every message builder in
app/panorama.py was verified to reproduce real captured bytes exactly
before this file was updated to use them - see that module's docstring
for the full story. This is NOT reed-tpse's JSON protocol; an earlier
version of this project ported that by mistake and it turned out to not
apply to this hardware at all (confirmed: zero response, in multiple real
captures, to every request built that way).

This panel is talked to on Windows via usbprint.sys (Windows' built-in
USB Printing Support class driver), which is what KANALI's own installer
sets up for it - confirmed against Device Manager. panel() below connects
that way by default (PanoramaPrint, via CreateFile/ReadFile/WriteFile - no
Device Manager changes needed), falling back to WinUSB (PanoramaUSB) only
if that fails, which requires having replaced usbprint.sys via Zadig
(https://zadig.akeo.ie) and breaks KANALI's own access while active.

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

from app.panorama import (
    PanoramaUSB, PanoramaPrint, Media,
    find_usb_panel, find_print_panel, find_usbprint_path,
    usb_probe, media_filename, build_ping_frame,
)

_panel = None  # PanoramaPrint | PanoramaUSB | None


def panel():
    global _panel
    if _panel is not None and _panel.connected:
        return _panel
    print("Connecting to the panel via usbprint.sys (CreateFile/ReadFile/WriteFile)...")
    found = find_print_panel()
    if found is not None:
        print("Connected via usbprint.sys.")
        _panel = found
        return _panel
    print("usbprint.sys transport failed - trying WinUSB fallback "
          "(only works if Zadig already replaced the driver)...")
    found = find_usb_panel()
    if found is None:
        print("No panel found via either transport. Run option 6 (raw USB visibility) "
              "or 7 (usbprint.sys path check) for details.")
        sys.exit(1)
    print("Connected via WinUSB.")
    _panel = found
    return _panel


def test_find():
    p = panel()
    print("Connected:", p.connected)


def test_handshake():
    p = panel()
    info = p.handshake()
    if info is None:
        print("No/invalid response to handshake.")
        return
    for key, value in info.items():
        print(f"  {key}: {value}")


def test_ping():
    p = panel()
    for _ in range(3):
        ok = p.ping()
        print("ping:", "ok" if ok else "no response")
        time.sleep(1)


def test_device_media_list():
    """Lists media the panel itself reports having (via the real protocol's
    f103 request), as opposed to option 4 which lists via adb's filesystem
    view - the two should mostly agree, but this one shows the exact names
    (with the .h264_WxH suffix) to reference in set_screen_config."""
    p = panel()
    items = p.list_media()
    if not items:
        print("No media reported (or request failed).")
        return
    for item in items:
        print(f"  {item['path']}  ({item['size']} bytes)")


def test_brightness():
    p = panel()
    for value in (20, 100, 0, 60):
        print(f"Setting brightness to {value}...")
        ok = p.set_brightness(value)
        print("  ok" if ok else "  no response")
        time.sleep(1)


def test_adb_media_list():
    media = Media()
    if not media.is_connected():
        print("adb doesn't see the panel as a device. `adb devices` to check manually - "
              "may need to accept a USB debugging prompt on first connect, if the "
              "panel's Android side shows one, or a Windows ADB driver.")
        return
    for name in media.list_media():
        print(" ", name)


def test_set_default_image():
    """Uploads a local image/gif and sets it as the persistent boot/default
    screen. This is the one CAM/KANALI-free thing this whole project exists
    to prove out for the display.

    Only single-file uploads for now - every confirmed real screen-config
    command carried exactly one 'active media' filename, not a list, so
    there's no confirmed multi-file slideshow mechanism to reproduce yet."""
    raw = input("Path to the image/gif to upload (local file): ").strip().strip('"')
    if not Path(raw).is_file():
        print("File not found:", raw)
        return
    remote_name = Path(raw).name

    media = Media()
    if not media.is_connected():
        print("adb doesn't see the panel - see option 5 for details.")
        return
    print(f"Pushing {raw} -> /sdcard/pcMedia/{remote_name} ...")
    if not media.push(raw, remote_name):
        print("Push failed.")
        return
    print("Pushed. Waiting a few seconds for the device to transcode it "
          "(unverified how long this actually takes - the real filename "
          "only becomes referenceable once transcoding finishes)...")
    time.sleep(3)

    p = panel()
    active = media_filename(remote_name)
    print(f"Setting screen config, active media = {active!r} ...")
    ok = p.set_screen_config(active, brightness=p.last_brightness)
    print("ok" if ok else "no response")
    print("Should be showing now on the physical panel.")


def test_usb_probe():
    """Checks the raw USB bus for VID 0x391A (Tryx), independent of any
    driver/COM port/KANALI - useful if 1-4 fail to connect at all."""
    result = usb_probe()
    if not result["ok"]:
        print("NOT FOUND:", result["reason"])
        return
    print(f"Found {len(result['devices'])} Tryx USB device(s):")
    for dev in result["devices"]:
        tag = " <-- Panorama (PID 0x1011)" if dev["is_panorama"] else ""
        print(f"  bus={dev['bus']} addr={dev['address']} "
              f"vid={dev['vendor_id']} pid={dev['product_id']}{tag}")
        if dev.get("manufacturer") or dev.get("product"):
            print(f"    manufacturer={dev.get('manufacturer')!r} product={dev.get('product')!r}")
        if dev["accessible"]:
            print(f"    accessible=True, interfaces={dev['num_interfaces']}, "
                  f"endpoints={dev['endpoints']}")
        else:
            print(f"    accessible=False ({dev['access_error']})")
            print(f"    -> {dev['likely_cause']}")


def test_check_usbprint_path():
    """Runs just the SetupAPI device-path lookup PanoramaPrint.connect()
    uses internally, without opening a handle - confirms whether Windows
    currently has a usbprint.sys device present for this VID/PID at all,
    independent of whether anything else has it open right now."""
    path = find_usbprint_path()
    if path is None:
        print("No usbprint.sys device found for VID 0x391A:0x1011. Check Device Manager: "
              "the entry's Driver tab should say usbprint.sys, not WinUSB - if it says "
              "WinUSB, Zadig's replacement from earlier testing is still active; uninstall "
              "that device and let Windows/KANALI rebind the default driver.")
    else:
        print("Found:", path)


def test_raw_ping_dump():
    """Bypasses handshake()/ping()'s parsing - writes the exact same
    'hello?' ping frame ping() sends, then dumps the raw reply bytes
    regardless of shape. Useful if option 3 (ping) reports no response."""
    p = panel()
    frame = build_ping_frame()
    print(f"Writing {len(frame)} bytes: {frame.hex()}")
    p._write(frame)
    print("Write did not raise. Reading for up to 3s...")
    raw = p._read_message(timeout_s=3.0)
    if raw:
        print(f"  got {len(raw)} bytes: {raw.hex()}")
    else:
        print("Nothing at all came back in 3s after writing.")


MENU = {
    "1": ("Find + connect (usbprint.sys, falls back to WinUSB)", test_find),
    "2": ("Full handshake info", test_handshake),
    "3": ("Ping x3 (keepalive)", test_ping),
    "4": ("List media the panel itself reports (real protocol)", test_device_media_list),
    "5": ("List media via adb (filesystem view)", test_adb_media_list),
    "6": ("Check raw USB visibility (pyusb, VID 0x391A)", test_usb_probe),
    "7": ("Check usbprint.sys device path (diagnostic, no handle opened)", test_check_usbprint_path),
    "8": ("Brightness sweep (20 -> 100 -> 0 -> 60)", test_brightness),
    "9": ("Upload an image/gif and set it as the default/boot screen", test_set_default_image),
    "10": ("Raw ping byte dump (diagnostic - use if option 3 reports no response)", test_raw_ping_dump),
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
