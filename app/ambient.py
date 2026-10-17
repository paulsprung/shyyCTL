"""Ambient-light colour sampling from the Panorama display's own currently
applied media - not screen/desktop capture. The panel plays a file shyyctl
already manages (the gallery); this decodes that same local file with
ffmpeg at a small resolution and keeps the colours along the picture's
edge. edge_color() then extends that edge outwards, the way a TV's
ambilight does: an LED gets the colour of the edge at the point where a
line from the display's centre towards the LED leaves the picture - so
the fans to the right of the panel glow in what's at its right edge,
those above in what's at its top, and so on round the case.

Which part of the picture counts as its edge is adjustable (configure()):
a frame cut in from each side - for a video with its own border, black
bars, or a busy rim - and how deep inwards the edge colour is averaged.

A video is decoded once, not continuously: the first time it's used, its
32x16 frames go into a small file next to it (_build_frames), at the
panel's own 30 fps, and from then on they're stepped through in time - no
ffmpeg running at all. Decoding the panel's 2240x1080 video live for a
32x16 edge cost ~10% of a core and ~70MB for as long as the effect ran.
The timeline starts when the panel started the video (set_source's
started_at), so the light runs with the picture rather than with whenever
the effect happened to start. Only a video too long for that (see
MAX_CACHED_FRAMES) is still decoded live, cheaply, by one long-lived
ffmpeg process.
"""

import logging
import subprocess
import threading
import time
from pathlib import Path

from .panorama import MEDIA_FPS, resolve_ffmpeg_path
from .procflags import NO_WINDOW

log = logging.getLogger("ambient")

# ffmpeg's scale already averages every source pixel landing in an output
# pixel, so one scale gives the edge colours directly: 32x16 is fine enough
# along the edge for a case full of fans.
GRID_W, GRID_H = 32, 16
FRAME_BYTES = GRID_W * GRID_H * 3  # rgb24
# The edge colours of one frame, packed: top and bottom left to right,
# then left and right top to bottom.
EDGE_CELLS = 2 * GRID_W + 2 * GRID_H
EDGE_BYTES = EDGE_CELLS * 3
# The panel plays every video at MEDIA_FPS (app/panorama.py re-encodes it
# to that), so sampling at the same rate gives the light one colour per
# picture the panel shows.
SAMPLE_FPS = MEDIA_FPS
# The panel's picture (2240x1080) - the shape the edge is projected from.
MEDIA_ASPECT = 2240 / 1080
# Videos longer than this many frames (10 minutes at 30 fps; ~28MB of
# 32x16 frames on disk, ~5MB of edges in memory) aren't precomputed but
# decoded live (_live_decode_loop).
MAX_CACHED_FRAMES = 18_000

# The frame the edge is taken at: (left, top, right, bottom), each the
# fraction of the picture cut off that side; and how deep inwards the
# colour is averaged, as a fraction of the framed picture's size.
FULL_CROP = (0.0, 0.0, 0.0, 0.0)
DEFAULT_DEPTH = 0.1

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


def depth_cells(depth: float) -> tuple[int, int]:
    """(rows, columns) of the 32x16 grid averaged for a depth fraction:
    rows for the top and bottom edge, columns for the left and right."""
    rows = max(1, min(GRID_H // 2, round(depth * GRID_H)))
    cols = max(1, min(GRID_W // 2, round(depth * GRID_W)))
    return rows, cols


def frame_edges(grid: bytes, rows: int, cols: int) -> bytes:
    """One rgb24 GRID_W x GRID_H frame -> its packed edge colours (see
    EDGE_BYTES), each averaged `rows`/`cols` cells inwards."""
    out = bytearray(EDGE_BYTES)
    o = 0
    row = GRID_W * 3
    for y0, dy in ((0, 1), (GRID_H - 1, -1)):          # top, bottom
        for x in range(GRID_W):
            r = g = b = 0
            for d in range(rows):
                i = (y0 + dy * d) * row + x * 3
                r += grid[i]; g += grid[i + 1]; b += grid[i + 2]
            out[o] = r // rows; out[o + 1] = g // rows; out[o + 2] = b // rows
            o += 3
    for x0, dx in ((0, 1), (GRID_W - 1, -1)):          # left, right
        for y in range(GRID_H):
            r = g = b = 0
            for d in range(cols):
                i = y * row + (x0 + dx * d) * 3
                r += grid[i]; g += grid[i + 1]; b += grid[i + 2]
            out[o] = r // cols; out[o + 1] = g // cols; out[o + 2] = b // cols
            o += 3
    return bytes(out)


def unpack_edges(packed: bytes) -> dict[str, list[tuple[int, int, int]]]:
    cells = [tuple(packed[i:i + 3]) for i in range(0, EDGE_BYTES, 3)]
    return {"top": cells[:GRID_W], "bottom": cells[GRID_W:2 * GRID_W],
            "left": cells[2 * GRID_W:2 * GRID_W + GRID_H], "right": cells[2 * GRID_W + GRID_H:]}


def crop_filter(crop: tuple[float, float, float, float]) -> str:
    """ffmpeg's -vf for the frame: cut it out, then scale it to the grid."""
    scale = f"scale={GRID_W}:{GRID_H}:flags=area"
    left, top, right, bottom = crop
    if not any(crop):
        return scale
    w, h = max(0.05, 1 - left - right), max(0.05, 1 - top - bottom)
    return f"crop=iw*{w:.4f}:ih*{h:.4f}:iw*{left:.4f}:ih*{top:.4f},{scale}"


class AmbientSampler:
    def __init__(self, fps: int = SAMPLE_FPS):
        self._fps = fps
        self._lock = threading.Lock()        # guards _edges
        self._state_lock = threading.Lock()  # guards source switching below
        self._edges = dict(_BLACK_EDGES)
        self._path: str | None = None
        self._is_video = False
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        # One Event per started source rather than one shared flag: a
        # shared flag was cleared again by the very next set_source(), so
        # an old decode thread that hadn't noticed its stop yet kept
        # running (and kept overwriting zones) alongside the new one.
        self._run_stop: threading.Event | None = None
        self._started_at = 0.0
        # time.monotonic() at which the video's first frame was showing
        self._anchor = time.monotonic()
        self._crop = FULL_CROP
        self._depth = DEFAULT_DEPTH
        self._offset_s = 0.0

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

    def configure(self, crop: tuple[float, float, float, float] = FULL_CROP,
                  depth: float = DEFAULT_DEPTH, offset_s: float = 0.0):
        """Where the edge is taken (crop, depth - see FULL_CROP) and how
        far ahead of the panel the light runs (offset_s, negative: behind).
        A new frame re-samples the running source; an offset alone just
        shifts its timeline."""
        crop = tuple(max(0.0, min(0.45, float(v))) for v in crop)
        depth = max(0.02, min(0.5, float(depth)))
        with self._state_lock:
            self._offset_s = float(offset_s)
            if crop == self._crop and depth == self._depth:
                return
            self._crop, self._depth = crop, depth
            path, is_video = self._path, self._is_video
            if path is not None:
                self._start_locked(path, is_video)

    def set_source(self, path: str, is_video: bool, started_at: float | None = None):
        """started_at: time.monotonic() when the panel started playing this
        video from its beginning (None: unknown - from now on). No-op if
        already sampling this exact file, apart from taking a new
        started_at - called every few seconds from the effect loop (see
        main.py's _run_position_effect) to notice the human switching
        what's applied to the panel while ambient is running, without
        restarting the decode on every one of those checks when nothing
        actually changed. A video decoder that has died (ffmpeg crashed,
        file briefly unreadable) is restarted on one of these calls
        instead of leaving the colours frozen for the rest of the effect
        - at most every 10s, so a file ffmpeg can't decode at all doesn't
        respawn it every 2s."""
        with self._state_lock:
            if started_at is not None:
                self._anchor = started_at
            if path == self._path:
                alive = self._thread is not None and self._thread.is_alive()
                if not is_video or alive or time.monotonic() - self._started_at < 10:
                    return
            if started_at is None and path != self._path:
                self._anchor = time.monotonic()
            self._start_locked(path, is_video)

    def stop(self):
        with self._state_lock:
            self._stop_locked()

    def _start_locked(self, path: str, is_video: bool):
        self._stop_locked()
        self._path, self._is_video = path, is_video
        stop = threading.Event()
        self._run_stop = stop
        self._started_at = time.monotonic()
        target = self._play_video if is_video else self._sample_image_once
        self._thread = threading.Thread(target=target, args=(path, stop, self._crop, self._depth),
                                        daemon=True)
        self._thread.start()

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

    def _show(self, packed: bytes, stop: threading.Event):
        if stop.is_set():
            return
        edges = unpack_edges(packed)
        with self._lock:
            self._edges = edges

    def _play_video(self, path: str, stop: threading.Event, crop: tuple, depth: float):
        """Steps through the video's precomputed edges in time with the
        panel, looping like it does; decodes live only if the video is too
        long to precompute (or precomputing failed)."""
        grids = self._cached_frames(path, stop, crop)
        if stop.is_set():
            return
        if not grids:
            self._live_decode_loop(path, stop, crop, depth)
            return
        count = len(grids) // FRAME_BYTES
        rows, cols = depth_cells(depth)
        # Only the edges are kept: a sixth of the frames' size.
        edges = b"".join(frame_edges(grids[i * FRAME_BYTES:(i + 1) * FRAME_BYTES], rows, cols)
                         for i in range(count))
        del grids
        shown = -1
        while not stop.is_set():
            pos = (time.monotonic() - self._anchor + self._offset_s) * self._fps
            index = int(pos // 1) % count
            if index != shown:
                self._show(edges[index * EDGE_BYTES:(index + 1) * EDGE_BYTES], stop)
                shown = index
            # wake for the next frame, not at some fixed rate beside it
            stop.wait(max(0.002, (1 - pos % 1) / self._fps))

    def _cache_file(self, path: str, crop: tuple) -> Path:
        source = Path(path)
        tag = "full" if not any(crop) else "-".join(str(round(v * 1000)) for v in crop)
        return source.with_name(f"{source.name}.edges-{GRID_W}x{GRID_H}-{self._fps}fps-{tag}.rgb")

    def _cached_frames(self, path: str, stop: threading.Event, crop: tuple) -> bytes | None:
        cache = self._cache_file(path, crop)
        try:
            if cache.stat().st_mtime >= Path(path).stat().st_mtime:
                data = cache.read_bytes()
                if data and len(data) % FRAME_BYTES == 0:
                    return data
        except OSError:
            pass
        if not self._build_frames(path, stop, crop, cache):
            return None
        try:
            return cache.read_bytes()
        except OSError:
            return None

    def _build_frames(self, path: str, stop: threading.Event, crop: tuple, cache: Path) -> bool:
        """Every frame of the video at the sampling rate, framed and scaled
        to 32x16, into `cache` - one ffmpeg run as fast as it can go (a
        second or two for a short clip). False if it fails or the video is
        too long to keep. A different frame's (or an older version's)
        file for the same video is removed - only one is ever used."""
        cmd = [resolve_ffmpeg_path(), "-nostdin", "-loglevel", "error", "-skip_loop_filter", "all",
               "-flags2", "fast", "-i", path, "-an", "-sn", "-dn",
               "-vf", crop_filter(crop), "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-r", str(self._fps), "-"]
        tmp = cache.with_name(cache.name + ".tmp")
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    creationflags=NO_WINDOW)
        except Exception as exc:
            log.warning("ambient: could not start ffmpeg for %s: %s", path, exc)
            return False
        with self._state_lock:
            if stop.is_set():
                proc.kill()
                return False
            self._proc = proc
        size, ok = 0, False
        try:
            # straight to disk: a long video's frames never sit in memory
            with open(tmp, "wb") as out:
                while not stop.is_set():
                    block = proc.stdout.read(FRAME_BYTES * 64)
                    if not block:
                        break
                    out.write(block)
                    size += len(block)
                    if size > MAX_CACHED_FRAMES * FRAME_BYTES:
                        log.info("ambient: %s is too long to precompute - decoding it live", path)
                        break
                ok =not stop.is_set() and FRAME_BYTES <= size <= MAX_CACHED_FRAMES * FRAME_BYTES
                if ok:
                    out.truncate(size - size % FRAME_BYTES)
        except OSError as exc:
            log.debug("ambient: could not keep %s: %s", cache, exc)
            ok = False
        finally:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
        try:
            if not ok:
                tmp.unlink(missing_ok=True)
                return False
            tmp.replace(cache)
        except OSError as exc:
            log.debug("ambient: could not keep %s: %s", cache, exc)
            return False
        for old in cache.parent.glob(f"{Path(path).name}.edges-*"):
            if old != cache:
                try:
                    old.unlink()
                except OSError:
                    pass
        log.info("ambient: %s precomputed - %d frames", Path(path).name, size // FRAME_BYTES)
        return True

    def _live_decode_loop(self, path: str, stop: threading.Event, crop: tuple, depth: float):
        ffmpeg_path = resolve_ffmpeg_path()
        # -re: read the input at its native frame rate. Without it ffmpeg
        # decodes as fast as the CPU allows - and with this loop pulling
        # each small frame immediately there's no back-pressure at all,
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
               "-vf", crop_filter(crop), "-f", "rawvideo", "-pix_fmt", "rgb24",
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
        rows, cols = depth_cells(depth)
        try:
            while not stop.is_set():
                data = proc.stdout.read(FRAME_BYTES)
                if not data or len(data) < FRAME_BYTES:
                    break  # the process died or was killed - set_source restarts it if still wanted
                self._show(frame_edges(data, rows, cols), stop)
        finally:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass

    def _sample_image_once(self, path: str, stop: threading.Event, crop: tuple, depth: float):
        """A static image never changes frame to frame - one decode, not
        a persistent process left running to redundantly redecode the
        same pixels every 0.5s."""
        ffmpeg_path = resolve_ffmpeg_path()
        cmd = [ffmpeg_path, "-nostdin", "-loglevel", "error", "-threads", "1", "-i", path,
               "-vf", crop_filter(crop), "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-frames:v", "1", "-"]
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    timeout=10, creationflags=NO_WINDOW)
            if len(result.stdout) >= FRAME_BYTES:
                self._show(frame_edges(result.stdout[:FRAME_BYTES], *depth_cells(depth)), stop)
        except Exception as exc:
            log.warning("ambient: could not sample image %s: %s", path, exc)
