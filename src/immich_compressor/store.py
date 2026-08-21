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

from .models import TERMINAL_STATES, Job, JobState, PauseState, SkipReason

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
    asset_type      TEXT
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
"""

# The single `service_state` key the surge breaker owns.
_PAUSE_KEY = "paused"

# Indexes over columns from `_ADDED_COLUMNS`. They have to run *after* the migration:
# on a database created before that column existed, `CREATE INDEX` would fail here.
_SCHEMA_POST_MIGRATION = """
CREATE INDEX IF NOT EXISTS idx_jobs_lane ON jobs (state, asset_type, run_after);
"""

_COLUMNS = (
    "source_asset_id, state, skip_reason, new_asset_id, new_checksum, orig_bytes, new_bytes, "
    "ratio, attempts, last_error, payload, created_at, updated_at, run_after, delete_after, "
    "asset_type"
)

# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` cannot add them to a
# database that already exists, so they are applied by hand on open.
_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("new_checksum", "TEXT"),
    # Lets a worker claim only its own kind of job. Without it a single two-hour video
    # holds the queue and every one-second image job waits behind it.
    ("asset_type", "TEXT"),
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
            "(?, ?, NULL, NULL, NULL, NULL, NULL, NULL, 0, NULL, ?, ?, ?, ?, NULL, ?) "
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

    async def skipped_asset_ids(self, reason: SkipReason) -> list[str]:
        """Every asset currently parked in ``skipped`` for one specific reason."""
        async with self._conn.execute(
            "SELECT source_asset_id FROM jobs WHERE state = ? AND skip_reason = ? ORDER BY updated_at ASC",
            (JobState.SKIPPED.value, reason.value),
        ) as cursor:
            return [row["source_asset_id"] for row in await cursor.fetchall()]

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


def _row_to_job(row: aiosqlite.Row) -> Job:
    return Job(
        source_asset_id=row["source_asset_id"],
        asset_type=row["asset_type"],
        state=JobState(row["state"]),
        skip_reason=SkipReason(row["skip_reason"]) if row["skip_reason"] else None,
        new_asset_id=row["new_asset_id"],
        new_checksum=row["new_checksum"],
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
