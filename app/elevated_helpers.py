"""Sets up (or removes) starting OpenRGB and SensorHelper with admin rights
without a UAC prompt at every login - run once, elevated, from shyyctl's
Settings page (main.py's /api/system/elevated-helpers), or with "remove"
for the uninstaller (shyyctl.exe --remove-elevated-helpers).

run_elevated() is the non-elevated side: it starts this same program -
shyyctl.exe, or python main.py from source - again through one UAC
prompt as `--elevated-helpers <action> ...`, which main.py hands to main()
here before anything else in it runs. No PowerShell: a hidden, elevated
PowerShell with its execution policy bypassed is what Defender's
machine-learning detection reported as a trojan in the uninstaller, and
the ScheduledTasks module failed with nothing more than "System error."
in the elevated session on the user's PC. The tasks are created with
Windows' own schtasks.exe from a task XML instead.

Both helpers need admin rights (RAM RGB over SMBus, CPU temperatures -
both through the PawnIO driver). Starting them silently with those rights
is only safe if nothing a normal user process can change ends up running
elevated, so:
  - the helpers are copied to %ProgramFiles%\\shyyctl\\helpers, which only
    administrators can write to (their install folder under
    %LOCALAPPDATA% is writable by any program the user runs);
  - OpenRGB gets its configuration folder - which is also where it loads
    plugins from - in there too (--config), instead of %APPDATA%\\OpenRGB;
  - SensorHelper is built to keep its native DLLs next to the exe rather
    than unpacking them into %TEMP% (sensor-helper/SensorHelper.csproj);
  - two scheduled tasks ("\\shyyctl\\OpenRGB", "\\shyyctl\\SensorHelper")
    start exactly those copies, with fixed arguments, at the highest run
    level for this user. shyyctl runs them with schtasks /run.

The elevated side reports {"ok": bool, "message": str} through a result
file, since an elevated process' exit code doesn't reach the
non-elevated caller.
"""

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from xml.sax.saxutils import escape

from .procflags import NO_WINDOW

DEST = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "shyyctl" / "helpers"
TASK_FOLDER = "\\shyyctl\\"
HELPERS = ("OpenRGB", "SensorHelper")
_DESCRIPTIONS = {
    "OpenRGB": "Started by shyyctl: OpenRGB with admin rights for RAM RGB, without a UAC prompt.",
    "SensorHelper": "Started by shyyctl: SensorHelper with admin rights for temperatures, without a UAC prompt.",
}


# -- the non-elevated side ---------------------------------------------------

def _self_command() -> tuple[str, list[str]]:
    """This program as it was started: the frozen exe, or the Python
    interpreter with main.py."""
    if getattr(sys, "frozen", False):
        return sys.executable, []
    return sys.executable, [str(Path(__file__).resolve().parent.parent / "main.py")]


def run_elevated(action: str, vendor_dir: Path | None = None, user: str = "",
                 openrgb_settings: Path | None = None, sensor_port: int = 8085) -> dict:
    """Runs main() below elevated (one UAC prompt) and waits for it.
    Blocking - call from a thread."""
    import ctypes
    from ctypes import wintypes

    result_file = Path(tempfile.gettempdir()) / f"shyyctl-helpers-{os.getpid()}.json"
    result_file.unlink(missing_ok=True)
    exe, args = _self_command()
    args += ["--elevated-helpers", action, "--result", str(result_file)]
    if action == "install":
        args += ["--vendor", str(vendor_dir), "--user", user,
                 "--openrgb-settings", str(openrgb_settings or ""), "--sensor-port", str(sensor_port)]

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
                    ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
                    ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
                    ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040 | 0x00000100  # SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.lpVerb = "runas"
    info.lpFile = exe
    info.lpParameters = subprocess.list2cmdline(args)
    info.nShow = 0  # SW_HIDE
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        return {"ok": False, "message": "the administrator prompt was declined" if err == 1223
                else f"could not start the setup (error {err})"}
    try:
        kernel32.WaitForSingleObject(info.hProcess, 300_000)
    finally:
        kernel32.CloseHandle(info.hProcess)
    try:
        return json.loads(result_file.read_text(encoding="utf-8"))
    except Exception:
        return {"ok": False, "message": "the setup didn't report back"}
    finally:
        result_file.unlink(missing_ok=True)


# -- the elevated side -------------------------------------------------------

class SetupError(Exception):
    pass


def _run(cmd: list[str], what: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or f"exit code {proc.returncode}"
        raise SetupError(f"{what}: {detail}")


def _helper_processes(only_under: Path | None = None) -> list:
    try:
        import psutil
    except ImportError:
        return []
    names = {f"{name.lower()}.exe" for name in HELPERS}
    out = []
    for proc in psutil.process_iter(["name", "exe"]):
        try:
            if (proc.info["name"] or "").lower() not in names:
                continue
            if only_under is not None:
                exe = proc.info["exe"] or ""
                if not exe.lower().startswith(str(only_under).lower() + os.sep):
                    continue
            out.append(proc)
        except Exception:
            continue
    return out


def _stop_helpers(only_under: Path | None = None) -> None:
    """Every running copy (or only those started from only_under) - the
    files can't be replaced or removed while one is running."""
    procs = _helper_processes(only_under)
    if not procs and only_under is None:
        # no psutil: by name
        subprocess.run(["taskkill", "/F", "/IM", "OpenRGB.exe", "/IM", "SensorHelper.exe"],
                       capture_output=True, timeout=30, creationflags=NO_WINDOW)
    for proc in procs:
        try:
            proc.kill()
        except Exception:
            pass
    time.sleep(0.8)


def _task_xml(user: str, command: Path, arguments: str, description: str) -> str:
    """No trigger - shyyctl starts it (schtasks /run). Runs only while the
    user is logged on, at the highest level the account has, for as long
    as it likes, on battery too, never twice at once."""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(description)}</Description>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(command))}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(str(command.parent))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _register_task(name: str, xml: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{name}.xml"
        path.write_text(xml, encoding="utf-16")  # schtasks wants UTF-16 with a BOM
        _run(["schtasks", "/create", "/tn", TASK_FOLDER + name, "/xml", str(path), "/f"],
             f"could not create the scheduled task {name}")


def _delete_task(name: str) -> None:
    subprocess.run(["schtasks", "/delete", "/tn", TASK_FOLDER + name, "/f"],
                   capture_output=True, timeout=30, creationflags=NO_WINDOW)


def install(vendor_dir: Path, user: str, openrgb_settings: Path | None, sensor_port: int,
            dest: Path = DEST) -> str:
    if not vendor_dir.is_dir():
        raise SetupError(f"vendor folder not found: {vendor_dir}")
    if not user:
        raise SetupError("no user given")

    _stop_helpers()
    dest.mkdir(parents=True, exist_ok=True)
    for name in HELPERS:
        src, dst = vendor_dir / name, dest / name
        if dst.exists():
            shutil.rmtree(dst)
        if src.is_dir():
            shutil.copytree(src, dst)

    # OpenRGB's settings - and the plugins it would load from next to them -
    # in a folder only administrators can write to. Starts from the user's
    # current OpenRGB.json (detectors, SDK server) and zone sizes; plugins
    # are deliberately not carried over.
    config = dest / "OpenRGB-config"
    if config.exists():
        shutil.rmtree(config)
    config.mkdir()
    if openrgb_settings and openrgb_settings.is_file():
        shutil.copyfile(openrgb_settings, config / "OpenRGB.json")
        sizes = openrgb_settings.parent / "sizes.ors"
        if sizes.is_file():
            shutil.copyfile(sizes, config / "sizes.ors")

    registered = []
    openrgb = next((dest / "OpenRGB").rglob("OpenRGB.exe"), None) if (dest / "OpenRGB").is_dir() else None
    if openrgb is not None:
        _register_task("OpenRGB", _task_xml(user, openrgb, f'--server --config "{config}"',
                                            _DESCRIPTIONS["OpenRGB"]))
        registered.append("OpenRGB")
    sensor = dest / "SensorHelper" / "SensorHelper.exe"
    if sensor.is_file():
        _register_task("SensorHelper", _task_xml(user, sensor, str(int(sensor_port)),
                                                 _DESCRIPTIONS["SensorHelper"]))
        registered.append("SensorHelper")
    if not registered:
        raise SetupError(f"neither helper was found in {vendor_dir}")

    (dest / "setup.json").write_text(json.dumps({
        "sensor_port": int(sensor_port), "user": user,
        "installed": datetime.datetime.now().astimezone().isoformat()}), encoding="utf-8")

    # Start the protected copies right away, so this takes effect now.
    for name in registered:
        _run(["schtasks", "/run", "/tn", TASK_FOLDER + name], f"could not start {name}")
    return "installed"


def remove(dest: Path = DEST) -> str:
    for name in HELPERS:
        _delete_task(name)
    if dest.exists():
        _stop_helpers(only_under=dest)
        shutil.rmtree(dest)
    parent = dest.parent
    try:
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass
    return "removed"


def main(argv: list[str]) -> int:
    """`--elevated-helpers install|remove --result FILE [--vendor DIR
    --user DOMAIN\\name --openrgb-settings FILE --sensor-port N]`"""
    action = argv[0] if argv else ""
    opts = {}
    rest = argv[1:]
    for key, value in zip(rest[::2], rest[1::2]):
        opts[key.lstrip("-")] = value
    result_file = opts.get("result")
    try:
        if action == "install":
            settings = opts.get("openrgb-settings")
            message = install(Path(opts.get("vendor", "")), opts.get("user", ""),
                              Path(settings) if settings else None, int(opts.get("sensor-port", 8085)))
        elif action == "remove":
            message = remove()
        else:
            raise SetupError(f"unknown action {action!r}")
        ok = True
    except Exception as exc:
        ok, message = False, str(exc) or type(exc).__name__
    if result_file:
        try:
            Path(result_file).write_text(json.dumps({"ok": ok, "message": message}), encoding="utf-8")
        except OSError:
            pass
    return 0 if ok else 1
