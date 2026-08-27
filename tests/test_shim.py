"""The checksum-translation shim: the gate, the rewrite, and the two proxied routes.

Every assertion about a line that should not change is a *byte* comparison, not a
comparison of parsed JSON. The shim re-serialises what it rewrites, and a test that only
compared meaning would happily accept a pass that reformatted every untouched line in a
sync stream — which changes `Content-Length`, churns the client's parser, and would be a
regression nobody could see.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from immich_compressor.models import LedgerEntry, ReturnedOriginal, SkipReason
from immich_compressor.shim import (
    ChecksumLedger,
    OwnerResolver,
    ShimDeps,
    TranslationMaps,
    build_router,
    describe,
    rewrite_sync_line,
    translate_upload_check,
)
from immich_compressor.store import (
    SHIM_GATES_OPENED,
    SHIM_HASHES_TRANSLATED,
    SHIM_LINES_REWRITTEN,
    SHIM_PASSTHROUGH_ERRORS,
    SHIM_REQUESTS,
    SHIM_TOUCHES,
    JobStore,
)

ORIGINAL_HASH = "02MpaJkpzGHNbGwxWtencVNK7uY="
REPLACEMENT_HASH = "z9K1aQq0PPnB1sVXhF2mQ7t0abc="
OWNER = "11111111-1111-4111-8111-111111111111"
ORIGINAL_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
REPLACEMENT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
# The original, uploaded a second time by a device that still held the file. A new id, and
# `ORIGINAL_HASH` live on the server again.
COPY_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"

PAYLOAD = {"type": "AssetV1", "trigger": "AssetMetadataExtraction", "data": {"asset": {"id": "a"}}}


def entry(*, freed: bool) -> LedgerEntry:
    from datetime import UTC, datetime

    return LedgerEntry(
        source_asset_id=ORIGINAL_ID,
        new_asset_id=REPLACEMENT_ID,
        source_checksum=ORIGINAL_HASH,
        owner_id=OWNER,
        new_checksum=REPLACEMENT_HASH,
        original_freed_at=datetime.now(UTC) if freed else None,
    )


def returned(
    asset_id: str = COPY_ID, *, checksum: str = ORIGINAL_HASH, owner: str = OWNER
) -> ReturnedOriginal:
    return ReturnedOriginal(asset_id=asset_id, owner_id=owner, checksum=checksum)


def asset_line(asset_id: str = REPLACEMENT_ID, checksum: str = REPLACEMENT_HASH) -> bytes:
    return (
        json.dumps(
            {
                "type": "AssetV2",
                "data": {"id": asset_id, "ownerId": OWNER, "checksum": checksum},
                "ack": f"AssetV2|0198-{asset_id[:8]}",
            }
        ).encode()
        + b"\n"
    )


def delete_line(asset_id: str = ORIGINAL_ID) -> bytes:
    return (
        json.dumps(
            {"type": "AssetDeleteV1", "data": {"assetId": asset_id}, "ack": "AssetDeleteV1|0198-del"}
        ).encode()
        + b"\n"
    )


# --------------------------------------------------------------------- the pure rewrite


def test_open_gate_translates_the_checksum() -> None:
    maps = TranslationMaps.build([entry(freed=True)])
    outcome = rewrite_sync_line(asset_line(), maps)

    assert outcome.rewritten is True
    record = json.loads(outcome.data)
    assert record["data"]["checksum"] == ORIGINAL_HASH
    # The resume cursor is the one field that must survive verbatim: the client acks the
    # last line of each batch, and a mangled ack would replay or skip a checkpoint.
    assert record["ack"] == f"AssetV2|0198-{REPLACEMENT_ID[:8]}"


def test_closed_gate_leaves_the_line_alone() -> None:
    """The whole point of the gate.

    While the original still exists it holds this checksum in the phone's mirror, which
    carries a UNIQUE index on (owner, checksum). Writing it onto the replacement now would
    either destroy the original's row or abort the client's entire sync batch.
    """
    maps = TranslationMaps.build([entry(freed=False)])
    line = asset_line()
    outcome = rewrite_sync_line(line, maps)

    assert outcome.rewritten is False
    assert outcome.data == line


def test_unrelated_asset_is_byte_identical() -> None:
    maps = TranslationMaps.build([entry(freed=True)])
    line = asset_line(asset_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc", checksum="other")
    assert rewrite_sync_line(line, maps).data == line


def test_line_already_carrying_the_original_hash_is_not_rewritten() -> None:
    """Idempotence. A second pass over an already-translated stream must be a no-op."""
    maps = TranslationMaps.build([entry(freed=True)])
    line = asset_line(checksum=ORIGINAL_HASH)
    outcome = rewrite_sync_line(line, maps)
    assert outcome.rewritten is False
    assert outcome.data == line


@pytest.mark.parametrize(
    "line",
    [
        b"not json at all\n",
        b'{"type":"AssetV2"}\n',  # no data
        b'{"type":"AssetV2","data":[]}\n',  # data is not an object
        b'{"type":"AssetV2","data":{"id":"x"}}\n',  # no checksum
        b'{"type":"AssetV2","data":{"id":1,"checksum":"y"}}\n',  # id is not a string
        b"[]\n",  # valid JSON, wrong shape
        b"\n",
        b"",
    ],
)
def test_malformed_input_comes_back_untouched(line: bytes) -> None:
    """Fail open, per line. Anything unrecognised is Immich's word, passed on as it came."""
    maps = TranslationMaps.build([entry(freed=True)])
    outcome = rewrite_sync_line(line, maps)
    assert outcome.data == line
    assert outcome.rewritten is False


def test_delete_of_a_replaced_original_opens_the_gate_and_is_forwarded() -> None:
    maps = TranslationMaps.build([entry(freed=False)])
    line = delete_line()
    outcome = rewrite_sync_line(line, maps)

    assert [item.source_asset_id for item in outcome.gate_opens] == [ORIGINAL_ID]
    # Forwarded unchanged: the phone still has to learn the original is gone, and dropping
    # the line would stall that type's ack cursor.
    assert outcome.data == line
    assert outcome.rewritten is False


def test_delete_of_an_unrelated_asset_opens_nothing() -> None:
    maps = TranslationMaps.build([entry(freed=False)])
    outcome = rewrite_sync_line(delete_line("dddddddd-dddd-4ddd-8ddd-dddddddddddd"), maps)
    assert outcome.gate_opens == ()


def test_delete_of_an_already_open_gate_opens_nothing() -> None:
    """An open gate is not in the watch map, so a replayed delete costs one dict miss."""
    maps = TranslationMaps.build([entry(freed=True)])
    assert rewrite_sync_line(delete_line(), maps).gate_opens == ()


def test_translate_off_still_watches_deletes() -> None:
    """`log_only` and `rewrite_sync_stream: false` must not blind the delete watch."""
    maps = TranslationMaps.build([entry(freed=False)])
    outcome = rewrite_sync_line(delete_line(), maps, translate=False)
    assert [item.source_asset_id for item in outcome.gate_opens] == [ORIGINAL_ID]


def test_translate_off_leaves_an_open_gate_untranslated() -> None:
    maps = TranslationMaps.build([entry(freed=True)])
    line = asset_line()
    assert rewrite_sync_line(line, maps, translate=False).data == line


# ------------------------------------------------------- the original, back on a new id


def test_a_returned_original_suppresses_the_translation() -> None:
    """The second way to break the one invariant, and the reason the gate is not enough.

    The gate opened honestly: the original really was deleted. Then a device that still had
    the file put it back, the pipeline recognised it and — correctly — left it alone, and
    the checksum is live again on a new id. Handing it to the replacement now puts two rows
    in the phone's mirror under one unique key.
    """
    maps = TranslationMaps.build([entry(freed=True)], [returned()])
    line = asset_line()

    assert maps.sync_rewrite == {}
    assert maps.suppressed == 1
    outcome = rewrite_sync_line(line, maps)
    assert outcome.rewritten is False
    assert outcome.data == line


def test_a_returned_original_of_another_owner_does_not_suppress() -> None:
    """The index is on ``(owner_id, checksum)``, and so is the suppression."""
    other = returned(owner="99999999-9999-4999-8999-999999999999")
    maps = TranslationMaps.build([entry(freed=True)], [other])
    assert maps.sync_rewrite == {REPLACEMENT_ID: ORIGINAL_HASH}
    assert maps.suppressed == 0


def test_a_returned_original_of_another_checksum_does_not_suppress() -> None:
    maps = TranslationMaps.build([entry(freed=True)], [returned(checksum="something-else=")])
    assert maps.sync_rewrite == {REPLACEMENT_ID: ORIGINAL_HASH}
    assert maps.suppressed == 0


def test_a_suppressed_entry_is_in_neither_the_rewrite_nor_the_delete_watch() -> None:
    """Its gate is open and stays open. What it waits on now is the copy, not the original."""
    maps = TranslationMaps.build([entry(freed=True)], [returned()])
    assert maps.sync_rewrite == {}
    assert maps.delete_watch == {}
    assert maps.claim_watch == {COPY_ID: [REPLACEMENT_ID]}


def test_a_closed_gate_with_a_returned_original_still_watches_both() -> None:
    """A re-upload can arrive before the purge does — on `trash`, up to 30 days before.

    The delete watch has to survive it, or the gate never opens at all; and the copy has to
    be watched even though it currently blocks nothing, so that the gate opening later does
    not walk straight into the collision.
    """
    maps = TranslationMaps.build([entry(freed=False)], [returned()])
    assert set(maps.delete_watch) == {ORIGINAL_ID}
    assert maps.claim_watch == {COPY_ID: []}
    assert maps.suppressed == 0


def test_the_delete_of_a_returned_original_releases_its_claim() -> None:
    maps = TranslationMaps.build([entry(freed=True)], [returned()])
    line = delete_line(COPY_ID)
    outcome = rewrite_sync_line(line, maps)

    assert outcome.data == line, "a delete is never rewritten"
    assert outcome.claims_released == (COPY_ID,)
    assert outcome.gate_opens == ()


def test_a_delete_of_an_unrelated_asset_releases_nothing() -> None:
    maps = TranslationMaps.build([entry(freed=True)], [returned()])
    outcome = rewrite_sync_line(delete_line("cccccccc-cccc-4ccc-8ccc-cccccccccccc"), maps)
    assert outcome.claims_released == ()


def test_a_delete_without_a_string_asset_id_is_forwarded_untouched() -> None:
    line = json.dumps({"type": "AssetDeleteV1", "data": {"assetId": None}}).encode() + b"\n"
    outcome = rewrite_sync_line(line, TranslationMaps.build([entry(freed=True)], [returned()]))
    assert outcome.data == line
    assert outcome.gate_opens == () and outcome.claims_released == ()


def test_upload_check_is_not_suppressed_by_a_returned_original() -> None:
    """The asymmetry, on purpose. This direction rewrites a *question*, not a mirror row.

    With the copy live, Immich answers ``duplicate`` whichever hash is asked about, so the
    translation changes nothing and costs nothing. Gating it here would only make the two
    directions differ for no reason.
    """
    maps = TranslationMaps.build([entry(freed=True)], [returned()])
    body = json.dumps({"assets": [{"id": "local-1", "checksum": ORIGINAL_HASH}]}).encode()

    forwarded, translated = translate_upload_check(body, OWNER, maps)

    assert translated == 1
    assert json.loads(forwarded)["assets"][0]["checksum"] == REPLACEMENT_HASH


# ------------------------------------------------------------------- the upload check


def test_upload_check_translates_a_known_original() -> None:
    maps = TranslationMaps.build([entry(freed=False)])
    body = json.dumps({"assets": [{"id": "local-1", "checksum": ORIGINAL_HASH}]}).encode()
    translated, count = translate_upload_check(body, OWNER, maps)

    assert count == 1
    assert json.loads(translated)["assets"][0]["checksum"] == REPLACEMENT_HASH
    # Ungated on purpose: this direction never writes to a mirror, so the unique index
    # that governs the sync rewrite has no bearing on it.


def test_upload_check_ignores_another_owners_hash() -> None:
    maps = TranslationMaps.build([entry(freed=True)])
    body = json.dumps({"assets": [{"id": "local-1", "checksum": ORIGINAL_HASH}]}).encode()
    translated, count = translate_upload_check(body, "someone-else", maps)
    assert (translated, count) == (body, 0)


def test_upload_check_ignores_unknown_hashes() -> None:
    maps = TranslationMaps.build([entry(freed=True)])
    body = json.dumps({"assets": [{"id": "local-1", "checksum": "nobody-knows-this="}]}).encode()
    assert translate_upload_check(body, OWNER, maps) == (body, 0)


def test_upload_check_without_a_resolved_owner_translates_nothing() -> None:
    maps = TranslationMaps.build([entry(freed=True)])
    body = json.dumps({"assets": [{"id": "local-1", "checksum": ORIGINAL_HASH}]}).encode()
    assert translate_upload_check(body, None, maps) == (body, 0)


@pytest.mark.parametrize("body", [b"{", b"[]", b'{"assets":"nope"}', b'{"assets":[1,2]}'])
def test_upload_check_survives_a_malformed_body(body: bytes) -> None:
    maps = TranslationMaps.build([entry(freed=True)])
    assert translate_upload_check(body, OWNER, maps) == (body, 0)


# ------------------------------------------------------------------------- the ledger


async def seeded_store(path: Path, *, freed: bool, came_back: bool = False) -> JobStore:
    store = JobStore(path)
    await store.open()
    await store.enqueue(ORIGINAL_ID, PAYLOAD, delay_seconds=0)
    await store.update(
        ORIGINAL_ID,
        new_asset_id=REPLACEMENT_ID,
        new_checksum=REPLACEMENT_HASH,
        source_checksum=ORIGINAL_HASH,
        owner_id=OWNER,
    )
    if freed:
        await store.mark_original_freed(ORIGINAL_ID)
    if came_back:
        # Exactly what `_check_re_upload` leaves behind: the returning asset's own row,
        # carrying the original's checksum, skipped and otherwise untouched.
        await store.enqueue(COPY_ID, PAYLOAD, delay_seconds=0)
        await store.update(COPY_ID, source_checksum=ORIGINAL_HASH, owner_id=OWNER)
        await store.mark_skipped(COPY_ID, SkipReason.RE_UPLOADED)
    return store


async def test_ledger_refreshes_on_its_timer(tmp_path: Path) -> None:
    clock = [0.0]
    store = await seeded_store(tmp_path / "s.db", freed=False)
    try:
        ledger = ChecksumLedger(store, 60.0, clock=lambda: clock[0])
        assert (await ledger.maps()).sync_rewrite == {}

        await store.mark_original_freed(ORIGINAL_ID)
        # Inside the refresh window the old maps stand.
        clock[0] = 30.0
        assert (await ledger.maps()).sync_rewrite == {}

        clock[0] = 61.0
        assert (await ledger.maps()).sync_rewrite == {REPLACEMENT_ID: ORIGINAL_HASH}
    finally:
        await store.close()


async def test_ledger_invalidate_forces_a_reload(tmp_path: Path) -> None:
    """Opening a gate has to take effect on the next line, not sixty seconds later."""
    store = await seeded_store(tmp_path / "s.db", freed=False)
    try:
        ledger = ChecksumLedger(store, 60.0, clock=lambda: 0.0)
        await ledger.maps()
        await store.mark_original_freed(ORIGINAL_ID)
        ledger.invalidate()
        assert (await ledger.maps()).sync_rewrite == {REPLACEMENT_ID: ORIGINAL_HASH}
    finally:
        await store.close()


async def test_ledger_reads_the_returned_originals_too(tmp_path: Path) -> None:
    """Both halves come from the same refresh, so they can never disagree by an interval."""
    store = await seeded_store(tmp_path / "s.db", freed=True, came_back=True)
    try:
        ledger = ChecksumLedger(store, 60.0, clock=lambda: 0.0)
        maps = await ledger.maps()
        assert maps.sync_rewrite == {}
        assert maps.claim_watch == {COPY_ID: [REPLACEMENT_ID]}

        # The copy goes; the translation it was holding back comes back on the next load.
        await store.mark_original_freed(COPY_ID)
        ledger.invalidate()
        assert (await ledger.maps()).sync_rewrite == {REPLACEMENT_ID: ORIGINAL_HASH}
    finally:
        await store.close()


async def test_ledger_keeps_serving_when_the_store_fails(tmp_path: Path) -> None:
    """A broken ledger read is a missed prevention, never a broken sync."""
    store = await seeded_store(tmp_path / "s.db", freed=True)
    ledger = ChecksumLedger(store, 0.0, clock=lambda: 0.0)
    try:
        assert (await ledger.maps()).sync_rewrite == {REPLACEMENT_ID: ORIGINAL_HASH}
        await store.close()  # every later query raises
        assert (await ledger.maps()).sync_rewrite == {REPLACEMENT_ID: ORIGINAL_HASH}
    finally:
        pass


# -------------------------------------------------------------------------- the routes


class Upstream:
    """A stand-in Immich. Records what it was asked, answers what the test set."""

    def __init__(self) -> None:
        self.status = 200
        self.lines: list[bytes] = []
        self.body: bytes = b""
        self.content_type = "application/jsonlines+json"
        # Extra response headers, for the relay rules. Immich really does send `date`.
        self.response_headers: dict[str, str] = {}
        self.seen_headers: httpx.Headers | None = None
        self.seen_body: bytes | None = None
        self.raise_error = False
        self.me: dict[str, Any] | None = {"id": OWNER}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/users/me":
            if self.me is None:
                return httpx.Response(401)
            return httpx.Response(200, json=self.me)
        if self.raise_error:
            raise httpx.ConnectError("upstream down", request=request)
        self.seen_headers = request.headers
        self.seen_body = request.content
        content = b"".join(self.lines) if self.lines else self.body
        headers = {"content-type": self.content_type, **self.response_headers}
        return httpx.Response(self.status, content=content, headers=headers)


def build(store: JobStore, upstream: Upstream, **overrides: Any) -> tuple[FastAPI, ShimDeps, list[str]]:
    touched: list[str] = []

    async def touch(asset_id: str) -> None:
        touched.append(asset_id)

    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler))
    deps = ShimDeps(
        upstream_url="http://immich-server:2283",
        rewrite_sync_stream=overrides.get("rewrite_sync_stream", True),
        rewrite_upload_check=overrides.get("rewrite_upload_check", True),
        watch_deletes=overrides.get("watch_deletes", True),
        log_only=overrides.get("log_only", False),
    )
    deps.client = client
    deps.store = store
    deps.ledger = ChecksumLedger(store, 0.0, clock=lambda: 0.0)
    deps.owners = OwnerResolver("http://immich-server:2283", client, 300.0, lambda: 0.0)
    deps.touch = touch

    app = FastAPI()
    app.include_router(build_router(deps))
    return app, deps, touched


async def test_sync_route_translates_and_counts(tmp_path: Path) -> None:
    store = await seeded_store(tmp_path / "s.db", freed=True)
    upstream = Upstream()
    upstream.lines = [asset_line(), asset_line("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", "keep-me=")]
    app, _, _ = build(store, upstream)
    try:
        with TestClient(app) as http:
            response = http.post("/api/sync/stream", json={"types": ["AssetsV2"]})
        assert response.status_code == 200
        first, second = [json.loads(line) for line in response.content.splitlines()]
        assert first["data"]["checksum"] == ORIGINAL_HASH
        assert second["data"]["checksum"] == "keep-me="

        counters = await store.counters()
        assert counters[SHIM_REQUESTS] == 1
        assert counters[SHIM_LINES_REWRITTEN] == 1
        assert counters[SHIM_HASHES_TRANSLATED] == 1
    finally:
        await store.close()


async def test_sync_route_forwards_the_callers_credentials_and_never_the_service_key(
    tmp_path: Path,
) -> None:
    """On a proxied route the shim is a pipe, not an authenticated caller."""
    store = await seeded_store(tmp_path / "s.db", freed=True)
    upstream = Upstream()
    upstream.lines = [asset_line()]
    app, _, _ = build(store, upstream)
    try:
        with TestClient(app) as http:
            http.post(
                "/api/sync/stream",
                json={},
                headers={"x-api-key": "the-callers-own-key", "accept-encoding": "gzip"},
            )
        assert upstream.seen_headers is not None
        assert upstream.seen_headers["x-api-key"] == "the-callers-own-key"
        # Overwritten, not just dropped: httpx would otherwise supply its own default and
        # the upstream would gzip a stream this code reads line by line.
        assert upstream.seen_headers["accept-encoding"] == "identity"
    finally:
        await store.close()


async def test_upstream_date_and_server_are_not_relayed(tmp_path: Path) -> None:
    """The ASGI server writes its own `Date` and `Server`, so relaying Immich's duplicates them.

    Uvicorn appends the application's headers to its own rather than replacing them, so a
    relayed `date` arrives as a second one of a field RFC 9110 defines as a singleton — with
    a different value, because the two hops read the clock a moment apart. nginx logged
    `upstream sent duplicate header line: "date: ..."` on every proxied request until this
    dropped them. `Server` is latent rather than observed — Immich sends `X-Powered-By` and no
    `Server` — but anything in front of it would duplicate the same way.

    Everything else Immich sets still comes through: this drops the two fields that describe
    the hop, not the response.
    """
    store = await seeded_store(tmp_path / "s.db", freed=True)
    upstream = Upstream()
    upstream.lines = [asset_line()]
    upstream.response_headers = {
        "date": "Mon, 01 Jan 2001 00:00:00 GMT",
        "server": "immich-upstream",
        "x-correlation-id": "abc123",
        "vary": "Accept-Encoding",
    }
    app, _, _ = build(store, upstream)
    try:
        with TestClient(app) as http:
            response = http.post("/api/sync/stream", json={})

        # `multi_items`, not `headers[...]`: the bug is a *duplicate*, and a mapping lookup
        # would happily return the first of two and call it a pass.
        relayed = [name.lower() for name, _ in response.headers.multi_items()]
        assert "date" not in relayed, f"the upstream Date must not be relayed: {relayed}"
        assert "server" not in relayed, f"the upstream Server must not be relayed: {relayed}"
        # The headers that describe the response, rather than the hop, are untouched.
        assert response.headers["x-correlation-id"] == "abc123"
        assert response.headers["vary"] == "Accept-Encoding"
        assert response.headers["content-type"] == "application/jsonlines+json"
    finally:
        await store.close()


async def test_sync_route_opens_the_gate_and_touches_the_replacement(tmp_path: Path) -> None:
    """The whole mechanism, end to end: purge seen, gate opened, replacement re-offered."""
    store = await seeded_store(tmp_path / "s.db", freed=False)
    upstream = Upstream()
    upstream.lines = [delete_line()]
    app, _, touched = build(store, upstream)
    try:
        with TestClient(app) as http:
            response = http.post("/api/sync/stream", json={})
        assert response.content == delete_line()

        job = await store.get(ORIGINAL_ID)
        assert job is not None and job.original_freed_at is not None
        assert touched == [REPLACEMENT_ID]
        counters = await store.counters()
        # Both, not one. An operator reading `shim_touches_total` is asking whether the
        # re-offer works, and the answer must not depend on which `delete_mode` produced
        # the touch — this path is the whole of it on a `trash` deployment.
        assert counters[SHIM_GATES_OPENED] == 1
        assert counters[SHIM_TOUCHES] == 1
    finally:
        await store.close()


async def test_a_failing_touch_leaves_the_gate_open(tmp_path: Path) -> None:
    """The ledger is still right; only the re-offer is missing. Never a client's problem."""
    store = await seeded_store(tmp_path / "s.db", freed=False)
    upstream = Upstream()
    upstream.lines = [delete_line()]
    app, deps, _ = build(store, upstream)

    async def explode(asset_id: str) -> None:
        raise RuntimeError("immich said no")

    deps.touch = explode
    try:
        with TestClient(app) as http:
            assert http.post("/api/sync/stream", json={}).status_code == 200
        job = await store.get(ORIGINAL_ID)
        assert job is not None and job.original_freed_at is not None
        counters = await store.counters()
        # The two counters part company exactly here, which is what makes the pair worth
        # reading: gates ahead of touches means the translation is armed but not delivered.
        assert counters[SHIM_GATES_OPENED] == 1
        assert counters[SHIM_TOUCHES] == 0
    finally:
        await store.close()


async def test_a_gate_the_pipeline_already_opened_is_counted_once(tmp_path: Path) -> None:
    """Both counters sit behind `mark_original_freed`, and it is first-write-wins.

    The collision is real on a deployment that switched `delete_mode`: the pipeline performs
    the permanent delete and opens that gate itself, and the purge for the same original
    still goes past on the sync stream — up to one refresh interval before the shim's ledger
    notices the row is already freed. The second observer must count nothing and touch
    nothing, or one permanent delete reads as two.
    """
    store = await seeded_store(tmp_path / "s.db", freed=False)
    upstream = Upstream()
    upstream.lines = [delete_line()]
    app, deps, touched = build(store, upstream)
    # A ledger that has read the closed gate and will not look again for an hour.
    deps.ledger = ChecksumLedger(store, 3600.0, clock=lambda: 0.0)
    assert set((await deps.ledger.maps()).delete_watch) == {ORIGINAL_ID}
    await store.mark_original_freed(ORIGINAL_ID)  # what the pipeline just did

    try:
        with TestClient(app) as http:
            assert http.post("/api/sync/stream", json={}).status_code == 200
        counters = await store.counters()
        assert counters[SHIM_GATES_OPENED] == 0
        assert counters[SHIM_TOUCHES] == 0
        assert touched == []
    finally:
        await store.close()


async def test_the_route_does_not_translate_while_the_copy_holds_the_checksum(tmp_path: Path) -> None:
    """The bug this exists to prevent, at the level a client would meet it.

    An open gate and a returned original together: the shim used to hand the checksum over
    anyway, and the phone's next batch died on ``UNIQUE constraint failed:
    remote_asset_entity.owner_id, remote_asset_entity.checksum``.
    """
    store = await seeded_store(tmp_path / "s.db", freed=True, came_back=True)
    upstream = Upstream()
    upstream.lines = [asset_line()]
    app, _, _ = build(store, upstream)
    try:
        with TestClient(app) as http:
            response = http.post("/api/sync/stream", json={})

        assert response.content == asset_line(), "byte-identical, not merely equivalent"
        counters = await store.counters()
        assert counters[SHIM_LINES_REWRITTEN] == 0
        assert counters[SHIM_HASHES_TRANSLATED] == 0
    finally:
        await store.close()


async def test_the_route_releases_a_claim_and_touches_what_it_unblocked(tmp_path: Path) -> None:
    """The re-arm, end to end: the copy is deleted, the replacement is re-offered.

    Without the touch this would be inert for exactly the reason `_open_gates` needs one —
    nothing has changed about the replacement, so it would never be sent again and the
    translation would never reach the device.
    """
    store = await seeded_store(tmp_path / "s.db", freed=True, came_back=True)
    upstream = Upstream()
    upstream.lines = [delete_line(COPY_ID)]
    app, _, touched = build(store, upstream)
    try:
        with TestClient(app) as http:
            response = http.post("/api/sync/stream", json={})
        assert response.content == delete_line(COPY_ID)

        copy = await store.get(COPY_ID)
        assert copy is not None and copy.original_freed_at is not None
        assert touched == [REPLACEMENT_ID]
        counters = await store.counters()
        assert counters[SHIM_TOUCHES] == 1
        # No gate opens here. The original's gate opened when the original died; this only
        # lifts what was standing on top of it, and counting it twice would overstate the
        # number of originals this service has seen go.
        assert counters[SHIM_GATES_OPENED] == 0

        assert (await store.returned_originals()) == []
    finally:
        await store.close()


async def test_a_claim_already_released_is_not_touched_twice(tmp_path: Path) -> None:
    """First-write-wins, from the second observer's side.

    Two devices sync at once and both see the same delete go past. Whichever gets there
    second is holding maps built before the release, so it still believes the copy is live —
    and must count nothing and touch nothing, or one deleted copy reads as two.
    """
    store = await seeded_store(tmp_path / "s.db", freed=True, came_back=True)
    upstream = Upstream()
    upstream.lines = [delete_line(COPY_ID)]
    app, deps, touched = build(store, upstream)
    try:
        # A ledger that has read the suppression and will not look again for an hour.
        deps.ledger = ChecksumLedger(store, 3600.0, clock=lambda: 0.0)
        assert (await deps.ledger.maps()).claim_watch == {COPY_ID: [REPLACEMENT_ID]}
        await store.mark_original_freed(COPY_ID)  # what the other stream just did

        with TestClient(app) as http:
            assert http.post("/api/sync/stream", json={}).status_code == 200
        assert touched == []
        assert (await store.counters())[SHIM_TOUCHES] == 0
    finally:
        await store.close()


async def test_a_failing_touch_still_leaves_the_claim_released(tmp_path: Path) -> None:
    """The record is the durable part; the re-offer is best effort, as it is for a gate."""
    store = await seeded_store(tmp_path / "s.db", freed=True, came_back=True)
    upstream = Upstream()
    upstream.lines = [delete_line(COPY_ID)]
    app, deps, _ = build(store, upstream)

    async def explode(asset_id: str) -> None:
        raise RuntimeError("immich said no")

    deps.touch = explode
    try:
        with TestClient(app) as http:
            assert http.post("/api/sync/stream", json={}).status_code == 200
        copy = await store.get(COPY_ID)
        assert copy is not None and copy.original_freed_at is not None
        assert (await store.counters())[SHIM_TOUCHES] == 0
    finally:
        await store.close()


async def test_log_only_does_not_release_a_claim(tmp_path: Path) -> None:
    store = await seeded_store(tmp_path / "s.db", freed=True, came_back=True)
    upstream = Upstream()
    upstream.lines = [delete_line(COPY_ID)]
    app, _, touched = build(store, upstream, log_only=True)
    try:
        with TestClient(app) as http:
            http.post("/api/sync/stream", json={})
        copy = await store.get(COPY_ID)
        assert copy is not None and copy.original_freed_at is None
        assert touched == []
    finally:
        await store.close()


async def test_log_only_changes_nothing_at_all(tmp_path: Path) -> None:
    store = await seeded_store(tmp_path / "s.db", freed=True)
    upstream = Upstream()
    upstream.lines = [asset_line(), delete_line()]
    app, _, touched = build(store, upstream, log_only=True)
    try:
        with TestClient(app) as http:
            response = http.post("/api/sync/stream", json={})
        assert response.content == asset_line() + delete_line()
        assert touched == []
        assert (await store.counters())[SHIM_LINES_REWRITTEN] == 0
    finally:
        await store.close()


async def test_log_only_does_not_open_a_gate(tmp_path: Path) -> None:
    store = await seeded_store(tmp_path / "s.db", freed=False)
    upstream = Upstream()
    upstream.lines = [delete_line()]
    app, _, _ = build(store, upstream, log_only=True)
    try:
        with TestClient(app) as http:
            http.post("/api/sync/stream", json={})
        job = await store.get(ORIGINAL_ID)
        assert job is not None and job.original_freed_at is None
    finally:
        await store.close()


async def test_sync_route_passes_an_upstream_error_through(tmp_path: Path) -> None:
    store = await seeded_store(tmp_path / "s.db", freed=True)
    upstream = Upstream()
    upstream.status = 503
    upstream.body = b"immich is restarting"
    app, _, _ = build(store, upstream)
    try:
        with TestClient(app) as http:
            response = http.post("/api/sync/stream", json={})
        assert response.status_code == 503
        assert response.content == b"immich is restarting"
    finally:
        await store.close()


async def test_sync_route_reports_an_unreachable_upstream(tmp_path: Path) -> None:
    store = await seeded_store(tmp_path / "s.db", freed=True)
    upstream = Upstream()
    upstream.raise_error = True
    app, _, _ = build(store, upstream)
    try:
        with TestClient(app) as http:
            assert http.post("/api/sync/stream", json={}).status_code == 502
        assert (await store.counters())[SHIM_PASSTHROUGH_ERRORS] == 1
    finally:
        await store.close()


async def test_a_line_split_across_chunks_is_still_translated(tmp_path: Path) -> None:
    """The stream is buffered by line, and Immich does not align lines to TCP chunks."""
    store = await seeded_store(tmp_path / "s.db", freed=True)
    upstream = Upstream()
    whole = asset_line()
    upstream.lines = [whole[:20], whole[20:]]
    app, _, _ = build(store, upstream)
    try:
        with TestClient(app) as http:
            response = http.post("/api/sync/stream", json={})
        assert json.loads(response.content)["data"]["checksum"] == ORIGINAL_HASH
    finally:
        await store.close()


async def test_a_final_line_without_a_newline_is_still_emitted(tmp_path: Path) -> None:
    store = await seeded_store(tmp_path / "s.db", freed=True)
    upstream = Upstream()
    upstream.lines = [asset_line().rstrip(b"\n")]
    app, _, _ = build(store, upstream)
    try:
        with TestClient(app) as http:
            response = http.post("/api/sync/stream", json={})
        assert json.loads(response.content)["data"]["checksum"] == ORIGINAL_HASH
    finally:
        await store.close()


async def test_upload_check_route_rewrites_the_request(tmp_path: Path) -> None:
    store = await seeded_store(tmp_path / "s.db", freed=False)
    upstream = Upstream()
    upstream.body = json.dumps({"results": []}).encode()
    upstream.content_type = "application/json"
    app, _, _ = build(store, upstream)
    try:
        with TestClient(app) as http:
            response = http.post(
                "/api/assets/bulk-upload-check",
                json={"assets": [{"id": "local-1", "checksum": ORIGINAL_HASH}]},
                headers={"x-api-key": "callers-key"},
            )
        assert response.status_code == 200
        assert upstream.seen_body is not None
        assert json.loads(upstream.seen_body)["assets"][0]["checksum"] == REPLACEMENT_HASH
        # The response is Immich's own verdict, forwarded untouched.
        assert response.content == upstream.body
    finally:
        await store.close()


async def test_upload_check_passes_through_when_the_owner_is_unknown(tmp_path: Path) -> None:
    store = await seeded_store(tmp_path / "s.db", freed=True)
    upstream = Upstream()
    upstream.me = None  # /users/me refuses the credential
    upstream.body = b"{}"
    app, _, _ = build(store, upstream)
    body = {"assets": [{"id": "local-1", "checksum": ORIGINAL_HASH}]}
    try:
        with TestClient(app) as http:
            http.post("/api/assets/bulk-upload-check", json=body, headers={"x-api-key": "k"})
        assert upstream.seen_body is not None
        assert json.loads(upstream.seen_body)["assets"][0]["checksum"] == ORIGINAL_HASH
    finally:
        await store.close()


async def test_upload_check_can_be_switched_off_alone(tmp_path: Path) -> None:
    store = await seeded_store(tmp_path / "s.db", freed=True)
    upstream = Upstream()
    upstream.body = b"{}"
    app, _, _ = build(store, upstream, rewrite_upload_check=False)
    body = {"assets": [{"id": "local-1", "checksum": ORIGINAL_HASH}]}
    try:
        with TestClient(app) as http:
            http.post("/api/assets/bulk-upload-check", json=body, headers={"x-api-key": "k"})
        assert upstream.seen_body is not None
        assert json.loads(upstream.seen_body)["assets"][0]["checksum"] == ORIGINAL_HASH
    finally:
        await store.close()


def test_describe_names_the_live_routes() -> None:
    from immich_compressor.config import ShimSettings

    line = describe(ShimSettings(enabled=True, log_only=True))
    assert "log_only" in line
    assert "/api/sync/stream" in line
    assert "http://immich-server:2283" in line


# --------------------------------------------------------- mounting, through create_app


def test_the_routes_do_not_exist_when_the_shim_is_off(settings: Any) -> None:
    """Inert means absent, not merely inactive.

    A route that existed and merely passed through would still put this service in the path
    of every sync request, where a bug in it could break a client. Off means Immich's own
    routes are the only ones there are — and a reverse proxy misconfigured to send traffic
    here gets an unmistakable 404 rather than a silent pass-through.
    """
    from immich_compressor.server import create_app

    with TestClient(create_app(settings)) as http:
        assert http.post("/api/sync/stream", json={}).status_code == 404
        assert http.post("/api/assets/bulk-upload-check", json={"assets": []}).status_code == 404


def test_the_routes_are_mounted_and_unauthenticated_when_the_shim_is_on(settings: Any) -> None:
    """502, not 404 and not 401.

    404 would mean the routes never mounted. 401 would mean they wanted this service's
    shared secret — which no Immich client knows, so every sync request from every phone
    would fail and the library would go dark. 502 is the honest answer here: mounted,
    authenticated by passthrough, and unable to reach the upstream that does not exist in
    this test.
    """
    from immich_compressor.server import create_app

    settings.shim.enabled = True
    settings.shim.connect_timeout_s = 0.05
    with TestClient(create_app(settings)) as http:
        assert http.post("/api/sync/stream", json={}).status_code == 502
        assert http.post("/api/assets/bulk-upload-check", json={"assets": []}).status_code == 502
