"""The CLI as a user meets it: what each command prints, and what it queues.

The commands are thin, but they are also the entire interface for anybody who never
publishes a port — which is the default deployment. What they say is the product.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from immich_compressor.__main__ import _backfill, _jobs, _report
from immich_compressor.config import Settings
from immich_compressor.store import WEBHOOKS_RECEIVED, WEBHOOKS_REJECTED, JobStore

BASE = "http://immich-test:2283/api"


def _asset(asset_id: str, asset_type: str, name: str) -> dict[str, Any]:
    return {"id": asset_id, "type": asset_type, "originalFileName": name, "originalPath": f"/x/{name}"}


@respx.mock
async def test_backfill_queues_only_the_type_that_was_asked_for(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """Immich ignores the `type` field of POST /search/large-assets.

    Measured against a live v3.1.0: IMAGE and VIDEO answer with the identical set of
    videos. Without the client-side check `backfill --type IMAGE` queues videos — invisible
    while dry_run is on, and destructive from stage 3 on.
    """
    respx.post(f"{BASE}/search/large-assets").mock(
        return_value=httpx.Response(
            200,
            json=[
                _asset("v1", "VIDEO", "2022_06_28_07_25_25.mp4"),
                _asset("i1", "IMAGE", "DSC_0001.jpg"),
                _asset("v2", "VIDEO", "20231125_104744.mp4"),
            ],
        )
    )

    assert await _backfill(settings, "IMAGE", limit=5, apply=True) == 0

    async with JobStore(settings.database_path) as store:
        queued = {job.source_asset_id for job in await store.list_jobs()}
    assert queued == {"i1"}

    out = capsys.readouterr().out
    assert "scanned 1 assets, queued 1" in out
    assert "ignored 2 result(s) that were not IMAGE" in out


@respx.mock
async def test_backfill_counts_only_what_it_looked_at(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--limit 2` means two assets, and the closing line says two.

    It used to say three: the counter was incremented once more to notice it had gone past
    the limit, and then reported that number.
    """
    respx.post(f"{BASE}/search/large-assets").mock(
        return_value=httpx.Response(
            200, json=[_asset(f"v{index}", "VIDEO", f"clip{index}.mp4") for index in range(5)]
        )
    )

    assert await _backfill(settings, "VIDEO", limit=2, apply=False) == 0

    out = capsys.readouterr().out
    assert "scanned 2 assets, queued 0 (dry run — pass --apply)" in out
    assert out.count("[dry] would queue") == 2
    assert "ignored" not in out


async def test_report_leads_with_the_webhook_counters(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """ "0 received, 7 rejected" explains every zero underneath it, so it goes first.

    Without it the report of a deployment whose shared secret does not match is
    indistinguishable from one that simply has not been given anything to do.
    """
    async with JobStore(settings.database_path) as store:
        await store.bump_counter(WEBHOOKS_REJECTED, 7)

    assert await _report(settings, as_json=False) == 0

    out = capsys.readouterr().out
    assert "webhooks: 0 received, 7 rejected (bad or missing token)" in out
    assert "WEBHOOK__TOKEN disagree" in out
    assert out.index("webhooks:") < out.index("jobs total:")


async def test_report_names_no_cause_when_webhooks_are_getting_through(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    async with JobStore(settings.database_path) as store:
        await store.bump_counter(WEBHOOKS_RECEIVED, 4)

    assert await _report(settings, as_json=False) == 0

    out = capsys.readouterr().out
    assert "webhooks: 4 received, 0 rejected" in out
    assert "disagree" not in out


async def test_report_json_carries_the_counters_too(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    async with JobStore(settings.database_path) as store:
        await store.bump_counter(WEBHOOKS_RECEIVED, 2)
        await store.bump_counter(WEBHOOKS_REJECTED)

    assert await _report(settings, as_json=True) == 0

    assert json.loads(capsys.readouterr().out)["webhooks"] == {"received": 2, "rejected": 1}


async def test_jobs_prints_the_error_of_a_failed_job(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """`last_error` had exactly one documented route — `curl localhost:8080/jobs` — and
    that route is open nowhere in a default install: no port is published, and the image
    ships neither curl nor wget."""
    async with JobStore(settings.database_path) as store:
        await store.enqueue("a1", {}, delay_seconds=0)
        await store.enqueue("a2", {}, delay_seconds=0)
        await store.mark_failed("a1", "checksum mismatch on the replacement")

    assert await _jobs(settings, status="failed", limit=100, as_json=False) == 0

    out = capsys.readouterr().out
    assert "a1" in out
    assert "checksum mismatch on the replacement" in out
    assert "a2" not in out, "--status failed must not list a queued job"
    assert "1 job(s) in state failed" in out


async def test_jobs_says_so_when_there_are_none(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await _jobs(settings, status="failed", limit=100, as_json=False) == 0
    assert "no jobs in state failed" in capsys.readouterr().out


async def test_jobs_json_leaves_out_the_payload(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """The payload is the whole webhook body — noise here, and it carries file paths."""
    async with JobStore(settings.database_path) as store:
        await store.enqueue(
            "a1", {"data": {"asset": {"originalPath": "/photos/secret.jpg"}}}, delay_seconds=0
        )

    assert await _jobs(settings, status=None, limit=100, as_json=True) == 0

    rows = json.loads(capsys.readouterr().out)
    assert [row["source_asset_id"] for row in rows] == ["a1"]
    assert "payload" not in rows[0]


async def test_jobs_refuses_a_status_it_does_not_know(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await _jobs(settings, status="nonsense", limit=100, as_json=False) == 2
    assert "one of: queued" in capsys.readouterr().err
