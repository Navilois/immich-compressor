# immich-compressor

Out-of-band recompression for [Immich](https://immich.app) v3, driven by a workflow webhook.

An Immich workflow fires a webhook when a new asset finishes metadata extraction. This
service downloads the original, recompresses it with ffmpeg/jpegli, uploads the result as
a new asset, carries over everything that can be carried over (album, favourite, shared
links, stack, sidecar, tags, description, rating, GPS, capture date) and — only if you ask
it to — moves the original into the trash after a retention period.

**Why a separate service?** Immich workflow plugins run as WASM (Extism) modules with five
host functions (`searchAlbums`, `createAlbum`, `addAssetsToAlbum`, `addAssetsToAlbums`,
`httpRequest`). They have no filesystem access, so a plugin can never transcode anything.
The `webhook` action is the only place to hook in; all the actual work happens here, over
the public REST API.

---

## Table of contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Setup](#setup)
  - [1. API key and permissions](#1-api-key-and-permissions)
  - [2. Configure the service](#2-configure-the-service)
  - [3. Start it](#3-start-it)
  - [4. Create the Immich workflow](#4-create-the-immich-workflow)
- [Going live safely](#going-live-safely)
- [Operating](#operating)
- [Rollback](#rollback)
- [Verified API behaviour](#verified-api-behaviour-immich-v310)
- [Known limits](#known-limits)
- [Development](#development)

---

## How it works

```
Immich  (Workflow: AssetMetadataExtraction -> filters -> webhook)
   |  POST /webhook   {type, trigger, data.asset}   header: X-Compressor-Token
   v
immich-compressor (FastAPI)
   POST /webhook  -> verify secret, persist job, 202 Accepted    (must return instantly)
   worker (asyncio, concurrency 1)
       guard -> download -> encode -> sanity gate -> upload
             -> copy -> tags/fields -> markers -> (deferred) trash
   GET  /healthz  /stats  /jobs[?status=]  /jobs/{id}
   POST /reprocess/{assetId}
```

The webhook action runs **synchronously** inside Immich's `WorkflowAssetTrigger` job, so
the endpoint only enqueues and returns `202`. A four-minute ffmpeg run there would block
or time out the server.

### Pipeline steps

| # | Step | Notes |
|---|---|---|
| 1 | Delay | `initial_delay_seconds` (default 300) so Immich's own thumbnail/ML/OCR jobs finish first |
| 2 | Guards | external library, edited, live photo, locked, trashed, wrong type, too small, existing marker, named people |
| 3 | Download | `GET /assets/{id}/original`, streamed to a temp file, free space checked first |
| 4 | Encode | preset command, no shell; `exiftool -TagsFromFile` for stills |
| 5 | Sanity gate | size ratio, decodable, same resolution, duration ±0.5 s, same audio stream count, capture date present |
| 6 | Upload | `POST /assets`, filename gets the `.cmp` marker |
| 7 | Copy | `PUT /assets/copy` — albums, favourite, shared links, stack, sidecar (the sidecar indirectly carries tags/description/rating/GPS, see below) |
| 8 | Nudge | Re-reads the source, then `PUT /assets/{new}` for description/rating/GPS/date and `PUT /tags/assets` for tags — idempotent, and independent of the extraction race |
| 9 | Markers | `compressor` metadata key on both assets — the hard loop guard |
| 10 | Deferred trash | `delete_after = now + retention_days`, swept by a background task |

Every step is idempotent and the state is persisted in SQLite, so a crash between upload
and copy resumes rather than duplicating work.

---

## Requirements

- Immich **v3.0.0 or newer** (workflows were introduced there). Developed and verified
  against **v3.1.0**.
- `ffmpeg`, `ffprobe`, `exiftool`, and ImageMagick for stills. The bundled image installs
  all of them. (`cjpegli` is **not** available: Debian/Ubuntu ship `libjxl-tools` without
  it — 0.11.2 in trixie contains only `cjxl`, `djxl` and `jxlinfo`. Build libjxl from
  source if you want it, then switch to the `image-jpegli` preset in
  `config.example.yaml`.)
- Network reachability in both directions: Immich must reach the service's `/webhook`,
  and the service must reach Immich's API.

---

## Setup

### 1. API key and permissions

Create the key in Immich under **Account Settings → API Keys**. Immich v3 has granular
permissions; grant exactly these and nothing more:

| Permission | Needed for |
|---|---|
| `asset.read` | `GET /assets/{id}`, `GET /assets/{id}/metadata` |
| `asset.download` | `GET /assets/{id}/original` |
| `asset.upload` | `POST /assets` |
| `asset.update` | `PUT /assets/{id}`, `PUT /assets/{id}/metadata` |
| `asset.copy` | `PUT /assets/copy` |
| `asset.delete` | `DELETE /assets` — **only needed if `trash_original: true`** |
| `tag.read` | `GET /tags` |
| `tag.create` | `PUT /tags` (upsert by name) |
| `tag.asset` | `PUT /tags/assets` |

Leave `asset.delete` out for the first production run. The service then physically cannot
remove anything.

The key is sent as the `x-api-key` header and is read **only** from the environment
(`IMMICH__API_KEY`). Putting it into `config.yaml` makes the service refuse to start.

### 2. Configure the service

```bash
cp config.example.yaml config.yaml
export IMMICH_API_KEY='<the key from step 1>'
export COMPRESSOR_TOKEN="$(openssl rand -hex 32)"   # shared secret for the webhook
```

Every value in `config.yaml` can be overridden by an environment variable using `__` as
the nesting separator, e.g. `BEHAVIOR__DRY_RUN=false`, `IMMICH__BASE_URL=...`.

The shipped defaults are deliberately inert:

```yaml
behavior:
  dry_run: true          # download nothing, upload nothing, delete nothing
  trash_original: false  # originals are never trashed
```

### 3. Start it

```bash
# Attach to the network your Immich stack already uses.
export IMMICH_NETWORK=immich_default
docker compose up -d
docker compose exec immich-compressor immich-compressor check
```

`check` validates the config and confirms it can reach the Immich API.

### 4. Create the Immich workflow

**Utilities → Workflows → New**, or via the API. This is the exact JSON that was created
and fired successfully against a live v3.1.0 instance:

```json
{
  "name": "compressor",
  "description": "Recompress large videos out of band",
  "trigger": "AssetMetadataExtraction",
  "enabled": true,
  "steps": [
    {
      "method": "immich-plugin-core#assetTypeFilter",
      "config": { "allowedTypes": ["VIDEO"] },
      "enabled": true
    },
    {
      "method": "immich-plugin-core#assetFileFilter",
      "config": { "pattern": "^(?!.*\\.cmp\\.).*$", "matchType": "regex", "usePath": false },
      "enabled": true
    },
    {
      "method": "immich-plugin-core#webhook",
      "config": {
        "url": "http://immich-compressor:8080/webhook",
        "method": "POST",
        "headerName": "X-Compressor-Token",
        "headerValue": "<COMPRESSOR_TOKEN>"
      },
      "enabled": true
    }
  ]
}
```

Create it with:

```bash
curl -X POST "$IMMICH_URL/api/workflows" \
  -H "Authorization: Bearer $SESSION_TOKEN" \
  -H 'Content-Type: application/json' \
  -d @workflow.json
```

> **Gotcha:** `POST /workflows` answers with `"steps": []` even though the steps *were*
> saved. Confirm with `GET /workflows/{id}` — the steps are there.
>
> Workflow endpoints need `workflow.create` / `workflow.read`, which are *not* part of the
> compressor's key. Use an admin session token (or a separate key) for this one call.

> **Immich ignores the webhook's response status.** A 401, 422 or 500 from your service
> is still logged as *"Workflow … executed successfully"*. Never diagnose from the Immich
> side alone — check the compressor's own log, which records rejections at WARNING/ERROR.
>
> **If a workflow stops firing**, restart `immich-server`. Creating or editing a workflow
> normally takes effect immediately (verified), but we did see execution go quiet after a
> workflow run threw `NoResultError` — triggered by an asset being hard-deleted while its
> workflow was executing. A restart cleared it.

Notes on the workflow:

- Trigger is **`AssetMetadataExtraction`**, not `AssetCreate`: only afterwards is
  `exifInfo` populated, and GPS, tags, rating and description are what we need to carry
  over.
- The filename filter is a negative lookahead because `assetFileFilter` has no `inverse`
  option. **Verified working** — Immich's regex engine supports lookaheads. It is only the
  first line of defence; the hard loop guard is the `compressor` metadata marker.
  Without the filter, the compressed upload re-triggers the workflow (confirmed).
- Only one custom header can be configured, which is exactly our shared secret.

---

## Going live safely

1. **Dry run.** Leave `dry_run: true`. Upload a few assets, then
   `docker compose exec immich-compressor immich-compressor report`. Nothing is created,
   changed or deleted on the server — asserted by an automated test.
2. **Real compression, originals kept.** Set `BEHAVIOR__DRY_RUN=false`, leave
   `trash_original: false`. Now both versions exist side by side. Check album membership,
   tags, rating, description, GPS, timeline position, stack and shared links on the new
   asset.
3. **Enable trashing.** Only then set `trash_original: true` and grant `asset.delete`.
   Originals move to the trash after `retention_days` (default 7) and stay recoverable
   until the trash is emptied.

**Disk space is only reclaimed when the Immich trash is emptied.** Until then you are
using *more* space, not less.

---

## Operating

```bash
# Health, including whether Immich is reachable
curl localhost:8080/healthz

# Aggregate statistics: state counts, skip reasons, bytes saved
curl localhost:8080/stats

# Job list, optionally filtered
curl 'localhost:8080/jobs?status=failed'
curl localhost:8080/jobs/<assetId>

# Re-queue one asset (needs the shared secret)
curl -X POST -H "X-Compressor-Token: $COMPRESSOR_TOKEN" \
     localhost:8080/reprocess/<assetId>
```

CLI (inside the container, or locally with `COMPRESSOR_CONFIG` set):

```bash
immich-compressor check                    # config + connectivity
immich-compressor encode /path/clip.mp4    # run a preset offline, print ratio + gate result
immich-compressor report [--json]          # job statistics
immich-compressor reprocess <assetId>      # re-queue
immich-compressor backfill --type VIDEO --limit 50 [--apply]
immich-compressor restore <assetId> ...    # pull originals back out of the trash
```

`encode` is the way to tune a preset: it never talks to Immich, it just runs the command
and the sanity gate against a local file.

### Job states

`queued → running → uploaded → linked → pending_delete → done`, plus `skipped` (with a
`skip_reason`) and `failed`. Skip reasons: `already_compressed`, `too_small`, `wrong_type`,
`no_gain`, `duplicate`, `named_people`, `edited`, `external_library`, `live_photo`,
`locked`, `trashed`, `no_preset`, `dry_run`.

Failures retry with exponential backoff up to `max_attempts` (default 3), then land in
`failed` and show up in `/stats` and `report`.

### Troubleshooting

| Symptom | Where to look |
|---|---|
| Immich says the workflow ran, nothing happens here | The service log. Immich discards the webhook response, so a 401 (wrong `headerValue`) or 422 (payload the service could not parse) is invisible on the Immich side. Both are logged here. |
| Webhook arrives, job never runs | `initial_delay_seconds` (default 300). `GET /jobs/{id}` shows `run_after`. |
| Everything is `skipped: too_small` | `min_size_bytes` defaults to 20 MiB. |
| Everything is `skipped: dry_run` | That is the shipped default. Set `BEHAVIOR__DRY_RUN=false`. |
| `skipped: no_gain` | The preset did not reach `max_ratio` (0.6). Tune it offline with `immich-compressor encode`. |
| Nothing at all in the log | Immich cannot reach the service. Test from inside the Immich container: `docker exec immich_server curl -s -o /dev/null -w '%{http_code}' http://immich-compressor:8080/healthz`. |

---

## Rollback

Nothing this service does is irreversible as long as the Immich trash has not been
emptied.

**1. Stop the flow.**

```bash
# Disable the workflow in Immich (Utilities -> Workflows -> toggle), or:
curl -X PUT "$IMMICH_URL/api/workflows/$WORKFLOW_ID" \
  -H "Authorization: Bearer $SESSION_TOKEN" -H 'Content-Type: application/json' \
  -d '{"enabled": false}'

docker compose stop immich-compressor
```

**2. Restore trashed originals.**

```bash
# Everything this service trashed:
docker compose run --rm immich-compressor restore --all-pending

# Or individual assets:
docker compose run --rm immich-compressor restore <assetId> <assetId>
```

Equivalent to `POST /trash/restore/assets {"ids": [...]}`, or **Utilities → Trash →
Restore** in the UI. Verified: the asset comes back with `isTrashed: false`.

**3. Remove the replacements** (optional). They are identifiable in three ways: the
filename ends in `.cmp.<ext>`, the `compressor` metadata key is set, and its value carries
`sourceId`. Delete them normally; the restored originals keep their albums and tags.

**4. Clear the service state** if you want a clean slate:

```bash
docker compose down
docker volume rm immich-compressor_compressor-state
```

> **If the trash was already emptied, the original is gone.** There is no undo. Set
> `retention_days` generously and do not empty the trash until you have spot-checked the
> replacements.

---

## Verified API behaviour (Immich v3.1.0)

Everything below was checked against a running v3.1.0 instance, not against docs. The
captured webhook payloads are committed as test fixtures in `tests/fixtures/`.

| Assumption | Result |
|---|---|
| Webhook body is `{type, trigger, data.asset}` | ✅ exact; also carries `createdAt`, `updatedAt`, `status`, `duration` |
| `checksum` arrives as `{"type":"Buffer","data":[…]}` | ✅ — ignored, we do not need it |
| `duration` unit | integer **milliseconds** (`20000` for a 20 s clip), same unit as `POST /assets` |
| `exifInfo.tags` are names, not IDs | ✅ |
| Upload fields: `assetData`, `fileCreatedAt`, `fileModifiedAt`, `filename`, `isFavorite`, `visibility`, `duration` | ✅; `deviceAssetId`/`deviceId` no longer exist in v3 |
| `PUT /assets/copy` copies albums, favourite, shared links, stack, sidecar | ✅ |
| `PUT /assets/copy` does **not** copy tags, description, rating, GPS | ⚠️ half right — no direct copy, but the copied XMP sidecar carries them (see below) |
| `PUT /assets/copy` does not copy people/faces or the metadata KV | ✅ |
| Asset metadata KV accepts free string keys and nested object values | ✅ |
| Duplicate upload returns `{"status":"duplicate","id":<existing>}` | ✅ |
| `DELETE /assets` without `force` is a soft delete | ✅ (`isTrashed: true`, restorable) |
| Negative-lookahead regex in `assetFileFilter` | ✅ works |
| A compressed upload re-triggers the workflow | ✅ — loop protection is genuinely required |

### Where the original plan was wrong

1. **`exifInfo.tags` arrives as `null`, not `[]`, for an asset without tags.** A pydantic
   default only fills a *missing* key, so the webhook rejected every untagged asset with
   HTTP 422 — and because Immich's webhook action **ignores the response status and logs
   the workflow as "executed successfully"**, this was invisible from both sides. Every
   nullable collection, bool and string in the payload models is now explicitly
   null-tolerant, and the service logs its own 422s at ERROR level. If you write another
   webhook consumer for Immich, do the same: a silent 422 is indistinguishable from
   success.
2. **`PUT /assets/copy` moves tags, description, rating and GPS across — but through the
   sidecar, not through a field copy.** `copy()` in `asset.service.ts` only calls
   `copyAlbums`, `copySharedLinks`, `copyStack`, the `isFavorite` update and
   `copySidecar`; there is no `copyTags` anywhere in the Immich source. What actually
   happens is that `copySidecar` writes the source's XMP next to the new original,
   registers it as a sidecar file and queues `AssetExtractMetadata` for the target — and
   metadata extraction then reads those fields back out of the XMP. The XMP itself is
   produced by the `SidecarWrite` job, which Immich queues on every tag/untag and every
   metadata update, and which writes exactly `Description`, `ImageDescription`,
   `DateTimeOriginal`, `GPSLatitude`, `GPSLongitude`, `Rating`, `TagsList` — precisely the
   set we observed surviving.

   The distinction matters, because the transfer silently does not happen when:
   - `sidecar: false` is passed to the copy call;
   - the source has no XMP yet — `copySidecar` returns early on a missing sidecar path,
     which is the case if `SidecarWrite` has not run yet (e.g. copying immediately after
     tagging);
   - the field is not in that XMP list (people/faces, the metadata KV and album/stack
     membership are handled elsewhere or not at all).

   That is why step 8 exists. The explicit `PUT /assets/{id}` and `PUT /tags/assets` are
   not redundant insurance — they are what makes the outcome deterministic instead of
   dependent on sidecar state and job ordering.
3. **The webhook payload is a snapshot, not current state.** It is produced when metadata
   extraction finishes, but the job runs `initial_delay_seconds` later (default 5 min) —
   long enough for the user to have added tags, a description or a rating in the UI.
   Reproduced in the full-stack run: the stored payload had `tags: []`, `description: ""`,
   `rating: null`. The pipeline therefore re-reads `GET /assets/{id}` and prefers the live
   values, falling back to the payload.
4. **Inline `metadata` on upload is unusable.** `POST /assets` is multipart, so
   `metadata[0][value]` can only be expressed as nested form fields
   (`metadata[0][value][v]=1`), and every value arrives as a **string** (`{"v": "1"}`).
   Passing a JSON string is rejected outright (`expected record, received string`).
   The marker is therefore written with a separate, properly typed
   `PUT /assets/{id}/metadata` call.
5. **`GET /assets/{id}/metadata/{key}` returns 400, not 404, for a missing key.** The
   marker check uses the list endpoint `GET /assets/{id}/metadata`, which returns `[]`.
6. **`rating: 0` is rejected with HTTP 400** in v3 (*"Rating must be -1 (rejected), 1–5
   (starred), or null (unrated); 0 is not valid"*). Ratings outside `{-1, 1..5}` are
   dropped instead of forwarded.
7. **Metadata extraction races the field nudge.** Immich runs metadata extraction
   asynchronously ~300 ms after `POST /assets` returns, and that job **overwrites**
   `description` and `rating` from the file. Writing before it lands silently loses them.
   The pipeline now waits for extraction (`post_upload_settle_s`, default 30 s) before
   step 8.
8. **`POST /workflows` returns `"steps": []`** even though the steps were persisted.
   Cosmetic, but it looks like a failure if you only read the create response.
9. **`-map_metadata 0 -movflags use_metadata_tags` is not enough on its own.** It preserves
   QuickTime `CreateDate`, GPS, `Make` and `Model`, but drops XMP `Description`, `Rating`
   and `Subject` — confirming that the explicit field nudge in step 8 is load-bearing, not
   belt-and-braces.
10. **x265 ignores the cgroup CPU limit.** It sizes its thread pool from the host core
   count (8 threads on a 2-vCPU container). The default preset pins
   `-x265-params pools=2 -threads 2`.
11. **`cjpegli` does not exist as a package.** The plan's stills preset assumed it comes
   with `libjxl-tools`; Debian trixie's 0.11.2 build ships only `cjxl`, `djxl` and
   `jxlinfo`. The shipped IMAGE preset uses ImageMagick instead (verified: 371 kB → 244 kB
   with full EXIF/GPS/rating carry-over via exiftool).

---

## Known limits

- **The asset ID changes.** There is no replace endpoint in the API, so the compressed
  version is a new asset with a new ID. External deep links to the old asset break.
- **Faces and people are re-detected** for the new asset; manually assigned names can be
  lost. `skip_if_named_people: true` (the default) avoids the problem by never touching
  such assets.
- **Mobile re-upload.** The app deduplicates by checksum
  (`POST /assets/bulk-upload-check`). Once the original is *permanently* deleted, a device
  that still holds the file can upload it again, and the service will compress it again —
  the marker does not help, because it is a new asset. Watch this during the rollout with
  a real device; there is no clean fix from this side.
- **Space is only reclaimed when the trash is emptied.**
- **ML load.** Every new asset re-triggers thumbnails, metadata, smart search, faces and
  OCR. On a small box, keep `concurrency: 1`.
- **External libraries and live photos are never touched**, by design.
- **Timeline position depends on GPS in the file.** Immich derives the time zone from GPS
  and computes `localDateTime` from `dateTimeOriginal` in that zone. As long as the
  encoder preserves both (the default does, and the sanity gate enforces the capture
  date), original and replacement land at the same spot.
- The compressor's own state lives in a SQLite file. Back up the
  `compressor-state` volume if you care about the report history.

---

## Development

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[dev]'

.venv/bin/python -m ruff check .
.venv/bin/python -m pytest -m 'not live'     # unit tests, mocked HTTP
```

### Live tests

`tests/test_e2e_live.py` runs against a real instance. It is marked `live` and skipped
unless the environment is set:

```bash
mkdir -p testinstance
cp testinstance/example.env testinstance/.env   # then set a DB_PASSWORD of your own
docker compose --env-file testinstance/.env -f docker-compose.test.yaml up -d

export E2E_IMMICH_URL=http://172.25.0.2:2283/api
export E2E_IMMICH_KEY=<api key>
.venv/bin/python -m pytest -m live
```

The live suite uploads its own throwaway assets, drives the full pipeline, asserts that
album/tags/rating/description/GPS/timeline position survived, checks that a second webhook
for the same asset is a no-op, exercises trash + restore, and cleans up after itself.
`docker-compose.test.yaml` brings up a complete Immich v3.1.0 stack (machine learning is
behind the `ml` profile, since it costs ~2 GB of RAM).

### Layout

```
├── src/immich_compressor/
│   ├── __main__.py   CLI
│   ├── config.py     pydantic-settings, preset validation (fail fast)
│   ├── models.py     webhook payload + REST DTOs
│   ├── api.py        typed async Immich client
│   ├── store.py      SQLite job store (WAL)
│   ├── encoder.py    preset execution, exiftool, sanity gate
│   ├── pipeline.py   the ten steps, worker loop, trash sweeper
│   └── server.py     FastAPI endpoints
└── tests/
    ├── fixtures/     webhook payloads captured from a live v3.1.0 instance
    └── test_*.py
```

### Security notes

- Subprocesses are started with `asyncio.create_subprocess_exec` and an argv list — never
  a shell. Preset commands are `shlex.split` at load time and rejected outright if they
  contain `|`, `&&`, `;`, `>`, `<`, `` ` `` or `$(`.
- The webhook secret is compared with `hmac.compare_digest`.
- Secrets come from the environment only; the service refuses to start if they appear in
  `config.yaml`.
- The container runs as a non-root user and needs no capabilities.
