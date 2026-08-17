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
