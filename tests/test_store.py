"""Job store: idempotent enqueue, atomic claim, resumable states."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from immich_compressor.models import BackfillCandidate, JobState, SkipReason
from immich_compressor.store import JobStore

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
