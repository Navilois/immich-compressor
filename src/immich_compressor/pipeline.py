"""The ten pipeline steps for one asset, plus the worker loop and the trash sweeper.

Guiding rules:

* every step is idempotent, so a crash anywhere is recoverable by replaying from ``state``;
* nothing is destroyed before the replacement is confirmed to exist on the server;
* ``dry_run`` short-circuits before the first mutating call.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import encoder
from .api import ImmichClient, ImmichError, sanitize_rating
from .config import Preset, Settings
from .models import (
    AssetDetail,
    Job,
    JobState,
    MetadataItem,
    SkipReason,
    UpdateAssetFields,
    WebhookAsset,
    WebhookPayload,
)
from .store import JobStore

logger = logging.getLogger(__name__)

MARKER_VERSION = 1


class SkipJob(Exception):  # noqa: N818 - control flow, not an error condition
    """Raised by a guard to abandon a job without touching anything."""

    def __init__(self, reason: SkipReason, detail: str = "") -> None:
        super().__init__(detail or reason.value)
        self.reason = reason
        self.detail = detail


@dataclass(slots=True)
class PipelineStats:
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    deleted: int = 0
    bytes_saved: int = 0


def check_guards(asset: WebhookAsset, settings: Settings) -> None:
    """Steps 2a-2f: everything decidable from the webhook payload alone.

    Raises :class:`SkipJob`; returns ``None`` when the asset may be processed.
    """
    behavior = settings.behavior

    if asset.is_external or asset.library_id:
        raise SkipJob(SkipReason.EXTERNAL_LIBRARY, "asset belongs to an external library")
    if asset.is_edited:
        raise SkipJob(SkipReason.EDITED, "asset has non-destructive edits attached")
    if asset.live_photo_video_id:
        raise SkipJob(SkipReason.LIVE_PHOTO, "asset is one half of a live photo")
    if (asset.visibility or "").lower() == "locked":
        raise SkipJob(SkipReason.LOCKED, "asset is in the locked folder")
    if asset.deleted_at is not None or (asset.status or "").lower() == "trashed":
        raise SkipJob(SkipReason.TRASHED, "asset is already in the trash")
    if asset.type not in behavior.enabled_types:
        raise SkipJob(SkipReason.WRONG_TYPE, f"type {asset.type} is not enabled")
    if settings.preset_for(asset.type) is None:
        raise SkipJob(SkipReason.NO_PRESET, f"no preset matches type {asset.type}")

    size = asset.exif_info.file_size_in_byte
    if size is not None and size < behavior.min_size_bytes:
        raise SkipJob(
            SkipReason.TOO_SMALL, f"{size} bytes < min_size_bytes {behavior.min_size_bytes}"
        )
    if behavior.compressed_marker in Path(asset.original_file_name).name:
        raise SkipJob(
            SkipReason.ALREADY_COMPRESSED,
            f"filename already carries the {behavior.compressed_marker!r} marker",
        )


def build_marker(
    *,
    source_id: str,
    new_id: str | None,
    preset_name: str,
    ratio: float | None,
) -> MetadataItem:
    """The metadata KV value written to both assets. Free keys and nested objects are
    accepted by Immich v3.1 — verified against a live instance."""
    value: dict[str, object] = {
        "v": MARKER_VERSION,
        "sourceId": source_id,
        "preset": preset_name,
        "at": datetime.now(UTC).isoformat(),
    }
    if new_id:
        value["replacedBy"] = new_id
    if ratio is not None:
        value["ratio"] = round(ratio, 4)
    return MetadataItem(key="compressor", value=value)


class Pipeline:
    """Executes one job at a time against the Immich API."""

    def __init__(self, settings: Settings, client: ImmichClient, store: JobStore) -> None:
        self._settings = settings
        self._client = client
        self._store = store
        self.stats = PipelineStats()

    # ----------------------------------------------------------------- one job

    async def run_job(self, job: Job) -> None:
        """Drive a single job to a terminal or deferred state. Never raises."""
        asset_id = job.source_asset_id
        try:
            payload = WebhookPayload.model_validate(json.loads(job.payload))
            asset = payload.data.asset
        except Exception as exc:
            logger.exception("job %s has an unparseable payload", asset_id)
            await self._store.mark_failed(asset_id, f"bad payload: {exc}")
            self.stats.failed += 1
            return

        try:
            await self._process(job, asset)
        except SkipJob as skip:
            logger.info("skip %s (%s): %s", asset_id, skip.reason.value, skip.detail)
            await self._store.mark_skipped(asset_id, skip.reason)
            self.stats.skipped += 1
        except Exception as exc:  # noqa: BLE001 - the worker must never die on one job
            await self._handle_failure(job, exc)

    async def _handle_failure(self, job: Job, exc: Exception) -> None:
        asset_id = job.source_asset_id
        max_attempts = self._settings.behavior.max_attempts
        if job.attempts >= max_attempts:
            logger.error("job %s failed permanently after %d attempts: %s", asset_id, job.attempts, exc)
            await self._store.mark_failed(asset_id, str(exc))
            self.stats.failed += 1
            return
        delay = min(60.0 * (2 ** (job.attempts - 1)), 3600.0)
        logger.warning(
            "job %s failed (attempt %d/%d), retrying in %.0fs: %s",
            asset_id,
            job.attempts,
            max_attempts,
            delay,
            exc,
        )
        await self._store.reschedule(asset_id, delay_seconds=delay, error=str(exc))

    async def _process(self, job: Job, asset: WebhookAsset) -> None:
        settings = self._settings
        behavior = settings.behavior
        asset_id = asset.id

        # --- Step 2: guards ------------------------------------------------------
        check_guards(asset, settings)

        preset = settings.preset_for(asset.type)
        if preset is None:  # pragma: no cover - check_guards already rejected this
            raise SkipJob(SkipReason.NO_PRESET, f"no preset for {asset.type}")

        # Hard loop guard: has this asset already been through the compressor?
        if await self._client.has_metadata_key(asset_id, behavior.metadata_key):
            raise SkipJob(SkipReason.ALREADY_COMPRESSED, "compressor marker present on the asset")

        # Always re-read the source. The webhook payload is a snapshot from the moment
        # metadata extraction finished, but we deliberately process `initial_delay_seconds`
        # later (default 5 minutes) — by then the user may have added tags, a description
        # or a rating. The live state is authoritative for step 8; the payload is only the
        # trigger.
        detail = await self._client.get_asset(asset_id)
        if detail.is_trashed:
            raise SkipJob(SkipReason.TRASHED, "asset is in the trash")
        if behavior.skip_if_named_people:
            named = detail.named_people()
            if named:
                raise SkipJob(SkipReason.NAMED_PEOPLE, f"named people attached: {', '.join(named)}")

        # --- dry run stops here, before anything mutating ------------------------
        if behavior.dry_run:
            logger.info(
                "[dry-run] would compress asset %s (%s, %s bytes) with preset %r",
                asset_id,
                asset.original_file_name,
                asset.exif_info.file_size_in_byte,
                preset.name,
            )
            await self._store.update(
                asset_id,
                state=JobState.SKIPPED,
                skip_reason=SkipReason.DRY_RUN,
                orig_bytes=asset.exif_info.file_size_in_byte,
            )
            self.stats.skipped += 1
            return

        behavior.work_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=behavior.work_dir, prefix="job-") as tmp_name:
            tmp = Path(tmp_name)
            await self._run_media_steps(job, asset, detail, preset, tmp)

    async def _run_media_steps(
        self,
        job: Job,
        asset: WebhookAsset,
        source_detail: AssetDetail,
        preset: Preset,
        tmp: Path,
    ) -> None:
        behavior = self._settings.behavior
        asset_id = asset.id

        # --- Step 3: download ----------------------------------------------------
        expected = asset.exif_info.file_size_in_byte or 0
        needed = int(max(expected, 1) * behavior.free_space_factor)
        if not encoder.has_free_space(tmp, needed):
            raise RuntimeError(f"not enough free space in {tmp} (need ~{needed} bytes)")

        source = tmp / Path(asset.original_file_name).name
        orig_bytes = await self._client.download_original(asset_id, source)
        await self._store.update(asset_id, orig_bytes=orig_bytes)
        logger.info("downloaded %s (%d bytes)", asset_id, orig_bytes)

        source_probe = await encoder.probe(source)
        is_video = asset.type == "VIDEO"

        # --- Step 4: encode ------------------------------------------------------
        result = await encoder.encode(source, preset, tmp)
        logger.info(
            "encoded %s: %d -> %d bytes (ratio %.3f)",
            asset_id,
            result.orig_bytes,
            result.new_bytes,
            result.ratio,
        )

        # --- Step 5: sanity gate -------------------------------------------------
        sanity = await encoder.check_sanity(
            source=source,
            result=result,
            source_probe=source_probe,
            behavior=behavior,
            is_video=is_video,
        )
        if not sanity.ok:
            # Mark the *original* so we do not burn CPU on it again on the next webhook.
            await self._safe_mark(
                asset_id,
                build_marker(
                    source_id=asset_id, new_id=None, preset_name=preset.name, ratio=result.ratio
                ),
                extra={"skipped": "no_gain", "detail": sanity.reason()[:300]},
            )
            await self._store.update(
                asset_id, new_bytes=result.new_bytes, ratio=round(result.ratio, 4)
            )
            raise SkipJob(SkipReason.NO_GAIN, sanity.reason())

        # --- Step 6: upload ------------------------------------------------------
        new_asset_id = job.new_asset_id
        if new_asset_id is None:
            filename = encoder.compressed_filename(
                asset.original_file_name, behavior.compressed_marker, preset.suffix
            )
            created = asset.file_created_at or datetime.now(UTC)
            modified = asset.file_modified_at or created
            duration_ms = asset.duration if is_video else None
            if duration_ms is None and is_video and result.probe.duration_s is not None:
                duration_ms = round(result.probe.duration_s * 1000)

            upload = await self._client.upload_asset(
                result.output_path,
                filename=filename,
                file_created_at=created,
                file_modified_at=modified,
                duration_ms=duration_ms,
                is_favorite=asset.is_favorite,
                visibility=asset.visibility,
            )
            if upload.status == "duplicate":
                logger.info("upload of %s reported duplicate of %s — leaving original alone",
                            asset_id, upload.id)
                await self._store.update(asset_id, new_asset_id=upload.id)
                raise SkipJob(SkipReason.DUPLICATE, f"server already has this file as {upload.id}")

            new_asset_id = upload.id
            await self._store.update(
                asset_id,
                state=JobState.UPLOADED,
                new_asset_id=new_asset_id,
                new_bytes=result.new_bytes,
                ratio=round(result.ratio, 4),
            )
            logger.info("uploaded %s -> new asset %s", asset_id, new_asset_id)

        # --- Step 7: copy transferable links -------------------------------------
        await self._client.copy_asset(asset_id, new_asset_id)

        # Immich extracts metadata asynchronously right after the upload and overwrites
        # description/rating from the file. Wait for that job, otherwise step 8 writes
        # into a value that is about to be clobbered.
        if behavior.post_upload_settle_s > 0:
            await self._client.wait_for_metadata_extraction(
                new_asset_id, timeout_s=behavior.post_upload_settle_s
            )

        # --- Step 8: make the carry-over deterministic ---------------------------
        # `PUT /assets/copy` already transfers tags, description, rating and GPS in
        # v3.1.0 (contrary to the plan), but it is order-sensitive against the metadata
        # extraction job. Writing them explicitly is idempotent and makes the outcome
        # independent of that race.
        await self._apply_fields(asset, source_detail, new_asset_id)
        await self._apply_tags(asset, source_detail, new_asset_id)
        await self._store.update(asset_id, state=JobState.LINKED)

        # --- Step 9: markers on both assets --------------------------------------
        marker = build_marker(
            source_id=asset_id,
            new_id=new_asset_id,
            preset_name=preset.name,
            ratio=result.ratio,
        )
        await self._safe_mark(new_asset_id, marker)
        await self._safe_mark(asset_id, marker)

        # --- Step 10: delayed trash ----------------------------------------------
        self.stats.processed += 1
        self.stats.bytes_saved += max(result.orig_bytes - result.new_bytes, 0)

        if not behavior.trash_original:
            logger.info(
                "trash_original is false — original %s stays; replacement is %s",
                asset_id,
                new_asset_id,
            )
            await self._store.update(asset_id, state=JobState.DONE)
            return

        delete_after = datetime.now(UTC) + timedelta(days=behavior.retention_days)
        await self._store.update(
            asset_id, state=JobState.PENDING_DELETE, delete_after=delete_after
        )
        logger.info("original %s scheduled for trash at %s", asset_id, delete_after.isoformat())

    # ------------------------------------------------------------------ helpers

    async def _apply_fields(
        self, asset: WebhookAsset, source_detail: AssetDetail, new_asset_id: str
    ) -> None:
        """Step 8a: description / rating / GPS / capture date.

        The live source state wins over the webhook snapshot: the payload was produced at
        metadata-extraction time, and we run `initial_delay_seconds` later. The re-encode
        also drops XMP Description/Rating even when ffmpeg keeps the container tags, so
        these have to be written rather than inferred from the new file.
        """
        live = source_detail.exif_info
        stale = asset.exif_info
        fields = UpdateAssetFields(
            description=live.description or stale.description or None,
            rating=sanitize_rating(live.rating if live.rating is not None else stale.rating),  # type: ignore[arg-type]
            latitude=live.latitude if live.latitude is not None else stale.latitude,
            longitude=live.longitude if live.longitude is not None else stale.longitude,
            date_time_original=live.date_time_original or stale.date_time_original,
        )
        if fields.is_empty():
            return
        await self._client.update_asset(new_asset_id, fields)

    async def _apply_tags(
        self, asset: WebhookAsset, source_detail: AssetDetail, new_asset_id: str
    ) -> None:
        """Step 8b: tags.

        The live source carries real tag objects; the webhook payload only has names in
        `exifInfo.tags`. Prefer the live list, fall back to the payload. `PUT /tags` is an
        upsert by name and `PUT /tags/assets` is idempotent (a repeat returns count 0).
        """
        names = [tag.value or tag.name for tag in source_detail.tags if (tag.value or tag.name).strip()]
        if not names:
            names = [name for name in asset.exif_info.tags if name and name.strip()]
        if not names:
            return
        tags = await self._client.upsert_tags(names)
        await self._client.tag_assets([tag.id for tag in tags], [new_asset_id])

    async def _safe_mark(
        self,
        asset_id: str,
        item: MetadataItem,
        *,
        extra: dict[str, object] | None = None,
    ) -> None:
        """Write the marker; never let a marker failure undo real work."""
        value = dict(item.value)
        if extra:
            value.update(extra)
        try:
            await self._client.put_metadata(asset_id, [MetadataItem(key=item.key, value=value)])
        except ImmichError as exc:
            logger.warning("could not write compressor marker on %s: %s", asset_id, exc)


class Worker:
    """Pulls due jobs off the store, one at a time per task."""

    def __init__(self, settings: Settings, client: ImmichClient, store: JobStore) -> None:
        self._settings = settings
        self._client = client
        self._store = store
        self.pipeline = Pipeline(settings, client, store)
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()

    async def start(self) -> None:
        for index in range(self._settings.behavior.concurrency):
            self._tasks.append(asyncio.create_task(self._loop(index), name=f"worker-{index}"))
        self._tasks.append(asyncio.create_task(self._sweeper(), name="trash-sweeper"))
        logger.info(
            "worker started (concurrency=%d, dry_run=%s, trash_original=%s)",
            self._settings.behavior.concurrency,
            self._settings.behavior.dry_run,
            self._settings.behavior.trash_original,
        )

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    async def _loop(self, index: int) -> None:
        interval = self._settings.behavior.poll_interval_seconds
        while not self._stop.is_set():
            try:
                job = await self._store.claim_next()
                if job is None:
                    await asyncio.sleep(interval)
                    continue
                logger.info("worker-%d picked up %s", index, job.source_asset_id)
                await self.pipeline.run_job(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("worker-%d loop error", index)
                await asyncio.sleep(interval)

    async def _sweeper(self) -> None:
        """Step 10b: move due originals into the trash (soft delete)."""
        while not self._stop.is_set():
            try:
                await asyncio.sleep(60.0)
                if not self._settings.behavior.trash_original:
                    continue
                for job in await self._store.due_deletions():
                    await self._trash_one(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("trash sweeper error")

    async def _trash_one(self, job: Job) -> None:
        asset_id = job.source_asset_id
        if not job.new_asset_id:
            logger.error("refusing to trash %s: no replacement asset recorded", asset_id)
            await self._store.mark_failed(asset_id, "no replacement asset recorded")
            return
        try:
            # Last-second safety: the replacement must still exist and not be trashed.
            replacement = await self._client.get_asset(job.new_asset_id)
            if replacement.is_trashed:
                logger.error(
                    "refusing to trash %s: replacement %s is itself trashed",
                    asset_id,
                    job.new_asset_id,
                )
                await self._store.mark_failed(asset_id, "replacement is trashed")
                return
            await self._client.delete_assets([asset_id])
        except ImmichError as exc:
            logger.error("could not trash %s: %s", asset_id, exc)
            await self._store.reschedule(asset_id, delay_seconds=3600.0, error=str(exc))
            await self._store.update(asset_id, state=JobState.PENDING_DELETE)
            return
        await self._store.update(asset_id, state=JobState.DONE)
        self.pipeline.stats.deleted += 1
        logger.info("original %s moved to trash (replacement %s)", asset_id, job.new_asset_id)
