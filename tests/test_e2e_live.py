"""End-to-end test against a real Immich instance.

Deselect with ``pytest -m "not live"``. Requires:

    E2E_IMMICH_URL=http://127.0.0.1:2283/api
    E2E_IMMICH_KEY=<api key with asset.* and tag.* permissions>

The test uploads its own throwaway video, drives the full pipeline, asserts that album,
tags, rating, description, GPS and timeline position survived, and finally trashes the
original and restores it again (the documented rollback path).

Two of these tests drive ``POST /sync/stream``, which **no API key can open** — Immich
answers ``403 {"message": "Sync endpoints cannot be used with API keys"}`` on every
``/sync`` route. They need a session token, so they need the credentials of the account
that owns the key above:

    E2E_IMMICH_EMAIL=<that account's email>
    E2E_IMMICH_PASSWORD=<that account's password>

Without them the sync tests skip, and a skip is not a pass: the one claim the shim's whole
delivery path rests on goes unverified. See ``docs/immich-api-notes.md`` #17.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from immich_compressor.api import ImmichClient, ImmichError
from immich_compressor.config import Preset, Settings
from immich_compressor.encoder import run_command
from immich_compressor.models import JobState, UpdateAssetFields
from immich_compressor.pipeline import Pipeline
from immich_compressor.store import JobStore

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("E2E_IMMICH_URL") or not os.environ.get("E2E_IMMICH_KEY"),
        reason="E2E_IMMICH_URL / E2E_IMMICH_KEY not set",
    ),
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed"),
]

BASE = os.environ.get("E2E_IMMICH_URL", "")
KEY = os.environ.get("E2E_IMMICH_KEY", "")
# Only the sync routes need these, and only because an API key cannot open a sync session.
SESSION_EMAIL = os.environ.get("E2E_IMMICH_EMAIL", "")
SESSION_PASSWORD = os.environ.get("E2E_IMMICH_PASSWORD", "")
MARKER = "e2e-live"


@pytest.fixture
async def api() -> ImmichClient:
    client = ImmichClient(BASE, KEY, timeout_s=300)
    yield client
    await client.aclose()


@pytest.fixture
async def raw() -> httpx.AsyncClient:
    async with httpx.AsyncClient(base_url=BASE, headers={"x-api-key": KEY}, timeout=300) as client:
        yield client


@pytest.fixture
async def session() -> httpx.AsyncClient:
    """A client holding a real session token, which is what the ``/sync`` routes require.

    Immich refuses API keys on every sync endpoint, so the `raw` fixture's key cannot open
    a sync session however it is scoped. The phone logs in; so does this. The account must
    be the one that owns ``E2E_IMMICH_KEY`` — the assertions here compare what one
    credential uploaded against what the other is offered, and two users would not see each
    other's assets.
    """
    if not SESSION_EMAIL or not SESSION_PASSWORD:
        pytest.skip("E2E_IMMICH_EMAIL / E2E_IMMICH_PASSWORD not set, and the sync routes reject API keys")
    async with httpx.AsyncClient(base_url=BASE, timeout=300) as client:
        login = await client.post("/auth/login", json={"email": SESSION_EMAIL, "password": SESSION_PASSWORD})
        login.raise_for_status()
        client.headers["Authorization"] = f"Bearer {login.json()['accessToken']}"
        try:
            yield client
        finally:
            await client.post("/auth/logout")


async def _make_fat_clip(path: Path) -> Path:
    """A deliberately over-bitrate MPEG-4 clip, so h265 has an easy win.

    The content must be unique per call: ffmpeg's synthetic sources are deterministic,
    and Immich deduplicates by checksum, so two identical clips would come back as
    ``status: "duplicate"`` and the test would assert against the wrong asset.
    """
    nonce = uuid.uuid4().hex
    code, _, stderr = await run_command(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x480:rate=25:duration=6",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={400 + secrets.randbelow(200)}:duration=6",
            "-f",
            "lavfi",
            "-i",
            "nullsrc=size=64x64:rate=25:duration=6,geq=random(1)*255:128:128,format=yuv420p",
            "-filter_complex",
            "[0:v][2:v]overlay=x=0:y=0[v]",
            "-map",
            "[v]",
            "-map",
            "1:a",
            "-c:v",
            "mpeg4",
            "-b:v",
            "9000k",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            "-metadata",
            "creation_time=2024-06-15T12:30:00Z",
            "-metadata",
            f"comment={nonce}",
            str(path),
        ],
        timeout_s=300,
    )
    assert code == 0, stderr
    # Bake GPS into the file itself, the way a real camera does. This matters: Immich
    # derives `timeZone` from GPS and computes `localDateTime` from `dateTimeOriginal`
    # in that zone. Setting GPS through the API *after* upload sets the zone but leaves
    # `localDateTime` stale, so original and replacement would disagree for reasons that
    # have nothing to do with this service.
    code, _, stderr = await run_command(
        [
            "exiftool",
            "-quiet",
            "-overwrite_original",
            "-api",
            "QuickTimeUTC=1",
            "-Keys:GPSCoordinates=48.2082, 16.3738",
            str(path),
        ],
        timeout_s=120,
    )
    assert code == 0, stderr
    return path


def _live_settings(tmp_path: Path) -> Settings:
    return Settings(
        immich={"base_url": BASE, "api_key": KEY, "timeout_s": 300},
        webhook={"token": "e2e-token"},
        behavior={
            "dry_run": False,
            "trash_original": False,
            "initial_delay_seconds": 0,
            "min_savings_bytes": 1024,
            "max_ratio": 0.8,
            "enabled_types": ["VIDEO"],
            "skip_if_named_people": True,
            "work_dir": tmp_path / "work",
        },
        presets=[
            Preset(
                name="video-h265",
                type="VIDEO",
                cmd="ffmpeg -y -loglevel error -i {input} -map_metadata 0 -map 0 "
                "-movflags use_metadata_tags+faststart -c:v libx265 -preset ultrafast "
                "-crf 30 -tag:v hvc1 -x265-params log-level=none:pools=2 -threads 2 "
                "-c:a aac -b:a 96k {output}",
                suffix=".mp4",
                timeout_s=1800,
            )
        ],
        database_path=tmp_path / "state.db",
    )


async def test_live_end_to_end(tmp_path: Path, api: ImmichClient, raw: httpx.AsyncClient) -> None:
    assert (await api.server_version()).startswith("3."), "expected an Immich v3 instance"

    # ---- arrange: upload a source asset and give it album, tags, rating, GPS -------
    clip = await _make_fat_clip(tmp_path / f"{MARKER}-source.mp4")
    stamp = datetime.now(UTC).strftime("%H%M%S")
    upload = await raw.post(
        "/assets",
        files={"assetData": (f"{MARKER}-{stamp}.mp4", clip.read_bytes(), "video/mp4")},
        data={
            "fileCreatedAt": "2024-06-15T12:30:00.000Z",
            "fileModifiedAt": "2024-06-15T12:30:00.000Z",
            "filename": f"{MARKER}-{stamp}.mp4",
            "duration": "6000",
        },
    )
    assert upload.status_code in (200, 201), upload.text
    source_id: str = upload.json()["id"]
    assert upload.json()["status"] == "created"

    tags = await api.upsert_tags([f"{MARKER}-tag-a", f"{MARKER}-tag-b"])
    await api.tag_assets([tag.id for tag in tags], [source_id])
    # Let Immich's metadata extraction settle before layering our own fields on top,
    # otherwise the extraction job overwrites them (see wait_for_metadata_extraction).
    await api.wait_for_metadata_extraction(source_id, timeout_s=60)
    response = await raw.put(
        f"/assets/{source_id}",
        json={"description": "e2e description", "rating": 4, "isFavorite": True},
    )
    response.raise_for_status()

    # ---- act: drive the real pipeline with a synthetic (but real-shaped) payload ---
    detail = (await raw.get(f"/assets/{source_id}")).json()
    payload = {
        "type": "AssetV1",
        "trigger": "AssetMetadataExtraction",
        "data": {
            "asset": {
                "id": source_id,
                "type": "VIDEO",
                "originalFileName": f"{MARKER}-{stamp}.mp4",
                "fileCreatedAt": "2024-06-15T12:30:00.000Z",
                "fileModifiedAt": "2024-06-15T12:30:00.000Z",
                "localDateTime": detail.get("localDateTime"),
                "isFavorite": True,
                "isExternal": False,
                "isEdited": False,
                "visibility": "timeline",
                "duration": 6000,
                "exifInfo": {
                    **detail.get("exifInfo", {}),
                    "tags": [f"{MARKER}-tag-a", f"{MARKER}-tag-b"],
                },
            }
        },
    }

    settings = _live_settings(tmp_path)
    new_id: str | None = None
    try:
        async with JobStore(settings.database_path) as store:
            await store.enqueue(source_id, payload, delay_seconds=0)
            job = await store.claim_next()
            assert job is not None
            await Pipeline(settings, api, store).run_job(job)
            job = await store.get(source_id)

        assert job is not None
        assert job.state is JobState.DONE, f"{job.state}: {job.skip_reason} {job.last_error}"
        new_id = job.new_asset_id
        assert new_id is not None
        assert job.ratio is not None and job.ratio < 0.8

        # ---- assert: everything transferable actually came across ------------------
        new_detail = (await raw.get(f"/assets/{new_id}")).json()
        exif = new_detail["exifInfo"]

        assert new_detail["originalFileName"].endswith(".cmp.mp4")
        assert new_detail["isFavorite"] is True  # via PUT /assets/copy
        assert exif["description"] == "e2e description"  # via PUT /assets/{id}
        assert exif["rating"] == 4
        assert exif["latitude"] == pytest.approx(48.2082, abs=1e-3)
        assert exif["longitude"] == pytest.approx(16.3738, abs=1e-3)
        assert exif["dateTimeOriginal"] is not None
        # Timeline position must not move.
        assert new_detail["localDateTime"] == detail["localDateTime"]

        new_tags = {tag["value"] for tag in new_detail.get("tags", [])}
        assert {f"{MARKER}-tag-a", f"{MARKER}-tag-b"} <= new_tags  # via PUT /tags/assets

        # Marker on both assets -> a second webhook is a no-op.
        marker_new = await api.has_metadata_key(new_id, "compressor")
        marker_old = await api.has_metadata_key(source_id, "compressor")
        assert marker_new is not None and marker_new.value["sourceId"] == source_id
        assert marker_old is not None and marker_old.value["replacedBy"] == new_id

        # ---- second webhook for the same asset is a no-op --------------------------
        async with JobStore(settings.database_path) as store:
            assert await store.enqueue(source_id, payload, delay_seconds=0) is False
            await store.delete(source_id)
            await store.enqueue(source_id, payload, delay_seconds=0)
            job2 = await store.claim_next()
            assert job2 is not None
            await Pipeline(settings, api, store).run_job(job2)
            job2 = await store.get(source_id)
        assert job2 is not None
        assert job2.state is JobState.SKIPPED
        assert job2.skip_reason is not None
        assert job2.skip_reason.value == "already_compressed"

        # ---- trash the original, then restore it (the documented rollback) ---------
        await api.delete_assets([source_id])
        assert (await raw.get(f"/assets/{source_id}")).json()["isTrashed"] is True
        await api.restore_assets([source_id])
        assert (await raw.get(f"/assets/{source_id}")).json()["isTrashed"] is False

    finally:
        ids = [source_id] + ([new_id] if new_id else [])
        await raw.request("DELETE", "/assets", json={"ids": ids, "force": True})


async def test_live_permanent_delete_leaves_nothing_behind(
    tmp_path: Path, api: ImmichClient, raw: httpx.AsyncClient
) -> None:
    """`delete_mode: permanent` + `retention_days: 0`: the original is gone when the job ends.

    The whole phase rests on `DELETE /assets {"force": true}` really bypassing the trash —
    the OpenAPI spec only claims "force delete even if in use". This asserts the stronger
    property directly against the server: no asset, and no trash entry either.
    """
    clip = await _make_fat_clip(tmp_path / f"{MARKER}-permanent.mp4")
    stamp = datetime.now(UTC).strftime("%H%M%S%f")
    upload = await raw.post(
        "/assets",
        files={"assetData": (f"{MARKER}-{stamp}.mp4", clip.read_bytes(), "video/mp4")},
        data={
            "fileCreatedAt": "2024-06-15T12:30:00.000Z",
            "fileModifiedAt": "2024-06-15T12:30:00.000Z",
            "filename": f"{MARKER}-{stamp}.mp4",
            "duration": "6000",
        },
    )
    assert upload.status_code in (200, 201), upload.text
    source_id: str = upload.json()["id"]
    await api.wait_for_metadata_extraction(source_id, timeout_s=60)
    detail = (await raw.get(f"/assets/{source_id}")).json()

    settings = _live_settings(tmp_path)
    settings.behavior.trash_original = True
    settings.behavior.retention_days = 0
    settings.behavior.delete_mode = "permanent"

    payload = {
        "type": "AssetV1",
        "trigger": "AssetMetadataExtraction",
        "data": {
            "asset": {
                "id": source_id,
                "type": "VIDEO",
                "originalFileName": f"{MARKER}-{stamp}.mp4",
                "fileCreatedAt": "2024-06-15T12:30:00.000Z",
                "fileModifiedAt": "2024-06-15T12:30:00.000Z",
                "localDateTime": detail.get("localDateTime"),
                "isFavorite": False,
                "isExternal": False,
                "isEdited": False,
                "visibility": "timeline",
                "duration": 6000,
                "exifInfo": {**detail.get("exifInfo", {}), "tags": []},
            }
        },
    }

    new_id: str | None = None
    try:
        async with JobStore(settings.database_path) as store:
            await store.enqueue(source_id, payload, delay_seconds=0)
            job = await store.claim_next()
            assert job is not None
            await Pipeline(settings, api, store).run_job(job)
            job = await store.get(source_id)

        assert job is not None
        assert job.state is JobState.DONE, f"{job.state}: {job.skip_reason} {job.last_error}"
        # Finished inside the job — the sweeper was never involved.
        assert job.delete_after is None
        new_id = job.new_asset_id
        assert new_id is not None

        # The checksum gate compared against a real server value, not a mock.
        assert job.new_checksum == (await raw.get(f"/assets/{new_id}")).json()["checksum"]

        # The original is not merely trashed — it is unreachable and absent from the trash.
        gone = await raw.get(f"/assets/{source_id}")
        assert gone.status_code in (400, 404), gone.text
        trashed = await raw.post("/search/metadata", json={"withDeleted": True, "isTrashed": True})
        trashed.raise_for_status()
        assert source_id not in {item["id"] for item in trashed.json()["assets"]["items"]}

        # Restoring is not a quiet no-op — the server rejects it outright, which is what
        # the CLI turns into an explanation instead of a traceback.
        with pytest.raises(ImmichError) as restore_failed:
            await api.restore_assets([source_id])
        assert restore_failed.value.status_code == 400
    finally:
        if new_id:
            await raw.request("DELETE", "/assets", json={"ids": [new_id], "force": True})


async def test_live_dry_run_changes_nothing(
    tmp_path: Path, api: ImmichClient, raw: httpx.AsyncClient
) -> None:
    """With ``dry_run: true`` the server state before and after must be byte-identical."""
    clip = await _make_fat_clip(tmp_path / f"{MARKER}-dry.mp4")
    stamp = datetime.now(UTC).strftime("%H%M%S%f")
    upload = await raw.post(
        "/assets",
        files={"assetData": (f"{MARKER}-dry-{stamp}.mp4", clip.read_bytes(), "video/mp4")},
        data={
            "fileCreatedAt": "2024-06-15T12:30:00.000Z",
            "fileModifiedAt": "2024-06-15T12:30:00.000Z",
            "filename": f"{MARKER}-dry-{stamp}.mp4",
        },
    )
    assert upload.json()["status"] == "created", upload.text
    source_id = upload.json()["id"]
    try:
        # Snapshot only once Immich's own async jobs have finished touching the asset,
        # otherwise the diff below picks up Immich's changes rather than ours.
        await api.wait_for_metadata_extraction(source_id, timeout_s=60)
        before = (await raw.get(f"/assets/{source_id}")).json()
        total_before = (await raw.post("/search/metadata", json={"size": 1})).json()["assets"]["total"]

        settings = _live_settings(tmp_path)
        settings.behavior.dry_run = True
        payload = {
            "type": "AssetV1",
            "trigger": "AssetMetadataExtraction",
            "data": {
                "asset": {
                    "id": source_id,
                    "type": "VIDEO",
                    "originalFileName": f"{MARKER}-dry-{stamp}.mp4",
                    "fileCreatedAt": "2024-06-15T12:30:00.000Z",
                    "fileModifiedAt": "2024-06-15T12:30:00.000Z",
                    "exifInfo": before.get("exifInfo", {}),
                }
            },
        }
        async with JobStore(settings.database_path) as store:
            await store.enqueue(source_id, payload, delay_seconds=0)
            job = await store.claim_next()
            assert job is not None
            await Pipeline(settings, api, store).run_job(job)
            job = await store.get(source_id)

        assert job is not None
        assert job.state is JobState.SKIPPED
        assert job.skip_reason is not None and job.skip_reason.value == "dry_run"

        after = (await raw.get(f"/assets/{source_id}")).json()
        total_after = (await raw.post("/search/metadata", json={"size": 1})).json()["assets"]["total"]

        assert total_after == total_before, "dry_run created an asset"
        assert json.dumps(after, sort_keys=True) == json.dumps(before, sort_keys=True)
        assert await api.has_metadata_key(source_id, "compressor") is None
    finally:
        await raw.request("DELETE", "/assets", json={"ids": [source_id], "force": True})


async def _read_sync_stream(client: httpx.AsyncClient, types: list[str]) -> list[dict[str, object]]:
    """One pass of `POST /sync/stream`, acking every batch as a real client would.

    Returns the parsed lines. Acking matters: the point of every assertion below is what
    the *next* pass contains, and that is decided entirely by the checkpoint this one
    leaves behind.

    Takes a client rather than using the API key directly: this route needs the `session`
    fixture's token, and passing `raw` here is the mistake that made this test skip
    silently for the whole life of the shim.
    """
    lines: list[dict[str, object]] = []
    async with client.stream("POST", "/sync/stream", json={"types": types}) as response:
        if response.status_code != 200:
            body = await response.aread()
            pytest.skip(
                f"this server/credential cannot open a sync session: {response.status_code} {body[:200]!r}"
            )
        async for line in response.aiter_lines():
            if line.strip():
                lines.append(json.loads(line))
    # The last ack of each type, which is what a real client sends. Acking only the final
    # line of the response does not work: every response ends with `SyncCompleteV1`, and
    # that ack advances no asset checkpoint — so the same backlog comes back on every pass
    # and the stream never drains. Measured on v3.1.0: six consecutive passes acking only
    # the last line returned the identical nine lines; acking per type drained in one.
    last_ack_per_type: dict[str, str] = {}
    for line in lines:
        kind, ack_value = line.get("type"), line.get("ack")
        if isinstance(kind, str) and isinstance(ack_value, str):
            last_ack_per_type[kind] = ack_value
    if last_ack_per_type:
        ack = await client.post("/sync/ack", json={"acks": list(last_ack_per_type.values())})
        ack.raise_for_status()
    return lines


def _offered_ids(lines: list[dict[str, object]]) -> set[str]:
    """The asset ids a pass actually offered, ignoring deletes and the terminator."""
    return {
        str(item["data"]["id"])  # type: ignore[index]
        for item in lines
        if isinstance(item.get("data"), dict) and "id" in item["data"]  # type: ignore[operator]
    }


async def _drain_sync_stream(client: httpx.AsyncClient, types: list[str], max_passes: int = 8) -> None:
    """Read and ack until a pass offers no assets at all.

    A fixed number of passes is not enough: metadata extraction keeps updating a freshly
    uploaded row for a moment, and any backlog on the instance has to clear first. What
    matters is only that a checkpoint is reached before the control asserts against it.
    """
    for _ in range(max_passes):
        if not _offered_ids(await _read_sync_stream(client, types)):
            return
    raise AssertionError(f"the sync stream still offered assets after {max_passes} acked passes")


async def test_live_touch_makes_the_sync_stream_reoffer_an_asset(
    tmp_path: Path, api: ImmichClient, raw: httpx.AsyncClient, session: httpx.AsyncClient
) -> None:
    """The one link in the shim's chain that source reading cannot settle.

    The shim can only rewrite a line the server actually sends, and the sync stream only
    offers assets whose `updateId` is newer than the client's checkpoint. Nothing changes
    about a replacement after it is created, so without a deliberate no-op update it would
    never be sent again and the translation would be armed but never delivered.

    Two halves, and the control is the important one:

    * after acking everything, a second pass must **not** contain the asset — otherwise the
      test proves nothing, because every pass would contain it;
    * after `touch_asset`, the next pass must contain it.

    That the trigger fires on a value-identical UPDATE was measured directly against
    PostgreSQL 16; this asserts the whole path through Immich's own API and query.
    """
    clip = await _make_fat_clip(tmp_path / f"{MARKER}-touch.mp4")
    stamp = datetime.now(UTC).strftime("%H%M%S%f")
    upload = await raw.post(
        "/assets",
        files={"assetData": (f"{MARKER}-{stamp}.mp4", clip.read_bytes(), "video/mp4")},
        data={
            "fileCreatedAt": "2024-06-15T12:30:00.000Z",
            "fileModifiedAt": "2024-06-15T12:30:00.000Z",
            "filename": f"{MARKER}-{stamp}.mp4",
            "duration": "6000",
        },
    )
    assert upload.status_code in (200, 201), upload.text
    asset_id: str = upload.json()["id"]

    try:
        await api.wait_for_metadata_extraction(asset_id, timeout_s=60)

        # Drain to a checkpoint, however many passes that takes.
        await _drain_sync_stream(session, ["AssetsV2"])

        # The control. If this fails, every pass contains everything and the assertion
        # below would pass for the wrong reason.
        assert asset_id not in _offered_ids(await _read_sync_stream(session, ["AssetsV2"]))

        await api.touch_asset(asset_id)

        assert asset_id in _offered_ids(await _read_sync_stream(session, ["AssetsV2"]))
    finally:
        await raw.request("DELETE", "/assets", json={"ids": [asset_id], "force": True})


async def test_live_touch_changes_nothing_a_user_would_see(
    tmp_path: Path, api: ImmichClient, raw: httpx.AsyncClient
) -> None:
    """It writes back the value it read. `isFavorite` must survive in both states."""
    clip = await _make_fat_clip(tmp_path / f"{MARKER}-touch-noop.mp4")
    stamp = datetime.now(UTC).strftime("%H%M%S%f")
    upload = await raw.post(
        "/assets",
        files={"assetData": (f"{MARKER}-{stamp}.mp4", clip.read_bytes(), "video/mp4")},
        data={
            "fileCreatedAt": "2024-06-15T12:30:00.000Z",
            "fileModifiedAt": "2024-06-15T12:30:00.000Z",
            "filename": f"{MARKER}-{stamp}.mp4",
            "duration": "6000",
        },
    )
    assert upload.status_code in (200, 201), upload.text
    asset_id: str = upload.json()["id"]

    try:
        await api.wait_for_metadata_extraction(asset_id, timeout_s=60)
        for favorite in (False, True):
            await api.update_asset(asset_id, UpdateAssetFields(is_favorite=favorite))
            before = (await raw.get(f"/assets/{asset_id}")).json()
            await api.touch_asset(asset_id)
            after = (await raw.get(f"/assets/{asset_id}")).json()

            assert after["isFavorite"] is favorite
            assert after["checksum"] == before["checksum"]
            assert after["originalFileName"] == before["originalFileName"]
    finally:
        await raw.request("DELETE", "/assets", json={"ids": [asset_id], "force": True})
