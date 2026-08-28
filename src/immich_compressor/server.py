"""The ASGI application: what it is made of, and what it opens and closes.

The endpoints themselves live in :mod:`~immich_compressor.routes`. What is left here is the
wiring — the lifespan that owns the store, the Immich client and the worker, the optional
shim mount, and the settings the routes read back off ``app.state``.

The webhook endpoint does the absolute minimum: verify the shared secret, persist the
job, answer ``202``. Immich runs the ``webhook`` action synchronously inside the
``WorkflowAssetTrigger`` job, so anything slower would block or time out the server.

Two ingest guards sit in front of the store: the freshness gate that refuses a bulk
metadata-extraction re-trigger, and the surge breaker that latches the whole service paused
when far more work arrives than anybody asked for.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from . import __version__
from .api import client_for
from .config import Settings, load_settings, warn_about_permanent_deletion, workflow_file_pattern
from .encoder import probe_hardware_encoder
from .ingest import SurgeDetector
from .pipeline import Worker
from .routes import log_validation_errors, router
from .shim import ChecksumLedger, OwnerResolver, ShimDeps, build_router, describe
from .store import JobStore

logger = logging.getLogger(__name__)


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


def _shim_deps_for(settings: Settings) -> ShimDeps | None:
    """The shim's dependency object, or ``None`` when the shim is off.

    Built before the application so the routes exist in the table FastAPI compiles at
    startup; the runtime handles are filled in by the lifespan. ``None`` means nothing is
    mounted at all — the two proxied paths simply do not exist here.
    """
    if not settings.shim.enabled:
        return None
    return ShimDeps(
        upstream_url=settings.shim.upstream_url,
        rewrite_sync_stream=settings.shim.rewrite_sync_stream,
        rewrite_upload_check=settings.shim.rewrite_upload_check,
        watch_deletes=settings.shim.watch_deletes,
        log_only=settings.shim.log_only,
    )


def _lifespan(
    resolved: Settings, shim_deps: ShimDeps | None
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Everything the running process owns, opened in order and closed in reverse."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = JobStore(resolved.database_path)
        await store.open()
        client = client_for(resolved.immich)
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

    return lifespan


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI app. Accepts injected settings so tests can skip the YAML/env layer."""
    resolved = settings or load_settings()
    shim_deps = _shim_deps_for(resolved)

    app = FastAPI(
        title="immich-compressor",
        version=__version__,
        description="Out-of-band recompression for Immich assets, driven by a workflow webhook.",
        lifespan=_lifespan(resolved, shim_deps),
    )

    # Set here rather than only in the lifespan, because the token check reads it: a request
    # that arrives at an application which was built but never started still gets the same
    # 401 it always did, instead of an AttributeError. The lifespan assigns the same object
    # again on startup.
    app.state.settings = resolved

    if shim_deps is not None:
        app.include_router(build_router(shim_deps))
    app.include_router(router)
    app.add_exception_handler(RequestValidationError, log_validation_errors)

    return app
