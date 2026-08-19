"""Configuration model.

Layering, lowest priority first:

1. defaults declared here
2. ``config.yaml`` (path from ``COMPRESSOR_CONFIG``, default ``./config.yaml``)
3. process environment (``IMMICH__API_KEY``, ``BEHAVIOR__DRY_RUN``, ...)
4. arguments passed to ``Settings(...)`` directly — the programmatic escape hatch,
   used by the tests; :func:`load_settings` never uses it.

The file is fed in through a settings *source* rather than as init arguments, because
init has the highest precedence in pydantic-settings. Passing the YAML there made it
outrank the environment, so every key written in ``config.yaml`` silently ignored its
``BEHAVIOR__*`` override — exactly the keys anyone would want to override.

Secrets (``immich.api_key``, ``webhook.token``) are *only* read from the environment.
Putting them in the YAML file is rejected at startup so they cannot leak into a
repository or an image layer.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

AssetType = Literal["IMAGE", "VIDEO", "AUDIO", "OTHER"]
HardwareMode = Literal["auto", "cpu", "qsv", "vaapi", "nvenc"]
QualityLevel = Literal["balanced", "higher", "smaller"]

# Placeholders a preset command template must contain.
INPUT_PLACEHOLDER = "{input}"
OUTPUT_PLACEHOLDER = "{output}"

# Suffixes ffmpeg uses for encoders that need a GPU.
HARDWARE_ENCODER_SUFFIXES = ("_qsv", "_vaapi", "_nvenc")
DEFAULT_RENDER_NODE = "/dev/dri/renderD128"

# Shell syntax a preset cannot use, because commands are executed directly, without a shell.
#
# The distinction is what matters here. A shell separates control operators into words of
# their own, so a genuine `ffmpeg ... | tee log` leaves "|" as a standalone token after
# shlex.split. A "|" *inside* a token is ordinary argument text: ffmpeg's filter syntax
# uses it for format alternations (`-vf format=nv12|vaapi,hwupload`) and comparison
# operators turn up in filter expressions. Testing the raw command string rejected those
# too, which made every such preset impossible to load at all.
SHELL_CONTROL_TOKENS = frozenset({"|", "||", "&", "&&", ";", ";;", "|&"})
# Redirections may be written attached to their target (`>log`, `2>&1`), so they are
# recognised by shape instead of by equality.
SHELL_REDIRECT_RE = re.compile(r"^\d*[<>]")
# Command substitution is never split off into a word, so it stays a substring test.
SHELL_SUBSTITUTIONS = ("`", "$(")


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

    # Quality target for the *generated* presets, mapped per encoder to the right
    # CRF / -global_quality / -cq number. Ignored when `presets:` is written by hand.
    # "balanced" reproduces exactly what this project shipped before the catalog existed.
    quality: QualityLevel = "balanced"

    min_size_bytes: int = Field(default=20 * 1024 * 1024, ge=0)
    max_ratio: float = Field(default=0.6, gt=0, le=1.0)
    enabled_types: list[AssetType] = Field(default_factory=lambda: ["VIDEO"])
    skip_if_named_people: bool = True

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
        return self


class HardwareSettings(BaseModel):
    """Which encoder to use. The default is "work it out for me"."""

    model_config = {"extra": "forbid"}

    # "auto"  detect the best encoder this machine can actually run (see hardware.py)
    # "cpu"   never consider a GPU
    # "qsv" / "vaapi" / "nvenc"  pin one hardware encoder; if it fails its one-frame
    #         test encode the service still falls back to the CPU preset rather than
    #         refusing to start
    mode: HardwareMode = "auto"
    # "auto", or a specific DRM render node such as /dev/dri/renderD129 on a box with
    # more than one GPU.
    render_node: str = "auto"


class Preset(BaseModel):
    """A single encoder recipe."""

    model_config = {"extra": "forbid", "populate_by_name": True}

    name: str
    match_type: AssetType = Field(alias="type")
    cmd: str
    suffix: str
    # Copy EXIF/XMP from the source onto the output with exiftool after encoding.
    # Required for stills; ffmpeg's -map_metadata already handles containers.
    exiftool_copy: bool = False
    # Keep the source's EXIF Orientation out of that copy and pin the output to 1.
    # Only correct when the command normalises the pixels itself (`convert -auto-orient`),
    # otherwise the image ends up rotated twice.
    normalize_orientation: bool = False
    timeout_s: float = Field(default=3600.0, gt=0)

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
        for token in argv:
            if token in SHELL_CONTROL_TOKENS or SHELL_REDIRECT_RE.match(token):
                raise ConfigError(
                    f"preset {self.name!r}: {token!r} is not supported — commands run without a shell"
                )
        for meta in SHELL_SUBSTITUTIONS:
            if meta in self.cmd:
                raise ConfigError(
                    f"preset {self.name!r}: {meta!r} is not supported — commands run without a shell"
                )
        if not self.suffix.startswith("."):
            raise ConfigError(f"preset {self.name!r}: suffix must start with a dot")
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
        return self._value_after(("-qsv_device", "-vaapi_device", "-hwaccel_device")) or (DEFAULT_RENDER_NODE)

    def _value_after(self, flags: tuple[str, ...], *, endswith: tuple[str, ...] | None = None) -> str | None:
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


# The parsed config.yaml, handed to the settings source below. Scoped to the context so a
# nested or concurrent load cannot leak its file into another one.
_yaml_values: ContextVar[dict[str, Any] | None] = ContextVar("_yaml_values", default=None)


class _YamlSource(PydanticBaseSettingsSource):
    """Feeds the parsed ``config.yaml`` in as a source, below the environment."""

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        # Not used: __call__ supplies the whole mapping in one go.
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return _yaml_values.get() or {}


class Settings(BaseSettings):
    """Root settings object."""

    model_config = SettingsConfigDict(
        env_nested_delimiter="__",
        extra="forbid",
        env_file=None,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Highest priority first. The YAML sits *below* the environment, on purpose."""
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _YamlSource(settings_cls),
            file_secret_settings,
        )

    immich: ImmichSettings = Field(default_factory=ImmichSettings)
    webhook: WebhookSettings = Field(default_factory=WebhookSettings)
    behavior: BehaviorSettings = Field(default_factory=BehaviorSettings)
    hardware: HardwareSettings = Field(default_factory=HardwareSettings)
    # Written by hand, this always wins. Left empty, the presets are generated from
    # the detected hardware — see `immich_compressor.hardware`.
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
        # Only meaningful once presets exist. An empty list means "generate them from the
        # detected hardware", and `load_settings` fills it in before anyone sees it.
        if self.presets:
            for asset_type in self.behavior.enabled_types:
                if not any(preset.match_type == asset_type for preset in self.presets):
                    raise ConfigError(f"enabled_types contains {asset_type} but no preset matches it")
        return self

    def preset_for(self, asset_type: str) -> Preset | None:
        """First preset whose ``type`` matches, or ``None``."""
        return next((preset for preset in self.presets if preset.match_type == asset_type), None)


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
        if isinstance(match, dict) and "type" in match:
            item.setdefault("type", match["type"])
        if "type" not in item:
            raise ConfigError(f"preset {item.get('name')!r}: missing match.type")
    raw["presets"] = items


def load_settings(
    config_path: Path | None = None,
    *,
    require_secrets: bool = True,
    autodetect: bool = True,
) -> Settings:
    """Read YAML + environment into a validated :class:`Settings`.

    Raises :class:`ConfigError` on any structural problem so a misconfigured deployment
    dies at startup rather than halfway through a job.

    ``require_secrets=False`` is for the commands that inspect the machine rather than the
    server (``hardware``): they must work before an API key exists. ``autodetect=False``
    returns the configuration exactly as written, without generating presets — which is
    what the ``hardware`` command wants, because it runs detection itself and would
    otherwise probe the GPU twice.
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

    token = _yaml_values.set(raw)
    try:
        settings = Settings()
    except ConfigError:
        raise
    except Exception as exc:  # pydantic ValidationError and friends
        raise ConfigError(f"invalid configuration: {exc}") from exc
    finally:
        _yaml_values.reset(token)

    if require_secrets:
        if not settings.immich.api_key.get_secret_value():
            raise ConfigError("IMMICH__API_KEY is not set")
        if not settings.webhook.token.get_secret_value():
            raise ConfigError("WEBHOOK__TOKEN is not set")
    if not autodetect:
        return settings
    return resolve_hardware(settings)


def resolve_hardware(settings: Settings) -> Settings:
    """Fill in generated presets when the configuration did not write any by hand.

    Imported lazily: ``hardware`` needs ``Preset`` and ``Settings`` from this module, so a
    module-level import here would be circular. Nothing is detected when ``presets:`` is
    written out, which is what keeps an upgrade from a hand-written 1.0.0 config a no-op.
    """
    if settings.presets:
        return settings

    from .hardware import apply_to_settings

    try:
        resolved, report = apply_to_settings(settings)
    except ValueError as exc:
        # build_presets refuses an asset type the catalog has no recipe for.
        raise ConfigError(str(exc)) from exc
    logger.info("%s", report.summary_line())
    for candidate in report.rejected:
        logger.info("  not using %s: %s", candidate.where(), candidate.reason)
    return resolved


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
