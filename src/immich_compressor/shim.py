"""The checksum-translation shim: two Immich routes proxied through this service.

The problem it solves is one sentence long. The Immich app decides what to back up by
comparing the SHA-1 of each local file against the checksums it has mirrored from the
server, entirely offline; when this service replaces an asset and the original is
eventually hard-deleted, that checksum stops existing anywhere, the phone's local file
matches nothing, and it uploads the original again. The ledger on ``jobs`` already lets the
pipeline *recognise* that (``re_uploaded``), but recognising it does not stop it: the bytes
still cross the network and the duplicate still lands in the library.

So: where the sync stream hands the phone the replacement, substitute the original's
checksum into that one field. The phone finds a match and never queues the file.

One invariant governs the whole module. **At most one mirrored row may hold a given
checksum at any moment** — the app's mirror carries a partial UNIQUE index on
``(owner_id, checksum)``. The original's own row holds it until the original is really
gone, so the replacement may take it only after that, never before. That is what
``LedgerEntry.gate_is_open`` means, and translating with a closed gate does not merely fail
to help: it either silently destroys the original's mirror row or aborts the phone's whole
sync batch with a constraint violation.

Everything here fails open. A parse error, a ledger error, an unreachable upstream — all of
them forward what Immich said, unchanged. A shim that breaks sync is worse than the problem
it solves.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from .models import LedgerEntry
from .store import (
    SHIM_GATES_OPENED,
    SHIM_HASHES_TRANSLATED,
    SHIM_LINES_REWRITTEN,
    SHIM_PASSTHROUGH_ERRORS,
    SHIM_REQUESTS,
    SHIM_TOUCHES,
    JobStore,
)

logger = logging.getLogger(__name__)

SYNC_STREAM_PATH = "/api/sync/stream"
UPLOAD_CHECK_PATH = "/api/assets/bulk-upload-check"

# The sync stream's media type. Preserved verbatim on the way back out.
JSONLINES = "application/jsonlines+json"

# Hop-by-hop headers, which belong to one connection and must not be relayed onto another.
# `Content-Length` goes with them on the response side because the rewrite changes it and
# the stream is chunked anyway; `Content-Encoding` because the request asks for identity,
# so what comes back is uncompressed and claiming otherwise would be a lie.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "transfer-encoding",
        "upgrade",
        "te",
        "trailer",
        "proxy-authenticate",
        "proxy-authorization",
    }
)
_DROP_FROM_RESPONSE = _HOP_BY_HOP | {"content-length", "content-encoding"}
# Replaced rather than relayed on the forwarded request — see `_forward_request_headers`.
_DROP_FROM_REQUEST = _HOP_BY_HOP | {"host", "accept-encoding", "content-length"}

# Sync line types that carry an asset payload with a checksum. Deliberately *not* used as
# an allowlist — see `rewrite_sync_line`. Kept only for the delete watch, which needs an
# exact type match because `AssetDeleteV1` is the one event that means "the original is
# gone for good".
DELETE_TYPES = frozenset({"AssetDeleteV1", "PartnerAssetDeleteV1"})


@dataclass(slots=True)
class TranslationMaps:
    """The three lookups a request needs, built from the ledger in one pass.

    A full sync stream is thousands of lines and every one of them asks the same two
    questions, so both are dict hits. The maps are one small entry per replaced asset.
    """

    # new_asset_id -> the original's checksum. Only gates that are open.
    sync_rewrite: dict[str, str] = field(default_factory=dict)
    # source_asset_id -> the entry whose gate that delete would open. Only closed gates.
    delete_watch: dict[str, LedgerEntry] = field(default_factory=dict)
    # (owner_id, original checksum) -> the replacement's checksum. Ungated: this one never
    # writes to a mirror, it only changes which hash a question is asked about.
    upload_check: dict[tuple[str, str], str] = field(default_factory=dict)

    @classmethod
    def build(cls, entries: list[LedgerEntry]) -> TranslationMaps:
        maps = cls()
        for entry in entries:
            if entry.gate_is_open:
                maps.sync_rewrite[entry.new_asset_id] = entry.source_checksum
            else:
                maps.delete_watch[entry.source_asset_id] = entry
            if entry.new_checksum:
                maps.upload_check[(entry.owner_id, entry.source_checksum)] = entry.new_checksum
        return maps


@dataclass(slots=True)
class LineOutcome:
    """What `rewrite_sync_line` did with one line."""

    data: bytes
    rewritten: bool = False
    # Ledger entries whose gate this line should open, i.e. originals whose purge just
    # went past. Returned rather than acted on so the rewrite stays a pure function.
    gate_opens: tuple[LedgerEntry, ...] = ()


def rewrite_sync_line(line: bytes, maps: TranslationMaps, *, translate: bool = True) -> LineOutcome:
    """Translate one JSON Lines record of ``POST /api/sync/stream``.

    Pure, and total: every input produces an output, and anything unrecognised comes back
    byte-identical. A line is never dropped and ``ack`` is never touched — that field is
    the client's resume cursor, and the client acks only the last line of each run of
    same-typed lines, so removing one would stall a checkpoint permanently.

    Matching is on ``data.id`` plus the presence of a string ``data.checksum``, not on a
    list of line types. The type names have already churned once: ``AssetV1`` and
    ``PartnerAssetV1`` were dropped from the server's map while six V2 spellings carry the
    same payload. An id and a checksum are what actually matter, and they do not go stale
    on the next Immich release.
    """
    stripped = line.strip()
    if not stripped:
        return LineOutcome(line)
    try:
        record = json.loads(stripped)
    except (ValueError, UnicodeDecodeError):
        # Not JSON, or not JSON we understand. Immich said it; the client gets it.
        return LineOutcome(line)
    if not isinstance(record, dict):
        return LineOutcome(line)

    data = record.get("data")
    if not isinstance(data, dict):
        return LineOutcome(line)

    if record.get("type") in DELETE_TYPES:
        asset_id = data.get("assetId")
        entry = maps.delete_watch.get(asset_id) if isinstance(asset_id, str) else None
        # Emitted unchanged either way: the phone still has to learn the original is gone.
        return LineOutcome(line, gate_opens=(entry,) if entry else ())

    asset_id = data.get("id")
    checksum = data.get("checksum")
    if not translate or not isinstance(asset_id, str) or not isinstance(checksum, str):
        return LineOutcome(line)
    original = maps.sync_rewrite.get(asset_id)
    if original is None or original == checksum:
        return LineOutcome(line)

    data["checksum"] = original
    return LineOutcome(json.dumps(record, separators=(",", ":")).encode() + b"\n", rewritten=True)


def translate_upload_check(body: bytes, owner_id: str | None, maps: TranslationMaps) -> tuple[bytes, int]:
    """Rewrite the checksums in a ``bulk-upload-check`` *request*.

    The client asks "do you already have these hashes?" about originals this service has
    replaced; the honest answer is about their replacements, so the question is restated
    and Immich answers it. The response is forwarded untouched, which is what makes this
    self-healing: if the replacement has itself been deleted, Immich says ``accept`` and
    the client uploads, which is correct.

    Needs no gate. It never writes to any mirror.

    Returns the body to forward and how many hashes were translated; on anything
    unexpected it returns the original body and zero.
    """
    if owner_id is None:
        return body, 0
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return body, 0
    if not isinstance(payload, dict):
        return body, 0
    assets = payload.get("assets")
    if not isinstance(assets, list):
        return body, 0

    translated = 0
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        checksum = asset.get("checksum")
        if not isinstance(checksum, str):
            continue
        replacement = maps.upload_check.get((owner_id, checksum))
        if replacement and replacement != checksum:
            asset["checksum"] = replacement
            translated += 1
    if not translated:
        return body, 0
    return json.dumps(payload, separators=(",", ":")).encode(), translated


class ChecksumLedger:
    """The translation maps, refreshed from the store on a timer.

    Rebuilt wholesale rather than invalidated per row: the query is one table scan of a
    partial index and the result is small, and a cache that can be subtly stale in one
    direction is exactly the kind of thing that makes a rewrite bug unreproducible.
    """

    def __init__(self, store: JobStore, refresh_seconds: float, *, clock: Callable[[], float]) -> None:
        self._store = store
        self._refresh_seconds = refresh_seconds
        self._clock = clock
        self._maps = TranslationMaps()
        self._loaded_at: float | None = None

    async def maps(self) -> TranslationMaps:
        now = self._clock()
        if self._loaded_at is None or now - self._loaded_at >= self._refresh_seconds:
            try:
                self._maps = TranslationMaps.build(await self._store.ledger_entries())
            except Exception:
                # Keep serving the previous maps. A stale translation is a missed
                # prevention; a raised exception here would be a broken sync.
                logger.exception("shim: could not refresh the ledger, keeping the previous maps")
            self._loaded_at = now
        return self._maps

    def invalidate(self) -> None:
        """Force the next `maps()` to reload — used after a gate opens."""
        self._loaded_at = None


class OwnerResolver:
    """Maps a caller's credentials to their Immich user id, with a short-lived cache.

    Needed only by the upload-check direction, whose ledger lookups are scoped by owner
    because Immich's own uniqueness constraint is. The cache is keyed by a hash of the
    credential, never the credential itself, so a log line or a heap dump does not hand
    somebody an API key.
    """

    def __init__(
        self, upstream_url: str, client: httpx.AsyncClient, ttl_seconds: float, clock: Callable[[], float]
    ) -> None:
        self._upstream_url = upstream_url
        self._client = client
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache: dict[str, tuple[float, str | None]] = {}

    @staticmethod
    def _key(headers: httpx.Headers) -> str | None:
        import hashlib

        material = "\n".join(headers.get(name, "") for name in ("authorization", "x-api-key", "cookie"))
        if not material.strip():
            return None
        return hashlib.sha256(material.encode()).hexdigest()

    async def resolve(self, headers: httpx.Headers) -> str | None:
        key = self._key(headers)
        if key is None:
            return None
        now = self._clock()
        cached = self._cache.get(key)
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]
        owner: str | None = None
        try:
            response = await self._client.get(
                f"{self._upstream_url}/api/users/me",
                headers={
                    name: value
                    for name, value in headers.items()
                    if name.lower() in {"authorization", "x-api-key", "cookie"}
                },
            )
            if response.status_code == 200:
                body = response.json()
                if isinstance(body, dict) and isinstance(body.get("id"), str):
                    owner = body["id"]
        except (httpx.HTTPError, ValueError):
            # Unresolved owner means "translate nothing", which is the safe direction.
            logger.debug("shim: could not resolve the caller's user id", exc_info=True)
        self._cache[key] = (now, owner)
        return owner


def _forward_request_headers(request: Request) -> dict[str, str]:
    """The client's headers, minus what belongs to this hop.

    The client's own credentials go through verbatim and the service's API key is never
    added: on these two routes the shim is a pipe, not an authenticated caller.

    ``Accept-Encoding`` is not merely dropped but overwritten with ``identity``. Dropping
    it is not enough — httpx supplies its own default when the header is absent, and the
    upstream would then compress a stream this code reads line by line and re-emits
    uncompressed. Asking for identity keeps the wire format and the parsed format the same
    thing, and saves compressing bytes nobody will ever decompress.
    """
    headers = {
        name: value for name, value in request.headers.items() if name.lower() not in _DROP_FROM_REQUEST
    }
    headers["accept-encoding"] = "identity"
    return headers


def _response_headers(upstream: httpx.Response) -> dict[str, str]:
    return {
        name: value for name, value in upstream.headers.items() if name.lower() not in _DROP_FROM_RESPONSE
    }


@dataclass(slots=True)
class ShimDeps:
    """Everything the routes need, injected so the tests can drive them directly.

    The four runtime handles default to ``None`` because the routes are mounted when the
    app is built and the store, the Immich client and the proxy's connection pool only
    exist once it is running. ASGI startup completes before the first request, so a route
    never observes one of them unset — the ``assert`` in `_deps` says so out loud rather
    than letting a half-built shim fail somewhere less obvious.
    """

    upstream_url: str
    rewrite_sync_stream: bool
    rewrite_upload_check: bool
    watch_deletes: bool
    log_only: bool
    client: httpx.AsyncClient | None = None
    ledger: ChecksumLedger | None = None
    owners: OwnerResolver | None = None
    store: JobStore | None = None
    # Called with the replacement's asset id when a gate opens. The touch that makes the
    # server re-offer it; injected because it is an authenticated write and belongs to the
    # Immich client, not to the proxy.
    touch: Callable[[str], Awaitable[None]] | None = None

    def ready(self) -> _Ready:
        """The same dependencies, with the runtime handles known to be present."""
        assert self.client is not None, "shim used before startup filled in its client"
        assert self.ledger is not None, "shim used before startup filled in its ledger"
        assert self.owners is not None, "shim used before startup filled in its resolver"
        assert self.store is not None, "shim used before startup filled in its store"
        return _Ready(self, self.client, self.ledger, self.owners, self.store)


@dataclass(slots=True, frozen=True)
class _Ready:
    """A `ShimDeps` whose runtime handles are non-optional, for the type checker."""

    deps: ShimDeps
    client: httpx.AsyncClient
    ledger: ChecksumLedger
    owners: OwnerResolver
    store: JobStore


async def _open_gates(ready: _Ready, entries: tuple[LedgerEntry, ...]) -> None:
    """Record that these originals are gone, and get their replacements re-offered.

    Without the touch this is inert. The sync stream only offers assets whose ``updateId``
    is newer than the client's checkpoint, and nothing has updated the replacement since
    it was created — so the line the shim wants to rewrite would never be sent again.

    Both counters are bumped here, exactly as the pipeline bumps both on the ``permanent``
    path: a counter names the event, not the module that saw it. Neither can be counted
    twice, because both sit behind `JobStore.mark_original_freed`.
    """
    deps = ready.deps
    for entry in entries:
        if deps.log_only:
            logger.info(
                "shim (log_only): would open the gate for %s, replaced by %s",
                entry.source_asset_id,
                entry.new_asset_id,
            )
            continue
        if not await ready.store.mark_original_freed(entry.source_asset_id):
            continue
        await ready.store.bump_counter(SHIM_GATES_OPENED)
        ready.ledger.invalidate()
        logger.info(
            "original %s is gone for good; translating %s to its checksum from now on",
            entry.source_asset_id,
            entry.new_asset_id,
        )
        try:
            assert deps.touch is not None
            await deps.touch(entry.new_asset_id)
        except Exception:
            # The gate stays open and the ledger stays correct; only the re-offer is
            # missing, and the next update to that asset supplies it. Not worth failing a
            # client's sync over.
            logger.warning(
                "shim: could not touch %s to have it re-sent; the translation is armed but "
                "the client may not see it until that asset changes for another reason",
                entry.new_asset_id,
                exc_info=True,
            )
        else:
            # In the `else`, not the `try`: a store error here is not a failed touch, and
            # logging it as one would send an operator after the wrong thing entirely.
            await ready.store.bump_counter(SHIM_TOUCHES)


async def _stream_sync(ready: _Ready, upstream: httpx.Response) -> AsyncIterator[bytes]:
    """Rewrite the JSON Lines response as it arrives, one line at a time.

    Buffered by line, never whole: a full sync of a large library is far too big to hold,
    and the client starts applying batches long before the response ends.
    """
    deps = ready.deps
    translate = deps.rewrite_sync_stream and not deps.log_only
    pending = b""
    rewritten = 0
    gate_opens: list[LedgerEntry] = []
    try:
        maps = await ready.ledger.maps()
        async for chunk in upstream.aiter_bytes():
            pending += chunk
            while b"\n" in pending:
                raw, pending = pending.split(b"\n", 1)
                outcome = rewrite_sync_line(raw + b"\n", maps, translate=translate)
                rewritten += outcome.rewritten
                if deps.watch_deletes:
                    gate_opens.extend(outcome.gate_opens)
                yield outcome.data
        if pending:
            outcome = rewrite_sync_line(pending, maps, translate=translate)
            rewritten += outcome.rewritten
            if deps.watch_deletes:
                gate_opens.extend(outcome.gate_opens)
            yield outcome.data
    except httpx.HTTPError:
        logger.warning("shim: the sync stream ended early", exc_info=True)
        await ready.store.bump_counter(SHIM_PASSTHROUGH_ERRORS)
        if pending:
            yield pending
    finally:
        await upstream.aclose()

    if rewritten:
        await ready.store.bump_counter(SHIM_LINES_REWRITTEN, rewritten)
        await ready.store.bump_counter(SHIM_HASHES_TRANSLATED, rewritten)
    if gate_opens:
        await _open_gates(ready, tuple(gate_opens))


def build_router(deps: ShimDeps) -> APIRouter:
    """The two proxied routes. Mounted only when ``shim.enabled``."""
    router = APIRouter()

    @router.post(SYNC_STREAM_PATH)
    async def sync_stream(request: Request) -> Response:
        ready = deps.ready()
        await ready.store.bump_counter(SHIM_REQUESTS)
        body = await request.body()
        upstream_request = ready.client.build_request(
            "POST",
            f"{deps.upstream_url}{request.url.path}",
            params=dict(request.query_params),
            headers=_forward_request_headers(request),
            content=body,
            # The stream is long-lived by design: the server holds it open while it walks
            # the library. A read timeout here would cut off a large first sync.
            timeout=httpx.Timeout(connect=ready.client.timeout.connect, read=None, write=None, pool=None),
        )
        try:
            upstream = await ready.client.send(upstream_request, stream=True)
        except httpx.HTTPError:
            logger.warning("shim: sync stream upstream unreachable", exc_info=True)
            await ready.store.bump_counter(SHIM_PASSTHROUGH_ERRORS)
            return Response(status_code=502, content=b"upstream unreachable")

        return StreamingResponse(
            _stream_sync(ready, upstream),
            status_code=upstream.status_code,
            headers=_response_headers(upstream),
            media_type=upstream.headers.get("content-type", JSONLINES),
        )

    @router.post(UPLOAD_CHECK_PATH)
    async def bulk_upload_check(request: Request) -> Response:
        ready = deps.ready()
        await ready.store.bump_counter(SHIM_REQUESTS)
        body = await request.body()
        headers = _forward_request_headers(request)
        forwarded = body
        if deps.rewrite_upload_check:
            maps = await ready.ledger.maps()
            owner = await ready.owners.resolve(httpx.Headers(headers))
            candidate, translated = translate_upload_check(body, owner, maps)
            if translated:
                await ready.store.bump_counter(SHIM_HASHES_TRANSLATED, translated)
                if deps.log_only:
                    logger.info("shim (log_only): would translate %d checksum(s)", translated)
                else:
                    forwarded = candidate
        try:
            upstream = await ready.client.post(
                f"{deps.upstream_url}{request.url.path}",
                params=dict(request.query_params),
                headers=headers,
                content=forwarded,
            )
        except httpx.HTTPError:
            logger.warning("shim: bulk-upload-check upstream unreachable", exc_info=True)
            await ready.store.bump_counter(SHIM_PASSTHROUGH_ERRORS)
            return Response(status_code=502, content=b"upstream unreachable")

        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=_response_headers(upstream),
            media_type=upstream.headers.get("content-type"),
        )

    return router


def describe(settings: Any) -> str:
    """One startup line: which routes are live and where they point."""
    routes = []
    if settings.rewrite_sync_stream:
        routes.append(SYNC_STREAM_PATH)
    if settings.rewrite_upload_check:
        routes.append(UPLOAD_CHECK_PATH)
    mode = "log_only (nothing is altered)" if settings.log_only else "translating"
    return (
        f"shim {mode}, proxying {', '.join(routes) or 'nothing'} to {settings.upstream_url}"
        f"{', watching deletes' if settings.watch_deletes else ''}"
    )
