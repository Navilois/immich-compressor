"""Configuration must fail fast and must never accept secrets from the YAML file."""

from __future__ import annotations

from pathlib import Path

import pytest

from immich_compressor.config import ConfigError, Preset, load_settings

_MINIMAL = """
behavior:
  enabled_types: [VIDEO]
presets:
  video-h265:
    match: { type: VIDEO }
    cmd: ffmpeg -i {input} -c:v libx265 {output}
    suffix: .mp4
"""


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


def test_argv_does_not_interpret_filenames_as_arguments() -> None:
    preset = Preset(name="p", type="VIDEO", cmd="ffmpeg -i {input} {output}", suffix=".mp4")
    argv = preset.argv(Path("/tmp/a; rm -rf ~.mov"), Path("/tmp/o.mp4"))
    assert argv[2] == "/tmp/a; rm -rf ~.mov"
    assert len(argv) == 4
