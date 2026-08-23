"""The ten pipeline steps for one asset, plus the worker loop and the trash sweeper.

Guiding rules:

* every step is idempotent, so a crash anywhere is recoverable by replaying from ``state``;
* nothing is destroyed before the replacement is confirmed to exist on the server;
* ``dry_run`` short-circuits before the first mutating call.

:func:`check_ingest_guards` is the odd one out — it runs in the webhook handler, before a
job exists at all. It lives here so that every guard is in one file.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import encoder
from .api import ImmichClient, ImmichError, sanitize_rating
from .config import BehaviorSettings, Preset, Settings
from .metrics import Histogram
from .models import (
    AssetDetail,
    Job,
    JobState,
    MetadataItem,
    RejectReason,
    SkipReason,
    UpdateAssetFields,
    WebhookAsset,
    WebhookPayload,
)
from .store import JobStore

logger = logging.getLogger(__name__)

# v1 -> v2: the sanity gate compared stored frame sizes and therefore rejected every
# rotated video as a resolution change. Markers written by v1 that record only a giving-up
# decision are re-tried once under the current gate; see `marker_blocks_reprocessing`.
MARKER_VERSION = 2


class SkipJob(Exception):  # noqa: N818 - control flow, not an error condition
    """Raised by a guard to abandon a job without touching anything."""

    def __init__(self, reason: SkipReason, detail: str = "") -> None:
        super().__init__(detail or reason.value)
        self.reason = reason
        self.detail = detail


class WebhookRejected(Exception):  # noqa: N818 - control flow, not an error condition
    """Raised by :func:`check_ingest_guards` to refuse a webhook before it becomes a job."""

    def __init__(self, reason: RejectReason, detail: str = "") -> None:
        super().__init__(detail or reason.value)
        self.reason = reason
        self.detail = detail


def check_ingest_guards(
    asset: WebhookAsset,
    behavior: BehaviorSettings,
    *,
    now: datetime | None = None,
) -> None:
    """Step 1a: decide whether this webhook is a new upload or a bulk re-trigger.

    Immich's workflow trigger is ``AssetMetadataExtraction``, and metadata extraction is a
    maintenance operation: one click on **Administration -> Jobs -> Extract Metadata**
    re-fires the workflow for every asset in the library, unbounded. Assets this service
    has already seen are immune — ``store.enqueue`` is ``ON CONFLICT DO NOTHING`` — but the
    ones it has never seen are not, and that is the entire library until it has been worked
    through.

    ``createdAt`` is when Immich created the database row, so it dates the *upload*, not
    the exposure. A webhook for a genuine upload arrives seconds after it; a re-trigger
    carries whatever age the asset already had. That is the whole discriminator, and unlike
    a rate limiter it does not fire on a legitimate import of a thousand photos, because
    every one of those is new.

    This runs in the webhook handler rather than in :func:`check_guards`, and the placement
    is load-bearing: a rejection must leave *no* row behind. ``backfill`` enqueues through
    the same ``ON CONFLICT DO NOTHING``, so a library recorded here as skipped would be a
    library ``backfill`` could never reach again.

    Raises :class:`WebhookRejected`; returns ``None`` when the asset may be queued.
    """
    limit_hours = behavior.max_asset_age_hours
    if limit_hours is None:
        return

    created = asset.created_at
    if created is None:
        # Fail closed. An Immich that stops sending `createdAt` makes this service inert
        # and loud, which is the correct direction for something that deletes originals:
        # the alternative is compressing a whole library on the assumption it is new.
        raise WebhookRejected(
            RejectReason.NO_CREATED_AT,
            "payload carries no createdAt, so a new upload cannot be told from a bulk re-trigger",
        )
    if created.tzinfo is None:  # pragma: no cover - live Immich always sends a UTC "Z"
        created = created.replace(tzinfo=UTC)

    age_hours = ((now or datetime.now(UTC)) - created).total_seconds() / 3600.0
    if age_hours > limit_hours:
        raise WebhookRejected(
            RejectReason.TOO_OLD,
            f"added to Immich {age_hours:.1f} h ago, past max_asset_age_hours {limit_hours:g} — "
            "this is a re-trigger, not a new upload; use `immich-compressor backfill` if it was meant",
        )


@dataclass(slots=True)
class PipelineStats:
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    deleted: int = 0
    bytes_saved: int = 0
    # Wall-clock time of the encoder command, for /metrics. Observed around the encode
    # itself, not around the whole job, so a slow download does not read as a slow encoder.
    encode_seconds: Histogram = field(default_factory=Histogram)


class SurgeDetector:
    """Rolling count of assets newly queued from webhooks, for the surge breaker.

    Deliberately in memory. The window is minutes wide, so losing it to a restart costs
    nothing — a surge still in progress simply re-trips within the next window. What must
    survive a restart is the *latch*, and that lives in the job store: restarting the
    container is the first thing an operator reaches for, and it must not be the thing that
    clears a pause.

    Only assets that were actually inserted count. A re-trigger for something already
    recorded is a no-op in the store and must not be one here either, or the breaker would
    fire on a replay that queues no work at all.
    """

    def __init__(self, threshold: int | None, window_seconds: float) -> None:
        self._threshold = threshold
        self._window = timedelta(seconds=window_seconds)
        self._seen: deque[datetime] = deque()

    def record(self, *, now: datetime | None = None) -> int | None:
        """Note one newly queued asset. Returns the count when this trips the breaker.

        Returns ``None`` while the rate is acceptable, and on every call after the trip —
        the caller latches on the first one, so reporting it twice would only produce a
        second log line about a pause that is already in force.
        """
        if self._threshold is None:
            return None
        moment = now or datetime.now(UTC)
        self._seen.append(moment)
        cutoff = moment - self._window
        while self._seen and self._seen[0] < cutoff:
            self._seen.popleft()
        if len(self._seen) <= self._threshold:
            return None
        tripped = len(self._seen)
        # Drop the window so the next webhook starts counting afresh. Without this every
        # further webhook would re-report a trip for as long as the window is saturated.
        self._seen.clear()
        return tripped


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
    if not settings.type_is_covered(asset.type):
        raise SkipJob(SkipReason.NO_PRESET, f"no preset matches type {asset.type}")

    filename = asset.original_file_name
    preset = settings.preset_for(asset.type, filename)
    if preset is None:
        # The type is covered, this file extension is not. An allowlist, because Immich
        # files RAW, PNG, GIF, TIFF and WebP under IMAGE just like JPEG — and a RAW that
        # reaches the encoder is developed to 8-bit and loses its sensor data for good.
        raise SkipJob(
            SkipReason.UNSUPPORTED_FORMAT,
            f"no {asset.type} preset accepts {Path(filename).suffix or 'a nameless file'!r}",
        )

    # The pre-download filter, and the only threshold here that needs no tuning: a file
    # cannot save more bytes than it has, so this can never reject something that would
    # have passed the gate after the encode.
    size = asset.exif_info.file_size_in_byte
    min_savings = preset.effective_min_savings_bytes(behavior)
    if size is not None and size < min_savings:
        raise SkipJob(
            SkipReason.TOO_SMALL,
            f"{size} bytes cannot save min_savings_bytes {min_savings}",
        )
    if behavior.compressed_marker in Path(asset.original_file_name).name:
        raise SkipJob(
            SkipReason.ALREADY_COMPRESSED,
            f"filename already carries the {behavior.compressed_marker!r} marker",
        )


def preflight(asset: WebhookAsset, settings: Settings) -> SkipReason | None:
    """The verdict :func:`check_guards` would reach, as a value instead of an exception.

    The backfill asks about assets it is *considering*, which is the same question the
    worker asks about a job — so it asks the same function. A second, "cheap" copy of the
    guard list in the CLI is how a scanner and a worker end up disagreeing about what is
    worth encoding, and the disagreement would show up as jobs skipped the moment they are
    claimed.

    Payload-decidable guards only, by construction: the named-people check and the
    compressor marker each cost a request per asset and stay where they are.
    """
    try:
        check_guards(asset, settings)
    except SkipJob as skip:
        return skip.reason
    return None


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


def marker_blocks_reprocessing(item: MetadataItem) -> bool:
    """Whether an existing compressor marker must stop us from touching the asset again.

    Two kinds of marker exist. One records that a replacement asset was created
    (``replacedBy``) — reprocessing that would produce a duplicate, so it always blocks.
    The other records that a run gave up, almost always at the sanity gate. Those are
    worth a second attempt when they predate the current :data:`MARKER_VERSION`, because
    the gate itself has changed since: v1 rejected every rotated video outright.

    Anything we cannot interpret blocks: a marker without a readable version is not
    evidence that reprocessing is safe.
    """
    if item.value.get("replacedBy"):
        return True
    version = item.value.get("v")
    return not (isinstance(version, int) and version < MARKER_VERSION)


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

        preset = settings.preset_for(asset.type, asset.original_file_name)
        if preset is None:  # pragma: no cover - check_guards already rejected this
            raise SkipJob(SkipReason.NO_PRESET, f"no preset for {asset.type}")

        # Hard loop guard: has this asset already been through the compressor?
        marker = await self._client.has_metadata_key(asset_id, behavior.metadata_key)
        if marker is not None:
            if marker_blocks_reprocessing(marker):
                raise SkipJob(SkipReason.ALREADY_COMPRESSED, "compressor marker present on the asset")
            logger.info(
                "%s carries a v%s marker without a replacement — re-trying under the current sanity gate",
                asset_id,
                marker.value.get("v"),
            )

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

        is_video = asset.type == "VIDEO"
        if not is_video:
            await self._check_still(source, preset)
        source_probe = await encoder.probe(source, is_still=not is_video)

        # --- Step 4: encode ------------------------------------------------------
        started = time.monotonic()
        result = await encoder.encode(source, preset, tmp)
        self.stats.encode_seconds.observe(time.monotonic() - started)
        logger.info(
            "encoded %s: %d -> %d bytes (ratio %.3f)",
            asset_id,
            result.orig_bytes,
            result.new_bytes,
            result.ratio,
        )

        # --- Step 4b: did the metadata actually survive? -------------------------
        if preset.exiftool_copy:
            await self._verify_metadata(source, result.output_path, asset_id)

        # --- Step 5: sanity gate -------------------------------------------------
        sanity = await encoder.check_sanity(
            source=source,
            result=result,
            source_probe=source_probe,
            behavior=behavior,
            preset=preset,
            is_video=is_video,
        )
        if not sanity.ok:
            # Mark the *original* so we do not burn CPU on it again on the next webhook.
            await self._safe_mark(
                asset_id,
                build_marker(source_id=asset_id, new_id=None, preset_name=preset.name, ratio=result.ratio),
                extra={"skipped": "no_gain", "detail": sanity.reason()[:300]},
            )
            await self._store.update(asset_id, new_bytes=result.new_bytes, ratio=round(result.ratio, 4))
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
                logger.info(
                    "upload of %s reported duplicate of %s — leaving original alone", asset_id, upload.id
                )
                await self._store.update(asset_id, new_asset_id=upload.id)
                raise SkipJob(SkipReason.DUPLICATE, f"server already has this file as {upload.id}")

            new_asset_id = upload.id
            await self._store.update(
                asset_id,
                state=JobState.UPLOADED,
                new_asset_id=new_asset_id,
                # Recorded now so the sweeper can still check it hours later, once the
                # local output file has long been cleaned up.
                new_checksum=result.checksum,
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
        # `PUT /assets/copy` usually gets tags, description, rating and GPS across in
        # v3.1.0, but only as a side effect of the copied XMP sidecar being re-extracted —
        # it does not copy those fields directly. That path is silent when the source has
        # no sidecar yet or `sidecar` is false, and it is order-sensitive against the
        # metadata extraction job. Writing them explicitly is idempotent and makes the
        # outcome independent of both.
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

        # --- Step 10: remove the original ----------------------------------------
        # Strictly after steps 7 and 8: `copy_asset` and `_apply_fields`/`_apply_tags`
        # both read from the source, so anything that removes it has to come last.
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

        await self._store.update(asset_id, state=JobState.PENDING_DELETE)
        if behavior.retention_days == 0:
            # No retention window asked for, so the 60 s sweeper interval would be pure
            # latency. Same code, same verification chain, just called here.
            await self.finalize_original(job, new_asset_id, result.checksum)
            return

        delete_after = datetime.now(UTC) + timedelta(days=behavior.retention_days)
        await self._store.update(asset_id, delete_after=delete_after)
        logger.info(
            "original %s scheduled for %s at %s",
            asset_id,
            behavior.delete_mode,
            delete_after.isoformat(),
        )

    # -------------------------------------------------------------- still guards

    async def _check_still(self, source: Path, preset: Preset) -> None:
        """Reasons a downloaded still must not be encoded at all. Raises :class:`SkipJob`.

        Both checks need the file, so they cannot move into ``check_guards``. They still
        pay for themselves: they cost one ``exiftool``/``identify`` call and save the whole
        encode, the upload and — in the motion-photo case — the original.
        """
        embedded = await encoder.embedded_media_reason(source)
        if embedded is not None:
            # Re-encoding would drop the appended video while every downstream check
            # reports success: the metadata copy carries the motion-photo markers across
            # faithfully, and the size ratio looks *better* for the missing megabytes.
            raise SkipJob(SkipReason.EMBEDDED_MEDIA, embedded)

        if preset.min_source_quality is None:
            return
        quality = await encoder.jpeg_quality(source)
        if quality is not None and quality < preset.min_source_quality:
            raise SkipJob(
                SkipReason.SOURCE_QUALITY,
                f"source is already q{quality}, below min_source_quality "
                f"{preset.min_source_quality} — a re-encode would only add artefacts",
            )

    async def _verify_metadata(self, source: Path, output: Path, asset_id: str) -> None:
        """Compare the source's EXIF/GPS/XMP/IPTC against the encoded file.

        ``metadata_verify: strict`` turns a difference into a job failure, which leaves the
        original untouched and puts the asset in ``failed`` where it is visible. ``warn``
        only logs — and the config refuses that combination together with
        ``delete_mode: permanent``, because a warning cannot undo a force-deleted original.
        """
        differences = await encoder.verify_metadata(source, output)
        if not differences:
            return
        summary = "; ".join(differences[:10])
        if len(differences) > 10:
            summary += f" (+{len(differences) - 10} more)"
        if self._settings.behavior.metadata_verify == "strict":
            raise RuntimeError(f"metadata carry-over incomplete: {summary}")
        logger.warning("metadata carry-over incomplete for %s: %s", asset_id, summary)

    # ------------------------------------------------------------------ deletion

    async def finalize_original(self, job: Job, new_asset_id: str, expected_checksum: str | None) -> bool:
        """Step 10b: verify the replacement, then remove the original.

        The single place the original is ever deleted. Two callers: ``_run_media_steps``
        inline when ``retention_days == 0``, and ``Worker._trash_one`` once the retention
        window has elapsed. Returns ``True`` when the original is gone.

        Anything that does not check out leaves the job in ``pending_delete`` with a
        one-hour backoff, so a transient server state costs a retry rather than the
        original.
        """
        asset_id = job.source_asset_id
        permanent = self._settings.behavior.delete_mode == "permanent"
        try:
            problem = await self._verify_replacement(new_asset_id, expected_checksum)
            if problem is not None:
                logger.error("refusing to delete %s: %s", asset_id, problem)
                await self._defer_deletion(asset_id, problem)
                return False
            # Deliberately *not* POST /trash/empty: that endpoint drops the user's entire
            # trash, including assets they deleted by hand and may still want back. A
            # force delete on the one asset id we are responsible for reclaims the same
            # space with no collateral damage. Do not "simplify" this.
            await self._client.delete_assets([asset_id], force=permanent)
        except ImmichError as exc:
            logger.error("could not delete %s: %s", asset_id, exc)
            await self._defer_deletion(asset_id, str(exc))
            return False

        await self._store.update(asset_id, state=JobState.DONE, delete_after=None)
        self.stats.deleted += 1
        logger.info(
            "original %s %s (replacement %s)",
            asset_id,
            "permanently deleted — not recoverable" if permanent else "moved to trash",
            new_asset_id,
        )
        return True

    async def _verify_replacement(self, new_asset_id: str, expected_checksum: str | None) -> str | None:
        """The gate in front of the delete. Returns the first failure, or ``None``.

        All four conditions are checked in both delete modes. In ``trash`` mode the delete
        is undoable and the chain is merely cheap insurance; in ``permanent`` mode it is
        the only thing standing between a bad upload and a lost original — and running it
        in both modes means a deployment discovers a failing condition while the delete is
        still reversible.
        """
        replacement = await self._client.get_asset(new_asset_id)

        # 1. The replacement is there and is not itself on its way out.
        if replacement.is_trashed:
            return f"replacement {new_asset_id} is itself trashed"

        # 2. The server stored exactly the bytes we uploaded.
        if expected_checksum is None:
            return f"no checksum recorded for the file uploaded as {new_asset_id}"
        if replacement.checksum is None:
            return f"replacement {new_asset_id} reports no checksum"
        if replacement.checksum != expected_checksum:
            return (
                f"checksum mismatch on {new_asset_id}: "
                f"server {replacement.checksum!r} != uploaded {expected_checksum!r}"
            )

        # 3. Metadata extraction ran — otherwise the replacement has no capture date and
        #    would land at the wrong place in the timeline.
        if not replacement.exif_info.date_time_original:
            return f"replacement {new_asset_id} has no exifInfo.dateTimeOriginal"

        # 4. Step 9 got its marker written, so the replacement is traceable back here.
        key = self._settings.behavior.metadata_key
        if await self._client.has_metadata_key(new_asset_id, key) is None:
            return f"replacement {new_asset_id} carries no {key!r} marker"

        return None

    async def _defer_deletion(self, asset_id: str, error: str) -> None:
        """Back off an hour and stay in ``pending_delete`` so the sweeper tries again."""
        await self._store.reschedule(asset_id, delay_seconds=3600.0, error=error)
        await self._store.update(
            asset_id,
            state=JobState.PENDING_DELETE,
            delete_after=datetime.now(UTC) + timedelta(hours=1),
        )

    # ------------------------------------------------------------------ helpers

    async def _apply_fields(self, asset: WebhookAsset, source_detail: AssetDetail, new_asset_id: str) -> None:
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

    async def _apply_tags(self, asset: WebhookAsset, source_detail: AssetDetail, new_asset_id: str) -> None:
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
        # One lane per enabled asset type, `concurrency` workers each. Without the split a
        # single clip with `timeout_s: 7200` holds the only worker for two hours while
        # every one-second image job queues up behind it.
        behavior = self._settings.behavior
        for asset_type in behavior.enabled_types:
            for index in range(behavior.concurrency):
                name = f"worker-{asset_type.lower()}-{index}"
                self._tasks.append(asyncio.create_task(self._loop(name, (asset_type,)), name=name))
        self._tasks.append(asyncio.create_task(self._sweeper(), name="trash-sweeper"))
        logger.info(
            "worker started (lanes=%s, concurrency=%d each, dry_run=%s, trash_original=%s, "
            "delete_mode=%s, retention_days=%d)",
            ",".join(behavior.enabled_types),
            self._settings.behavior.concurrency,
            self._settings.behavior.dry_run,
            self._settings.behavior.trash_original,
            self._settings.behavior.delete_mode,
            self._settings.behavior.retention_days,
        )

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    async def _loop(self, name: str, types: tuple[str, ...]) -> None:
        interval = self._settings.behavior.poll_interval_seconds
        while not self._stop.is_set():
            try:
                # Checked before the claim, not after: a paused service must not move a job
                # into `running` and then abandon it there.
                if await self._store.pause_state() is not None:
                    await asyncio.sleep(interval)
                    continue
                job = await self._store.claim_next(types=types)
                if job is None:
                    await asyncio.sleep(interval)
                    continue
                logger.info("%s picked up %s", name, job.source_asset_id)
                await self.pipeline.run_job(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("%s loop error", name)
                await asyncio.sleep(interval)

    async def _sweeper(self) -> None:
        """Step 10b for jobs with a retention window: remove originals once they are due.

        Jobs configured with ``retention_days: 0`` never reach this loop — the pipeline
        finalises them inline, so the 60 s interval costs them nothing.

        Nothing is finalised while the surge breaker is latched. Jobs simply stay in
        ``pending_delete`` until somebody resumes, which is the recoverable direction.
        """
        while not self._stop.is_set():
            try:
                await asyncio.sleep(60.0)
                if not self._settings.behavior.trash_original:
                    continue
                # The whole point of the breaker: a surge must not keep deleting originals
                # while nobody has confirmed that the surge was intended.
                if await self._store.pause_state() is not None:
                    continue
                for job in await self._store.due_deletions():
                    await self._trash_one(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("trash sweeper error")

    async def _trash_one(self, job: Job) -> None:
        """The sweeper's call into the shared finaliser."""
        if not job.new_asset_id:
            logger.error("refusing to delete %s: no replacement asset recorded", job.source_asset_id)
            await self._store.mark_failed(job.source_asset_id, "no replacement asset recorded")
            return
        await self.pipeline.finalize_original(job, job.new_asset_id, job.new_checksum)
