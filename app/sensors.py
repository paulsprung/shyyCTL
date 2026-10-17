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
import threading
import time
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


_resource_procs: dict[str, "psutil.Process | None"] = {}


def process_resources() -> dict:
    """Live CPU/RAM cost of shyyctl itself and its two vendored helper
    processes (OpenRGB.exe, SensorHelper.exe) - what this app specifically
    is costing the machine, for the Settings resource viewer. Deliberately
    separate from cpu_ram() above, which reports the whole machine's load,
    not this app's share of it.

    psutil.Process.cpu_percent(interval=None) is a non-blocking call that
    reports the delta since that same Process handle's *previous*
    cpu_percent() call - meaningless (always 0.0) on the very first call
    after a handle is created, which is why every handle here gets one
    throwaway priming call the moment it's first found, before its result
    is ever returned to a caller. Handles are cached across polls (by
    name, since neither helper's PID is tracked anywhere else in this
    file - both are found/killed by image name via tasklist/taskkill, see
    main.py's _process_running/_kill_process) so a real reading is
    available from the very next poll onward rather than resetting to 0.0
    every single call. is_running() on a cached handle correctly reports
    False after that specific process exits even if a new process reuses
    the same name (psutil tracks each handle's own start time internally
    to detect PID reuse) - a stale cache entry is dropped and re-searched
    for automatically rather than ever reporting a dead process's last
    known numbers as if they were live."""
    if psutil is None:
        return {"self": None, "openrgb": None, "sensor_helper": None}

    def usage(proc) -> dict | None:
        if proc is None:
            return None
        try:
            with proc.oneshot():
                # Process.cpu_percent() is expressed relative to ONE core
                # (0-100*cpu_count, confirmed against psutil's own docs) -
                # unlike the module-level psutil.cpu_percent() cpu_ram()
                # uses below, which is already normalized to the whole
                # machine. Direct user report: this showed OpenRGB at
                # "95.3%" while Windows' own Task Manager showed 11.6% for
                # the exact same process at the exact same moment on an
                # 8-core machine (95.3/8 = 11.9, matching) - dividing by
                # cpu_count() here makes this match Task Manager's numbers
                # instead of alarming anyone into thinking one process is
                # close to pegging the entire CPU when it's actually maxed
                # out on a single core.
                cpu = proc.cpu_percent(interval=None) / (psutil.cpu_count() or 1)
                rss_mb = proc.memory_info().rss / (1024 ** 2)
            return {"cpu_percent": round(cpu, 1), "ram_mb": round(rss_mb, 1)}
        except Exception:
            return None

    def find_by_name(exe_name: str):
        cached = _resource_procs.get(exe_name)
        if cached is not None:
            try:
                if not cached.is_running():
                    cached = None
            except Exception:
                cached = None
        if cached is None:
            for p in psutil.process_iter(["name"]):
                try:
                    if (p.info.get("name") or "").lower() == exe_name:
                        p.cpu_percent(interval=None)  # prime - see docstring
                        cached = p
                        break
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            _resource_procs[exe_name] = cached
        return cached

    try:
        self_proc = _resource_procs.get("self")
        if self_proc is None:
            self_proc = psutil.Process()
            self_proc.cpu_percent(interval=None)  # prime - see docstring
            _resource_procs["self"] = self_proc
        self_usage = usage(self_proc)
    except Exception as exc:
        log.warning("could not read shyyctl's own process usage: %s", exc)
        self_usage = None

    openrgb_usage = usage(find_by_name("openrgb.exe"))
    sensor_helper_usage = usage(find_by_name("sensorhelper.exe"))

    total_cpu = sum(u["cpu_percent"] for u in (self_usage, openrgb_usage, sensor_helper_usage) if u)
    total_ram = sum(u["ram_mb"] for u in (self_usage, openrgb_usage, sensor_helper_usage) if u)

    return {
        "self": self_usage, "openrgb": openrgb_usage, "sensor_helper": sensor_helper_usage,
        "total_cpu_percent": round(total_cpu, 1), "total_ram_mb": round(total_ram, 1),
    }


_gpu_cache: tuple[float, dict] | None = None
_gpu_lock = threading.Lock()


def gpu() -> dict:
    """First NVIDIA GPU's utilisation/memory/temperature via nvidia-smi.
    None across the board (not an exception) on a box with no NVIDIA GPU or
    no driver on PATH - that's a normal, expected shape, not a failure.

    Both the fan-curve loop (every 4s) and the Status page (every 3s) ask
    for this - one nvidia-smi process answers both if they land within 2s
    of each other, instead of one each."""
    global _gpu_cache
    with _gpu_lock:
        cached = _gpu_cache
        if cached is not None and time.monotonic() - cached[0] < 2.0:
            return dict(cached[1])
        result = _gpu_read()
        _gpu_cache = (time.monotonic(), result)
        return dict(result)


def _gpu_read() -> dict:
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


def _parse_celsius(value: str) -> float | None:
    """"45.3 °C", "45,3°C" -> 45.3. LibreHardwareMonitor (and older
    SensorHelper builds) format with the Windows display culture, so on a
    German/most-European-locale machine the decimal separator is a comma -
    float("45,3") raised, every reading counted as "no match", and CPU
    temperature (and with it every CPU fan curve) stayed unavailable."""
    text = value.replace("°C", "").replace("\u00a0", " ").strip().replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


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
            parsed = _parse_celsius(value)
            if parsed is not None:
                return parsed
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
