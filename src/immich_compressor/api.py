"""Typed async client for the Immich v3 REST API.

Only the endpoints the pipeline needs are implemented. Everything was exercised against
a live Immich v3.1.0 instance; deviations from the original plan are called out inline.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import httpx

from .models import (
    AssetDetail,
    AssetMediaResponse,
    MetadataItem,
    TagRef,
    UpdateAssetFields,
)

logger = logging.getLogger(__name__)

# Immich rejects a rating of 0 since v3 ("Rating must be -1, 1-5, or null").
VALID_RATINGS: frozenset[int] = frozenset({-1, 1, 2, 3, 4, 5})

_RETRY_STATUS: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})
_ISO_MS = "%Y-%m-%dT%H:%M:%S.%f"


class ImmichError(RuntimeError):
    """Non-retryable API failure."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def format_timestamp(value: datetime) -> str:
    """Render a datetime in the exact shape ``POST /assets`` validates against.

    The OpenAPI pattern demands ``YYYY-MM-DDTHH:MM:SS[.sss]`` followed by ``Z`` or a
    numeric offset. We normalise to UTC with millisecond precision, which is what the
    official Immich clients send.
    """
    if value.tzinfo is None:
        raise ValueError("refusing to serialise a naive datetime")
    return value.astimezone(UTC).strftime(_ISO_MS)[:-3] + "Z"


class ImmichClient:
    """Thin, retrying wrapper around ``httpx.AsyncClient``."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_s: float = 120.0,
        connect_timeout_s: float = 10.0,
        max_retries: int = 3,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            headers={"x-api-key": api_key, "Accept": "application/json"},
            timeout=httpx.Timeout(timeout_s, connect=connect_timeout_s),
            follow_redirects=True,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ---------------------------------------------------------------- internals

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json: Any | None = None,
        files: Any | None = None,
        data: Any | None = None,
        params: Any | None = None,
        expected: tuple[int, ...] = (200, 201, 204),
    ) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                response = await self._client.request(
                    method, url, json=json, files=files, data=data, params=params
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                logger.warning("%s %s transport error (attempt %d): %s", method, url, attempt, exc)
            else:
                if response.status_code in expected:
                    return response
                if response.status_code in _RETRY_STATUS and attempt < self._max_retries:
                    logger.warning(
                        "%s %s -> HTTP %d (attempt %d), retrying",
                        method,
                        url,
                        response.status_code,
                        attempt,
                    )
                    last_exc = None
                else:
                    raise ImmichError(
                        f"{method} {url} -> HTTP {response.status_code}: {response.text[:400]}",
                        status_code=response.status_code,
                    )
            if attempt < self._max_retries:
                await asyncio.sleep(min(2.0**attempt, 30.0))
        raise ImmichError(f"{method} {url} failed after {self._max_retries} attempts: {last_exc}")

    # ---------------------------------------------------------------- endpoints

    async def ping(self) -> bool:
        response = await self._request("GET", "/server/ping", expected=(200,))
        return response.json().get("res") == "pong"

    async def server_version(self) -> str:
        response = await self._request("GET", "/server/version", expected=(200,))
        body = response.json()
        return f"{body['major']}.{body['minor']}.{body['patch']}"

    async def get_asset(self, asset_id: str) -> AssetDetail:
        response = await self._request("GET", f"/assets/{asset_id}", expected=(200,))
        return AssetDetail.model_validate(response.json())

    async def wait_for_metadata_extraction(
        self, asset_id: str, *, timeout_s: float = 30.0, interval_s: float = 0.5
    ) -> bool:
        """Block until Immich's metadata-extraction job has run on a freshly uploaded asset.

        Immich runs extraction asynchronously, roughly 300 ms after ``POST /assets``
        returns, and that job **overwrites** ``description`` and ``rating`` with whatever
        it reads from the file. Writing our own values before it lands silently loses
        them — reproduced on a live v3.1.0 instance. ``dateTimeOriginal`` flipping from
        ``null`` to a value is the observable signal that extraction finished; our sanity
        gate guarantees every uploaded file carries a capture date, so it always flips.

        Returns ``True`` if extraction was observed, ``False`` on timeout (the caller
        proceeds anyway — a lost description is better than a stuck job).
        """
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            asset = await self.get_asset(asset_id)
            if asset.exif_info.date_time_original is not None:
                return True
            if asyncio.get_running_loop().time() >= deadline:
                logger.warning(
                    "metadata extraction for %s did not settle within %.0fs", asset_id, timeout_s
                )
                return False
            await asyncio.sleep(interval_s)

    async def get_metadata(self, asset_id: str) -> list[MetadataItem]:
        """List the asset's metadata KV entries.

        Deviation from the plan: ``GET /assets/{id}/metadata/{key}`` answers **400**
        (not 404) for a missing key, so the marker check uses the list endpoint, which
        cleanly returns ``[]``.
        """
        response = await self._request("GET", f"/assets/{asset_id}/metadata", expected=(200,))
        return [MetadataItem.model_validate(item) for item in response.json()]

    async def has_metadata_key(self, asset_id: str, key: str) -> MetadataItem | None:
        for item in await self.get_metadata(asset_id):
            if item.key == key:
                return item
        return None

    async def put_metadata(self, asset_id: str, items: list[MetadataItem]) -> list[MetadataItem]:
        body = {"items": [{"key": item.key, "value": item.value} for item in items]}
        response = await self._request(
            "PUT", f"/assets/{asset_id}/metadata", json=body, expected=(200, 201)
        )
        return [MetadataItem.model_validate(item) for item in response.json()]

    async def download_original(self, asset_id: str, destination: Path) -> int:
        """Stream ``GET /assets/{id}/original`` to disk. Returns the byte count."""
        written = 0
        tmp = destination.with_suffix(destination.suffix + ".part")
        try:
            async with self._client.stream("GET", f"/assets/{asset_id}/original") as response:
                if response.status_code != 200:
                    await response.aread()
                    raise ImmichError(
                        f"download {asset_id} -> HTTP {response.status_code}",
                        status_code=response.status_code,
                    )
                with tmp.open("wb") as handle:
                    async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                        handle.write(chunk)
                        written += len(chunk)
            tmp.replace(destination)
        finally:
            tmp.unlink(missing_ok=True)
        return written

    async def upload_asset(
        self,
        path: Path,
        *,
        filename: str,
        file_created_at: datetime,
        file_modified_at: datetime,
        duration_ms: int | None = None,
        is_favorite: bool = False,
        visibility: str | None = None,
    ) -> AssetMediaResponse:
        """``POST /assets`` (multipart).

        Deviation from the plan: the ``metadata`` array is **not** sent here. Multipart
        can only express it as ``metadata[0][value][k]=v``, which coerces every value to
        a string (``{"v": "1"}`` instead of ``{"v": 1}``). The marker is written with a
        separate typed ``PUT /assets/{id}/metadata`` call instead.
        """
        form: dict[str, str] = {
            "fileCreatedAt": format_timestamp(file_created_at),
            "fileModifiedAt": format_timestamp(file_modified_at),
            "filename": filename,
            "isFavorite": "true" if is_favorite else "false",
        }
        if duration_ms is not None:
            form["duration"] = str(int(duration_ms))
        if visibility:
            form["visibility"] = visibility

        with path.open("rb") as handle:
            response = await self._request(
                "POST",
                "/assets",
                files={"assetData": (filename, handle, "application/octet-stream")},
                data=form,
                expected=(200, 201),
            )
        return AssetMediaResponse.model_validate(response.json())

    async def copy_asset(
        self,
        source_id: str,
        target_id: str,
        *,
        albums: bool = True,
        favorite: bool = True,
        shared_links: bool = True,
        stack: bool = True,
        sidecar: bool = True,
    ) -> None:
        """``PUT /assets/copy``.

        Verified against v3.1.0: copies album membership, favourite flag, shared links,
        stack and sidecar. It does **not** copy tags, description, rating, GPS, people or
        the metadata KV — those are handled separately by the pipeline.
        """
        await self._request(
            "PUT",
            "/assets/copy",
            json={
                "sourceId": source_id,
                "targetId": target_id,
                "albums": albums,
                "favorite": favorite,
                "sharedLinks": shared_links,
                "stack": stack,
                "sidecar": sidecar,
            },
            expected=(200, 204),
        )

    async def update_asset(self, asset_id: str, fields: UpdateAssetFields) -> None:
        body = fields.to_body()
        if not body:
            return
        await self._request("PUT", f"/assets/{asset_id}", json=body, expected=(200, 204))

    async def upsert_tags(self, names: list[str]) -> list[TagRef]:
        """``PUT /tags`` upserts by name and returns the tags — no separate GET/POST dance."""
        if not names:
            return []
        response = await self._request("PUT", "/tags", json={"tags": names}, expected=(200, 201))
        return [TagRef.model_validate(item) for item in response.json()]

    async def tag_assets(self, tag_ids: list[str], asset_ids: list[str]) -> int:
        if not tag_ids or not asset_ids:
            return 0
        response = await self._request(
            "PUT",
            "/tags/assets",
            json={"tagIds": tag_ids, "assetIds": asset_ids},
            expected=(200,),
        )
        return int(response.json().get("count", 0))

    async def delete_assets(self, asset_ids: list[str], *, force: bool = False) -> None:
        """Soft delete (into the trash) unless ``force`` is set."""
        if not asset_ids:
            return
        await self._request(
            "DELETE",
            "/assets",
            json={"ids": asset_ids, "force": force},
            expected=(200, 204),
        )

    async def restore_assets(self, asset_ids: list[str]) -> None:
        """Rollback helper — pull assets back out of the trash."""
        if not asset_ids:
            return
        await self._request(
            "POST", "/trash/restore/assets", json={"ids": asset_ids}, expected=(200, 204)
        )

    async def search_large_assets(
        self,
        *,
        min_file_size: int,
        asset_type: str,
        size: int = 100,
        page: int = 1,
    ) -> AsyncIterator[dict[str, Any]]:
        """``POST /search/large-assets`` — used only by the optional backfill CLI."""
        response = await self._request(
            "POST",
            "/search/large-assets",
            json={
                "minFileSize": min_file_size,
                "type": asset_type,
                "size": size,
                "page": page,
                "withExif": True,
            },
            expected=(200,),
        )
        body = response.json()
        items = body if isinstance(body, list) else body.get("assets", {}).get("items", [])
        for item in items:
            yield item


def sanitize_rating(rating: int | None) -> int | None:
    """Map an incoming rating onto something ``PUT /assets/{id}`` accepts.

    Immich v3 answers HTTP 400 for ``0``; unrated assets report ``0`` in some payloads,
    so anything outside ``{-1, 1..5}`` is dropped rather than sent.
    """
    if rating is None or rating not in VALID_RATINGS:
        return None
    return rating
