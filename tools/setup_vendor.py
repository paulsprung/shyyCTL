"""Downloads the portable Windows binaries this project needs but doesn't
want to force a separate install for: ffmpeg (Panorama media encoding),
OpenRGB (RGB control backend, with its SDK server this project talks to),
and LibreHardwareMonitor (CPU/motherboard/RAM temperature - see
app/sensors.py). None of these get committed to git - all sizeable (tens
to a couple hundred MB) and don't belong in source control; this script
fetches them into vendor/, which is gitignored. main.py looks there first
before falling back to PATH (see app/panorama.py's ffmpeg lookup, this
module's find_openrgb_exe(), and _launch_vendor_librehardwaremonitor()).

ffmpeg's URL is confirmed working (run against the real internet on the
target Windows machine this session - downloads and extracts cleanly).

OpenRGB moved: this script originally pointed at GitLab's releases API,
which turned out to no longer carry any downloadable binaries at all -
confirmed against a real run this session, GitLab's own release
descriptions now say "downloads are now hosted on Codeberg" and the
GitLab API's assets/links array comes back empty. OpenRGB's real
releases now live at https://codeberg.org/OpenRGB/OpenRGB/releases -
Codeberg runs Forgejo (a Gitea fork), whose releases API
(GET /repos/{owner}/{repo}/releases) is standard and well-documented,
returning each release's assets as {name, browser_download_url, ...} -
used here instead of a hardcoded per-version filename, since OpenRGB's
actual release asset names embed a build-specific commit hash that
changes every release (e.g. OpenRGB_1.0rc3_Windows_64_6fbcf62.zip).
This path is NOT yet confirmed against a real run (this project's dev
sandbox has Codeberg blocked at the network-policy level, unrelated to
the actual target Windows machine) - the API shape itself is standard
Forgejo/Gitea, not a guess, but the exact asset-name filtering below
hasn't been proven against a live response.

LibreHardwareMonitor: same situation as OpenRGB above - this sandbox's
network policy blocks direct GitHub API access outside this project's
own repo, so the exact asset filename below is NOT confirmed against a
live response either. GitHub's releases API shape
(GET /repos/{owner}/{repo}/releases) is standard and stable, so the
lookup logic itself isn't a guess, just unverified end-to-end here.
Matching by "librehardwaremonitor" + ".zip" in the asset name rather
than a hardcoded version string, for the same reason OpenRGB's filter
avoids one - release asset names change across versions.

Deliberately does NOT attempt to pre-configure LibreHardwareMonitor's
Remote Web Server (Options > Remote Web Server > Run, port 8085) - its
settings do persist in a LibreHardwareMonitor.config file next to the
exe once portable, so this only needs doing once by hand, but there is
no confirmed, documented way to write that setting into the config file
directly without risking a malformed file that stops the app from
starting at all. One manual toggle the first time beats a guessed file
that might silently break every launch after.

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
OPENRGB_RELEASES_API = "https://codeberg.org/api/v1/repos/OpenRGB/OpenRGB/releases?limit=10"
LHM_RELEASES_API = "https://api.github.com/repos/LibreHardwareMonitor/LibreHardwareMonitor/releases?per_page=10"


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
    print("Looking up the latest OpenRGB release via the Codeberg (Forgejo) API ...")
    asset_url = None
    asset_name = None
    try:
        req = urllib.request.Request(OPENRGB_RELEASES_API, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            releases = json.load(resp)
        for release in releases:
            for asset in release.get("assets", []):
                name = asset.get("name", "")
                lowered = name.lower()
                # Confirmed against a real run this session: OpenRGB ships
                # both Windows_32 and Windows_64 assets per release, and a
                # bare "windows" check matched the 32-bit build first. This
                # project (and any remotely current Windows machine) is
                # 64-bit only.
                if ("windows" in lowered and "64" in lowered and "32" not in lowered
                        and lowered.endswith(".zip") and "debug" not in lowered):
                    asset_url = asset.get("browser_download_url")
                    asset_name = name
                    break
            if asset_url:
                break
    except Exception as exc:
        print(f"Codeberg API request failed: {exc}")
    if asset_url is None:
        print("Could not find a Windows release asset via the Codeberg API - "
              "download manually from https://codeberg.org/OpenRGB/OpenRGB/releases "
              "(look for a file like 'OpenRGB_<version>_Windows_64_<hash>.zip') and "
              "extract it to vendor/OpenRGB/ yourself (so that "
              "vendor/OpenRGB/OpenRGB.exe exists).")
        sys.exit(1)
    print(f"Found: {asset_name}")
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


def setup_librehardwaremonitor():
    target_dir = VENDOR / "LibreHardwareMonitor"
    if target_dir.exists() and any(target_dir.rglob("LibreHardwareMonitor.exe")):
        print("LibreHardwareMonitor already present, skipping.")
        return
    VENDOR.mkdir(parents=True, exist_ok=True)
    print("Looking up the latest LibreHardwareMonitor release via the GitHub API ...")
    asset_url = None
    asset_name = None
    try:
        req = urllib.request.Request(LHM_RELEASES_API, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            releases = json.load(resp)
        for release in releases:
            for asset in release.get("assets", []):
                name = asset.get("name", "")
                lowered = name.lower()
                if "librehardwaremonitor" in lowered and lowered.endswith(".zip"):
                    asset_url = asset.get("browser_download_url")
                    asset_name = name
                    break
            if asset_url:
                break
    except Exception as exc:
        print(f"GitHub API request failed: {exc}")
    if asset_url is None:
        print("Could not find a release asset via the GitHub API - download manually "
              "from https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases "
              "(the portable zip, e.g. 'LibreHardwareMonitor-net472.zip') and extract it to "
              "vendor/LibreHardwareMonitor/ yourself (so that "
              "vendor/LibreHardwareMonitor/LibreHardwareMonitor.exe exists).")
        sys.exit(1)
    print(f"Found: {asset_name}")
    zip_path = VENDOR / "_lhm_download.zip"
    try:
        _download(asset_url, zip_path)
        print("Extracting LibreHardwareMonitor ...")
        target_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(target_dir)
    finally:
        zip_path.unlink(missing_ok=True)
    if not any(target_dir.rglob("LibreHardwareMonitor.exe")):
        print("Extracted, but no LibreHardwareMonitor.exe found anywhere inside - the "
              "release archive's layout may not match what this script expects. "
              "Check vendor/LibreHardwareMonitor/ manually.")
        sys.exit(1)
    print(f"Done: {target_dir}")
    print("One-time manual step still needed: launch it once, go to "
          "Options > Remote Web Server > Run (port 8085), then close it. "
          "That setting persists in LibreHardwareMonitor.config next to the exe, "
          "so every future auto-launch by shyyctl will already have it running.")


if __name__ == "__main__":
    setup_ffmpeg()
    setup_openrgb()
    setup_librehardwaremonitor()
    print("\nDone. vendor/ffmpeg.exe, vendor/OpenRGB/, and vendor/LibreHardwareMonitor/ ready.")
