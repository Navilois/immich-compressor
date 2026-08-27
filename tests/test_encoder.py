"""Encoder + sanity gate. Uses real ffmpeg against tiny generated clips."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from immich_compressor.config import BehaviorSettings, ConfigError, Preset
from immich_compressor.encoder import (
    EncodeError,
    EncodeResult,
    MediaProbe,
    _diff_metadata,
    _trailer_bytes,
    _values_match,
    check_sanity,
    compressed_filename,
    embedded_media_reason,
    encode,
    has_free_space,
    jpeg_quality,
    probe,
    probe_exif,
    probe_hardware_encoder,
    run_command,
    verify_metadata,
)

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)

needs_still_tools = pytest.mark.skipif(
    shutil.which("magick") is None or shutil.which("exiftool") is None,
    reason="imagemagick/exiftool not installed",
)


async def _make_clip(path: Path, *, seconds: int = 2, size: str = "320x240", bitrate: str = "4000k") -> Path:
    code, _, stderr = await run_command(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={size}:rate=15:duration={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-c:v",
            "mpeg4",
            "-b:v",
            bitrate,
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-shortest",
            "-metadata",
            "creation_time=2024-06-15T12:30:00Z",
            str(path),
        ],
        timeout_s=180,
    )
    assert code == 0, stderr
    return path


async def _rotate(source: Path, target: Path, degrees: int = 90) -> Path:
    """Remux with a display matrix — what every portrait phone clip carries."""
    code, _, stderr = await run_command(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-display_rotation",
            str(degrees),
            "-i",
            str(source),
            "-c",
            "copy",
            "-map_metadata",
            "0",
            "-movflags",
            "use_metadata_tags",
            str(target),
        ],
        timeout_s=180,
    )
    assert code == 0, stderr
    return target


async def _make_still(path: Path, *, orientation: int = 6, size: str = "1200x800") -> Path:
    """A JPEG whose EXIF says "rotate me", with the metadata a real photo carries."""
    code, _, stderr = await run_command(
        ["magick", "-size", size, "gradient:red-blue", "-quality", "95", str(path)],
        timeout_s=120,
    )
    assert code == 0, stderr
    code, _, stderr = await run_command(
        [
            "exiftool",
            "-quiet",
            "-overwrite_original",
            f"-Orientation#={orientation}",
            "-DateTimeOriginal=2024:06:15 12:30:00",
            "-Make=TestCam",
            "-Description=Hallo Welt",
            str(path),
        ],
        timeout_s=120,
    )
    assert code == 0, stderr
    return path


@pytest.fixture
def behavior(tmp_path: Path) -> BehaviorSettings:
    # min_savings_bytes off: these tests exercise the other gates, and a generated
    # two-second clip never saves a megabyte.
    return BehaviorSettings(work_dir=tmp_path / "work", max_ratio=0.6, min_savings_bytes=0)


@pytest.fixture
def h265_preset() -> Preset:
    return Preset(
        name="video-h265",
        type="VIDEO",
        cmd="ffmpeg -y -loglevel error -i {input} -map_metadata 0 "
        "-movflags use_metadata_tags -c:v libx265 -preset ultrafast -crf 30 "
        "-threads 2 -x265-params log-level=none:pools=2 -c:a aac -b:a 96k {output}",
        suffix=".mp4",
        timeout_s=600,
    )


async def test_run_command_never_uses_a_shell() -> None:
    """`;` must stay a literal argument, not a command separator."""
    code, stdout, _ = await run_command(["echo", "a; echo pwned"], timeout_s=10)
    assert code == 0
    assert stdout.strip() == "a; echo pwned"


async def test_run_command_times_out() -> None:
    with pytest.raises(EncodeError, match="timed out"):
        await run_command(["sleep", "30"], timeout_s=0.5)


async def test_probe_reads_streams(tmp_path: Path) -> None:
    clip = await _make_clip(tmp_path / "in.mp4")
    result = await probe(clip)
    assert result.video_streams == 1
    assert result.audio_streams == 1
    assert result.width == 320
    assert result.height == 240
    assert result.duration_s is not None
    assert result.has_date_time_original is True


async def test_probe_rejects_garbage(tmp_path: Path) -> None:
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"definitely not a video")
    with pytest.raises(EncodeError):
        await probe(junk)


async def test_encode_shrinks_and_passes_the_gate(
    tmp_path: Path, behavior: BehaviorSettings, h265_preset: Preset
) -> None:
    clip = await _make_clip(tmp_path / "in.mp4", bitrate="8000k")
    work = tmp_path / "work"
    work.mkdir()
    source_probe = await probe(clip)

    result = await encode(clip, h265_preset, work)
    assert result.new_bytes < result.orig_bytes
    assert result.probe.video_streams == 1

    sanity = await check_sanity(
        source=clip,
        result=result,
        source_probe=source_probe,
        behavior=behavior,
        preset=h265_preset,
        is_video=True,
    )
    assert sanity.ok, sanity.reason()


async def test_sanity_rejects_when_there_is_no_gain(tmp_path: Path, behavior: BehaviorSettings) -> None:
    """A "compression" that barely shrinks must not reach the upload step."""
    clip = await _make_clip(tmp_path / "in.mp4", bitrate="600k")
    work = tmp_path / "work"
    work.mkdir()
    # Stream-copy: output ~= input, so the ratio gate must fire.
    copy_preset = Preset(
        name="copy",
        type="VIDEO",
        cmd="ffmpeg -y -loglevel error -i {input} -map_metadata 0 -c copy {output}",
        suffix=".mp4",
    )
    source_probe = await probe(clip)
    result = await encode(clip, copy_preset, work)
    sanity = await check_sanity(
        source=clip,
        result=result,
        source_probe=source_probe,
        behavior=behavior,
        preset=copy_preset,
        is_video=True,
    )
    assert not sanity.ok
    assert any("no gain" in failure for failure in sanity.failures)


async def test_sanity_rejects_resolution_change(tmp_path: Path, behavior: BehaviorSettings) -> None:
    clip = await _make_clip(tmp_path / "in.mp4", size="640x480", bitrate="8000k")
    work = tmp_path / "work"
    work.mkdir()
    downscale = Preset(
        name="downscale",
        type="VIDEO",
        cmd="ffmpeg -y -loglevel error -i {input} -map_metadata 0 -vf scale=320:240 "
        "-c:v libx265 -preset ultrafast -crf 30 -threads 2 "
        "-x265-params log-level=none:pools=2 -c:a copy {output}",
        suffix=".mp4",
    )
    source_probe = await probe(clip)
    result = await encode(clip, downscale, work)
    sanity = await check_sanity(
        source=clip,
        result=result,
        source_probe=source_probe,
        behavior=behavior,
        preset=downscale,
        is_video=True,
    )
    assert not sanity.ok
    assert any("display size changed" in failure for failure in sanity.failures)


async def test_sanity_rejects_dropped_audio(tmp_path: Path, behavior: BehaviorSettings) -> None:
    clip = await _make_clip(tmp_path / "in.mp4", bitrate="8000k")
    work = tmp_path / "work"
    work.mkdir()
    no_audio = Preset(
        name="no-audio",
        type="VIDEO",
        cmd="ffmpeg -y -loglevel error -i {input} -map_metadata 0 -an "
        "-c:v libx265 -preset ultrafast -crf 30 -threads 2 "
        "-x265-params log-level=none:pools=2 {output}",
        suffix=".mp4",
    )
    source_probe = await probe(clip)
    result = await encode(clip, no_audio, work)
    sanity = await check_sanity(
        source=clip,
        result=result,
        source_probe=source_probe,
        behavior=behavior,
        preset=no_audio,
        is_video=True,
    )
    assert not sanity.ok
    assert any("audio stream count" in failure for failure in sanity.failures)


# --------------------------------------------------------------------------- rotation


async def test_probe_reports_rotation_and_swaps_the_display_size(tmp_path: Path) -> None:
    flat = await _make_clip(tmp_path / "flat.mp4", size="320x240")
    rotated = await _rotate(flat, tmp_path / "rot.mp4", degrees=90)

    result = await probe(rotated)
    assert (result.width, result.height) == (320, 240), "the stored frame is untouched"
    assert result.rotation == 90
    assert result.display_size == (240, 320), "a viewer sees it upright"


async def test_rotated_video_passes_the_gate(tmp_path: Path, behavior: BehaviorSettings) -> None:
    """The regression this whole change exists for.

    With ffmpeg's default ``-autorotate`` the output is 240x320 while the source is stored
    as 320x240, so the old stored-size comparison rejected *every* portrait clip.
    """
    flat = await _make_clip(tmp_path / "flat.mp4", size="320x240", bitrate="8000k")
    rotated = await _rotate(flat, tmp_path / "rot.mp4", degrees=90)
    work = tmp_path / "work"
    work.mkdir()
    preset = Preset(
        name="noautorotate",
        type="VIDEO",
        cmd="ffmpeg -y -loglevel error -noautorotate -i {input} -map_metadata 0 -map 0 "
        "-movflags use_metadata_tags -c:v libx265 -preset ultrafast -crf 30 -threads 2 "
        "-x265-params log-level=none:pools=2 -c:a copy {output}",
        suffix=".mp4",
        timeout_s=600,
    )
    source_probe = await probe(rotated)
    result = await encode(rotated, preset, work)

    assert result.probe.rotation == 90, "the display matrix must survive the re-encode"
    assert result.probe.display_size == source_probe.display_size

    sanity = await check_sanity(
        source=rotated,
        result=result,
        source_probe=source_probe,
        behavior=behavior,
        preset=preset,
        is_video=True,
    )
    assert sanity.ok, sanity.reason()


async def test_sanity_rejects_a_lost_rotation(tmp_path: Path, behavior: BehaviorSettings) -> None:
    """Pixels left unrotated *and* the matrix dropped — the one case that is real damage."""
    flat = await _make_clip(tmp_path / "flat.mp4", size="320x240", bitrate="8000k")
    rotated = await _rotate(flat, tmp_path / "rot.mp4", degrees=90)
    work = tmp_path / "work"
    work.mkdir()
    # `-display_rotation 0` overrides the input matrix, so nothing rotates and nothing is
    # written to the output either.
    lossy = Preset(
        name="drops-rotation",
        type="VIDEO",
        cmd="ffmpeg -y -loglevel error -display_rotation 0 -i {input} -map_metadata 0 -map 0 "
        "-c:v libx265 -preset ultrafast -crf 30 -threads 2 "
        "-x265-params log-level=none:pools=2 -c:a copy {output}",
        suffix=".mp4",
        timeout_s=600,
    )
    source_probe = await probe(rotated)
    result = await encode(rotated, lossy, work)

    sanity = await check_sanity(
        source=rotated,
        result=result,
        source_probe=source_probe,
        behavior=behavior,
        preset=lossy,
        is_video=True,
    )
    assert not sanity.ok
    assert any("display size changed" in failure for failure in sanity.failures)


async def test_sanity_rejects_a_bit_depth_drop(tmp_path: Path, behavior: BehaviorSettings) -> None:
    source = tmp_path / "10bit.mp4"
    code, _, stderr = await run_command(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=15:duration=1",
            "-c:v",
            "libx265",
            "-preset",
            "ultrafast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p10le",
            "-x265-params",
            "log-level=none:pools=2",
            "-threads",
            "2",
            "-metadata",
            "creation_time=2024-06-15T12:30:00Z",
            str(source),
        ],
        timeout_s=300,
    )
    assert code == 0, stderr
    work = tmp_path / "work"
    work.mkdir()
    to_8bit = Preset(
        name="flatten",
        type="VIDEO",
        cmd="ffmpeg -y -loglevel error -noautorotate -i {input} -map_metadata 0 "
        "-pix_fmt yuv420p -c:v libx265 -preset ultrafast -crf 30 -threads 2 "
        "-x265-params log-level=none:pools=2 {output}",
        suffix=".mp4",
        timeout_s=600,
    )
    source_probe = await probe(source)
    assert source_probe.bit_depth == 10
    result = await encode(source, to_8bit, work)

    sanity = await check_sanity(
        source=source,
        result=result,
        source_probe=source_probe,
        behavior=behavior,
        preset=to_8bit,
        is_video=True,
    )
    assert not sanity.ok
    assert any("bit depth dropped" in failure for failure in sanity.failures)


# ------------------------------------------------------------------------- stills


@needs_still_tools
async def test_still_orientation_is_normalised_and_metadata_survives(tmp_path: Path) -> None:
    source = await _make_still(tmp_path / "in.jpg", orientation=6)
    work = tmp_path / "work"
    work.mkdir()
    preset = Preset(
        name="image-jpeg",
        type="IMAGE",
        cmd="magick {input} -auto-orient -quality 82 -interlace Plane {output}",
        suffix=".jpg",
        exiftool_copy=True,
        normalize_orientation=True,
        timeout_s=300,
    )
    source_probe = await probe(source, is_still=True)
    assert source_probe.rotation == 90, "EXIF Orientation 6 means the viewer rotates it"
    assert source_probe.display_size == (800, 1200)

    result = await encode(source, preset, work)

    facts = await probe_exif(result.output_path)
    assert facts.orientation == 1, "pixels are upright, so the tag must say so"
    assert facts.has_date
    assert result.probe.display_size == source_probe.display_size

    code, stdout, _ = await run_command(
        ["exiftool", "-json", "-Description", "-Make", str(result.output_path)],
        timeout_s=60,
    )
    assert code == 0
    assert "Hallo Welt" in stdout and "TestCam" in stdout

    # max_ratio is not what this test is about — a gradient JPEG barely shrinks.
    sanity = await check_sanity(
        source=source,
        result=result,
        source_probe=source_probe,
        behavior=BehaviorSettings(work_dir=work, max_ratio=1.0, min_savings_bytes=0),
        preset=preset,
        is_video=False,
    )
    assert sanity.ok, sanity.reason()


@needs_still_tools
async def test_still_double_rotation_is_rejected(tmp_path: Path) -> None:
    """`-auto-orient` without `normalize_orientation` copies the tag back onto already
    upright pixels. The gate has to catch that, because the picture ends up sideways."""
    source = await _make_still(tmp_path / "in.jpg", orientation=6)
    work = tmp_path / "work"
    work.mkdir()
    trap = Preset(
        name="double-rotation",
        type="IMAGE",
        cmd="magick {input} -auto-orient -quality 82 -interlace Plane {output}",
        suffix=".jpg",
        exiftool_copy=True,
        normalize_orientation=False,
        timeout_s=300,
    )
    source_probe = await probe(source, is_still=True)
    result = await encode(source, trap, work)

    assert result.probe.rotation == 90, "the copied tag rotates the upright pixels again"
    sanity = await check_sanity(
        source=source,
        result=result,
        source_probe=source_probe,
        behavior=BehaviorSettings(work_dir=work, max_ratio=1.0, min_savings_bytes=0),
        preset=trap,
        is_video=False,
    )
    assert not sanity.ok
    assert any("display size changed" in failure for failure in sanity.failures)


@needs_still_tools
@pytest.mark.parametrize(
    ("orientation", "expected_rotation"),
    [(1, 0), (3, 180), (6, 90), (8, 270)],
)
async def test_exif_orientation_maps_to_rotation(
    tmp_path: Path, orientation: int, expected_rotation: int
) -> None:
    still = await _make_still(tmp_path / f"o{orientation}.jpg", orientation=orientation)
    result = await probe(still, is_still=True)
    assert result.rotation == expected_rotation
    expected_size = (800, 1200) if expected_rotation in (90, 270) else (1200, 800)
    assert result.display_size == expected_size


def test_normalize_orientation_needs_auto_orient() -> None:
    with pytest.raises(ConfigError, match="auto-orient"):
        Preset(
            name="bad",
            type="IMAGE",
            cmd="magick {input} -quality 82 {output}",
            suffix=".jpg",
            exiftool_copy=True,
            normalize_orientation=True,
        )


def test_normalize_orientation_needs_the_metadata_copy() -> None:
    with pytest.raises(ConfigError, match="exiftool_copy"):
        Preset(
            name="bad",
            type="IMAGE",
            cmd="magick {input} -auto-orient -quality 82 {output}",
            suffix=".jpg",
            exiftool_copy=False,
            normalize_orientation=True,
        )


async def test_hardware_probe_explains_a_missing_device(tmp_path: Path) -> None:
    """No GPU here, so this covers the path that matters operationally: a clear reason
    instead of a stack trace. The success path needs real hardware and is checked by
    `immich-compressor check` on the target host."""
    problem = await probe_hardware_encoder("hevc_qsv", str(tmp_path / "renderD_nope"))
    assert problem, "a missing render node must produce a reason, not silence"


async def test_encode_fails_loudly_on_a_bad_command(tmp_path: Path) -> None:
    clip = await _make_clip(tmp_path / "in.mp4")
    work = tmp_path / "work"
    work.mkdir()
    broken = Preset(
        name="broken",
        type="VIDEO",
        cmd="ffmpeg -y -loglevel error -i {input} -c:v definitely_not_a_codec {output}",
        suffix=".mp4",
    )
    with pytest.raises(EncodeError):
        await encode(clip, broken, work)


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        ("clip.MOV", "clip.cmp.mp4"),
        ("holiday video.mp4", "holiday video.cmp.mp4"),
        ("no-extension", "no-extension.cmp.mp4"),
        ("", "asset.cmp.mp4"),
    ],
)
def test_compressed_filename(original: str, expected: str) -> None:
    assert compressed_filename(original, ".cmp", ".mp4") == expected


def test_free_space_check(tmp_path: Path) -> None:
    assert has_free_space(tmp_path / "sub", 1024) is True
    assert has_free_space(tmp_path / "sub", 10**18) is False


# ------------------------------------------------------- embedded media / motion photos


def _synthetic_jpeg(trailer: bytes = b"") -> bytes:
    """A structurally valid minimal JPEG, optionally with a payload glued on behind EOI.

    Hand-built so the marker walk can be tested without ImageMagick — and so the scan data
    deliberately contains both a stuffed ``FF 00`` and a restart marker, the two things
    that make "search for the last FFD9" the wrong answer.
    """
    app0 = b"\xff\xe0" + (16).to_bytes(2, "big") + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sos = b"\xff\xda" + (8).to_bytes(2, "big") + b"\x01\x01\x00\x00\x3f\x00"
    scan = b"\x12\x34\xff\x00\x56\xff\xd0\x78\x9a"
    return b"\xff\xd8" + app0 + sos + scan + b"\xff\xd9" + trailer


def test_trailer_detection_walks_the_marker_structure(tmp_path: Path) -> None:
    clean = tmp_path / "clean.jpg"
    clean.write_bytes(_synthetic_jpeg())
    assert _trailer_bytes(clean) == 0

    with_payload = tmp_path / "motion.jpg"
    payload = b"\x00\x00\x00\x18ftypmp42" + b"\xab" * 50_000
    with_payload.write_bytes(_synthetic_jpeg(payload))
    assert _trailer_bytes(with_payload) == len(payload)


def test_trailer_detection_reports_unknown_for_a_non_jpeg(tmp_path: Path) -> None:
    """``None`` means "cannot tell", and the caller must not read it as "clean"."""
    other = tmp_path / "not.jpg"
    other.write_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 ")
    assert _trailer_bytes(other) is None


async def test_appended_payload_is_flagged(tmp_path: Path) -> None:
    path = tmp_path / "motion.jpg"
    path.write_bytes(_synthetic_jpeg(b"\x00\x00\x00\x18ftypmp42" + b"\xab" * 50_000))
    reason = await embedded_media_reason(path)
    assert reason is not None and "end-of-image marker" in reason


async def test_padding_below_the_threshold_is_tolerated(tmp_path: Path) -> None:
    path = tmp_path / "padded.jpg"
    path.write_bytes(_synthetic_jpeg(b"\x00" * 16))
    assert await embedded_media_reason(path) is None


@needs_still_tools
async def test_motion_photo_markers_are_flagged_without_a_trailer(tmp_path: Path) -> None:
    """The second signal, on its own: a vendor variant that carries the XMP tags only."""
    source = await _make_still(tmp_path / "in.jpg")
    code, _, stderr = await run_command(
        [
            "exiftool",
            "-quiet",
            "-overwrite_original",
            "-XMP-GCamera:MotionPhoto=1",
            str(source),
        ],
        timeout_s=60,
    )
    assert code == 0, stderr
    reason = await embedded_media_reason(source)
    assert reason is not None and "MotionPhoto" in reason


@needs_still_tools
async def test_plain_still_is_not_flagged(tmp_path: Path) -> None:
    source = await _make_still(tmp_path / "in.jpg")
    assert await embedded_media_reason(source) is None


# ------------------------------------------------------------- metadata verification


@pytest.mark.parametrize(
    ("before", "after"),
    [
        # Measured on a live library on 2026-08-24: EXIF:FocalPlaneYResolution failed 24 of
        # 67 encoded images on this, a difference in the 8th significant digit.
        ("6734.006734", "6734.006711"),
        # The same class with a unit, from an earlier failure in the same store.
        ("339.569 m", "339.5690021 m"),
        (6734.006734, 6734.006711),
        ("48.2082", "48.2082"),
        ("Canon", "Canon"),
        # Measured on a live library on 2026-08-26: EXIF:ShutterSpeedValue failed 6 jobs on
        # this. The printed form is itself a fraction, so it never parsed as a number and
        # fell through to the exact comparison. Evaluated, the two differ by 6.9e-8.
        ("1/999963365", "1/999963296"),
        # Not measured, and a consequence of evaluating the fraction rather than a case
        # anybody reported: the same value written two ways is the same value.
        ("1/100", "0.01"),
    ],
)
def test_values_match_tolerates_re_approximation(before: object, after: object) -> None:
    """The re-approximation the gate exists to survive, in its printed form."""
    assert _values_match(before, after)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        # A real move, not arithmetic: ten metres up.
        ("339.569 m", "350.0 m"),
        # Same number, different unit — metres are not feet.
        ("339.569 m", "339.569 ft"),
        # Well outside the tolerance in the last digits that matter.
        ("6734.006734", "6734.1"),
        ("Canon", "Nikon"),
        ("48.2082", ""),
        (48.2082, "north"),
        # Every exposure time a camera can write. Two integer denominators only land inside
        # the tolerance once they are past a million, so a real change of shutter speed is
        # still a finding — which is what makes evaluating the fraction safe at all.
        ("1/100", "1/101"),
        ("1/8000", "1/7999"),
        ("1/2", "1/3"),
        # A denominator of zero is not a number, and must not raise on the way to saying so.
        ("1/0", "1/2"),
        # Two dates in a free-text caption. They are only equal if a fraction is allowed to
        # carry '/2026' along as a unit, which is why it may not.
        ("4/2/2026", "2/1/2026"),
    ],
)
def test_values_match_still_reports_a_real_difference(before: object, after: object) -> None:
    """Tolerating arithmetic must not tolerate an edit, a unit change or free text."""
    assert not _values_match(before, after)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        # Measured on a live instance on 2026-08-26: exiftool wrote an explicit zero offset
        # onto IPTC:TimeCreated and IPTC:DigitalCreationTime, failing 92 jobs in one
        # backfill run. Same clock, same displayed value.
        ("11:24:38", "11:24:38+00:00"),
        # Symmetric: dropping the explicit zero offset is the same non-change.
        ("11:24:38+00:00", "11:24:38"),
        # 'Z' is the same zero offset spelled differently.
        ("11:24:38", "11:24:38Z"),
        ("11:24:38Z", "11:24:38+00:00"),
        ("11:24:38", "11:24:38-00:00"),
        # A non-zero offset that is written on both sides is not a difference either.
        ("15:46:30+01:00", "15:46:30+01:00"),
        # The date-time form takes the same rule. It is not the form that was measured, but
        # it is the same printed value with a date in front of it.
        ("2026:08:25 15:46:30", "2026:08:25 15:46:30+00:00"),
        # Sub-second precision survives the split into clock and offset.
        ("11:24:38.25", "11:24:38.25+00:00"),
    ],
)
def test_values_match_tolerates_an_added_zero_utc_offset(before: object, after: object) -> None:
    """The offset exiftool adds to a time that carried none is a representation change."""
    assert _values_match(before, after)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        # An offset that is not zero moves the time. Adding one is a real change.
        ("15:46:30", "15:46:30+01:00"),
        ("15:46:30+01:00", "15:46:30"),
        # Two explicit offsets that disagree, whichever way round.
        ("15:46:30+01:00", "15:46:30+02:00"),
        ("15:46:30+01:00", "15:46:30-01:00"),
        # The clock itself, one second apart, offsets or not.
        ("15:46:30", "15:46:31"),
        ("15:46:30+00:00", "15:46:31"),
        # The date in front of an identical clock.
        ("2026:08:25 15:46:30", "2026:08:26 15:46:30"),
        # A bare number must stay a number: '11' is not 11:00:00 with anything appended.
        ("11", "11:00"),
    ],
)
def test_values_match_still_reports_a_real_time_difference(before: object, after: object) -> None:
    """Tolerating a zero offset must not tolerate a real one, or a moved clock."""
    assert not _values_match(before, after)


def test_metadata_diff_ignores_only_the_listed_tags() -> None:
    """The ignore list passes what it names, and the gate still reports everything else.

    `XMP:Orientation` is the entry this test was written for. Measured on a live instance on
    2026-08-26: 'Rotate 270 CW' -> 'Horizontal (normal)', after `-auto-orient` had baked the
    rotation into the pixels and `normalize_orientation` pinned the tag.
    """
    before: dict[str, object] = {
        "EXIF:Orientation": "Rotate 90 CW",
        "XMP:Orientation": "Rotate 270 CW",
        "XMP:XMPToolkit": "Image::ExifTool 12.76",
        "XMP:Rating": 4,
        "IPTC:TimeCreated": "11:24:38",
    }
    after: dict[str, object] = {
        "EXIF:Orientation": "Horizontal (normal)",
        "XMP:Orientation": "Horizontal (normal)",
        "XMP:XMPToolkit": "Image::ExifTool 13.25",
        "XMP:Rating": 4,
        "IPTC:TimeCreated": "11:24:38+00:00",
    }
    assert _diff_metadata(before, after) == []

    # A tag outside the list is reported by both routes it can fail.
    assert _diff_metadata({**before, "XMP:Label": "keep"}, after) == ["XMP:Label lost"]
    assert _diff_metadata(before, {**after, "XMP:Rating": 3}) == ["XMP:Rating changed: 4 -> 3"]


@needs_still_tools
async def test_metadata_survives_the_production_path(tmp_path: Path) -> None:
    """The hard requirement, asserted end to end rather than trusted.

    Orientation is the single expected difference: `-auto-orient` bakes the rotation into
    the pixels and `normalize_orientation` pins the tag to 1, which is exactly why it is
    the only entry on the ignore list.
    """
    source = await _make_still(tmp_path / "in.jpg", orientation=6)
    code, _, stderr = await run_command(
        [
            "exiftool",
            "-quiet",
            "-overwrite_original",
            "-GPSLatitude=48.2082",
            "-GPSLatitudeRef=N",
            "-GPSLongitude=16.3738",
            "-GPSLongitudeRef=E",
            "-Artist=A. Krichmayr",
            "-Copyright=(c) 2024",
            "-XMP:Rating=4",
            "-XMP:Subject=urlaub",
            "-IPTC:City=Wien",
            str(source),
        ],
        timeout_s=60,
    )
    assert code == 0, stderr

    work = tmp_path / "work"
    work.mkdir()
    preset = Preset(
        name="image-jpeg",
        type="IMAGE",
        extensions=[".jpg"],
        cmd="magick {input} -auto-orient -quality 82 -interlace Plane {output}",
        suffix=".jpg",
        exiftool_copy=True,
        normalize_orientation=True,
        timeout_s=300,
    )
    result = await encode(source, preset, work)
    assert await verify_metadata(source, result.output_path) == []


@needs_still_tools
async def test_metadata_gate_reports_a_lost_tag(tmp_path: Path) -> None:
    """`-strip` is the failure this gate exists for: the picture is fine, the data is gone."""
    source = await _make_still(tmp_path / "in.jpg", orientation=1)
    work = tmp_path / "work"
    work.mkdir()
    stripping = Preset(
        name="strips-everything",
        type="IMAGE",
        cmd="magick {input} -strip -quality 82 {output}",
        suffix=".jpg",
        timeout_s=300,
    )
    result = await encode(source, stripping, work)
    differences = await verify_metadata(source, result.output_path)
    assert differences, "a stripped output must not pass as complete"
    assert any("Make" in entry for entry in differences)


def _camera_still_preset() -> Preset:
    """The shipped IMAGE preset, so these tests exercise the production recipe."""
    return Preset(
        name="image-jpeg",
        type="IMAGE",
        extensions=[".jpg"],
        cmd="magick {input} -auto-orient -quality 82 -interlace Plane {output}",
        suffix=".jpg",
        exiftool_copy=True,
        normalize_orientation=True,
        timeout_s=300,
    )


@needs_still_tools
async def test_metadata_gate_survives_rational_re_encoding(tmp_path: Path) -> None:
    """Regression: a real camera JPEG failed the gate on arithmetic, not on loss.

    EXIF stores rationals, and copying a tag re-approximates the fraction. Measured on the
    phone JPEG that produced this test, through the shipped preset:

        ExposureTime  2497831/250000000 -> 1/100          (prints "1/100" either way)
        GPSLatitude   ...16316639/1000000 -> 39421/2416    (prints 48 deg 18' 16.32")
        ThumbnailOffset       1008 -> 1026                 (a file position, not content)

    Comparing exiftool's ``-n`` floats made every one of those a difference, so no
    geotagged photo could pass a strict gate. The values a viewer is shown are identical.
    """
    source = await _make_still(tmp_path / "in.jpg", orientation=6)
    thumb = tmp_path / "thumb.jpg"
    code, _, stderr = await run_command(
        ["magick", "-size", "160x120", "gradient:red-blue", "-quality", "70", str(thumb)],
        timeout_s=120,
    )
    assert code == 0, stderr
    code, _, stderr = await run_command(
        [
            "exiftool",
            "-quiet",
            "-overwrite_original",
            # Awkward on purpose: enough decimals that exiftool cannot store the value
            # exactly and has to pick a fraction, which the copy then picks differently.
            "-GPSLatitude=48.3045323997222",
            "-GPSLatitudeRef=N",
            "-GPSLongitude=14.2868721",
            "-GPSLongitudeRef=E",
            "-ExposureTime=0.009991324",
            "-ApertureValue=1.69",
            "-MaxApertureValue=1.69",
            "-Model=SM-G990B",
            f"-ThumbnailImage<={thumb}",
            str(source),
        ],
        timeout_s=120,
    )
    assert code == 0, stderr

    work = tmp_path / "work"
    work.mkdir()
    result = await encode(source, _camera_still_preset(), work)
    assert await verify_metadata(source, result.output_path) == []


@needs_still_tools
async def test_metadata_gate_reports_a_changed_value(tmp_path: Path) -> None:
    """The other half of the regression: tolerating re-encoding must not tolerate edits.

    A gate that passes the file above has to keep failing one where a tag really moved.
    """
    source = await _make_still(tmp_path / "in.jpg", orientation=6)
    code, _, stderr = await run_command(
        [
            "exiftool",
            "-quiet",
            "-overwrite_original",
            "-GPSLatitude=48.3045323997222",
            "-GPSLatitudeRef=N",
            "-Model=SM-G990B",
            str(source),
        ],
        timeout_s=120,
    )
    assert code == 0, stderr

    work = tmp_path / "work"
    work.mkdir()
    result = await encode(source, _camera_still_preset(), work)
    assert await verify_metadata(source, result.output_path) == []

    # A camera model that is not the source's, and a latitude 1.3 degrees away.
    code, _, stderr = await run_command(
        [
            "exiftool",
            "-quiet",
            "-overwrite_original",
            "-Model=Not The Real Camera",
            "-GPSLatitude=47.0",
            str(result.output_path),
        ],
        timeout_s=120,
    )
    assert code == 0, stderr

    differences = await verify_metadata(source, result.output_path)
    assert any("Model" in entry for entry in differences), differences
    assert any("GPSLatitude" in entry for entry in differences), differences


# -------------------------------------------------------------------- source quality


@needs_still_tools
async def test_jpeg_quality_reads_the_source(tmp_path: Path) -> None:
    source = await _make_still(tmp_path / "in.jpg")  # written at -quality 95
    assert await jpeg_quality(source) == 95


@needs_still_tools
async def test_sanity_rejects_a_result_below_min_savings(tmp_path: Path) -> None:
    """The economic gate: a good ratio on a small file is still not worth an asset."""
    source = await _make_still(tmp_path / "in.jpg", orientation=1)
    work = tmp_path / "work"
    work.mkdir()
    preset = Preset(
        name="image-jpeg",
        type="IMAGE",
        cmd="magick {input} -auto-orient -quality 40 -interlace Plane {output}",
        suffix=".jpg",
        exiftool_copy=True,
        normalize_orientation=True,
        min_savings_bytes=100 * 1024 * 1024,
        timeout_s=300,
    )
    source_probe = await probe(source, is_still=True)
    result = await encode(source, preset, work)
    sanity = await check_sanity(
        source=source,
        result=result,
        source_probe=source_probe,
        behavior=BehaviorSettings(work_dir=work, max_ratio=1.0),
        preset=preset,
        is_video=False,
    )
    assert not sanity.ok
    assert any("min_savings_bytes" in failure for failure in sanity.failures)


# ------------------------------------------------------------------- the capture date


def _probe_of(**overrides: object) -> MediaProbe:
    """A probe of an ordinary 1080p clip, with the one field under test swapped."""
    fields: dict[str, object] = {
        "width": 1920,
        "height": 1080,
        "duration_s": 10.0,
        "video_streams": 1,
        "audio_streams": 1,
        "has_date_time_original": True,
    }
    return MediaProbe(**{**fields, **overrides})  # type: ignore[arg-type]


def _result_of(output_probe: MediaProbe) -> EncodeResult:
    """A result that clears every other gate: a real 0.2 ratio on a 10 MB source."""
    return EncodeResult(
        output_path=Path("/nonexistent/out.mp4"),
        orig_bytes=10_000_000,
        new_bytes=2_000_000,
        probe=output_probe,
        checksum="c2hh",
    )


async def test_the_capture_date_gate_fires_when_the_encode_loses_the_date(
    behavior: BehaviorSettings, h265_preset: Preset
) -> None:
    """The loss the gate exists for: the source had a capture date, the output does not."""
    sanity = await check_sanity(
        source=Path("/nonexistent/in.mp4"),
        result=_result_of(_probe_of(has_date_time_original=False)),
        source_probe=_probe_of(has_date_time_original=True),
        behavior=behavior,
        preset=h265_preset,
        is_video=True,
    )
    assert not sanity.ok
    assert any("lost the capture date" in failure for failure in sanity.failures)


async def test_a_source_without_a_capture_date_is_judged_on_everything_else(
    behavior: BehaviorSettings, h265_preset: Preset
) -> None:
    """A clip that never had a `creation_time` could not pass this gate at any quality.

    That is every screen recording, messenger video, drone export and cut file in a
    library — and the failure pointed at the *output*, so the search for the cause started
    in the encoder. An output cannot lose what the input never carried.
    """
    sanity = await check_sanity(
        source=Path("/nonexistent/screen-recording.mp4"),
        result=_result_of(_probe_of(has_date_time_original=False)),
        source_probe=_probe_of(has_date_time_original=False),
        behavior=behavior,
        preset=h265_preset,
        is_video=True,
    )
    assert sanity.ok, sanity.reason()


async def test_the_capture_date_gate_still_answers_to_its_setting(
    behavior: BehaviorSettings, h265_preset: Preset
) -> None:
    """`require_date_time_original: false` turns the check off, source or no source."""
    sanity = await check_sanity(
        source=Path("/nonexistent/in.mp4"),
        result=_result_of(_probe_of(has_date_time_original=False)),
        source_probe=_probe_of(has_date_time_original=True),
        behavior=behavior.model_copy(update={"require_date_time_original": False}),
        preset=h265_preset,
        is_video=True,
    )
    assert sanity.ok, sanity.reason()
