"""Immich client against respx mocks. Request shapes match the live v3.1.0 API."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from immich_compressor.api import ImmichClient, ImmichError, format_timestamp, sanitize_rating
from immich_compressor.models import MetadataItem, UpdateAssetFields

BASE = "http://immich-test:2283/api"


@pytest.fixture
async def client() -> ImmichClient:
    api_client = ImmichClient(BASE, "test-key", timeout_s=5, max_retries=2)
    yield api_client
    await api_client.aclose()


def test_format_timestamp_matches_the_openapi_pattern() -> None:
    value = datetime(2024, 6, 15, 12, 30, 0, 123456, tzinfo=UTC)
    assert format_timestamp(value) == "2024-06-15T12:30:00.123Z"


def test_format_timestamp_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="naive"):
        format_timestamp(datetime(2024, 6, 15, 12, 30))  # noqa: DTZ001


@pytest.mark.parametrize(
    ("value", "expected"),
    # Immich v3 answers HTTP 400 for a rating of 0 — verified against the live server.
    [(0, None), (None, None), (-1, -1), (1, 1), (5, 5), (6, None), (-2, None)],
)
def test_sanitize_rating(value: int | None, expected: int | None) -> None:
    assert sanitize_rating(value) == expected


@respx.mock
async def test_api_key_header_is_sent(client: ImmichClient) -> None:
    route = respx.get(f"{BASE}/server/ping").mock(return_value=httpx.Response(200, json={"res": "pong"}))
    assert await client.ping() is True
    assert route.calls.last.request.headers["x-api-key"] == "test-key"


@respx.mock
async def test_has_metadata_key_uses_the_list_endpoint(client: ImmichClient) -> None:
    """GET /assets/{id}/metadata/{key} answers 400 (not 404) for a missing key, so the
    marker check goes through the list endpoint instead."""
    respx.get(f"{BASE}/assets/a1/metadata").mock(
        return_value=httpx.Response(200, json=[{"key": "compressor", "value": {"v": 1}}])
    )
    found = await client.has_metadata_key("a1", "compressor")
    assert found is not None
    assert found.value["v"] == 1
    assert await client.has_metadata_key("a1", "nope") is None


@respx.mock
async def test_put_metadata_sends_typed_json(client: ImmichClient) -> None:
    route = respx.put(f"{BASE}/assets/a1/metadata").mock(
        return_value=httpx.Response(200, json=[{"key": "compressor", "value": {"v": 1}}])
    )
    await client.put_metadata("a1", [MetadataItem(key="compressor", value={"v": 1, "ratio": 0.4})])
    body = route.calls.last.request.content
    # Values keep their JSON types here — unlike the multipart upload form, where
    # everything would arrive as a string.
    assert b'"v":1' in body.replace(b" ", b"")
    assert b'"ratio":0.4' in body.replace(b" ", b"")


@respx.mock
async def test_upload_sends_the_verified_field_names(client: ImmichClient, tmp_path: Path) -> None:
    payload = tmp_path / "clip.cmp.mp4"
    payload.write_bytes(b"binary")
    route = respx.post(f"{BASE}/assets").mock(
        return_value=httpx.Response(201, json={"id": "new-1", "status": "created"})
    )
    when = datetime(2024, 6, 15, 12, 30, tzinfo=UTC)
    result = await client.upload_asset(
        payload,
        filename="clip.cmp.mp4",
        file_created_at=when,
        file_modified_at=when,
        duration_ms=20000,
        is_favorite=True,
        visibility="timeline",
    )
    assert result.id == "new-1"
    assert result.status == "created"

    body = route.calls.last.request.content
    for field in (b"assetData", b"fileCreatedAt", b"fileModifiedAt", b"filename", b"duration"):
        assert field in body
    # v3 dropped these; sending them would be a validation error.
    assert b"deviceAssetId" not in body
    assert b"deviceId" not in body
    # The metadata array is deliberately not part of the multipart body.
    assert b'name="metadata' not in body


@respx.mock
async def test_upload_reports_duplicates(client: ImmichClient, tmp_path: Path) -> None:
    payload = tmp_path / "clip.mp4"
    payload.write_bytes(b"binary")
    respx.post(f"{BASE}/assets").mock(
        return_value=httpx.Response(200, json={"id": "existing-1", "status": "duplicate"})
    )
    result = await client.upload_asset(
        payload,
        filename="clip.mp4",
        file_created_at=datetime.now(UTC),
        file_modified_at=datetime.now(UTC),
    )
    assert result.status == "duplicate"
    assert result.id == "existing-1"


@respx.mock
async def test_copy_asset_body(client: ImmichClient) -> None:
    route = respx.put(f"{BASE}/assets/copy").mock(return_value=httpx.Response(204))
    await client.copy_asset("src", "dst")
    body = route.calls.last.request.content.decode()
    assert '"sourceId":"src"' in body.replace(" ", "")
    assert '"targetId":"dst"' in body.replace(" ", "")
    for flag in ("albums", "favorite", "sharedLinks", "stack", "sidecar"):
        assert flag in body


@respx.mock
async def test_update_asset_skips_empty_bodies(client: ImmichClient) -> None:
    route = respx.put(f"{BASE}/assets/a1").mock(return_value=httpx.Response(200, json={}))
    await client.update_asset("a1", UpdateAssetFields())
    assert route.call_count == 0

    await client.update_asset("a1", UpdateAssetFields(description="hi"))
    assert route.call_count == 1


@respx.mock
async def test_tag_flow(client: ImmichClient) -> None:
    respx.put(f"{BASE}/tags").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "t1", "value": "urlaub", "name": "urlaub"},
                {"id": "t2", "value": "wien", "name": "wien"},
            ],
        )
    )
    assign = respx.put(f"{BASE}/tags/assets").mock(return_value=httpx.Response(200, json={"count": 2}))
    tags = await client.upsert_tags(["urlaub", "wien"])
    count = await client.tag_assets([tag.id for tag in tags], ["a1"])
    assert count == 2
    assert b'"tagIds"' in assign.calls.last.request.content


@respx.mock
async def test_empty_tag_calls_do_not_hit_the_network(client: ImmichClient) -> None:
    route = respx.put(f"{BASE}/tags")
    assert await client.upsert_tags([]) == []
    assert await client.tag_assets([], ["a1"]) == 0
    assert route.call_count == 0


@respx.mock
async def test_delete_is_soft_by_default(client: ImmichClient) -> None:
    route = respx.delete(f"{BASE}/assets").mock(return_value=httpx.Response(204))
    await client.delete_assets(["a1"])
    assert b'"force":false' in route.calls.last.request.content.replace(b" ", b"")


@respx.mock
async def test_download_streams_to_disk(client: ImmichClient, tmp_path: Path) -> None:
    respx.get(f"{BASE}/assets/a1/original").mock(return_value=httpx.Response(200, content=b"x" * 4096))
    target = tmp_path / "orig.mp4"
    written = await client.download_original("a1", target)
    assert written == 4096
    assert target.read_bytes() == b"x" * 4096
    # No stray part file left behind.
    assert not list(tmp_path.glob("*.part"))


@respx.mock
async def test_download_failure_leaves_no_partial_file(client: ImmichClient, tmp_path: Path) -> None:
    respx.get(f"{BASE}/assets/a1/original").mock(return_value=httpx.Response(404))
    target = tmp_path / "orig.mp4"
    with pytest.raises(ImmichError):
        await client.download_original("a1", target)
    assert not target.exists()
    assert not list(tmp_path.glob("*.part"))


@respx.mock
async def test_client_error_is_not_retried(client: ImmichClient) -> None:
    route = respx.get(f"{BASE}/assets/a1").mock(return_value=httpx.Response(403))
    with pytest.raises(ImmichError) as excinfo:
        await client.get_asset("a1")
    assert excinfo.value.status_code == 403
    assert route.call_count == 1


@respx.mock
async def test_server_error_is_retried(client: ImmichClient) -> None:
    route = respx.get(f"{BASE}/assets/a1").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json={"id": "a1", "type": "VIDEO"}),
        ]
    )
    asset = await client.get_asset("a1")
    assert asset.id == "a1"
    assert route.call_count == 2


@respx.mock
async def test_named_people_detection(client: ImmichClient) -> None:
    respx.get(f"{BASE}/assets/a1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "a1",
                "type": "VIDEO",
                "people": [{"id": "p1", "name": "Anna"}, {"id": "p2", "name": ""}],
            },
        )
    )
    asset = await client.get_asset("a1")
    assert asset.named_people() == ["Anna"]
