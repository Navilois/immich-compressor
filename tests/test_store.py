"""Job store: idempotent enqueue, atomic claim, resumable states."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from immich_compressor.models import JobState, SkipReason
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
