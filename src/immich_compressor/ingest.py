"""Ingest policy: what happens to a webhook before it is allowed to become a job.

Two guards and the exception between them, all three reached from the webhook handler and
from nowhere else. They used to sit in :mod:`~immich_compressor.pipeline` on the reasoning
that every guard belongs in one file, which put the code that runs *before* a job exists in
the same module as the ten steps that run after one does — and made the pipeline the import
every request path had to reach through.

The freshness gate refuses a bulk metadata-extraction re-trigger. The surge breaker counts
what actually got queued and trips when far more arrives than anybody asked for. Neither
knows anything about jobs, encoding or the store: :func:`check_ingest_guards` is a pure
function of one asset and the behaviour settings, and :class:`SurgeDetector` is a rolling
count in memory. That is what makes them testable without a pipeline at all.

The other guard, :func:`~immich_compressor.pipeline.check_guards`, deliberately stays with
the pipeline: it runs once a job exists and raises
:class:`~immich_compressor.pipeline.SkipJob`, which is a job outcome. The placement of this
one is load-bearing for a different reason, spelled out in `check_ingest_guards` itself: a
rejection here must leave no row behind, or `backfill` could never reach the asset again.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import UTC, datetime, timedelta

from .config import BehaviorSettings
from .models import RejectReason, WebhookAsset

logger = logging.getLogger(__name__)


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
