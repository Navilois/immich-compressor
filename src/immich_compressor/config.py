"""Configuration model.

Layering, lowest priority first:

1. defaults declared here
2. ``config.yaml`` (path from ``COMPRESSOR_CONFIG``, default ``./config.yaml``)
3. process environment (``IMMICH__API_KEY``, ``BEHAVIOR__DRY_RUN``, ...)

Secrets (``immich.api_key``, ``webhook.token``) are *only* read from the environment.
Putting them in the YAML file is rejected at startup so they cannot leak into a
repository or an image layer.
"""

from __future__ import annotations

import logging
import os
import shlex
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AssetType = Literal["IMAGE", "VIDEO", "AUDIO", "OTHER"]

# Placeholders a preset command template must contain.
INPUT_PLACEHOLDER = "{input}"
OUTPUT_PLACEHOLDER = "{output}"

# Suffixes ffmpeg uses for encoders that need a GPU.
HARDWARE_ENCODER_SUFFIXES = ("_qsv", "_vaapi", "_nvenc")
DEFAULT_RENDER_NODE = "/dev/dri/renderD128"


logger = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """Raised when the configuration is structurally invalid (fail fast at startup)."""


class ImmichSettings(BaseModel):
    """Connection details for the Immich server."""

    model_config = {"extra": "forbid"}

    base_url: str = "http://immich-server:2283/api"
    api_key: SecretStr = SecretStr("")
    timeout_s: float = 120.0
    connect_timeout_s: float = 10.0

    @model_validator(mode="after")
    def _normalise(self) -> Self:
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))
        return self


class WebhookSettings(BaseModel):
    """Shared-secret configuration for the inbound webhook endpoint."""

    model_config = {"extra": "forbid"}

    token: SecretStr = SecretStr("")
    header_name: str = "X-Compressor-Token"


class BehaviorSettings(BaseModel):
    """Everything that decides whether and how an asset gets touched."""

    model_config = {"extra": "forbid"}

    # Shipping defaults are deliberately inert: nothing is uploaded, nothing is deleted.
    dry_run: bool = True
    trash_original: bool = False

    # How the original is removed once the replacement has been verified.
    #   "trash"     — soft delete; recoverable from Immich's trash, `restore` undoes it.
    #   "permanent" — `DELETE /assets` with force=true. Verified against a live v3.1.0
    #                 instance: the asset vanishes from the database and its files are
    #                 unlinked immediately, bypassing the trash entirely. There is no
    #                 undo other than a backup of Postgres plus the upload directory.
    delete_mode: Literal["trash", "permanent"] = "trash"

    # 0 means "as soon as the verification chain passes", inline in the job rather than
    # on the sweeper's next pass.
    retention_days: int = Field(default=7, ge=0)
    initial_delay_seconds: int = Field(default=300, ge=0)
    concurrency: int = Field(default=1, ge=1, le=4)
    max_attempts: int = Field(default=3, ge=1)
    poll_interval_seconds: float = Field(default=5.0, gt=0)

    # How many bytes a job has to actually save to be worth an asset lifecycle — a new
    # database row, thumbnails, a smart-search embedding, face detection, OCR and a
    # timeline entry, all of it permanent. Replaces the old `min_size_bytes`, which
    # guessed from the input size instead of measuring the outcome.
    #
    # It doubles as the pre-download filter, and that part needs no tuning at all: a file
    # cannot save more bytes than it has, so rejecting `fileSizeInByte < min_savings_bytes`
    # before the download is provably free of false negatives.
    min_savings_bytes: int = Field(default=1024 * 1024, ge=0)
    max_ratio: float = Field(default=0.6, gt=0, le=1.0)
    enabled_types: list[AssetType] = Field(default_factory=lambda: ["VIDEO"])
    skip_if_named_people: bool = True

    # What happens when the post-encode metadata diff finds a tag the copy did not carry.
    #   "strict" — the job fails, the original is never touched.
    #   "warn"   — the difference is logged and the job continues.
    # "warn" exists for the first days on unknown camera material, where an unlisted
    # MakerNotes quirk would otherwise block every image. It is only defensible while a
    # delete can still be undone — see `_validate_delete_mode`.
    metadata_verify: Literal["strict", "warn"] = "strict"

    # How long to wait for Immich's metadata-extraction job on a freshly uploaded asset
    # before writing description/rating/GPS. Extraction overwrites those fields, so
    # writing too early silently loses them.
    post_upload_settle_s: float = Field(default=30.0, ge=0)

    # Sanity gate tolerances.
    duration_tolerance_s: float = Field(default=0.5, ge=0)
    require_same_resolution: bool = True
    require_date_time_original: bool = True

    work_dir: Path = Path("/var/tmp/immich-compressor")  # noqa: S108 - configurable, not a fixed tmp path
    # Refuse to start a job unless this much free space is available in work_dir,
    # expressed as a multiple of the source file size.
    free_space_factor: float = Field(default=3.0, ge=1.0)

    compressed_marker: str = ".cmp"
    metadata_key: str = "compressor"

    @model_validator(mode="after")
    def _validate_delete_mode(self) -> Self:
        """Permanent deletion is irreversible — refuse the contradictory combinations.

        Both guards catch a configuration that cannot mean what it says: deleting an
        original for good while `trash_original` says not to touch it at all, or while
        `dry_run` promises that nothing is mutated.
        """
        if self.delete_mode != "permanent":
            return self
        if not self.trash_original:
            raise ConfigError(
                "behavior.delete_mode: 'permanent' needs trash_original: true — "
                "without it originals are never removed at all"
            )
        if self.dry_run:
            raise ConfigError(
                "behavior.delete_mode: 'permanent' is incompatible with dry_run: true — "
                "a dry run must not delete anything"
            )
        if self.metadata_verify != "strict":
            # The cost of the two failure directions is not symmetric. A gate that fires
            # wrongly costs a failed job and leaves the original alone; a gate that stays
            # silent wrongly costs the metadata *and* the original, with no rollback but a
            # Postgres backup. Warning about a loss that can no longer be undone is not a
            # learning phase, it is a log entry.
            raise ConfigError(
                "behavior.metadata_verify: 'warn' is incompatible with "
                "delete_mode: 'permanent' — a warning cannot undo a force-deleted original"
            )
        return self


class Preset(BaseModel):
    """A single encoder recipe."""

    model_config = {"extra": "forbid", "populate_by_name": True}

    name: str
    match_type: AssetType = Field(alias="type")
    # File extensions this preset accepts, e.g. ``[".jpg", ".jpeg"]``. Empty means "any",
    # which is what the video presets want.
    #
    # An allowlist, deliberately, because Immich files *everything* under type IMAGE:
    # JPEG, HEIC, PNG, GIF, WebP, TIFF — and RAW. ImageMagick in this image reads
    # DNG/CR2/CR3/NEF/ARW through libraw, so without this list a RAW would be developed
    # into an 8-bit JPEG, pass every sanity check, and have its original deleted.
    extensions: list[str] = Field(default_factory=list)
    cmd: str
    suffix: str
    # Copy EXIF/XMP from the source onto the output with exiftool after encoding.
    # Required for stills; ffmpeg's -map_metadata already handles containers.
    exiftool_copy: bool = False
    # Keep the source's EXIF Orientation out of that copy and pin the output to 1.
    # Only correct when the command normalises the pixels itself (`magick -auto-orient`),
    # otherwise the image ends up rotated twice.
    normalize_orientation: bool = False
    timeout_s: float = Field(default=3600.0, gt=0)

    # Per-preset overrides of the sanity gate. ``None`` means "use the behavior value".
    #
    # These exist because video and stills have opposite economics. A video encode costs
    # minutes, so demanding a strong size ratio is right. A still costs about a second,
    # and then the ratio is the wrong axis entirely: ratio 0.75 on a 12 MB photo saves
    # 3 MB, ratio 0.60 on a 371 KB photo saves 147 KB. For stills `max_ratio` is therefore
    # only a "something went badly wrong" net, and `min_savings_bytes` does the real work.
    max_ratio: float | None = Field(default=None, gt=0, le=1.0)
    min_savings_bytes: int | None = Field(default=None, ge=0)
    require_date_time_original: bool | None = None
    # Skip a still whose source JPEG quality is at or below this value. Re-encoding an
    # already heavily compressed image buys a second generation of quantisation error and
    # usually a *larger* file. ``None`` disables the check.
    min_source_quality: int | None = Field(default=None, ge=1, le=100)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if INPUT_PLACEHOLDER not in self.cmd:
            raise ConfigError(f"preset {self.name!r}: command is missing {INPUT_PLACEHOLDER}")
        if OUTPUT_PLACEHOLDER not in self.cmd:
            raise ConfigError(f"preset {self.name!r}: command is missing {OUTPUT_PLACEHOLDER}")
        try:
            argv = shlex.split(self.cmd)
        except ValueError as exc:
            raise ConfigError(f"preset {self.name!r}: command is not shell-splittable: {exc}") from exc
        if not argv:
            raise ConfigError(f"preset {self.name!r}: command is empty")
        for meta in ("|", "&&", ";", ">", "<", "`", "$("):
            if meta in self.cmd:
                raise ConfigError(
                    f"preset {self.name!r}: {meta!r} is not supported — commands run without a shell"
                )
        if not self.suffix.startswith("."):
            raise ConfigError(f"preset {self.name!r}: suffix must start with a dot")
        normalised: list[str] = []
        for extension in self.extensions:
            if not extension.startswith("."):
                raise ConfigError(
                    f"preset {self.name!r}: extension {extension!r} must start with a dot"
                )
            normalised.append(extension.lower())
        object.__setattr__(self, "extensions", normalised)
        if self.normalize_orientation:
            if not self.exiftool_copy:
                raise ConfigError(
                    f"preset {self.name!r}: normalize_orientation needs exiftool_copy — "
                    "without the metadata copy there is no orientation tag to correct"
                )
            if "-auto-orient" not in self.cmd:
                raise ConfigError(
                    f"preset {self.name!r}: normalize_orientation requires the command to "
                    "normalise the pixels itself — add -auto-orient"
                )
        return self

    def accepts(self, filename: str) -> bool:
        """Whether this preset handles ``filename``. An empty ``extensions`` accepts all."""
        if not self.extensions:
            return True
        return Path(filename).suffix.lower() in self.extensions

    def effective_max_ratio(self, behavior: BehaviorSettings) -> float:
        return self.max_ratio if self.max_ratio is not None else behavior.max_ratio

    def effective_min_savings_bytes(self, behavior: BehaviorSettings) -> int:
        if self.min_savings_bytes is not None:
            return self.min_savings_bytes
        return behavior.min_savings_bytes

    def effective_require_date_time_original(self, behavior: BehaviorSettings) -> bool:
        if self.require_date_time_original is not None:
            return self.require_date_time_original
        return behavior.require_date_time_original

    @property
    def hardware_encoder(self) -> str | None:
        """The GPU encoder this preset asks for, e.g. ``hevc_qsv`` — ``None`` for CPU.

        Read from the token after ``-c:v`` rather than by scanning the whole command, so a
        path that happens to end in ``_qsv`` cannot be mistaken for an encoder.
        """
        return self._value_after(("-c:v", "-codec:v", "-vcodec"), endswith=HARDWARE_ENCODER_SUFFIXES)

    @property
    def render_node(self) -> str:
        """The DRM device the preset pins itself to, or the conventional default."""
        return self._value_after(("-qsv_device", "-vaapi_device", "-hwaccel_device")) or (
            DEFAULT_RENDER_NODE
        )

    def _value_after(
        self, flags: tuple[str, ...], *, endswith: tuple[str, ...] | None = None
    ) -> str | None:
        tokens = shlex.split(self.cmd)
        for index, token in enumerate(tokens[:-1]):
            if token in flags:
                value = tokens[index + 1]
                if endswith is None or value.endswith(endswith):
                    return value
        return None

    def argv(self, input_path: Path, output_path: Path) -> list[str]:
        """Render the command template into an argv list. Never goes through a shell."""
        rendered: list[str] = []
        for token in shlex.split(self.cmd):
            rendered.append(
                token.replace(INPUT_PLACEHOLDER, str(input_path)).replace(
                    OUTPUT_PLACEHOLDER, str(output_path)
                )
            )
        return rendered


class Settings(BaseSettings):
    """Root settings object."""

    model_config = SettingsConfigDict(
        env_nested_delimiter="__",
        extra="forbid",
        env_file=None,
    )

    immich: ImmichSettings = Field(default_factory=ImmichSettings)
    webhook: WebhookSettings = Field(default_factory=WebhookSettings)
    behavior: BehaviorSettings = Field(default_factory=BehaviorSettings)
    presets: list[Preset] = Field(default_factory=list)

    database_path: Path = Path("/var/lib/immich-compressor/state.db")
    listen_host: str = "0.0.0.0"  # noqa: S104 - inside a container; publish selectively at the host
    listen_port: int = 8080
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _validate(self) -> Self:
        names = [preset.name for preset in self.presets]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ConfigError(f"duplicate preset names: {sorted(duplicates)}")
        for asset_type in self.behavior.enabled_types:
            if not self.type_is_covered(asset_type):
                raise ConfigError(f"enabled_types contains {asset_type} but no preset matches it")
        return self

    def preset_for(self, asset_type: str, filename: str | None = None) -> Preset | None:
        """First preset whose type — and, when given, file extension — matches.

        ``filename`` is optional so callers that only ask "is this type covered at all?"
        (the startup validation, the ``encode`` CLI command) keep working unchanged.
        """
        for preset in self.presets:
            if preset.match_type != asset_type:
                continue
            if filename is not None and not preset.accepts(filename):
                continue
            return preset
        return None

    def type_is_covered(self, asset_type: str) -> bool:
        """Whether any preset handles ``asset_type``, regardless of extension."""
        return any(preset.match_type == asset_type for preset in self.presets)


def _forbid_secrets_in_file(raw: dict[str, Any]) -> None:
    immich = raw.get("immich")
    if isinstance(immich, dict) and immich.get("api_key"):
        raise ConfigError("immich.api_key must not be set in config.yaml — use IMMICH__API_KEY")
    webhook = raw.get("webhook")
    if isinstance(webhook, dict) and webhook.get("token"):
        raise ConfigError("webhook.token must not be set in config.yaml — use WEBHOOK__TOKEN")


def _normalise_presets(raw: dict[str, Any]) -> None:
    """Accept the mapping form ``presets: {name: {match: {type: ...}, ...}}`` from the plan
    as well as a plain list, and flatten both into the list-of-``Preset`` shape."""
    presets = raw.get("presets")
    if presets is None:
        return
    items: list[dict[str, Any]] = []
    if isinstance(presets, dict):
        for name, body in presets.items():
            if not isinstance(body, dict):
                raise ConfigError(f"preset {name!r}: expected a mapping")
            items.append({"name": name, **body})
    elif isinstance(presets, list):
        items = [dict(item) for item in presets]
    else:
        raise ConfigError("presets must be a mapping or a list")

    for item in items:
        match = item.pop("match", None)
        if isinstance(match, dict):
            if "type" in match:
                item.setdefault("type", match["type"])
            if "extensions" in match:
                item.setdefault("extensions", match["extensions"])
        if "type" not in item:
            raise ConfigError(f"preset {item.get('name')!r}: missing match.type")
    raw["presets"] = items


def load_settings(config_path: Path | None = None) -> Settings:
    """Read YAML + environment into a validated :class:`Settings`.

    Raises :class:`ConfigError` on any structural problem so a misconfigured deployment
    dies at startup rather than halfway through a job.
    """
    path = config_path or Path(os.environ.get("COMPRESSOR_CONFIG", "config.yaml"))
    raw: dict[str, Any] = {}
    if path.is_file():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"{path}: top level must be a mapping")
        raw = loaded

    _forbid_secrets_in_file(raw)
    _normalise_presets(raw)

    try:
        settings = Settings(**raw)
    except ConfigError:
        raise
    except Exception as exc:  # pydantic ValidationError and friends
        raise ConfigError(f"invalid configuration: {exc}") from exc

    if not settings.immich.api_key.get_secret_value():
        raise ConfigError("IMMICH__API_KEY is not set")
    if not settings.webhook.token.get_secret_value():
        raise ConfigError("WEBHOOK__TOKEN is not set")
    return settings


def warn_about_permanent_deletion(behavior: BehaviorSettings) -> None:
    """Say it once, loudly, at startup — the setting has no undo and leaves no trace.

    Called from the app lifespan rather than from :func:`load_settings`, because the
    config is loaded before logging is configured and the line would otherwise go out
    through the last-resort handler.
    """
    if behavior.delete_mode != "permanent":
        return
    logger.warning(
        "!!! behavior.delete_mode = 'permanent': originals are deleted with force=true "
        "%s, bypassing the trash. They cannot be restored — the only rollback is a "
        "backup of Postgres and the upload directory. !!!",
        "immediately after the replacement is verified"
        if behavior.retention_days == 0
        else f"{behavior.retention_days} day(s) after the replacement is verified",
    )
