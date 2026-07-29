"""Tryx Panorama display - one USB control channel for everything.

The real wire protocol, confirmed against a genuine KANALI session this
project captured (Wireshark + USBPcap, decoded byte-for-byte, correlated
against a precise documented action timeline - not reverse-engineered by
guesswork): b"TRYX" + a 4-byte little-endian length + a real Protocol
Buffers message, on host->device (OUT) frames. Device->host (IN) replies
skip the "TRYX" prefix entirely and are raw protobuf directly.

This supersedes an earlier version of this file that ported
github.com/fadli0029/reed-tpse's JSON-over-0x5A-frames protocol - that
project verified its work against the Panorama SE (2160x1080) over Linux
CDC-ACM, and turned out to not apply to this hardware at all: this panel
(VID 0x391A / PID 0x1011, shows up as "RK PANO" - RK = Rockchip, its SoC)
sent and received zero bytes of that protocol in real captures. The actual
protocol confirmed here is unrelated to reed-tpse's, and this panel talks
to Windows via usbprint.sys (USB Printing Support), not a COM port -
PanoramaPrint below, via CreateFile/ReadFile/WriteFile, is the confirmed
correct transport; this was independently verified as the driver KANALI's
own installer sets up.

Confirmed message types (field numbers are what real captured traffic
uses - names are inferred from context, not from a schema, since none is
public):
    f100/f102/f101 (registration burst, sent once after connecting) ->
        triggers the device to volunteer three replies unprompted:
        f500 (device info: OS, kernel, firmware/app versions, serial),
        f502 (screen resolution), f501 ("ok" ack).
    f103 ("N/A" placeholder param) -> f503, a full listing of every media
        file already on the device (both user-uploaded and built-in
        defaults), each named "<original filename>.h264_<W>x<H>" - the
        device transcodes everything (including plain images) to H.264
        video internally after upload_media() below finishes (see f400/
        f401/f402 further down).
    f10 ("hello?") -> f10 ("Hey!") - lightweight periodic keepalive,
        confirmed sent roughly every 2-4s during an active KANALI session.
    f200 (screen config: poweron media, standby media, active media,
        brightness) -> f600/f601 "ok" ack, immediately followed by a
        second short f201 (empty) commit/flush packet. This ONE message
        carries both "which file is showing" and "what brightness" -
        every observed KANALI action (brightness change, applying an
        image or gif) resends the FULL config, not a partial update.
        Brightness (f200.f5.f2, 0-100) is simply omitted from the message
        when 0, since protobuf skips default/zero values - confirmed by
        directly diffing a 0% and a 100% capture, byte for byte.

Media transfer is NOT ADB, despite an earlier version of this project
assuming it would be (ported from reed-tpse's approach, which used ADB
push against a different Panorama variant): `adb devices` sees nothing
for this panel at all, confirmed directly. Real uploads go over the exact
same usbprint.sys connection as everything else above, via a separate
confirmed three-message exchange (f400 announce/f401 data/f402 finalize -
see upload_media() and the section docstring near those builders further
down). The Media class and MEDIA_PATH near the bottom of this file are
what's left of that disproven ADB theory - kept only because
tools/test_panorama.py still has an (unused-by-anything-real) menu option
for it, not because anything here still relies on it. liquidctl/OpenRGB
are never involved with this panel either way - it's a fully independent
control surface from the fan/RGB side of this project.
"""

import ctypes
import datetime
import logging
import random
import struct
import subprocess
import time
from pathlib import Path

log = logging.getLogger("panorama")

MEDIA_PATH = "/sdcard/pcMedia/"
TRYX_MAGIC = b"TRYX"
MEDIA_WIDTH = 2240
MEDIA_HEIGHT = 1080
MEDIA_SUFFIX = f".h264_{MEDIA_WIDTH}x{MEDIA_HEIGHT}"

# These two were placeholder guesses ("default_poweron.mp4"/
# "default_standby.mp4") until a real device's own media listing (f103 ->
# f503, list_media() below) showed the actual factory filenames are
# "start.mp4" and "screensaver.mp4" under /userdata/default/ - alongside
# default_01..06.mp4 (selectable built-in animations, likely what a bare
# KANALI install's own preset gallery shows) and whatever a human has
# separately uploaded, listed under /userdata/user/. This matters beyond
# cosmetics: set_screen_config() below resends poweron_media/standby_media
# on EVERY call (confirmed real protocol behavior, not a shortcut this
# code took - see its docstring), including from set_media() (applying a
# picture) and set_brightness() (just changing brightness), neither of
# which overrides these two params. With the old wrong guesses, every one
# of those calls was silently pointing the panel's poweron/standby slots
# at files that don't exist on it.
DEFAULT_POWERON_MEDIA = f"start.mp4{MEDIA_SUFFIX}"
DEFAULT_STANDBY_MEDIA = f"screensaver.mp4{MEDIA_SUFFIX}"


class PanoramaError(Exception):
    pass


def media_filename(remote_name: str) -> str:
    """The name a freshly-pushed file is referenced by in a screen config
    message, once the device has transcoded it - confirmed against real
    media listing responses this session (every entry, including plain
    .png uploads, ends up suffixed exactly this way)."""
    return remote_name + MEDIA_SUFFIX


# -- protobuf wire format: minimal encoder + a schema-less decoder ----------
#
# No .proto schema is public for this device, so there's no code-generated
# client here - just enough hand-rolled wire-format plumbing to build the
# few confirmed message shapes above, and a generic decoder (field number +
# wire type only, no names) good enough to inspect any response.

def _pb_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _pb_tag(field_no: int, wire_type: int) -> bytes:
    return _pb_varint((field_no << 3) | wire_type)


def pb_varint_field(field_no: int, value: int) -> bytes:
    return _pb_tag(field_no, 0) + _pb_varint(value)


def pb_bytes_field(field_no: int, data: bytes) -> bytes:
    return _pb_tag(field_no, 2) + _pb_varint(len(data)) + data


def pb_string_field(field_no: int, text: str) -> bytes:
    return pb_bytes_field(field_no, text.encode("utf-8"))


def pb_message_field(field_no: int, *parts: bytes) -> bytes:
    return pb_bytes_field(field_no, b"".join(parts))


def decode_pb(buf: bytes) -> list:
    """Schema-less decode: [(field_no, kind, value), ...], kind one of
    varint/string/bytes/message/fixed32/fixed64. 'string' is a best-effort
    guess (all-printable-ASCII length-delimited fields render as text,
    everything else as a nested message or raw bytes) - good enough for
    inspecting real traffic, not a substitute for a real schema."""
    out = []
    pos = 0
    n = len(buf)
    while pos < n:
        start = pos
        try:
            tag = 0
            shift = 0
            while True:
                b = buf[pos]
                pos += 1
                tag |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
        except IndexError:
            break
        field_no, wire_type = tag >> 3, tag & 0x7
        try:
            if wire_type == 0:
                val = 0
                shift = 0
                while True:
                    b = buf[pos]
                    pos += 1
                    val |= (b & 0x7F) << shift
                    if not (b & 0x80):
                        break
                    shift += 7
                out.append((field_no, "varint", val))
            elif wire_type == 2:
                length = 0
                shift = 0
                while True:
                    b = buf[pos]
                    pos += 1
                    length |= (b & 0x7F) << shift
                    if not (b & 0x80):
                        break
                    shift += 7
                chunk = buf[pos:pos + length]
                pos += length
                if len(chunk) != length:
                    break
                if chunk and all(32 <= b < 127 or b in (9, 10, 13) for b in chunk):
                    out.append((field_no, "string", chunk.decode("ascii", "replace")))
                else:
                    nested = decode_pb(chunk) if chunk else []
                    out.append((field_no, "message", nested) if nested else (field_no, "bytes", chunk))
            elif wire_type == 5:
                out.append((field_no, "fixed32", struct.unpack_from("<I", buf, pos)[0]))
                pos += 4
            elif wire_type == 1:
                out.append((field_no, "fixed64", struct.unpack_from("<Q", buf, pos)[0]))
                pos += 8
            else:
                pos = start
                break
        except (IndexError, struct.error):
            pos = start
            break
    return out


def pb_get(entries: list, field_no: int):
    """First value for a given top-level field number, or None."""
    for fno, _kind, val in entries:
        if fno == field_no:
            return val
    return None


def build_tryx_frame(protobuf_body: bytes) -> bytes:
    return TRYX_MAGIC + struct.pack("<I", len(protobuf_body)) + protobuf_body


# -- confirmed message builders ----------------------------------------------

def _envelope(inner: bytes) -> bytes:
    """Every observed OUT message starts with an empty/near-empty f1
    wrapper before the real payload field - reproduced as-is rather than
    explained, since its purpose isn't clear from traffic alone."""
    return inner


def build_registration_burst() -> list:
    """Three frames sent once after connecting; triggers the device to
    volunteer its handshake info unprompted. Field values ("NA"/1) are
    reproduced exactly as KANALI sends them - placeholders, not addresses
    or anything device-specific, so hardcoding them is safe."""
    f1 = pb_message_field(1, pb_varint_field(1, 1))
    return [
        build_tryx_frame(f1 + pb_message_field(100, pb_string_field(1, "NA"))),
        build_tryx_frame(f1 + pb_message_field(102, pb_string_field(1, "NA"))),
        build_tryx_frame(f1 + pb_message_field(101, pb_varint_field(1, 1))),
    ]


def build_ping_frame() -> bytes:
    return build_tryx_frame(pb_bytes_field(1, b"") + pb_message_field(10, pb_string_field(1, "hello?")))


def build_media_list_frame() -> bytes:
    return build_tryx_frame(pb_bytes_field(1, b"") + pb_message_field(103, pb_string_field(1, "N/A")))


def build_screen_config_frame(active_media: str, brightness: int,
                              poweron_media: str = DEFAULT_POWERON_MEDIA,
                              standby_media: str = DEFAULT_STANDBY_MEDIA) -> bytes:
    """The one confirmed command for both 'which file is showing' and
    'what brightness' - every real KANALI action resent this whole thing,
    not a partial update, so this does too. brightness is clamped 0-100;
    the subfield is omitted entirely at 0, matching real captured traffic
    (protobuf skips default/zero values) rather than sending an explicit 0.
    """
    brightness = max(0, min(100, int(brightness)))
    slot1 = pb_message_field(1, pb_string_field(1, poweron_media))
    slot2 = pb_message_field(2, pb_varint_field(1, 1) + pb_string_field(2, standby_media))
    slot3 = pb_message_field(3, pb_string_field(3, active_media))
    if brightness > 0:
        brightness_field = pb_message_field(5, pb_varint_field(1, 1) + pb_varint_field(2, brightness))
    else:
        brightness_field = pb_message_field(5, pb_varint_field(1, 1))
    f200 = pb_message_field(200, slot1 + slot2 + slot3 + brightness_field)
    return build_tryx_frame(pb_bytes_field(1, b"") + f200)


def build_commit_frame() -> bytes:
    """Empty follow-up sent right after screen config, every time,
    reproduced as observed - purpose not confirmed (possibly a generic
    flush/sync, since the same field also appeared after an unrelated
    request in captured traffic), but harmless and cheap to always send."""
    return build_tryx_frame(pb_bytes_field(1, b"") + pb_bytes_field(201, b""))


# -- media upload: the real mechanism, confirmed against two independent ----
# -- real uploads (different files, different sessions), NOT adb -----------
#
# ADB genuinely isn't involved in getting a new image/gif onto the panel -
# `adb devices` sees nothing at all, and this three-message exchange (all
# over the same usbprint.sys connection as everything else) is what two
# separate real captures showed happening instead, byte-for-byte identical
# in structure between them:
#
#   f400 announce: {session_id, filename, size}       -> ack (f800)
#   f401 data:     {session_id, <container bytes>}    -> ack (f801)
#   f402 finalize: {session_id, "media"}               -> ack (f802)
#
# session_id is a fresh small integer per upload (56559 and 176425 in the
# two captures) - nothing suggests it needs to be anything but different
# from other in-flight uploads, so a random int is used here.
#
# The container bytes (f401's payload) are NOT the original image/gif -
# confirmed by fully decoding a real one field-by-field:
#
#   u32-LE(66)                     length of the header block below
#   + protobuf header (66 bytes):
#       f1 varint  = 1297631300    magic/version constant (MEDIA_MAGIC)
#       f2 string  = "Tryx media header v1, fps={F}, size={W}x{H}"
#       f3 varint  = 2             assumed constant (codec id?), unconfirmed
#       f4 varint  = 1             assumed constant, unconfirmed
#       f5 varint  = fps
#       f6 varint  = width
#       f7 varint  = height
#       f8 varint  = 1             assumed constant, unconfirmed
#   + raw H.264 Annex-B elementary stream (starts with the standard
#     00 00 00 01 NAL start code)
#
# The video stream's own embedded x264 metadata string in the real capture
# documents the exact encoder settings used (fps=30, keyint=30, crf=18.0,
# profile baseline, yuv420p) - reproduced in encode_to_h264() below via
# ffmpeg/libx264, not guessed. f3/f4/f8 are only confirmed as constants in
# the one real upload decoded so far, not proven to never vary.

MEDIA_MAGIC = 1297631300
MEDIA_FPS = 30


def _media_envelope(session_id: int) -> bytes:
    return pb_message_field(1, pb_varint_field(2, session_id))


def build_media_announce_frame(session_id: int, filename: str, size: int) -> bytes:
    body = _media_envelope(session_id) + pb_message_field(
        400, pb_string_field(1, filename) + pb_varint_field(2, size))
    return build_tryx_frame(body)


def build_media_data_frame(session_id: int, container: bytes) -> bytes:
    body = _media_envelope(session_id) + pb_message_field(401, pb_bytes_field(1, container))
    return build_tryx_frame(body)


def build_media_finalize_frame(session_id: int) -> bytes:
    body = _media_envelope(session_id) + pb_message_field(402, pb_string_field(1, "media"))
    return build_tryx_frame(body)


def count_h264_frames(h264_bytes: bytes) -> int:
    """Counts real coded slice NAL units (type 1 = non-IDR, type 5 = IDR)
    in a raw H.264 Annex-B stream - actual displayable frames, not the
    SPS/PPS/SEI/AUD housekeeping NALs also present in the stream.

    This exists because build_media_container's f8 field (below) was
    wrongly assumed to be a constant "1", inferred from a single earlier
    real sample that turned out to be a short static-image loop. A real
    KANALI capture of an actual ~38s video upload (session traced this
    session) showed f8=2272 instead - not a constant at all, and closely
    matching that video's own real frame count (2272 / 60fps ≈ 38s,
    consistent with the clip's actual length; a direct NAL count of the
    ~29% of that same stream this project could capture already showed
    proportionally the right ballpark). Sending "1" regardless of the
    real stream length is the most likely reason a genuine video upload
    only ever showed its first frame on the panel: the device most
    likely trusts this declared count rather than independently parsing
    the whole stream to find out how long it actually is."""
    count = 0
    pos = 0
    n = len(h264_bytes)
    while True:
        pos = h264_bytes.find(b"\x00\x00\x01", pos)
        if pos == -1 or pos + 3 >= n:
            break
        if h264_bytes[pos + 3] & 0x1F in (1, 5):
            count += 1
        pos += 3
    return count or 1


def build_media_container(width: int, height: int, fps: int, h264_bytes: bytes,
                          frame_count: int, content_type: int = 2) -> bytes:
    """content_type (f3) and frame_count (f8) were both wrongly assumed
    to be constants (2 and 1 respectively) from a single real sample
    that turned out to be a short static-image loop - see
    count_h264_frames's docstring for the frame_count evidence.
    content_type's real meaning is still a guess beyond two data points
    (2 for that image sample, 4 for a real ~38s video sample) - "still
    image" vs "real video/motion" is the interpretation used here,
    matching upload_media()'s own image-vs-motion distinction exactly
    (see _has_own_motion), but genuinely unconfirmed as anything more
    specific than that."""
    header = (
        pb_varint_field(1, MEDIA_MAGIC)
        + pb_string_field(2, f"Tryx media header v1, fps={fps}, size={width}x{height}")
        + pb_varint_field(3, content_type)
        + pb_varint_field(4, 1)
        + pb_varint_field(5, fps)
        + pb_varint_field(6, width)
        + pb_varint_field(7, height)
        + pb_varint_field(8, frame_count)
    )
    return struct.pack("<I", len(header)) + header + h264_bytes


_MOTION_SUFFIXES = (".gif", ".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v")


def _has_own_motion(local_path: str) -> bool:
    """Whether local_path already carries its own frame timing (a real
    video or animated gif) rather than being a single still image that
    needs to be looped into one. Shared between encode_to_h264 (decides
    whether to loop it) and upload_media (decides the container header's
    content-type field, see build_media_container) so the two can never
    disagree about what kind of thing is actually being uploaded."""
    return Path(local_path).suffix.lower() in _MOTION_SUFFIXES


def resolve_ffmpeg_path() -> str:
    """vendor/ffmpeg.exe (see tools/setup_vendor.py) if it's been downloaded,
    else just 'ffmpeg' to fall back on PATH - resolved at call time, not
    import time, since the vendor file may not exist yet when this module
    is first imported but does by the time media is actually uploaded."""
    vendored = Path(__file__).resolve().parent.parent / "vendor" / "ffmpeg.exe"
    return str(vendored) if vendored.is_file() else "ffmpeg"


def encode_to_h264(local_path: str, width: int = MEDIA_WIDTH, height: int = MEDIA_HEIGHT,
                   fps: int = MEDIA_FPS, duration_s: float = 2.0,
                   ffmpeg_path: str | None = None) -> bytes | None:
    """Encodes a local image, gif, or video to a raw H.264 Annex-B stream
    matching the encoder settings seen in a real captured upload
    (keyint=fps, crf=18, baseline profile, yuv420p) - not proven to be
    strict requirements, just what's confirmed to work. Static images are
    looped for duration_s; gifs and video files keep their own frame
    content/timing, just resampled to fps. Uses vendor/ffmpeg.exe if
    present (see resolve_ffmpeg_path()), else PATH. Returns None (logging
    the reason) on any failure rather than raising - encoding failures are
    an expected, recoverable case for a media-upload UI, not a bug."""
    if ffmpeg_path is None:
        ffmpeg_path = resolve_ffmpeg_path()
    already_has_motion = _has_own_motion(local_path)
    cmd = [ffmpeg_path, "-y"]
    if not already_has_motion:
        cmd += ["-loop", "1", "-t", str(duration_s)]
    cmd += [
        "-i", local_path,
        "-vf", (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"),
        "-r", str(fps),
        "-c:v", "libx264", "-profile:v", "baseline", "-pix_fmt", "yuv420p",
        "-x264-params", f"keyint={fps}:crf=18",
        "-f", "h264", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=60)
    except FileNotFoundError:
        log.error("ffmpeg not found (checked vendor/ffmpeg.exe and PATH) - "
                  "run tools/setup_vendor.py or install ffmpeg manually")
        return None
    except subprocess.TimeoutExpired:
        log.error("ffmpeg timed out encoding %s", local_path)
        return None
    if result.returncode != 0:
        log.error("ffmpeg failed encoding %s: %s", local_path,
                  result.stderr.decode(errors="replace")[-800:])
        return None
    return result.stdout


def generate_thumbnail(local_path: str, out_path: str, size: int = 240,
                        ffmpeg_path: str | None = None) -> bool:
    """A small JPEG preview for the Display page's gallery. One frame is
    enough for a gallery tile regardless of source type - ffmpeg grabs it
    the same way for an image, a gif, or a video (-vframes 1 just means
    "stop after the first output frame"), so this doesn't need to branch
    on file type the way encode_to_h264 does for looping a still image.
    Returns False (logging why) rather than raising - a missing thumbnail
    shouldn't block the upload it belongs to."""
    if ffmpeg_path is None:
        ffmpeg_path = resolve_ffmpeg_path()
    cmd = [
        ffmpeg_path, "-y", "-i", local_path,
        "-vf", f"scale={size}:-1", "-vframes", "1", "-q:v", "4",
        out_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=20)
    except FileNotFoundError:
        log.error("ffmpeg not found - cannot generate thumbnail for %s", local_path)
        return False
    except subprocess.TimeoutExpired:
        log.error("ffmpeg timed out generating thumbnail for %s", local_path)
        return False
    if result.returncode != 0:
        log.error("ffmpeg failed generating thumbnail for %s: %s", local_path,
                  result.stderr.decode(errors="replace")[-400:])
        return False
    return True


def default_media_name(local_path: str) -> str:
    """Matches the naming pattern seen in real KANALI uploads: a
    millisecond-precision timestamp, a middle extension, then the
    confirmed .h264_WxH suffix. The middle extension is '.png' for a
    still image regardless of its real format (confirmed against a real
    .jpg upload that still ended up named '....png.h264_WxH'), but the
    real source extension for anything with its own motion (confirmed
    against a real KANALI .mp4 upload, which kept '.mp4' rather than
    being relabelled) - this corrects an earlier, too-narrow reading of
    that single .jpg sample as "the middle extension is just a generic
    label"; it turns out that's only true for still images."""
    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")[:-3]
    suffix = Path(local_path).suffix if _has_own_motion(local_path) else ".png"
    return f"{ts}{suffix}{MEDIA_SUFFIX}"


# -- shared command layer: identical over every transport -------------------
#
# The protocol above doesn't care how the bytes get to the device - only
# _write()/_read_message()/connect()/disconnect() differ between
# transports; every actual command (handshake, ping, screen config) is
# transport-agnostic and lives here once. Responses are read message by
# message (one _read_message() call = one complete protobuf reply), not
# accumulated byte-by-byte looking for a framing marker - confirmed against
# real hardware that each read already returns exactly one complete reply.

class _PanoramaBase:
    def __init__(self, timeout: float = 1.0):
        self.timeout = timeout
        self.last_media: str = DEFAULT_STANDBY_MEDIA
        self.last_brightness: int = 100

    @property
    def connected(self) -> bool:
        raise NotImplementedError

    def connect(self) -> bool:
        raise NotImplementedError

    def disconnect(self):
        raise NotImplementedError

    def _write(self, frame: bytes):
        raise NotImplementedError

    def _read_message(self, timeout_s: float) -> bytes:
        """One read attempt; returns whatever came back (already a
        complete protobuf message) or b'' on timeout/nothing."""
        raise NotImplementedError

    def read_message(self, timeout_s: float = 2.0) -> list | None:
        raw = self._read_message(timeout_s)
        if not raw:
            return None
        return decode_pb(raw)

    def read_messages(self, count: int, timeout_s: float = 2.0) -> list:
        out = []
        for _ in range(count):
            entries = self.read_message(timeout_s)
            if entries is None:
                break
            out.append(entries)
        return out

    def _drain_stray_reply(self, timeout_s: float = 0.3):
        """One short, low-timeout read that discards whatever comes back -
        called right before starting a multi-step exchange (currently
        just upload_media's announce/data/finalize sequence below).

        Confirmed necessary against two real uploads: the keepalive
        loop's periodic ping (main.py's _keepalive_loop) can have its own
        write already in flight when a slower operation starts, and if
        the device's "Hey!" reply for that ping arrives a moment late -
        after our own ping() call already gave up waiting for it - it
        sits there for whatever the NEXT read happens to be, regardless
        of which logical step issued it. Confirmed directly: two separate
        real upload attempts each showed the ping's literal "Hey!" reply
        content displacing a different one of the three real acks
        (announce ack the first time, data ack the second) - not a fixed
        position, exactly what a stray leftover reply landing wherever
        the timing happens to put it would look like. A short timeout
        here is deliberate - if nothing stray is pending, this should
        return almost immediately empty-handed."""
        raw = self._read_message(timeout_s)
        if raw:
            log.info("upload_media: drained a stray reply before starting: %r (raw=%s)",
                     decode_pb(raw), raw.hex())

    def handshake(self, timeout_s: float = 4.0) -> dict | None:
        """timeout_s bumped from 2.0: confirmed against a real run that
        product/model/firmware/serial (from f500) came back fine while
        resolution (f502) stayed empty - f502 is one of three replies
        read_messages() tries to collect, and it breaks off the read
        attempt entirely the moment any single one times out. That fits a
        real device that's simply slower to send this specific reply than
        the 2s window budgeted per message - this is now called once per
        connection (see main.py's get_panorama), not once per request, so
        there's no UX cost to being more patient here."""
        if not self.connected:
            raise PanoramaError("not connected")
        for frame in build_registration_burst():
            self._write(frame)
        replies = self.read_messages(3, timeout_s=timeout_s)
        info = None
        resolution = None
        for entries in replies:
            device_info = pb_get(entries, 500)
            if device_info is not None:
                info = device_info
            res = pb_get(entries, 502)
            if res is not None:
                resolution = res
        if info is None:
            return None
        result = {
            "os": pb_get(info, 1),
            "kernel": pb_get(info, 2),
            "firmware": pb_get(info, 3),
            "product": pb_get(info, 4),
            "app_version": pb_get(info, 5),
            "model": pb_get(info, 8),
            "serial": pb_get(info, 10),
        }
        if resolution is not None:
            res_field = pb_get(resolution, 10)
            if res_field is not None:
                result["width"] = pb_get(res_field, 1)
                result["height"] = pb_get(res_field, 2)
        # f502 still comes back empty on some real units even at this
        # bumped timeout (see this method's docstring), and pb_get returns
        # an explicit None rather than omitting the key when that happens -
        # setdefault alone wouldn't catch that. Rather than show "? x ?"
        # for a value that's never actually in doubt, fall back to
        # MEDIA_WIDTH/MEDIA_HEIGHT: every upload already gets encoded to
        # exactly that resolution (encode_to_h264's own defaults), so it's
        # not a guess, it's what this code already assumes elsewhere.
        result["width"] = result.get("width") or MEDIA_WIDTH
        result["height"] = result.get("height") or MEDIA_HEIGHT
        return result

    def ping(self, timeout_s: float = 2.0) -> bool:
        if not self.connected:
            raise PanoramaError("not connected")
        self._write(build_ping_frame())
        entries = self.read_message(timeout_s=timeout_s)
        return entries is not None

    def list_media(self, timeout_s: float = 2.0) -> list:
        """Media already on the device, per its own listing response -
        parsed generically since the exact per-entry field layout beyond
        f1 (path)/f2 (suffix)/f3 (size bytes) is only loosely confirmed."""
        if not self.connected:
            raise PanoramaError("not connected")
        self._write(build_media_list_frame())
        entries = self.read_message(timeout_s=timeout_s)
        if entries is None:
            return []
        listing = pb_get(entries, 503)
        if listing is None:
            return []
        items = []
        for field_no, kind, val in listing:
            if kind != "message":
                continue
            items.append({
                "path": pb_get(val, 1),
                "suffix": pb_get(val, 2),
                "size": pb_get(val, 3),
            })
        return items

    def set_screen_config(self, active_media: str, brightness: int | None = None,
                          poweron_media: str = DEFAULT_POWERON_MEDIA,
                          standby_media: str = DEFAULT_STANDBY_MEDIA,
                          timeout_s: float = 2.0) -> bool:
        if not self.connected:
            raise PanoramaError("not connected")
        if brightness is None:
            brightness = self.last_brightness
        self._write(build_screen_config_frame(active_media, brightness, poweron_media, standby_media))
        ok = self.read_message(timeout_s=timeout_s) is not None
        self._write(build_commit_frame())
        # Confirmed against real captured traffic: the device replies to
        # BOTH the main config frame and this commit follow-up (two "ok"
        # acks in a row). Not reading this second reply leaves it sitting
        # unconsumed in the pipe, which desyncs every subsequent command's
        # read against the wrong reply - exactly the failure mode seen
        # against real hardware (first command in a sweep worked, every
        # one after it didn't).
        self.read_message(timeout_s=timeout_s)
        self.last_media = active_media
        self.last_brightness = max(0, min(100, int(brightness)))
        return ok

    def set_brightness(self, value: int, timeout_s: float = 2.0) -> bool:
        """Resends the full screen config with the last-known active media
        unchanged - there's no confirmed way to set brightness alone; every
        real KANALI action resent the whole config, this does the same."""
        return self.set_screen_config(self.last_media, value, timeout_s=timeout_s)

    def set_media(self, filename: str, timeout_s: float = 2.0) -> bool:
        """Switches to a file already on the device (see list_media()) -
        filename must already include the confirmed suffix (.h264_WxH),
        e.g. one of the built-in defaults, or a name returned by
        upload_media() below."""
        return self.set_screen_config(filename, self.last_brightness, timeout_s=timeout_s)

    def set_boot_media(self, filename: str, timeout_s: float = 2.0) -> bool:
        """Sets what shows at power-on before any host software connects -
        the f200.f1 slot (poweron_media), independent of the currently
        active screen (set_media() above uses f200.f3 instead). Every real
        captured screen-config command included this as its own stable
        field, separate from "what's showing right now" - unverified
        whether it actually persists correctly across a real power cycle
        with the host fully disconnected, since that's not something a
        live USB capture can observe; worth confirming by physically
        power-cycling the PC (or the AIO's own connector) after calling
        this and checking what the panel shows before any software runs.
        """
        return self.set_screen_config(self.last_media, self.last_brightness,
                                      poweron_media=filename, timeout_s=timeout_s)

    def upload_media(self, local_path: str, remote_name: str | None = None,
                     duration_s: float = 2.0, timeout_s: float = 15.0) -> str | None:
        """Encodes local_path (image or gif) and uploads it directly over
        this same connection - the confirmed real mechanism (see
        build_media_announce_frame() and friends above), not ADB. Requires
        ffmpeg on PATH. Returns the filename to pass to set_media()/
        set_screen_config() on success, or None (logged) on failure -
        anywhere in encode/announce/send/finalize.

        Does not itself call set_screen_config() - apply the upload with a
        separate set_media(name) call once this returns, mirroring how the
        real protocol keeps "store a file" and "show a file" as separate
        steps."""
        if not self.connected:
            raise PanoramaError("not connected")
        if remote_name is None:
            remote_name = default_media_name(local_path)
        h264 = encode_to_h264(local_path, MEDIA_WIDTH, MEDIA_HEIGHT, MEDIA_FPS, duration_s)
        if h264 is None:
            return None
        # content_type/frame_count used to be hardcoded (2, 1) - see
        # build_media_container's and count_h264_frames' docstrings for
        # the real capture evidence that this was wrong for anything with
        # more than one real frame, and the most likely reason a genuine
        # video previously only ever showed its first frame on the panel.
        frame_count = count_h264_frames(h264)
        content_type = 4 if _has_own_motion(local_path) else 2
        container = build_media_container(MEDIA_WIDTH, MEDIA_HEIGHT, MEDIA_FPS, h264,
                                          frame_count=frame_count, content_type=content_type)
        session_id = random.randint(1, 999_999)
        log.info("upload_media: %s, container=%d bytes (h264=%d bytes, frames=%d, content_type=%d)",
                 remote_name, len(container), len(h264), frame_count, content_type)

        self._drain_stray_reply()

        self._write(build_media_announce_frame(session_id, remote_name, len(container)))
        raw = self._read_message(timeout_s)
        if not raw:
            log.warning("upload_media: no response to announce frame")
            return None
        log.info("upload_media: announce ack = %r (raw=%s)", decode_pb(raw), raw.hex())

        self._write(build_media_data_frame(session_id, container))
        raw = self._read_message(timeout_s)
        if not raw:
            log.warning("upload_media: no response to data frame")
            return None
        log.info("upload_media: data ack = %r (raw=%s)", decode_pb(raw), raw.hex())

        self._write(build_media_finalize_frame(session_id))
        raw = self._read_message(timeout_s)
        if not raw:
            log.warning("upload_media: no response to finalize frame")
            return None
        log.info("upload_media: finalize ack = %r (raw=%s)", decode_pb(raw), raw.hex())

        return remote_name


# -- raw USB visibility check (WinUSB, no COM port) -------------------------

TRYX_VENDOR_ID = 0x391A
PANORAMA_PRODUCT_ID = 0x1011


def usb_probe() -> dict:
    """Look for the panel at the raw USB bus level via pyusb/libusb - the one
    check that works no matter what's bound to the device: no driver, the
    WinUSB driver, KANALI running or not, COM port or not. This is what
    Device Manager's Ports list can't show, and it's the only way left to
    tell a real hardware/cabling problem apart from "something's just
    holding the device open".

    Never raises for the ordinary failure cases (nothing plugged in,
    something else has it open) - those are exactly the cases this exists
    to tell apart, and each comes back as reason/likely_cause text instead
    of an exception. Only genuinely unexpected failures propagate.
    """
    try:
        import usb.core
        import usb.util
    except ImportError:
        return {
            "ok": False,
            "reason": "pyusb not installed (pip install pyusb libusb-package)",
        }

    backend = None
    try:
        import libusb_package
        backend = libusb_package.get_libusb1_backend()
    except Exception as exc:
        log.debug("libusb_package backend unavailable, falling back to pyusb's default search: %s", exc)

    try:
        find_kwargs = {"find_all": True, "idVendor": TRYX_VENDOR_ID}
        if backend is not None:
            find_kwargs["backend"] = backend
        devices = list(usb.core.find(**find_kwargs))
    except usb.core.NoBackendError:
        return {
            "ok": False,
            "reason": "no libusb backend found (pip install libusb-package, or "
                      "put libusb-1.0.dll next to python.exe)",
        }

    if not devices:
        return {
            "ok": False,
            "reason": (
                f"No USB device with vendor id 0x{TRYX_VENDOR_ID:04x} (Tryx) is visible "
                "on the bus at all. This is below any driver or software - Windows isn't "
                "seeing the device electrically. Points at the physical connection: the "
                "panel's internal USB 2.0 header cable to the motherboard (reseat it, try "
                "a different header), or its power connector, not KANALI/drivers/software."
            ),
            "devices": [],
        }

    result = {"ok": True, "devices": []}
    for dev in devices:
        info = {
            "vendor_id": f"0x{dev.idVendor:04x}",
            "product_id": f"0x{dev.idProduct:04x}",
            "is_panorama": dev.idProduct == PANORAMA_PRODUCT_ID,
            "bus": dev.bus,
            "address": dev.address,
        }
        try:
            info["manufacturer"] = usb.util.get_string(dev, dev.iManufacturer) if dev.iManufacturer else None
            info["product"] = usb.util.get_string(dev, dev.iProduct) if dev.iProduct else None
        except Exception as exc:
            info["manufacturer"] = None
            info["product"] = None
            info["string_read_error"] = str(exc)
        try:
            cfg = dev.get_active_configuration()
            info["accessible"] = True
            info["num_interfaces"] = cfg.bNumInterfaces
            info["endpoints"] = [
                f"0x{ep.bEndpointAddress:02x}" for intf in cfg for ep in intf
            ]
        except Exception as exc:
            # Broad except deliberately: libusb's Windows backend raises
            # NotImplementedError (not usb.core.USBError) when the bound
            # driver isn't one it can open (needs WinUSB/libusb0/libusbK
            # specifically), which is the expected failure mode here, not
            # a bug - it means the device enumerated but nothing suitable
            # is bound to talk to it.
            info["accessible"] = False
            info["access_error"] = f"{type(exc).__name__}: {exc}"
            lowered = str(exc).lower()
            if "access" in lowered or "denied" in lowered or "busy" in lowered:
                info["likely_cause"] = (
                    "Another process already has it open exclusively - most likely KANALI's "
                    "UDB.exe (even a stuck/background instance after 'closing' the app). Check "
                    "Task Manager and end it, then retry."
                )
            elif "not supported" in lowered or "not implemented" in lowered:
                info["likely_cause"] = (
                    "Device enumerates (Windows sees the VID/PID) but no WinUSB/libusb0/libusbK "
                    "compatible driver is currently bound to this interface, so libusb can't open "
                    "it - not a cabling problem. In Device Manager, find this device (search "
                    "hardware ID VID_391A&PID_1011 under View > Devices by container, or check "
                    "'Other devices'/'Universal Serial Bus devices'), then either: (a) right-click "
                    "> Uninstall device (tick 'delete the driver'), unplug/replug or reboot, and "
                    "let Windows/KANALI reinstall it fresh, or (b) use Zadig (zadig.akeo.ie) to "
                    "force-bind WinUSB to this specific device so this tool can open it directly "
                    "without KANALI at all. Worth trying (a) first since KANALI's own logs show "
                    "it failing the same way - the current driver binding may just be stuck."
                )
            else:
                info["likely_cause"] = (
                    "Device is visible but its configuration couldn't be read - may need the "
                    "WinUSB driver (re)bound via Zadig, or running this as Administrator."
                )
        result["devices"].append(info)
    return result


# -- raw USB (WinUSB bulk) transport -----------------------------------------
#
# The confirmed-correct transport for this panel on Windows. Requires WinUSB
# actually bound to the device's interface first (Windows binds its generic
# usbprint.sys class driver by default on this hardware, per this session's
# Device Manager check - use Zadig, https://zadig.akeo.ie, "List All
# Devices", target driver WinUSB, Replace Driver). usb_probe() reports which
# state a given machine is in.
#
# Endpoints confirmed against real hardware this session: one bulk OUT
# (0x01) and one bulk IN (0x81) on the panel's single interface - the same
# two-endpoint shape a CDC-ACM data interface would have, just addressed
# directly instead of through a COM port. The frame protocol riding on top
# is unverified against this specific panel until a real handshake succeeds.

class PanoramaUSB(_PanoramaBase):
    def __init__(self, timeout: float = 1.0):
        super().__init__(timeout)
        self.dev = None
        self.ep_out = None
        self.ep_in = None

    def connect(self) -> bool:
        import usb.core
        import usb.util

        backend = None
        try:
            import libusb_package
            backend = libusb_package.get_libusb1_backend()
        except Exception as exc:
            log.debug("libusb_package backend unavailable, falling back to pyusb's default search: %s", exc)

        find_kwargs = {"idVendor": TRYX_VENDOR_ID, "idProduct": PANORAMA_PRODUCT_ID}
        if backend is not None:
            find_kwargs["backend"] = backend
        dev = usb.core.find(**find_kwargs)
        if dev is None:
            log.warning("panel not found on the USB bus (VID 0x391A:0x1011) - "
                        "see usb_probe() for what's actually visible")
            return False

        try:
            dev.set_configuration()
        except usb.core.USBError as exc:
            log.debug("set_configuration (often harmless if already configured): %s", exc)

        cfg = dev.get_active_configuration()
        intf = cfg[(0, 0)]
        ep_out = usb.util.find_descriptor(
            intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
        ep_in = usb.util.find_descriptor(
            intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
        if ep_out is None or ep_in is None:
            log.warning("panel found on the bus but its interface has no bulk IN/OUT pair")
            return False

        self.dev = dev
        self.ep_out = ep_out
        self.ep_in = ep_in
        self._try_cdc_line_coding(intf.bInterfaceNumber)
        return True

    def _try_cdc_line_coding(self, interface_number: int):
        """Sends the two standard USB CDC control requests a real serial
        driver issues before any data flows - SET_LINE_CODING (115200 8N1)
        and SET_CONTROL_LINE_STATE asserting DTR+RTS.

        Reasoning, not a guess pulled from nowhere: reed-tpse's Linux build
        opens /dev/ttyACM0 through the OS's normal serial API, which sends
        exactly these two control requests automatically as a side effect
        of "opening the port" - before that happens, many USB-UART bridge
        firmwares simply refuse to send anything on the data pipe. Our raw
        WinUSB path skips that entirely by going straight to bulk transfers,
        which is one explanation (not yet confirmed) for the panel staying
        completely silent to both real requests and passive listening this
        session. Harmless if the device doesn't care: failure here is
        logged and swallowed, connect() proceeds regardless.
        """
        import usb.core
        line_coding = (115200).to_bytes(4, "little") + bytes((0, 0, 8))  # 1 stop bit, no parity, 8 data bits
        try:
            # bmRequestType 0x21 = host-to-device, class, interface
            self.dev.ctrl_transfer(0x21, 0x20, 0, interface_number, line_coding)  # SET_LINE_CODING
            self.dev.ctrl_transfer(0x21, 0x22, 0x0003, interface_number, None)     # SET_CONTROL_LINE_STATE, DTR+RTS
            log.debug("CDC line-coding control requests accepted")
        except usb.core.USBError as exc:
            log.debug("CDC line-coding control requests rejected/unsupported (may be harmless): %s", exc)

    def disconnect(self):
        if self.dev is not None:
            import usb.util
            usb.util.dispose_resources(self.dev)
        self.dev = None
        self.ep_out = None
        self.ep_in = None

    @property
    def connected(self) -> bool:
        return self.dev is not None

    def _write(self, frame: bytes):
        self.ep_out.write(frame, timeout=max(int(self.timeout * 1000), 1000))

    def _read_message(self, timeout_s: float) -> bytes:
        """One bulk-IN read. Confirmed against real captured traffic that a
        single USB bulk transfer already carries exactly one complete
        protobuf reply - no framing marker needed to know where it ends."""
        import usb.core
        # Confirmed real replies as large as ~10.7KB in captured traffic
        # (media metadata) - the buffer must cover the whole message, not
        # just one wire-level max-packet-size chunk.
        read_size = 32768
        try:
            chunk = self.ep_in.read(read_size, timeout=max(int(timeout_s * 1000), 1))
        except usb.core.USBError as exc:
            if "timeout" in str(exc).lower() or "timed out" in str(exc).lower():
                return b""
            raise
        return bytes(chunk) if chunk else b""


def find_usb_panel(timeout: float = 1.0) -> "PanoramaUSB | None":
    """Connect to the panel over WinUSB and return it ready to use, or None.

    Fallback/diagnostic path only - confirmed this session that KANALI's
    own installer sets up usbprint.sys for this device, not WinUSB, so
    using this requires replacing that driver via Zadig, which breaks
    KANALI's own access. Prefer find_print_panel() below, which uses the
    vendor-intended driver and doesn't require touching anything in Device
    Manager. Does not itself distinguish *why* it failed (nothing on the
    bus vs. wrong driver still bound) - call usb_probe() for that."""
    panel = PanoramaUSB(timeout=timeout)
    if not panel.connect():
        return None
    return panel


# -- usbprint.sys transport (CreateFile/ReadFile/WriteFile) -----------------
#
# The vendor-intended transport on Windows, confirmed this session: KANALI's
# own installer sets up Windows' built-in USB Printing Support class driver
# (usbprint.sys) for this device, not WinUSB - a known technique some
# vendors use to get raw bulk I/O without needing a signed custom driver,
# since usbprint.sys is inbox and exposes a plain CreateFile/ReadFile/
# WriteFile interface. libusb/pyusb (PanoramaUSB above) cannot open a
# device in this state at all - hence this separate, lower-level transport
# using ctypes directly against kernel32/setupapi.
#
# Unverified end to end against this panel - the device-path discovery
# (SetupAPI GUID_DEVINTERFACE_USBPRINT enumeration) follows the standard,
# widely-documented Windows pattern, but has not been run for real; treat
# this the same as everything else ported from documentation rather than
# a live test in this codebase.

import ctypes
from ctypes import wintypes

GUID_DEVINTERFACE_USBPRINT = "{28D78FAD-5A12-11D1-AE5B-0000F803A8C2}"

_DIGCF_PRESENT = 0x02
_DIGCF_DEVICEINTERFACE = 0x10
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x1
_FILE_SHARE_WRITE = 0x2
_OPEN_EXISTING = 3
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid_from_string(text: str) -> _GUID:
    """'{28D78FAD-5A12-11D1-AE5B-0000F803A8C2}' -> _GUID struct."""
    text = text.strip("{}")
    data1, data2, data3, data4a, data4b = text.split("-")
    data4_bytes = bytes.fromhex(data4a) + bytes.fromhex(data4b)
    return _GUID(int(data1, 16), int(data2, 16), int(data3, 16),
                 (ctypes.c_ubyte * 8)(*data4_bytes))


class _SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("InterfaceClassGuid", _GUID),
        ("Flags", wintypes.DWORD),
        ("Reserved", ctypes.POINTER(ctypes.c_ulong)),
    ]


def _setupapi_dll():
    """Loads setupapi with explicit argtypes/restype on every function used
    here. Not optional: without this, ctypes defaults HDEVINFO's return
    type to a 32-bit int, silently truncating the real (pointer-sized,
    64-bit) handle - which doesn't error, it just corrupts the handle so
    every subsequent call quietly fails, seen exactly this way against
    real hardware this session (Device Manager confirmed the device was
    genuinely present and bound to usbprint.sys, but find_usbprint_path()
    still came back empty until this was added)."""
    setupapi = ctypes.WinDLL("setupapi")

    setupapi.SetupDiGetClassDevsW.argtypes = [
        ctypes.POINTER(_GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
    setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE

    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(_GUID),
        wintypes.DWORD, ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA)]
    setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL

    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA),
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL

    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
    setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL

    return setupapi


def find_usbprint_path(vendor_id: int = TRYX_VENDOR_ID,
                       product_id: int = PANORAMA_PRODUCT_ID) -> str | None:
    """Find the Windows device path for a usbprint.sys-bound device with the
    given VID/PID, via the standard SetupAPI device-interface enumeration
    (SetupDiGetClassDevs + SetupDiEnumDeviceInterfaces +
    SetupDiGetDeviceInterfaceDetail against GUID_DEVINTERFACE_USBPRINT) -
    the same mechanism Windows itself uses to hand this path to any
    application, including presumably KANALI. Returns None if nothing
    matching is currently present (wrong driver bound, or not plugged in)."""
    setupapi = _setupapi_dll()
    guid = _guid_from_string(GUID_DEVINTERFACE_USBPRINT)

    devinfo = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(guid), None, None, _DIGCF_PRESENT | _DIGCF_DEVICEINTERFACE)
    if devinfo is None or devinfo == _INVALID_HANDLE_VALUE:
        log.warning("SetupDiGetClassDevsW failed (gle=%d)", ctypes.get_last_error())
        return None

    match_fragment = f"vid_{vendor_id:04x}&pid_{product_id:04x}".lower()
    found_path = None
    try:
        index = 0
        while True:
            did = _SP_DEVICE_INTERFACE_DATA()
            did.cbSize = ctypes.sizeof(did)
            if not setupapi.SetupDiEnumDeviceInterfaces(
                    devinfo, None, ctypes.byref(guid), index, ctypes.byref(did)):
                break  # ERROR_NO_MORE_ITEMS - done

            required = wintypes.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                devinfo, ctypes.byref(did), None, 0, ctypes.byref(required), None)
            buf = ctypes.create_string_buffer(required.value)
            # SP_DEVICE_INTERFACE_DETAIL_DATA_W: DWORD cbSize followed by a
            # WCHAR[] DevicePath. cbSize itself must be set to the size of
            # just the struct's fixed header (DWORD + one WCHAR) - 6 bytes
            # logically, but a long-documented Windows quirk requires 8 on
            # 64-bit and 5 (well, 6) on 32-bit for this call to succeed
            # regardless of the buffer's real total size. Hardcoding 8 since
            # this project only targets 64-bit Windows.
            ctypes.memmove(buf, ctypes.byref(wintypes.DWORD(8)), ctypes.sizeof(wintypes.DWORD))
            ok = setupapi.SetupDiGetDeviceInterfaceDetailW(
                devinfo, ctypes.byref(did), buf, required, None, None)
            if ok:
                path = ctypes.wstring_at(ctypes.addressof(buf) + ctypes.sizeof(wintypes.DWORD))
                if match_fragment in path.lower():
                    found_path = path
                    break
            index += 1
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(devinfo)

    return found_path


_FILE_FLAG_OVERLAPPED = 0x40000000
_ERROR_IO_PENDING = 997
_WAIT_OBJECT_0 = 0x0
_WAIT_TIMEOUT = 0x102


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


class PanoramaPrint(_PanoramaBase):
    """Talks to the panel via Windows' native CreateFile/ReadFile/WriteFile
    on its usbprint.sys device path - the same access model KANALI itself
    uses, so this does not require touching any driver in Device Manager
    (unlike PanoramaUSB), but does still conflict with KANALI if it's
    running at the same time, same as any single-open-handle device.

    Uses overlapped (asynchronous) I/O throughout, not plain synchronous
    ReadFile/WriteFile - a plain synchronous ReadFile on a raw device
    handle like this has no per-call timeout at all (that's a COM-port-only
    concept, via SetCommTimeouts, which doesn't apply here) and simply
    blocks forever if the device never answers. Confirmed hanging exactly
    this way against real hardware this session. Overlapped I/O plus
    WaitForSingleObject on the operation's event handle is the standard
    way to get a real, cancelable timeout on an arbitrary Windows handle.
    """

    def __init__(self, timeout: float = 1.0):
        super().__init__(timeout)
        self._handle = None
        self._event = None
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # Same reasoning as _setupapi_dll(): CreateFileW/CloseHandle return
        # or take a 64-bit HANDLE, which ctypes truncates to 32 bits without
        # an explicit restype/argtypes - must be declared, not left implicit.
        self._kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self._kernel32.CreateFileW.restype = wintypes.HANDLE
        self._kernel32.ReadFile.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_OVERLAPPED)]
        self._kernel32.ReadFile.restype = wintypes.BOOL
        self._kernel32.WriteFile.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_OVERLAPPED)]
        self._kernel32.WriteFile.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.CreateEventW.argtypes = [
            ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        self._kernel32.CreateEventW.restype = wintypes.HANDLE
        self._kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
        self._kernel32.ResetEvent.restype = wintypes.BOOL
        self._kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self._kernel32.GetOverlappedResult.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_OVERLAPPED),
            ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
        self._kernel32.GetOverlappedResult.restype = wintypes.BOOL
        self._kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(_OVERLAPPED)]
        self._kernel32.CancelIoEx.restype = wintypes.BOOL

    def connect(self) -> bool:
        path = find_usbprint_path()
        if path is None:
            log.warning("no usbprint.sys device found for VID 0x391A:0x1011 - "
                        "wrong driver bound, or not plugged in")
            return False
        handle = self._kernel32.CreateFileW(
            path, _GENERIC_READ | _GENERIC_WRITE,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE, None, _OPEN_EXISTING,
            _FILE_FLAG_OVERLAPPED, None)
        if handle is None or handle == _INVALID_HANDLE_VALUE:
            log.warning("CreateFileW failed on %s (gle=%d) - probably still open "
                        "elsewhere (KANALI's UDB.exe?)", path, ctypes.get_last_error())
            return False
        event = self._kernel32.CreateEventW(None, True, False, None)  # manual-reset, initially unsignaled
        if event is None:
            log.warning("CreateEventW failed (gle=%d)", ctypes.get_last_error())
            self._kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        self._event = event
        return True

    def disconnect(self):
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None
        if self._event is not None:
            self._kernel32.CloseHandle(self._event)
            self._event = None

    @property
    def connected(self) -> bool:
        return self._handle is not None

    def _overlapped_call(self, io_func, buf, length: int, timeout_s: float) -> int:
        """Issues one overlapped ReadFile/WriteFile call and waits for it to
        complete or time out, returning the number of bytes transferred (0
        on timeout). Cancels the operation itself on timeout, rather than
        leaving it dangling on the handle."""
        self._kernel32.ResetEvent(self._event)
        ov = _OVERLAPPED()
        ov.hEvent = self._event
        ok = io_func(self._handle, buf, length, None, ctypes.byref(ov))
        if not ok:
            err = ctypes.get_last_error()
            if err != _ERROR_IO_PENDING:
                raise PanoramaError(f"{io_func.__name__} failed (gle={err})")
            wait_result = self._kernel32.WaitForSingleObject(
                self._event, max(int(timeout_s * 1000), 1))
            if wait_result == _WAIT_TIMEOUT:
                self._kernel32.CancelIoEx(self._handle, ctypes.byref(ov))
                # Still call GetOverlappedResult after cancelling - lets the
                # cancellation actually complete and reports any bytes that
                # made it through before the cancel took effect.
                transferred = wintypes.DWORD(0)
                self._kernel32.GetOverlappedResult(
                    self._handle, ctypes.byref(ov), ctypes.byref(transferred), True)
                return transferred.value
            if wait_result != _WAIT_OBJECT_0:
                raise PanoramaError(f"WaitForSingleObject returned {wait_result} (gle={ctypes.get_last_error()})")
        transferred = wintypes.DWORD(0)
        self._kernel32.GetOverlappedResult(
            self._handle, ctypes.byref(ov), ctypes.byref(transferred), True)
        return transferred.value

    def _write(self, frame: bytes):
        buf = ctypes.create_string_buffer(frame, len(frame))
        transferred = self._overlapped_call(self._kernel32.WriteFile, buf, len(frame), self.timeout)
        if transferred != len(frame):
            raise PanoramaError(f"WriteFile only sent {transferred}/{len(frame)} bytes")

    def _read_message(self, timeout_s: float) -> bytes:
        """One ReadFile. Confirmed against real captured traffic that a
        single read already carries exactly one complete protobuf reply -
        no framing marker needed to know where it ends. Buffer sized well
        above the largest real reply seen (~10.7KB, media metadata)."""
        chunk = ctypes.create_string_buffer(32768)
        transferred = self._overlapped_call(self._kernel32.ReadFile, chunk, 32768, timeout_s)
        return chunk.raw[:transferred] if transferred else b""


def find_print_panel(timeout: float = 1.0) -> "PanoramaPrint | None":
    """Connect to the panel via its usbprint.sys device path and return it
    ready to use, or None - the transport to actually use on Windows, since
    it matches what KANALI's own installer sets up (see module docstring).
    """
    panel = PanoramaPrint(timeout=timeout)
    if not panel.connect():
        return None
    return panel


# -- media transfer over ADB -----------------------------------------------

class Media:
    """Wraps adb.exe - subprocess only, same pattern as hub.py's liquidctl
    wrapper. Requires adb on PATH and the panel already authorised as an ADB
    device (Windows may need Google's ADB driver if it doesn't enumerate on
    its own - unverified for this specific panel)."""

    def __init__(self, adb_path: str = "adb"):
        self.adb_path = adb_path

    def _run(self, *args: str) -> str | None:
        try:
            result = subprocess.run([self.adb_path, *args], capture_output=True,
                                     text=True, timeout=30)
        except FileNotFoundError:
            log.error("adb not found on PATH")
            return None
        except subprocess.TimeoutExpired:
            log.error("adb timed out: %s", " ".join(args))
            return None
        if result.returncode != 0:
            # Same fix as hub.py's Hub._run(): a non-zero exit used to
            # still return stdout, so a caller checking "is None" for
            # failure (push()/delete() below) could never actually catch
            # a real adb error.
            log.warning("adb failed (%s): %s", " ".join(args), result.stderr.strip())
            return None
        return result.stdout

    def is_connected(self) -> bool:
        out = self._run("devices")
        if out is None:
            return False
        return any("\tdevice" in line for line in out.splitlines())

    def push(self, local_path: str, remote_name: str) -> bool:
        out = self._run("push", local_path, MEDIA_PATH + remote_name)
        return out is not None and ("pushed" in out or "1 file" in out)

    def list_media(self) -> list[str]:
        out = self._run("shell", "ls", "-1", MEDIA_PATH)
        if out is None or "No such file" in out or "error:" in out:
            return []
        return [line.strip() for line in out.splitlines() if line.strip()]

    def delete(self, remote_name: str) -> bool:
        out = self._run("shell", "rm", MEDIA_PATH + remote_name)
        return out is not None
