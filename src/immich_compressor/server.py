"""FastAPI application.

The webhook endpoint does the absolute minimum: verify the shared secret, persist the
job, answer ``202``. Immich runs the ``webhook`` action synchronously inside the
``WorkflowAssetTrigger`` job, so anything slower would block or time out the server.

Two ingest guards sit in front of the store: the freshness gate that refuses a bulk
metadata-extraction re-trigger, and the surge breaker that latches the whole service paused
when far more work arrives than anybody asked for.
"""

from __future__ import annotations

import hmac
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from . import __version__
from .api import ImmichClient
from .config import (
    BehaviorSettings,
    Settings,
    load_settings,
    warn_about_permanent_deletion,
    workflow_file_pattern,
)
from .encoder import probe_hardware_encoder
from .metrics import CONTENT_TYPE as METRICS_CONTENT_TYPE
from .metrics import render as render_metrics
from .models import JobState, RejectReason, WebhookPayload
from .pipeline import SurgeDetector, WebhookRejected, Worker, check_ingest_guards
from .shim import ChecksumLedger, OwnerResolver, ShimDeps, build_router, describe
from .store import WEBHOOKS_RECEIVED, WEBHOOKS_REJECTED, JobStore

logger = logging.getLogger(__name__)


def _token_fingerprint(value: str) -> str:
    """Enough of a token to recognise, not enough to use.

    Six of 64 hex characters is exactly what separates the two ways this goes wrong in
    practice — a paste that was cut short, and a token left over from an earlier
    installation — and it leaves the remaining 58 untouched. The log already carries asset
    ids and file paths; this is not the line that changes its sensitivity.
    """
    if not value:
        return "no token at all"
    return f"{len(value)} characters starting {value[:6]}"


class EnqueueResponse(BaseModel):
    accepted: bool
    asset_id: str
    duplicate: bool
    # Set only when `accepted` is false: why the webhook was refused before it became a job.
    reason: RejectReason | None = None


class HealthResponse(BaseModel):
    status: str
    dry_run: bool
    trash_original: bool
    immich_reachable: bool
    immich_version: str | None = None
    # True while the surge breaker is latched: nothing is queued, processed or deleted.
    paused: bool = False
    paused_reason: str | None = None


def _config_snapshot(behavior: BehaviorSettings) -> dict[str, Any]:
    """The settings both reporting surfaces publish.

    ``/stats`` shows all of it; ``render`` picks the three it has a gauge for and ignores
    the rest. Built once so the two can never disagree about what the service is
    configured to do — which is the question both of them exist to answer.
    """
    return {
        "dry_run": behavior.dry_run,
        "trash_original": behavior.trash_original,
        "delete_mode": behavior.delete_mode,
        "retention_days": behavior.retention_days,
        "enabled_types": behavior.enabled_types,
        "max_ratio": behavior.max_ratio,
        "min_savings_bytes": behavior.min_savings_bytes,
        "metadata_verify": behavior.metadata_verify,
    }


async def _warn_about_unusable_hardware(settings: Settings) -> None:
    """Say so at startup when a preset wants a GPU that is not reachable.

    Deliberately a warning, not a hard exit: a device that reappears after a host reboot
    should not keep the service down, and the job simply fails loudly in the meantime.
    """
    for preset in settings.presets:
        encoder_name = preset.hardware_encoder
        if encoder_name is None:
            continue
        problem = await probe_hardware_encoder(encoder_name, preset.render_node)
        if problem:
            logger.warning(
                "preset %r wants %s on %s, but it is not usable here: %s",
                preset.name,
                encoder_name,
                preset.render_node,
                problem,
            )
        else:
            logger.info("preset %r: %s on %s ready", preset.name, encoder_name, preset.render_node)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI app. Accepts injected settings so tests can skip the YAML/env layer."""
    resolved = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = JobStore(resolved.database_path)
        await store.open()
        client = ImmichClient(
            resolved.immich.base_url,
            resolved.immich.api_key.get_secret_value(),
            timeout_s=resolved.immich.timeout_s,
            connect_timeout_s=resolved.immich.connect_timeout_s,
        )
        warn_about_permanent_deletion(resolved.behavior)
        # The marker couples three things nobody ever sees side by side: this setting, the
        # filename the encoder writes, and the workflow's `assetFileFilter` regex — which
        # lives inside Immich, out of reach of any validation here. Printing the expected
        # pattern once at startup is what makes the comparison possible at all.
        logger.info(
            "compressed marker %r: the workflow's assetFileFilter pattern must be %s",
            resolved.behavior.compressed_marker,
            workflow_file_pattern(resolved.behavior.compressed_marker),
        )
        await _warn_about_unusable_hardware(resolved)
        worker = Worker(resolved, client, store)
        app.state.settings = resolved
        app.state.store = store
        app.state.client = client
        app.state.worker = worker
        app.state.surge = SurgeDetector(
            resolved.behavior.surge_threshold, resolved.behavior.surge_window_seconds
        )
        if (latched := await store.pause_state()) is not None:
            logger.warning(
                "starting PAUSED since %s: %s — clear it with `immich-compressor resume --apply`",
                latched.since.isoformat(),
                latched.reason,
            )
        if shim_deps is not None:
            # Filled in here rather than at import time: the store, the Immich client and
            # the proxy's connection pool all belong to the running process, not to the
            # app object. ASGI startup completes before the first request arrives, so the
            # routes never see a half-built dependency.
            shim_deps.client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=resolved.shim.connect_timeout_s, read=60.0, write=60.0, pool=None
                ),
                follow_redirects=False,
            )
            shim_deps.store = store
            shim_deps.ledger = ChecksumLedger(
                store, resolved.shim.ledger_refresh_seconds, clock=time.monotonic
            )
            shim_deps.owners = OwnerResolver(
                resolved.shim.upstream_url, shim_deps.client, 300.0, time.monotonic
            )
            shim_deps.touch = client.touch_asset
            logger.info("%s", describe(resolved.shim))
        await worker.start()
        try:
            yield
        finally:
            await worker.stop()
            if shim_deps is not None and shim_deps.client is not None:
                await shim_deps.client.aclose()
            await client.aclose()
            await store.close()

    # Built before the app so the routes exist in the table FastAPI compiles at startup;
    # its fields are populated by the lifespan above. `None` means the shim is off, and
    # then nothing is mounted at all — the two proxied paths simply do not exist here.
    shim_deps: ShimDeps | None = (
        ShimDeps(
            upstream_url=resolved.shim.upstream_url,
            rewrite_sync_stream=resolved.shim.rewrite_sync_stream,
            rewrite_upload_check=resolved.shim.rewrite_upload_check,
            watch_deletes=resolved.shim.watch_deletes,
            log_only=resolved.shim.log_only,
        )
        if resolved.shim.enabled
        else None
    )

    app = FastAPI(
        title="immich-compressor",
        version=__version__,
        description="Out-of-band recompression for Immich assets, driven by a workflow webhook.",
        lifespan=lifespan,
    )

    if shim_deps is not None:
        app.include_router(build_router(shim_deps))

    @app.exception_handler(RequestValidationError)
    async def log_validation_errors(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Never let a rejected webhook fail silently.

        FastAPI's default handler returns 422 without a log line. Immich's webhook action
        also swallows non-2xx responses and reports the workflow as "executed
        successfully", so a schema mismatch is otherwise completely invisible on both
        sides — which is exactly how the `exifInfo.tags: null` bug hid.
        """
        logger.error("rejected %s %s with 422: %s", request.method, request.url.path, exc.errors())
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})

    async def _verify_token(request: Request, *, count: bool) -> None:
        """Constant-time comparison of the workflow's single configurable header.

        ``count`` is on for the webhook routes only. A token mismatch there is the one
        failure in this architecture that leaves no other trace anywhere: Immich discards
        the response and logs the workflow as executed successfully, no job row is written,
        and every surface a user would consult — `check`, `report`, `/healthz` — looks
        exactly like a healthy installation with nothing to do yet. The counters are what
        turn that into a sentence somebody can read.
        """
        expected = resolved.webhook.token.get_secret_value()
        presented = request.headers.get(resolved.webhook.header_name, "")
        store: JobStore | None = getattr(request.app.state, "store", None)
        if not presented or not hmac.compare_digest(presented, expected):
            if count and store is not None:
                await store.bump_counter(WEBHOOKS_REJECTED)
            logger.warning(
                "rejected webhook from %s: bad or missing shared secret. Immich sent %s in "
                "%s, this service expects %s — the workflow's headerValue and WEBHOOK__TOKEN "
                "must be equal (docs/workflow-setup.md)",
                request.client.host if request.client else "unknown",
                _token_fingerprint(presented),
                resolved.webhook.header_name,
                _token_fingerprint(expected),
            )
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")
        if count and store is not None:
            await store.bump_counter(WEBHOOKS_RECEIVED)

    async def verify_token(request: Request) -> None:
        """The shared secret on the maintenance routes, which are not webhooks."""
        await _verify_token(request, count=False)

    async def verify_webhook_token(request: Request) -> None:
        await _verify_token(request, count=True)

    @app.post(
        "/webhook",
        response_model=EnqueueResponse,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(verify_webhook_token)],
    )
    async def webhook(request: Request, payload: WebhookPayload) -> EnqueueResponse:
        store: JobStore = request.app.state.store
        settings: Settings = request.app.state.settings
        asset = payload.data.asset

        # Before the store, never after: a rejection must not leave a row behind, or
        # `backfill` could never reach the asset again. Answered 202 like everything else
        # — Immich logs a non-2xx as "executed successfully" anyway, so the status code is
        # not a channel we have; the body and the log line are.
        try:
            check_ingest_guards(asset, settings.behavior)
        except WebhookRejected as rejected:
            logger.warning(
                "refused %s asset=%s type=%s (%s): %s",
                payload.trigger,
                asset.id,
                asset.type,
                rejected.reason.value,
                rejected.detail,
            )
            return EnqueueResponse(accepted=False, asset_id=asset.id, duplicate=False, reason=rejected.reason)

        # A latched breaker refuses rather than queues. Accepting would grow a queue nobody
        # has approved, and every row written would be one `backfill` can never reach again.
        if (latched := await store.pause_state()) is not None:
            logger.warning(
                "refused %s asset=%s: service is paused since %s (%s)",
                payload.trigger,
                asset.id,
                latched.since.isoformat(),
                latched.reason,
            )
            return EnqueueResponse(
                accepted=False, asset_id=asset.id, duplicate=False, reason=RejectReason.PAUSED
            )

        inserted = await store.enqueue(
            asset.id,
            payload.model_dump(mode="json", by_alias=True),
            delay_seconds=settings.behavior.initial_delay_seconds,
        )
        logger.info(
            "webhook %s asset=%s type=%s %s",
            payload.trigger,
            asset.id,
            asset.type,
            "queued" if inserted else "already known (no-op)",
        )

        # Only newly queued assets count towards the surge: a replay of something already
        # recorded queues no work, so it must not push the breaker either.
        if inserted and (tripped := request.app.state.surge.record()) is not None:
            reason = (
                f"{tripped} assets queued from webhooks within "
                f"{settings.behavior.surge_window_seconds:g}s, over surge_threshold "
                f"{settings.behavior.surge_threshold}"
            )
            if await store.pause(reason):
                logger.error(
                    "SURGE BREAKER TRIPPED: %s. Nothing further is queued, processed or "
                    "deleted until `immich-compressor resume --apply`.",
                    reason,
                )

        return EnqueueResponse(accepted=True, asset_id=asset.id, duplicate=not inserted)

    # Immich's webhook action can be configured to use PUT as well.
    @app.put(
        "/webhook",
        response_model=EnqueueResponse,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(verify_webhook_token)],
    )
    async def webhook_put(request: Request, payload: WebhookPayload) -> EnqueueResponse:
        return await webhook(request, payload)

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz(request: Request) -> HealthResponse:
        client: ImmichClient = request.app.state.client
        settings: Settings = request.app.state.settings
        version: str | None = None
        reachable = False
        try:
            version = await client.server_version()
            reachable = True
        except Exception as exc:  # noqa: BLE001 - health must report, not raise
            logger.debug("health check could not reach Immich: %s", exc)
        latched = await request.app.state.store.pause_state()
        return HealthResponse(
            status="paused" if latched else "ok",
            dry_run=settings.behavior.dry_run,
            trash_original=settings.behavior.trash_original,
            immich_reachable=reachable,
            immich_version=version,
            paused=latched is not None,
            paused_reason=latched.reason if latched else None,
        )

    @app.get("/stats")
    async def stats(request: Request) -> dict[str, Any]:
        store: JobStore = request.app.state.store
        worker: Worker = request.app.state.worker
        settings: Settings = request.app.state.settings
        body = await store.stats()
        latched = await store.pause_state()
        body["paused"] = latched.as_dict() if latched else None
        # First thing to read when nothing is happening: "0 received, 7 rejected" is a
        # different problem from "0 received, 0 rejected", and neither is visible anywhere
        # else. Persisted, so these survive a restart.
        counters = await store.counters()
        body["webhooks"] = {
            "received": counters[WEBHOOKS_RECEIVED],
            "rejected": counters[WEBHOOKS_REJECTED],
        }
        body["session"] = worker.pipeline.stats.as_dict()
        body["config"] = _config_snapshot(settings.behavior)
        return body

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics(request: Request) -> PlainTextResponse:
        """Prometheus exposition. Unauthenticated, like /stats — it carries no asset data.

        No port is published by default, so this is reachable from the docker network the
        service already shares with Immich, which is where a Prometheus in the same stack
        lives anyway.
        """
        store: JobStore = request.app.state.store
        worker: Worker = request.app.state.worker
        settings: Settings = request.app.state.settings
        stats = worker.pipeline.stats
        body = render_metrics(
            store_stats=await store.stats(),
            counters=await store.counters(),
            session=stats.as_dict(),
            encode_seconds=stats.encode_seconds,
            config=_config_snapshot(settings.behavior),
            paused=await store.pause_state() is not None,
            version=__version__,
        )
        return PlainTextResponse(body, media_type=METRICS_CONTENT_TYPE)

    @app.get("/jobs")
    async def jobs(
        request: Request,
        job_status: Annotated[str | None, Query(alias="status")] = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        store: JobStore = request.app.state.store
        state: JobState | None = None
        if job_status:
            try:
                state = JobState(job_status)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"unknown status {job_status!r}") from exc
        found = await store.list_jobs(state=state, limit=min(max(limit, 1), 1000))
        return {
            "count": len(found),
            "jobs": [job.model_dump(mode="json", exclude={"payload"}) for job in found],
        }

    @app.get("/jobs/{asset_id}")
    async def job_detail(request: Request, asset_id: str) -> dict[str, Any]:
        store: JobStore = request.app.state.store
        job = await store.get(asset_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown asset")
        return job.model_dump(mode="json")

    @app.post("/reprocess/{asset_id}", dependencies=[Depends(verify_token)])
    async def reprocess(request: Request, asset_id: str) -> dict[str, Any]:
        """Re-queue an asset that was skipped or failed.

        Note this does not remove the ``compressor`` marker on the server, so an
        already-compressed asset will simply be skipped again — by design.
        """
        store: JobStore = request.app.state.store
        if not await store.reset(asset_id):
            raise HTTPException(status_code=404, detail="unknown asset")
        return {"requeued": True, "asset_id": asset_id}

    @app.post("/resume", dependencies=[Depends(verify_token)])
    async def resume(request: Request) -> dict[str, Any]:
        """Clear the surge breaker. Token-protected: it re-arms a service that deletes.

        Nothing else is touched — jobs queued before the pause keep their state and are
        picked up again on the next poll.
        """
        store: JobStore = request.app.state.store
        latched = await store.pause_state()
        if latched is None:
            return {"resumed": False, "detail": "the service was not paused"}
        await store.resume()
        logger.warning("resumed by request; the pause since %s is cleared", latched.since.isoformat())
        return {"resumed": True, "was_paused_since": latched.since.isoformat(), "reason": latched.reason}

    @app.head("/healthz")
    async def healthz_head() -> Response:
        return Response(status_code=200)

    return app
