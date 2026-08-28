"""SQLite-backed job store (WAL mode).

The store is the single source of truth for idempotency: enqueueing the same asset twice
is a no-op, and every pipeline state transition is persisted so a restart can resume
mid-job instead of redoing work that already hit the server.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import aiosqlite

from .models import (
    TERMINAL_STATES,
    BackfillCandidate,
    Job,
    JobState,
    LedgerEntry,
    PauseState,
    ReturnedOriginal,
    SkipReason,
)

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    source_asset_id TEXT PRIMARY KEY,
    state           TEXT NOT NULL,
    skip_reason     TEXT,
    new_asset_id    TEXT,
    new_checksum    TEXT,
    orig_bytes      INTEGER,
    new_bytes       INTEGER,
    ratio           REAL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    payload         TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    run_after       TEXT NOT NULL,
    delete_after    TEXT,
    asset_type      TEXT,
    source_checksum TEXT,
    owner_id        TEXT,
    original_freed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_state_runafter ON jobs (state, run_after);
CREATE INDEX IF NOT EXISTS idx_jobs_delete_after   ON jobs (delete_after);

-- Service-wide state that has to outlive the process. One row today, the surge breaker's
-- latch; a table rather than a column so the next such flag does not need a migration.
CREATE TABLE IF NOT EXISTS service_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Monotonic counters for things that happen without leaving a job row. Persisted rather
-- than held in memory because the commands that report them — `report`, `check` — run in
-- a different process from `serve`: an in-memory counter would read zero for both of them
-- no matter what the server saw.
CREATE TABLE IF NOT EXISTS counters (
    name       TEXT PRIMARY KEY,
    value      INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

-- What `backfill scan` found, so `backfill run` can queue from a plan instead of from
-- whatever one search request happened to answer. Rows the guards already rejected are
-- kept — they are what `backfill status` counts — but only candidates carry a payload.
-- Not part of `jobs`: a job row is a decision this service has taken and is immune to
-- replay forever, and an inventory entry must stay re-scannable and re-orderable.
CREATE TABLE IF NOT EXISTS backfill_candidates (
    asset_id   TEXT PRIMARY KEY,
    asset_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    filename   TEXT NOT NULL DEFAULT '',
    verdict    TEXT,
    payload    TEXT NOT NULL DEFAULT '{}',
    scanned_at TEXT NOT NULL,
    queued_at  TEXT
);
-- Covers the one query that runs per queue run: candidates of a type, biggest first.
CREATE INDEX IF NOT EXISTS idx_backfill_pick
    ON backfill_candidates (asset_type, verdict, queued_at, size_bytes DESC);
"""

# The single `service_state` key the surge breaker owns.
_PAUSE_KEY = "paused"

# Counter names. A webhook that fails the token check writes nothing else anywhere, which
# is what made a mismatched shared secret look exactly like an idle installation.
WEBHOOKS_RECEIVED = "webhooks_received"
WEBHOOKS_REJECTED = "webhooks_rejected"

# Shim counters. Same reasoning as the webhook pair: the shim runs inside `serve`, and
# `report` and `check` read it from a different process, so an in-memory number would be
# invisible exactly where somebody looks for it.
SHIM_REQUESTS = "shim_requests"
SHIM_LINES_REWRITTEN = "shim_lines_rewritten"
SHIM_HASHES_TRANSLATED = "shim_hashes_translated"
SHIM_GATES_OPENED = "shim_gates_opened"
SHIM_TOUCHES = "shim_touches"
SHIM_PASSTHROUGH_ERRORS = "shim_passthrough_errors"

SHIM_COUNTERS: tuple[str, ...] = (
    SHIM_REQUESTS,
    SHIM_LINES_REWRITTEN,
    SHIM_HASHES_TRANSLATED,
    SHIM_GATES_OPENED,
    SHIM_TOUCHES,
    SHIM_PASSTHROUGH_ERRORS,
)

# Indexes over columns from `_ADDED_COLUMNS`. They have to run *after* the migration:
# on a database created before that column existed, `CREATE INDEX` would fail here.
_SCHEMA_POST_MIGRATION = """
CREATE INDEX IF NOT EXISTS idx_jobs_lane ON jobs (state, asset_type, run_after);
-- The ledger lookup that runs once per job, before the download. Partial: only rows that
-- actually replaced something can answer it, which on a real store is a small minority.
CREATE INDEX IF NOT EXISTS idx_jobs_ledger ON jobs (source_checksum, owner_id)
    WHERE source_checksum IS NOT NULL AND new_asset_id IS NOT NULL;
"""

_CANDIDATE_COLUMNS = "asset_id, asset_type, size_bytes, filename, verdict, payload, scanned_at, queued_at"

_COLUMNS = (
    "source_asset_id, state, skip_reason, new_asset_id, new_checksum, orig_bytes, new_bytes, "
    "ratio, attempts, last_error, payload, created_at, updated_at, run_after, delete_after, "
    "asset_type, source_checksum, owner_id, original_freed_at"
)

# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` cannot add them to a
# database that already exists, so they are applied by hand on open.
_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("new_checksum", "TEXT"),
    # Lets a worker claim only its own kind of job. Without it a single two-hour video
    # holds the queue and every one-second image job waits behind it.
    ("asset_type", "TEXT"),
    # The ledger. What the *original* hashed to and who owned it, so the same bytes coming
    # back as a new asset can be recognised instead of compressed a second time.
    ("source_checksum", "TEXT"),
    ("owner_id", "TEXT"),
    # Set once, when the asset this row is about stops existing on the server. On a
    # replacement's row that is the shim's gate opening; on a `re_uploaded` row it is the
    # returned copy going, which frees the checksum it was holding. Both readings are the
    # same fact. See `LedgerEntry` and `ReturnedOriginal`.
    ("original_freed_at", "TEXT"),
)

# States a worker is allowed to pick up and (re)drive.
_RESUMABLE: tuple[str, ...] = (
    JobState.QUEUED,
    JobState.RUNNING,
    JobState.UPLOADED,
    JobState.LINKED,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _failed_predicate(error_contains: str | None) -> tuple[str, tuple[Any, ...]]:
    """The ``WHERE`` clause selecting failed jobs, and its parameters.

    Shared so that the listing and the update behind a requeue can never drift apart and
    re-queue a different set than the one that was shown.

    ``instr`` rather than ``LIKE``: what an operator types is a substring of an error
    message, not a pattern, and ffmpeg and exiftool both put ``%`` and ``_`` in theirs.
    """
    if error_contains is None:
        return "state = ?", (JobState.FAILED.value,)
    return (
        "state = ? AND last_error IS NOT NULL AND instr(last_error, ?) > 0",
        (JobState.FAILED.value, error_contains),
    )


class JobStore:
    """Async SQLite job store. One instance per process."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None

    async def __aenter__(self) -> Self:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.executescript(_SCHEMA)
        await self._migrate()
        await self._db.executescript(_SCHEMA_POST_MIGRATION)
        await self._db.commit()

    async def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        async with self._conn.execute("PRAGMA table_info(jobs)") as cursor:
            existing = {row["name"] for row in await cursor.fetchall()}
        for name, sql_type in _ADDED_COLUMNS:
            if name not in existing:
                # Both interpolated names come from `_ADDED_COLUMNS`, never from input.
                await self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {sql_type}")
                logger.info("migrated job store: added column %s", name)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("JobStore is not open")
        return self._db

    # ------------------------------------------------------------------ writes

    async def enqueue(
        self,
        source_asset_id: str,
        payload: dict[str, Any],
        *,
        delay_seconds: int,
        asset_type: str | None = None,
    ) -> bool:
        """Insert a new job. Returns ``False`` if the asset is already known.

        The ``ON CONFLICT DO NOTHING`` is the hard guarantee that a webhook replay for
        an asset we have already seen never starts a second pipeline run.

        ``asset_type`` decides which worker lane picks the job up. It is derived from the
        payload when not given, so callers never have to pass it twice.
        """
        now = _now()
        run_after = now + timedelta(seconds=delay_seconds)
        cursor = await self._conn.execute(
            f"INSERT INTO jobs ({_COLUMNS}) VALUES "  # noqa: S608 - _COLUMNS is a module constant
            "(?, ?, NULL, NULL, NULL, NULL, NULL, NULL, 0, NULL, ?, ?, ?, ?, NULL, ?, NULL, NULL, NULL) "
            "ON CONFLICT(source_asset_id) DO NOTHING",
            (
                source_asset_id,
                JobState.QUEUED.value,
                json.dumps(payload, separators=(",", ":")),
                _iso(now),
                _iso(now),
                _iso(run_after),
                asset_type or _asset_type_from_payload(payload),
            ),
        )
        await self._conn.commit()
        inserted = cursor.rowcount > 0
        if not inserted:
            logger.debug("asset %s is already queued/processed — ignoring replay", source_asset_id)
        return inserted

    async def claim_next(self, *, types: Sequence[str] | None = None) -> Job | None:
        """Atomically move one due job into ``running`` and return it.

        ``types`` restricts the claim to one worker lane. Rows written before the
        ``asset_type`` column existed carry ``NULL`` and are claimable from every lane, so
        an upgrade never strands a job that was queued by the previous version.
        """
        now = _iso(_now())
        placeholders = ", ".join("?" for _ in _RESUMABLE)
        params: list[Any] = [*(state.value for state in _RESUMABLE), now]
        lane = ""
        if types:
            lane = f"AND (asset_type IS NULL OR asset_type IN ({', '.join('?' for _ in types)})) "
            params.extend(types)
        async with self._conn.execute(
            "SELECT source_asset_id FROM jobs "  # noqa: S608 - placeholders are generated from constants
            f"WHERE state IN ({placeholders}) AND run_after <= ? {lane}"
            "ORDER BY run_after ASC LIMIT 1",
            params,
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        asset_id: str = row["source_asset_id"]
        await self._conn.execute(
            "UPDATE jobs SET state = ?, attempts = attempts + 1, updated_at = ? WHERE source_asset_id = ?",
            (JobState.RUNNING.value, now, asset_id),
        )
        await self._conn.commit()
        return await self.get(asset_id)

    async def update(self, source_asset_id: str, **fields: Any) -> None:
        """Patch arbitrary columns of one job. Enum values are unwrapped automatically."""
        if not fields:
            return
        allowed = {
            "state",
            "skip_reason",
            "new_asset_id",
            "new_checksum",
            "orig_bytes",
            "new_bytes",
            "ratio",
            "attempts",
            "last_error",
            "delete_after",
            "run_after",
            "source_checksum",
            "owner_id",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown job columns: {sorted(unknown)}")

        values: list[Any] = []
        for key in fields:
            value = fields[key]
            if isinstance(value, JobState | SkipReason):
                value = value.value
            elif isinstance(value, datetime):
                value = _iso(value)
            values.append(value)

        assignments = ", ".join(f"{key} = ?" for key in fields)
        await self._conn.execute(
            f"UPDATE jobs SET {assignments}, updated_at = ? WHERE source_asset_id = ?",  # noqa: S608 - keys checked against `allowed`
            (*values, _iso(_now()), source_asset_id),
        )
        await self._conn.commit()

    async def mark_skipped(self, source_asset_id: str, reason: SkipReason) -> None:
        await self.update(source_asset_id, state=JobState.SKIPPED, skip_reason=reason)

    async def mark_failed(self, source_asset_id: str, error: str) -> None:
        await self.update(source_asset_id, state=JobState.FAILED, last_error=error[:2000])

    async def reschedule(self, source_asset_id: str, *, delay_seconds: float, error: str) -> None:
        """Put a job back into ``queued`` with a backoff delay."""
        run_after = _now() + timedelta(seconds=delay_seconds)
        await self.update(
            source_asset_id,
            state=JobState.QUEUED,
            run_after=run_after,
            last_error=error[:2000],
        )

    async def reset(self, source_asset_id: str) -> bool:
        """Force a re-run of an asset (``POST /reprocess/{id}``)."""
        cursor = await self._conn.execute(
            "UPDATE jobs SET state = ?, skip_reason = NULL, attempts = 0, last_error = NULL, "
            "run_after = ?, updated_at = ? WHERE source_asset_id = ?",
            (JobState.QUEUED.value, _iso(_now()), _iso(_now()), source_asset_id),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def requeue_skipped(self, reason: SkipReason) -> list[str]:
        """Put every job skipped for ``reason`` back into the queue.

        Needed whenever a guard or the sanity gate itself changes: the affected assets are
        in a terminal state locally and no webhook will fire for them a second time.
        Returns the asset ids that were re-queued.
        """
        asset_ids = await self.skipped_asset_ids(reason)
        if not asset_ids:
            return []
        now = _iso(_now())
        await self._conn.execute(
            "UPDATE jobs SET state = ?, skip_reason = NULL, attempts = 0, last_error = NULL, "
            "run_after = ?, updated_at = ? WHERE state = ? AND skip_reason = ?",
            (JobState.QUEUED.value, now, now, JobState.SKIPPED.value, reason.value),
        )
        await self._conn.commit()
        return asset_ids

    async def requeue_failed(self, *, error_contains: str | None = None) -> list[str]:
        """Put every failed job back into the queue, or only those the error names.

        The counterpart of :meth:`requeue_skipped` for the other terminal state, and needed
        for the same reason: when a gate or the encoder changes, the jobs its previous
        version rejected are parked locally and no webhook will fire for them again. A failed
        job has also used up its attempts, which is why the worker's own backoff never comes
        back to it — ``attempts = 0`` is what makes this a re-run rather than a no-op.

        Returns the asset ids that were re-queued.
        """
        asset_ids = await self.failed_asset_ids(error_contains=error_contains)
        if not asset_ids:
            return []
        predicate, parameters = _failed_predicate(error_contains)
        now = _iso(_now())
        await self._conn.execute(
            "UPDATE jobs SET state = ?, skip_reason = NULL, attempts = 0, last_error = NULL, "  # noqa: S608 - predicate is a literal
            f"run_after = ?, updated_at = ? WHERE {predicate}",
            (JobState.QUEUED.value, now, now, *parameters),
        )
        await self._conn.commit()
        return asset_ids

    async def delete(self, source_asset_id: str) -> bool:
        cursor = await self._conn.execute("DELETE FROM jobs WHERE source_asset_id = ?", (source_asset_id,))
        await self._conn.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------- reads

    async def get(self, source_asset_id: str) -> Job | None:
        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM jobs WHERE source_asset_id = ?",  # noqa: S608 - _COLUMNS is a module constant
            (source_asset_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_job(row) if row else None

    async def list_jobs(self, *, state: JobState | None = None, limit: int = 200) -> list[Job]:
        if state is not None:
            query = f"SELECT {_COLUMNS} FROM jobs WHERE state = ? ORDER BY updated_at DESC LIMIT ?"  # noqa: S608
            params: Sequence[Any] = (state.value, limit)
        else:
            query = f"SELECT {_COLUMNS} FROM jobs ORDER BY updated_at DESC LIMIT ?"  # noqa: S608
            params = (limit,)
        async with self._conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_job(row) for row in rows]

    async def find_replaced_original(
        self,
        *,
        checksum: str | None,
        owner_id: str | None,
        exclude_asset_id: str,
    ) -> Job | None:
        """The earlier job whose *original* had exactly these bytes, if there is one.

        This is the ledger read: an asset that arrives carrying the checksum of an original
        this service already replaced is that original, uploaded again by a device that
        still held the file. Immich's own uniqueness constraint is ``(ownerId, checksum)``,
        so the match is scoped the same way — two users owning the same photo are two
        assets, and neither is evidence about the other.

        Both halves must be present. A row from before the ledger existed carries neither
        and can never match, which is the correct outcome: silence, not a guess. The excess
        of caution is deliberate — a false negative costs one wasted re-encode, exactly
        what happens today, while a false positive would skip an asset that deserved work.
        """
        if not checksum or not owner_id:
            return None
        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM jobs "  # noqa: S608 - _COLUMNS is a module constant
            "WHERE source_checksum = ? AND owner_id = ? AND new_asset_id IS NOT NULL "
            "AND source_asset_id != ? ORDER BY updated_at DESC LIMIT 1",
            (checksum, owner_id, exclude_asset_id),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_job(row) if row else None

    # The predicate `ledger_entries` shares with `find_replaced_original`. Written once so
    # the two can never drift: the guard and the shim must agree on what "this service
    # replaced that" means, or the shim translates a checksum the guard would not
    # recognise coming back.
    _LEDGER_PREDICATE = "source_checksum IS NOT NULL AND owner_id IS NOT NULL AND new_asset_id IS NOT NULL"

    async def ledger_entries(self) -> list[LedgerEntry]:
        """Every replacement this service made, for the shim's translation maps.

        Deliberately unfiltered by job state and by the gate: the shim needs the closed
        rows too, because an ``AssetDeleteV1`` for one of their originals is exactly what
        opens them. Rows from before the ledger existed carry no checksum and no owner and
        are excluded here for the same reason `find_replaced_original` ignores them —
        there is nothing left to identify the original by.
        """
        query = (
            "SELECT source_asset_id, new_asset_id, source_checksum, owner_id, new_checksum, "  # noqa: S608 - a class constant, never caller input
            f"original_freed_at FROM jobs WHERE {self._LEDGER_PREDICATE}"
        )
        async with self._conn.execute(query) as cursor:
            rows = await cursor.fetchall()
        return [
            LedgerEntry(
                source_asset_id=row["source_asset_id"],
                new_asset_id=row["new_asset_id"],
                source_checksum=row["source_checksum"],
                owner_id=row["owner_id"],
                new_checksum=row["new_checksum"],
                original_freed_at=_parse(row["original_freed_at"]),
            )
            for row in rows
        ]

    async def returned_originals(self) -> list[ReturnedOriginal]:
        """Assets that brought a replaced original's checksum back, and are still here.

        The other half of what the shim's maps are built from, and the reason
        `ledger_entries` alone is not enough. `_check_re_upload` parks a returning file at
        ``re_uploaded`` and deliberately leaves it alone, so its checksum — which is some
        replacement's ``source_checksum`` — is live on the server again under a new id. A
        translation for that checksum has to wait for this row's own asset to go, and
        ``original_freed_at`` is where that is recorded, meaning here exactly what it means
        on a ledger row.

        Not filtered by state: a row carrying this reason is skipped by construction, and
        the direction to err in is suppressing a translation that would have been safe
        rather than making one that is not.
        """
        async with self._conn.execute(
            "SELECT source_asset_id, owner_id, source_checksum FROM jobs "
            "WHERE skip_reason = ? AND source_checksum IS NOT NULL AND owner_id IS NOT NULL "
            "AND original_freed_at IS NULL",
            (SkipReason.RE_UPLOADED.value,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            ReturnedOriginal(
                asset_id=row["source_asset_id"],
                owner_id=row["owner_id"],
                checksum=row["source_checksum"],
            )
            for row in rows
        ]

    async def mark_original_freed(self, source_asset_id: str, *, when: datetime | None = None) -> bool:
        """Record that this asset has stopped existing. Returns ``False`` when it already had.

        First write wins. Three callers reach it, and the first two arrive from opposite
        directions at the same row — the pipeline right after a ``permanent`` delete it
        performed itself, the shim when it sees the purge for a trashed original go past on
        the sync stream — so on a deployment that switched ``delete_mode`` both can fire for
        one replacement. The timestamp is only ever read as "is this set", so the earlier
        one is the honest one to keep.

        The third is the shim seeing a delete for a `ReturnedOriginal`, whose row is not a
        replacement at all. Same column, same meaning, different consequence: no gate opens,
        a suppressed translation is re-armed instead.

        This return value is also what keeps `SHIM_GATES_OPENED` and `SHIM_TOUCHES` honest:
        every caller stops on a ``False`` from here, so one row cannot be counted twice even
        when the shim's ledger is up to a refresh interval behind the delete the pipeline
        just performed.
        """
        async with self._conn.execute(
            "UPDATE jobs SET original_freed_at = ?, updated_at = ? "
            "WHERE source_asset_id = ? AND original_freed_at IS NULL",
            (_iso(when or _now()), _iso(_now()), source_asset_id),
        ) as cursor:
            changed = cursor.rowcount > 0
        await self._conn.commit()
        return changed

    async def _asset_ids(self, where: str, parameters: Sequence[Any], *, order: str) -> list[str]:
        """The ``source_asset_id`` of every job one predicate selects, in ``order``.

        The three selections below are the terminal states a CLI command offers to act on,
        and they only differ in their ``WHERE`` and their tie-break. Sharing the query
        keeps them returning the same shape — a list of ids, in a defined order, never a
        row — which is what every caller passes straight on to Immich or to a requeue.
        """
        async with self._conn.execute(
            f"SELECT source_asset_id FROM jobs WHERE {where} ORDER BY {order}",  # noqa: S608 - both are literals
            parameters,
        ) as cursor:
            return [row["source_asset_id"] for row in await cursor.fetchall()]

    async def replaced_source_asset_ids(self) -> list[str]:
        """Every original this service replaced — the selection ``restore --all-pending`` uses.

        Completed jobs that carry a replacement, oldest first. Deliberately unlimited and
        deliberately narrow: a limit would leave the oldest originals sitting in the trash
        without saying so, and any state short of ``done`` has not had its original
        removed yet, so restoring it would touch an asset this service did not take away.
        """
        return await self._asset_ids(
            "state = ? AND new_asset_id IS NOT NULL",
            (JobState.DONE.value,),
            order="updated_at ASC, source_asset_id ASC",
        )

    async def skipped_asset_ids(self, reason: SkipReason) -> list[str]:
        """Every asset currently parked in ``skipped`` for one specific reason."""
        return await self._asset_ids(
            "state = ? AND skip_reason = ?",
            (JobState.SKIPPED.value, reason.value),
            order="updated_at ASC",
        )

    async def failed_asset_ids(self, *, error_contains: str | None = None) -> list[str]:
        """Every asset parked in ``failed``, or only those whose error contains a string."""
        predicate, parameters = _failed_predicate(error_contains)
        return await self._asset_ids(predicate, parameters, order="updated_at ASC")

    async def due_deletions(self, limit: int = 50) -> list[Job]:
        async with self._conn.execute(
            f"SELECT {_COLUMNS} FROM jobs WHERE state = ? AND delete_after IS NOT NULL "  # noqa: S608
            "AND delete_after <= ? ORDER BY delete_after ASC LIMIT ?",
            (JobState.PENDING_DELETE.value, _iso(_now()), limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_job(row) for row in rows]

    async def stats(self) -> dict[str, Any]:
        async with self._conn.execute("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state") as cur:
            by_state = {row["state"]: row["n"] for row in await cur.fetchall()}
        async with self._conn.execute(
            "SELECT skip_reason, COUNT(*) AS n FROM jobs WHERE skip_reason IS NOT NULL GROUP BY skip_reason"
        ) as cur:
            by_reason = {row["skip_reason"]: row["n"] for row in await cur.fetchall()}
        async with self._conn.execute(
            "SELECT COALESCE(SUM(orig_bytes), 0) AS o, COALESCE(SUM(new_bytes), 0) AS n, "
            "COUNT(*) AS c FROM jobs WHERE new_bytes IS NOT NULL AND state IN (?, ?, ?)",
            (JobState.DONE.value, JobState.PENDING_DELETE.value, JobState.LINKED.value),
        ) as cur:
            row = await cur.fetchone()
        orig = int(row["o"]) if row else 0
        new = int(row["n"]) if row else 0
        return {
            "by_state": by_state,
            "by_skip_reason": by_reason,
            "total": sum(by_state.values()),
            "compressed_assets": int(row["c"]) if row else 0,
            "original_bytes": orig,
            "compressed_bytes": new,
            "saved_bytes": max(orig - new, 0),
            "average_ratio": round(new / orig, 4) if orig else None,
        }

    # ----------------------------------------------------------------- counters

    async def bump_counter(self, name: str, delta: int = 1) -> None:
        """Add ``delta`` to a counter, creating it at that value if it is new."""
        await self._conn.execute(
            "INSERT INTO counters (name, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value = value + excluded.value, "
            "updated_at = excluded.updated_at",
            (name, delta, _iso(_now())),
        )
        await self._conn.commit()

    async def counters(self) -> dict[str, int]:
        """Every counter, with the known names present at zero.

        Zero-filled on purpose: "0 received, 7 rejected" is the whole diagnosis, and it
        cannot be read off a row that does not exist yet.
        """
        values = {WEBHOOKS_RECEIVED: 0, WEBHOOKS_REJECTED: 0}
        values.update(dict.fromkeys(SHIM_COUNTERS, 0))
        async with self._conn.execute("SELECT name, value FROM counters") as cursor:
            values.update({row["name"]: int(row["value"]) for row in await cursor.fetchall()})
        return values

    # ------------------------------------------------------------- the pause latch

    async def pause(self, reason: str) -> bool:
        """Latch the service paused. Returns ``False`` when it already was.

        Deliberately not idempotent-silent: the first trip is the one worth logging, and a
        second surge inside an existing pause must not overwrite the original reason.
        """
        now = _now()
        state = PauseState(reason=reason, since=now)
        cursor = await self._conn.execute(
            "INSERT INTO service_state (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO NOTHING",
            (_PAUSE_KEY, state.model_dump_json(), _iso(now)),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def resume(self) -> bool:
        """Clear the latch. Returns ``False`` when the service was not paused."""
        cursor = await self._conn.execute("DELETE FROM service_state WHERE key = ?", (_PAUSE_KEY,))
        await self._conn.commit()
        return cursor.rowcount > 0

    async def pause_state(self) -> PauseState | None:
        """The latch, or ``None`` when the service is running."""
        async with self._conn.execute(
            "SELECT value FROM service_state WHERE key = ?", (_PAUSE_KEY,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        try:
            return PauseState.model_validate_json(row["value"])
        except ValueError:  # pragma: no cover - only a hand-edited database gets here
            logger.warning("service_state.%s is unreadable; treating the service as paused", _PAUSE_KEY)
            return PauseState(reason="unreadable latch in the database", since=_now())

    # ---------------------------------------------------- service state, generally

    async def get_state(self, key: str) -> dict[str, Any] | None:
        """Read one ``service_state`` row as JSON, or ``None`` when it is not set.

        The table has held exactly one hand-written key since it was introduced; this is
        the generic pair for everything that only needs a small JSON blob to outlive the
        process — the backfill scan cursor being the first.
        """
        async with self._conn.execute("SELECT value FROM service_state WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row["value"])
        except ValueError:  # pragma: no cover - only a hand-edited database gets here
            logger.warning("service_state.%s is unreadable; ignoring it", key)
            return None
        return value if isinstance(value, dict) else None

    async def set_state(self, key: str, value: dict[str, Any]) -> None:
        await self._conn.execute(
            "INSERT INTO service_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, json.dumps(value, separators=(",", ":")), _iso(_now())),
        )
        await self._conn.commit()

    async def clear_state(self, key: str) -> None:
        await self._conn.execute("DELETE FROM service_state WHERE key = ?", (key,))
        await self._conn.commit()

    # --------------------------------------------------------- backfill inventory

    async def record_candidates(self, candidates: Sequence[BackfillCandidate]) -> int:
        """Upsert one scanned page. Returns how many rows were written.

        ``queued_at`` is deliberately absent from the update list: a re-scan refreshes what
        the server says about an asset, and must not forget that a queue run already
        reached it.
        """
        if not candidates:
            return 0
        await self._conn.executemany(
            f"INSERT INTO backfill_candidates ({_CANDIDATE_COLUMNS}) "  # noqa: S608 - module constant
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL) "
            "ON CONFLICT(asset_id) DO UPDATE SET "
            "asset_type = excluded.asset_type, size_bytes = excluded.size_bytes, "
            "filename = excluded.filename, verdict = excluded.verdict, "
            "payload = excluded.payload, scanned_at = excluded.scanned_at",
            [
                (
                    candidate.asset_id,
                    candidate.asset_type,
                    candidate.size_bytes,
                    candidate.filename,
                    candidate.verdict,
                    json.dumps(candidate.payload, separators=(",", ":")),
                    _iso(candidate.scanned_at),
                )
                for candidate in candidates
            ],
        )
        await self._conn.commit()
        return len(candidates)

    async def pick_candidates(
        self,
        *,
        asset_types: Sequence[str] | None = None,
        order: str = "size",
        limit: int = 50,
    ) -> list[BackfillCandidate]:
        """The rows a queue run may enqueue: guards passed, not queued yet.

        ``order`` is ``size`` (biggest first, which is where the savings are) or
        ``scanned`` (the order the library came back in).
        """
        clauses = ["verdict IS NULL", "queued_at IS NULL"]
        params: list[Any] = []
        if asset_types:
            clauses.append(f"asset_type IN ({', '.join('?' for _ in asset_types)})")
            params.extend(asset_types)
        ordering = "size_bytes DESC, asset_id" if order == "size" else "scanned_at ASC, asset_id"
        params.append(max(limit, 0))
        async with self._conn.execute(
            f"SELECT {_CANDIDATE_COLUMNS} FROM backfill_candidates "  # noqa: S608 - generated from constants
            f"WHERE {' AND '.join(clauses)} ORDER BY {ordering} LIMIT ?",
            params,
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_candidate(row) for row in rows]

    async def mark_candidate_queued(self, asset_id: str) -> None:
        """Record that this asset made it into the job store."""
        await self._conn.execute(
            "UPDATE backfill_candidates SET queued_at = ? WHERE asset_id = ?",
            (_iso(_now()), asset_id),
        )
        await self._conn.commit()

    async def set_candidate_verdict(self, asset_id: str, verdict: str) -> None:
        """Downgrade a candidate the live re-check refused. Also frees its payload.

        Called when the world moved between the scan and the queue run — the asset is
        gone, in the trash, or already has a job. The payload is dropped with it: nothing
        will enqueue this row again, so keeping a kilobyte of JSON per asset would be
        pure ballast in a database that lives next to the photos it processes.
        """
        await self._conn.execute(
            "UPDATE backfill_candidates SET verdict = ?, payload = '{}' WHERE asset_id = ?",
            (verdict, asset_id),
        )
        await self._conn.commit()

    async def clear_inventory(self, asset_types: Sequence[str] | None = None) -> int:
        """Drop the inventory, entirely or for one type. Returns the rows removed."""
        if asset_types:
            placeholders = ", ".join("?" for _ in asset_types)
            cursor = await self._conn.execute(
                f"DELETE FROM backfill_candidates WHERE asset_type IN ({placeholders})",  # noqa: S608
                tuple(asset_types),
            )
        else:
            cursor = await self._conn.execute("DELETE FROM backfill_candidates")
        await self._conn.commit()
        return cursor.rowcount

    async def inventory_stats(self) -> dict[str, Any]:
        """What the scan knows, per asset type, shaped for printing and for ``--json``."""
        async with self._conn.execute(
            "SELECT asset_type, COUNT(*) AS scanned, "
            "COALESCE(SUM(size_bytes), 0) AS scanned_bytes, "
            "COALESCE(SUM(verdict IS NULL AND queued_at IS NULL), 0) AS candidates, "
            "COALESCE(SUM(CASE WHEN verdict IS NULL AND queued_at IS NULL THEN size_bytes ELSE 0 END), 0) "
            "AS candidate_bytes, "
            "COALESCE(SUM(queued_at IS NOT NULL), 0) AS queued, "
            "MAX(scanned_at) AS last_scan "
            "FROM backfill_candidates GROUP BY asset_type"
        ) as cursor:
            per_type = {
                row["asset_type"]: {
                    "scanned": int(row["scanned"]),
                    "scanned_bytes": int(row["scanned_bytes"]),
                    "candidates": int(row["candidates"]),
                    "candidate_bytes": int(row["candidate_bytes"]),
                    "queued": int(row["queued"]),
                    "last_scan": row["last_scan"],
                    "by_verdict": {},
                }
                for row in await cursor.fetchall()
            }
        async with self._conn.execute(
            "SELECT asset_type, verdict, COUNT(*) AS n FROM backfill_candidates "
            "WHERE verdict IS NOT NULL GROUP BY asset_type, verdict"
        ) as cursor:
            for row in await cursor.fetchall():
                entry = per_type.get(row["asset_type"])
                if entry is not None:
                    entry["by_verdict"][row["verdict"]] = int(row["n"])
        return {
            "types": per_type,
            "scanned": sum(entry["scanned"] for entry in per_type.values()),
            "candidates": sum(entry["candidates"] for entry in per_type.values()),
            "candidate_bytes": sum(entry["candidate_bytes"] for entry in per_type.values()),
            "queued": sum(entry["queued"] for entry in per_type.values()),
        }

    async def terminal_count(self) -> int:
        placeholders = ", ".join("?" for _ in TERMINAL_STATES)
        async with self._conn.execute(
            f"SELECT COUNT(*) AS n FROM jobs WHERE state IN ({placeholders})",  # noqa: S608
            tuple(state.value for state in TERMINAL_STATES),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["n"]) if row else 0


def _asset_type_from_payload(payload: dict[str, Any]) -> str | None:
    """``VIDEO``/``IMAGE`` out of a webhook body, or ``None`` when it is not in there."""
    asset = payload.get("data", {}).get("asset", {}) if isinstance(payload.get("data"), dict) else {}
    asset_type = asset.get("type") if isinstance(asset, dict) else None
    return asset_type if isinstance(asset_type, str) and asset_type else None


def _row_to_candidate(row: aiosqlite.Row) -> BackfillCandidate:
    try:
        payload = json.loads(row["payload"])
    except ValueError:  # pragma: no cover - only a hand-edited database gets here
        payload = {}
    return BackfillCandidate(
        asset_id=row["asset_id"],
        asset_type=row["asset_type"],
        size_bytes=int(row["size_bytes"]),
        filename=row["filename"],
        verdict=row["verdict"],
        payload=payload if isinstance(payload, dict) else {},
        scanned_at=_parse(row["scanned_at"]) or _now(),
        queued_at=_parse(row["queued_at"]),
    )


def _row_to_job(row: aiosqlite.Row) -> Job:
    return Job(
        source_asset_id=row["source_asset_id"],
        asset_type=row["asset_type"],
        state=JobState(row["state"]),
        skip_reason=SkipReason(row["skip_reason"]) if row["skip_reason"] else None,
        new_asset_id=row["new_asset_id"],
        new_checksum=row["new_checksum"],
        source_checksum=row["source_checksum"],
        owner_id=row["owner_id"],
        original_freed_at=_parse(row["original_freed_at"]),
        orig_bytes=row["orig_bytes"],
        new_bytes=row["new_bytes"],
        ratio=row["ratio"],
        attempts=row["attempts"],
        last_error=row["last_error"],
        payload=row["payload"],
        created_at=_parse(row["created_at"]) or _now(),
        updated_at=_parse(row["updated_at"]) or _now(),
        run_after=_parse(row["run_after"]) or _now(),
        delete_after=_parse(row["delete_after"]),
    )
