"""Downloads or builds the portable Windows binaries this project needs
but doesn't want to force a separate install for: ffmpeg (Panorama media
encoding), OpenRGB (RGB control backend, with its SDK server this project
talks to), and sensor-helper (CPU/motherboard/RAM temperature - see
app/sensors.py and sensor-helper/Program.cs). None of these get committed
to git - all sizeable (tens to a couple hundred MB) and don't belong in
source control; this script fetches/builds them into vendor/, which is
gitignored. main.py looks there first before falling back to PATH (see
app/panorama.py's ffmpeg lookup, this module's find_openrgb_exe(), and
_launch_vendor_sensor_helper()).

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

sensor-helper: this project's OWN small C#/.NET console app
(sensor-helper/Program.cs), not a downloaded third party binary - it
uses the LibreHardwareMonitorLib NuGet package directly (the same
library the full LibreHardwareMonitor GUI app is built on) to serve
just the CPU/motherboard/RAM temperature data this project actually
needs, over a tiny built-in HTTP listener, with no visible window and
no manual "Remote Web Server" toggle to click through - replaces an
earlier version of this script that downloaded and auto-launched the
full LibreHardwareMonitor GUI app instead. Built here via `dotnet
publish` (self-contained, single-file win-x64) rather than downloaded,
so this machine needs the .NET 8 SDK installed at setup time only - not
at runtime, since the published output is self-contained. Confirmed
against a real build in this project's dev environment (a real .NET 8
SDK installed via apt, LibreHardwareMonitorLib 0.9.6 restored from
NuGet, compiled clean, published a real ~70MB self-contained win-x64
.exe) - the actual sensor reads themselves are NOT confirmed against
real Windows hardware from here, only that the code compiles against
the library's real API surface (verified via its own XML docs, not
guessed from memory) and produces a valid executable.

Usage:
    python tools/setup_vendor.py
"""

import json
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor"

FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
OPENRGB_RELEASES_API = "https://codeberg.org/api/v1/repos/OpenRGB/OpenRGB/releases?limit=10"


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


def setup_sensor_helper():
    target_dir = VENDOR / "SensorHelper"
    target_exe = target_dir / "SensorHelper.exe"
    if target_exe.exists():
        print("SensorHelper.exe already present, skipping (delete vendor/SensorHelper/ "
              "to force a rebuild after changing sensor-helper/Program.cs).")
        return
    project_dir = ROOT / "sensor-helper"
    if not (project_dir / "SensorHelper.csproj").exists():
        print(f"No sensor-helper/SensorHelper.csproj found at {project_dir} - "
              "skipping (nothing to build).")
        return
    print("Building sensor-helper (dotnet publish, self-contained win-x64) ...")
    try:
        subprocess.run(
            ["dotnet", "publish", "-c", "Release", "-r", "win-x64",
             "--self-contained", "true", "-o", str(target_dir)],
            cwd=str(project_dir), check=True,
        )
    except FileNotFoundError:
        print("dotnet not found on PATH - install the .NET 8 SDK "
              "(https://dotnet.microsoft.com/download/dotnet/8.0) to build this. "
              "CPU/motherboard/RAM temperature just won't be available until then; "
              "everything else in shyyctl works fine without it.")
        return
    except subprocess.CalledProcessError as exc:
        print(f"dotnet publish failed (exit code {exc.returncode}) - see output above.")
        return
    if not target_exe.exists():
        print(f"Build finished, but no SensorHelper.exe found at {target_exe} - "
              "check the dotnet publish output above.")
        return
    print(f"Done: {target_exe}")


if __name__ == "__main__":
    setup_ffmpeg()
    setup_openrgb()
    setup_sensor_helper()
    print("\nDone. vendor/ffmpeg.exe, vendor/OpenRGB/, and vendor/SensorHelper/ ready "
          "(whichever of these had what they needed to build/download).")
