"""Encoder + sanity gate. Uses real ffmpeg against tiny generated clips."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from immich_compressor.config import BehaviorSettings, Preset
from immich_compressor.encoder import (
    EncodeError,
    check_sanity,
    compressed_filename,
    encode,
    has_free_space,
    probe,
    run_command,
)

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
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
    assert any("resolution changed" in failure for failure in sanity.failures)


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
