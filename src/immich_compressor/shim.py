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
``(owner_id, checksum)``. Translating in breach of it does not merely fail to help: it
either silently destroys the other mirror row or aborts the phone's whole sync batch with
a constraint violation.

Two things can hold the checksum, so the shim waits on both:

- **The original itself**, until it is really gone. That is ``LedgerEntry.gate_is_open``,
  and it is set once, when the delete is observed.
- **A copy of the original that came back afterwards.** The gate opening frees the
  checksum; a device that still held the file can put it straight back, and the pipeline
  lets that stand (``re_uploaded`` recognises it and touches nothing). The checksum is
  then live again under a new id, and the translation has to stand down until *that* asset
  is deleted in turn. That is :class:`~immich_compressor.models.ReturnedOriginal`.

An open gate is not a licence to translate for ever. It records that one asset died once;
the rule is about the checksum, and any asset can hold that.

Everything here fails open. A parse error, a ledger error, an unreachable upstream — all of
them forward what Immich said, unchanged. A shim that breaks sync is worse than the problem
it solves.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from .models import LedgerEntry, ReturnedOriginal
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
# `Date` and `Server` join them because the ASGI server emits its own of each and appends
# the application's afterwards rather than replacing them — measured, not assumed: an app
# returning both gets two of both on the wire. Relaying Immich's `Date` therefore produced
# two of a field RFC 9110 defines as a singleton, whose values disagreed by a second, and
# nginx logged `upstream sent duplicate header line: "date: ..."` on every proxied request.
# `Server` is dropped for the same reason and not because Immich sends one — it does not,
# so that half is latent, but a `Server` arriving from anything in front of Immich would
# duplicate exactly the same way. Both describe the hop that answered, which is this one.
_DROP_FROM_RESPONSE = _HOP_BY_HOP | {"content-length", "content-encoding", "date", "server"}
# Replaced rather than relayed on the forwarded request — see `_forward_request_headers`.
_DROP_FROM_REQUEST = _HOP_BY_HOP | {"host", "accept-encoding", "content-length"}

# Sync line types that carry an asset payload with a checksum. Deliberately *not* used as
# an allowlist — see `rewrite_sync_line`. Kept only for the delete watch, which needs an
# exact type match because `AssetDeleteV1` is the one event that means "the original is
# gone for good".
DELETE_TYPES = frozenset({"AssetDeleteV1", "PartnerAssetDeleteV1"})


@dataclass(slots=True)
class TranslationMaps:
    """The five lookups a request needs, built from the store in one pass.

    A full sync stream is thousands of lines and every one of them asks the same two
    questions, so both are dict hits. The maps are one small entry per replaced asset.
    """

    # new_asset_id -> the original's checksum. Only gates that are open, and only while
    # nothing else holds that checksum.
    sync_rewrite: dict[str, str] = field(default_factory=dict)
    # source_asset_id -> the entry whose gate that delete would open. Only closed gates.
    delete_watch: dict[str, LedgerEntry] = field(default_factory=dict)
    # (owner_id, original checksum) -> the replacement's checksum. Ungated: this one never
    # writes to a mirror, it only changes which hash a question is asked about.
    upload_check: dict[tuple[str, str], str] = field(default_factory=dict)
    # asset id of a returned original -> the replacements whose translation it holds back.
    # A delete for one of these frees the checksum and re-arms them. The value is empty
    # when it currently blocks nothing, which is not the same as being absent: the row
    # still has to stop suppressing once its asset goes, or a gate that opens later would
    # find itself blocked by an asset nobody has.
    claim_watch: dict[str, list[str]] = field(default_factory=dict)
    # (owner_id, original checksum) -> the replacements armed to be handed that checksum.
    # The reverse of `sync_rewrite`, and the only thing that lets a line be recognised as
    # somebody else's claim on a hash this shim is about to give away. Armed entries only:
    # a checksum nothing is waiting for cannot be taken from anyone.
    armed: dict[tuple[str, str], list[str]] = field(default_factory=dict)

    @property
    def suppressed(self) -> int:
        """How many translations a returned original is currently holding back."""
        return sum(len(blocked) for blocked in self.claim_watch.values())

    @classmethod
    def build(cls, entries: list[LedgerEntry], returned: Sequence[ReturnedOriginal] = ()) -> TranslationMaps:
        maps = cls()
        live: dict[tuple[str, str], list[str]] = {}
        for holder in returned:
            if holder.asset_id in maps.claim_watch:
                # The same asset from both sources — the store's row and the shim's own
                # sighting of it on a stream. One claim, counted once, or `suppressed`
                # reports double and an operator reads a number that means nothing.
                continue
            live.setdefault((holder.owner_id, holder.checksum), []).append(holder.asset_id)
            maps.claim_watch[holder.asset_id] = []
        for entry in entries:
            key = (entry.owner_id, entry.source_checksum)
            if not entry.gate_is_open:
                maps.delete_watch[entry.source_asset_id] = entry
            elif key in live:
                # The gate opened, and then the checksum came back on a new asset. Waiting
                # is the only safe move: the phone's mirror has room for one row per key.
                for asset_id in live[key]:
                    maps.claim_watch[asset_id].append(entry.new_asset_id)
            else:
                maps.sync_rewrite[entry.new_asset_id] = entry.source_checksum
                maps.armed.setdefault(key, []).append(entry.new_asset_id)
            if entry.new_checksum:
                maps.upload_check[key] = entry.new_checksum
        return maps


@dataclass(slots=True)
class LineOutcome:
    """What `rewrite_sync_line` did with one line."""

    data: bytes
    rewritten: bool = False
    # Ledger entries whose gate this line should open, i.e. originals whose purge just
    # went past. Returned rather than acted on so the rewrite stays a pure function.
    gate_opens: tuple[LedgerEntry, ...] = ()
    # Asset ids of returned originals this line says are gone, so whatever they were
    # holding back can be armed again. Returned rather than acted on for the same reason.
    claims_released: tuple[str, ...] = ()
    # Assets this line says are holding a checksum the shim is armed to hand to somebody
    # else. The store learns this minutes later, when the pipeline reaches the job; the
    # line itself says it now. Returned rather than acted on for the same reason again.
    claims_observed: tuple[ReturnedOriginal, ...] = ()


def rewrite_sync_line(
    line: bytes,
    maps: TranslationMaps,
    *,
    translate: bool = True,
    claimed: Collection[tuple[str, str]] = (),
) -> LineOutcome:
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

    ``claimed`` is the set of ``(owner_id, checksum)`` pairs already known to be held by
    some other asset, and a translation into one of them is withheld. The caller owns that
    set and adds to it as `claims_observed` comes back, which is what makes suppression
    take effect part-way through a response; purity is unaffected, because what this
    returns is still a function of what it was passed.
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
        if not isinstance(asset_id, str):
            return LineOutcome(line)
        entry = maps.delete_watch.get(asset_id)
        # Emitted unchanged either way: the phone still has to learn the asset is gone.
        return LineOutcome(
            line,
            gate_opens=(entry,) if entry else (),
            claims_released=(asset_id,) if asset_id in maps.claim_watch else (),
        )

    asset_id = data.get("id")
    checksum = data.get("checksum")
    owner_id = data.get("ownerId")
    if not isinstance(asset_id, str) or not isinstance(checksum, str):
        return LineOutcome(line)

    # Somebody else's row, holding a hash this shim is armed to hand over. That is a claim
    # whatever else the line is: a soft-deleted asset still occupies the mirror's unique
    # index, and a hard-deleted one has no row left to send a line for, so a line carrying
    # the checksum at all means the checksum is taken. No filter on the library either —
    # a library asset lands in the mirror's *other* unique index and could not collide,
    # but it suppresses the phone's upload just as well, so waiting costs nothing.
    observed: tuple[ReturnedOriginal, ...] = ()
    if isinstance(owner_id, str):
        armed_for = maps.armed.get((owner_id, checksum))
        if armed_for is not None and asset_id not in armed_for:
            observed = (ReturnedOriginal(asset_id=asset_id, owner_id=owner_id, checksum=checksum),)

    original = maps.sync_rewrite.get(asset_id)
    if not translate or original is None or original == checksum:
        return LineOutcome(line, claims_observed=observed)
    if isinstance(owner_id, str) and (owner_id, original) in claimed:
        # Seen earlier in this same response: another asset of this owner already holds the
        # checksum, and the store will not say so until the pipeline reaches its job.
        return LineOutcome(line, claims_observed=observed)

    data["checksum"] = original
    return LineOutcome(
        json.dumps(record, separators=(",", ":")).encode() + b"\n",
        rewritten=True,
        claims_observed=observed,
    )


def translate_upload_check(body: bytes, owner_id: str | None, maps: TranslationMaps) -> tuple[bytes, int]:
    """Rewrite the checksums in a ``bulk-upload-check`` *request*.

    The client asks "do you already have these hashes?" about originals this service has
    replaced; the honest answer is about their replacements, so the question is restated
    and Immich answers it. The response is forwarded untouched, which is what makes this
    self-healing: if the replacement has itself been deleted, Immich says ``accept`` and
    the client uploads, which is correct.

    Needs no gate, and no suppression either — the two things that hold the sync stream
    back. It never writes to any mirror, so there is no unique index to violate: when a
    returned original holds the checksum the client is asking about, Immich answers
    ``duplicate`` to the original question and to the translated one alike.

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

    It has a second source the store cannot supply. ``returned_originals`` is authoritative
    and late: an asset is live in Immich the moment ``POST /assets`` answers 201, while its
    job sits in ``queued`` — carrying no checksum at all, because the pipeline writes that
    in step 2 — until a worker reaches it and parks it at ``re_uploaded``. Everything the
    shim hands out in between is decided against a store that does not yet know the
    checksum is taken. `observe` closes that window with what the sync stream itself said,
    and holds it in memory only: the store's own answer arrives within minutes and is the
    durable one, so a restart costs at worst the rest of one window.
    """

    def __init__(self, store: JobStore, refresh_seconds: float, *, clock: Callable[[], float]) -> None:
        self._store = store
        self._refresh_seconds = refresh_seconds
        self._clock = clock
        self._maps = TranslationMaps()
        self._loaded_at: float | None = None
        self._observed: dict[str, ReturnedOriginal] = {}

    def observe(self, claims: Iterable[ReturnedOriginal]) -> int:
        """Remember claims a sync stream showed. Returns how many were new.

        A new one forces a rebuild, because the maps the next request is served from have
        to have stopped arming that checksum before the first line goes out.
        """
        new = [claim for claim in claims if claim.asset_id not in self._observed]
        for claim in new:
            self._observed[claim.asset_id] = claim
        if new:
            self.invalidate()
        return len(new)

    def release(self, asset_id: str) -> bool:
        """Forget an observed claim whose asset the stream says is gone. Returns whether it held one."""
        return self._observed.pop(asset_id, None) is not None

    async def maps(self) -> TranslationMaps:
        now = self._clock()
        if self._loaded_at is None or now - self._loaded_at >= self._refresh_seconds:
            try:
                from_store = await self._store.returned_originals()
                # Whatever the pipeline has since classified is held durably now, so the
                # sighting has nothing left to add. Dropping it keeps this from growing to
                # the size of every re-upload the process has ever watched go past.
                for row in from_store:
                    self._observed.pop(row.asset_id, None)
                # The store first, so that where both still know an asset the store's row
                # wins and `build` drops the sighting as a duplicate of it.
                rebuilt = TranslationMaps.build(
                    await self._store.ledger_entries(),
                    [*from_store, *self._observed.values()],
                )
            except Exception:
                # Keep serving the previous maps. A stale translation is a missed
                # prevention; a raised exception here would be a broken sync.
                logger.exception("shim: could not refresh the ledger, keeping the previous maps")
            else:
                if rebuilt.suppressed != self._maps.suppressed:
                    # The one state in here an operator cannot read off a counter: the shim
                    # is deliberately doing nothing, and silence would look like a fault.
                    logger.info(
                        "shim: %d translation(s) held back by a returned original that still "
                        "holds the checksum (was %d)",
                        rebuilt.suppressed,
                        self._maps.suppressed,
                    )
                self._maps = rebuilt
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


async def _release_claims(ready: _Ready, maps: TranslationMaps, asset_ids: tuple[str, ...]) -> None:
    """Record that a returned original is gone, and re-arm what it was holding back.

    The mirror image of `_open_gates`, and it needs the same no-op update for the same
    reason: nothing has changed about the replacement since the client last saw it, so
    without one the line this now wants to rewrite would never be offered again.

    `SHIM_GATES_OPENED` deliberately does not move. No gate opens here — the original's
    gate opened when the original died and has stayed open. What was blocked was the
    translation, and what is counted is the write that unblocks it.
    """
    deps = ready.deps
    for asset_id in asset_ids:
        blocked = maps.claim_watch.get(asset_id, [])
        if deps.log_only:
            logger.info(
                "shim (log_only): would stop %s suppressing %d translation(s)",
                asset_id,
                len(blocked),
            )
            continue
        # Both, and in this order: a claim the shim only ever saw on a stream has no job
        # row for `mark_original_freed` to find, and dropping out on its `False` would
        # leave the sighting suppressing that checksum for the life of the process.
        forgotten = ready.ledger.release(asset_id)
        if not await ready.store.mark_original_freed(asset_id) and not forgotten:
            continue
        ready.ledger.invalidate()
        logger.info(
            "the returned copy %s is gone; the %d translation(s) it held back are armed again",
            asset_id,
            len(blocked),
        )
        for new_asset_id in blocked:
            try:
                assert deps.touch is not None
                await deps.touch(new_asset_id)
            except Exception:
                # As in `_open_gates`: the record is correct and only the re-offer is
                # missing. Not worth failing a client's sync over.
                logger.warning(
                    "shim: could not touch %s to have it re-sent; the translation is armed but "
                    "the client may not see it until that asset changes for another reason",
                    new_asset_id,
                    exc_info=True,
                )
            else:
                await ready.store.bump_counter(SHIM_TOUCHES)


async def _stream_sync(ready: _Ready, upstream: httpx.Response) -> AsyncIterator[bytes]:
    """Rewrite the JSON Lines response as it arrives, one line at a time.

    Buffered by line, never whole: a full sync of a large library is far too big to hold,
    and the client starts applying batches long before the response ends.

    That is also why suppression works forwards only. A second pass would see every claim
    before deciding anything, and there is no held response to make a second pass over — so
    a claim seen on line *n* governs the lines after it, and the ledger remembers it for
    every request after this one. A claim that arrives *behind* the line it should have
    stopped therefore still costs that one batch: the client's upsert fails, it does not
    ack, and Immich re-sends. What the memory buys is that the re-send is served from maps
    that already know, so the retry applies instead of wedging the client in a loop.
    """
    deps = ready.deps
    translate = deps.rewrite_sync_stream and not deps.log_only
    pending = b""
    rewritten = 0
    gate_opens: list[LedgerEntry] = []
    claims_released: list[str] = []
    observed: dict[str, ReturnedOriginal] = {}
    claimed: set[tuple[str, str]] = set()

    def take(outcome: LineOutcome) -> bytes:
        nonlocal rewritten
        rewritten += outcome.rewritten
        if deps.watch_deletes:
            gate_opens.extend(outcome.gate_opens)
            claims_released.extend(outcome.claims_released)
        for claim in outcome.claims_observed:
            observed[claim.asset_id] = claim
            claimed.add((claim.owner_id, claim.checksum))
        return outcome.data

    # Bound before the try so the dispatch after it never reads an unassigned name; the
    # real maps are the first thing loaded inside.
    maps = TranslationMaps()
    try:
        maps = await ready.ledger.maps()
        async for chunk in upstream.aiter_bytes():
            pending += chunk
            while b"\n" in pending:
                raw, pending = pending.split(b"\n", 1)
                yield take(rewrite_sync_line(raw + b"\n", maps, translate=translate, claimed=claimed))
        if pending:
            yield take(rewrite_sync_line(pending, maps, translate=translate, claimed=claimed))
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
    if observed:
        # Before the release, so that a claim this response both showed and withdrew ends
        # withdrawn. Logged at all because the pipeline has not classified these yet: an
        # operator comparing the shim against `jobs` would otherwise find nothing there.
        new = ready.ledger.observe(observed.values())
        if new:
            logger.info(
                "shim: %d asset(s) seen on the sync stream still hold a replaced original's "
                "checksum; their translations stand down until those assets go",
                new,
            )
    if gate_opens:
        await _open_gates(ready, tuple(gate_opens))
    if claims_released:
        await _release_claims(ready, maps, tuple(claims_released))


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
