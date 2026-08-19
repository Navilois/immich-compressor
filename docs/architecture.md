# Architecture

```
Immich  (Workflow: AssetMetadataExtraction -> filters -> webhook)
   |  POST /webhook   {type, trigger, data.asset}   header: X-Compressor-Token
   v
immich-compressor (FastAPI, one process, one container)
   POST /webhook  -> verify secret, persist job, 202 Accepted    (must return instantly)
   worker (asyncio)
       guard -> download -> encode -> sanity gate -> upload
             -> copy -> tags/fields -> markers -> (deferred) remove original
   GET  /healthz  /stats  /metrics  /jobs[?status=]  /jobs/{id}
   POST /reprocess/{assetId}
```

## Why a separate service

Immich workflow plugins run as WASM (Extism) modules with five host functions —
`searchAlbums`, `createAlbum`, `addAssetsToAlbum`, `addAssetsToAlbums`, `httpRequest`. They
have **no filesystem access**, so a plugin can never transcode anything. The `webhook`
action is the only place to hook in; all the work happens out of band, over the public REST
API.

## Why the endpoint only enqueues

Immich runs the `webhook` action **synchronously** inside its `WorkflowAssetTrigger` job. A
four-minute ffmpeg run there would block or time out the server. So `/webhook` verifies the
shared secret, writes one row, and answers `202` — everything else is the worker's problem.

## The ten steps

| # | Step | Notes |
|---|---|---|
| 1 | Delay | `initial_delay_seconds` (300) so Immich's own thumbnail, ML and OCR jobs finish first |
| 2 | Guards | external library, edited, live photo, locked, trashed, wrong type, too small, existing marker, named people |
| 3 | Download | `GET /assets/{id}/original`, streamed to a temp file; free space checked first |
| 4 | Encode | the preset command, without a shell; `exiftool -TagsFromFile` for stills, with orientation normalised |
| 5 | Sanity gate | ratio, decodability, rotation-aware display size, bit depth, HDR transfer, duration, audio streams, capture date — see [safety.md](safety.md#the-sanity-gate) |
| 6 | Upload | `POST /assets`; the filename gets the `.cmp` marker |
| 7 | Copy | `PUT /assets/copy` — albums, favourite, shared links, stack, sidecar |
| 8 | Fields and tags | re-reads the source, then `PUT /assets/{new}` and `PUT /tags/assets`. Idempotent, and independent of the extraction race |
| 9 | Markers | the `compressor` metadata key on both assets — the hard loop guard, versioned |
| 10 | Remove the original | only after the [verification chain](safety.md#the-verification-chain). `retention_days: 0` runs it inline; anything higher leaves it to a background sweeper |

Step 10 is strictly after 7 and 8, because both of those read from the source.

## Idempotency

Three independent mechanisms, because each covers a different failure:

1. **`INSERT … ON CONFLICT(source_asset_id) DO NOTHING`.** A webhook replay for an asset
   already recorded is a no-op, in *any* state, forever.
2. **The `compressor` metadata marker on the asset**, server-side. Survives losing the local
   database entirely. It is versioned: a marker that records only a giving-up decision (no
   `replacedBy`) and predates the current marker version is retried once, because the sanity
   gate itself has changed since. A marker that records a real replacement always blocks.
3. **The `.cmp` in the filename**, matched by the workflow's regex filter, so most replays
   never reach the service at all.

Every step is also individually idempotent: `PUT /assets/copy`, `PUT /tags/assets` and
`PUT /assets/{id}/metadata` can all be repeated, and the job's persisted state means a crash
between upload and copy resumes rather than uploading twice.

## Rotation and orientation

A portrait phone clip is not stored as a portrait frame. It is coded 1920x1080 and carries a
90° display matrix that the player applies. Stills work the same way through EXIF
`Orientation`. Two consequences, both verified against ffmpeg 7.1 and exiftool 13.25:

| | Without the countermeasure | With it |
|---|---|---|
| **Video** | ffmpeg's default `-autorotate` bakes the rotation into the pixels and drops the matrix, so 1920x1080 comes out as 1080x1920 — and the sanity gate rejects the clip as a resolution change | `-noautorotate` keeps the matrix and the stored frame untouched. Also required for a full-GPU pipeline, where the rotate filter cannot run on hardware frames |
| **Still** | `convert -auto-orient` rotates the pixels, then `exiftool -all:all` copies the source `Orientation` back on top — the image ends up rotated twice | `normalize_orientation: true` keeps `Orientation` out of the copy and pins the output to 1 |

The gate therefore compares **display** sizes, not stored sizes.

## Hardware selection

Collection and decision are separate. `collect_host_facts()` touches the machine — sysfs,
`/dev/dri`, `ffmpeg -encoders`, `vainfo`, `/sys/fs/cgroup/cpu.max`. Everything after it is a
pure function of the resulting `HostFacts`, so the ranking is unit-tested against captured
tool output for Intel Gen9, Intel Gen12, AMD, NVIDIA and a CPU-only box without any test
needing a GPU.

The last stage is not pure: each surviving candidate is confirmed with a real one-frame
encode. See [hardware.md](hardware.md).

## Modules

| File | What |
|---|---|
| `config.py` | settings model, preset validation, fail-fast startup |
| `hardware.py` | device detection, the preset catalog, the CPU budget |
| `models.py` | webhook payload and REST DTOs, all explicitly null-tolerant |
| `api.py` | typed async Immich client, retries on transport errors and 5xx |
| `store.py` | SQLite job store, WAL |
| `encoder.py` | preset execution, exiftool, probes, the sanity gate |
| `pipeline.py` | the ten steps, the worker loop, the trash sweeper |
| `server.py` | FastAPI endpoints |
| `setup_cmd.py` | the guided `setup` command |
| `__main__.py` | CLI |

## What it deliberately is not

One container, one process, SQLite. No web UI, no Postgres, no Redis, no message queue, no
telemetry. The workload is a handful of jobs a day on a home server; anything more would be
infrastructure to maintain in exchange for nothing.
