"""Ambient-light colour sampling from the Panorama display's own currently
applied media - not screen/desktop capture. The panel plays a file shyyctl
already manages (the gallery); this decodes that same local file with
ffmpeg at a small resolution and keeps the colours along the picture's
edge. edge_color() then extends that edge outwards, the way a TV's
ambilight does: an LED gets the colour of the edge at the point where a
line from the display's centre towards the LED leaves the picture - so
the fans to the right of the panel glow in what's at its right edge,
those above in what's at its top, and so on round the case.

A video is decoded once, not continuously: the first time it's used, its
32x16 frames (at the sampling rate) go into a small file next to it
(_build_frames), and from then on they're simply stepped through in time -
no ffmpeg running at all. Decoding the panel's 2240x1080 video live for a
32x16 edge cost ~10% of a core and ~70MB for as long as the effect ran.
Only a video too long for that (see MAX_CACHED_FRAMES) is still decoded
live, cheaply, by one long-lived ffmpeg process.
"""

import logging
import subprocess
import threading
import time
from pathlib import Path

from .panorama import resolve_ffmpeg_path
from .procflags import NO_WINDOW

log = logging.getLogger("ambient")

# ffmpeg's scale already averages every source pixel landing in an output
# pixel, so one scale gives the edge colours directly: 32x16 is fine enough
# along the edge for a case full of fans, and the edge band is averaged
# EDGE_DEPTH cells deep so a thin border line in the video doesn't decide
# the colour alone.
GRID_W, GRID_H = 32, 16
EDGE_DEPTH = 2
FRAME_BYTES = GRID_W * GRID_H * 3  # rgb24
# Every frame is decoded anyway (-re paces the decode, not the output), so
# sampling more of them costs little - and the light follows the video
# instead of jumping twice a second.
SAMPLE_FPS = 10
# The panel's picture (2240x1080) - the shape the edge is projected from.
MEDIA_ASPECT = 2240 / 1080
# Videos longer than this many sampled frames (~20 minutes at 10 fps, ~20MB
# of 32x16 frames) aren't precomputed but decoded live (_live_decode_loop).
MAX_CACHED_FRAMES = 13_000

_BLACK_EDGES = {"top": [(0, 0, 0)] * GRID_W, "bottom": [(0, 0, 0)] * GRID_W,
                "left": [(0, 0, 0)] * GRID_H, "right": [(0, 0, 0)] * GRID_H}


def edge_point(dx: float, dy: float, aspect: float = MEDIA_ASPECT) -> tuple[str, float]:
    """Where a line from the picture's centre in direction (dx, dy) - y
    downwards - leaves it: the edge ("top"/"bottom"/"left"/"right") and
    the position along it, 0..1 (left to right, or top to bottom)."""
    if dx == 0 and dy == 0:
        return "top", 0.5
    # the picture spans [-aspect, aspect] x [-1, 1]; whichever pair of
    # sides the line reaches first is the one it leaves through
    to_side = aspect / abs(dx) if dx else float("inf")
    to_top_bottom = 1 / abs(dy) if dy else float("inf")
    if to_side <= to_top_bottom:
        return ("right" if dx > 0 else "left"), (dy * to_side + 1) / 2
    return ("bottom" if dy > 0 else "top"), (dx * to_top_bottom / aspect + 1) / 2


def sample_edge(edges: dict, side: str, u: float) -> tuple[int, int, int]:
    """The edge colour at u (0..1) along one side, interpolated between
    the sampled cells."""
    cells = edges[side]
    pos = max(0.0, min(1.0, u)) * len(cells) - 0.5
    i = max(0, min(len(cells) - 1, int(pos // 1)))
    j = min(len(cells) - 1, i + 1)
    f = max(0.0, min(1.0, pos - i))
    a, b = cells[i], cells[j]
    return tuple(round(a[c] + (b[c] - a[c]) * f) for c in range(3))


class AmbientSampler:
    def __init__(self, fps: int = SAMPLE_FPS):
        # The startup animation's ambient show samples faster than the
        # effect: a few seconds of intro need to follow the video closely.
        self._fps = fps
        self._lock = threading.Lock()        # guards _edges
        self._state_lock = threading.Lock()  # guards source switching below
        self._edges = dict(_BLACK_EDGES)
        self._path: str | None = None
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        # One Event per started source rather than one shared flag: a
        # shared flag was cleared again by the very next set_source(), so
        # an old decode thread that hadn't noticed its stop yet kept
        # running (and kept overwriting zones) alongside the new one.
        self._run_stop: threading.Event | None = None
        self._started_at = 0.0

    def edges(self) -> dict[str, list[tuple[int, int, int]]]:
        """The latest frame's edge colours: "top"/"bottom" left to right,
        "left"/"right" top to bottom."""
        with self._lock:
            return self._edges

    def edge_color(self, dx: float, dy: float) -> tuple[int, int, int]:
        """The picture's edge extended outwards: the colour where a line
        from its centre in direction (dx, dy) - y downwards - leaves it."""
        side, u = edge_point(dx, dy)
        return sample_edge(self.edges(), side, u)

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
            target = self._play_video if is_video else self._sample_image_once
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

    def _play_video(self, path: str, stop: threading.Event):
        """Steps through the video's precomputed 32x16 frames in time,
        looping like the panel does; decodes live only if the video is too
        long to precompute (or precomputing failed)."""
        frames = self._cached_frames(path, stop)
        if stop.is_set():
            return
        if not frames:
            self._live_decode_loop(path, stop)
            return
        count = len(frames) // FRAME_BYTES
        start = time.monotonic()
        shown = -1
        while not stop.is_set():
            index = int((time.monotonic() - start) * self._fps) % count
            if index != shown:
                self._update_zones_from_frame(frames[index * FRAME_BYTES:(index + 1) * FRAME_BYTES])
                shown = index
            stop.wait(1 / self._fps / 2)

    def _cache_file(self, path: str) -> Path:
        source = Path(path)
        return source.with_name(f"{source.name}.edges-{GRID_W}x{GRID_H}-{self._fps}fps.rgb")

    def _cached_frames(self, path: str, stop: threading.Event) -> bytes | None:
        cache = self._cache_file(path)
        try:
            if cache.stat().st_mtime >= Path(path).stat().st_mtime:
                data = cache.read_bytes()
                if data and len(data) % FRAME_BYTES == 0:
                    return data
        except OSError:
            pass
        data = self._build_frames(path, stop)
        if data:
            try:
                tmp = cache.with_name(cache.name + ".tmp")
                tmp.write_bytes(data)
                tmp.replace(cache)
            except OSError as exc:
                log.debug("ambient: could not keep %s: %s", cache, exc)
        return data

    def _build_frames(self, path: str, stop: threading.Event) -> bytes | None:
        """Every frame of the video at the sampling rate, scaled to 32x16 -
        one ffmpeg run as fast as it can go (a second or two for a short
        clip). None if it fails or the video is too long to keep."""
        cmd = [resolve_ffmpeg_path(), "-nostdin", "-loglevel", "error", "-skip_loop_filter", "all",
               "-flags2", "fast", "-i", path, "-an", "-sn", "-dn",
               "-vf", f"scale={GRID_W}:{GRID_H}:flags=area", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-r", str(self._fps), "-"]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    creationflags=NO_WINDOW)
        except Exception as exc:
            log.warning("ambient: could not start ffmpeg for %s: %s", path, exc)
            return None
        with self._state_lock:
            if stop.is_set():
                proc.kill()
                return None
            self._proc = proc
        chunks, size = [], 0
        try:
            while not stop.is_set():
                block = proc.stdout.read(FRAME_BYTES * 64)
                if not block:
                    break
                chunks.append(block)
                size += len(block)
                if size > MAX_CACHED_FRAMES * FRAME_BYTES:
                    log.info("ambient: %s is too long to precompute - decoding it live", path)
                    return None
        finally:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
        data = b"".join(chunks)
        data = data[:len(data) - len(data) % FRAME_BYTES]
        if stop.is_set() or not data:
            return None
        log.info("ambient: %s precomputed - %d frames", Path(path).name, len(data) // FRAME_BYTES)
        return data

    def _live_decode_loop(self, path: str, stop: threading.Event):
        ffmpeg_path = resolve_ffmpeg_path()
        # -re: read the input at its native frame rate. Without it ffmpeg
        # decodes as fast as the CPU allows - and with this loop pulling
        # each 48-byte frame immediately there's no back-pressure at all,
        # so it burned a full core re-decoding the (2240px-wide) video
        # over and over, with the sampled colours racing far ahead of
        # what the panel was actually showing.
        # Only the picture's edge at 32x16 is wanted, so decode cheaply:
        # one thread (by default ffmpeg starts one per core, each holding
        # full-size 2240x1080 frames - 150MB+ for this), no deblocking
        # (invisible at 32x16), no audio. Measured on the real panel video:
        # ~19% of a core and 153MB down to ~10% and 31MB.
        cmd = [ffmpeg_path, "-nostdin", "-loglevel", "error",
               "-threads", "1", "-skip_loop_filter", "all", "-flags2", "fast",
               "-re", "-stream_loop", "-1", "-i", path,
               "-an", "-sn", "-dn", "-filter_threads", "1",
               "-vf", f"scale={GRID_W}:{GRID_H}:flags=area", "-f", "rawvideo", "-pix_fmt", "rgb24",
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
        cmd = [ffmpeg_path, "-nostdin", "-loglevel", "error", "-threads", "1", "-i", path,
               "-vf", f"scale={GRID_W}:{GRID_H}:flags=area", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-frames:v", "1", "-"]
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    timeout=10, creationflags=NO_WINDOW)
            if len(result.stdout) >= FRAME_BYTES and not stop.is_set():
                self._update_zones_from_frame(result.stdout[:FRAME_BYTES])
        except Exception as exc:
            log.warning("ambient: could not sample image %s: %s", path, exc)

    def _update_zones_from_frame(self, data: bytes):
        """One rgb24 GRID_W x GRID_H frame -> the colours along its edge,
        each averaged EDGE_DEPTH cells inwards."""
        def px(x, y):
            i = (y * GRID_W + x) * 3
            return data[i], data[i + 1], data[i + 2]

        def avg(pxs):
            n = len(pxs)
            return tuple(sum(p[c] for p in pxs) // n for c in range(3))

        depth = range(EDGE_DEPTH)
        edges = {
            "top": [avg([px(x, d) for d in depth]) for x in range(GRID_W)],
            "bottom": [avg([px(x, GRID_H - 1 - d) for d in depth]) for x in range(GRID_W)],
            "left": [avg([px(d, y) for d in depth]) for y in range(GRID_H)],
            "right": [avg([px(GRID_W - 1 - d, y) for d in depth]) for y in range(GRID_H)],
        }
        with self._lock:
            self._edges = edges
