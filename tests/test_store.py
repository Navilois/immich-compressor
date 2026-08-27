"""Job store: idempotent enqueue, atomic claim, resumable states."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from immich_compressor.models import BackfillCandidate, JobState, SkipReason
from immich_compressor.store import SHIM_COUNTERS, JobStore

PAYLOAD = {"type": "AssetV1", "trigger": "AssetMetadataExtraction", "data": {"asset": {"id": "a"}}}


async def test_enqueue_is_idempotent(tmp_path: Path) -> None:
    async with JobStore(tmp_path / "s.db") as store:
        assert await store.enqueue("asset-1", PAYLOAD, delay_seconds=0) is True
        # A webhook replay for the same asset must never start a second run.
        assert await store.enqueue("asset-1", PAYLOAD, delay_seconds=0) is False
        assert len(await store.list_jobs()) == 1


async def test_claim_respects_run_after(tmp_path: Path) -> None:
    async with JobStore(tmp_path / "s.db") as store:
        await store.enqueue("asset-1", PAYLOAD, delay_seconds=600)
        assert await store.claim_next() is None  # still inside the initial delay

        await store.update("asset-1", run_after=datetime.now(UTC) - timedelta(seconds=1))
        job = await store.claim_next()
        assert job is not None
        assert job.state is JobState.RUNNING
        assert job.attempts == 1


async def test_claim_is_exclusive(tmp_path: Path) -> None:
    async with JobStore(tmp_path / "s.db") as store:
        await store.enqueue("asset-1", PAYLOAD, delay_seconds=0)
        first = await store.claim_next()
        second = await store.claim_next()
        assert first is not None
        # `running` is resumable, so it can be re-claimed — but only after the first
        # claim has bumped the attempt counter, which is what bounds the retries.
        assert second is None or second.attempts == 2


async def test_terminal_states_are_not_reclaimed(tmp_path: Path) -> None:
    async with JobStore(tmp_path / "s.db") as store:
        await store.enqueue("asset-1", PAYLOAD, delay_seconds=0)
        await store.mark_skipped("asset-1", SkipReason.TOO_SMALL)
        assert await store.claim_next() is None

        await store.enqueue("asset-2", PAYLOAD, delay_seconds=0)
        await store.mark_failed("asset-2", "boom")
        claimed = await store.claim_next()
        assert claimed is None


async def test_uploaded_state_is_resumable(tmp_path: Path) -> None:
    """A crash after upload but before copy must be recoverable."""
    async with JobStore(tmp_path / "s.db") as store:
        await store.enqueue("asset-1", PAYLOAD, delay_seconds=0)
        await store.update("asset-1", state=JobState.UPLOADED, new_asset_id="new-1")
        job = await store.claim_next()
        assert job is not None
        assert job.new_asset_id == "new-1"


async def test_reset_requeues(tmp_path: Path) -> None:
    async with JobStore(tmp_path / "s.db") as store:
        await store.enqueue("asset-1", PAYLOAD, delay_seconds=0)
        await store.mark_failed("asset-1", "boom")
        assert await store.reset("asset-1") is True
        job = await store.get("asset-1")
        assert job is not None
        assert job.state is JobState.QUEUED
        assert job.attempts == 0
        assert await store.reset("nope") is False


async def test_requeue_skipped_only_touches_the_named_reason(tmp_path: Path) -> None:
    """After a gate changes, its old verdicts have to be re-run — but nothing else."""
    async with JobStore(tmp_path / "s.db") as store:
        for asset_id, reason in (
            ("no-gain-1", SkipReason.NO_GAIN),
            ("no-gain-2", SkipReason.NO_GAIN),
            ("too-small", SkipReason.TOO_SMALL),
        ):
            await store.enqueue(asset_id, PAYLOAD, delay_seconds=0)
            await store.mark_skipped(asset_id, reason)
        await store.enqueue("done", PAYLOAD, delay_seconds=0)
        await store.update("done", state=JobState.DONE)

        assert await store.skipped_asset_ids(SkipReason.NO_GAIN) == ["no-gain-1", "no-gain-2"]

        requeued = await store.requeue_skipped(SkipReason.NO_GAIN)
        assert sorted(requeued) == ["no-gain-1", "no-gain-2"]

        for asset_id in requeued:
            job = await store.get(asset_id)
            assert job is not None
            assert job.state is JobState.QUEUED
            assert job.skip_reason is None
            assert job.attempts == 0

        untouched = await store.get("too-small")
        assert untouched is not None
        assert untouched.state is JobState.SKIPPED
        assert untouched.skip_reason is SkipReason.TOO_SMALL
        assert (await store.get("done")).state is JobState.DONE  # type: ignore[union-attr]

        # Second run has nothing left to do.
        assert await store.requeue_skipped(SkipReason.NO_GAIN) == []


async def test_requeue_failed_only_touches_the_error_it_names(tmp_path: Path) -> None:
    """A gate fix has to reach the jobs that gate failed, and leave the rest where they are."""
    async with JobStore(tmp_path / "s.db") as store:
        for asset_id, error in (
            ("shutter-1", "EXIF:ShutterSpeedValue changed: '1/999963365' -> '1/999963296'"),
            ("shutter-2", "EXIF:ShutterSpeedValue changed: '1/999963365' -> '1/999963301'"),
            ("broken", "exiftool: Error reading OtherImageStart data in IFD0"),
        ):
            await store.enqueue(asset_id, PAYLOAD, delay_seconds=0)
            await store.update(asset_id, attempts=3)
            await store.mark_failed(asset_id, error)
        await store.enqueue("skipped", PAYLOAD, delay_seconds=0)
        await store.mark_skipped("skipped", SkipReason.NO_GAIN)

        assert await store.failed_asset_ids(error_contains="ShutterSpeedValue") == [
            "shutter-1",
            "shutter-2",
        ]
        # A substring, not a pattern: the wildcards of LIKE are ordinary characters here.
        assert await store.failed_asset_ids(error_contains="%ShutterSpeedValue%") == []

        requeued = await store.requeue_failed(error_contains="ShutterSpeedValue")
        assert sorted(requeued) == ["shutter-1", "shutter-2"]

        for asset_id in requeued:
            job = await store.get(asset_id)
            assert job is not None
            assert job.state is JobState.QUEUED
            assert job.last_error is None
            # The attempts are what the worker's backoff counts, so a re-run needs them
            # cleared or the job is claimed once and abandoned again.
            assert job.attempts == 0

        untouched = await store.get("broken")
        assert untouched is not None
        assert untouched.state is JobState.FAILED
        assert (await store.get("skipped")).state is JobState.SKIPPED  # type: ignore[union-attr]

        # Second run has nothing left to do, and without a filter the rest comes back.
        assert await store.requeue_failed(error_contains="ShutterSpeedValue") == []
        assert await store.requeue_failed() == ["broken"]


async def test_due_deletions(tmp_path: Path) -> None:
    async with JobStore(tmp_path / "s.db") as store:
        await store.enqueue("late", PAYLOAD, delay_seconds=0)
        await store.update(
            "late",
            state=JobState.PENDING_DELETE,
            delete_after=datetime.now(UTC) + timedelta(days=7),
        )
        await store.enqueue("due", PAYLOAD, delay_seconds=0)
        await store.update(
            "due",
            state=JobState.PENDING_DELETE,
            delete_after=datetime.now(UTC) - timedelta(seconds=1),
        )
        due = await store.due_deletions()
        assert [job.source_asset_id for job in due] == ["due"]


async def test_stats_aggregate(tmp_path: Path) -> None:
    async with JobStore(tmp_path / "s.db") as store:
        await store.enqueue("a", PAYLOAD, delay_seconds=0)
        await store.update("a", state=JobState.DONE, orig_bytes=1000, new_bytes=250, ratio=0.25)
        await store.enqueue("b", PAYLOAD, delay_seconds=0)
        await store.mark_skipped("b", SkipReason.TOO_SMALL)

        stats = await store.stats()
        assert stats["total"] == 2
        assert stats["by_state"]["done"] == 1
        assert stats["by_skip_reason"]["too_small"] == 1
        assert stats["saved_bytes"] == 750
        assert stats["average_ratio"] == 0.25


async def test_unknown_column_is_rejected(tmp_path: Path) -> None:
    async with JobStore(tmp_path / "s.db") as store:
        await store.enqueue("a", PAYLOAD, delay_seconds=0)
        try:
            await store.update("a", nonsense="x")
        except ValueError as exc:
            assert "nonsense" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected ValueError")


# ------------------------------------------------------------------- worker lanes

VIDEO_PAYLOAD = {
    "type": "AssetV1",
    "trigger": "AssetMetadataExtraction",
    "data": {"asset": {"id": "v", "type": "VIDEO"}},
}
IMAGE_PAYLOAD = {
    "type": "AssetV1",
    "trigger": "AssetMetadataExtraction",
    "data": {"asset": {"id": "i", "type": "IMAGE"}},
}


async def test_asset_type_is_derived_from_the_payload(tmp_path: Path) -> None:
    async with JobStore(tmp_path / "s.db") as store:
        await store.enqueue("video-1", VIDEO_PAYLOAD, delay_seconds=0)
        job = await store.get("video-1")
        assert job is not None and job.asset_type == "VIDEO"


async def test_a_lane_only_claims_its_own_type(tmp_path: Path) -> None:
    """Without this a single two-hour clip holds the queue and every image waits behind it."""
    async with JobStore(tmp_path / "s.db") as store:
        await store.enqueue("video-1", VIDEO_PAYLOAD, delay_seconds=0)
        await store.enqueue("image-1", IMAGE_PAYLOAD, delay_seconds=0)

        image_job = await store.claim_next(types=("IMAGE",))
        assert image_job is not None and image_job.source_asset_id == "image-1"

        # `running` stays resumable so a crash mid-job can be replayed, so finish it before
        # asking again. The image lane is then empty even though a video job is still due.
        await store.update("image-1", state=JobState.DONE)
        assert await store.claim_next(types=("IMAGE",)) is None

        video_job = await store.claim_next(types=("VIDEO",))
        assert video_job is not None and video_job.source_asset_id == "video-1"


async def test_rows_without_a_type_stay_claimable(tmp_path: Path) -> None:
    """Jobs queued by the previous version carry NULL and must not be stranded."""
    async with JobStore(tmp_path / "s.db") as store:
        await store.enqueue("legacy", {"type": "AssetV1", "data": {}}, delay_seconds=0)
        assert (await store.get("legacy")).asset_type is None  # type: ignore[union-attr]
        claimed = await store.claim_next(types=("IMAGE",))
        assert claimed is not None and claimed.source_asset_id == "legacy"


# ------------------------------------------------------------------- the pause latch


async def test_the_pause_latch_survives_a_restart(tmp_path: Path) -> None:
    """A restart is the first thing an operator reaches for; it must not clear a pause."""
    db = tmp_path / "state.db"
    async with JobStore(db) as store:
        assert await store.pause_state() is None
        assert await store.pause("300 assets in 600s") is True

    async with JobStore(db) as store:
        latched = await store.pause_state()
        assert latched is not None
        assert latched.reason == "300 assets in 600s"


async def test_a_second_surge_does_not_overwrite_the_first_reason(tmp_path: Path) -> None:
    """The first trip is the one that explains the pause. Later ones are noise."""
    async with JobStore(tmp_path / "state.db") as store:
        assert await store.pause("the original reason") is True
        assert await store.pause("a later surge") is False
        latched = await store.pause_state()
        assert latched is not None
        assert latched.reason == "the original reason"


async def test_resume_clears_the_latch_and_reports_whether_it_had_to(tmp_path: Path) -> None:
    async with JobStore(tmp_path / "state.db") as store:
        assert await store.resume() is False  # was never paused
        await store.pause("surge")
        assert await store.resume() is True
        assert await store.pause_state() is None


# ------------------------------------------------------------- the backfill inventory


def _candidate(asset_id: str, *, size: int = 1000, verdict: str | None = None) -> BackfillCandidate:
    return BackfillCandidate(
        asset_id=asset_id,
        asset_type="VIDEO",
        size_bytes=size,
        filename=f"{asset_id}.mp4",
        verdict=verdict,
        payload={"data": {"asset": {"id": asset_id}}} if verdict is None else {},
        scanned_at=datetime.now(UTC),
    )


async def test_a_rescan_does_not_forget_what_was_already_queued(tmp_path: Path) -> None:
    """The inventory is refreshed by every scan; `queued_at` is the one column it may not
    overwrite, or a re-scan would offer the same assets again on every run."""
    async with JobStore(tmp_path / "state.db") as store:
        await store.record_candidates([_candidate("v1")])
        await store.mark_candidate_queued("v1")

        await store.record_candidates([_candidate("v1", size=2000)])

        assert await store.pick_candidates(limit=10) == []
        stats = await store.inventory_stats()
        assert stats["types"]["VIDEO"]["queued"] == 1
        assert stats["types"]["VIDEO"]["scanned_bytes"] == 2000


async def test_candidates_come_back_biggest_first(tmp_path: Path) -> None:
    async with JobStore(tmp_path / "state.db") as store:
        await store.record_candidates(
            [_candidate("small", size=10), _candidate("big", size=9000), _candidate("mid", size=500)]
        )
        by_size = await store.pick_candidates(order="size", limit=10)
        assert [candidate.asset_id for candidate in by_size] == ["big", "mid", "small"]


async def test_a_verdict_takes_a_row_out_of_the_candidate_set_and_frees_its_payload(
    tmp_path: Path,
) -> None:
    async with JobStore(tmp_path / "state.db") as store:
        await store.record_candidates([_candidate("v1"), _candidate("v2", verdict="too_small")])
        await store.set_candidate_verdict("v1", "missing")

        assert await store.pick_candidates(limit=10) == []
        stats = await store.inventory_stats()
        assert stats["types"]["VIDEO"]["by_verdict"] == {"missing": 1, "too_small": 1}
        assert stats["candidates"] == 0


async def test_clear_inventory_is_per_type(tmp_path: Path) -> None:
    async with JobStore(tmp_path / "state.db") as store:
        image = _candidate("i1").model_copy(update={"asset_type": "IMAGE"})
        await store.record_candidates([_candidate("v1"), image])

        assert await store.clear_inventory(["VIDEO"]) == 1

        assert set((await store.inventory_stats())["types"]) == {"IMAGE"}


async def test_service_state_round_trips_json(tmp_path: Path) -> None:
    """The scan cursor lives here, so an interrupted walk survives the process."""
    async with JobStore(tmp_path / "state.db") as store:
        assert await store.get_state("backfill_scan:VIDEO") is None
        await store.set_state("backfill_scan:VIDEO", {"next_page": 7})
        assert await store.get_state("backfill_scan:VIDEO") == {"next_page": 7}
        await store.set_state("backfill_scan:VIDEO", {"next_page": 8})
        assert await store.get_state("backfill_scan:VIDEO") == {"next_page": 8}
        await store.clear_state("backfill_scan:VIDEO")
        assert await store.get_state("backfill_scan:VIDEO") is None


async def test_replaced_source_asset_ids_lists_only_completed_replacements(tmp_path: Path) -> None:
    """The selection behind `restore --all-pending`.

    Only originals this service actually replaced and removed: a job still waiting on its
    retention window has not had its original taken away, and restoring it would touch an
    asset nobody trashed.
    """
    async with JobStore(tmp_path / "s.db") as store:
        for asset_id in ("done-1", "done-2", "still-pending", "failed", "done-without-copy"):
            await store.enqueue(asset_id, PAYLOAD, delay_seconds=0)
        await store.update("done-1", state=JobState.DONE, new_asset_id="copy-1")
        await store.update("done-2", state=JobState.DONE, new_asset_id="copy-2")
        await store.update("still-pending", state=JobState.PENDING_DELETE, new_asset_id="copy-3")
        await store.update("failed", state=JobState.FAILED)
        await store.update("done-without-copy", state=JobState.DONE)

        assert await store.replaced_source_asset_ids() == ["done-1", "done-2"]


async def test_replaced_source_asset_ids_is_not_capped(tmp_path: Path) -> None:
    """It used to run through `list_jobs(limit=10_000)`. A rollback that silently leaves
    the oldest originals in the trash is the failure nobody would notice."""
    async with JobStore(tmp_path / "s.db") as store:
        for index in range(250):
            await store.enqueue(f"a{index:03d}", PAYLOAD, delay_seconds=0)
            await store.update(f"a{index:03d}", state=JobState.DONE, new_asset_id=f"c{index:03d}")

        assert len(await store.replaced_source_asset_ids()) == 250


# --------------------------------------------------------------------- the ledger


async def test_find_replaced_original_recognises_the_same_bytes(tmp_path: Path) -> None:
    """The re-upload case: same checksum, same owner, a brand-new asset id."""
    async with JobStore(tmp_path / "s.db") as store:
        await store.enqueue("original", PAYLOAD, delay_seconds=0)
        await store.update(
            "original",
            state=JobState.DONE,
            new_asset_id="replacement",
            source_checksum="c3Vt",
            owner_id="user-1",
        )
        await store.enqueue("re-uploaded", PAYLOAD, delay_seconds=0)

        found = await store.find_replaced_original(
            checksum="c3Vt", owner_id="user-1", exclude_asset_id="re-uploaded"
        )
        assert found is not None
        assert found.source_asset_id == "original"
        assert found.new_asset_id == "replacement"


async def test_find_replaced_original_is_scoped_to_the_owner(tmp_path: Path) -> None:
    """Immich's uniqueness constraint is (ownerId, checksum). Two users owning the same
    photo are two assets, and neither is evidence about the other."""
    async with JobStore(tmp_path / "s.db") as store:
        await store.enqueue("hers", PAYLOAD, delay_seconds=0)
        await store.update(
            "hers", state=JobState.DONE, new_asset_id="copy", source_checksum="c3Vt", owner_id="user-1"
        )

        assert (
            await store.find_replaced_original(checksum="c3Vt", owner_id="user-2", exclude_asset_id="theirs")
            is None
        )


async def test_find_replaced_original_ignores_jobs_that_replaced_nothing(tmp_path: Path) -> None:
    """A skipped or failed job left its original in place, so its checksum is still the
    server's. Matching on it would skip an asset that was never compressed."""
    async with JobStore(tmp_path / "s.db") as store:
        await store.enqueue("skipped", PAYLOAD, delay_seconds=0)
        await store.update("skipped", source_checksum="c3Vt", owner_id="user-1")
        await store.mark_skipped("skipped", SkipReason.NO_GAIN)

        assert (
            await store.find_replaced_original(checksum="c3Vt", owner_id="user-1", exclude_asset_id="other")
            is None
        )


async def test_find_replaced_original_never_matches_the_job_asking(tmp_path: Path) -> None:
    """A retry re-reads the same asset and must not recognise itself as its own re-upload."""
    async with JobStore(tmp_path / "s.db") as store:
        await store.enqueue("asset-1", PAYLOAD, delay_seconds=0)
        await store.update(
            "asset-1", state=JobState.DONE, new_asset_id="copy", source_checksum="c3Vt", owner_id="user-1"
        )

        assert (
            await store.find_replaced_original(checksum="c3Vt", owner_id="user-1", exclude_asset_id="asset-1")
            is None
        )


async def test_find_replaced_original_needs_both_halves(tmp_path: Path) -> None:
    """Rows from before the ledger existed carry neither, and a partial match is a guess."""
    async with JobStore(tmp_path / "s.db") as store:
        await store.enqueue("legacy", PAYLOAD, delay_seconds=0)
        await store.update("legacy", state=JobState.DONE, new_asset_id="copy")
        job = await store.get("legacy")
        assert job is not None and job.source_checksum is None and job.owner_id is None

        for checksum, owner in (("c3Vt", None), (None, "user-1"), (None, None)):
            assert (
                await store.find_replaced_original(
                    checksum=checksum, owner_id=owner, exclude_asset_id="other"
                )
                is None
            )


async def test_ledger_columns_are_added_to_an_existing_database(tmp_path: Path) -> None:
    """A store created before the ledger existed opens, migrates and keeps its rows."""
    import aiosqlite

    path = tmp_path / "old.db"
    async with JobStore(path) as store:
        await store.enqueue("asset-1", PAYLOAD, delay_seconds=0)

    # Rebuild `jobs` without the two ledger columns, exactly as 1.3.1 wrote it.
    async with aiosqlite.connect(path) as db:
        await db.execute("DROP INDEX IF EXISTS idx_jobs_ledger")
        await db.execute("ALTER TABLE jobs DROP COLUMN source_checksum")
        await db.execute("ALTER TABLE jobs DROP COLUMN owner_id")
        await db.commit()

    async with JobStore(path) as store:
        job = await store.get("asset-1")
        assert job is not None
        assert job.source_checksum is None and job.owner_id is None
        await store.update("asset-1", source_checksum="c3Vt", owner_id="user-1")
        assert (await store.get("asset-1")).source_checksum == "c3Vt"  # type: ignore[union-attr]


# ------------------------------------------------------------------------- the ledger


LEDGER_HASH = "02MpaJkpzGHNbGwxWtencVNK7uY="
LEDGER_OWNER = "11111111-1111-4111-8111-111111111111"


async def _replaced(store: JobStore, asset_id: str, *, checksum: str | None, owner: str | None) -> None:
    await store.enqueue(asset_id, PAYLOAD, delay_seconds=0)
    await store.update(
        asset_id,
        new_asset_id=f"{asset_id}-new",
        new_checksum="replacement-hash=",
        source_checksum=checksum,
        owner_id=owner,
    )


async def test_ledger_entries_matches_find_replaced_original(tmp_path: Path) -> None:
    """The two ledger reads must agree on what "this service replaced that" means.

    They answer different questions — one row for the guard, all rows for the shim — and
    if they ever disagreed the shim would translate a checksum the guard would not
    recognise coming back, which is the one combination that produces a silent loop.
    """
    async with JobStore(tmp_path / "s.db") as store:
        await _replaced(store, "eligible", checksum=LEDGER_HASH, owner=LEDGER_OWNER)
        # Ineligible three different ways, one of which is a pre-ledger row.
        await _replaced(store, "no-owner", checksum="x=", owner=None)
        await _replaced(store, "no-checksum", checksum=None, owner=LEDGER_OWNER)
        await _replaced(store, "pre-ledger", checksum=None, owner=None)
        await store.enqueue("never-replaced", PAYLOAD, delay_seconds=0)
        await store.update("never-replaced", source_checksum="y=", owner_id=LEDGER_OWNER)

        entries = await store.ledger_entries()
        assert {entry.source_asset_id for entry in entries} == {"eligible"}

        for entry in entries:
            match = await store.find_replaced_original(
                checksum=entry.source_checksum,
                owner_id=entry.owner_id,
                exclude_asset_id="something-else",
            )
            assert match is not None and match.source_asset_id == entry.source_asset_id


async def test_ledger_entry_starts_with_its_gate_closed(tmp_path: Path) -> None:
    async with JobStore(tmp_path / "s.db") as store:
        await _replaced(store, "asset-1", checksum=LEDGER_HASH, owner=LEDGER_OWNER)
        (entry,) = await store.ledger_entries()
        assert entry.gate_is_open is False


async def test_mark_original_freed_is_first_write_wins(tmp_path: Path) -> None:
    """Both callers can fire for the same row on a deployment that switched delete_mode."""
    async with JobStore(tmp_path / "s.db") as store:
        await _replaced(store, "asset-1", checksum=LEDGER_HASH, owner=LEDGER_OWNER)

        assert await store.mark_original_freed("asset-1") is True
        first = await store.get("asset-1")
        assert first is not None and first.original_freed_at is not None

        assert await store.mark_original_freed("asset-1") is False
        again = await store.get("asset-1")
        assert again is not None and again.original_freed_at == first.original_freed_at


async def test_mark_original_freed_ignores_an_unknown_asset(tmp_path: Path) -> None:
    async with JobStore(tmp_path / "s.db") as store:
        assert await store.mark_original_freed("never-heard-of-it") is False


async def test_shim_counters_are_zero_filled(tmp_path: Path) -> None:
    """ "0 requests" is a diagnosis — it means the reverse proxy is not routing to us."""
    async with JobStore(tmp_path / "s.db") as store:
        counters = await store.counters()
        for name in SHIM_COUNTERS:
            assert counters[name] == 0
