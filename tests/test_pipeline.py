"""Full pipeline against respx mocks, plus the webhook endpoint."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from conftest import aged
from immich_compressor.api import ImmichClient
from immich_compressor.config import Settings
from immich_compressor.encoder import run_command
from immich_compressor.models import Job, JobState, MetadataItem, SkipReason, WebhookPayload
from immich_compressor.pipeline import (
    MARKER_VERSION,
    Pipeline,
    WebhookRejected,
    Worker,
    check_ingest_guards,
    marker_blocks_reprocessing,
)
from immich_compressor.server import create_app
from immich_compressor.store import SHIM_GATES_OPENED, SHIM_TOUCHES, JobStore

BASE = "http://immich-test:2283/api"

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


async def _make_clip(path: Path, *, bitrate: str = "8000k") -> Path:
    code, _, stderr = await run_command(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=15:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
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


async def _seed(store: JobStore, raw: dict[str, Any]) -> Job:
    asset_id = raw["data"]["asset"]["id"]
    await store.enqueue(asset_id, raw, delay_seconds=0)
    job = await store.claim_next()
    assert job is not None
    return job


def _mock_no_marker(asset_id: str) -> None:
    respx.get(f"{BASE}/assets/{asset_id}/metadata").mock(return_value=httpx.Response(200, json=[]))


def _mock_extracted(asset_id: str) -> None:
    """Stand in for a new asset whose metadata extraction has already run."""
    respx.get(f"{BASE}/assets/{asset_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": asset_id,
                "type": "VIDEO",
                "isTrashed": False,
                "people": [],
                "exifInfo": {"dateTimeOriginal": "2024-06-15T12:30:00+00:00"},
            },
        )
    )


def _mock_asset_detail(
    asset_id: str,
    people: list[dict[str, str]] | None = None,
    *,
    checksum: str | None = None,
    owner_id: str | None = None,
) -> None:
    body: dict[str, Any] = {
        "id": asset_id,
        "type": "VIDEO",
        "isTrashed": False,
        "people": people or [],
    }
    if checksum is not None:
        body["checksum"] = checksum
    if owner_id is not None:
        body["ownerId"] = owner_id
    respx.get(f"{BASE}/assets/{asset_id}").mock(return_value=httpx.Response(200, json=body))


# ------------------------------------------------------------------------- dry run


@respx.mock
async def test_dry_run_touches_nothing(
    settings: Settings, video_payload_raw: dict[str, Any], tmp_path: Path
) -> None:
    """The defining property of dry_run: not a single mutating request goes out."""
    settings.behavior.dry_run = True
    asset_id = video_payload_raw["data"]["asset"]["id"]
    _mock_no_marker(asset_id)
    _mock_asset_detail(asset_id)

    upload = respx.post(f"{BASE}/assets")
    copy = respx.put(f"{BASE}/assets/copy")
    update = respx.put(f"{BASE}/assets/{asset_id}")
    delete = respx.delete(f"{BASE}/assets")
    download = respx.get(f"{BASE}/assets/{asset_id}/original")
    metadata_write = respx.put(f"{BASE}/assets/{asset_id}/metadata")

    async with JobStore(settings.database_path) as store:
        client = ImmichClient(BASE, "k")
        pipeline = Pipeline(settings, client, store)
        await pipeline.run_job(await _seed(store, video_payload_raw))
        await client.aclose()

        job = await store.get(asset_id)
        assert job is not None
        assert job.state is JobState.SKIPPED
        assert job.skip_reason is SkipReason.DRY_RUN

    for route in (upload, copy, update, delete, download, metadata_write):
        assert route.call_count == 0


# --------------------------------------------------------------------------- guards


@pytest.mark.parametrize(
    "marker_value",
    [
        pytest.param({"v": MARKER_VERSION}, id="current-version"),
        pytest.param({"v": MARKER_VERSION, "replacedBy": "new-id"}, id="replacement-exists"),
        pytest.param({"v": 1, "replacedBy": "new-id"}, id="old-but-replaced"),
        pytest.param({}, id="unreadable"),
    ],
)
@respx.mock
async def test_existing_marker_stops_the_loop(
    settings: Settings, video_payload_raw: dict[str, Any], marker_value: dict[str, Any]
) -> None:
    asset_id = video_payload_raw["data"]["asset"]["id"]
    respx.get(f"{BASE}/assets/{asset_id}/metadata").mock(
        return_value=httpx.Response(200, json=[{"key": "compressor", "value": marker_value}])
    )
    download = respx.get(f"{BASE}/assets/{asset_id}/original")

    async with JobStore(settings.database_path) as store:
        client = ImmichClient(BASE, "k")
        await Pipeline(settings, client, store).run_job(await _seed(store, video_payload_raw))
        await client.aclose()
        job = await store.get(asset_id)

    assert job is not None
    assert job.skip_reason is SkipReason.ALREADY_COMPRESSED
    assert download.call_count == 0


@respx.mock
async def test_a_marker_from_the_broken_gate_is_re_tried(
    settings: Settings, video_payload_raw: dict[str, Any]
) -> None:
    """v1 rejected every rotated video. Those markers must not block the fixed gate.

    Runs with ``dry_run`` so the assertion is about the guard alone: reaching the dry-run
    stop means the marker check let the job through.
    """
    settings.behavior.dry_run = True
    asset_id = video_payload_raw["data"]["asset"]["id"]
    respx.get(f"{BASE}/assets/{asset_id}/metadata").mock(
        return_value=httpx.Response(
            200,
            json=[{"key": "compressor", "value": {"v": 1, "skipped": "no_gain"}}],
        )
    )
    _mock_asset_detail(asset_id)

    async with JobStore(settings.database_path) as store:
        client = ImmichClient(BASE, "k")
        await Pipeline(settings, client, store).run_job(await _seed(store, video_payload_raw))
        await client.aclose()
        job = await store.get(asset_id)

    assert job is not None
    assert job.skip_reason is SkipReason.DRY_RUN


@pytest.mark.parametrize(
    ("value", "blocks"),
    [
        pytest.param({"v": MARKER_VERSION}, True, id="current-version-gave-up"),
        pytest.param({"v": 1, "replacedBy": "x"}, True, id="replacement-exists"),
        pytest.param({"v": 1}, False, id="stale-gave-up"),
        pytest.param({"v": 1, "skipped": "no_gain"}, False, id="stale-no-gain"),
        pytest.param({}, True, id="no-version"),
        pytest.param({"v": "1"}, True, id="version-not-an-int"),
    ],
)
def test_marker_blocks_reprocessing(value: dict[str, Any], blocks: bool) -> None:
    assert marker_blocks_reprocessing(MetadataItem(key="compressor", value=value)) is blocks


@respx.mock
async def test_named_people_are_left_alone(settings: Settings, video_payload_raw: dict[str, Any]) -> None:
    asset_id = video_payload_raw["data"]["asset"]["id"]
    _mock_no_marker(asset_id)
    _mock_asset_detail(asset_id, people=[{"id": "p1", "name": "Anna"}])
    download = respx.get(f"{BASE}/assets/{asset_id}/original")

    async with JobStore(settings.database_path) as store:
        client = ImmichClient(BASE, "k")
        await Pipeline(settings, client, store).run_job(await _seed(store, video_payload_raw))
        await client.aclose()
        job = await store.get(asset_id)

    assert job is not None
    assert job.skip_reason is SkipReason.NAMED_PEOPLE
    assert download.call_count == 0


# ------------------------------------------------------------------- the ledger


@respx.mock
async def test_a_re_uploaded_original_is_recognised_not_recompressed(
    settings: Settings, video_payload_raw: dict[str, Any]
) -> None:
    """The gap upstream leaves open: a device that still holds the file uploads it again.

    It arrives as a new asset — new id, no compressor marker — so the loop guard cannot
    see it. The ledger can, and it stops the job before the download.
    """
    settings.behavior.dry_run = False
    asset_id = video_payload_raw["data"]["asset"]["id"]
    _mock_no_marker(asset_id)
    _mock_asset_detail(asset_id, checksum="c3Vt", owner_id="user-1")
    download = respx.get(f"{BASE}/assets/{asset_id}/original")
    delete = respx.delete(f"{BASE}/assets")

    async with JobStore(settings.database_path) as store:
        # An original this service replaced, whose bytes are now back under a new id.
        await store.enqueue("earlier-original", {"type": "AssetV1", "data": {}}, delay_seconds=0)
        await store.update(
            "earlier-original",
            state=JobState.DONE,
            new_asset_id="replacement",
            source_checksum="c3Vt",
            owner_id="user-1",
        )

        client = ImmichClient(BASE, "k")
        pipeline = Pipeline(settings, client, store)
        await pipeline.run_job(await _seed(store, video_payload_raw))
        job = await store.get(asset_id)

        assert job is not None
        assert job.state is JobState.SKIPPED
        assert job.skip_reason is SkipReason.RE_UPLOADED

        # And the verdict is stable: `reprocess` re-runs the check and reaches it again,
        # exactly as it does for an asset that carries a compressor marker.
        await store.reset(asset_id)
        claimed = await store.claim_next()
        assert claimed is not None
        await pipeline.run_job(claimed)
        await client.aclose()
        again = await store.get(asset_id)

    assert again is not None
    assert again.skip_reason is SkipReason.RE_UPLOADED
    # Recognition only. Nothing was downloaded, encoded, uploaded or removed.
    assert download.call_count == 0
    assert delete.call_count == 0


@respx.mock
async def test_the_ledger_is_recorded_before_anything_mutating(
    settings: Settings, video_payload_raw: dict[str, Any]
) -> None:
    """A dry run stops before the first mutating request, and still leaves the ledger
    behind — the checksum is unrecoverable once the original is gone, so it cannot wait
    for a later step."""
    settings.behavior.dry_run = True
    asset_id = video_payload_raw["data"]["asset"]["id"]
    _mock_no_marker(asset_id)
    _mock_asset_detail(asset_id, checksum="c3Vt", owner_id="user-1")

    async with JobStore(settings.database_path) as store:
        client = ImmichClient(BASE, "k")
        await Pipeline(settings, client, store).run_job(await _seed(store, video_payload_raw))
        await client.aclose()
        job = await store.get(asset_id)

    assert job is not None
    assert job.skip_reason is SkipReason.DRY_RUN
    assert job.source_checksum == "c3Vt"
    assert job.owner_id == "user-1"


@respx.mock
async def test_an_unrelated_asset_is_not_taken_for_a_re_upload(
    settings: Settings, video_payload_raw: dict[str, Any]
) -> None:
    """Different bytes, same owner. The guard must stay out of the way."""
    settings.behavior.dry_run = True
    asset_id = video_payload_raw["data"]["asset"]["id"]
    _mock_no_marker(asset_id)
    _mock_asset_detail(asset_id, checksum="b3RoZXI=", owner_id="user-1")

    async with JobStore(settings.database_path) as store:
        await store.enqueue("earlier-original", {"type": "AssetV1", "data": {}}, delay_seconds=0)
        await store.update(
            "earlier-original",
            state=JobState.DONE,
            new_asset_id="replacement",
            source_checksum="c3Vt",
            owner_id="user-1",
        )

        client = ImmichClient(BASE, "k")
        await Pipeline(settings, client, store).run_job(await _seed(store, video_payload_raw))
        await client.aclose()
        job = await store.get(asset_id)

    assert job is not None
    assert job.skip_reason is SkipReason.DRY_RUN


# ------------------------------------------------------------------- happy path


@needs_ffmpeg
@respx.mock
async def test_full_pipeline(settings: Settings, video_payload_raw: dict[str, Any], tmp_path: Path) -> None:
    asset_id = video_payload_raw["data"]["asset"]["id"]
    new_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    clip = await _make_clip(tmp_path / "src.mp4")
    video_payload_raw["data"]["asset"]["exifInfo"]["fileSizeInByte"] = clip.stat().st_size

    _mock_no_marker(asset_id)
    _mock_asset_detail(asset_id)
    respx.get(f"{BASE}/assets/{asset_id}/original").mock(
        return_value=httpx.Response(200, content=clip.read_bytes())
    )
    upload = respx.post(f"{BASE}/assets").mock(
        return_value=httpx.Response(201, json={"id": new_id, "status": "created"})
    )
    copy = respx.put(f"{BASE}/assets/copy").mock(return_value=httpx.Response(204))
    _mock_extracted(new_id)
    update = respx.put(f"{BASE}/assets/{new_id}").mock(return_value=httpx.Response(200, json={}))
    respx.put(f"{BASE}/tags").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": "t1", "value": "urlaub"}, {"id": "t2", "value": "wien"}],
        )
    )
    tag_assign = respx.put(f"{BASE}/tags/assets").mock(return_value=httpx.Response(200, json={"count": 1}))
    mark_new = respx.put(f"{BASE}/assets/{new_id}/metadata").mock(return_value=httpx.Response(200, json=[]))
    mark_old = respx.put(f"{BASE}/assets/{asset_id}/metadata").mock(return_value=httpx.Response(200, json=[]))
    delete = respx.delete(f"{BASE}/assets")

    async with JobStore(settings.database_path) as store:
        client = ImmichClient(BASE, "k")
        pipeline = Pipeline(settings, client, store)
        await pipeline.run_job(await _seed(store, video_payload_raw))
        await client.aclose()
        job = await store.get(asset_id)

    assert job is not None, "job disappeared"
    assert job.state is JobState.DONE, job.last_error
    assert job.new_asset_id == new_id
    assert job.ratio is not None and job.ratio < 0.6

    assert upload.call_count == 1
    assert copy.call_count == 1
    assert mark_new.call_count == 1
    assert mark_old.call_count == 1
    # trash_original is false in the fixture settings -> nothing is deleted.
    assert delete.call_count == 0

    # Tags and the fields `copy` does not carry are pulled over explicitly.
    assert tag_assign.call_count == 1
    body = json.loads(update.calls.last.request.content)
    assert body["description"] == "Compressor testclip Vienna"
    assert body["rating"] == 4
    assert body["latitude"] == pytest.approx(48.2082)
    assert "dateTimeOriginal" in body


@needs_ffmpeg
@respx.mock
async def test_rating_zero_is_never_sent(
    settings: Settings, video_payload_raw: dict[str, Any], tmp_path: Path
) -> None:
    """Immich v3 answers 400 for rating 0 — the pipeline must drop it, not forward it."""
    asset_id = video_payload_raw["data"]["asset"]["id"]
    new_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    clip = await _make_clip(tmp_path / "src.mp4")
    video_payload_raw["data"]["asset"]["exifInfo"]["fileSizeInByte"] = clip.stat().st_size
    video_payload_raw["data"]["asset"]["exifInfo"]["rating"] = 0

    _mock_no_marker(asset_id)
    _mock_asset_detail(asset_id)
    respx.get(f"{BASE}/assets/{asset_id}/original").mock(
        return_value=httpx.Response(200, content=clip.read_bytes())
    )
    respx.post(f"{BASE}/assets").mock(
        return_value=httpx.Response(201, json={"id": new_id, "status": "created"})
    )
    respx.put(f"{BASE}/assets/copy").mock(return_value=httpx.Response(204))
    _mock_extracted(new_id)
    update = respx.put(f"{BASE}/assets/{new_id}").mock(return_value=httpx.Response(200, json={}))
    respx.put(f"{BASE}/tags").mock(return_value=httpx.Response(200, json=[]))
    respx.put(f"{BASE}/tags/assets").mock(return_value=httpx.Response(200, json={"count": 0}))
    respx.put(f"{BASE}/assets/{new_id}/metadata").mock(return_value=httpx.Response(200, json=[]))
    respx.put(f"{BASE}/assets/{asset_id}/metadata").mock(return_value=httpx.Response(200, json=[]))

    async with JobStore(settings.database_path) as store:
        client = ImmichClient(BASE, "k")
        await Pipeline(settings, client, store).run_job(await _seed(store, video_payload_raw))
        await client.aclose()

    assert "rating" not in json.loads(update.calls.last.request.content)


@needs_ffmpeg
@respx.mock
async def test_live_source_state_beats_the_stale_payload(
    settings: Settings, video_payload_raw: dict[str, Any], tmp_path: Path
) -> None:
    """Tags/description added *after* the webhook fired must still be carried over.

    The webhook payload is a snapshot from metadata-extraction time, but the job runs
    `initial_delay_seconds` later — by then the user may have tagged or described the
    asset. The pipeline re-reads the source and prefers the live values.
    """
    asset_id = video_payload_raw["data"]["asset"]["id"]
    new_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    clip = await _make_clip(tmp_path / "src.mp4")
    video_payload_raw["data"]["asset"]["exifInfo"]["fileSizeInByte"] = clip.stat().st_size
    # The payload knows nothing about them yet.
    video_payload_raw["data"]["asset"]["exifInfo"]["tags"] = []
    video_payload_raw["data"]["asset"]["exifInfo"]["description"] = None

    _mock_no_marker(asset_id)
    # ...but the live asset has since gained a tag and a description.
    respx.get(f"{BASE}/assets/{asset_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": asset_id,
                "type": "VIDEO",
                "isTrashed": False,
                "people": [],
                "tags": [{"id": "t9", "value": "added-later"}],
                "exifInfo": {"description": "added later", "rating": 2},
            },
        )
    )
    respx.get(f"{BASE}/assets/{asset_id}/original").mock(
        return_value=httpx.Response(200, content=clip.read_bytes())
    )
    respx.post(f"{BASE}/assets").mock(
        return_value=httpx.Response(201, json={"id": new_id, "status": "created"})
    )
    respx.put(f"{BASE}/assets/copy").mock(return_value=httpx.Response(204))
    _mock_extracted(new_id)
    update = respx.put(f"{BASE}/assets/{new_id}").mock(return_value=httpx.Response(200, json={}))
    upsert = respx.put(f"{BASE}/tags").mock(
        return_value=httpx.Response(200, json=[{"id": "t9", "value": "added-later"}])
    )
    respx.put(f"{BASE}/tags/assets").mock(return_value=httpx.Response(200, json={"count": 1}))
    respx.put(f"{BASE}/assets/{new_id}/metadata").mock(return_value=httpx.Response(200, json=[]))
    respx.put(f"{BASE}/assets/{asset_id}/metadata").mock(return_value=httpx.Response(200, json=[]))

    async with JobStore(settings.database_path) as store:
        client = ImmichClient(BASE, "k")
        await Pipeline(settings, client, store).run_job(await _seed(store, video_payload_raw))
        await client.aclose()

    body = json.loads(update.calls.last.request.content)
    assert body["description"] == "added later"
    assert body["rating"] == 2
    assert b"added-later" in upsert.calls.last.request.content


@needs_ffmpeg
@respx.mock
async def test_duplicate_upload_leaves_the_original_alone(
    settings: Settings, video_payload_raw: dict[str, Any], tmp_path: Path
) -> None:
    settings.behavior.trash_original = True
    asset_id = video_payload_raw["data"]["asset"]["id"]
    clip = await _make_clip(tmp_path / "src.mp4")
    video_payload_raw["data"]["asset"]["exifInfo"]["fileSizeInByte"] = clip.stat().st_size

    _mock_no_marker(asset_id)
    _mock_asset_detail(asset_id)
    respx.get(f"{BASE}/assets/{asset_id}/original").mock(
        return_value=httpx.Response(200, content=clip.read_bytes())
    )
    respx.post(f"{BASE}/assets").mock(
        return_value=httpx.Response(200, json={"id": "existing-1", "status": "duplicate"})
    )
    copy = respx.put(f"{BASE}/assets/copy")
    delete = respx.delete(f"{BASE}/assets")

    async with JobStore(settings.database_path) as store:
        client = ImmichClient(BASE, "k")
        await Pipeline(settings, client, store).run_job(await _seed(store, video_payload_raw))
        await client.aclose()
        job = await store.get(asset_id)

    assert job is not None
    assert job.skip_reason is SkipReason.DUPLICATE
    assert copy.call_count == 0
    assert delete.call_count == 0


@needs_ffmpeg
@respx.mock
async def test_no_gain_marks_the_original_and_uploads_nothing(
    settings: Settings, video_payload_raw: dict[str, Any], tmp_path: Path
) -> None:
    # Stream-copy preset: the output is the same size, so the gate must reject it.
    settings.presets[0].cmd = "ffmpeg -y -loglevel error -i {input} -map_metadata 0 -c copy {output}"
    asset_id = video_payload_raw["data"]["asset"]["id"]
    clip = await _make_clip(tmp_path / "src.mp4", bitrate="600k")
    video_payload_raw["data"]["asset"]["exifInfo"]["fileSizeInByte"] = clip.stat().st_size

    _mock_no_marker(asset_id)
    _mock_asset_detail(asset_id)
    respx.get(f"{BASE}/assets/{asset_id}/original").mock(
        return_value=httpx.Response(200, content=clip.read_bytes())
    )
    upload = respx.post(f"{BASE}/assets")
    mark_old = respx.put(f"{BASE}/assets/{asset_id}/metadata").mock(return_value=httpx.Response(200, json=[]))

    async with JobStore(settings.database_path) as store:
        client = ImmichClient(BASE, "k")
        await Pipeline(settings, client, store).run_job(await _seed(store, video_payload_raw))
        await client.aclose()
        job = await store.get(asset_id)

    assert job is not None
    assert job.skip_reason is SkipReason.NO_GAIN
    assert upload.call_count == 0
    # The original gets a marker so the next webhook does not redo the work.
    assert mark_old.call_count == 1
    assert b"no_gain" in mark_old.calls.last.request.content


@needs_ffmpeg
@respx.mock
async def test_trash_is_deferred_not_immediate(
    settings: Settings, video_payload_raw: dict[str, Any], tmp_path: Path
) -> None:
    settings.behavior.trash_original = True
    settings.behavior.retention_days = 7
    asset_id = video_payload_raw["data"]["asset"]["id"]
    new_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    clip = await _make_clip(tmp_path / "src.mp4")
    video_payload_raw["data"]["asset"]["exifInfo"]["fileSizeInByte"] = clip.stat().st_size

    _mock_no_marker(asset_id)
    _mock_asset_detail(asset_id)
    respx.get(f"{BASE}/assets/{asset_id}/original").mock(
        return_value=httpx.Response(200, content=clip.read_bytes())
    )
    respx.post(f"{BASE}/assets").mock(
        return_value=httpx.Response(201, json={"id": new_id, "status": "created"})
    )
    respx.put(f"{BASE}/assets/copy").mock(return_value=httpx.Response(204))
    _mock_extracted(new_id)
    respx.put(f"{BASE}/assets/{new_id}").mock(return_value=httpx.Response(200, json={}))
    respx.put(f"{BASE}/tags").mock(return_value=httpx.Response(200, json=[]))
    respx.put(f"{BASE}/tags/assets").mock(return_value=httpx.Response(200, json={"count": 0}))
    respx.put(f"{BASE}/assets/{new_id}/metadata").mock(return_value=httpx.Response(200, json=[]))
    respx.put(f"{BASE}/assets/{asset_id}/metadata").mock(return_value=httpx.Response(200, json=[]))
    delete = respx.delete(f"{BASE}/assets")

    async with JobStore(settings.database_path) as store:
        client = ImmichClient(BASE, "k")
        await Pipeline(settings, client, store).run_job(await _seed(store, video_payload_raw))
        await client.aclose()
        job = await store.get(asset_id)
        assert job is not None
        assert job.state is JobState.PENDING_DELETE
        assert job.delete_after is not None
        # Nothing is due yet, so the sweeper has nothing to do.
        assert await store.due_deletions() == []

    assert delete.call_count == 0


# --------------------------------------------------------------- immediate deletion


def _posted_checksum(upload: respx.Route) -> str:
    """Base64 SHA-1 of the file body the pipeline actually posted.

    Read out of the recorded multipart request rather than recomputed from the source, so
    the test asserts against the exact bytes that reached the server — the same thing
    Immich hashes into `AssetResponseDto.checksum`.
    """
    body: bytes = upload.calls.last.request.content
    start = body.index(b"\r\n\r\n", body.index(b'name="assetData"')) + 4
    end = body.index(b"\r\n--", start)
    return base64.b64encode(hashlib.sha1(body[start:end]).digest()).decode("ascii")  # noqa: S324


def _mock_replacement(
    new_id: str,
    *,
    checksum: str | Callable[[], str] | None = None,
    date_time_original: str | None = "2024-06-15T12:30:00+00:00",
    is_trashed: bool = False,
    marker: bool = True,
) -> None:
    """A replacement asset as the delete gate expects to find it.

    `checksum` may be a callable so a test can defer it until the upload has happened.
    """

    def _detail(_request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = {
            "id": new_id,
            "type": "VIDEO",
            "isTrashed": is_trashed,
            "people": [],
            "exifInfo": {"dateTimeOriginal": date_time_original} if date_time_original else {},
        }
        if checksum is not None:
            body["checksum"] = checksum() if callable(checksum) else checksum
        return httpx.Response(200, json=body)

    respx.get(f"{BASE}/assets/{new_id}").mock(side_effect=_detail)
    respx.get(f"{BASE}/assets/{new_id}/metadata").mock(
        return_value=httpx.Response(
            200,
            json=[{"key": "compressor", "value": {"v": MARKER_VERSION}}] if marker else [],
        )
    )


async def _run_until_deletion(
    settings: Settings,
    payload: dict[str, Any],
    tmp_path: Path,
    *,
    new_id: str,
    replacement: Callable[[respx.Route], None],
) -> tuple[Job | None, respx.Route]:
    """Drive the whole pipeline with `retention_days: 0` and hand back job + delete route."""
    asset_id = payload["data"]["asset"]["id"]
    clip = await _make_clip(tmp_path / "src.mp4")
    payload["data"]["asset"]["exifInfo"]["fileSizeInByte"] = clip.stat().st_size

    _mock_no_marker(asset_id)
    _mock_asset_detail(asset_id)
    respx.get(f"{BASE}/assets/{asset_id}/original").mock(
        return_value=httpx.Response(200, content=clip.read_bytes())
    )
    upload = respx.post(f"{BASE}/assets").mock(
        return_value=httpx.Response(201, json={"id": new_id, "status": "created"})
    )
    replacement(upload)
    respx.put(f"{BASE}/assets/copy").mock(return_value=httpx.Response(204))
    respx.put(f"{BASE}/assets/{new_id}").mock(return_value=httpx.Response(200, json={}))
    respx.put(f"{BASE}/tags").mock(return_value=httpx.Response(200, json=[]))
    respx.put(f"{BASE}/tags/assets").mock(return_value=httpx.Response(200, json={"count": 0}))
    respx.put(f"{BASE}/assets/{new_id}/metadata").mock(return_value=httpx.Response(200, json=[]))
    respx.put(f"{BASE}/assets/{asset_id}/metadata").mock(return_value=httpx.Response(200, json=[]))
    delete = respx.delete(f"{BASE}/assets").mock(return_value=httpx.Response(204))

    async with JobStore(settings.database_path) as store:
        client = ImmichClient(BASE, "k")
        await Pipeline(settings, client, store).run_job(await _seed(store, payload))
        await client.aclose()
        return await store.get(asset_id), delete


@needs_ffmpeg
@respx.mock
async def test_retention_zero_deletes_inline_without_the_sweeper(
    settings: Settings, video_payload_raw: dict[str, Any], tmp_path: Path
) -> None:
    """`retention_days: 0` must not leave the job sitting for the 60 s sweeper interval."""
    settings.behavior.trash_original = True
    settings.behavior.retention_days = 0
    new_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

    job, delete = await _run_until_deletion(
        settings,
        video_payload_raw,
        tmp_path,
        new_id=new_id,
        replacement=lambda upload: _mock_replacement(new_id, checksum=lambda: _posted_checksum(upload)),
    )

    assert job is not None
    assert job.state is JobState.DONE, job.last_error
    # Nothing left for the sweeper to pick up.
    assert job.delete_after is None
    assert delete.call_count == 1
    assert json.loads(delete.calls.last.request.content)["ids"] == [video_payload_raw["data"]["asset"]["id"]]


@needs_ffmpeg
@respx.mock
@pytest.mark.parametrize(
    ("broken", "settle_s"),
    [
        pytest.param({"is_trashed": True}, 30.0, id="replacement-is-trashed"),
        pytest.param({"checksum": "Zm9vYmFyYmF6cXV1eDEyMzQ1Njc="}, 30.0, id="checksum-mismatch"),
        pytest.param({"date_time_original": None}, 0.0, id="no-dateTimeOriginal"),
        pytest.param({"marker": False}, 30.0, id="no-compressor-marker"),
    ],
)
async def test_a_failed_verification_never_deletes_the_original(
    settings: Settings,
    video_payload_raw: dict[str, Any],
    tmp_path: Path,
    broken: dict[str, Any],
    settle_s: float,
) -> None:
    """Each of the four gate conditions on its own is enough to keep the original."""
    settings.behavior.trash_original = True
    settings.behavior.retention_days = 0
    settings.behavior.delete_mode = "permanent"
    # The no-dateTimeOriginal case would otherwise sit out the extraction wait.
    settings.behavior.post_upload_settle_s = settle_s
    new_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

    def _replacement(upload: respx.Route) -> None:
        _mock_replacement(new_id, **{"checksum": lambda: _posted_checksum(upload), **broken})

    job, delete = await _run_until_deletion(
        settings, video_payload_raw, tmp_path, new_id=new_id, replacement=_replacement
    )

    assert delete.call_count == 0
    assert job is not None
    assert job.state is JobState.PENDING_DELETE
    assert job.last_error
    # Left for the sweeper to retry rather than abandoned.
    assert job.delete_after is not None


@needs_ffmpeg
@respx.mock
@pytest.mark.parametrize(
    ("delete_mode", "expected_force"),
    [
        pytest.param("trash", False, id="trash-is-recoverable"),
        pytest.param("permanent", True, id="permanent-bypasses-the-trash"),
    ],
)
async def test_delete_mode_decides_the_force_flag(
    settings: Settings,
    video_payload_raw: dict[str, Any],
    tmp_path: Path,
    delete_mode: str,
    expected_force: bool,
) -> None:
    """`force: true` is what makes the delete permanent — verified on a live v3.1.0."""
    settings.behavior.trash_original = True
    settings.behavior.retention_days = 0
    settings.behavior.delete_mode = delete_mode  # type: ignore[assignment]
    new_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

    job, delete = await _run_until_deletion(
        settings,
        video_payload_raw,
        tmp_path,
        new_id=new_id,
        replacement=lambda upload: _mock_replacement(new_id, checksum=lambda: _posted_checksum(upload)),
    )

    assert job is not None and job.state is JobState.DONE, job.last_error if job else "no job"
    assert delete.call_count == 1
    assert json.loads(delete.calls.last.request.content)["force"] is expected_force


@needs_ffmpeg
@respx.mock
async def test_the_uploaded_checksum_is_persisted_for_the_sweeper(
    settings: Settings, video_payload_raw: dict[str, Any], tmp_path: Path
) -> None:
    """With a retention window the local file is long gone — the job row has to remember."""
    settings.behavior.trash_original = True
    settings.behavior.retention_days = 7
    new_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

    job, delete = await _run_until_deletion(
        settings,
        video_payload_raw,
        tmp_path,
        new_id=new_id,
        replacement=lambda upload: _mock_replacement(new_id, checksum=lambda: _posted_checksum(upload)),
    )

    assert delete.call_count == 0
    assert job is not None
    assert job.state is JobState.PENDING_DELETE
    assert job.new_checksum is not None
    assert len(base64.b64decode(job.new_checksum)) == 20  # SHA-1


# ------------------------------------------------------------------- webhook API


def _test_client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def test_webhook_requires_the_shared_secret(settings: Settings, video_payload_raw: dict[str, Any]) -> None:
    with _test_client(settings) as client:
        assert client.post("/webhook", json=video_payload_raw).status_code == 401
        assert (
            client.post(
                "/webhook",
                json=video_payload_raw,
                headers={"X-Compressor-Token": "wrong"},
            ).status_code
            == 401
        )


def test_webhook_accepts_and_is_idempotent(
    settings: Settings, fresh_video_payload_raw: dict[str, Any]
) -> None:
    headers = {"X-Compressor-Token": "test-token"}
    settings.behavior.initial_delay_seconds = 3600  # keep the worker away from the job
    with _test_client(settings) as client:
        first = client.post("/webhook", json=fresh_video_payload_raw, headers=headers)
        assert first.status_code == 202
        assert first.json()["duplicate"] is False

        # A second webhook for the same asset must be a no-op.
        second = client.post("/webhook", json=fresh_video_payload_raw, headers=headers)
        assert second.status_code == 202
        assert second.json()["duplicate"] is True

        jobs = client.get("/jobs").json()
        assert jobs["count"] == 1


def test_webhook_refuses_a_bulk_retrigger_and_writes_no_job(
    settings: Settings, video_payload_raw: dict[str, Any]
) -> None:
    """One click on Extract Metadata must not become a library-wide queue.

    The 202 is deliberate — Immich logs any non-2xx as "executed successfully", so the
    status code carries nothing. The body and the log line are where the refusal lives.
    """
    headers = {"X-Compressor-Token": "test-token"}
    with _test_client(settings) as client:
        response = client.post("/webhook", json=aged(video_payload_raw, hours=24 * 30), headers=headers)
        assert response.status_code == 202
        body = response.json()
        assert body["accepted"] is False
        assert body["reason"] == "too_old"

        assert client.get("/jobs").json()["count"] == 0


async def test_a_refused_webhook_leaves_the_asset_reachable_by_backfill(
    settings: Settings, video_payload_raw: dict[str, Any]
) -> None:
    """The reason the gate sits in front of the store rather than inside `check_guards`.

    `backfill` enqueues through the same `ON CONFLICT DO NOTHING`, so a row written here —
    in any state, including `skipped` — would make the asset permanently unreachable by the
    one path that is supposed to work through the library on purpose.
    """
    old = aged(video_payload_raw, hours=24 * 30)
    asset_id = old["data"]["asset"]["id"]

    with pytest.raises(WebhookRejected):
        check_ingest_guards(WebhookPayload.model_validate(old).data.asset, settings.behavior)

    async with JobStore(settings.database_path) as store:
        # What `immich-compressor backfill --apply` does, with its own trigger name.
        assert await store.enqueue(asset_id, {**old, "trigger": "Backfill"}, delay_seconds=0) is True


def _fresh(raw: dict[str, Any], asset_id: str) -> dict[str, Any]:
    """A brand-new upload with its own asset id, so each one is a distinct insert."""
    payload = aged(raw, hours=0)
    payload["data"]["asset"]["id"] = asset_id
    return payload


def test_the_surge_breaker_latches_and_then_refuses_everything(
    settings: Settings, video_payload_raw: dict[str, Any]
) -> None:
    headers = {"X-Compressor-Token": "test-token"}
    settings.behavior.initial_delay_seconds = 3600  # keep the workers off the queue
    settings.behavior.surge_threshold = 3
    with _test_client(settings) as client:
        for index in range(3):
            body = client.post(
                "/webhook", json=_fresh(video_payload_raw, f"asset-{index}"), headers=headers
            ).json()
            assert body["accepted"] is True

        # The fourth new asset inside the window is the one over the line.
        tripped = client.post("/webhook", json=_fresh(video_payload_raw, "asset-3"), headers=headers).json()
        assert tripped["accepted"] is True  # this one was already queued before the trip

        # Everything after it is refused — including a perfectly fresh upload.
        after = client.post("/webhook", json=_fresh(video_payload_raw, "asset-4"), headers=headers).json()
        assert after["accepted"] is False
        assert after["reason"] == "paused"
        assert client.get("/jobs").json()["count"] == 4

        # /stats rather than /healthz: the latter reaches out to Immich, which costs the
        # connect timeout in a unit test. `test_healthz_reports_the_pause` covers that side.
        paused = client.get("/stats").json()["paused"]
        assert paused is not None
        assert "surge_threshold 3" in paused["reason"]


def test_a_replay_of_a_known_asset_does_not_push_the_breaker(
    settings: Settings, video_payload_raw: dict[str, Any]
) -> None:
    """A re-trigger queues no work, so it must not count as work arriving."""
    headers = {"X-Compressor-Token": "test-token"}
    settings.behavior.initial_delay_seconds = 3600
    settings.behavior.surge_threshold = 3
    payload = _fresh(video_payload_raw, "the-same-asset")
    with _test_client(settings) as client:
        for _ in range(10):
            assert client.post("/webhook", json=payload, headers=headers).json()["accepted"] is True
        assert client.get("/stats").json()["paused"] is None


def test_resume_clears_the_latch_and_needs_the_token(
    settings: Settings, video_payload_raw: dict[str, Any]
) -> None:
    headers = {"X-Compressor-Token": "test-token"}
    settings.behavior.initial_delay_seconds = 3600
    settings.behavior.surge_threshold = 1
    with _test_client(settings) as client:
        for index in range(2):
            client.post("/webhook", json=_fresh(video_payload_raw, f"a-{index}"), headers=headers)
        assert client.get("/stats").json()["paused"] is not None

        # Re-arming a service that deletes originals is not an unauthenticated action.
        assert client.post("/resume").status_code == 401

        resumed = client.post("/resume", headers=headers).json()
        assert resumed["resumed"] is True
        assert client.get("/stats").json()["paused"] is None

        # A second resume is honest about having had nothing to do.
        assert client.post("/resume", headers=headers).json()["resumed"] is False

        # And webhooks are accepted again.
        again = client.post("/webhook", json=_fresh(video_payload_raw, "a-9"), headers=headers)
        assert again.json()["accepted"] is True


async def test_a_paused_worker_claims_nothing(settings: Settings, video_payload_raw: dict[str, Any]) -> None:
    """The latch has to stop work already in the queue, not only new arrivals."""
    async with JobStore(settings.database_path) as store:
        await store.enqueue("queued-before-the-pause", video_payload_raw, delay_seconds=0)
        await store.pause("surge")

        worker = Worker(settings, ImmichClient("http://immich-test/api", "k"), store)
        await worker.start()
        try:
            await asyncio.sleep(settings.behavior.poll_interval_seconds * 4)
        finally:
            await worker.stop()

        job = await store.get("queued-before-the-pause")
        assert job is not None
        assert job.state is JobState.QUEUED  # never claimed, never moved to running
        assert job.attempts == 0


def test_healthz_reports_the_pause(settings: Settings, video_payload_raw: dict[str, Any]) -> None:
    """The one place the latch has to be visible to a container health check."""
    headers = {"X-Compressor-Token": "test-token"}
    settings.behavior.initial_delay_seconds = 3600
    settings.behavior.surge_threshold = 1
    with _test_client(settings) as client:
        for index in range(2):
            client.post("/webhook", json=_fresh(video_payload_raw, f"h-{index}"), headers=headers)
        health = client.get("/healthz").json()
    assert health["status"] == "paused"
    assert health["paused"] is True
    assert "surge_threshold 1" in health["paused_reason"]


def test_webhook_rejects_a_malformed_body(settings: Settings) -> None:
    with _test_client(settings) as client:
        response = client.post("/webhook", json={"nope": True}, headers={"X-Compressor-Token": "test-token"})
        assert response.status_code == 422


def test_stats_and_health(settings: Settings) -> None:
    with _test_client(settings) as client:
        health = client.get("/healthz").json()
        assert health["status"] == "ok"
        assert health["dry_run"] is False  # the fixture flips it; the shipped default is true

        stats = client.get("/stats").json()
        assert stats["total"] == 0
        assert stats["config"]["trash_original"] is False


def test_reprocess_requires_the_shared_secret(settings: Settings, video_payload_raw: dict[str, Any]) -> None:
    asset_id = video_payload_raw["data"]["asset"]["id"]
    with _test_client(settings) as client:
        assert client.post(f"/reprocess/{asset_id}").status_code == 401
        assert (
            client.post(f"/reprocess/{asset_id}", headers={"X-Compressor-Token": "test-token"}).status_code
            == 404
        )


# ------------------------------------------------------- the webhook counters


def test_a_refused_token_is_counted_where_somebody_will_see_it(
    settings: Settings, fresh_video_payload_raw: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """A shared secret that does not match leaves no other trace anywhere.

    Immich discards the 401 and logs the workflow as executed successfully; no job row is
    written; `/healthz` says healthy and `report` says zero jobs, which is exactly what an
    idle installation says. Reproduced on a live v3.1.0 — the only evidence in the whole
    system was one WARNING line in the container log.
    """
    caplog.set_level(logging.WARNING)
    with _test_client(settings) as client:
        assert (
            client.post(
                "/webhook", json=fresh_video_payload_raw, headers={"X-Compressor-Token": "wrong"}
            ).status_code
            == 401
        )
        webhooks = client.get("/stats").json()["webhooks"]
        assert webhooks == {"received": 0, "rejected": 1}
        assert "immich_compressor_webhooks_rejected_total 1" in client.get("/metrics").text

    # The line has to name what arrived, or a truncated paste and a token from an earlier
    # installation are indistinguishable — and the second one is what actually happens.
    message = caplog.text
    assert "5 characters starting wrong" in message
    assert "10 characters starting test-t" in message


def test_an_accepted_webhook_is_counted_too(
    settings: Settings, fresh_video_payload_raw: dict[str, Any]
) -> None:
    """ "0 received, 7 rejected" and "0 received, 0 rejected" are different problems: a
    wrong token, and Immich never reaching the service at all."""
    settings.behavior.initial_delay_seconds = 3600
    with _test_client(settings) as client:
        client.post("/webhook", json=fresh_video_payload_raw, headers={"X-Compressor-Token": "test-token"})
        assert client.get("/stats").json()["webhooks"] == {"received": 1, "rejected": 0}


def test_the_counters_outlive_the_process(
    settings: Settings, fresh_video_payload_raw: dict[str, Any]
) -> None:
    """Restarting the container is the first thing anybody tries. It must not be the thing
    that erases the evidence — and `report` reads them from a different process anyway."""
    with _test_client(settings) as client:
        client.post("/webhook", json=fresh_video_payload_raw, headers={"X-Compressor-Token": "no"})

    with _test_client(settings) as client:
        assert client.get("/stats").json()["webhooks"]["rejected"] == 1


def test_the_maintenance_routes_do_not_count_as_webhooks(settings: Settings) -> None:
    """`/reprocess` and `/resume` carry the same header and are not webhooks. Counting them
    would put a number in front of a user that does not answer the question they asked."""
    with _test_client(settings) as client:
        client.post("/resume", headers={"X-Compressor-Token": "wrong"})
        client.post("/resume", headers={"X-Compressor-Token": "test-token"})
        assert client.get("/stats").json()["webhooks"] == {"received": 0, "rejected": 0}


def test_startup_prints_the_pattern_the_workflow_has_to_carry(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """The marker couples three things nobody sees side by side: `compressed_marker`, the
    filename the encoder writes, and the workflow's regex — which lives inside Immich,
    where nothing here can check it. Printing the expected pattern is what makes the
    comparison possible at all."""
    caplog.set_level(logging.INFO)
    with _test_client(settings):
        pass

    assert r"^(?!.*\.cmp\.).*$" in caplog.text


# ------------------------------------------------ the shim's gate, opened by the delete


ORIGINAL_HASH = "02MpaJkpzGHNbGwxWtencVNK7uY="
OWNER_ID = "11111111-1111-4111-8111-111111111111"
REPLACEMENT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def _mock_verified_replacement(new_id: str, checksum: str, *, favorite: bool = False) -> None:
    """Everything `_verify_replacement` asks about the replacement, all answers good."""
    respx.get(f"{BASE}/assets/{new_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": new_id,
                "type": "VIDEO",
                "ownerId": OWNER_ID,
                "isFavorite": favorite,
                "isTrashed": False,
                "checksum": checksum,
                "originalFileName": "clip.cmp.mp4",
                "exifInfo": {"dateTimeOriginal": "2024-06-15T12:30:00.000Z"},
            },
        )
    )
    respx.get(f"{BASE}/assets/{new_id}/metadata").mock(
        return_value=httpx.Response(200, json=[{"key": "compressor", "value": {"v": 1}}])
    )


async def _finalize(
    settings: Settings, store: JobStore, *, checksum: str | None = ORIGINAL_HASH
) -> Job | None:
    """Seed a replaced job at the point just before its original is removed."""
    asset_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    await store.enqueue(asset_id, {"data": {"asset": {"id": asset_id, "type": "VIDEO"}}}, delay_seconds=0)
    await store.update(
        asset_id,
        new_asset_id=REPLACEMENT_ID,
        new_checksum="replacement-hash=",
        source_checksum=checksum,
        owner_id=OWNER_ID if checksum else None,
    )
    job = await store.get(asset_id)
    assert job is not None
    client = ImmichClient(BASE, "k")
    try:
        await Pipeline(settings, client, store).finalize_original(job, REPLACEMENT_ID, "replacement-hash=")
    finally:
        await client.aclose()
    return await store.get(asset_id)


@respx.mock
async def test_permanent_delete_opens_the_gate_and_touches_the_replacement(
    settings: Settings, tmp_path: Path
) -> None:
    """The only moment this service ever witnesses an original ceasing to exist."""
    settings.behavior.delete_mode = "permanent"
    settings.shim.enabled = True
    _mock_verified_replacement(REPLACEMENT_ID, "replacement-hash=")
    respx.delete(f"{BASE}/assets").mock(return_value=httpx.Response(204))
    touch = respx.put(f"{BASE}/assets/{REPLACEMENT_ID}").mock(return_value=httpx.Response(204))

    async with JobStore(settings.database_path) as store:
        job = await _finalize(settings, store)
        assert job is not None
        assert job.state is JobState.DONE
        assert job.original_freed_at is not None
        counters = await store.counters()
        # Both, not one. `shim_gates_opened_total` is what step 3 of the rollout tells an
        # operator to watch, and on a `permanent` deployment this is the only path that
        # ever opens a gate — it staying at zero here would read as "nothing happening".
        assert counters[SHIM_GATES_OPENED] == 1
        assert counters[SHIM_TOUCHES] == 1

    assert touch.call_count == 1
    # The body must carry a field: an empty PUT is a plain read upstream and bumps no
    # `updateId`, so the replacement would never be re-offered to the phone.
    assert json.loads(touch.calls.last.request.content) == {"isFavorite": False}


async def _finalize_inline(settings: Settings, store: JobStore) -> Job | None:
    """The same finalise, in the ordering the `retention_days: 0` path really uses.

    `_finalize` above reloads the job after the ledger write, which is how the sweeper
    reaches the finaliser: it takes its jobs from `due_deletions()`, long after everything
    about them was written. The pipeline's own inline call never reloads. It claims a job,
    writes `source_checksum`/`owner_id` onto the *row* in step 2, and carries that same
    object — still holding the values it was claimed with — down into `finalize_original`.
    """
    asset_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    await store.enqueue(asset_id, {"data": {"asset": {"id": asset_id, "type": "VIDEO"}}}, delay_seconds=0)
    job = await store.claim_next()
    assert job is not None
    assert job.source_checksum is None  # claimed before step 2 wrote the ledger

    await store.update(asset_id, source_checksum=ORIGINAL_HASH, owner_id=OWNER_ID)  # step 2
    await store.update(asset_id, new_asset_id=REPLACEMENT_ID, new_checksum="replacement-hash=")  # step 6

    client = ImmichClient(BASE, "k")
    try:
        await Pipeline(settings, client, store).finalize_original(job, REPLACEMENT_ID, "replacement-hash=")
    finally:
        await client.aclose()
    return await store.get(asset_id)


@respx.mock
async def test_the_inline_delete_path_opens_the_gate_from_a_pre_ledger_job(
    settings: Settings, tmp_path: Path
) -> None:
    """`permanent` + `retention_days: 0` — the configuration the shim exists for.

    Every shipped test of the gate entered `finalize_original` with a job reloaded after
    the ledger write, which is the sweeper's calling convention, so the inline path had no
    coverage at all — and there the gate never opened: the finaliser read the ledger pair
    off an object that predated the write, found `None`, and returned before
    `mark_original_freed`. The store holds what step 2 wrote, so the store is what decides.
    """
    settings.behavior.delete_mode = "permanent"
    settings.behavior.retention_days = 0
    settings.shim.enabled = True
    _mock_verified_replacement(REPLACEMENT_ID, "replacement-hash=")
    respx.delete(f"{BASE}/assets").mock(return_value=httpx.Response(204))
    touch = respx.put(f"{BASE}/assets/{REPLACEMENT_ID}").mock(return_value=httpx.Response(204))

    async with JobStore(settings.database_path) as store:
        job = await _finalize_inline(settings, store)
        assert job is not None
        assert job.state is JobState.DONE
        assert job.original_freed_at is not None
        assert (await store.counters())[SHIM_TOUCHES] == 1

    assert touch.call_count == 1


@respx.mock
async def test_the_touch_writes_back_the_value_it_read(settings: Settings, tmp_path: Path) -> None:
    """A favourited replacement must not be un-favourited by being re-offered."""
    settings.behavior.delete_mode = "permanent"
    settings.shim.enabled = True
    _mock_verified_replacement(REPLACEMENT_ID, "replacement-hash=", favorite=True)
    respx.delete(f"{BASE}/assets").mock(return_value=httpx.Response(204))
    touch = respx.put(f"{BASE}/assets/{REPLACEMENT_ID}").mock(return_value=httpx.Response(204))

    async with JobStore(settings.database_path) as store:
        await _finalize(settings, store)

    assert json.loads(touch.calls.last.request.content) == {"isFavorite": True}


@respx.mock
async def test_trash_mode_leaves_the_gate_closed(settings: Settings, tmp_path: Path) -> None:
    """The original still exists, holding its checksum. Only the purge frees it, and that
    happens inside Immich up to a month later — the shim watches for it instead."""
    settings.behavior.delete_mode = "trash"
    settings.shim.enabled = True
    _mock_verified_replacement(REPLACEMENT_ID, "replacement-hash=")
    respx.delete(f"{BASE}/assets").mock(return_value=httpx.Response(204))
    touch = respx.put(f"{BASE}/assets/{REPLACEMENT_ID}").mock(return_value=httpx.Response(204))

    async with JobStore(settings.database_path) as store:
        job = await _finalize(settings, store)
        assert job is not None and job.original_freed_at is None
        counters = await store.counters()
        assert counters[SHIM_GATES_OPENED] == 0
        assert counters[SHIM_TOUCHES] == 0

    assert touch.call_count == 0


@respx.mock
async def test_the_gate_is_recorded_even_with_the_shim_off(settings: Settings, tmp_path: Path) -> None:
    """It is a fact about the server, not about this service's configuration.

    Only the touch is conditional: it is a write, and worth making only when something is
    listening for the result.
    """
    settings.behavior.delete_mode = "permanent"
    settings.shim.enabled = False
    _mock_verified_replacement(REPLACEMENT_ID, "replacement-hash=")
    respx.delete(f"{BASE}/assets").mock(return_value=httpx.Response(204))
    touch = respx.put(f"{BASE}/assets/{REPLACEMENT_ID}").mock(return_value=httpx.Response(204))

    async with JobStore(settings.database_path) as store:
        job = await _finalize(settings, store)
        assert job is not None and job.original_freed_at is not None
        counters = await store.counters()
        # The counter follows the UPDATE, so it is recorded on the same terms the gate is.
        assert counters[SHIM_GATES_OPENED] == 1
        assert counters[SHIM_TOUCHES] == 0

    assert touch.call_count == 0


@respx.mock
async def test_a_pre_ledger_job_opens_no_gate(settings: Settings, tmp_path: Path) -> None:
    """No checksum was ever recorded, so there is nothing to translate to."""
    settings.behavior.delete_mode = "permanent"
    settings.shim.enabled = True
    _mock_verified_replacement(REPLACEMENT_ID, "replacement-hash=")
    respx.delete(f"{BASE}/assets").mock(return_value=httpx.Response(204))
    touch = respx.put(f"{BASE}/assets/{REPLACEMENT_ID}").mock(return_value=httpx.Response(204))

    async with JobStore(settings.database_path) as store:
        job = await _finalize(settings, store, checksum=None)
        assert job is not None and job.original_freed_at is None
        assert (await store.counters())[SHIM_GATES_OPENED] == 0

    assert touch.call_count == 0


@respx.mock
async def test_a_failing_touch_does_not_fail_the_job(settings: Settings, tmp_path: Path) -> None:
    """The original is already gone; the job is finished. Only the re-offer is missing."""
    settings.behavior.delete_mode = "permanent"
    settings.shim.enabled = True
    _mock_verified_replacement(REPLACEMENT_ID, "replacement-hash=")
    respx.delete(f"{BASE}/assets").mock(return_value=httpx.Response(204))
    respx.put(f"{BASE}/assets/{REPLACEMENT_ID}").mock(return_value=httpx.Response(500))

    async with JobStore(settings.database_path) as store:
        job = await _finalize(settings, store)
        assert job is not None
        assert job.state is JobState.DONE
        # Still recorded: the ledger is right, so a later change to the asset re-offers it.
        assert job.original_freed_at is not None
        counters = await store.counters()
        assert counters[SHIM_GATES_OPENED] == 1
        assert counters[SHIM_TOUCHES] == 0
