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
import time

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
    def __init__(self, fps: int = SAMPLE_FPS):
        # The startup animation's ambient show samples faster than the
        # effect: a few seconds of intro need to follow the video closely.
        self._fps = fps
        self._lock = threading.Lock()        # guards _zones
        self._state_lock = threading.Lock()  # guards source switching below
        self._zones = {"top": (0, 0, 0), "bottom": (0, 0, 0), "left": (0, 0, 0), "right": (0, 0, 0)}
        self._path: str | None = None
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        # One Event per started source rather than one shared flag: a
        # shared flag was cleared again by the very next set_source(), so
        # an old decode thread that hadn't noticed its stop yet kept
        # running (and kept overwriting zones) alongside the new one.
        self._run_stop: threading.Event | None = None
        self._started_at = 0.0

    def zones(self) -> dict[str, tuple[int, int, int]]:
        with self._lock:
            return dict(self._zones)

    def set_source(self, path: str, is_video: bool):
        """No-op if already sampling this exact file - called every few
        seconds from the effect loop (see main.py's _run_position_effect)
        to notice the human switching what's applied to the panel while
        ambient is running, without restarting the decode on every one
        of those checks when nothing actually changed. A video decoder
        that has died (ffmpeg crashed, file briefly unreadable) is
        restarted on one of these calls instead of leaving the colours
        frozen for the rest of the effect - at most every 10s, so a file
        ffmpeg can't decode at all doesn't respawn it every 2s."""
        with self._state_lock:
            if path == self._path:
                alive = self._thread is not None and self._thread.is_alive()
                if not is_video or alive or time.monotonic() - self._started_at < 10:
                    return
            self._stop_locked()
            self._path = path
            stop = threading.Event()
            self._run_stop = stop
            self._started_at = time.monotonic()
            target = self._decode_loop if is_video else self._sample_image_once
            self._thread = threading.Thread(target=target, args=(path, stop), daemon=True)
            self._thread.start()

    def stop(self):
        with self._state_lock:
            self._stop_locked()

    def _stop_locked(self):
        if self._run_stop is not None:
            self._run_stop.set()
            self._run_stop = None
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        self._path = None
        self._thread = None

    def _decode_loop(self, path: str, stop: threading.Event):
        ffmpeg_path = resolve_ffmpeg_path()
        # -re: read the input at its native frame rate. Without it ffmpeg
        # decodes as fast as the CPU allows - and with this loop pulling
        # each 48-byte frame immediately there's no back-pressure at all,
        # so it burned a full core re-decoding the (2240px-wide) video
        # over and over, with the sampled colours racing far ahead of
        # what the panel was actually showing.
        cmd = [ffmpeg_path, "-nostdin", "-loglevel", "error", "-re", "-stream_loop", "-1", "-i", path,
               "-vf", f"scale={GRID_W}:{GRID_H}", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-r", str(self._fps), "-"]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     creationflags=NO_WINDOW)
        except FileNotFoundError:
            log.warning("ambient: ffmpeg not found (checked vendor/ffmpeg.exe and PATH)")
            return
        except Exception as exc:
            log.warning("ambient: could not start ffmpeg for %s: %s", path, exc)
            return
        # Registered for stop() under the same lock stop() takes, and only
        # if this run wasn't already stopped while Popen was starting -
        # otherwise nothing would ever kill this process.
        with self._state_lock:
            if stop.is_set():
                proc.kill()
                return
            self._proc = proc
        try:
            while not stop.is_set():
                data = proc.stdout.read(FRAME_BYTES)
                if not data or len(data) < FRAME_BYTES:
                    break  # the process died or was killed - set_source restarts it if still wanted
                if stop.is_set():
                    break
                self._update_zones_from_frame(data)
        finally:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass

    def _sample_image_once(self, path: str, stop: threading.Event):
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
            if len(result.stdout) >= FRAME_BYTES and not stop.is_set():
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
