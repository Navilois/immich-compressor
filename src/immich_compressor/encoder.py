"""Preset execution, metadata carry-over and the sanity gate.

Every external tool is invoked with :func:`asyncio.create_subprocess_exec` — argv lists,
never a shell string — so a filename can never be interpreted as a command.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .config import BehaviorSettings, Preset

logger = logging.getLogger(__name__)


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

    @property
    def has_visual_stream(self) -> bool:
        return self.video_streams > 0


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

    @property
    def ratio(self) -> float:
        return self.new_bytes / self.orig_bytes if self.orig_bytes else 1.0


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


async def probe(path: Path, *, timeout_s: float = 120.0) -> MediaProbe:
    """Inspect a media file with ``ffprobe``. Raises :class:`EncodeError` if undecodable."""
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

    width = height = None
    if video:
        width = _as_int(video[0].get("width"))
        height = _as_int(video[0].get("height"))

    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    for stream in streams:
        tags.update({k.lower(): v for k, v in (stream.get("tags") or {}).items()})
    has_date = any(
        key in tags and str(tags[key]).strip()
        for key in ("creation_time", "date", "datetimeoriginal", "com.apple.quicktime.creationdate")
    )

    return MediaProbe(
        width=width,
        height=height,
        duration_s=duration,
        video_streams=len(video),
        audio_streams=len(audio),
        has_date_time_original=has_date,
    )


async def probe_still(path: Path, *, timeout_s: float = 60.0) -> bool:
    """Return whether a still image carries a usable capture date, via exiftool."""
    if shutil.which("exiftool") is None:
        return False
    code, stdout, _ = await run_command(
        ["exiftool", "-json", "-DateTimeOriginal", "-CreateDate", str(path)],
        timeout_s=timeout_s,
    )
    if code != 0:
        return False
    try:
        entries = json.loads(stdout)
    except json.JSONDecodeError:
        return False
    if not entries:
        return False
    entry = entries[0]
    return bool(entry.get("DateTimeOriginal") or entry.get("CreateDate"))


async def copy_metadata(source: Path, target: Path, *, timeout_s: float = 300.0) -> None:
    """Carry EXIF/XMP/IPTC from ``source`` onto ``target`` with exiftool.

    Verified necessity: ``ffmpeg -map_metadata 0 -movflags use_metadata_tags`` preserves
    QuickTime CreateDate, GPS, Make and Model, but drops XMP Description, Rating and
    Subject. For stills, nothing survives a re-encode at all without this step.
    """
    if shutil.which("exiftool") is None:
        raise EncodeError("exiftool is not installed but a preset requires exiftool_copy")
    code, _, stderr = await run_command(
        [
            "exiftool",
            "-quiet",
            "-TagsFromFile",
            str(source),
            "-all:all",
            "-unsafe",
            "-icc_profile",
            "-overwrite_original",
            str(target),
        ],
        timeout_s=timeout_s,
    )
    if code != 0:
        raise EncodeError(f"exiftool metadata copy failed: {stderr.strip()[:400]}")


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
        await copy_metadata(source, output)

    return EncodeResult(
        output_path=output,
        orig_bytes=orig_bytes,
        new_bytes=output.stat().st_size,
        probe=await probe(output),
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

    if (
        behavior.require_same_resolution
        and source_probe.width
        and source_probe.height
        and (out.width, out.height) != (source_probe.width, source_probe.height)
    ):
        failures.append(
            f"resolution changed: {source_probe.width}x{source_probe.height} "
            f"-> {out.width}x{out.height}"
        )

    if is_video:
        if source_probe.duration_s is not None and out.duration_s is not None:
            drift = abs(source_probe.duration_s - out.duration_s)
            if drift > behavior.duration_tolerance_s:
                failures.append(
                    f"duration drift {drift:.3f}s exceeds {behavior.duration_tolerance_s}s"
                )
        elif source_probe.duration_s is not None:
            failures.append("output has no readable duration")

        if out.audio_streams != source_probe.audio_streams:
            failures.append(
                f"audio stream count changed: {source_probe.audio_streams} -> {out.audio_streams}"
            )

    if behavior.require_date_time_original:
        has_date = out.has_date_time_original or (
            not is_video and await probe_still(result.output_path)
        )
        if not has_date:
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
