"""FastAPI application.

The webhook endpoint does the absolute minimum: verify the shared secret, persist the
job, answer ``202``. Immich runs the ``webhook`` action synchronously inside the
``WorkflowAssetTrigger`` job, so anything slower would block or time out the server.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .api import ImmichClient
from .config import Settings, load_settings
from .models import JobState, WebhookPayload
from .pipeline import Worker
from .store import JobStore

logger = logging.getLogger(__name__)


class EnqueueResponse(BaseModel):
    accepted: bool
    asset_id: str
    duplicate: bool


class HealthResponse(BaseModel):
    status: str
    dry_run: bool
    trash_original: bool
    immich_reachable: bool
    immich_version: str | None = None


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
        worker = Worker(resolved, client, store)
        app.state.settings = resolved
        app.state.store = store
        app.state.client = client
        app.state.worker = worker
        await worker.start()
        try:
            yield
        finally:
            await worker.stop()
            await client.aclose()
            await store.close()

    app = FastAPI(
        title="immich-compressor",
        version="1.0.0",
        description="Out-of-band recompression for Immich assets, driven by a workflow webhook.",
        lifespan=lifespan,
    )

    @app.exception_handler(RequestValidationError)
    async def log_validation_errors(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Never let a rejected webhook fail silently.

        FastAPI's default handler returns 422 without a log line. Immich's webhook action
        also swallows non-2xx responses and reports the workflow as "executed
        successfully", so a schema mismatch is otherwise completely invisible on both
        sides — which is exactly how the `exifInfo.tags: null` bug hid.
        """
        logger.error(
            "rejected %s %s with 422: %s", request.method, request.url.path, exc.errors()
        )
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})

    async def verify_token(request: Request) -> None:
        """Constant-time comparison of the workflow's single configurable header."""
        expected = resolved.webhook.token.get_secret_value()
        presented = request.headers.get(resolved.webhook.header_name, "")
        if not presented or not hmac.compare_digest(presented, expected):
            logger.warning("rejected webhook from %s: bad or missing shared secret",
                           request.client.host if request.client else "unknown")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")

    @app.post(
        "/webhook",
        response_model=EnqueueResponse,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(verify_token)],
    )
    async def webhook(request: Request, payload: WebhookPayload) -> EnqueueResponse:
        store: JobStore = request.app.state.store
        settings: Settings = request.app.state.settings
        asset = payload.data.asset
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
        return EnqueueResponse(accepted=True, asset_id=asset.id, duplicate=not inserted)

    # Immich's webhook action can be configured to use PUT as well.
    @app.put(
        "/webhook",
        response_model=EnqueueResponse,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(verify_token)],
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
        return HealthResponse(
            status="ok",
            dry_run=settings.behavior.dry_run,
            trash_original=settings.behavior.trash_original,
            immich_reachable=reachable,
            immich_version=version,
        )

    @app.get("/stats")
    async def stats(request: Request) -> dict[str, Any]:
        store: JobStore = request.app.state.store
        worker: Worker = request.app.state.worker
        settings: Settings = request.app.state.settings
        body = await store.stats()
        body["session"] = {
            "processed": worker.pipeline.stats.processed,
            "skipped": worker.pipeline.stats.skipped,
            "failed": worker.pipeline.stats.failed,
            "deleted": worker.pipeline.stats.deleted,
            "bytes_saved": worker.pipeline.stats.bytes_saved,
        }
        body["config"] = {
            "dry_run": settings.behavior.dry_run,
            "trash_original": settings.behavior.trash_original,
            "retention_days": settings.behavior.retention_days,
            "enabled_types": settings.behavior.enabled_types,
            "max_ratio": settings.behavior.max_ratio,
            "min_size_bytes": settings.behavior.min_size_bytes,
        }
        return body

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

    @app.head("/healthz")
    async def healthz_head() -> Response:
        return Response(status_code=200)

    return app
