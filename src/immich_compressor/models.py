"""Pydantic models for the Immich webhook payload and the REST DTOs we touch.

Every field here was checked against a payload captured from a live Immich v3.1.0
instance; the captures live in ``tests/fixtures/``. Unknown fields are tolerated
(``extra="ignore"``) so a server-side addition cannot break the ingest path.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, TypeVar

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

_T = TypeVar("_T")


def _none_to_empty_list(value: object) -> object:
    """Immich sends ``null`` — not ``[]`` — for empty collections.

    A pydantic default only fills in a *missing* key, so an explicit ``null`` would
    otherwise fail validation. This cost a full debugging round: the workflow webhook
    silently returned 422 for every asset without tags.
    """
    return [] if value is None else value


def _none_to_false(value: object) -> object:
    return False if value is None else value


def _none_to_empty_str(value: object) -> object:
    return "" if value is None else value


def _none_to_empty_dict(value: object) -> object:
    return {} if value is None else value


NullableList = Annotated[list[_T], BeforeValidator(_none_to_empty_list)]
NullableBool = Annotated[bool, BeforeValidator(_none_to_false)]
NullableStr = Annotated[str, BeforeValidator(_none_to_empty_str)]
NullableModel = Annotated[_T, BeforeValidator(_none_to_empty_dict)]

# --------------------------------------------------------------------------------------
# Webhook payload
# --------------------------------------------------------------------------------------


class ExifInfo(BaseModel):
    """``data.asset.exifInfo`` — all fields optional, Immich nulls out what it lacks."""

    model_config = ConfigDict(extra="ignore")

    make: str | None = None
    model: str | None = None
    orientation: str | None = None
    date_time_original: str | None = Field(default=None, alias="dateTimeOriginal")
    modify_date: str | None = Field(default=None, alias="modifyDate")
    exif_image_width: int | None = Field(default=None, alias="exifImageWidth")
    exif_image_height: int | None = Field(default=None, alias="exifImageHeight")
    file_size_in_byte: int | None = Field(default=None, alias="fileSizeInByte")
    lens_model: str | None = Field(default=None, alias="lensModel")
    f_number: float | None = Field(default=None, alias="fNumber")
    focal_length: float | None = Field(default=None, alias="focalLength")
    iso: int | None = None
    latitude: float | None = None
    longitude: float | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    description: str | None = None
    fps: float | None = None
    exposure_time: str | None = Field(default=None, alias="exposureTime")
    live_photo_cid: str | None = Field(default=None, alias="livePhotoCID")
    time_zone: str | None = Field(default=None, alias="timeZone")
    projection_type: str | None = Field(default=None, alias="projectionType")
    profile_description: str | None = Field(default=None, alias="profileDescription")
    colorspace: str | None = None
    bits_per_sample: int | None = Field(default=None, alias="bitsPerSample")
    rating: int | None = None
    # Tag *names*, not IDs — verified against the live payload. Arrives as `null` when
    # the asset has no tags, hence NullableList.
    tags: NullableList[str] = Field(default_factory=list)


class WebhookAsset(BaseModel):
    """``data.asset`` — the ``AssetV1`` shape the plugin core emits."""

    model_config = ConfigDict(extra="ignore")

    id: str
    owner_id: str | None = Field(default=None, alias="ownerId")
    type: str
    original_path: str | None = Field(default=None, alias="originalPath")
    original_file_name: NullableStr = Field(default="asset", alias="originalFileName")
    # When Immich created the database *row* — that is, when the asset was uploaded.
    # Deliberately distinct from `fileCreatedAt`, which is the capture date read out of
    # EXIF: the captured v3.1.0 payload for a photo shot in 2024 and uploaded in 2026
    # carries createdAt=2026-08-07, fileCreatedAt=2024-06-15. That gap is the only thing
    # in the payload that separates a fresh upload from a bulk re-trigger of the
    # `AssetMetadataExtraction` workflow, and `check_ingest_guards` reads it.
    created_at: datetime | None = Field(default=None, alias="createdAt")
    file_created_at: datetime | None = Field(default=None, alias="fileCreatedAt")
    file_modified_at: datetime | None = Field(default=None, alias="fileModifiedAt")
    local_date_time: datetime | None = Field(default=None, alias="localDateTime")
    is_favorite: NullableBool = Field(default=False, alias="isFavorite")
    is_offline: NullableBool = Field(default=False, alias="isOffline")
    is_external: NullableBool = Field(default=False, alias="isExternal")
    is_edited: NullableBool = Field(default=False, alias="isEdited")
    library_id: str | None = Field(default=None, alias="libraryId")
    live_photo_video_id: str | None = Field(default=None, alias="livePhotoVideoId")
    stack_id: str | None = Field(default=None, alias="stackId")
    duplicate_id: str | None = Field(default=None, alias="duplicateId")
    deleted_at: datetime | None = Field(default=None, alias="deletedAt")
    visibility: str | None = None
    status: str | None = None
    # Milliseconds. Verified: the live payload sends 20000 for a 20 s clip, and
    # POST /assets expects the same integer-milliseconds unit.
    duration: int | None = None
    # `exifInfo` is null until metadata extraction has run.
    exif_info: NullableModel[ExifInfo] = Field(default_factory=ExifInfo, alias="exifInfo")

    # The *webhook* serialises `checksum` as the raw Postgres bytea,
    # {"type":"Buffer","data":[...]}, which is useless to us — `extra="ignore"` drops it.
    # `GET /assets/{id}` is different: there it is a base64 SHA-1 string, and
    # `AssetDetail.checksum` models it because the delete gate compares against it.


class WebhookData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    asset: WebhookAsset


class WebhookPayload(BaseModel):
    """Top level of the webhook body: ``{type, trigger, data}``."""

    model_config = ConfigDict(extra="ignore")

    type: str
    trigger: str
    data: WebhookData


# --------------------------------------------------------------------------------------
# Immich REST DTOs (only the parts we consume)
# --------------------------------------------------------------------------------------


class AssetMediaStatus(StrEnum):
    CREATED = "created"
    REPLACED = "replaced"
    DUPLICATE = "duplicate"


class AssetMediaResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    status: AssetMediaStatus


class MetadataItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str
    value: dict[str, Any]


class PersonRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: NullableStr = ""


class TagRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    value: NullableStr = ""
    name: NullableStr = ""


class AssetDetail(BaseModel):
    """``GET /assets/{id}`` — only the fields the guards and E2E assertions need."""

    model_config = ConfigDict(extra="ignore")

    id: str
    type: str
    is_favorite: NullableBool = Field(default=False, alias="isFavorite")
    is_trashed: NullableBool = Field(default=False, alias="isTrashed")
    visibility: str | None = None
    duration: int | None = None
    original_file_name: NullableStr = Field(default="", alias="originalFileName")
    # Base64-encoded SHA-1 of the stored file, e.g. "02MpaJkpzGHNbGwxWtencVNK7uY=".
    # Verified against a live v3.1.0 instance; the delete gate compares it with the
    # checksum the encoder computed for the file it uploaded.
    checksum: str | None = None
    people: NullableList[PersonRef] = Field(default_factory=list)
    tags: NullableList[TagRef] = Field(default_factory=list)
    # `exifInfo` is null until metadata extraction has run.
    exif_info: NullableModel[ExifInfo] = Field(default_factory=ExifInfo, alias="exifInfo")

    def named_people(self) -> list[str]:
        return [person.name for person in self.people if person.name.strip()]


# --------------------------------------------------------------------------------------
# Job state
# --------------------------------------------------------------------------------------


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    UPLOADED = "uploaded"
    LINKED = "linked"
    PENDING_DELETE = "pending_delete"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"


class SkipReason(StrEnum):
    ALREADY_COMPRESSED = "already_compressed"
    TOO_SMALL = "too_small"
    WRONG_TYPE = "wrong_type"
    NO_GAIN = "no_gain"
    DUPLICATE = "duplicate"
    NAMED_PEOPLE = "named_people"
    EDITED = "edited"
    EXTERNAL_LIBRARY = "external_library"
    LIVE_PHOTO = "live_photo"
    LOCKED = "locked"
    TRASHED = "trashed"
    NO_PRESET = "no_preset"
    DRY_RUN = "dry_run"
    # The asset type is enabled, but no preset accepts this file extension. Distinct from
    # NO_PRESET, which means no preset covers the type at all.
    UNSUPPORTED_FORMAT = "unsupported_format"
    # A still that carries a second payload the encoder would silently drop — a Samsung or
    # Google motion photo, whose video hangs behind the JPEG's end-of-image marker.
    EMBEDDED_MEDIA = "embedded_media"
    # The source is already compressed at or below the preset's target quality, so a
    # re-encode would cost a generation of quantisation error for no gain.
    SOURCE_QUALITY = "source_quality"


class RejectReason(StrEnum):
    """Why a webhook was refused at ingest, before any job row was written.

    Deliberately not a :class:`SkipReason`. A skip is a verdict recorded *against a job*,
    which makes the asset permanently immune to a later replay — including a replay from
    `backfill`, which enqueues through the same `ON CONFLICT DO NOTHING`. A rejection
    writes nothing at all, so the asset stays eligible for the intentional path.
    """

    # Older than `behavior.max_asset_age_hours`: this is a re-trigger for an asset that
    # has been in the library for a while, not the webhook for a new upload.
    TOO_OLD = "too_old"
    # The payload carried no `createdAt`, so freshness cannot be established either way.
    NO_CREATED_AT = "no_created_at"
    # The surge breaker has latched. Nothing is queued or processed until it is cleared.
    PAUSED = "paused"


class BackfillVerdict(StrEnum):
    """Inventory verdicts that are *not* :class:`SkipReason`s.

    The ``verdict`` column of ``backfill_candidates`` holds a :class:`SkipReason` for
    everything the guards decide from the scanned payload, and one of these two for the
    states that only exist between a scan and a queue run. Both are written by
    ``backfill run``, never by the pipeline, and neither ever reaches a job row.
    """

    # Gone by the time the queue run looked: deleted, or replaced from the webhook side
    # while the inventory sat there. An inventory is a snapshot, and this is how it ages.
    MISSING = "missing"
    # A job row already exists, so `store.enqueue` would be a no-op. Not an error — it is
    # what a scan of a library that is already half worked through looks like.
    ALREADY_KNOWN = "already_known"


TERMINAL_STATES: frozenset[JobState] = frozenset({JobState.DONE, JobState.SKIPPED, JobState.FAILED})


class PauseState(BaseModel):
    """The surge breaker's latch, as stored in the ``service_state`` table.

    Persisted rather than held in memory: restarting the container is the first thing an
    operator reaches for, and it must not be the thing that clears the latch.
    """

    model_config = ConfigDict(extra="ignore")

    reason: str
    since: datetime


class Job(BaseModel):
    """A row of the ``jobs`` table."""

    model_config = ConfigDict(extra="forbid")

    source_asset_id: str
    state: JobState = JobState.QUEUED
    skip_reason: SkipReason | None = None
    # Which worker lane owns this job. NULL on rows written before the column existed.
    asset_type: str | None = None
    new_asset_id: str | None = None
    # Base64 SHA-1 of the file we uploaded, kept so the sweeper can still verify the
    # replacement hours later, long after the local output file is gone.
    new_checksum: str | None = None
    orig_bytes: int | None = None
    new_bytes: int | None = None
    ratio: float | None = None
    attempts: int = 0
    last_error: str | None = None
    payload: str = "{}"
    created_at: datetime
    updated_at: datetime
    run_after: datetime
    delete_after: datetime | None = None


class BackfillCandidate(BaseModel):
    """A row of the ``backfill_candidates`` table — one asset a scan looked at.

    ``verdict`` is ``None`` exactly when the guards would let the asset through; that is
    what makes a row a candidate. Rejected rows are kept because they are what
    ``backfill status`` counts, but they carry no ``payload``: the payload is the only
    expensive column and an asset the guards already refused is never enqueued.
    """

    model_config = ConfigDict(extra="forbid")

    asset_id: str
    asset_type: str
    size_bytes: int = 0
    filename: str = ""
    # A `SkipReason` or a `BackfillVerdict`, kept as a plain string: the two enums share
    # this column and a row written by a newer version must not fail to load on an older
    # one. Nothing branches on the value, it is counted and printed.
    verdict: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    scanned_at: datetime
    queued_at: datetime | None = None


class UpdateAssetFields(BaseModel):
    """Body for ``PUT /assets/{id}``. Only non-``None`` fields are sent."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    description: str | None = None
    # v3 rejects 0 with HTTP 400: valid values are -1 or 1..5 or null.
    rating: Literal[-1, 1, 2, 3, 4, 5] | None = None
    latitude: float | None = None
    longitude: float | None = None
    date_time_original: str | None = Field(default=None, serialization_alias="dateTimeOriginal")
    is_favorite: bool | None = Field(default=None, serialization_alias="isFavorite")
    visibility: str | None = None

    def to_body(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True, by_alias=True)

    def is_empty(self) -> bool:
        return not self.to_body()
