"""Shared fixtures. The webhook payloads are real captures from Immich v3.1.0."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from immich_compressor.config import Preset, Settings
from immich_compressor.models import WebhookPayload

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def video_payload_raw() -> dict[str, Any]:
    """Verbatim webhook body captured for a VIDEO asset."""
    return load_fixture("webhook_video.json")


@pytest.fixture
def image_payload_raw() -> dict[str, Any]:
    """Verbatim webhook body captured for an IMAGE asset."""
    return load_fixture("webhook_image.json")


@pytest.fixture
def video_payload(video_payload_raw: dict[str, Any]) -> WebhookPayload:
    return WebhookPayload.model_validate(video_payload_raw)


@pytest.fixture
def video_preset() -> Preset:
    return Preset(
        name="video-h265",
        type="VIDEO",
        cmd="ffmpeg -y -loglevel error -i {input} -map_metadata 0 -c:v libx265 "
        "-preset ultrafast -crf 30 -threads 2 -c:a aac -b:a 96k {output}",
        suffix=".mp4",
        timeout_s=600,
    )


@pytest.fixture
def settings(tmp_path: Path, video_preset: Preset) -> Settings:
    """A fully wired Settings object that never touches the real filesystem layout."""
    return Settings(
        immich={"base_url": "http://immich-test:2283/api", "api_key": "test-key"},
        webhook={"token": "test-token", "header_name": "X-Compressor-Token"},
        behavior={
            "dry_run": False,
            "trash_original": False,
            "initial_delay_seconds": 0,
            "min_size_bytes": 1024,
            "max_ratio": 0.6,
            "enabled_types": ["VIDEO"],
            "skip_if_named_people": True,
            "work_dir": tmp_path / "work",
            "poll_interval_seconds": 0.05,
        },
        presets=[video_preset],
        database_path=tmp_path / "state.db",
    )
