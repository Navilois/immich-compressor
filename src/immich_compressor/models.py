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

    # `checksum` arrives as {"type":"Buffer","data":[...]} — we never need it, so it is
    # deliberately not modelled (extra="ignore" drops it).


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


TERMINAL_STATES: frozenset[JobState] = frozenset(
    {JobState.DONE, JobState.SKIPPED, JobState.FAILED}
)


class Job(BaseModel):
    """A row of the ``jobs`` table."""

    model_config = ConfigDict(extra="forbid")

    source_asset_id: str
    state: JobState = JobState.QUEUED
    skip_reason: SkipReason | None = None
    new_asset_id: str | None = None
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
