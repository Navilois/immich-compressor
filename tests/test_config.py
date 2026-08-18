"""Configuration must fail fast and must never accept secrets from the YAML file."""

from __future__ import annotations

from pathlib import Path

import pytest

from immich_compressor.config import ConfigError, Preset, load_settings

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


def test_enabled_type_without_preset_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    ],
)
def test_invalid_preset_commands_are_rejected(cmd: str) -> None:
    with pytest.raises(ConfigError):
        Preset(name="bad", type="VIDEO", cmd=cmd, suffix=".mp4")


def test_suffix_must_start_with_dot() -> None:
    with pytest.raises(ConfigError, match="suffix"):
        Preset(name="bad", type="VIDEO", cmd="x {input} {output}", suffix="mp4")


def test_argv_renders_without_a_shell() -> None:
    preset = Preset(
        name="p", type="VIDEO", cmd="ffmpeg -i {input} -c copy {output}", suffix=".mp4"
    )
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
    body = _with_behavior(
        dry_run=False, trash_original=True, retention_days=0, delete_mode="permanent"
    )
    settings = load_settings(_write(tmp_path, body))
    assert settings.behavior.delete_mode == "permanent"
    assert settings.behavior.retention_days == 0


def test_delete_mode_defaults_to_the_recoverable_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
