"""Full pipeline against respx mocks, plus the webhook endpoint."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from immich_compressor.api import ImmichClient
from immich_compressor.config import Settings
from immich_compressor.encoder import run_command
from immich_compressor.models import Job, JobState, SkipReason
from immich_compressor.pipeline import Pipeline
from immich_compressor.server import create_app
from immich_compressor.store import JobStore

BASE = "http://immich-test:2283/api"

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed"
)


async def _make_clip(path: Path, *, bitrate: str = "8000k") -> Path:
    code, _, stderr = await run_command(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=15:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "mpeg4", "-b:v", bitrate, "-c:a", "aac", "-b:a", "128k",
            "-shortest", "-metadata", "creation_time=2024-06-15T12:30:00Z", str(path),
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
    respx.get(f"{BASE}/assets/{asset_id}/metadata").mock(
        return_value=httpx.Response(200, json=[])
    )


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


def _mock_asset_detail(asset_id: str, people: list[dict[str, str]] | None = None) -> None:
    respx.get(f"{BASE}/assets/{asset_id}").mock(
        return_value=httpx.Response(
            200,
            json={"id": asset_id, "type": "VIDEO", "isTrashed": False, "people": people or []},
        )
    )


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


@respx.mock
async def test_existing_marker_stops_the_loop(
    settings: Settings, video_payload_raw: dict[str, Any]
) -> None:
    asset_id = video_payload_raw["data"]["asset"]["id"]
    respx.get(f"{BASE}/assets/{asset_id}/metadata").mock(
        return_value=httpx.Response(200, json=[{"key": "compressor", "value": {"v": 1}}])
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
async def test_named_people_are_left_alone(
    settings: Settings, video_payload_raw: dict[str, Any]
) -> None:
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


# ------------------------------------------------------------------- happy path


@needs_ffmpeg
@respx.mock
async def test_full_pipeline(
    settings: Settings, video_payload_raw: dict[str, Any], tmp_path: Path
) -> None:
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
    tag_assign = respx.put(f"{BASE}/tags/assets").mock(
        return_value=httpx.Response(200, json={"count": 1})
    )
    mark_new = respx.put(f"{BASE}/assets/{new_id}/metadata").mock(
        return_value=httpx.Response(200, json=[])
    )
    mark_old = respx.put(f"{BASE}/assets/{asset_id}/metadata").mock(
        return_value=httpx.Response(200, json=[])
    )
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
    mark_old = respx.put(f"{BASE}/assets/{asset_id}/metadata").mock(
        return_value=httpx.Response(200, json=[])
    )

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


# ------------------------------------------------------------------- webhook API


def _test_client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def test_webhook_requires_the_shared_secret(
    settings: Settings, video_payload_raw: dict[str, Any]
) -> None:
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
    settings: Settings, video_payload_raw: dict[str, Any]
) -> None:
    headers = {"X-Compressor-Token": "test-token"}
    settings.behavior.initial_delay_seconds = 3600  # keep the worker away from the job
    with _test_client(settings) as client:
        first = client.post("/webhook", json=video_payload_raw, headers=headers)
        assert first.status_code == 202
        assert first.json()["duplicate"] is False

        # A second webhook for the same asset must be a no-op.
        second = client.post("/webhook", json=video_payload_raw, headers=headers)
        assert second.status_code == 202
        assert second.json()["duplicate"] is True

        jobs = client.get("/jobs").json()
        assert jobs["count"] == 1


def test_webhook_rejects_a_malformed_body(settings: Settings) -> None:
    with _test_client(settings) as client:
        response = client.post(
            "/webhook", json={"nope": True}, headers={"X-Compressor-Token": "test-token"}
        )
        assert response.status_code == 422


def test_stats_and_health(settings: Settings) -> None:
    with _test_client(settings) as client:
        health = client.get("/healthz").json()
        assert health["status"] == "ok"
        assert health["dry_run"] is False  # the fixture flips it; the shipped default is true

        stats = client.get("/stats").json()
        assert stats["total"] == 0
        assert stats["config"]["trash_original"] is False


def test_reprocess_requires_the_shared_secret(
    settings: Settings, video_payload_raw: dict[str, Any]
) -> None:
    asset_id = video_payload_raw["data"]["asset"]["id"]
    with _test_client(settings) as client:
        assert client.post(f"/reprocess/{asset_id}").status_code == 401
        assert (
            client.post(
                f"/reprocess/{asset_id}", headers={"X-Compressor-Token": "test-token"}
            ).status_code
            == 404
        )
