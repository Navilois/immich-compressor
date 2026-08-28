"""The ten pipeline steps for one asset, plus the worker loop and the trash sweeper.

Guiding rules:

* every step is idempotent, so a crash anywhere is recoverable by replaying from ``state``;
* nothing is destroyed before the replacement is confirmed to exist on the server;
* ``dry_run`` short-circuits before the first mutating call.

Everything here is about a job that exists. What decides whether a webhook may *become* a
job — the freshness gate and the surge breaker — is in :mod:`~immich_compressor.ingest`,
which the webhook handler reaches directly rather than through this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import encoder
from .api import ImmichClient, ImmichError, sanitize_rating
from .config import Preset, Settings
from .metrics import Histogram
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
from .store import SHIM_GATES_OPENED, SHIM_TOUCHES, JobStore

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

    def as_dict(self) -> dict[str, int]:
        """The five counters, for ``/stats`` and ``/metrics``.

        Written out rather than derived from the fields, because ``encode_seconds`` is a
        histogram and belongs to neither surface: ``/stats`` cannot serialise it and
        ``/metrics`` renders it through its own block.
        """
        return {
            "processed": self.processed,
            "skipped": self.skipped,
            "failed": self.failed,
            "deleted": self.deleted,
            "bytes_saved": self.bytes_saved,
        }


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

        # The ledger, written before anything mutating: what this asset hashed to and who
        # owns it. Once the original is deleted the server forgets its checksum, so this
        # row is the only remaining way to recognise the same bytes coming back.
        await self._store.update(asset_id, source_checksum=detail.checksum, owner_id=detail.owner_id)
        await self._check_re_upload(asset_id, detail)

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
        result = await encoder.encode(
            source,
            preset,
            tmp,
            transcode_unsupported_audio=preset.effective_transcode_unsupported_audio(behavior),
        )
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
                # With `source_checksum` and `owner_id` already written in step 2, this row
                # is a full ledger entry the moment `new_asset_id` lands — so the shim will
                # build a `sync_rewrite` for it either way. `upload_check` is the half that
                # is built from `new_checksum` alone, and it is the half that matters here:
                # it is what answers a device asking "do you already have this hash?" during
                # the window before the rewritten row has reached its mirror.
                await self._store.update(
                    asset_id,
                    new_asset_id=upload.id,
                    new_checksum=await self._duplicate_checksum(upload.id, result.checksum),
                )
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

    # ------------------------------------------------------------ ledger guard

    async def _check_re_upload(self, asset_id: str, detail: AssetDetail) -> None:
        """Recognise an original this service already replaced, arriving a second time.

        A device that still holds the file has no way to know the server ever had it: the
        app decides what to back up by joining its local checksums against the assets it
        has mirrored, and a deleted asset leaves no trace in that mirror. So the same bytes
        come back as a *new* asset, with a new id and no compressor marker, and the loop
        guard in step 2 cannot see it. This can.

        It never deletes and never touches the asset. The job stops at ``re_uploaded``,
        which is the whole point — the operator learns that a device is re-uploading, and
        the file is not put through a second generation of the same encode.

        The verdict is stable: ``reprocess`` and ``requeue`` both re-run this check and
        reach it again, exactly as they do for an asset carrying a compressor marker. The
        bytes have been compressed once; wanting them compressed twice is not a state the
        pipeline offers.
        """
        earlier = await self._store.find_replaced_original(
            checksum=detail.checksum,
            owner_id=detail.owner_id,
            exclude_asset_id=asset_id,
        )
        if earlier is None:
            return
        raise SkipJob(
            SkipReason.RE_UPLOADED,
            f"same bytes as {earlier.source_asset_id}, replaced by "
            f"{earlier.new_asset_id} on {earlier.updated_at.date().isoformat()} — a device "
            f"still holding the original has uploaded it again",
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
        if permanent:
            await self._free_original_checksum(job, new_asset_id)
        return True

    async def _free_original_checksum(self, job: Job, new_asset_id: str) -> None:
        """Record that the original's checksum is nobody's any more, and get it handed over.

        Only after a *permanent* delete, which is the one case this service witnesses. In
        ``trash`` mode the row survives, holding its checksum, until Immich's retention
        expires up to a month later — an event that happens entirely inside Immich and is
        never reported here. The shim watches the sync stream for it instead.

        The gate is recorded whether or not the shim is running: it is a fact about the
        server, it costs one UPDATE, and a deployment that turns the shim on later wants
        the history. The touch is a write against the library, so it is only worth making
        when something is actually listening for the result.

        The ledger pair is read back from the store, never off ``job``. Step 2 writes those
        two columns to the row and the inline caller then carries the *same* object all the
        way down here, so ``job.source_checksum`` still holds what it did when the job was
        claimed — ``None`` on every job this service has ever processed. The sweeper's job
        is freshly loaded and carries the same values as the row, so both callers read one
        source of truth. Only the pair is stale; ``job.source_asset_id`` is the identity.

        Both counters are bumped here, exactly as the shim bumps both on the ``trash``
        path, and they part company where the writes do: ``shim_gates_opened`` follows the
        UPDATE and is therefore unconditional, ``shim_touches`` follows the touch and is
        therefore not.
        """
        current = await self._store.get(job.source_asset_id)
        if current is None or not (current.source_checksum and current.owner_id):
            return  # A job from before the ledger existed. Nothing to translate to.
        if not await self._store.mark_original_freed(job.source_asset_id):
            return
        await self._store.bump_counter(SHIM_GATES_OPENED)
        shim = self._settings.shim
        if not shim.enabled or shim.log_only or not shim.rewrite_sync_stream:
            return
        try:
            await self._client.touch_asset(new_asset_id)
        except ImmichError as exc:
            # The gate is open and the ledger is correct; only the re-offer is missing, and
            # the next change to that asset supplies it. Never worth failing a finished job.
            logger.warning(
                "could not touch %s to have it re-sent to clients: %s. The checksum "
                "translation is armed but may not reach a device until that asset changes "
                "for another reason",
                new_asset_id,
                exc,
            )
            return
        await self._store.bump_counter(SHIM_TOUCHES)

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

    async def _duplicate_checksum(self, new_asset_id: str, encoded: str) -> str | None:
        """The checksum the server actually holds for the asset it answered ``duplicate`` with.

        Read back, not inferred. ``encoded`` is what *this* run produced, and the two are
        equal only if Immich's duplicate detection is purely checksum-based — plausible,
        unverified here, and not something this value may rest on. The shim's
        `upload_check` map restates a device's "do you already have this hash?" in terms of
        it, so a hash the server does not hold turns that answer into the very re-upload the
        translation exists to prevent. Storing nothing is strictly better than storing that.

        Best effort, for the same reason: a read that fails leaves the column NULL, which is
        what this path wrote before and costs only the `upload_check` half of a translation.
        The skip stands either way.
        """
        try:
            detail = await self._client.get_asset(new_asset_id)
        except ImmichError as exc:
            logger.warning(
                "could not read back the checksum of duplicate %s: %s — the shim gets no "
                "upload-check translation for this row",
                new_asset_id,
                exc,
            )
            return None
        if detail.checksum and detail.checksum != encoded:
            # The server matched this upload to an asset by something other than its bytes.
            # The stored value is still the right one — it is what the server has — but the
            # assumption the paragraph above declines to make just failed, visibly.
            logger.warning(
                "duplicate %s holds checksum %s, not the %s this run encoded — storing the server's value",
                new_asset_id,
                detail.checksum,
                encoded,
            )
        return detail.checksum

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
