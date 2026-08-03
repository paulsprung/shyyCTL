"""Ambient-light colour sampling from the Panorama display's own currently
applied media - not screen/desktop capture. The panel plays a file shyyctl
already manages (the gallery); this decodes that same local file with
ffmpeg at a tiny resolution and low frame rate, and reduces each frame to
four edge-zone average colours (top/bottom/left/right) the "ambient" RGB
effect blends per-LED by position - the same idea a TV's ambilight uses,
just against a known local file instead of a live HDMI/display feed.

One long-lived ffmpeg subprocess per source (not re-invoked per frame) -
video decode overhead should happen once regardless of how many frames
the effect loop consumes from it, same reasoning app/openrgb_raw.py's
docstring gives for holding one hub connection open for a whole effect
rather than reopening it constantly.
"""

import logging
import subprocess
import threading

from .panorama import resolve_ffmpeg_path
from .procflags import NO_WINDOW

log = logging.getLogger("ambient")

# 4x4 is the smallest grid that still gives four distinct edges (a top
# row, a bottom row, a left column, a right column) without needing a
# second ffmpeg filter pass to average sub-regions separately - scale's
# own default downsampling already averages every source pixel that
# lands in each output pixel, so this one scale is the only reduction
# step needed.
GRID_W, GRID_H = 4, 4
FRAME_BYTES = GRID_W * GRID_H * 3  # rgb24
SAMPLE_FPS = 2  # LEDs don't need video frame rate, just to track big colour changes


class AmbientSampler:
    def __init__(self):
        self._lock = threading.Lock()
        self._zones = {"top": (0, 0, 0), "bottom": (0, 0, 0), "left": (0, 0, 0), "right": (0, 0, 0)}
        self._path: str | None = None
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def zones(self) -> dict[str, tuple[int, int, int]]:
        with self._lock:
            return dict(self._zones)

    def set_source(self, path: str, is_video: bool):
        """No-op if already sampling this exact file - called every few
        seconds from the effect loop (see main.py's _run_position_effect)
        to notice the human switching what's applied to the panel while
        ambient is running, without restarting the decode on every one
        of those checks when nothing actually changed."""
        if path == self._path:
            return
        self.stop()
        self._path = path
        self._stop.clear()
        if is_video:
            self._thread = threading.Thread(target=self._decode_loop, args=(path,), daemon=True)
            self._thread.start()
        else:
            threading.Thread(target=self._sample_image_once, args=(path,), daemon=True).start()

    def stop(self):
        self._stop.set()
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass
            self._proc = None
        self._path = None

    def _decode_loop(self, path: str):
        ffmpeg_path = resolve_ffmpeg_path()
        cmd = [ffmpeg_path, "-nostdin", "-loglevel", "error", "-stream_loop", "-1", "-i", path,
               "-vf", f"scale={GRID_W}:{GRID_H}", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-r", str(SAMPLE_FPS), "-"]
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                          creationflags=NO_WINDOW)
        except FileNotFoundError:
            log.warning("ambient: ffmpeg not found (checked vendor/ffmpeg.exe and PATH)")
            return
        except Exception as exc:
            log.warning("ambient: could not start ffmpeg for %s: %s", path, exc)
            return
        try:
            while not self._stop.is_set():
                data = self._proc.stdout.read(FRAME_BYTES)
                if not data or len(data) < FRAME_BYTES:
                    break  # video ended without -stream_loop looping (or the process died) - stop quietly
                self._update_zones_from_frame(data)
        finally:
            try:
                self._proc.kill()
            except Exception:
                pass

    def _sample_image_once(self, path: str):
        """A static image never changes frame to frame - one decode, not
        a persistent process left running to redundantly redecode the
        same pixels every 0.5s."""
        ffmpeg_path = resolve_ffmpeg_path()
        cmd = [ffmpeg_path, "-nostdin", "-loglevel", "error", "-i", path,
               "-vf", f"scale={GRID_W}:{GRID_H}", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-frames:v", "1", "-"]
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    timeout=10, creationflags=NO_WINDOW)
            if len(result.stdout) >= FRAME_BYTES:
                self._update_zones_from_frame(result.stdout[:FRAME_BYTES])
        except Exception as exc:
            log.warning("ambient: could not sample image %s: %s", path, exc)

    def _update_zones_from_frame(self, data: bytes):
        pixels = [tuple(data[i:i + 3]) for i in range(0, FRAME_BYTES, 3)]  # row-major, GRID_H x GRID_W

        def avg(pxs):
            n = len(pxs)
            return tuple(sum(p[c] for p in pxs) // n for c in range(3))

        top = avg(pixels[0:GRID_W])
        bottom = avg(pixels[(GRID_H - 1) * GRID_W:GRID_H * GRID_W])
        left = avg([pixels[r * GRID_W] for r in range(GRID_H)])
        right = avg([pixels[r * GRID_W + GRID_W - 1] for r in range(GRID_H)])
        with self._lock:
            self._zones = {"top": top, "bottom": bottom, "left": left, "right": right}
