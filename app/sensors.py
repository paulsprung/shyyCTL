"""System monitoring for the Status page - CPU/RAM load, GPU load/temp, and
CPU temperature.

Three independent sources, each optional and independently degrading (same
"never crash on missing hardware" rule as the rest of this app - a laptop
with no NVIDIA GPU, or a machine that's never run LibreHardwareMonitor,
should still show whatever it *can* read, not a blank page):

- CPU/RAM load: psutil. Cross-platform, no extra service needed.
- GPU load/mem/temp: nvidia-smi, shelled out to exactly like liquidctl and
  ffmpeg elsewhere in this app - it ships with the NVIDIA driver itself, no
  separate install. Only NVIDIA is supported; there's no AMD/Intel
  equivalent CLI worth shelling out to here.
- CPU temperature: psutil has no working Windows implementation
  (psutil.sensors_temperatures() is Linux-only) - the config's
  sensors.librehardwaremonitor_url/cpu_sensor_match (config.yaml) point at
  LibreHardwareMonitor's own local web server instead (Options > Remote
  Web Server in LHM), which is already the project's answer for the
  coolant-temperature gap noted there (Asetek Gen8 pump not
  liquidctl-recognised). Unavailable until a human runs LHM with that
  server enabled - reported as None, not an error.
"""

import json
import logging
import subprocess
import urllib.request

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
            capture_output=True, text=True, timeout=3,
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


def cpu_temp(url: str, match: str) -> float | None:
    """Walks LibreHardwareMonitor's data.json sensor tree for the first
    leaf whose Text contains `match` (case-insensitive) and has a °C
    value - the tree's shape is an arbitrary-depth "Children" list, not a
    fixed schema, so this recurses rather than assuming a path."""
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            data = json.loads(resp.read())
    except Exception:
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
        return walk(data)
    except Exception as exc:
        log.warning("could not parse LibreHardwareMonitor data: %s", exc)
        return None


def snapshot(config: dict) -> dict:
    """Everything the Status page needs in one call."""
    sensors_cfg = config.get("sensors", {})
    result = cpu_ram()
    result["gpu"] = gpu()
    result["cpu_temp_c"] = cpu_temp(
        sensors_cfg.get("librehardwaremonitor_url", ""),
        sensors_cfg.get("cpu_sensor_match", "CPU Package"),
    ) if sensors_cfg.get("librehardwaremonitor_url") else None
    return result
