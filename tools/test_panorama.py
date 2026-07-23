"""Manual test of app/panorama.py against the real Tryx Panorama - the
display equivalent of tools/test_app.py.

Needs:
    pip install pyserial pyusb libusb-package
    adb.exe on PATH (Android platform-tools - just the zip, no Android
    Studio: https://developer.android.com/tools/releases/platform-tools)

Real KANALI logs from this hardware showed this panel is WinUSB
(VID 0x391A:0x1011) on Windows, not a COM port - confirmed against real
hardware this session, along with the fact that Windows binds its generic
usbprint.sys class driver here by default, which has to be replaced with
WinUSB via Zadig (https://zadig.akeo.ie, "List All Devices", target driver
WinUSB, Replace Driver) before pyusb/this file can open it at all - see
option 8 ("raw USB visibility") if anything below fails to connect.

Options 1-5 now go over the real WinUSB transport (PanoramaUSB) and are
confirmed to reach the device at the USB level - the frame protocol itself
(handshake/brightness/screen config, ported from reed-tpse) is still
unverified against this specific panel until a handshake actually succeeds.
Options 6/7/serial support are kept only for the Linux CDC-ACM case and are
known NOT to work for this panel on Windows.

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

from app.panorama import Panorama, PanoramaUSB, Media, find_device, find_usb_panel, usb_probe, build_frame

_panel: PanoramaUSB | None = None


def panel() -> PanoramaUSB:
    global _panel
    if _panel is not None and _panel.connected:
        return _panel
    print("Connecting to the panel over WinUSB (VID 0x391A:0x1011)...")
    found = find_usb_panel()
    if found is None:
        print("No panel found over USB. Run option 8 for details (needs WinUSB bound "
              "via Zadig, not the default usbprint.sys driver).")
        sys.exit(1)
    print("Connected.")
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
    writes the exact same 'conn' request option 2 sends, then dumps every
    raw read attempt for a few seconds regardless of shape. Option 2 came
    back "no/invalid response", which is ambiguous between "device said
    nothing" and "device replied but not wrapped the way reed-tpse's
    protocol.cpp does" - this pins down which one it actually is."""
    import usb.core
    p = panel()
    frame = build_frame("POST", "conn")
    print(f"Writing {len(frame)} bytes: {frame.hex()}")
    written = p.ep_out.write(frame, timeout=2000)
    print(f"ep_out.write() reported {written} bytes written")

    deadline = time.monotonic() + 3.0
    total = 0
    while time.monotonic() < deadline:
        try:
            chunk = p.ep_in.read(512, timeout=300)
        except usb.core.USBError as exc:
            if "timeout" in str(exc).lower() or "timed out" in str(exc).lower():
                continue
            print("read error:", exc)
            break
        if chunk:
            data = bytes(chunk)
            total += len(data)
            print(f"  got {len(data)} bytes: {data.hex()}")
            print(f"    ascii: {data!r}")
    if total == 0:
        print("Nothing at all came back on the IN endpoint in 3s after writing.")


MENU = {
    "1": ("Find + connect over WinUSB (handshake test)", test_find),
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
