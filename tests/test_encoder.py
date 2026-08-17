"""Encoder + sanity gate. Uses real ffmpeg against tiny generated clips."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from immich_compressor.config import BehaviorSettings, ConfigError, Preset
from immich_compressor.encoder import (
    EncodeError,
    check_sanity,
    compressed_filename,
    encode,
    has_free_space,
    probe,
    probe_exif,
    probe_hardware_encoder,
    run_command,
)

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)

needs_still_tools = pytest.mark.skipif(
    shutil.which("convert") is None or shutil.which("exiftool") is None,
    reason="imagemagick/exiftool not installed",
)


async def _make_clip(path: Path, *, seconds: int = 2, size: str = "320x240", bitrate: str = "4000k") -> Path:
    code, _, stderr = await run_command(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=15:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-c:v", "mpeg4", "-b:v", bitrate, "-c:a", "aac", "-b:a", "128k",
            "-shortest", "-metadata", "creation_time=2024-06-15T12:30:00Z",
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
            "ffmpeg", "-y", "-loglevel", "error",
            "-display_rotation", str(degrees), "-i", str(source),
            "-c", "copy", "-map_metadata", "0", "-movflags", "use_metadata_tags",
            str(target),
        ],
        timeout_s=180,
    )
    assert code == 0, stderr
    return target


async def _make_still(path: Path, *, orientation: int = 6, size: str = "1200x800") -> Path:
    """A JPEG whose EXIF says "rotate me", with the metadata a real photo carries."""
    code, _, stderr = await run_command(
        ["convert", "-size", size, "gradient:red-blue", "-quality", "95", str(path)],
        timeout_s=120,
    )
    assert code == 0, stderr
    code, _, stderr = await run_command(
        [
            "exiftool", "-quiet", "-overwrite_original",
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
    return BehaviorSettings(work_dir=tmp_path / "work", max_ratio=0.6)


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
        is_video=True,
    )
    assert sanity.ok, sanity.reason()


async def test_sanity_rejects_when_there_is_no_gain(
    tmp_path: Path, behavior: BehaviorSettings
) -> None:
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
        is_video=True,
    )
    assert not sanity.ok
    assert any("no gain" in failure for failure in sanity.failures)


async def test_sanity_rejects_resolution_change(
    tmp_path: Path, behavior: BehaviorSettings
) -> None:
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
        is_video=True,
    )
    assert not sanity.ok
    assert any("display size changed" in failure for failure in sanity.failures)


async def test_sanity_rejects_a_bit_depth_drop(tmp_path: Path, behavior: BehaviorSettings) -> None:
    source = tmp_path / "10bit.mp4"
    code, _, stderr = await run_command(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=15:duration=1",
            "-c:v", "libx265", "-preset", "ultrafast", "-crf", "20",
            "-pix_fmt", "yuv420p10le", "-x265-params", "log-level=none:pools=2",
            "-threads", "2", "-metadata", "creation_time=2024-06-15T12:30:00Z",
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
        name="image-magick",
        type="IMAGE",
        cmd="convert {input} -auto-orient -quality 82 -sampling-factor 4:2:0 {output}",
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
        behavior=BehaviorSettings(work_dir=work, max_ratio=1.0),
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
        cmd="convert {input} -auto-orient -quality 82 -sampling-factor 4:2:0 {output}",
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
        behavior=BehaviorSettings(work_dir=work, max_ratio=1.0),
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
            cmd="convert {input} -quality 82 {output}",
            suffix=".jpg",
            exiftool_copy=True,
            normalize_orientation=True,
        )


def test_normalize_orientation_needs_the_metadata_copy() -> None:
    with pytest.raises(ConfigError, match="exiftool_copy"):
        Preset(
            name="bad",
            type="IMAGE",
            cmd="convert {input} -auto-orient -quality 82 {output}",
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
