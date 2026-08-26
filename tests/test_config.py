"""Configuration must fail fast and must never accept secrets from the YAML file."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from immich_compressor.config import ConfigError, Preset, Settings, ShimSettings, load_settings

_PRESETS = """
presets:
  video-h265:
    match: { type: VIDEO }
    cmd: ffmpeg -i {input} -c:v libx265 {output}
    suffix: .mp4
"""

_MINIMAL = "\nbehavior:\n  enabled_types: [VIDEO]\n" + _PRESETS


def _with_behavior(**options: object) -> str:
    """A minimal config whose `behavior` block carries the given options."""
    lines = "".join(f"  {key}: {str(value).lower()}\n" for key, value in options.items())
    return f"\nbehavior:\n  enabled_types: [VIDEO]\n{lines}{_PRESETS}"


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_loads_mapping_style_presets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMMICH__API_KEY", "key")
    monkeypatch.setenv("WEBHOOK__TOKEN", "tok")
    settings = load_settings(_write(tmp_path, _MINIMAL))
    assert [p.name for p in settings.presets] == ["video-h265"]
    assert settings.preset_for("VIDEO") is not None
    assert settings.preset_for("IMAGE") is None


def test_environment_beats_a_value_written_in_the_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file must lose against the environment, and used not to.

    config.yaml was handed to BaseSettings as init arguments, and init outranks every
    other source in pydantic-settings. Every key actually written in the file therefore
    ignored its override: `BEHAVIOR__DRY_RUN=false` against a file saying `dry_run: true`
    left the service in a dry run while the compose file, the README and the environment
    all said otherwise. Only keys *absent* from the file — the secrets — ever worked,
    which is why it went unnoticed.
    """
    monkeypatch.setenv("IMMICH__API_KEY", "key")
    monkeypatch.setenv("WEBHOOK__TOKEN", "tok")
    body = _with_behavior(dry_run=True, trash_original=False, max_ratio=0.6)

    assert load_settings(_write(tmp_path, body)).behavior.dry_run is True

    monkeypatch.setenv("BEHAVIOR__DRY_RUN", "false")
    settings = load_settings(_write(tmp_path, body))
    assert settings.behavior.dry_run is False
    # The neighbours in the same nested block must survive the override, not be replaced
    # wholesale by the single key the environment carries.
    assert settings.behavior.trash_original is False
    assert settings.behavior.max_ratio == 0.6
    assert [p.name for p in settings.presets] == ["video-h265"]


def test_shipping_defaults_are_inert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The delivered defaults must not upload and must not delete."""
    monkeypatch.setenv("IMMICH__API_KEY", "key")
    monkeypatch.setenv("WEBHOOK__TOKEN", "tok")
    settings = load_settings(_write(tmp_path, _MINIMAL))
    assert settings.behavior.dry_run is True
    assert settings.behavior.trash_original is False


def test_env_override_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMMICH__API_KEY", "key")
    monkeypatch.setenv("WEBHOOK__TOKEN", "tok")
    monkeypatch.setenv("BEHAVIOR__DRY_RUN", "false")
    settings = load_settings(_write(tmp_path, _MINIMAL))
    assert settings.behavior.dry_run is False


def test_api_key_in_file_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMMICH__API_KEY", "key")
    monkeypatch.setenv("WEBHOOK__TOKEN", "tok")
    body = _MINIMAL + "\nimmich:\n  api_key: oops-a-secret\n"
    with pytest.raises(ConfigError, match=r"must not be set in config\.yaml"):
        load_settings(_write(tmp_path, body))


def test_missing_api_key_fails_fast(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IMMICH__API_KEY", raising=False)
    monkeypatch.setenv("WEBHOOK__TOKEN", "tok")
    with pytest.raises(ConfigError, match="IMMICH__API_KEY"):
        load_settings(_write(tmp_path, _MINIMAL))


def test_enabled_type_without_preset_fails_fast(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMMICH__API_KEY", "key")
    monkeypatch.setenv("WEBHOOK__TOKEN", "tok")
    body = _MINIMAL.replace("enabled_types: [VIDEO]", "enabled_types: [VIDEO, IMAGE]")
    with pytest.raises(ConfigError, match="no preset matches"):
        load_settings(_write(tmp_path, body))


@pytest.mark.parametrize(
    "cmd",
    [
        "ffmpeg -i {input} -c:v libx265 out.mp4",  # no {output}
        "ffmpeg -c:v libx265 {output}",  # no {input}
        "ffmpeg -i {input} {output} | tee log",  # shell metacharacter
        "ffmpeg -i {input} && rm -rf / {output}",
        "ffmpeg -i {input} > {output}",
        "ffmpeg -i {input} $(whoami) {output}",
        "ffmpeg -i {input} {output} ; rm -rf /",  # command separator
        "ffmpeg -i {input} {output} >log",  # redirect attached to its target
        "ffmpeg -i {input} {output} 2>&1",  # file-descriptor redirect
    ],
)
def test_invalid_preset_commands_are_rejected(cmd: str) -> None:
    with pytest.raises(ConfigError):
        Preset(name="bad", type="VIDEO", cmd=cmd, suffix=".mp4")


@pytest.mark.parametrize(
    "cmd",
    [
        # The Gen9-11 VAAPI preset: '|' is ffmpeg's format-alternation syntax, not a pipe.
        # A raw-substring check rejected this outright, so the preset could never load.
        "ffmpeg -i {input} -vf format=nv12|vaapi,hwupload -c:v hevc_vaapi {output}",
        # Comparison operators inside a filter expression are argument text too.
        "ffmpeg -i {input} -vf crop=w=if(gt(a,1),iw,ih) {output}",
        "ffmpeg -i {input} -vf drawtext=text=a<b {output}",
    ],
)
def test_shell_metacharacters_inside_a_token_are_argument_text(cmd: str) -> None:
    """A metacharacter is shell syntax only where a shell would have split it off.

    Inside a token it is ordinary argument text, and rejecting it locks out legitimate
    ffmpeg commands.
    """
    preset = Preset(name="ok", type="VIDEO", cmd=cmd, suffix=".mp4")
    argv = preset.argv(Path("/tmp/in.mov"), Path("/tmp/out.mp4"))
    assert argv[0] == "ffmpeg"
    # The metacharacter never becomes an argument of its own — that is the whole point.
    assert not any(token in {"|", ">", "<"} for token in argv)


def test_suffix_must_start_with_dot() -> None:
    with pytest.raises(ConfigError, match="suffix"):
        Preset(name="bad", type="VIDEO", cmd="x {input} {output}", suffix="mp4")


def test_argv_renders_without_a_shell() -> None:
    preset = Preset(name="p", type="VIDEO", cmd="ffmpeg -i {input} -c copy {output}", suffix=".mp4")
    argv = preset.argv(Path("/tmp/in put.mov"), Path("/tmp/out.mp4"))
    # The space in the filename stays inside a single argv element — it can never be
    # re-split into another argument, let alone another command.
    assert argv == ["ffmpeg", "-i", "/tmp/in put.mov", "-c", "copy", "/tmp/out.mp4"]


def test_permanent_delete_mode_requires_trashing_to_be_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting for good while `trash_original` says "never touch it" is a contradiction."""
    monkeypatch.setenv("IMMICH__API_KEY", "key")
    monkeypatch.setenv("WEBHOOK__TOKEN", "tok")
    body = _with_behavior(dry_run=False, delete_mode="permanent")
    with pytest.raises(ConfigError, match=r"needs trash_original: true"):
        load_settings(_write(tmp_path, body))


def test_permanent_delete_mode_is_rejected_in_a_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry run promises that nothing is mutated — it must not delete anything."""
    monkeypatch.setenv("IMMICH__API_KEY", "key")
    monkeypatch.setenv("WEBHOOK__TOKEN", "tok")
    body = _with_behavior(dry_run=True, trash_original=True, delete_mode="permanent")
    with pytest.raises(ConfigError, match=r"incompatible with dry_run"):
        load_settings(_write(tmp_path, body))


def test_permanent_delete_mode_loads_when_it_is_coherent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMMICH__API_KEY", "key")
    monkeypatch.setenv("WEBHOOK__TOKEN", "tok")
    body = _with_behavior(dry_run=False, trash_original=True, retention_days=0, delete_mode="permanent")
    settings = load_settings(_write(tmp_path, body))
    assert settings.behavior.delete_mode == "permanent"
    assert settings.behavior.retention_days == 0


def test_permanent_delete_mode_cannot_disable_the_bulk_trigger_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`null` there plus `permanent` here is one Extract Metadata click from an empty library."""
    monkeypatch.setenv("IMMICH__API_KEY", "key")
    monkeypatch.setenv("WEBHOOK__TOKEN", "tok")
    body = _with_behavior(
        dry_run=False,
        trash_original=True,
        delete_mode="permanent",
        max_asset_age_hours="null",
    )
    with pytest.raises(ConfigError, match=r"max_asset_age_hours"):
        load_settings(_write(tmp_path, body))


def test_the_bulk_trigger_gate_is_on_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMMICH__API_KEY", "key")
    monkeypatch.setenv("WEBHOOK__TOKEN", "tok")
    settings = load_settings(_write(tmp_path, _MINIMAL))
    assert settings.behavior.max_asset_age_hours == 24.0


def test_the_bulk_trigger_gate_may_be_disabled_when_deletes_are_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off is a legitimate choice — as long as an original can still be brought back."""
    monkeypatch.setenv("IMMICH__API_KEY", "key")
    monkeypatch.setenv("WEBHOOK__TOKEN", "tok")
    body = _with_behavior(dry_run=False, trash_original=True, max_asset_age_hours="null")
    settings = load_settings(_write(tmp_path, body))
    assert settings.behavior.max_asset_age_hours is None


def test_a_zero_length_freshness_window_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """0 would refuse every webhook, which is a stopped service, not a configured one."""
    monkeypatch.setenv("IMMICH__API_KEY", "key")
    monkeypatch.setenv("WEBHOOK__TOKEN", "tok")
    with pytest.raises(ConfigError):
        load_settings(_write(tmp_path, _with_behavior(max_asset_age_hours=0)))


def test_the_surge_breaker_is_off_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The breaker counts assets and knows nothing else about them.

    A first phone backup and a camera card import look exactly like the influx it exists to
    stop, and `IMAGE` in `enabled_types` makes that an ordinary day rather than an unusual
    one. `max_asset_age_hours` is the guard that can tell a re-trigger from an upload, and
    that one is still on by default — see the test above.
    """
    monkeypatch.setenv("IMMICH__API_KEY", "key")
    monkeypatch.setenv("WEBHOOK__TOKEN", "tok")
    settings = load_settings(_write(tmp_path, _MINIMAL))
    assert settings.behavior.surge_threshold is None
    # The window keeps its value: it is what the threshold is counted over once somebody
    # sets one, and a default of `null` there would be a second thing to configure.
    assert settings.behavior.surge_window_seconds == 600.0


def test_the_surge_breaker_turns_on_with_a_threshold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Off by default is a default, not a removal. A number still arms it."""
    monkeypatch.setenv("IMMICH__API_KEY", "key")
    monkeypatch.setenv("WEBHOOK__TOKEN", "tok")
    settings = load_settings(_write(tmp_path, _with_behavior(surge_threshold=2000, surge_window_seconds=900)))
    assert settings.behavior.surge_threshold == 2000
    assert settings.behavior.surge_window_seconds == 900.0


def test_the_surge_breaker_may_be_disabled_even_with_permanent_deletes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deliberately unlike `max_asset_age_hours`, which *is* refused there.

    The freshness gate is the precise fix for the bulk trigger and is mandatory. The breaker
    is a backstop with a real false-positive rate — a large phone backup is a surge by its
    definition — so turning it off stays a supported choice. Since it now ships off, this
    covers both the explicit `null` and the default that no longer writes one.
    """
    monkeypatch.setenv("IMMICH__API_KEY", "key")
    monkeypatch.setenv("WEBHOOK__TOKEN", "tok")
    body = _with_behavior(dry_run=False, trash_original=True, delete_mode="permanent", surge_threshold="null")
    assert load_settings(_write(tmp_path, body)).behavior.surge_threshold is None

    without = _with_behavior(dry_run=False, trash_original=True, delete_mode="permanent")
    assert load_settings(_write(tmp_path, without)).behavior.surge_threshold is None


def test_a_zero_surge_threshold_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """0 would pause on the first webhook ever received."""
    monkeypatch.setenv("IMMICH__API_KEY", "key")
    monkeypatch.setenv("WEBHOOK__TOKEN", "tok")
    with pytest.raises(ConfigError):
        load_settings(_write(tmp_path, _with_behavior(surge_threshold=0)))


def test_delete_mode_defaults_to_the_recoverable_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMMICH__API_KEY", "key")
    monkeypatch.setenv("WEBHOOK__TOKEN", "tok")
    assert load_settings(_write(tmp_path, _MINIMAL)).behavior.delete_mode == "trash"


def test_argv_does_not_interpret_filenames_as_arguments() -> None:
    preset = Preset(name="p", type="VIDEO", cmd="ffmpeg -i {input} {output}", suffix=".mp4")
    argv = preset.argv(Path("/tmp/a; rm -rf ~.mov"), Path("/tmp/o.mp4"))
    assert argv[2] == "/tmp/a; rm -rf ~.mov"
    assert len(argv) == 4


def test_hardware_encoder_and_render_node_are_read_from_the_command() -> None:
    qsv = Preset(
        name="qsv",
        type="VIDEO",
        cmd="ffmpeg -y -hwaccel qsv -qsv_device /dev/dri/renderD129 -i {input} "
        "-c:v hevc_qsv -global_quality 26 {output}",
        suffix=".mp4",
    )
    assert qsv.hardware_encoder == "hevc_qsv"
    assert qsv.render_node == "/dev/dri/renderD129"


def test_a_cpu_preset_reports_no_hardware_encoder() -> None:
    """Only the token after -c:v counts — a stray mention cannot fake a GPU preset."""
    cpu = Preset(
        name="cpu",
        type="VIDEO",
        cmd="ffmpeg -y -i {input} -c:v libx265 -crf 26 -metadata comment=hevc_nvenc {output}",
        suffix=".mp4",
    )
    assert cpu.hardware_encoder is None
    assert cpu.render_node == "/dev/dri/renderD128"


# ------------------------------------------------------------------- format allowlist

_IMAGE_PRESETS = """
presets:
  video-h265:
    match: { type: VIDEO }
    cmd: ffmpeg -i {input} -c:v libx265 {output}
    suffix: .mp4
  image-jpeg:
    match:
      type: IMAGE
      extensions: [.jpg, .JPEG]
    cmd: magick {input} -auto-orient -quality 82 {output}
    suffix: .jpg
    exiftool_copy: true
    normalize_orientation: true
    max_ratio: 0.9
    require_date_time_original: false
    min_source_quality: 86
"""

_WITH_IMAGES = "\nbehavior:\n  enabled_types: [VIDEO, IMAGE]\n" + _IMAGE_PRESETS


def _load_images(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str = _WITH_IMAGES):
    monkeypatch.setenv("IMMICH__API_KEY", "key")
    monkeypatch.setenv("WEBHOOK__TOKEN", "tok")
    return load_settings(_write(tmp_path, body))


@pytest.mark.parametrize(
    ("filename", "matches"),
    [
        ("holiday.jpg", True),
        ("holiday.JPG", True),  # extensions are compared case-insensitively
        ("holiday.jpeg", True),  # ... in both directions
        ("scan.png", False),
        ("raw.dng", False),  # ImageMagick would happily develop this one
        ("clip.CR2", False),
        ("noextension", False),
    ],
)
def test_extension_allowlist_decides_the_preset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str, matches: bool
) -> None:
    settings = _load_images(tmp_path, monkeypatch)
    assert (settings.preset_for("IMAGE", filename) is not None) is matches
    # The type stays covered either way — that distinction is what separates
    # SkipReason.UNSUPPORTED_FORMAT from SkipReason.NO_PRESET.
    assert settings.type_is_covered("IMAGE") is True


def test_empty_extensions_accept_everything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Video presets carry no extension list and must keep matching every container."""
    settings = _load_images(tmp_path, monkeypatch)
    for filename in ("clip.mp4", "clip.mov", "clip.avi", "clip.mkv"):
        assert settings.preset_for("VIDEO", filename) is not None


def test_preset_overrides_win_over_behavior(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _load_images(tmp_path, monkeypatch)
    behavior = settings.behavior
    image = settings.preset_for("IMAGE", "a.jpg")
    video = settings.preset_for("VIDEO", "a.mp4")
    assert image is not None and video is not None

    assert image.effective_max_ratio(behavior) == 0.9
    assert image.effective_require_date_time_original(behavior) is False
    # The video preset sets none of them and must fall back to the behavior block.
    assert video.effective_max_ratio(behavior) == behavior.max_ratio
    assert video.effective_require_date_time_original(behavior) is True
    assert video.effective_min_savings_bytes(behavior) == behavior.min_savings_bytes


def test_extension_without_dot_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMMICH__API_KEY", "key")
    monkeypatch.setenv("WEBHOOK__TOKEN", "tok")
    body = _WITH_IMAGES.replace("extensions: [.jpg, .JPEG]", "extensions: [jpg]")
    with pytest.raises(ConfigError, match="must start with a dot"):
        load_settings(_write(tmp_path, body))


def test_warn_mode_is_refused_with_permanent_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A warning cannot undo a force-deleted original, so the pair is rejected at startup."""
    monkeypatch.setenv("IMMICH__API_KEY", "key")
    monkeypatch.setenv("WEBHOOK__TOKEN", "tok")
    body = (
        "\nbehavior:\n"
        "  enabled_types: [VIDEO]\n"
        "  dry_run: false\n"
        "  trash_original: true\n"
        "  delete_mode: permanent\n"
        "  metadata_verify: warn\n" + _PRESETS
    )
    with pytest.raises(ConfigError, match="metadata_verify"):
        load_settings(_write(tmp_path, body))


def test_warn_mode_is_allowed_with_recoverable_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMMICH__API_KEY", "key")
    monkeypatch.setenv("WEBHOOK__TOKEN", "tok")
    body = (
        "\nbehavior:\n"
        "  enabled_types: [VIDEO]\n"
        "  dry_run: false\n"
        "  trash_original: true\n"
        "  delete_mode: trash\n"
        "  metadata_verify: warn\n" + _PRESETS
    )
    assert load_settings(_write(tmp_path, body)).behavior.metadata_verify == "warn"


# ------------------------------------------------------------------ renamed settings


def test_the_old_min_size_bytes_key_names_its_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 1.1.0 config must not fail with a bare "extra inputs are not permitted".

    `extra="forbid"` is what stops a typo from being silently ignored, but for a key that
    used to be valid it answers a question nobody asked. The startup error has to name the
    new key, because a config that will not load stops the service.
    """
    monkeypatch.setenv("IMMICH__API_KEY", "key")
    monkeypatch.setenv("WEBHOOK__TOKEN", "tok")
    body = "\nbehavior:\n  min_size_bytes: 20971520\n" + _PRESETS
    with pytest.raises(ConfigError, match="min_savings_bytes"):
        load_settings(_write(tmp_path, body))


# ---------------------------------------------------------------------------- the shim


def test_shim_ships_inert() -> None:
    """Same rule as dry_run: nothing that changes what a client sees is on by default."""
    settings = Settings()
    assert settings.shim.enabled is False
    assert settings.shim.log_only is False


def test_shim_rejects_an_upstream_url_with_the_api_suffix() -> None:
    """The one mistake this setting invites, and it fails silently at runtime.

    `immich.base_url` ends in `/api` and this one must not: the shim forwards the client's
    whole path, which already begins with `/api`. Getting it wrong yields 404s from a
    server that is demonstrably up.
    """
    with pytest.raises(ConfigError, match="without the /api suffix"):
        ShimSettings(upstream_url="http://immich-server:2283/api")


def test_shim_normalises_a_trailing_slash() -> None:
    assert ShimSettings(upstream_url="http://immich-server:2283/").upstream_url == (
        "http://immich-server:2283"
    )


def test_shim_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        ShimSettings(rewrite_everything=True)
