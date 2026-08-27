"""Preset execution, metadata carry-over and the sanity gate.

Every external tool is invoked with :func:`asyncio.create_subprocess_exec` — argv lists,
never a shell string — so a filename can never be interpreted as a command.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import mmap
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .config import BehaviorSettings, Preset

logger = logging.getLogger(__name__)

# EXIF Orientation -> clockwise display rotation. 5-8 swap the edge lengths, 1-4 do not.
_EXIF_ORIENTATION_ROTATION: dict[int, int] = {1: 0, 2: 0, 3: 180, 4: 180, 5: 90, 6: 90, 7: 270, 8: 270}

# Transfer functions that mean "this is HDR". Flattening one of these to SDR without
# tone mapping washes the picture out irreversibly.
_HDR_TRANSFERS: frozenset[str] = frozenset({"smpte2084", "arib-std-b67"})

# XMP tags a Samsung/Google motion photo carries. Their presence alone is not proof that
# the video is still attached, but it is proof that the file was one.
_MOTION_PHOTO_TAGS: tuple[str, ...] = (
    "MotionPhoto",
    "MotionPhotoVersion",
    "MicroVideo",
    "MicroVideoOffset",
    "EmbeddedVideoType",
)

# Metadata groups the post-encode diff compares. `File:` and `Composite:` are excluded on
# purpose: they are derived from the bytes on disk, not carried metadata, and would report
# a difference for every successful re-encode.
_METADATA_GROUPS: tuple[str, ...] = ("-EXIF:all", "-GPS:all", "-XMP:all", "-IPTC:all")

# Tags that differ by design rather than by loss. Keep this list short and justified: every
# entry is a hole in the guarantee.
#
#   EXIF:Orientation  `normalize_orientation` pins the output to 1 after `-auto-orient` has
#                     baked the rotation into the pixels, and writes the tag even when the
#                     source carried none — so both "changed" and "added" are expected.
#   XMP:Orientation   the XMP mirror of that same tag, describing the same rotation of the
#                     same pixels, so the reasoning above applies to it verbatim — it was
#                     simply not listed. Measured on a live instance on 2026-08-26,
#                     'Rotate 270 CW' -> 'Horizontal (normal)' on 2 jobs.
#   XMP:XMPToolkit    the version stamp of whatever last wrote the XMP packet, so exiftool
#                     stamps its own on every copy. It describes the writing tool, not the
#                     picture. Found by running the shipped preset against a real photo:
#                     'Image::ExifTool 12.76' -> 'Image::ExifTool 13.25' would otherwise
#                     fail every source that a different exiftool version had touched.
#   *Offset / *Start  byte positions of the embedded thumbnail and preview inside the file,
#                     not content. Rewriting the EXIF block moves them by definition:
#                     measured 1008 -> 1026 on a phone JPEG through the shipped preset.
#                     The matching *Length tags stay compared — a length that changes is a
#                     thumbnail that was truncated, which is a real loss.
_METADATA_IGNORED: frozenset[str] = frozenset(
    {
        "EXIF:Orientation",
        "XMP:Orientation",
        "XMP:XMPToolkit",
        "EXIF:ThumbnailOffset",
        "EXIF:PreviewImageStart",
        "EXIF:OtherImageStart",
        "EXIF:StripOffsets",
    }
)

# Relative tolerance for the metadata gate's numeric comparison. exiftool prints most
# rationals in a form the re-approximation cannot reach (48 deg 18' 16.32" N, and any
# fraction whose denominator is small), but a tag that prints as a raw decimal — or as a
# fraction over a denominator in the billions — carries the drift into the printed string:
# measured on a live library on 2026-08-24, EXIF:FocalPlaneYResolution moved 6734.006734 ->
# 6734.006711 (~3.4e-9 relative) on 24 of 67 encoded images, EXIF:GPSAltitude '339.569 m' ->
# '339.5690021 m' (~6.2e-9) on another, and on 2026-08-26 EXIF:ShutterSpeedValue
# '1/999963365' -> '1/999963296' (~6.9e-8) on 6 more. 1e-6 clears the largest of those by an
# order of magnitude and is still far below a difference any viewer could be shown: it is a
# change in the 7th significant digit, which no EXIF value is meaningful to.
_METADATA_REL_TOL = 1e-6

# A number, optionally followed by a unit ("339.569 m"). Deliberately strict: digits are
# required, so "inf" and "nan" are not numbers here, and the unit is whatever follows.
_NUMBER_WITH_UNIT = re.compile(r"([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*(.*)")

# A whole value that is one fraction, which is how exiftool prints a rational whose value is
# one: '1/100' for ExposureTime, '1/1000' for ShutterSpeedValue. To _NUMBER_WITH_UNIT above
# that is the number 1 with the unit '/100', so two fractions compared character by character
# and the re-approximation of a large denominator was a finding: measured on a live library
# on 2026-08-26, EXIF:ShutterSpeedValue came back '1/999963365' -> '1/999963296' and failed 6
# jobs in one backfill run.
#
# Evaluating the fraction costs nothing in discrimination. Two *integer* denominators can
# only land inside _METADATA_REL_TOL of each other once the denominator passes a million, so
# every exposure time a camera can write — 1/8000 at the fastest — still has to match
# exactly; the drift measured above is 6.9e-8 of the value.
#
# Deliberately nothing after the denominator, not even a unit: '4/2/2026' would otherwise
# parse as 2 with the unit '/2026' and compare equal to '2/1/2026', two different dates in a
# free-text caption. A fraction with anything appended stays an exact comparison.
_FRACTION = re.compile(r"(?P<numerator>[+-]?\d+)\s*/\s*(?P<denominator>\d+)")


def _as_number(value: object) -> tuple[float, str] | None:
    """``value`` as a (number, unit) pair, or ``None`` if it is not a number at all.

    A value that is one fraction is evaluated, and has no unit; everything else keeps
    whatever follows the digits as its unit, and two values only compare when those agree.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value), ""
    if not isinstance(value, str):
        return None
    text = value.strip()
    fraction = _FRACTION.fullmatch(text)
    if fraction is not None:
        denominator = int(fraction.group("denominator"))
        if denominator == 0:
            return None
        return int(fraction.group("numerator")) / denominator, ""
    match = _NUMBER_WITH_UNIT.fullmatch(text)
    if match is None:
        return None
    return float(match.group(1)), match.group(2).strip()


# A time as exiftool prints it, with the UTC offset split off: '11:24:38',
# '11:24:38+00:00', '11:24:38Z', and the same with the date exiftool writes in front of it,
# '2026:08:25 15:46:30'. Deliberately strict — seconds are required and the date uses
# exiftool's own `YYYY:MM:DD ` form, so a bare '11' or '2026' stays a number rather than
# becoming a time, and any shape not written here simply falls through to the exact
# comparison that has always applied. Only the bare-time form has been measured
# (IPTC:TimeCreated, IPTC:DigitalCreationTime); the date-time form is in because it is the
# same printed value with a date in front of it and the offset hangs off the end of it in
# exactly the same way, and leaving it out would be waiting for the same report twice.
_TIME_WITH_OFFSET = re.compile(
    r"(?P<clock>(?:\d{4}:\d{2}:\d{2} )?\d{2}:\d{2}:\d{2}(?:\.\d+)?)(?P<offset>Z|[+-]\d{2}:\d{2})?"
)


def _as_time(value: object) -> tuple[str, int | None] | None:
    """``value`` as a (clock, UTC offset in minutes) pair, or ``None`` if it is not a time.

    The offset is ``None`` when the value carries none, which is a different thing from an
    offset of zero and is why it is not folded into the number here.
    """
    if not isinstance(value, str):
        return None
    match = _TIME_WITH_OFFSET.fullmatch(value.strip())
    if match is None:
        return None
    clock, offset = match.group("clock"), match.group("offset")
    if offset is None:
        return clock, None
    if offset == "Z":
        return clock, 0
    minutes = int(offset[1:3]) * 60 + int(offset[4:6])
    return clock, -minutes if offset.startswith("-") else minutes


def _times_match(before: object, after: object) -> bool:
    """Whether two times are the same time written with and without a *zero* UTC offset.

    ``False`` unless both sides are times, so nothing else is affected.
    """
    before_time = _as_time(before)
    after_time = _as_time(after)
    if before_time is None or after_time is None:
        return False
    before_clock, before_offset = before_time
    after_clock, after_offset = after_time
    if before_clock != after_clock:
        return False
    # No offset written and an explicit +00:00 both mean the clock is the whole story, so
    # `None or 0` collapses them onto each other. Every other pair still has to agree:
    # '15:46:30' against '15:46:30+01:00' is a real difference, and so is +01:00 against
    # +02:00.
    return (before_offset or 0) == (after_offset or 0)


def _values_match(before: object, after: object) -> bool:
    """Whether two exiftool values mean the same thing.

    Exact for everything that is not a number or a time, so ``Make``, ``Model`` and any free
    text keep comparing character by character, and a differing unit ('339.569 m' against
    '339.569 ft') is a difference like any other. Two numbers with the same unit are
    compared within :data:`_METADATA_REL_TOL`, because a tag whose printed form is a raw
    decimal — or a bare fraction, which :func:`_as_number` evaluates — shows the rational
    re-approximation that :func:`verify_metadata` exists to tolerate everywhere else. Two
    times are compared by :func:`_times_match`, because exiftool writes an explicit zero UTC
    offset onto a time that carried none.

    Times are tried first: to :func:`_as_number` a time is the number in front of it with
    the rest as a unit, so '11:24:38+00:00' and '11:24:38' would be rejected on their units
    before they were ever recognised as the same time.
    """
    if before == after:
        return True
    if _times_match(before, after):
        return True
    before_number = _as_number(before)
    after_number = _as_number(after)
    if before_number is None or after_number is None:
        return False
    before_value, before_unit = before_number
    after_value, after_unit = after_number
    if before_unit != after_unit:
        return False
    return math.isclose(before_value, after_value, rel_tol=_METADATA_REL_TOL, abs_tol=0.0)


def _diff_metadata(before: dict[str, object], after: dict[str, object]) -> list[str]:
    """Every tag of ``before`` that ``after`` lost or changed, in the reported form.

    Split out from the exiftool call in :func:`verify_metadata` so the rules — the ignore
    list and :func:`_values_match` — are testable without exiftool installed.
    """
    differences: list[str] = []
    for key, value in before.items():
        if key in _METADATA_IGNORED:
            continue
        if key not in after:
            differences.append(f"{key} lost")
        elif not _values_match(value, after[key]):
            differences.append(f"{key} changed: {value!r} -> {after[key]!r}")
    return differences


# How many bytes may follow the JPEG's end-of-image marker before we call it a payload.
# Some encoders leave a handful of padding bytes; a motion photo leaves megabytes.
MAX_HARMLESS_TRAILER_BYTES = 4096


class EncodeError(RuntimeError):
    """The encoder command failed or produced nothing usable."""


@dataclass(frozen=True, slots=True)
class MediaProbe:
    """The subset of ``ffprobe`` output the sanity gate reasons about."""

    width: int | None
    height: int | None
    duration_s: float | None
    video_streams: int
    audio_streams: int
    has_date_time_original: bool
    # Clockwise display rotation in degrees: from the container's display matrix for
    # video, from EXIF Orientation for stills. Normalised to 0/90/180/270.
    rotation: int = 0
    pix_fmt: str | None = None
    bit_depth: int | None = None
    color_transfer: str | None = None

    @property
    def has_visual_stream(self) -> bool:
        return self.video_streams > 0

    @property
    def display_size(self) -> tuple[int | None, int | None]:
        """Width/height *as a viewer sees them*, with the rotation applied.

        The stored frame size alone is meaningless: a portrait phone clip is coded
        1920x1080 and carries a 90 degree display matrix. Comparing stored sizes across a
        re-encode therefore rejects every rotated video, because ffmpeg's default
        ``-autorotate`` bakes the rotation into the pixels and drops the matrix.
        """
        if self.rotation in (90, 270):
            return (self.height, self.width)
        return (self.width, self.height)

    @property
    def is_hdr(self) -> bool:
        return (self.color_transfer or "") in _HDR_TRANSFERS


@dataclass(frozen=True, slots=True)
class ExifFacts:
    """What exiftool knows about a still that ffprobe does not."""

    has_date: bool
    orientation: int | None


@dataclass(slots=True)
class SanityResult:
    """Outcome of the sanity gate."""

    ok: bool
    failures: list[str] = field(default_factory=list)

    def reason(self) -> str:
        return "; ".join(self.failures) if self.failures else "ok"


@dataclass(frozen=True, slots=True)
class EncodeResult:
    output_path: Path
    orig_bytes: int
    new_bytes: int
    probe: MediaProbe
    # Base64 SHA-1 of ``output_path``, in the exact shape Immich reports as
    # ``AssetResponseDto.checksum`` — see :func:`file_checksum`.
    checksum: str

    @property
    def ratio(self) -> float:
        return self.new_bytes / self.orig_bytes if self.orig_bytes else 1.0


def file_checksum(path: Path) -> str:
    """Base64-encoded SHA-1 of ``path`` — the form ``GET /assets/{id}`` returns.

    Verified against a live Immich v3.1.0 instance: uploading a file whose SHA-1 digest
    base64-encodes to ``X`` yields an asset with ``"checksum": "X"``. Streamed in chunks
    so a multi-gigabyte video never lands in memory.
    """
    digest = hashlib.sha1()  # noqa: S324 - not a security decision; Immich picked SHA-1
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return base64.b64encode(digest.digest()).decode("ascii")


async def run_command(argv: list[str], *, timeout_s: float) -> tuple[int, str, str]:
    """Run ``argv`` without a shell. Returns ``(returncode, stdout, stderr)``."""
    if not argv:
        raise EncodeError("empty command")
    logger.debug("exec: %s", argv)
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise EncodeError(f"{argv[0]} timed out after {timeout_s:.0f}s") from None
    return (
        process.returncode if process.returncode is not None else -1,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


async def probe(path: Path, *, is_still: bool = False, timeout_s: float = 120.0) -> MediaProbe:
    """Inspect a media file with ``ffprobe``. Raises :class:`EncodeError` if undecodable.

    ``is_still`` additionally consults exiftool, because ffprobe reports neither the EXIF
    Orientation nor the capture date of a JPEG/HEIC — for stills the rotation lives only
    in EXIF, never in a display matrix.
    """
    code, stdout, stderr = await run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        timeout_s=timeout_s,
    )
    if code != 0:
        raise EncodeError(f"ffprobe failed for {path.name}: {stderr.strip()[:400]}")
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise EncodeError(f"ffprobe returned invalid JSON for {path.name}: {exc}") from exc

    streams = data.get("streams", [])
    fmt = data.get("format", {})
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]

    duration: float | None = None
    raw_duration = fmt.get("duration")
    if raw_duration is not None:
        try:
            duration = float(raw_duration)
        except (TypeError, ValueError):
            duration = None

    width = height = bit_depth = None
    pix_fmt = color_transfer = None
    rotation = 0
    if video:
        first = video[0]
        width = _as_int(first.get("width"))
        height = _as_int(first.get("height"))
        pix_fmt = first.get("pix_fmt")
        bit_depth = _as_int(first.get("bits_per_raw_sample")) or _bit_depth_from_pix_fmt(pix_fmt)
        color_transfer = first.get("color_transfer")
        rotation = _display_rotation(first)

    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    for stream in streams:
        tags.update({k.lower(): v for k, v in (stream.get("tags") or {}).items()})
    has_date = any(
        key in tags and str(tags[key]).strip()
        for key in ("creation_time", "date", "datetimeoriginal", "com.apple.quicktime.creationdate")
    )

    if is_still:
        facts = await probe_exif(path)
        has_date = has_date or facts.has_date
        if facts.orientation is not None:
            rotation = _EXIF_ORIENTATION_ROTATION.get(facts.orientation, 0)

    return MediaProbe(
        width=width,
        height=height,
        duration_s=duration,
        video_streams=len(video),
        audio_streams=len(audio),
        has_date_time_original=has_date,
        rotation=rotation,
        pix_fmt=pix_fmt,
        bit_depth=bit_depth,
        color_transfer=color_transfer,
    )


async def probe_exif(path: Path, *, timeout_s: float = 60.0) -> ExifFacts:
    """Read capture date and EXIF Orientation of a still via exiftool.

    Missing exiftool is not fatal here — the caller degrades to "unknown", and the sanity
    gate rejects the result if it needed the capture date.
    """
    if shutil.which("exiftool") is None:
        return ExifFacts(has_date=False, orientation=None)
    code, stdout, _ = await run_command(
        ["exiftool", "-json", "-n", "-DateTimeOriginal", "-CreateDate", "-Orientation", str(path)],
        timeout_s=timeout_s,
    )
    if code != 0:
        return ExifFacts(has_date=False, orientation=None)
    try:
        entries = json.loads(stdout)
    except json.JSONDecodeError:
        return ExifFacts(has_date=False, orientation=None)
    if not entries:
        return ExifFacts(has_date=False, orientation=None)
    entry = entries[0]
    return ExifFacts(
        has_date=bool(entry.get("DateTimeOriginal") or entry.get("CreateDate")),
        # `-n` forces the numeric form; without it exiftool answers "Rotate 90 CW".
        orientation=_as_int(entry.get("Orientation")),
    )


async def jpeg_quality(path: Path, *, timeout_s: float = 60.0) -> int | None:
    """ImageMagick's estimate of the quality a JPEG was saved with, or ``None``.

    Used to refuse a re-encode that cannot pay for itself: quantisation error is
    cumulative, so running a q78 source through a q82 preset costs a second generation of
    artefacts and usually produces a *larger* file. The estimate is derived from the
    quantisation tables and is exact for anything libjpeg wrote.
    """
    if shutil.which("identify") is None:
        return None
    try:
        code, stdout, _ = await run_command(["identify", "-format", "%Q", str(path)], timeout_s=timeout_s)
    except EncodeError:
        return None
    if code != 0:
        return None
    # A multi-frame file prints one value per frame with no separator; take the first.
    digits = ""
    for char in stdout.strip():
        if not char.isdigit():
            break
        digits += char
    return int(digits) if digits else None


async def embedded_media_reason(path: Path, *, timeout_s: float = 60.0) -> str | None:
    """Why this still must not be re-encoded, or ``None`` when it is a plain image.

    A Samsung or Google motion photo is a JPEG with an MP4 glued on behind the
    end-of-image marker. Re-encoding reads the JPEG and drops the trailer, and *every*
    other check in this module says the result is fine: the metadata copy faithfully
    carries `XMP:MotionPhoto=1` across, the size ratio looks excellent precisely because
    the video is gone, and the picture itself is unchanged. Measured on a 1 935 292 byte
    source: 389 697 bytes out, no `ftyp` left, all XMP tags intact.

    Two independent signals, because neither alone is enough. The XMP tags identify the
    format but can be absent on vendor variants; the trailer check is format-agnostic but
    cannot tell a video from any other appended payload. Either one is a reason to stop.
    """
    trailer = _trailer_bytes(path)
    if trailer is not None and trailer > MAX_HARMLESS_TRAILER_BYTES:
        return f"{trailer} bytes of payload follow the JPEG end-of-image marker"

    if shutil.which("exiftool") is None:
        return None
    argv = ["exiftool", "-json", "-n", *(f"-{tag}" for tag in _MOTION_PHOTO_TAGS), str(path)]
    try:
        code, stdout, _ = await run_command(argv, timeout_s=timeout_s)
    except EncodeError:
        return None
    if code != 0:
        return None
    try:
        entries = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not entries:
        return None
    present = [tag for tag in _MOTION_PHOTO_TAGS if str(entries[0].get(tag, "")).strip() not in ("", "0")]
    if present:
        return f"motion photo markers present: {', '.join(present)}"
    return None


def _trailer_bytes(path: Path) -> int | None:
    """Bytes after the JPEG's EOI marker, or ``None`` if the file is not a walkable JPEG.

    ``None`` deliberately means "cannot tell", not "clean" — the caller then falls back to
    the metadata signal rather than assuming the file is safe.
    """
    try:
        with (
            path.open("rb") as handle,
            mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data,
        ):
            end = _jpeg_end_offset(data)
            return None if end is None else len(data) - end
    except (OSError, ValueError):
        return None


def _jpeg_end_offset(data: mmap.mmap) -> int | None:
    """Offset just past the EOI marker, by walking the segment structure from the SOI.

    Searching for the last ``FFD9`` in the file would be wrong twice over: an embedded
    thumbnail is a complete JPEG and ends in one, and an appended MP4 can contain the byte
    pair by chance. Walking the markers is the only way to find the real end.
    """
    size = len(data)
    if size < 4 or data[0:2] != b"\xff\xd8":
        return None
    pos = 2
    while pos + 1 < size:
        if data[pos] != 0xFF:
            return None
        # Any number of 0xFF fill bytes may precede a marker code.
        while pos < size and data[pos] == 0xFF:
            pos += 1
        if pos >= size:
            return None
        marker = data[pos]
        pos += 1
        if marker == 0xD9:  # EOI
            return pos
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:  # standalone markers carry no payload
            continue
        if pos + 1 >= size:
            return None
        length = int.from_bytes(data[pos : pos + 2], "big")
        if length < 2:
            return None
        pos += length
        if marker == 0xDA:  # SOS - entropy-coded data follows the header
            scanned = _skip_entropy_data(data, pos)
            if scanned is None:
                return None
            pos = scanned
    return None


def _skip_entropy_data(data: mmap.mmap, pos: int) -> int | None:
    """Advance past entropy-coded scan data to the next real marker.

    Inside the scan a literal 0xFF is stuffed as ``FF 00``, and ``FFD0``-``FFD7`` are
    restart markers that belong to the scan. Anything else is the next segment.
    """
    size = len(data)
    while True:
        index = data.find(b"\xff", pos)
        if index < 0 or index + 1 >= size:
            return None
        following = data[index + 1]
        if following == 0x00 or 0xD0 <= following <= 0xD7:
            pos = index + 2
            continue
        return index


async def verify_metadata(source: Path, target: Path, *, timeout_s: float = 120.0) -> list[str]:
    """Every EXIF/GPS/XMP/IPTC tag of ``source`` that ``target`` lost or changed.

    An empty list means the carry-over is complete. Tags *added* by the encode are not
    reported: gaining a tag is not losing one.

    Values are compared as exiftool *presents* them, not as ``-n`` floats. EXIF stores
    rationals, and copying a tag re-approximates the fraction: measured on a phone JPEG
    through the shipped preset, ``ExposureTime`` moved 2497831/250000000 -> 1/100 and the
    GPS latitude seconds 16316639/1000000 -> 39421/2416. Every one of those prints
    identically ("1/100", 48 deg 18' 16.32" N) because the change is below the precision
    the value is written at, but as floats they differ in the 4th to 11th digit and an
    exact comparison rejected every geotagged photo — with `metadata_verify: strict` and
    `delete_mode: permanent` that is a gate no image can pass.

    That covers every tag exiftool formats before printing, but not one that prints as a raw
    decimal: measured on a live library on 2026-08-24, ``FocalPlaneYResolution`` came back
    6734.006734 -> 6734.006711 and failed 24 of 67 encoded images on a difference in the 8th
    significant digit. So values that are numbers on both sides — with an identical unit, if
    any — are compared within :data:`_METADATA_REL_TOL` by :func:`_values_match`. Everything
    else, including a differing unit, still has to match exactly, and a tag that is gone is
    still gone.

    A value that is one whole fraction is a number too, evaluated rather than read as its
    first digit: measured on a live library on 2026-08-26, ``ShutterSpeedValue`` came back
    '1/999963365' -> '1/999963296' and failed 6 jobs. The denominator has to be in the
    millions before an integer one can move that little, so an exposure time still compares
    exactly — see :data:`_FRACTION`.

    The other printed-form change measured is exiftool writing an explicit ``+00:00`` onto a
    time that carried none: on a live instance on 2026-08-26, ``IPTC:TimeCreated`` and
    ``IPTC:DigitalCreationTime`` came back '11:24:38' -> '11:24:38+00:00' and failed 92 jobs
    in a single backfill run. Same clock, same displayed value, so :func:`_times_match`
    treats an absent offset and a zero one as the same time. A *non-zero* offset is a
    different time and still fails.

    The cost of comparing the printed form is that a change too small to alter the printed
    value cannot be seen here. That is the intended reading of "the metadata survived": the
    value a viewer is shown is the value that has to survive.
    """
    if shutil.which("exiftool") is None:
        raise EncodeError("exiftool is not installed but the metadata gate requires it")

    async def read(path: Path) -> dict[str, object]:
        code, stdout, stderr = await run_command(
            ["exiftool", "-json", "-G", *_METADATA_GROUPS, str(path)], timeout_s=timeout_s
        )
        if code != 0:
            raise EncodeError(f"exiftool could not read {path.name}: {stderr.strip()[:300]}")
        try:
            entries = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise EncodeError(f"exiftool returned invalid JSON for {path.name}: {exc}") from exc
        if not entries:
            return {}
        return {key: value for key, value in entries[0].items() if key != "SourceFile"}

    return _diff_metadata(await read(source), await read(target))


async def copy_metadata(
    source: Path,
    target: Path,
    *,
    normalize_orientation: bool = False,
    timeout_s: float = 300.0,
) -> None:
    """Carry EXIF/XMP/IPTC from ``source`` onto ``target`` with exiftool.

    Verified necessity: ``ffmpeg -map_metadata 0 -movflags use_metadata_tags`` preserves
    QuickTime CreateDate, GPS, Make and Model, but drops XMP Description, Rating and
    Subject. For stills, nothing survives a re-encode at all without this step.

    ``normalize_orientation`` excludes Orientation from the copy and pins it to 1. Presets
    that run ``magick -auto-orient`` have already baked the rotation into the pixels;
    copying the source Orientation back on top would rotate the image a second time.
    """
    if shutil.which("exiftool") is None:
        raise EncodeError("exiftool is not installed but a preset requires exiftool_copy")
    argv = ["exiftool", "-quiet", "-TagsFromFile", str(source), "-all:all"]
    if normalize_orientation:
        argv.append("--Orientation")
    argv += ["-unsafe", "-icc_profile"]
    if normalize_orientation:
        argv.append("-Orientation#=1")
    argv += ["-overwrite_original", str(target)]

    code, _, stderr = await run_command(argv, timeout_s=timeout_s)
    if code != 0:
        raise EncodeError(f"exiftool metadata copy failed: {stderr.strip()[:400]}")


# libva announces itself on stderr before anything interesting happens, and ffmpeg prefixes
# component messages with a heap address that differs on every run.
_LIBVA_CHATTER = re.compile(r"^libva info:")
_COMPONENT_PREFIX = re.compile(r"^\[[^\]]+@\s*0x[0-9a-f]+\]\s*")


def first_diagnostic_line(stderr: str) -> str | None:
    """The line of ffmpeg's stderr that actually says what went wrong.

    The *first* meaningful line carries the diagnosis ("No VA display found for device
    ...", "Error creating a MFX session: -9"); the last one is only ffmpeg giving up
    ("Error parsing global options"). Two kinds of noise sit in front of it: five
    ``libva info:`` lines that libva prints on every single VA-API call, and a
    ``[AVHWDeviceContext @ 0x...]`` prefix whose address changes per run and would make
    otherwise-identical failures look like different ones.
    """
    for line in stderr.strip().splitlines():
        stripped = line.strip()
        if not stripped or _LIBVA_CHATTER.match(stripped):
            continue
        return _COMPONENT_PREFIX.sub("", stripped)[:200]
    return None


async def probe_hardware_encoder(encoder_name: str, device: str, *, timeout_s: float = 60.0) -> str | None:
    """Encode a single black frame to prove the GPU path actually works.

    Returns ``None`` on success, otherwise the reason — a missing driver, a render node
    the process may not open, or a codec the chip does not implement. Worth doing at
    startup: without it the first real job is the one that discovers the problem, an hour
    after the container came up.
    """
    argv = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if encoder_name.endswith("_qsv"):
        argv += ["-qsv_device", device]
    elif encoder_name.endswith("_vaapi"):
        argv += ["-vaapi_device", device]
    argv += ["-f", "lavfi", "-i", "color=black:size=320x240:rate=1:duration=0.1"]
    if encoder_name.endswith("_vaapi"):
        argv += ["-vf", "format=nv12,hwupload"]
    argv += ["-c:v", encoder_name, "-f", "null", "-"]

    try:
        code, _, stderr = await run_command(argv, timeout_s=timeout_s)
    except EncodeError as exc:
        return str(exc)
    if code == 0:
        return None
    return first_diagnostic_line(stderr) or f"ffmpeg exited {code}"


async def encode(source: Path, preset: Preset, work_dir: Path) -> EncodeResult:
    """Run the preset and return sizes plus a probe of the output."""
    if not source.is_file():
        raise EncodeError(f"source does not exist: {source}")
    orig_bytes = source.stat().st_size

    output = work_dir / f"{source.stem}{preset.suffix}"
    if output.resolve() == source.resolve():
        output = work_dir / f"{source.stem}.encoded{preset.suffix}"
    output.unlink(missing_ok=True)

    argv = preset.argv(source, output)
    code, _, stderr = await run_command(argv, timeout_s=preset.timeout_s)
    if code != 0:
        raise EncodeError(f"preset {preset.name!r} exited {code}: {stderr.strip()[-600:]}")
    if not output.is_file() or output.stat().st_size == 0:
        raise EncodeError(f"preset {preset.name!r} produced no output")

    if preset.exiftool_copy:
        await copy_metadata(source, output, normalize_orientation=preset.normalize_orientation)

    # Probe *after* the metadata copy — it can change the effective orientation.
    return EncodeResult(
        output_path=output,
        orig_bytes=orig_bytes,
        new_bytes=output.stat().st_size,
        probe=await probe(output, is_still=preset.match_type != "VIDEO"),
        # Hashed here, after the last write to the file, so it describes exactly the
        # bytes the upload will send.
        checksum=file_checksum(output),
    )


async def check_sanity(
    *,
    source: Path,
    result: EncodeResult,
    source_probe: MediaProbe,
    behavior: BehaviorSettings,
    preset: Preset,
    is_video: bool,
) -> SanityResult:
    """Gate the upload. Every condition must hold — otherwise nothing is uploaded.

    Deliberately conservative: a false negative wastes CPU, a false positive can lose
    picture quality or timeline position permanently.

    The thresholds come from ``preset`` where it overrides ``behavior``, because video and
    stills have opposite economics — see :class:`~immich_compressor.config.Preset`.
    """
    failures: list[str] = []
    out = result.probe

    max_ratio = preset.effective_max_ratio(behavior)
    limit = result.orig_bytes * max_ratio
    if result.new_bytes > limit:
        failures.append(
            f"no gain: {result.new_bytes} bytes > {limit:.0f} ({result.ratio:.3f} > max_ratio {max_ratio})"
        )

    # The economic gate. Ratio alone is the wrong axis for a cheap encode: 0.75 on a 12 MB
    # photo saves 3 MB, 0.60 on a 371 KB photo saves 147 KB — and a saved 147 KB does not
    # pay for a permanent asset lifecycle (thumbnails, embedding, faces, OCR, timeline).
    min_savings = preset.effective_min_savings_bytes(behavior)
    saved = result.orig_bytes - result.new_bytes
    if saved < min_savings:
        failures.append(f"saves only {saved} bytes, below min_savings_bytes {min_savings}")

    if not out.has_visual_stream:
        failures.append("output has no video/image stream")

    # Compared as *displayed*, not as stored: an encoder may legitimately either keep the
    # rotation metadata or bake it into the pixels. What must never happen is that the
    # rotation is lost — that shows up here as a swapped display size.
    source_display = source_probe.display_size
    if behavior.require_same_resolution and all(source_display):
        out_display = out.display_size
        if out_display != source_display:
            failures.append(
                f"display size changed: {source_display[0]}x{source_display[1]} "
                f"-> {out_display[0]}x{out_display[1]} "
                f"(rotation {source_probe.rotation} -> {out.rotation})"
            )

    if source_probe.bit_depth and out.bit_depth and out.bit_depth < source_probe.bit_depth:
        failures.append(f"bit depth dropped: {source_probe.bit_depth} -> {out.bit_depth}")

    if source_probe.is_hdr and not out.is_hdr:
        failures.append(
            f"HDR transfer lost: {source_probe.color_transfer} -> {out.color_transfer or 'none'} "
            "— the output would be washed out"
        )

    if is_video:
        if source_probe.duration_s is not None and out.duration_s is not None:
            drift = abs(source_probe.duration_s - out.duration_s)
            if drift > behavior.duration_tolerance_s:
                failures.append(f"duration drift {drift:.3f}s exceeds {behavior.duration_tolerance_s}s")
        elif source_probe.duration_s is not None:
            failures.append("output has no readable duration")

        if out.audio_streams != source_probe.audio_streams:
            failures.append(
                f"audio stream count changed: {source_probe.audio_streams} -> {out.audio_streams}"
            )

    # `probe()` already folded exiftool's answer in for stills, so this holds for both.
    #
    # Off for stills by default: the timeline position of a replacement does not come from
    # the file. `upload_asset` sends `fileCreatedAt` from the source asset, and step 8
    # writes `dateTimeOriginal` explicitly over the API afterwards. For video the tag is
    # the real safety net, which is why the default stays on there.
    #
    # Measured against the source, not against nothing. A video that never had a
    # `creation_time` could not pass this gate at any quality — which is everything that
    # did not come straight out of a camera app: screen recordings, messenger clips, drone
    # exports, anything that was cut. The gate exists to catch a capture date the encode
    # *lost*, and an output cannot lose what the input never carried.
    if (
        preset.effective_require_date_time_original(behavior)
        and source_probe.has_date_time_original
        and not out.has_date_time_original
    ):
        failures.append("output lost the capture date the source had — would land wrong in the timeline")

    if failures:
        logger.info("sanity gate rejected %s: %s", source.name, "; ".join(failures))
    return SanityResult(ok=not failures, failures=failures)


def compressed_filename(original: str, marker: str, suffix: str) -> str:
    """``clip.mov`` + ``.cmp`` + ``.mp4`` -> ``clip.cmp.mp4``.

    The marker also lives in the filename so the Immich-side ``assetFileFilter`` regex can
    reject it before the webhook even fires. The metadata KV marker remains the hard
    loop guard.
    """
    stem = Path(original).stem or "asset"
    return f"{stem}{marker}{suffix}"


def has_free_space(directory: Path, needed_bytes: int) -> bool:
    directory.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(directory).free >= needed_bytes


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _display_rotation(stream: dict[str, object]) -> int:
    """Clockwise rotation from a stream's display matrix, normalised to 0/90/180/270.

    ffmpeg reports the angle counter-clockwise and signed (``-90`` and ``270`` mean the
    same thing). ``% 360`` folds both conventions onto the same value, and the only thing
    the caller asks of it — "do width and height swap?" — is unaffected by the direction.
    """
    side_data = stream.get("side_data_list")
    if not isinstance(side_data, list):
        return 0
    for entry in side_data:
        if isinstance(entry, dict) and "rotation" in entry:
            degrees = _as_int(entry["rotation"])
            if degrees is not None:
                return degrees % 360
    return 0


def _bit_depth_from_pix_fmt(pix_fmt: str | None) -> int | None:
    """Fallback for streams without ``bits_per_raw_sample`` (``yuv420p10le`` -> 10)."""
    if not pix_fmt:
        return None
    name = pix_fmt.removesuffix("le").removesuffix("be")
    digits = ""
    while name and name[-1].isdigit():
        digits = name[-1] + digits
        name = name[:-1]
    # Only a `p` right before the digits marks a depth suffix; `nv12` is 8-bit, not 12.
    if not digits or not name.endswith("p"):
        return 8
    return int(digits)
