"""Guard logic: every one of these must abandon the job without touching the server.

The last section covers the ingest gate, which runs one step earlier still — in the
webhook handler, before a job row exists at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from immich_compressor.config import Settings
from immich_compressor.ingest import SurgeDetector, WebhookRejected, check_ingest_guards
from immich_compressor.models import RejectReason, SkipReason, WebhookAsset, WebhookPayload
from immich_compressor.pipeline import (
    MARKER_VERSION,
    SkipJob,
    build_marker,
    check_guards,
)


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


def test_jpeg_passes_the_allowlist(image_payload_raw: dict[str, Any], settings: Settings) -> None:
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
        check_guards(_asset(video_payload_raw, exifInfo={"fileSizeInByte": 1024 * 1024 - 1}), settings)
    assert excinfo.value.reason is SkipReason.TOO_SMALL

    check_guards(
        _asset(video_payload_raw, exifInfo={"fileSizeInByte": 1024 * 1024}), settings
    )  # exactly at the threshold is allowed through


# ------------------------------------------------- the ingest gate (before a job exists)
#
# Immich's `AssetMetadataExtraction` trigger is a maintenance operation: one click on
# Administration -> Jobs -> Extract Metadata re-fires the workflow for every asset in the
# library. `createdAt` dates the upload, so it is what separates a real upload from a
# re-trigger.

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _ingest(raw: dict[str, Any], settings: Settings, **overrides: Any) -> None:
    check_ingest_guards(_asset(raw, **overrides), settings.behavior, now=NOW)


def test_a_fresh_upload_passes_the_ingest_gate(video_payload_raw: dict[str, Any], settings: Settings) -> None:
    """The live capture put 285 ms between the upload and the webhook."""
    just_now = (NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    _ingest(video_payload_raw, settings, createdAt=just_now)  # must not raise


def test_an_asset_that_predates_the_window_is_refused_as_a_re_trigger(
    video_payload_raw: dict[str, Any], settings: Settings
) -> None:
    month_old = (NOW - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    with pytest.raises(WebhookRejected) as caught:
        _ingest(video_payload_raw, settings, createdAt=month_old)
    assert caught.value.reason is RejectReason.TOO_OLD
    # The operator has to be told what to do instead, or they will reach for the button again.
    assert "backfill" in caught.value.detail


def test_the_boundary_is_inclusive(video_payload_raw: dict[str, Any], settings: Settings) -> None:
    """Exactly at the limit is still a pass: a big video can sit behind a busy queue."""
    settings.behavior.max_asset_age_hours = 24.0
    on_the_line = (NOW - timedelta(hours=24)).isoformat().replace("+00:00", "Z")
    _ingest(video_payload_raw, settings, createdAt=on_the_line)  # must not raise

    over_the_line = (NOW - timedelta(hours=24, minutes=1)).isoformat().replace("+00:00", "Z")
    with pytest.raises(WebhookRejected):
        _ingest(video_payload_raw, settings, createdAt=over_the_line)


def test_a_clock_skewed_future_timestamp_passes(
    video_payload_raw: dict[str, Any], settings: Settings
) -> None:
    """A negative age is not old. Refusing it would strand uploads on a skewed host."""
    tomorrow = (NOW + timedelta(hours=6)).isoformat().replace("+00:00", "Z")
    _ingest(video_payload_raw, settings, createdAt=tomorrow)  # must not raise


def test_a_payload_without_created_at_is_refused(
    video_payload_raw: dict[str, Any], settings: Settings
) -> None:
    """Fail closed: unable to tell means no, for something that deletes originals."""
    with pytest.raises(WebhookRejected) as caught:
        _ingest(video_payload_raw, settings, createdAt=None)
    assert caught.value.reason is RejectReason.NO_CREATED_AT


@pytest.mark.parametrize("created_at", ["2020-01-01T00:00:00.000Z", None])
def test_the_gate_can_be_turned_off(
    video_payload_raw: dict[str, Any], settings: Settings, created_at: str | None
) -> None:
    """`null` accepts any age — including a payload with no `createdAt` at all."""
    settings.behavior.max_asset_age_hours = None
    _ingest(video_payload_raw, settings, createdAt=created_at)  # must not raise


# ----------------------------------------------------------------- the surge detector


def test_the_detector_stays_quiet_up_to_the_threshold() -> None:
    detector = SurgeDetector(threshold=3, window_seconds=600)
    assert [detector.record(now=NOW) for _ in range(3)] == [None, None, None]


def test_the_detector_trips_on_the_one_over() -> None:
    detector = SurgeDetector(threshold=3, window_seconds=600)
    for _ in range(3):
        detector.record(now=NOW)
    assert detector.record(now=NOW) == 4


def test_a_trip_is_reported_once_not_on_every_later_webhook() -> None:
    """The caller latches on the first report; repeating it would only spam the log."""
    detector = SurgeDetector(threshold=2, window_seconds=600)
    for _ in range(3):
        detector.record(now=NOW)
    assert detector.record(now=NOW) is None


def test_arrivals_outside_the_window_do_not_count() -> None:
    """Rate, not total. A steady trickle must never add up to a surge."""
    detector = SurgeDetector(threshold=3, window_seconds=600)
    for minutes in (0, 20, 40, 60):
        assert detector.record(now=NOW + timedelta(minutes=minutes)) is None


def test_the_detector_can_be_turned_off() -> None:
    detector = SurgeDetector(threshold=None, window_seconds=600)
    assert all(detector.record(now=NOW) is None for _ in range(1000))
