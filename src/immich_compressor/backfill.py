"""The library backfill: an inventory of what could be compressed, and a run over it.

The webhook only fires for assets moving through Immich's own pipeline, so everything
already in the library is invisible to it — and the one trigger that would reach the whole
library in bulk is refused on arrival by `check_ingest_guards`. This module is the
intentional way in.

Two phases, deliberately separate:

``scan``
    walks the library once per asset type, runs the *same* guards the worker runs, and
    writes one row per asset into ``backfill_candidates``. Resumable: the cursor lives in
    the job store, so an interrupted walk continues where it stopped.
``run``
    picks candidates out of that inventory — biggest first by default — re-checks each one
    against the live server, and enqueues it as if a webhook had arrived for it.

The split is what makes ``--limit`` mean "queue this many jobs" instead of "look at this
many search results", what lets a second run make progress instead of re-reading the same
answer, and what lets ``status`` say how much of the library is left.

Nothing here touches the pipeline. A queued job is an ordinary job: same guards, same
sanity gate, same verification chain before anything is deleted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from .api import ImmichClient, ImmichError
from .config import Settings
from .models import BackfillCandidate, BackfillVerdict, SkipReason, WebhookAsset
from .pipeline import preflight
from .store import JobStore

logger = logging.getLogger(__name__)

# What the synthetic payload records as its trigger. It reaches the log line of every job
# this queued, which is the only place anyone can tell a backfilled asset from one a
# webhook delivered.
TRIGGER = "Backfill"

# `service_state` key per asset type, holding `{next_page, last_first_id, completed_at}`.
SCAN_STATE_PREFIX = "backfill_scan:"

# How many assets to ask for per request. Immich may or may not honour it (see
# `ImmichClient.search_assets`); a server that ignores it simply answers whatever it wants.
DEFAULT_PAGE_SIZE = 1000

# A hard stop for the walk. At the default page size this is more assets than any home
# library, and it is what keeps a server that ignores `page` from being asked forever.
MAX_PAGES = 1000

# How many candidates a queue run pulls out of the inventory at a time. Small enough that
# a run stopped halfway has not read the whole table, large enough that reaching a limit of
# 50 is one query and not fifty.
_PICK_BATCH = 100


@dataclass(slots=True)
class ScanSummary:
    """What one ``scan`` of one asset type did."""

    asset_type: str
    pages: int = 0
    # Items the server returned, before the type filter.
    seen: int = 0
    # Items of another type it returned anyway.
    foreign: int = 0
    recorded: int = 0
    candidates: int = 0
    candidate_bytes: int = 0
    by_verdict: dict[str, int] = field(default_factory=dict)
    resumed_from: int = 1
    completed: bool = False
    stopped_because: str | None = None


@dataclass(slots=True)
class QueuedAsset:
    """One asset a queue run put into the job store, for the report at the end."""

    asset_id: str
    asset_type: str
    filename: str
    size_bytes: int


@dataclass(slots=True)
class QueueSummary:
    """What one ``run`` did — or, in a dry run, what it would have done."""

    dry_run: bool = True
    considered: int = 0
    queued: list[QueuedAsset] = field(default_factory=list)
    # Verdict -> count for candidates the live re-check refused. Empty after a dry run,
    # which asks the server nothing.
    downgraded: dict[str, int] = field(default_factory=dict)
    # True when the inventory ran out before the limit was reached.
    exhausted: bool = False

    @property
    def queued_bytes(self) -> int:
        return sum(asset.size_bytes for asset in self.queued)


def scan_state_key(asset_type: str) -> str:
    return f"{SCAN_STATE_PREFIX}{asset_type}"


def resolve_types(settings: Settings, asset_type: str | None) -> list[str]:
    """Which lanes a command works on: the one that was asked for, or every enabled one.

    Defaulting to ``enabled_types`` rather than to ``VIDEO`` is what makes stills a first
    class citizen of the backfill. It cannot widen anything by accident: a type that is not
    enabled is refused by the guards anyway, and the shipped default is ``[VIDEO]``.
    """
    if asset_type is not None:
        return [asset_type]
    return list(settings.behavior.enabled_types)


def evaluate(item: dict[str, Any], settings: Settings) -> BackfillCandidate | None:
    """Turn one search result into an inventory row, guard verdict and all.

    ``None`` means the item was not a readable asset — a shape this client does not know
    how to talk about, which is worth a log line and nothing else.
    """
    try:
        asset = WebhookAsset.model_validate(item)
    except ValidationError as exc:
        logger.warning("skipping an unreadable search result: %s", exc)
        return None
    verdict = preflight(asset, settings)
    return BackfillCandidate(
        asset_id=asset.id,
        asset_type=asset.type,
        size_bytes=asset.exif_info.file_size_in_byte or 0,
        filename=asset.original_file_name,
        verdict=verdict.value if verdict is not None else None,
        # Only a candidate carries its payload: it is the one expensive column, and an
        # asset the guards already refused is never enqueued from here.
        payload=_payload_for(item) if verdict is None else {},
        scanned_at=datetime.now(UTC),
    )


def _payload_for(item: dict[str, Any]) -> dict[str, Any]:
    """The synthetic webhook body a job is driven from."""
    return {"type": "AssetV1", "trigger": TRIGGER, "data": {"asset": item}}


async def scan_type(
    client: ImmichClient,
    store: JobStore,
    settings: Settings,
    asset_type: str,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    rescan: bool = False,
    max_pages: int = MAX_PAGES,
) -> ScanSummary:
    """Walk one asset type and write what it finds into the inventory.

    Every page is committed before the cursor moves, so an interrupted scan loses at most
    the page it was on. A re-scan updates rows in place and never forgets that a queue run
    already reached an asset.
    """
    key = scan_state_key(asset_type)
    summary = ScanSummary(asset_type=asset_type)
    if rescan:
        removed = await store.clear_inventory([asset_type])
        await store.clear_state(key)
        logger.info("rescan: dropped %d inventory row(s) for %s", removed, asset_type)

    cursor = await store.get_state(key) or {}
    page = _as_positive_int(cursor.get("next_page"), default=1)
    previous_first_id = cursor.get("last_first_id")
    summary.resumed_from = page

    while summary.pages < max_pages:
        result = await client.search_assets(asset_type=asset_type, page=page, size=page_size)
        if not result.items:
            summary.completed = True
            break
        first_id = str(result.items[0].get("id") or "")
        if first_id and first_id == previous_first_id:
            # The server is answering every page with the same assets, which is what an
            # ignored `page` looks like from here. Stopping is the only honest response:
            # walking on would rewrite the same rows until `max_pages` ran out.
            summary.stopped_because = (
                f"page {page} came back with the same assets as the page before it — "
                "this Immich does not apply `page` to /search/metadata"
            )
            break
        previous_first_id = first_id

        rows: list[BackfillCandidate] = []
        for item in result.items:
            if item.get("type") != asset_type:
                # Same lesson as `/search/large-assets`: the type filter is a request, and
                # a scan that trusted it would file videos under stills.
                summary.foreign += 1
                continue
            candidate = evaluate(item, settings)
            if candidate is not None:
                rows.append(candidate)
        await store.record_candidates(rows)

        summary.pages += 1
        summary.seen += len(result.items)
        summary.recorded += len(rows)
        for candidate in rows:
            if candidate.verdict is None:
                summary.candidates += 1
                summary.candidate_bytes += candidate.size_bytes
            else:
                summary.by_verdict[candidate.verdict] = summary.by_verdict.get(candidate.verdict, 0) + 1

        if result.paged and result.next_page is None:
            summary.completed = True
            break
        page = result.next_page if result.next_page is not None else page + 1
        await store.set_state(key, {"next_page": page, "last_first_id": previous_first_id})
    else:
        summary.stopped_because = f"stopped after {max_pages} pages"

    if summary.completed:
        # A finished walk starts over next time: the library moves on, and re-scanning is
        # an upsert that keeps every `queued_at` it finds.
        await store.set_state(
            key, {"next_page": 1, "completed_at": datetime.now(UTC).isoformat(), "last_first_id": None}
        )
    return summary


async def scan(
    client: ImmichClient,
    store: JobStore,
    settings: Settings,
    *,
    asset_types: list[str],
    page_size: int = DEFAULT_PAGE_SIZE,
    rescan: bool = False,
    max_pages: int = MAX_PAGES,
) -> list[ScanSummary]:
    return [
        await scan_type(
            client,
            store,
            settings,
            asset_type,
            page_size=page_size,
            rescan=rescan,
            max_pages=max_pages,
        )
        for asset_type in asset_types
    ]


async def queue_candidates(
    client: ImmichClient,
    store: JobStore,
    settings: Settings,
    *,
    asset_types: list[str],
    limit: int,
    order: str = "size",
    apply: bool = False,
    verify: bool = True,
) -> QueueSummary:
    """Enqueue up to ``limit`` candidates from the inventory.

    ``limit`` counts jobs written, not rows looked at: an asset the live re-check refuses
    is recorded as such and the run moves on to the next candidate. That is the whole
    reason the inventory exists, and the reason a run can be repeated until `status` says
    there is nothing left.
    """
    summary = QueueSummary(dry_run=not apply)
    if limit <= 0:
        return summary

    if not apply:
        # A dry run asks the server nothing and writes nothing. It reports the plan: the
        # rows a real run would start with, in the order it would take them.
        picked = await store.pick_candidates(asset_types=asset_types, order=order, limit=limit)
        summary.considered = len(picked)
        summary.queued = [
            QueuedAsset(
                asset_id=candidate.asset_id,
                asset_type=candidate.asset_type,
                filename=candidate.filename,
                size_bytes=candidate.size_bytes,
            )
            for candidate in picked
        ]
        summary.exhausted = len(picked) < limit
        return summary

    while len(summary.queued) < limit:
        batch = await store.pick_candidates(
            asset_types=asset_types,
            order=order,
            limit=min(_PICK_BATCH, limit - len(summary.queued)),
        )
        if not batch:
            summary.exhausted = True
            break
        for candidate in batch:
            summary.considered += 1
            verdict = await _live_verdict(client, settings, candidate) if verify else None
            if verdict is None and not await store.enqueue(
                candidate.asset_id,
                candidate.payload,
                delay_seconds=0,
                asset_type=candidate.asset_type,
            ):
                # `ON CONFLICT DO NOTHING` said no: a job row already exists, from an
                # earlier run or from a webhook. Not an error, but not ours to queue.
                verdict = BackfillVerdict.ALREADY_KNOWN.value
            if verdict is not None:
                await store.set_candidate_verdict(candidate.asset_id, verdict)
                summary.downgraded[verdict] = summary.downgraded.get(verdict, 0) + 1
                continue
            await store.mark_candidate_queued(candidate.asset_id)
            summary.queued.append(
                QueuedAsset(
                    asset_id=candidate.asset_id,
                    asset_type=candidate.asset_type,
                    filename=candidate.filename,
                    size_bytes=candidate.size_bytes,
                )
            )
            logger.info(
                "backfill queued %s (%s, %d bytes)",
                candidate.asset_id,
                candidate.filename,
                candidate.size_bytes,
            )
            if len(summary.queued) >= limit:
                break
    return summary


async def _live_verdict(client: ImmichClient, settings: Settings, candidate: BackfillCandidate) -> str | None:
    """Ask the server about one candidate before queueing it. ``None`` means "go ahead".

    An inventory is a snapshot, and between the scan and the run an asset can be deleted,
    trashed, or given a name in a face. One request per asset that is actually about to be
    queued — bounded by ``--limit``, not by the size of the library.
    """
    try:
        detail = await client.get_asset(candidate.asset_id)
    except ImmichError as exc:
        # 404 is a deleted asset. 400 is what a v3 server answers for an id it will not
        # even parse, which from here is the same thing: there is nothing to compress.
        if exc.status_code in (400, 404):
            return BackfillVerdict.MISSING.value
        raise
    if detail.is_trashed:
        return SkipReason.TRASHED.value
    if settings.behavior.skip_if_named_people and detail.named_people():
        return SkipReason.NAMED_PEOPLE.value
    return None


def _as_positive_int(value: object, *, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value if value > 0 else default
