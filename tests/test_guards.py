"""Guard logic: every one of these must abandon the job without touching the server."""

from __future__ import annotations

from typing import Any

import pytest

from immich_compressor.config import Settings
from immich_compressor.models import SkipReason, WebhookAsset, WebhookPayload
from immich_compressor.pipeline import MARKER_VERSION, SkipJob, build_marker, check_guards


def _asset(raw: dict[str, Any], **overrides: Any) -> WebhookAsset:
    raw = {**raw}
    raw["data"] = {**raw["data"], "asset": {**raw["data"]["asset"], **overrides}}
    return WebhookPayload.model_validate(raw).data.asset


def test_clean_video_passes(video_payload_raw: dict[str, Any], settings: Settings) -> None:
    check_guards(_asset(video_payload_raw), settings)  # must not raise


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"isExternal": True}, SkipReason.EXTERNAL_LIBRARY),
        ({"libraryId": "11111111-1111-4111-8111-111111111111"}, SkipReason.EXTERNAL_LIBRARY),
        ({"isEdited": True}, SkipReason.EDITED),
        ({"livePhotoVideoId": "22222222-2222-4222-8222-222222222222"}, SkipReason.LIVE_PHOTO),
        ({"visibility": "locked"}, SkipReason.LOCKED),
        ({"status": "trashed"}, SkipReason.TRASHED),
        ({"deletedAt": "2026-01-01T00:00:00.000Z"}, SkipReason.TRASHED),
        ({"type": "IMAGE"}, SkipReason.WRONG_TYPE),
        ({"originalFileName": "clip.cmp.mp4"}, SkipReason.ALREADY_COMPRESSED),
    ],
)
def test_guard_rejects(
    video_payload_raw: dict[str, Any],
    settings: Settings,
    overrides: dict[str, Any],
    expected: SkipReason,
) -> None:
    with pytest.raises(SkipJob) as excinfo:
        check_guards(_asset(video_payload_raw, **overrides), settings)
    assert excinfo.value.reason is expected


def test_too_small_is_skipped(video_payload_raw: dict[str, Any], settings: Settings) -> None:
    asset_raw = {**video_payload_raw}
    asset_raw["data"]["asset"]["exifInfo"] = {
        **asset_raw["data"]["asset"]["exifInfo"],
        "fileSizeInByte": 10,
    }
    with pytest.raises(SkipJob) as excinfo:
        check_guards(WebhookPayload.model_validate(asset_raw).data.asset, settings)
    assert excinfo.value.reason is SkipReason.TOO_SMALL


def test_no_preset_is_skipped(video_payload_raw: dict[str, Any], settings: Settings) -> None:
    settings.behavior.enabled_types = ["VIDEO", "IMAGE"]
    with pytest.raises(SkipJob) as excinfo:
        check_guards(_asset(video_payload_raw, type="IMAGE"), settings)
    assert excinfo.value.reason is SkipReason.NO_PRESET


def test_marker_shape() -> None:
    marker = build_marker(source_id="src", new_id="dst", preset_name="video-h265", ratio=0.4123)
    assert marker.key == "compressor"
    assert marker.value["sourceId"] == "src"
    assert marker.value["replacedBy"] == "dst"
    assert marker.value["ratio"] == 0.4123
    assert marker.value["v"] == MARKER_VERSION
    assert isinstance(marker.value["at"], str)


def test_marker_without_replacement_has_no_replaced_by() -> None:
    marker = build_marker(source_id="src", new_id=None, preset_name="p", ratio=None)
    assert "replacedBy" not in marker.value
    assert "ratio" not in marker.value


# ------------------------------------------------------------------- format allowlist


def _with_image_preset(settings: Settings) -> Settings:
    """The shipped JPEG-only image preset alongside the video one."""
    from immich_compressor.config import Preset

    settings.behavior.enabled_types = ["VIDEO", "IMAGE"]
    settings.presets = [
        *settings.presets,
        Preset(
            name="image-jpeg",
            type="IMAGE",
            extensions=[".jpg", ".jpeg"],
            cmd="magick {input} -auto-orient -quality 82 {output}",
            suffix=".jpg",
            exiftool_copy=True,
            normalize_orientation=True,
        ),
    ]
    return settings


def test_jpeg_passes_the_allowlist(
    image_payload_raw: dict[str, Any], settings: Settings
) -> None:
    check_guards(
        _asset(image_payload_raw, originalFileName="holiday.jpg", exifInfo={"fileSizeInByte": 5_000_000}),
        _with_image_preset(settings),
    )  # must not raise


@pytest.mark.parametrize("filename", ["raw.dng", "raw.CR2", "shot.nef", "screen.png", "anim.gif"])
def test_non_jpeg_stills_are_skipped_as_unsupported(
    image_payload_raw: dict[str, Any], settings: Settings, filename: str
) -> None:
    """A RAW that reaches the encoder is developed to 8-bit and loses its sensor data.

    It must be rejected as UNSUPPORTED_FORMAT, not NO_PRESET: the type *is* covered, and
    the two reasons mean different things when reading a report.
    """
    with pytest.raises(SkipJob) as excinfo:
        check_guards(
            _asset(
                image_payload_raw,
                originalFileName=filename,
                exifInfo={"fileSizeInByte": 40_000_000},
            ),
            _with_image_preset(settings),
        )
    assert excinfo.value.reason is SkipReason.UNSUPPORTED_FORMAT


def test_min_savings_is_the_pre_download_filter(
    video_payload_raw: dict[str, Any], settings: Settings
) -> None:
    """A file smaller than min_savings_bytes provably cannot pass the gate after encoding."""
    settings.behavior.min_savings_bytes = 1024 * 1024
    with pytest.raises(SkipJob) as excinfo:
        check_guards(
            _asset(video_payload_raw, exifInfo={"fileSizeInByte": 1024 * 1024 - 1}), settings
        )
    assert excinfo.value.reason is SkipReason.TOO_SMALL

    check_guards(
        _asset(video_payload_raw, exifInfo={"fileSizeInByte": 1024 * 1024}), settings
    )  # exactly at the threshold is allowed through
