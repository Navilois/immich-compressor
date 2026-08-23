"""The CLI as a user meets it: what each command prints, and what it queues.

The commands are thin, but they are also the entire interface for anybody who never
publishes a port — which is the default deployment. What they say is the product.
"""

from __future__ import annotations

import argparse
import json
import logging
from typing import Any

import httpx
import pytest
import respx

import immich_compressor.__main__ as main
from immich_compressor.__main__ import (
    _backfill_run,
    _backfill_status,
    _jobs,
    _report,
    _reprocess,
    build_parser,
)
from immich_compressor.config import Settings
from immich_compressor.store import WEBHOOKS_RECEIVED, WEBHOOKS_REJECTED, JobStore

BASE = "http://immich-test:2283/api"


def _search_item(asset_id: str, asset_type: str, name: str, size: int = 4_000_000_000) -> dict[str, Any]:
    """One `POST /search/metadata` result, as the backfill scan sees it."""
    return {
        "id": asset_id,
        "type": asset_type,
        "originalFileName": name,
        "originalPath": f"/x/{name}",
        "createdAt": "2019-04-02T10:00:00.000Z",
        "exifInfo": {"fileSizeInByte": size},
    }


def test_backfill_without_a_mode_still_means_run() -> None:
    """`backfill --type VIDEO --limit 50 --apply` is in every doc and in muscle memory.

    The inventory added `scan` and `status` next to it; it did not move the command
    anybody already knows.
    """
    args = build_parser().parse_args(["backfill", "--type", "VIDEO", "--limit", "50", "--apply"])
    assert (args.mode, args.type, args.limit, args.apply) == ("run", "VIDEO", 50, True)
    assert build_parser().parse_args(["backfill", "scan", "--rescan"]).mode == "scan"


@respx.mock
async def test_backfill_run_scans_first_when_there_is_no_inventory(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two phases are honest about the cost; nobody should have to read a manual for job one."""
    respx.post(f"{BASE}/search/metadata").mock(
        return_value=httpx.Response(
            200,
            json={
                "assets": {
                    "items": [_search_item("v1", "VIDEO", "holiday.mp4")],
                    "total": 1,
                    "nextPage": None,
                }
            },
        )
    )

    assert await _backfill_run(settings, "VIDEO", 10, "size", False, True, 1000) == 0

    out = capsys.readouterr().out
    assert "no inventory for VIDEO yet — scanning first" in out
    assert "[dry] would queue v1" in out
    assert "pass --apply" in out


@respx.mock
async def test_backfill_run_says_that_dry_run_will_swallow_the_jobs(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """Queueing 500 jobs that all end as `skipped: dry_run` looks like a working backfill
    right up to the moment somebody reads the report."""
    settings.behavior.dry_run = True
    respx.post(f"{BASE}/search/metadata").mock(
        return_value=httpx.Response(
            200,
            json={"assets": {"items": [_search_item("v1", "VIDEO", "holiday.mp4")], "nextPage": None}},
        )
    )

    assert await _backfill_run(settings, "VIDEO", 10, "size", False, True, 1000) == 0

    out = capsys.readouterr().out
    assert "behavior.dry_run is on" in out
    assert "requeue --reason dry_run --apply" in out


@respx.mock
async def test_backfill_status_reports_what_is_left(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.post(f"{BASE}/search/metadata").mock(
        return_value=httpx.Response(
            200,
            json={
                "assets": {
                    "items": [
                        _search_item("v1", "VIDEO", "holiday.mp4"),
                        _search_item("v2", "VIDEO", "tiny.mp4", 10),
                    ],
                    "nextPage": None,
                }
            },
        )
    )
    assert await _backfill_run(settings, "VIDEO", 10, "size", False, True, 1000) == 0
    capsys.readouterr()

    assert await _backfill_status(settings, as_json=False) == 0

    out = capsys.readouterr().out
    assert "VIDEO: 2 scanned" in out
    assert "1 candidate(s)" in out
    assert "rejected: too_small 1" in out
    assert "last complete walk" in out


async def test_backfill_status_says_so_when_nothing_was_scanned(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await _backfill_status(settings, as_json=False) == 0
    assert "nothing scanned yet" in capsys.readouterr().out


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


def test_serve_configures_logging_before_it_loads_the_settings(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loading the settings is what runs hardware detection, and detection logs the
    encoder it chose and why every other candidate was rejected.

    Configured afterwards, those lines went out through `logging.lastResort`, which drops
    everything below WARNING — so the explanation docs/quickstart.md points at was thrown
    away on every start, and the log opened on an unrelated preset probe instead.
    """
    order: list[str] = []

    def fake_basic_config(**_: object) -> None:
        order.append("logging")

    def fake_load(*_: object, **__: object) -> Settings:
        order.append("settings")
        return settings

    monkeypatch.setattr(logging, "basicConfig", fake_basic_config)
    monkeypatch.setattr(main, "_load", fake_load)
    monkeypatch.setattr("uvicorn.run", lambda *_, **__: order.append("serve"))

    assert main.cmd_serve(argparse.Namespace(config=None)) == 0
    assert order.index("logging") < order.index("settings")


def test_the_help_description_is_written_for_a_terminal() -> None:
    """It used to be the module docstring, printed verbatim: RST backticks and a list of
    seven commands out of twelve, missing `setup` and all three recovery commands."""
    text = main.build_parser().format_help()

    assert "``" not in text
    for command in ("setup", "hardware", "resume", "restore", "jobs"):
        assert command in text


async def test_report_prints_no_python_none_before_anything_is_compressed(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """This is the very first command the quickstart has anybody run."""
    assert await _report(settings, as_json=False) == 0

    out = capsys.readouterr().out
    assert "average ratio —" in out
    assert "None" not in out


async def test_reprocess_names_the_command_that_would_have_worked(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """`reprocess` reads like "process this asset" and means "re-queue a job I know". For
    an asset the webhook never delivered, `backfill` is the way in — and this is the exact
    moment somebody needs to hear that."""
    assert await _reprocess(settings, "an-asset-nobody-sent-us") == 1
    assert "backfill" in capsys.readouterr().err


async def test_jobs_clamps_its_limit_like_the_endpoint_does(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """SQLite reads a negative LIMIT as "no limit at all"."""
    async with JobStore(settings.database_path) as store:
        for index in range(3):
            await store.enqueue(f"a{index}", {}, delay_seconds=0)

    assert await _jobs(settings, status=None, limit=-1, as_json=True) == 0
    assert len(json.loads(capsys.readouterr().out)) == 1
