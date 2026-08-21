"""The payload models are parsed against real captures, not hand-written samples."""

from __future__ import annotations

from typing import Any

import pytest

from immich_compressor.models import AssetDetail, UpdateAssetFields, WebhookPayload


def test_video_capture_parses(video_payload_raw: dict[str, Any]) -> None:
    payload = WebhookPayload.model_validate(video_payload_raw)
    asset = payload.data.asset

    assert payload.type == "AssetV1"
    assert payload.trigger == "AssetMetadataExtraction"
    assert asset.type == "VIDEO"
    assert asset.original_file_name.endswith(".mp4")
    # Verified against the live instance: duration is integer milliseconds, and
    # POST /assets expects the very same unit.
    assert asset.duration == 20000
    assert asset.exif_info.tags == ["urlaub", "wien"]
    assert asset.exif_info.rating == 4
    assert asset.exif_info.latitude is not None
    assert asset.exif_info.file_size_in_byte is not None


def test_image_capture_parses(image_payload_raw: dict[str, Any]) -> None:
    asset = WebhookPayload.model_validate(image_payload_raw).data.asset
    assert asset.type == "IMAGE"
    assert asset.duration is None
    assert asset.exif_info.exif_image_width == 3000
    assert asset.exif_info.description == "Compressor test photo Vienna"


def test_buffer_checksum_is_tolerated(video_payload_raw: dict[str, Any]) -> None:
    """`checksum` arrives as {"type":"Buffer","data":[...]}. We must not choke on it."""
    assert video_payload_raw["data"]["asset"]["checksum"]["type"] == "Buffer"
    WebhookPayload.model_validate(video_payload_raw)  # must not raise


def test_unknown_fields_are_ignored(video_payload_raw: dict[str, Any]) -> None:
    video_payload_raw["data"]["asset"]["someFutureField"] = {"a": 1}
    video_payload_raw["someTopLevelAddition"] = True
    WebhookPayload.model_validate(video_payload_raw)  # must not raise


def test_null_tags_are_accepted(video_payload_raw: dict[str, Any]) -> None:
    """Regression: Immich sends `exifInfo.tags: null` for an asset without tags.

    A pydantic default only fills a *missing* key, so this used to fail validation and
    the webhook silently answered 422 for every untagged asset.
    """
    video_payload_raw["data"]["asset"]["exifInfo"]["tags"] = None
    asset = WebhookPayload.model_validate(video_payload_raw).data.asset
    assert asset.exif_info.tags == []


def test_null_exif_info_is_accepted(video_payload_raw: dict[str, Any]) -> None:
    """`exifInfo` is null until metadata extraction has run."""
    video_payload_raw["data"]["asset"]["exifInfo"] = None
    asset = WebhookPayload.model_validate(video_payload_raw).data.asset
    assert asset.exif_info.file_size_in_byte is None
    assert asset.exif_info.tags == []


@pytest.mark.parametrize("field", ["originalFileName", "isFavorite", "isExternal", "isEdited", "isOffline"])
def test_explicit_nulls_fall_back_to_defaults(video_payload_raw: dict[str, Any], field: str) -> None:
    video_payload_raw["data"]["asset"][field] = None
    WebhookPayload.model_validate(video_payload_raw)  # must not raise


def test_asset_detail_tolerates_nulls() -> None:
    detail = AssetDetail.model_validate(
        {
            "id": "a1",
            "type": "VIDEO",
            "people": None,
            "tags": None,
            "exifInfo": None,
            "isFavorite": None,
            "originalFileName": None,
        }
    )
    assert detail.people == []
    assert detail.tags == []
    assert detail.named_people() == []
    assert detail.is_favorite is False


def test_update_fields_omits_none() -> None:
    fields = UpdateAssetFields(description="hello", latitude=1.5)
    body = fields.to_body()
    assert body == {"description": "hello", "latitude": 1.5}
    assert not UpdateAssetFields().to_body()
    assert UpdateAssetFields().is_empty()


def test_update_fields_serialises_camel_case() -> None:
    body = UpdateAssetFields(date_time_original="2024-06-15T12:30:00+00:00", is_favorite=True).to_body()
    assert body == {"dateTimeOriginal": "2024-06-15T12:30:00+00:00", "isFavorite": True}
