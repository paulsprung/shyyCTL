"""Manual test of app/panorama.py against the real Tryx Panorama - the
display equivalent of tools/test_app.py.

Needs:
    pip install pyserial pyusb libusb-package
    adb.exe on PATH (Android platform-tools - just the zip, no Android
    Studio: https://developer.android.com/tools/releases/platform-tools)

Confirmed against this hardware this session: this panel (VID 0x391A /
PID 0x1011, "RK PANO") is talked to on Windows via usbprint.sys - Windows'
built-in USB Printing Support class driver, which is what KANALI's own
installer actually sets up for it (an earlier assumption in this file that
WinUSB was the intended driver turned out to be wrong - Device Manager
directly confirms usbprint.sys is what KANALI expects). panel() below now
connects that way by default (PanoramaPrint, via CreateFile/ReadFile/
WriteFile - no Device Manager changes needed), falling back to the WinUSB
path (PanoramaUSB) only if that fails - which requires having replaced
usbprint.sys with WinUSB via Zadig (https://zadig.akeo.ie), and breaks
KANALI's own access while active. Use option 8 ("raw USB visibility") or
11 (usbprint.sys path check) if neither connects.

The frame protocol itself (handshake/brightness/screen config, ported from
reed-tpse's Linux implementation) is still unverified against this specific
panel/transport combination until a real handshake succeeds - options 1-5
reach the device but haven't yet confirmed the protocol shape matches.
Options 6/7/serial support are kept only for the Linux CDC-ACM case and are
known NOT to work for this panel on Windows at all.

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
    Panorama, PanoramaUSB, PanoramaPrint, Media,
    find_device, find_usb_panel, find_print_panel, find_usbprint_path,
    usb_probe, build_frame,
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
        print("No panel found via either transport. Run option 8 (raw USB visibility) "
              "or 11 (usbprint.sys path check) for details.")
        sys.exit(1)
    print("Connected via WinUSB.")
    _panel = found
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
    print("Connected:", p.connected)


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
    """Uploads one or more local images/gifs and sets them as the persistent
    boot/default screen - full screen, looping through all of them if more
    than one. This is the one CAM/KANALI-free thing this whole project
    exists to prove out for the display.

    A single file uses playMode "Single"; more than one uses "Sequence" -
    that second value is an assumption (reed-tpse documents the config
    shape but not every valid playMode string), so if a multi-file upload
    only shows the first image, that value is the first thing to check."""
    raw = input("Path(s) to upload, comma-separated for a slideshow (images/gifs): ").strip()
    paths = [p.strip().strip('"') for p in raw.split(",") if p.strip()]
    if not paths:
        print("Nothing entered.")
        return
    missing = [p for p in paths if not Path(p).is_file()]
    if missing:
        print("File(s) not found:", missing)
        return
    remote_names = [Path(p).name for p in paths]

    media = Media()
    if not media.is_connected():
        print("adb doesn't see the panel - see option 4 for details.")
        return
    for local_path, remote_name in zip(paths, remote_names):
        print(f"Pushing {local_path} -> /sdcard/pcMedia/{remote_name} ...")
        if not media.push(local_path, remote_name):
            print("Push failed for", local_path)
            return
    print("Pushed. Setting screen config...")

    p = panel()
    play_mode = "Single" if len(remote_names) == 1 else "Sequence"
    resp = p.set_screen_config(remote_names, screen_mode="Full Screen",
                               ratio="2:1", play_mode=play_mode)
    print("response:", resp)
    print("Should be showing now - and should persist across a reboot, per reed-tpse's README.")


def test_usb_probe():
    """Checks the raw USB bus for VID 0x391A (Tryx), independent of any
    driver/COM port/KANALI - start here, since this panel has no COM port
    on Windows at all (confirmed from real KANALI logs this session)."""
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


def test_raw_handshake_dump():
    """Bypasses build_frame()/parse_response()'s strict 0x5A...0x5A framing -
    writes the exact same 'conn' request option 2 sends via whichever
    transport panel() connected with, then dumps whatever comes back
    regardless of shape. Useful when option 2 says "no/invalid response",
    which is ambiguous between "device said nothing" and "device replied
    but not wrapped the way reed-tpse's protocol.cpp does"."""
    p = panel()
    frame = build_frame("POST", "conn")
    print(f"Writing {len(frame)} bytes: {frame.hex()}")
    p._write(frame)
    print("Write did not raise. Reading for up to 3s...")
    raw = p._read_frame(timeout_s=3.0)
    if raw:
        print(f"  got {len(raw)} bytes: {raw.hex()}")
        print(f"    ascii: {raw!r}")
    else:
        print("Nothing at all came back in 3s after writing.")


def test_passive_listen():
    """Reads for several seconds without writing anything first - checks
    whether the device is chatty on its own (some firmware sends periodic
    status/telemetry unprompted), and rules out one specific explanation
    for option 9 coming back silent: that writing first is somehow
    breaking the pipe, rather than the device just never answering."""
    p = panel()
    print("Listening for 5s with nothing sent...")
    raw = p._read_frame(timeout_s=5.0)
    if raw:
        print(f"  got {len(raw)} bytes: {raw.hex()}")
    else:
        print("Silent - nothing sent by the device on its own either.")


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


MENU = {
    "1": ("Find + connect (usbprint.sys, falls back to WinUSB)", test_find),
    "2": ("Full handshake info", test_handshake),
    "3": ("Brightness sweep (20 -> 100 -> 60)", test_brightness),
    "4": ("List media on the panel (via adb)", test_media_list),
    "5": ("Upload image(s)/gif(s) and set them as the default/boot screen", test_set_default_image),
    "6": ("[Linux/legacy] List every serial port Windows/pyserial can see", test_list_ports),
    "7": ("[Linux/legacy] Handshake on a specific COM port you name", test_handshake_on_port),
    "8": ("Check raw USB visibility (pyusb, VID 0x391A) - run this first if "
          "1-5 fail to connect", test_usb_probe),
    "9": ("Raw handshake byte dump (diagnostic - use if option 2 says "
          "'no/invalid response')", test_raw_handshake_dump),
    "10": ("Passive listen, 5s, nothing sent (diagnostic)", test_passive_listen),
    "11": ("Check usbprint.sys device path (diagnostic, no handle opened)", test_check_usbprint_path),
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
