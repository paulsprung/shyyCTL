"""Detects other RGB/fan-controlling software that's likely to fight
shyyctl over the same hardware.

Two programs holding the same USB device exclusively at once is a real,
previously-confirmed source of instability in this project - not a
hypothetical: hub.py's own connect() error message already blames "CAM/CAM
Service" for holding the hub open, and panorama.py's docstring documents
KANALI holding the Panorama's usbprint.sys handle exclusively. This module
gives the UI something concrete to warn about before the user spends time
debugging what looks like a shyyctl bug but is actually two controllers
fighting over one device.

Detection is a Windows `tasklist` process scan (no new dependency - same
"wrap the OS/CLI tool directly" pattern as hub.py's liquidctl wrapper)
for known process names. NOT exhaustive, and not every name below is
independently confirmed against a real captured process list - see the
per-entry notes. Best-effort only: a name that doesn't match isn't proof
nothing is running, just that this list doesn't know its process name yet.
"""

import logging
import subprocess

from app.procflags import NO_WINDOW

log = logging.getLogger("conflicts")

# (needle to match case-insensitively in `tasklist` output, display label).
# NZXT CAM and Corsair iCUE's process names are public/well-documented.
# SignalRGB, ASUS's various Aura/Armoury Crate service names, and Tryx
# KANALI's real executable name were never independently captured in this
# project (panorama.py only ever refers to it by product name, with one
# unconfirmed guess at "UDB.exe") - matched on a loose substring instead
# of a specific .exe, best-effort rather than a confirmed exact name.
KNOWN_CONTROLLERS: list[tuple[str, str]] = [
    ("nzxt cam.exe", "NZXT CAM"),
    ("icue.exe", "Corsair iCUE"),
    ("signalrgb", "SignalRGB"),
    ("auraservice", "ASUS Aura Sync"),
    ("lightingservice.exe", "ASUS Aura Sync / Armoury Crate"),
    ("armourycrate", "ASUS Armoury Crate"),
    ("kanali", "Tryx KANALI (Panorama display software)"),
]

# OpenRGB itself deliberately does NOT appear above - shyyctl depends on
# it being open (it may even be the copy this app launched itself, see
# main.py's _launch_vendor_openrgb) rather than conflicting with it.


def running_conflicts() -> list[str]:
    """Display names of every known controller currently running,
    deduplicated - [] if none found, or if `tasklist` isn't available
    (not on Windows, or some other error). Never raises: this is a
    best-effort warning banner, not something that should ever be able
    to break /api/status."""
    try:
        result = subprocess.run(["tasklist"], capture_output=True, text=True, timeout=5,
                                 creationflags=NO_WINDOW)
    except Exception as exc:
        log.debug("could not list running processes (tasklist unavailable?): %s", exc)
        return []
    if result.returncode != 0:
        return []
    out = result.stdout.lower()
    found: list[str] = []
    for needle, label in KNOWN_CONTROLLERS:
        if needle in out and label not in found:
            found.append(label)
    return found
