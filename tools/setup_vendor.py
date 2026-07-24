"""Downloads the portable Windows binaries this project needs but doesn't
want to force a separate install for: ffmpeg (Panorama media encoding) and
OpenRGB (RGB control backend, with its SDK server this project talks to).
Neither gets committed to git - both are sizeable (tens to a couple
hundred MB) and don't belong in source control; this script fetches them
into vendor/, which is gitignored. main.py looks there first before
falling back to PATH (see app/panorama.py's ffmpeg lookup and this
module's find_openrgb_exe()).

Unverified end to end - written without a way to actually run it against
the real internet/a real Windows machine in this project's dev
environment (see other docstrings in this codebase for why that's
flagged rather than silently assumed to work). Both URLs are meant to be
stable "always latest" endpoints per their own maintainers (ffmpeg:
gyan.dev's own build page documents this exact URL as the always-current
essentials build; OpenRGB: resolved dynamically via GitLab's releases
API rather than a hardcoded per-version filename, since OpenRGB's actual
release asset names embed a build-specific commit hash that changes every
release) - but the exact file layout inside each downloaded archive has
not been confirmed against a real download in this session, so the
extraction logic below is a best-effort guess at their structure, not a
confirmed fact.

Usage:
    python tools/setup_vendor.py
"""

import json
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor"

FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
OPENRGB_RELEASES_API = "https://gitlab.com/api/v4/projects/CalcProgrammer1%2FOpenRGB/releases"


def _download(url: str, dest: Path):
    print(f"Downloading {url} ...")
    with urllib.request.urlopen(url, timeout=180) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


def _extract_single_file(zip_path: Path, filename: str, dest_dir: Path) -> bool:
    """Pulls just one named file out of a zip (wherever it's nested inside),
    since these builds usually wrap everything in a version-named folder
    that isn't worth keeping around."""
    with zipfile.ZipFile(zip_path) as zf:
        matches = [n for n in zf.namelist() if n.lower().endswith(filename.lower())]
        if not matches:
            return False
        matches.sort(key=len)  # shortest path = least likely to be a nested extra copy
        with zf.open(matches[0]) as src, open(dest_dir / filename, "wb") as dst:
            shutil.copyfileobj(src, dst)
    return True


def setup_ffmpeg():
    target = VENDOR / "ffmpeg.exe"
    if target.exists():
        print("ffmpeg.exe already present, skipping.")
        return
    VENDOR.mkdir(parents=True, exist_ok=True)
    zip_path = VENDOR / "_ffmpeg_download.zip"
    try:
        _download(FFMPEG_URL, zip_path)
        print("Extracting ffmpeg.exe ...")
        if not _extract_single_file(zip_path, "ffmpeg.exe", VENDOR):
            print("Could not find ffmpeg.exe inside the downloaded archive - "
                  "gyan.dev's build layout may have changed; check "
                  "https://www.gyan.dev/ffmpeg/builds/ manually and place "
                  "ffmpeg.exe directly in vendor/.")
            sys.exit(1)
    finally:
        zip_path.unlink(missing_ok=True)
    print(f"Done: {target}")


def setup_openrgb():
    target_dir = VENDOR / "OpenRGB"
    if target_dir.exists() and any(target_dir.rglob("OpenRGB.exe")):
        print("OpenRGB already present, skipping.")
        return
    VENDOR.mkdir(parents=True, exist_ok=True)
    print("Looking up the latest OpenRGB release via the GitLab API ...")
    with urllib.request.urlopen(OPENRGB_RELEASES_API, timeout=30) as resp:
        releases = json.load(resp)
    asset_url = None
    for release in releases:
        for link in release.get("assets", {}).get("links", []):
            name = link.get("name", "")
            lowered = name.lower()
            if "windows" in lowered and lowered.endswith(".zip") and "debug" not in lowered:
                asset_url = link["url"]
                break
        if asset_url:
            break
    if asset_url is None:
        print("Could not find a Windows release asset via the GitLab API - "
              "download manually from https://gitlab.com/CalcProgrammer1/"
              "OpenRGB/-/releases and extract it to vendor/OpenRGB/ yourself "
              "(so that vendor/OpenRGB/OpenRGB.exe exists).")
        sys.exit(1)
    zip_path = VENDOR / "_openrgb_download.zip"
    try:
        _download(asset_url, zip_path)
        print("Extracting OpenRGB ...")
        target_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(target_dir)
    finally:
        zip_path.unlink(missing_ok=True)
    if not any(target_dir.rglob("OpenRGB.exe")):
        print("Extracted, but no OpenRGB.exe found anywhere inside - the "
              "release archive's layout may not match what this script "
              "expects. Check vendor/OpenRGB/ manually.")
        sys.exit(1)
    print(f"Done: {target_dir}")


if __name__ == "__main__":
    setup_ffmpeg()
    setup_openrgb()
    print("\nDone. vendor/ffmpeg.exe and vendor/OpenRGB/ ready.")
