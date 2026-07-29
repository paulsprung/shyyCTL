"""Windows-only subprocess flag suppressing the console window every child
process would otherwise pop open.

Only bites on the frozen build (console=False - see
tools/build_installer/shyyctl.spec): a windowed-subsystem parent has no
console of its own for a child console app (liquidctl, nvidia-smi,
ffmpeg, tasklist, adb) to inherit, so Windows creates a brand new,
visible one for each invocation instead - confirmed by direct user
report after the installer's first real release ("it now opens cmd
windows all the time"), which lines up exactly: liquidctl/nvidia-smi
alone are polled every few seconds by the Status/Fans pages and the
curve loop, so this was firing constantly. Running via `python main.py`
in a terminal never showed this, since children inherited that real
console instead of needing one of their own.

0 (a harmless no-op flag) on non-Windows, where CREATE_NO_WINDOW doesn't
exist at all - subprocess.CREATE_NO_WINDOW is a Windows-only attribute.
"""

import subprocess
import sys

NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
