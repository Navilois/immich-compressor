"""The CLI as a user meets it: what each command prints, and what it queues.

The commands are thin, but they are also the entire interface for anybody who never
publishes a port — which is the default deployment. What they say is the product.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from immich_compressor.__main__ import _backfill
from immich_compressor.config import Settings
from immich_compressor.store import JobStore

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
