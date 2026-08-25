"""/metrics must be valid exposition text, and must never leak asset data."""

from __future__ import annotations

import re
from typing import Any

from fastapi.testclient import TestClient

from immich_compressor.config import Settings
from immich_compressor.metrics import CONTENT_TYPE, Histogram, render
from immich_compressor.server import create_app

# One metric line: name, optional labels, then a value.
SAMPLE_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*(\{[^}]*\})? -?[0-9.eE+\-]+(\+Inf)?$")

STORE_STATS: dict[str, Any] = {
    "by_state": {"done": 2, "skipped": 3, "failed": 1},
    "by_skip_reason": {"no_gain": 2, "dry_run": 1},
    "total": 6,
    "compressed_assets": 2,
    "original_bytes": 50710662,
    "compressed_bytes": 26686614,
    "saved_bytes": 24024048,
    "average_ratio": 0.5263,
}
SESSION = {"processed": 2, "skipped": 3, "failed": 1, "deleted": 2, "bytes_saved": 24024048}


def _render(**overrides: Any) -> str:
    body: dict[str, Any] = {
        "store_stats": STORE_STATS,
        "counters": {"webhooks_received": 12, "webhooks_rejected": 3},
        "session": SESSION,
        "encode_seconds": Histogram(),
        "config": {"dry_run": False, "trash_original": True, "delete_mode": "trash"},
        "version": "1.1.0",
    }
    body.update(overrides)
    return render(**body)


def test_every_line_is_a_comment_or_a_valid_sample() -> None:
    for line in _render().splitlines():
        if not line or line.startswith("#"):
            continue
        assert SAMPLE_RE.match(line), line


def test_every_family_declares_its_help_and_type() -> None:
    """A family without HELP/TYPE still scrapes, but nothing can label it in a dashboard."""
    body = _render()
    families = {line.split()[2] for line in body.splitlines() if line.startswith("# TYPE")}
    helped = {line.split()[2] for line in body.splitlines() if line.startswith("# HELP")}
    assert families == helped
    for family in families:
        assert family.startswith("immich_compressor_")


def test_the_numbers_come_from_the_store() -> None:
    body = _render()
    assert 'immich_compressor_jobs{state="done"} 2' in body
    assert 'immich_compressor_jobs_skipped{reason="no_gain"} 2' in body
    assert "immich_compressor_saved_bytes 24024048" in body
    assert "immich_compressor_compressed_assets 2" in body
    assert "immich_compressor_session_deleted_total 2" in body
    # %g would have rendered this as 5.07107e+07 — valid exposition text, wrong number.
    assert "immich_compressor_original_bytes 50710662" in body


def test_the_settings_worth_alerting_on_are_exposed() -> None:
    live = _render(config={"dry_run": False, "trash_original": True, "delete_mode": "permanent"})
    assert "immich_compressor_config_dry_run 0" in live
    assert "immich_compressor_config_trash_original 1" in live
    assert "immich_compressor_config_permanent_delete 1" in live

    inert = _render(config={"dry_run": True, "trash_original": False, "delete_mode": "trash"})
    assert "immich_compressor_config_dry_run 1" in inert
    assert "immich_compressor_config_permanent_delete 0" in inert


def test_the_histogram_buckets_are_cumulative_and_end_at_inf() -> None:
    histogram = Histogram()
    for seconds in (5.0, 45.0, 45.0, 5000.0):
        histogram.observe(seconds)

    body = _render(encode_seconds=histogram)
    assert 'immich_compressor_encode_duration_seconds_bucket{le="10"} 1' in body
    assert 'immich_compressor_encode_duration_seconds_bucket{le="60"} 3' in body
    # The 5000 s observation falls outside every bucket but must still be counted.
    assert 'immich_compressor_encode_duration_seconds_bucket{le="3600"} 3' in body
    assert 'immich_compressor_encode_duration_seconds_bucket{le="+Inf"} 4' in body
    assert "immich_compressor_encode_duration_seconds_count 4" in body
    assert "immich_compressor_encode_duration_seconds_sum 5095" in body


def test_bucket_counts_never_decrease() -> None:
    histogram = Histogram()
    for seconds in (1.0, 15.0, 200.0, 200.0, 4000.0):
        histogram.observe(seconds)
    counts = [count for _, count in histogram.cumulative()]
    assert counts == sorted(counts)
    assert counts[-1] <= histogram.observations


def test_a_label_value_could_not_break_the_format() -> None:
    """Nothing user-controlled reaches a label today; the escaping is belt and braces."""
    body = render(
        store_stats={"by_state": {'we"ird\\state': 1}, "by_skip_reason": {}},
        counters={},
        session=SESSION,
        encode_seconds=Histogram(),
        config={},
        version='1.0"0',
    )
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        assert SAMPLE_RE.match(line), line


def test_an_empty_store_still_renders_every_family() -> None:
    body = render(
        store_stats={},
        counters={},
        session={},
        encode_seconds=Histogram(),
        config={},
        version="1.1.0",
    )
    assert "immich_compressor_jobs_total 0" in body
    assert 'immich_compressor_encode_duration_seconds_bucket{le="+Inf"} 0' in body
    assert "immich_compressor_webhooks_received_total 0" in body
    assert "immich_compressor_webhooks_rejected_total 0" in body


def test_the_endpoint_serves_it(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert CONTENT_TYPE.startswith("text/plain")
    assert "immich_compressor_build_info" in response.text
    assert "immich_compressor_jobs_total" in response.text


def test_the_endpoint_needs_no_secret_and_exposes_no_asset_ids(settings: Settings) -> None:
    """It is unauthenticated on purpose, so it must carry counts and nothing else."""
    with TestClient(create_app(settings)) as client:
        body = client.get("/metrics").text
    # A uuid anywhere in here would mean an asset id leaked into an unauthenticated page.
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}", body)


def test_shim_counters_are_exposed() -> None:
    """Zero is a reading, not an absence: a silent shim and an unrouted one look alike."""
    text = render(
        store_stats={},
        counters={"shim_requests": 12, "shim_lines_rewritten": 3},
        session={},
        encode_seconds=Histogram(),
        config={},
        version="1.3.1",
    )
    assert "immich_compressor_shim_requests_total 12" in text
    assert "immich_compressor_shim_lines_rewritten_total 3" in text
    assert "immich_compressor_shim_gates_opened_total 0" in text
    assert "# TYPE immich_compressor_shim_touches_total counter" in text
