"""/metrics must be valid exposition text, and must never leak asset data."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi.testclient import TestClient

from immich_compressor.config import Settings
from immich_compressor.metrics import CONTENT_TYPE, PREFIX, Histogram, render
from immich_compressor.server import create_app
from immich_compressor.store import JobStore

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
        "paused": False,
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


def test_the_published_families_are_exactly_these() -> None:
    """The scrape surface, written down.

    A family is a public interface: dropping or renaming one silently empties whatever
    dashboard and alert was built on it, and nothing else in this suite would notice —
    the checks around it all pass on a body that is one family short. This is the list
    that has to be changed on purpose, in the same commit as the metric.
    """
    families = {line.split()[2] for line in _render().splitlines() if line.startswith("# TYPE")}
    assert families == {
        f"{PREFIX}_{name}"
        for name in (
            "build_info",
            "jobs",
            "jobs_skipped",
            "jobs_total",
            "compressed_assets",
            "original_bytes",
            "compressed_bytes",
            "saved_bytes",
            "webhooks_received_total",
            "webhooks_rejected_total",
            "shim_requests_total",
            "shim_lines_rewritten_total",
            "shim_hashes_translated_total",
            "shim_gates_opened_total",
            "shim_touches_total",
            "shim_passthrough_errors_total",
            "session_processed_total",
            "session_skipped_total",
            "session_failed_total",
            "session_deleted_total",
            "session_bytes_saved_total",
            "encode_duration_seconds",
            "paused",
            "config_dry_run",
            "config_trash_original",
            "config_permanent_delete",
        )
    }


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


def test_the_pause_is_a_number_here_and_not_only_in_healthz() -> None:
    """A latched breaker stops everything, and /metrics is where that gets noticed."""
    assert "immich_compressor_paused 0" in _render()
    assert "immich_compressor_paused 1" in _render(paused=True)


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
        paused=False,
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
        paused=False,
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


def test_the_endpoint_reports_a_latched_pause(settings: Settings) -> None:
    """The wiring, not the rendering: the number has to come from the store the app opens."""

    async def latch() -> None:
        async with JobStore(settings.database_path) as store:
            await store.pause("201 assets queued from webhooks within 600s, over surge_threshold 200")

    asyncio.run(latch())
    with TestClient(create_app(settings)) as client:
        assert "immich_compressor_paused 1" in client.get("/metrics").text


def test_stats_and_metrics_report_the_same_session_counters(settings: Settings) -> None:
    """Two surfaces, one snapshot of the pipeline's counters.

    They used to be assembled separately, field by field, from the same object. A counter
    added to one is then invisible on the other until somebody notices — and the person
    alerting on `session_failed_total` and the person reading `/stats` are looking at the
    same incident.
    """
    app = create_app(settings)
    with TestClient(app) as client:
        stats = app.state.worker.pipeline.stats
        stats.processed, stats.skipped, stats.failed = 7, 3, 2
        stats.deleted, stats.bytes_saved = 5, 24024048

        session = client.get("/stats").json()["session"]
        exposition = client.get("/metrics").text

    assert session == {"processed": 7, "skipped": 3, "failed": 2, "deleted": 5, "bytes_saved": 24024048}
    for name, value in session.items():
        assert f"immich_compressor_session_{name}_total {value}" in exposition


def test_the_exposition_carries_only_the_settings_it_has_a_gauge_for(settings: Settings) -> None:
    """`/stats` and `/metrics` share one settings snapshot, and it is the wider of the two.

    `render` picks the three it can express as a number and drops the rest. Nothing may
    fall through: `enabled_types` is a list, and a list rendered as a sample value is
    exposition text no scraper can parse.
    """
    app = create_app(settings)
    with TestClient(app) as client:
        body = client.get("/metrics").text
        published = client.get("/stats").json()["config"]

    assert "config_dry_run" in body and "config_trash_original" in body
    for ignored in set(published) - {"dry_run", "trash_original", "delete_mode"}:
        assert ignored not in body


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
        paused=False,
        version="1.3.1",
    )
    assert "immich_compressor_shim_requests_total 12" in text
    assert "immich_compressor_shim_lines_rewritten_total 3" in text
    assert "immich_compressor_shim_gates_opened_total 0" in text
    assert "# TYPE immich_compressor_shim_touches_total counter" in text
