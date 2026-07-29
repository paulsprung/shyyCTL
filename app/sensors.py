"""System monitoring for the Status page - CPU/RAM load, GPU load/temp, and
CPU/motherboard/RAM temperature.

Independent sources, each optional and independently degrading (same
"never crash on missing hardware" rule as the rest of this app - a laptop
with no NVIDIA GPU, or a machine where nothing is serving sensor data,
should still show whatever it *can* read, not a blank page):

- CPU/RAM load: psutil. Cross-platform, no extra service needed.
- GPU load/mem/temp: nvidia-smi, shelled out to exactly like liquidctl and
  ffmpeg elsewhere in this app - it ships with the NVIDIA driver itself, no
  separate install. Only NVIDIA is supported; there's no AMD/Intel
  equivalent CLI worth shelling out to here.
- CPU/motherboard/RAM temperature: psutil has no working Windows
  implementation (psutil.sensors_temperatures() is Linux-only), and there
  is no cross-vendor CLI worth shelling out to the way nvidia-smi covers
  GPUs - the config's sensors.librehardwaremonitor_url plus
  cpu_sensor_match/mobo_sensor_match/ram_sensor_match (config.yaml) point
  at a local web server serving LibreHardwareMonitor-shaped JSON instead.
  By default that's this project's own SensorHelper.exe (sensor-helper/
  Program.cs, built by tools/setup_vendor.py, auto-launched by main.py's
  _launch_vendor_sensor_helper()) - a tiny background wrapper around the
  LibreHardwareMonitorLib package with no GUI window and nothing to
  enable by hand, built specifically so a human doesn't have to run the
  full LibreHardwareMonitor app just for this. Running the real
  LibreHardwareMonitor app yourself (Options > Remote Web Server > Run,
  same port) works exactly the same, since the JSON shape is identical -
  shyyctl just won't launch its own copy if something's already answering
  on that port. This is already the project's answer for the
  coolant-temperature gap noted there too (Asetek Gen8 pump not
  liquidctl-recognised). CPU temp is on by default (cpu_sensor_match
  defaults to "CPU Package", a near-universal name across vendors);
  motherboard/RAM are opt-in, since every board/kit names those sensors
  differently in the sensor tree - they only appear once a human has
  checked what they're actually named there and set a real match string
  in config.yaml. All three are unavailable (reported as None, not an
  error) until something is actually serving that URL.
"""

import json
import logging
import subprocess
import urllib.request

from app.procflags import NO_WINDOW

log = logging.getLogger("sensors")

try:
    import psutil
except ImportError:  # pragma: no cover - exercised on a box without psutil installed
    psutil = None


def cpu_ram() -> dict:
    if psutil is None:
        return {"cpu_percent": None, "ram_percent": None, "ram_used_gb": None, "ram_total_gb": None}
    try:
        vm = psutil.virtual_memory()
        return {
            "cpu_percent": psutil.cpu_percent(interval=None),
            "ram_percent": vm.percent,
            "ram_used_gb": round(vm.used / (1024 ** 3), 1),
            "ram_total_gb": round(vm.total / (1024 ** 3), 1),
        }
    except Exception as exc:
        log.warning("psutil read failed: %s", exc)
        return {"cpu_percent": None, "ram_percent": None, "ram_used_gb": None, "ram_total_gb": None}


def gpu() -> dict:
    """First NVIDIA GPU's utilisation/memory/temperature via nvidia-smi.
    None across the board (not an exception) on a box with no NVIDIA GPU or
    no driver on PATH - that's a normal, expected shape, not a failure."""
    empty = {"name": None, "load_percent": None, "mem_used_mb": None, "mem_total_mb": None, "temp_c": None}
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3, creationflags=NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return empty
    if out.returncode != 0 or not out.stdout.strip():
        return empty
    try:
        name, load, mem_used, mem_total, temp = (p.strip() for p in out.stdout.strip().splitlines()[0].split(","))
        return {
            "name": name, "load_percent": float(load), "mem_used_mb": float(mem_used),
            "mem_total_mb": float(mem_total), "temp_c": float(temp),
        }
    except Exception as exc:
        log.warning("could not parse nvidia-smi output %r: %s", out.stdout, exc)
        return empty


_lhm_temp_warned: set[str] = set()


def lhm_temp(url: str, match: str, config_key: str = "cpu_sensor_match") -> float | None:
    """Walks LibreHardwareMonitor's data.json sensor tree for the first
    leaf whose Text contains `match` (case-insensitive) and has a °C
    value - the tree's shape is an arbitrary-depth "Children" list, not a
    fixed schema, so this recurses rather than assuming a path. Used for
    CPU, motherboard, and RAM temperatures alike - LHM exposes all three
    through the same tree, just under different device nodes, so this is
    one general "find a sensor whose name contains X" reader rather than
    a CPU-specific one. config_key is only used in the diagnostic log
    message below, to point at the right config.yaml key to fix.

    Logs (once per distinct reason, not every poll - this gets called
    every few seconds) whether the failure was "couldn't even reach the
    server" (LHM not running, or not running with Options > Remote Web
    Server enabled, or on a different port than configured) versus "got a
    real response but nothing in it matched `match`" (LHM is running
    fine, but this exact machine's sensor tree names this reading
    something other than what's configured) - two different fixes, and
    silently returning None for both left a human with no way to tell
    which one they actually have."""
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        key = f"connect:{url}"
        if key not in _lhm_temp_warned:
            _lhm_temp_warned.add(key)
            log.warning(
                "could not reach LibreHardwareMonitor at %s (%s) - temperature readings will "
                "show as unavailable until it's running with Options > Remote Web Server "
                "enabled on this port. This is only logged once.", url, exc)
        return None

    def walk(node):
        if not isinstance(node, dict):
            return None
        text = str(node.get("Text", ""))
        value = node.get("Value")
        if match.lower() in text.lower() and isinstance(value, str) and "°C" in value:
            try:
                return float(value.replace("°C", "").strip())
            except ValueError:
                pass
        for child in node.get("Children", []) or []:
            found = walk(child)
            if found is not None:
                return found
        return None

    try:
        result = walk(data)
    except Exception as exc:
        log.warning("could not parse LibreHardwareMonitor data: %s", exc)
        return None
    if result is None:
        key = f"nomatch:{url}:{match}"
        if key not in _lhm_temp_warned:
            _lhm_temp_warned.add(key)
            log.warning(
                "LibreHardwareMonitor at %s responded, but no sensor whose name contains "
                "%r had a temperature reading - check what this sensor is actually named in "
                "LHM's own UI and update config.yaml's sensors.%s to match. "
                "This is only logged once.", url, match, config_key)
    return result


def snapshot(config: dict) -> dict:
    """Everything the Status page needs in one call."""
    sensors_cfg = config.get("sensors", {})
    result = cpu_ram()
    result["gpu"] = gpu()
    url = sensors_cfg.get("librehardwaremonitor_url", "")
    result["cpu_temp_c"] = lhm_temp(
        url, sensors_cfg.get("cpu_sensor_match", "CPU Package"), "cpu_sensor_match"
    ) if url else None
    # Motherboard/RAM temperature sensor names vary per-board and per-kit
    # (unlike "CPU Package", which is a near-universal name across
    # vendors) - only attempt these once a human has actually looked at
    # their own LHM sensor tree and configured a real match string for
    # their hardware; an unconfigured (empty/absent) key means "not set
    # up for this machine yet", not a failure, so this stays out of the
    # response entirely rather than showing as a false "unavailable".
    mobo_match = sensors_cfg.get("mobo_sensor_match")
    if url and mobo_match:
        result["mobo_temp_c"] = lhm_temp(url, mobo_match, "mobo_sensor_match")
    ram_match = sensors_cfg.get("ram_sensor_match")
    if url and ram_match:
        result["ram_temp_c"] = lhm_temp(url, ram_match, "ram_sensor_match")
    return result
