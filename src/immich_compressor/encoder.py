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
    that run ``convert -auto-orient`` have already baked the rotation into the pixels;
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
    is_video: bool,
) -> SanityResult:
    """Gate the upload. Every condition must hold — otherwise nothing is uploaded.

    Deliberately conservative: a false negative wastes CPU, a false positive can lose
    picture quality or timeline position permanently.
    """
    failures: list[str] = []
    out = result.probe

    limit = result.orig_bytes * behavior.max_ratio
    if result.new_bytes > limit:
        failures.append(
            f"no gain: {result.new_bytes} bytes > {limit:.0f} "
            f"({result.ratio:.3f} > max_ratio {behavior.max_ratio})"
        )

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
    if behavior.require_date_time_original and not out.has_date_time_original:
        failures.append("output has no capture date — would land wrong in the timeline")

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
