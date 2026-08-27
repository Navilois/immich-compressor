"""The Immich app's local mirror, reproduced, with the shim's output replayed through it.

The shim's whole design turns on one invariant: **at most one mirrored row may hold a
given ``(owner_id, checksum)`` pair**. The app enforces that with a partial UNIQUE index,
and the pipeline's ``jobs.original_freed_at`` gate exists to keep the replacement from
taking the original's checksum while the original's row still holds it. Every other test
in this repository checks that the gate is *consulted*. This one checks that the gate is
*right* — that an open gate produces an upsert the app accepts, and that a closed one is
the only thing standing between a user and a broken sync.

It needs no live Immich and no phone. The mirror is a SQLite database built from Immich's
own schema, driven by the same three operations the app performs, so the damage a premature
translation would do happens here instead of on someone's device.

Everything below is transcribed from ``immich-app/immich`` at
``093f5c070ad14ccc63ff3087e48d5d50f6fbda24``:

- ``mobile/lib/data/db/main/table/remote/asset.dart`` — the columns, ``isStrict``,
  ``withoutRowId``, and both partial unique indexes.
- ``mobile/lib/infrastructure/repositories/sync_stream.repository.dart`` —
  ``updateAssetsV2`` (the upsert) and ``deleteAssetsV1`` (the delete), each wrapped in a
  ``batch()`` whose ``catch`` ends in ``rethrow``.
- ``mobile/lib/infrastructure/repositories/backup.repository.dart`` — ``getCandidates``,
  whose ``notExistsQuery`` on ``(checksum, owner_id)`` is what decides whether a local file
  gets uploaded. Note there is no ``deleted_at`` filter in it: a soft-deleted row still
  suppresses the upload.

``batch.insert(..., mode: InsertMode.insertOrReplace, onConflict: DoUpdate((_) => companion))``
compiles to ``INSERT OR REPLACE INTO … ON CONFLICT(id) DO UPDATE SET …`` — the mode keyword
comes from drift's ``InsertMode`` map and the conflict target defaults to the primary key,
which for this table is ``id`` alone.

That combination has *two* distinct failure modes, and which one fires depends on whether
the phone has already mirrored the replacement. Both are exercised in the negative control
at the bottom of this file, because a simulation that cannot reproduce the damage proves
nothing when it comes back green.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

import pytest

from immich_compressor.models import LedgerEntry
from immich_compressor.shim import TranslationMaps, rewrite_sync_line

OWNER = "11111111-1111-4111-8111-111111111111"
OTHER_OWNER = "22222222-2222-4222-8222-222222222222"
ORIGINAL_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
REPLACEMENT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
UNRELATED_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

# Base64 SHA-1, the shape Immich actually puts on the wire.
ORIGINAL_HASH = "02MpaJkpzGHNbGwxWtencVNK7uY="
REPLACEMENT_HASH = "z9K1aQq0PPnB1sVXhF2mQ7t0abc="
UNRELATED_HASH = "Qm9ndXNIYXNoRm9yVGVzdGluZzA9"

# Transcribed from RemoteAssetEntity. `STRICT, WITHOUT ROWID` and the two partial unique
# indexes are the parts that matter; the column list is complete so that a payload which
# would not fit the real table does not fit this one either.
MIRROR_DDL = """
CREATE TABLE remote_asset_entity (
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    type INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    width INTEGER,
    height INTEGER,
    duration_ms INTEGER,
    checksum TEXT NOT NULL,
    is_favorite INTEGER NOT NULL DEFAULT 0,
    owner_id TEXT NOT NULL,
    local_date_time INTEGER,
    thumb_hash TEXT,
    deleted_at INTEGER,
    uploaded_at INTEGER,
    live_photo_video_id TEXT,
    visibility INTEGER NOT NULL,
    stack_id TEXT,
    library_id TEXT,
    is_edited INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (id)
) STRICT, WITHOUT ROWID;

CREATE UNIQUE INDEX IF NOT EXISTS UQ_remote_assets_owner_checksum
ON remote_asset_entity (owner_id, checksum)
WHERE (library_id IS NULL);

CREATE UNIQUE INDEX IF NOT EXISTS UQ_remote_assets_owner_library_checksum
ON remote_asset_entity (owner_id, library_id, checksum)
WHERE (library_id IS NOT NULL);
"""

# The column order used for every insert. `id` first because the companion is
# `copyWith(id: ...)`-ed onto the insert only; the DO UPDATE set is the companion without
# it, which is why `_UPDATE_COLUMNS` drops it.
_COLUMNS = (
    "id",
    "name",
    "type",
    "created_at",
    "updated_at",
    "width",
    "height",
    "duration_ms",
    "checksum",
    "is_favorite",
    "owner_id",
    "local_date_time",
    "thumb_hash",
    "deleted_at",
    "uploaded_at",
    "live_photo_video_id",
    "visibility",
    "stack_id",
    "library_id",
    "is_edited",
)
_UPDATE_COLUMNS = tuple(column for column in _COLUMNS if column != "id")

# `InsertMode.insertOrReplace` + `DoUpdate` with no explicit target.
_UPSERT_SQL = (
    f"INSERT OR REPLACE INTO remote_asset_entity ({', '.join(_COLUMNS)}) "  # noqa: S608 - _COLUMNS is a constant
    f"VALUES ({', '.join('?' * len(_COLUMNS))}) "
    f"ON CONFLICT(id) DO UPDATE SET "
    f"{', '.join(f'{column} = excluded.{column}' for column in _UPDATE_COLUMNS)}"
)


def sync_asset(
    asset_id: str = REPLACEMENT_ID,
    checksum: str = REPLACEMENT_HASH,
    *,
    owner: str = OWNER,
    deleted_at: str | None = None,
) -> dict[str, Any]:
    """One ``SyncAssetV2`` payload, with the field names ``updateAssetsV2`` reads."""
    return {
        "id": asset_id,
        "ownerId": owner,
        "checksum": checksum,
        "originalFileName": f"{asset_id[:8]}.mp4",
        "type": "VIDEO",
        "fileCreatedAt": "2026-01-01T00:00:00.000Z",
        "fileModifiedAt": "2026-01-01T00:00:00.000Z",
        "createdAt": "2026-01-01T00:00:00.000Z",
        "localDateTime": "2026-01-01T00:00:00.000Z",
        "duration": 1000,
        "isFavorite": False,
        "thumbhash": None,
        "deletedAt": deleted_at,
        "visibility": "timeline",
        "livePhotoVideoId": None,
        "stackId": None,
        "libraryId": None,
        "width": 1920,
        "height": 1080,
        "isEdited": False,
    }


def asset_line(asset_id: str = REPLACEMENT_ID, checksum: str = REPLACEMENT_HASH, **kwargs: Any) -> bytes:
    data = sync_asset(asset_id, checksum, **kwargs)
    record = {"type": "AssetV2", "data": data, "ack": f"AssetV2|0198-{asset_id[:8]}"}
    return json.dumps(record).encode() + b"\n"


def delete_line(asset_id: str = ORIGINAL_ID) -> bytes:
    record = {"type": "AssetDeleteV1", "data": {"assetId": asset_id}, "ack": "AssetDeleteV1|0198-del"}
    return json.dumps(record).encode() + b"\n"


def ledger(*, freed: bool, new_checksum: str | None = REPLACEMENT_HASH) -> LedgerEntry:
    return LedgerEntry(
        source_asset_id=ORIGINAL_ID,
        new_asset_id=REPLACEMENT_ID,
        source_checksum=ORIGINAL_HASH,
        owner_id=OWNER,
        new_checksum=new_checksum,
        original_freed_at=datetime.now(UTC) if freed else None,
    )


class Mirror:
    """``remote_asset_entity`` and the three operations the app performs on it.

    ``apply`` is deliberately the *only* way a row changes: it takes the bytes a client
    would receive and does what the client does with them, so a test cannot accidentally
    assert against a state the app could never reach.
    """

    def __init__(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(MIRROR_DDL)

    def _to_row(self, data: dict[str, Any]) -> list[Any]:
        """The ``RemoteAssetEntityCompanion`` of ``updateAssetsV2``, as bound parameters."""
        return [
            data["id"],
            data["originalFileName"],
            1 if data["type"] == "VIDEO" else 0,
            0,
            0,
            data["width"],
            data["height"],
            data["duration"],
            data["checksum"],
            int(bool(data["isFavorite"])),
            data["ownerId"],
            0,
            data["thumbhash"],
            None if data["deletedAt"] is None else 1,
            0,
            data["livePhotoVideoId"],
            0 if data["visibility"] == "timeline" else 1,
            data["stackId"],
            data["libraryId"],
            int(bool(data["isEdited"])),
        ]

    def apply(self, *lines: bytes) -> None:
        """Replay a run of sync lines as one ``batch()``: all of it, or none of it.

        drift's ``batch()`` is a single transaction and every one of these repository
        methods ends its ``catch`` with ``rethrow``, so a constraint violation on any line
        takes the whole batch — including healthy lines that came before it — and then
        propagates out into the client's sync.
        """
        with self.db:
            for line in lines:
                record = json.loads(line)
                data = record["data"]
                if record["type"] == "AssetDeleteV1":
                    self.db.execute("DELETE FROM remote_asset_entity WHERE id = ?", (data["assetId"],))
                else:
                    self.db.execute(_UPSERT_SQL, self._to_row(data))

    def checksum_of(self, asset_id: str) -> str | None:
        row = self.db.execute("SELECT checksum FROM remote_asset_entity WHERE id = ?", (asset_id,)).fetchone()
        return None if row is None else row["checksum"]

    def holders_of(self, checksum: str, owner: str = OWNER) -> list[str]:
        """Every mirrored asset id holding this checksum. The invariant says at most one."""
        return [
            row["id"]
            for row in self.db.execute(
                "SELECT id FROM remote_asset_entity WHERE checksum = ? AND owner_id = ? ORDER BY id",
                (checksum, owner),
            )
        ]

    def is_backup_candidate(self, local_checksum: str, owner: str = OWNER) -> bool:
        """``getCandidates``' ``notExistsQuery``, which is what queues an upload.

        A local file is a candidate exactly when nothing in the mirror holds its checksum
        for this user. There is no ``deleted_at`` condition in the original and there is
        none here.
        """
        row = self.db.execute(
            "SELECT 1 FROM remote_asset_entity WHERE checksum = ? AND owner_id = ?",
            (local_checksum, owner),
        ).fetchone()
        return row is None


def translate(line: bytes, entries: list[LedgerEntry]) -> bytes:
    """The shim's own rewrite, driven by a ledger built the way the shim builds it."""
    return rewrite_sync_line(line, TranslationMaps.build(entries)).data


@pytest.fixture
def mirror() -> Mirror:
    """A mirror that has already seen both assets, as a phone in the field would have."""
    m = Mirror()
    m.apply(asset_line(ORIGINAL_ID, ORIGINAL_HASH), asset_line(REPLACEMENT_ID, REPLACEMENT_HASH))
    return m


def test_the_mirror_starts_with_both_rows_and_their_own_checksums(mirror: Mirror) -> None:
    assert mirror.checksum_of(ORIGINAL_ID) == ORIGINAL_HASH
    assert mirror.checksum_of(REPLACEMENT_ID) == REPLACEMENT_HASH
    assert mirror.holders_of(ORIGINAL_HASH) == [ORIGINAL_ID]


def test_a_closed_gate_leaves_the_mirror_untouched(mirror: Mirror) -> None:
    """The replacement's line arrives while the original still exists. Nothing may move."""
    entries = [ledger(freed=False)]
    line = asset_line(REPLACEMENT_ID, REPLACEMENT_HASH)

    assert translate(line, entries) == line, "a closed gate must not rewrite"
    mirror.apply(translate(line, entries))

    assert mirror.checksum_of(ORIGINAL_ID) == ORIGINAL_HASH
    assert mirror.checksum_of(REPLACEMENT_ID) == REPLACEMENT_HASH
    assert mirror.holders_of(ORIGINAL_HASH) == [ORIGINAL_ID]


def test_the_delete_frees_the_original_checksum(mirror: Mirror) -> None:
    """``AssetDeleteV1`` is forwarded unchanged, and it is what makes room."""
    entries = [ledger(freed=False)]
    line = delete_line(ORIGINAL_ID)

    outcome = rewrite_sync_line(line, TranslationMaps.build(entries))
    assert outcome.data == line, "a delete is never rewritten"
    assert [entry.source_asset_id for entry in outcome.gate_opens] == [ORIGINAL_ID]

    mirror.apply(outcome.data)
    assert mirror.checksum_of(ORIGINAL_ID) is None
    assert mirror.holders_of(ORIGINAL_HASH) == []


def test_an_open_gate_hands_the_checksum_over_and_the_app_accepts_it(mirror: Mirror) -> None:
    """The whole sequence, in the order the server emits it. This is the point of the shim."""
    mirror.apply(translate(delete_line(ORIGINAL_ID), [ledger(freed=False)]))

    line = asset_line(REPLACEMENT_ID, REPLACEMENT_HASH)
    rewritten = translate(line, [ledger(freed=True)])
    assert json.loads(rewritten)["data"]["checksum"] == ORIGINAL_HASH

    mirror.apply(rewritten)

    holders = mirror.holders_of(ORIGINAL_HASH)
    assert holders == [REPLACEMENT_ID], "exactly one row holds it, and it is the replacement's"
    assert mirror.checksum_of(REPLACEMENT_ID) == ORIGINAL_HASH


def test_the_local_original_is_no_longer_a_backup_candidate(mirror: Mirror) -> None:
    """The one user-visible outcome: the phone does not re-upload the file it still holds."""
    mirror.apply(translate(delete_line(ORIGINAL_ID), [ledger(freed=False)]))
    control = mirror.is_backup_candidate(ORIGINAL_HASH)
    assert control is True, "control: with nothing holding the checksum, the file would upload"

    mirror.apply(translate(asset_line(REPLACEMENT_ID, REPLACEMENT_HASH), [ledger(freed=True)]))

    assert mirror.is_backup_candidate(ORIGINAL_HASH) is False


def test_the_ack_and_every_other_field_survive_the_rewrite() -> None:
    """One field on one line changes. The client's resume cursor is not ours to touch."""
    line = asset_line(REPLACEMENT_ID, REPLACEMENT_HASH)
    rewritten = json.loads(translate(line, [ledger(freed=True)]))
    original = json.loads(line)

    assert rewritten["ack"] == original["ack"]
    assert rewritten["type"] == original["type"]
    differing = {key for key in original["data"] if original["data"][key] != rewritten["data"][key]}
    assert differing == {"checksum"}


def test_a_soft_deleted_original_still_blocks_the_translation(mirror: Mirror) -> None:
    """Trash is not a delete. The row is still there, still holding the checksum.

    This is why the gate waits for the purge rather than for the move to trash: the
    candidate query has no ``deleted_at`` filter, so a trashed row both suppresses the
    upload and occupies the unique index.
    """
    mirror.apply(asset_line(ORIGINAL_ID, ORIGINAL_HASH, deleted_at="2026-01-02T00:00:00.000Z"))

    assert mirror.holders_of(ORIGINAL_HASH) == [ORIGINAL_ID]
    assert mirror.is_backup_candidate(ORIGINAL_HASH) is False
    assert translate(asset_line(), [ledger(freed=False)]) == asset_line(), "gate still closed"


def test_another_owners_identical_checksum_is_not_a_conflict() -> None:
    """The index is on ``(owner_id, checksum)``, so two users may hold the same bytes."""
    m = Mirror()
    m.apply(
        asset_line(ORIGINAL_ID, ORIGINAL_HASH),
        asset_line(UNRELATED_ID, ORIGINAL_HASH, owner=OTHER_OWNER),
    )
    assert m.holders_of(ORIGINAL_HASH, OWNER) == [ORIGINAL_ID]
    assert m.holders_of(ORIGINAL_HASH, OTHER_OWNER) == [UNRELATED_ID]


# --------------------------------------------------------------------------------------
# The negative control.
#
# Everything above is only worth reading if the damage is real. These two force the
# ungated rewrite — the replacement's line carrying the original's checksum while the
# original's row is still present — and pin down what the app actually does with it.
# --------------------------------------------------------------------------------------


def test_ungated_translation_aborts_the_sync_batch_when_the_replacement_is_mirrored(mirror: Mirror) -> None:
    """Branch one, and the common case: the phone already has the replacement's row.

    The ``id`` conflict fires ``DO UPDATE``, and ``OR REPLACE`` does not cover a violation
    raised by that update, so the unique index aborts it. drift's ``batch()`` is one
    transaction and ``rethrow`` carries the error out — a healthy line in the same batch
    is rolled back with it, which is the user-visible "sync failed".
    """
    healthy = asset_line(UNRELATED_ID, UNRELATED_HASH)
    forced = asset_line(REPLACEMENT_ID, ORIGINAL_HASH)

    with pytest.raises(sqlite3.IntegrityError, match="owner_id"):
        mirror.apply(healthy, forced)

    assert mirror.checksum_of(UNRELATED_ID) is None, "the whole batch rolled back, healthy line included"
    assert mirror.checksum_of(REPLACEMENT_ID) == REPLACEMENT_HASH
    assert mirror.holders_of(ORIGINAL_HASH) == [ORIGINAL_ID]


def test_ungated_translation_destroys_the_original_row_when_the_replacement_is_new() -> None:
    """Branch two, and the quiet one: the phone has not mirrored the replacement yet.

    With no ``id`` to conflict on, the insert violates only the checksum index, ``OR
    REPLACE`` resolves it by *deleting the conflicting row*, and the original vanishes from
    the mirror while it is still very much alive on the server. No exception, no log line,
    and the asset stops existing for that client until a full resync.
    """
    m = Mirror()
    m.apply(asset_line(ORIGINAL_ID, ORIGINAL_HASH))

    m.apply(asset_line(REPLACEMENT_ID, ORIGINAL_HASH))

    assert m.checksum_of(ORIGINAL_ID) is None, "the original's row was silently destroyed"
    assert m.holders_of(ORIGINAL_HASH) == [REPLACEMENT_ID]


def test_the_gate_is_the_only_thing_preventing_both(mirror: Mirror) -> None:
    """The same line, the same mirror, the same code — only ``original_freed_at`` differs."""
    line = asset_line(REPLACEMENT_ID, REPLACEMENT_HASH)

    assert translate(line, [ledger(freed=False)]) == line
    assert json.loads(translate(line, [ledger(freed=True)]))["data"]["checksum"] == ORIGINAL_HASH

    with pytest.raises(sqlite3.IntegrityError):
        mirror.apply(translate(line, [ledger(freed=True)]))
