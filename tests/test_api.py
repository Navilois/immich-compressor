"""Immich client against respx mocks. Request shapes match the live v3.1.0 API."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from immich_compressor.api import (
    ImmichClient,
    ImmichError,
    client_for,
    format_timestamp,
    sanitize_rating,
)
from immich_compressor.config import ImmichSettings
from immich_compressor.models import MetadataItem, UpdateAssetFields

BASE = "http://immich-test:2283/api"


@pytest.fixture
async def client() -> ImmichClient:
    api_client = ImmichClient(BASE, "test-key", timeout_s=5, max_retries=2)
    yield api_client
    await api_client.aclose()


async def test_the_client_factory_carries_both_timeouts_out_of_the_settings() -> None:
    """`immich.connect_timeout_s` used to reach the running service and nothing else.

    Five callers spelled out the client's construction, and the four in the command line
    left this setting off. A deployment that raised it — a slow or distant Immich — still
    got the ten-second default from `check`, `backfill` and `restore`, with nothing to say
    so. There is one factory now, and the transport it configures is where both settings
    become observable.
    """
    built = client_for(ImmichSettings(base_url=BASE, api_key="k", timeout_s=42.0, connect_timeout_s=7.0))
    try:
        assert built._client.timeout == httpx.Timeout(42.0, connect=7.0)
        assert built._client.headers["x-api-key"] == "k"
    finally:
        await built.aclose()


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


@respx.mock
async def test_search_assets_reads_the_paginated_envelope(client: ImmichClient) -> None:
    """v3 answers `{assets: {items, total, nextPage}}`, and `nextPage` arrives as a string."""
    route = respx.post(f"{BASE}/search/metadata").mock(
        return_value=httpx.Response(
            200,
            json={"assets": {"items": [{"id": "a1"}], "total": 51, "count": 1, "nextPage": "2"}},
        )
    )

    page = await client.search_assets(asset_type="VIDEO", page=1, size=50)

    assert (page.items, page.next_page, page.total, page.paged) == ([{"id": "a1"}], 2, 51, True)
    assert json.loads(route.calls[0].request.read()) == {
        "type": "VIDEO",
        "size": 50,
        "page": 1,
        # Without it the guards see no `fileSizeInByte` and every asset looks small enough
        # to be worth compressing.
        "withExif": True,
    }


@respx.mock
async def test_search_assets_survives_a_bare_array(client: ImmichClient) -> None:
    """What `/search/large-assets` answers with. No envelope means no paging information,
    and the caller — not this method — decides when the walk is over."""
    respx.post(f"{BASE}/search/metadata").mock(return_value=httpx.Response(200, json=[{"id": "a1"}]))

    page = await client.search_assets(asset_type="VIDEO")

    assert page.items == [{"id": "a1"}]
    assert page.next_page is None
    assert page.paged is False


@respx.mock
async def test_search_assets_treats_an_unknown_shape_as_an_empty_page(client: ImmichClient) -> None:
    respx.post(f"{BASE}/search/metadata").mock(return_value=httpx.Response(200, json={"albums": {}}))

    page = await client.search_assets(asset_type="VIDEO")

    assert page.items == []
    assert page.paged is False


def _trash_restore(gone: set[str]) -> Callable[[httpx.Request], httpx.Response]:
    """`POST /trash/restore/assets` as measured on v3.1.0.

    One id the server cannot find refuses the whole request with HTTP 400, and the answer
    never says which id it was. An id that is merely untrashed is a no-op that still
    counts.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        ids = json.loads(request.read())["ids"]
        if any(asset_id in gone for asset_id in ids):
            return httpx.Response(400, json={"message": "Not found or no asset.delete access"})
        return httpx.Response(200, json={"count": len(ids)})

    return handler


@respx.mock
async def test_restore_assets_returns_the_servers_own_count(client: ImmichClient) -> None:
    """The caller's arithmetic is not the truth — only the server knows what it moved."""
    respx.post(f"{BASE}/trash/restore/assets").mock(return_value=httpx.Response(200, json={"count": 2}))

    assert await client.restore_assets(["a1", "a2", "a3"]) == 2


@respx.mock
async def test_restore_assets_falls_back_when_there_is_no_body(client: ImmichClient) -> None:
    """A 204 is in `expected`, so it must not turn a successful restore into a zero."""
    respx.post(f"{BASE}/trash/restore/assets").mock(return_value=httpx.Response(204))

    assert await client.restore_assets(["a1", "a2"]) == 2


@respx.mock
async def test_best_effort_restore_sends_one_request_when_no_id_is_missing(client: ImmichClient) -> None:
    """The healthy deployment must not pay for the broken one."""
    route = respx.post(f"{BASE}/trash/restore/assets").mock(side_effect=_trash_restore(set()))

    outcome = await client.restore_assets_best_effort([f"a{index}" for index in range(8)])

    assert (outcome.restored, outcome.missing) == (8, [])
    assert route.call_count == 1


@respx.mock
async def test_best_effort_restore_isolates_the_ids_the_server_lost(client: ImmichClient) -> None:
    """The bug this exists for: one force-deleted original used to cost the whole batch,
    including the one asset that really was in the trash and could have come back."""
    route = respx.post(f"{BASE}/trash/restore/assets").mock(side_effect=_trash_restore({"a2", "a5"}))

    outcome = await client.restore_assets_best_effort([f"a{index}" for index in range(8)])

    assert outcome.restored == 6
    assert sorted(outcome.missing) == ["a2", "a5"]
    # Every id the server still knows was accepted, and no request carried a dead one twice.
    accepted = [
        asset_id
        for call in route.calls
        if call.response.status_code == 200
        for asset_id in json.loads(call.request.read())["ids"]
    ]
    assert sorted(accepted) == ["a0", "a1", "a3", "a4", "a6", "a7"]


@respx.mock
async def test_best_effort_restore_when_every_id_is_gone(client: ImmichClient) -> None:
    """A stage-4 deployment. Nothing comes back, and every id is named as gone rather
    than the run failing on the first one."""
    ids = [f"a{index}" for index in range(4)]
    respx.post(f"{BASE}/trash/restore/assets").mock(side_effect=_trash_restore(set(ids)))

    outcome = await client.restore_assets_best_effort(ids)

    assert outcome.restored == 0
    assert sorted(outcome.missing) == ids


@respx.mock
async def test_best_effort_restore_of_nothing_stays_off_the_network(client: ImmichClient) -> None:
    route = respx.post(f"{BASE}/trash/restore/assets")

    outcome = await client.restore_assets_best_effort([])

    assert (outcome.restored, outcome.missing) == (0, [])
    assert route.call_count == 0


@respx.mock
async def test_best_effort_restore_raises_anything_that_is_not_a_missing_id(client: ImmichClient) -> None:
    """A wrong API key is not evidence about a single asset, and swallowing it would
    report "these originals are gone" about a library that is perfectly intact."""
    respx.post(f"{BASE}/trash/restore/assets").mock(return_value=httpx.Response(401, json={"message": "no"}))

    with pytest.raises(ImmichError) as failed:
        await client.restore_assets_best_effort(["a1", "a2"])

    assert failed.value.status_code == 401


@respx.mock
async def test_best_effort_restore_stops_halving_a_mostly_dead_store(client: ImmichClient) -> None:
    """Halving costs two requests per id once everything in a batch is missing, which is
    exactly what a deployment that has run `delete_mode: permanent` looks like. After the
    first such chunk the rest goes out one id at a time — the floor for isolating them."""
    ids = [f"a{index}" for index in range(12)]
    route = respx.post(f"{BASE}/trash/restore/assets").mock(side_effect=_trash_restore(set(ids)))

    outcome = await client.restore_assets_best_effort(ids, chunk_size=4)

    assert outcome.missing == ids
    # 7 to take the first chunk of four apart (1 + 2 + 4), then 8 singletons. Halving all
    # three chunks would have been 21.
    assert route.call_count == 15
    assert all(len(json.loads(call.request.read())["ids"]) == 1 for call in route.calls[-8:])


@respx.mock
async def test_touch_asset_writes_back_the_current_favorite(client: ImmichClient) -> None:
    """The whole point: change nothing, but make the row's `updateId` move.

    Immich's sync stream only offers assets newer than the client's checkpoint, and
    `updateId` is regenerated by a BEFORE UPDATE row trigger with no WHEN clause — so
    writing a column's own value back is enough. What is *not* enough is an empty body:
    the server skips the UPDATE entirely when every field is undefined.
    """
    asset_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    respx.get(f"{BASE}/assets/{asset_id}").mock(
        return_value=httpx.Response(
            200, json={"id": asset_id, "type": "VIDEO", "isFavorite": True, "checksum": "x="}
        )
    )
    update = respx.put(f"{BASE}/assets/{asset_id}").mock(return_value=httpx.Response(204))

    await client.touch_asset(asset_id)

    assert update.call_count == 1
    body = json.loads(update.calls.last.request.content)
    assert body == {"isFavorite": True}


@respx.mock
async def test_touch_asset_sends_a_field_even_when_false(client: ImmichClient) -> None:
    """`isFavorite: false` is a value, not an absence — an empty body bumps nothing."""
    asset_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    respx.get(f"{BASE}/assets/{asset_id}").mock(
        return_value=httpx.Response(
            200, json={"id": asset_id, "type": "VIDEO", "isFavorite": False, "checksum": "x="}
        )
    )
    update = respx.put(f"{BASE}/assets/{asset_id}").mock(return_value=httpx.Response(204))

    await client.touch_asset(asset_id)

    assert json.loads(update.calls.last.request.content) == {"isFavorite": False}
