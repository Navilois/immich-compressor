"""The library backfill: the scan, the inventory it writes, and the queue run over it.

Every fact these tests pin down is one that cost something in the field: a server that
answers with the wrong asset type, a `--limit` that counted the wrong thing, an inventory
row that outlived the asset it describes.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from immich_compressor import backfill
from immich_compressor.api import ImmichClient
from immich_compressor.config import Preset, Settings
from immich_compressor.models import BackfillVerdict, SkipReason
from immich_compressor.store import JobStore

BASE = "http://immich-test:2283/api"
SEARCH = f"{BASE}/search/metadata"


def _item(asset_id: str, asset_type: str, name: str, size: int = 50_000_000, **extra: Any) -> dict[str, Any]:
    """One search result, in the shape `POST /search/metadata` answers with."""
    return {
        "id": asset_id,
        "type": asset_type,
        "originalFileName": name,
        "originalPath": f"/library/{name}",
        # Deliberately old: the freshness gate is an *ingest* guard, and the backfill is
        # the intentional way past it.
        "createdAt": "2019-04-02T10:00:00.000Z",
        "exifInfo": {"fileSizeInByte": size},
        **extra,
    }


def _page(items: list[dict[str, Any]], *, next_page: int | None = None) -> httpx.Response:
    """The paginated envelope, with `nextPage` as the string v3 sends."""
    return httpx.Response(
        200,
        json={
            "assets": {
                "items": items,
                "total": len(items),
                "count": len(items),
                "nextPage": str(next_page) if next_page is not None else None,
            }
        },
    )


def _with_images(settings: Settings) -> Settings:
    """The shipped JPEG-only image preset alongside the video one."""
    settings.behavior.enabled_types = ["VIDEO", "IMAGE"]
    settings.presets = [
        *settings.presets,
        Preset(
            name="image-jpeg",
            type="IMAGE",
            extensions=[".jpg", ".jpeg"],
            cmd="magick {input} -auto-orient -quality 82 {output}",
            suffix=".jpg",
            exiftool_copy=True,
            normalize_orientation=True,
        ),
    ]
    return settings


async def _scan(settings: Settings, asset_types: list[str], **kwargs: Any) -> list[backfill.ScanSummary]:
    async with (
        ImmichClient(BASE, "key") as client,
        JobStore(settings.database_path) as store,
    ):
        return await backfill.scan(client, store, settings, asset_types=asset_types, **kwargs)


async def _queue(settings: Settings, **kwargs: Any) -> backfill.QueueSummary:
    async with (
        ImmichClient(BASE, "key") as client,
        JobStore(settings.database_path) as store,
    ):
        return await backfill.queue_candidates(client, store, settings, **kwargs)


# ------------------------------------------------------------------------------- scan


@respx.mock
async def test_scan_files_a_verdict_for_every_asset(settings: Settings) -> None:
    """The scan runs the worker's own guards, so the inventory knows what is worth queueing.

    The rejected rows are kept — they are the answer to "why is my library not being
    compressed" — but they carry no payload, because nothing will ever enqueue them.
    """
    respx.post(SEARCH).mock(
        return_value=_page(
            [
                _item("v1", "VIDEO", "holiday.mp4", 4_000_000_000),
                _item("v2", "VIDEO", "tiny.mp4", 500),
                _item("v3", "VIDEO", "already.cmp.mp4", 900_000_000),
                _item("v4", "VIDEO", "external.mp4", 800_000_000, isExternal=True),
            ]
        )
    )

    [summary] = await _scan(settings, ["VIDEO"])

    assert summary.candidates == 1
    assert summary.candidate_bytes == 4_000_000_000
    assert summary.by_verdict == {
        SkipReason.TOO_SMALL.value: 1,
        SkipReason.ALREADY_COMPRESSED.value: 1,
        SkipReason.EXTERNAL_LIBRARY.value: 1,
    }
    assert summary.completed is True

    async with JobStore(settings.database_path) as store:
        stats = await store.inventory_stats()
        candidates = await store.pick_candidates(asset_types=["VIDEO"], limit=10)
    assert stats["types"]["VIDEO"]["scanned"] == 4
    assert [candidate.asset_id for candidate in candidates] == ["v1"]
    assert candidates[0].payload["data"]["asset"]["id"] == "v1"
    assert candidates[0].payload["trigger"] == backfill.TRIGGER


@respx.mock
async def test_scan_reaches_stills(settings: Settings) -> None:
    """`--type IMAGE` was unreachable on the old endpoint. This is the test that says so."""
    respx.post(SEARCH).mock(
        return_value=_page(
            [
                _item("i1", "IMAGE", "DSC_0001.jpg", 12_000_000),
                _item("i2", "IMAGE", "raw.dng", 40_000_000),
            ]
        )
    )

    [summary] = await _scan(_with_images(settings), ["IMAGE"])

    # The RAW is refused by the extension allowlist, not by the type check: developing it
    # to 8-bit and deleting the original is the one mistake this project cannot undo.
    assert summary.candidates == 1
    assert summary.by_verdict == {SkipReason.UNSUPPORTED_FORMAT.value: 1}


@respx.mock
async def test_scan_drops_results_of_another_type(settings: Settings) -> None:
    """The type filter is a request, not a guarantee — `/search/large-assets` proved it.

    A scan that trusted the server would file videos in the stills inventory and re-encode
    them the moment somebody ran `backfill run --type IMAGE --apply`.
    """
    respx.post(SEARCH).mock(
        return_value=_page(
            [
                _item("v1", "VIDEO", "clip.mp4"),
                _item("i1", "IMAGE", "photo.jpg", 12_000_000),
                _item("v2", "VIDEO", "clip2.mp4"),
            ]
        )
    )

    [summary] = await _scan(_with_images(settings), ["IMAGE"])

    assert summary.foreign == 2
    assert summary.recorded == 1
    async with JobStore(settings.database_path) as store:
        stats = await store.inventory_stats()
    assert set(stats["types"]) == {"IMAGE"}


@respx.mock
async def test_scan_walks_every_page(settings: Settings) -> None:
    respx.post(SEARCH).mock(
        side_effect=[
            _page([_item("v1", "VIDEO", "a.mp4")], next_page=2),
            _page([_item("v2", "VIDEO", "b.mp4")], next_page=3),
            _page([_item("v3", "VIDEO", "c.mp4")]),
        ]
    )

    [summary] = await _scan(settings, ["VIDEO"], page_size=1)

    assert (summary.pages, summary.seen, summary.candidates) == (3, 3, 3)
    assert summary.completed is True


@respx.mock
async def test_an_interrupted_scan_resumes_at_its_cursor(settings: Settings) -> None:
    """Every page is committed before the cursor moves, so a walk survives being cut off."""
    respx.post(SEARCH).mock(
        side_effect=[
            _page([_item("v1", "VIDEO", "a.mp4")], next_page=2),
            _page([_item("v2", "VIDEO", "b.mp4")], next_page=3),
        ]
    )

    [first] = await _scan(settings, ["VIDEO"], page_size=1, max_pages=1)
    assert first.completed is False
    assert first.stopped_because == "stopped after 1 pages"

    respx.post(SEARCH).mock(side_effect=[_page([_item("v2", "VIDEO", "b.mp4")])])
    [second] = await _scan(settings, ["VIDEO"], page_size=1)

    assert second.resumed_from == 2
    assert second.completed is True
    async with JobStore(settings.database_path) as store:
        stats = await store.inventory_stats()
    assert stats["types"]["VIDEO"]["scanned"] == 2


@respx.mock
async def test_scan_stops_when_the_server_ignores_page(settings: Settings) -> None:
    """`/search/large-assets` ignored `type` and `size`. Assume nothing about `page` either.

    Without this the scan rewrites the same page until `max_pages` runs out, and reports a
    number of "scanned" assets that is a multiple of the library.
    """
    same = _page([_item("v1", "VIDEO", "a.mp4")], next_page=2)
    respx.post(SEARCH).mock(side_effect=[same, same, same])

    [summary] = await _scan(settings, ["VIDEO"], page_size=1)

    assert summary.pages == 1
    assert summary.stopped_because is not None
    assert "does not apply `page`" in summary.stopped_because


@respx.mock
async def test_scan_is_not_subject_to_the_freshness_gate(settings: Settings) -> None:
    """The whole point: assets too old for a webhook are exactly what the backfill is for."""
    respx.post(SEARCH).mock(return_value=_page([_item("v1", "VIDEO", "from-2019.mp4")]))

    [summary] = await _scan(settings, ["VIDEO"])

    assert summary.candidates == 1


# -------------------------------------------------------------------------- queue run


async def _seed(settings: Settings, items: list[dict[str, Any]]) -> None:
    with respx.mock:
        respx.post(SEARCH).mock(return_value=_page(items))
        await _scan(settings, ["VIDEO"])


@respx.mock
async def test_a_dry_run_writes_nothing_and_takes_the_biggest_first(settings: Settings) -> None:
    await _seed(
        settings,
        [
            _item("small", "VIDEO", "small.mp4", 100_000_000),
            _item("huge", "VIDEO", "huge.mp4", 9_000_000_000),
            _item("mid", "VIDEO", "mid.mp4", 900_000_000),
        ],
    )

    summary = await _queue(settings, asset_types=["VIDEO"], limit=2, apply=False)

    assert [queued.asset_id for queued in summary.queued] == ["huge", "mid"]
    async with JobStore(settings.database_path) as store:
        assert await store.list_jobs() == []
        assert len(await store.pick_candidates(asset_types=["VIDEO"], limit=10)) == 3


@respx.mock
async def test_apply_queues_the_scanned_payload(settings: Settings) -> None:
    """The job is driven from what the scan saw, so the worker needs no second lookup."""
    await _seed(settings, [_item("v1", "VIDEO", "holiday.mp4", 4_000_000_000)])
    respx.get(f"{BASE}/assets/v1").mock(
        return_value=httpx.Response(200, json={"id": "v1", "type": "VIDEO", "isTrashed": False})
    )

    summary = await _queue(settings, asset_types=["VIDEO"], limit=5, apply=True)

    assert [queued.asset_id for queued in summary.queued] == ["v1"]
    assert summary.exhausted is True
    async with JobStore(settings.database_path) as store:
        [job] = await store.list_jobs()
        assert job.asset_type == "VIDEO"
        assert "holiday.mp4" in job.payload
        # Out of the candidate set now, but still counted by `status`.
        assert await store.pick_candidates(asset_types=["VIDEO"], limit=10) == []
        assert (await store.inventory_stats())["types"]["VIDEO"]["queued"] == 1


@respx.mock
async def test_limit_counts_queued_jobs_not_candidates_looked_at(settings: Settings) -> None:
    """The old `--limit` was spent on anything the search answered with, duplicates included.

    Here the two biggest candidates are refused by the live re-check, and `--limit 1` still
    delivers one job.
    """
    await _seed(
        settings,
        [
            _item("gone", "VIDEO", "gone.mp4", 9_000_000_000),
            _item("trashed", "VIDEO", "trashed.mp4", 8_000_000_000),
            _item("good", "VIDEO", "good.mp4", 7_000_000_000),
        ],
    )
    respx.get(f"{BASE}/assets/gone").mock(return_value=httpx.Response(404, json={"message": "not found"}))
    respx.get(f"{BASE}/assets/trashed").mock(
        return_value=httpx.Response(200, json={"id": "trashed", "type": "VIDEO", "isTrashed": True})
    )
    respx.get(f"{BASE}/assets/good").mock(
        return_value=httpx.Response(200, json={"id": "good", "type": "VIDEO", "isTrashed": False})
    )

    summary = await _queue(settings, asset_types=["VIDEO"], limit=1, apply=True)

    assert [queued.asset_id for queued in summary.queued] == ["good"]
    assert summary.downgraded == {
        BackfillVerdict.MISSING.value: 1,
        SkipReason.TRASHED.value: 1,
    }
    async with JobStore(settings.database_path) as store:
        assert [job.source_asset_id for job in await store.list_jobs()] == ["good"]
        # The two refusals are recorded, so the next run does not look at them again.
        assert await store.pick_candidates(asset_types=["VIDEO"], limit=10) == []


@respx.mock
async def test_named_people_are_refused_before_a_job_exists(settings: Settings) -> None:
    await _seed(settings, [_item("v1", "VIDEO", "birthday.mp4", 4_000_000_000)])
    respx.get(f"{BASE}/assets/v1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "v1",
                "type": "VIDEO",
                "isTrashed": False,
                "people": [{"id": "p1", "name": "Grandma"}],
            },
        )
    )

    summary = await _queue(settings, asset_types=["VIDEO"], limit=5, apply=True)

    assert summary.queued == []
    assert summary.downgraded == {SkipReason.NAMED_PEOPLE.value: 1}
    async with JobStore(settings.database_path) as store:
        assert await store.list_jobs() == []


@respx.mock
async def test_an_asset_that_already_has_a_job_is_recorded_not_requeued(settings: Settings) -> None:
    """`ON CONFLICT DO NOTHING` is the guarantee; this is what the operator sees of it."""
    await _seed(settings, [_item("v1", "VIDEO", "holiday.mp4", 4_000_000_000)])
    async with JobStore(settings.database_path) as store:
        await store.enqueue("v1", {"data": {"asset": {"type": "VIDEO"}}}, delay_seconds=0)
    respx.get(f"{BASE}/assets/v1").mock(
        return_value=httpx.Response(200, json={"id": "v1", "type": "VIDEO", "isTrashed": False})
    )

    summary = await _queue(settings, asset_types=["VIDEO"], limit=5, apply=True)

    assert summary.queued == []
    assert summary.downgraded == {BackfillVerdict.ALREADY_KNOWN.value: 1}


@respx.mock
async def test_no_verify_asks_the_server_nothing(settings: Settings) -> None:
    """The re-check is one request per asset. Somebody working through 4000 clips may not
    want them, and the pipeline re-reads the live asset anyway before it touches it."""
    await _seed(settings, [_item("v1", "VIDEO", "holiday.mp4", 4_000_000_000)])
    route = respx.get(f"{BASE}/assets/v1")

    summary = await _queue(settings, asset_types=["VIDEO"], limit=5, apply=True, verify=False)

    assert [queued.asset_id for queued in summary.queued] == ["v1"]
    assert route.call_count == 0


@respx.mock
async def test_a_second_apply_run_makes_progress(settings: Settings) -> None:
    """The head-of-the-list problem, gone: run twice and you get the next batch."""
    await _seed(
        settings,
        [_item(f"v{index}", "VIDEO", f"clip{index}.mp4", 9_000_000_000 - index) for index in range(4)],
    )
    for index in range(4):
        respx.get(f"{BASE}/assets/v{index}").mock(
            return_value=httpx.Response(200, json={"id": f"v{index}", "type": "VIDEO", "isTrashed": False})
        )

    first = await _queue(settings, asset_types=["VIDEO"], limit=2, apply=True)
    second = await _queue(settings, asset_types=["VIDEO"], limit=2, apply=True)
    third = await _queue(settings, asset_types=["VIDEO"], limit=2, apply=True)

    assert [queued.asset_id for queued in first.queued] == ["v0", "v1"]
    assert [queued.asset_id for queued in second.queued] == ["v2", "v3"]
    # `exhausted` means "the inventory ran out", not "the limit was reached": the second
    # run stopped on its limit with nothing left behind it, the third found nothing at all.
    assert second.exhausted is False
    assert third.queued == []
    assert third.exhausted is True


# ------------------------------------------------------------------------- resolution


def test_resolve_types_defaults_to_every_enabled_type(settings: Settings) -> None:
    assert backfill.resolve_types(_with_images(settings), None) == ["VIDEO", "IMAGE"]
    assert backfill.resolve_types(settings, "IMAGE") == ["IMAGE"]


@pytest.mark.parametrize("bad", [{}, {"type": "VIDEO"}, {"id": "x"}])
def test_evaluate_survives_a_result_it_cannot_read(settings: Settings, bad: dict[str, Any]) -> None:
    """A shape this client does not know is a log line, not a crashed scan."""
    assert backfill.evaluate(bad, settings) is None
