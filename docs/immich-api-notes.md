# Verified Immich API behaviour

**Everything on this page was checked against a running Immich instance, not against
documentation.** It was written against **Immich v3.1.0** (last verified **2026-08-23**) and
re-checked against **Immich v3.3.1** on **2026-10-10** — [Re-checked on
v3.3.1](#re-checked-on-v331) says which items that covers; everything else still stands on
v3.1.0 alone. The captured webhook payloads from both versions are committed as test
fixtures in `tests/fixtures/`.

If you are writing another Immich integration, this is the page worth reading. Several of
these cost a full debugging round to find.

## The workflow system

Merged in [PR #26727](https://github.com/immich-app/immich/pull/26727) (2026-05-18), preview
release in v3.0.0, UI under **Utilities → Workflows**.

- **Triggers:** `AssetCreate` and `AssetMetadataExtraction` only.
- **Filters:** `assetFileFilter` (name or path; contains / startsWith / exact / **regex**,
  with `usePath`), `assetTypeFilter`, `assetExifFilter`, `assetDateFilter`,
  `assetLocationFilter`, `assetMissingTimeZoneFilter`. `usePath` only takes effect from
  v3.2.0: v3.1.0 matched `originalFileName` whatever it said
  ([immich-app/immich#30394](https://github.com/immich-app/immich/pull/30394) — read in the
  source, not measured). `setup` sends `usePath: false`, which behaves the same on both.
- **Steps run in their stored order only from v3.3.1**
  ([immich-app/immich#32240](https://github.com/immich-app/immich/pull/32240)). The query
  that loads a run had no `ORDER BY` before, and upstream reported action steps running
  after a filter had halted the run on v3.2.x
  ([immich-app/immich#32083](https://github.com/immich-app/immich/issues/32083)). On v3.3.1
  the steps `setup` creates are stored as `order` 0, 1, 2 and the filters gate as they
  should: eight uploads named `*.cmp.*` fired no webhook, and neither did the five such
  assets when "Extract Metadata → All" re-ran the workflow over the library. On an older
  release, what slips past the filters is refused by the service itself — the `.cmp.`
  filename check, the metadata marker and `enabled_types`.
- **Actions:** `assetArchive`, `assetLock`, `assetVisibility`, `assetFavorite`,
  `assetAddToAlbums`, **`webhook`**.
- Plugins run as WASM (Extism) with five host functions — `searchAlbums`, `createAlbum`,
  `addAssetsToAlbum`, `addAssetsToAlbums`, `httpRequest`. **No filesystem access**, so a
  plugin can never transcode anything. The `webhook` action is the only place to hook in,
  which is why this project is a separate service.
- The webhook action can configure exactly **one** `headerName`/`headerValue` pair. That is
  the shared secret.

## The payload

The action posts exactly this, with no `config` and no `workflow` object:

```jsonc
{ "type": "AssetV1", "trigger": "AssetMetadataExtraction", "data": { "asset": { … } } }
```

`data.asset` carries `id, ownerId, type, originalPath, originalFileName, fileCreatedAt,
fileModifiedAt, localDateTime, isFavorite, isOffline, isExternal, isEdited, libraryId,
livePhotoVideoId, stackId, duplicateId, deletedAt, visibility, status, duration, checksum`
and an `exifInfo` object.

**From v3.2.0 it also carries `tags`**, added with the tag trigger
([immich-app/immich#29043](https://github.com/immich-app/immich/pull/29043)): whole tag rows,
`{id, value, color, parentId, createdAt, updatedAt}`, and `[]` for an untagged asset. The
names stay in `exifInfo.tags`, which is still `null` rather than `[]` when there are none
([1](#1-exifinfotags-arrives-as-null-not-)). Captured from v3.3.1; the ingest ignores the new
field. Nothing else in the payload changed — every field of the v3.1.0 captures is in the
v3.3.1 ones, with the same type.

## Behaviour table

| Assumption | Result |
|---|---|
| Webhook body is `{type, trigger, data.asset}` | ✅ exact; also carries `createdAt`, `updatedAt`, `status`, `duration` |
| `checksum` arrives as `{"type":"Buffer","data":[…]}` | ✅ **in the webhook payload only.** `GET /assets/{id}` returns the same digest as a base64 string (`"02MpaJkpzGHNbGwxWtencVNK7uY="`), and `base64(sha1(file)) == checksum` holds exactly — that is what the delete gate compares |
| `duration` unit | integer **milliseconds** (`20000` for a 20 s clip), the same unit `POST /assets` expects |
| `exifInfo.tags` are names, not ids | ✅ |
| Upload fields: `assetData`, `fileCreatedAt`, `fileModifiedAt`, `filename`, `isFavorite`, `visibility`, `duration` | ✅; **`deviceAssetId`/`deviceId` no longer exist in v3** — every older tutorial is wrong here |
| `PUT /assets/copy` copies albums, favourite, shared links, stack, sidecar | ✅ |
| `PUT /assets/copy` does **not** copy tags, description, rating, GPS | ⚠️ half right — no direct copy, but the copied XMP sidecar carries them (see below) |
| `PUT /assets/copy` does not copy people/faces or the metadata KV | ✅ |
| Asset metadata KV accepts free string keys and nested object values | ✅ — which is what makes it usable as an idempotency marker |
| Duplicate upload returns `{"status":"duplicate","id":<existing>}` | ✅ |
| `DELETE /assets` without `force` is a soft delete | ✅ (`isTrashed: true`, restorable) |
| `DELETE /assets` with `force: true` is a *permanent* delete | ✅ — the spec only says "force delete even if in use", so this was measured: the asset answers HTTP 400 afterwards, does not appear in the trash view, and its files are unlinked from the upload directory. It behaves the same on an asset already in the trash, which is why `POST /trash/empty` is never needed. **Re-verified 2026-08-23** on a stage-4 run against a live library: the row is gone from `asset` entirely — not flagged — and `asset_exif`, `asset_file` and `album_asset` keep no orphan. The message is `Not found or no asset.read access`, the same permission-shaped wording `/trash/restore/assets` uses, not the bare `Not found` recorded here earlier |
| `POST /trash/restore/assets` on a force-deleted asset | HTTP 400 `Not found or no asset.delete access` — not a quiet no-op. **Re-confirmed 2026-08-23, and the whole request fails with it:** a single unknown id in `{ids}` costs every other id in the same body, and the answer never names it. `restore --all-pending` therefore batches, and halves a refused batch until each unknown id stands alone — see [safety.md](safety.md#rolling-back) |
| `POST /trash/restore/assets` on an asset that is **not** trashed | HTTP 200 `{"count":1}` — a harmless no-op, and the asset stays `active`. Measured 2026-08-23 on an isolated pair: only ids the server cannot find *at all* are fatal, so `{active, gone}` answers 400 while `{active}` alone answers 200. The `count` is the number of ids in the request, not the number restored: this no-op counts 1, exactly like a real restoration. Re-measured on v3.3.1, and both versions' source return `ids.length` |
| Negative-lookahead regex in `assetFileFilter` | ✅ works |
| A compressed upload re-triggers the workflow | ✅ — loop protection is genuinely required |

## Endpoints this project uses

| Purpose | Call | Note |
|---|---|---|
| Fetch the original | `GET /assets/{id}/original` | Stream it; do not buffer in RAM |
| Upload | `POST /assets` (multipart) | `assetData`, `fileCreatedAt` and `fileModifiedAt` are required |
| Upload response | `AssetMediaResponseDto` = `{id, status}`, `status ∈ {created, duplicate}` | `duplicate` means the file already exists — do **not** delete the original |
| Links | `PUT /assets/copy` `{sourceId, targetId, albums, favorite, sharedLinks, stack, sidecar}` | See the caveat below |
| Tags | `GET /tags`, `PUT /tags` (upsert by name), `PUT /tags/assets` `{assetIds, tagIds}` | Both are idempotent |
| Fields | `PUT /assets/{id}` `{description, rating, latitude, longitude, dateTimeOriginal, isFavorite, visibility}` | Only non-null fields are sent |
| Marker | `GET`/`PUT /assets/{id}/metadata`, body `{items:[{key, value}]}` | Free string key, object value |
| Delete | `DELETE /assets` `{ids, force}` | `force: false` → trash |
| Restore | `POST /trash/restore/assets` `{ids}` | |
| Detail | `GET /assets/{id}` | For the people check, the checksum and the live field values |
| Backfill | `POST /search/metadata` `{type, size, page, withExif}` | Optional, CLI only. A paged walk of the library. All three parameters *are* applied, `size` caps at 1000, and `nextPage` arrives as a string — see [16](#16-post-searchmetadata-applies-type-size-and-page). The client re-checks type and repeated pages anyway, because [15](#15-post-searchlarge-assets-ignores-type-and-size) does not. `type`, `page` and `nextPage` are deprecated since v3.2.0 and still work on v3.3.1 |

## API key permissions

Grant exactly these and nothing more. Header is `x-api-key`.

| Permission | Needed for |
|---|---|
| `asset.read` | `GET /assets/{id}`, `GET /assets/{id}/metadata` |
| `asset.download` | `GET /assets/{id}/original` |
| `asset.upload` | `POST /assets` |
| `asset.update` | `PUT /assets/{id}`, `PUT /assets/{id}/metadata` |
| `asset.copy` | `PUT /assets/copy` |
| `asset.delete` | `DELETE /assets` — **only when `trash_original: true`**; the same permission covers the `force: true` delete |
| `tag.read` | `GET /tags` |
| `tag.create` | `PUT /tags` |
| `tag.asset` | `PUT /tags/assets` |

`workflow.create` and `workflow.read` are **not** in this list. Creating the workflow is a
one-off that wants a session token, not a permanent widening of a long-lived key.
`immich-compressor setup` names any permission the key is missing, by asking the server one
deliberately inert request per permission.

## Where the original plan was wrong

These are the findings that changed the implementation.

### 1. `exifInfo.tags` arrives as `null`, not `[]`

A pydantic default only fills in a *missing* key, so the webhook rejected every untagged
asset with HTTP 422. And because **Immich's webhook action ignores the response status and
logs the workflow as "executed successfully"**, this was invisible from both sides.

Every nullable collection, bool and string in the payload models is now explicitly
null-tolerant, and the service logs its own 422s at ERROR level. If you write another
webhook consumer for Immich, do the same: a silent 422 is indistinguishable from success.

### 2. `PUT /assets/copy` moves tags, description, rating and GPS — but through the sidecar

`copy()` in `asset.service.ts` only calls `copyAlbums`, `copySharedLinks`, `copyStack`, the
`isFavorite` update and `copySidecar`. There is no `copyTags` anywhere in the Immich source.

What actually happens is that `copySidecar` writes the source's XMP next to the new
original, registers it as a sidecar file and queues `AssetExtractMetadata` for the target —
and metadata extraction then reads those fields back out of the XMP. The XMP itself is
produced by the `SidecarWrite` job, which Immich queues on every tag/untag and every
metadata update, and which writes exactly `Description`, `ImageDescription`,
`DateTimeOriginal`, `GPSLatitude`, `GPSLongitude`, `Rating`, `TagsList` — precisely the set
observed surviving.

The distinction matters, because the transfer silently does not happen when:

- `sidecar: false` is passed to the copy call;
- the source has no XMP yet (`copySidecar` returns early on a missing sidecar path, which is
  the case if `SidecarWrite` has not run — e.g. copying immediately after tagging);
- the field is not in that list (people/faces, the metadata KV, album and stack membership
  are handled elsewhere or not at all).

That is why the pipeline writes those fields explicitly afterwards. The extra
`PUT /assets/{id}` and `PUT /tags/assets` are not belt-and-braces — they are what makes the
outcome deterministic instead of dependent on sidecar state and job ordering.

### 3. The webhook payload is a snapshot, not current state

It is produced when metadata extraction finishes, but the job runs `initial_delay_seconds`
later (5 minutes by default) — long enough for the user to have added tags, a description or
a rating in the UI. Reproduced in a full-stack run: the stored payload had `tags: []`,
`description: ""`, `rating: null` while the live asset had all three. The pipeline therefore
re-reads `GET /assets/{id}` and prefers the live values.

### 4. Inline `metadata` on upload is unusable

`POST /assets` is multipart, so `metadata[0][value]` can only be expressed as nested form
fields (`metadata[0][value][v]=1`), and every value arrives as a **string** (`{"v": "1"}`).
Passing a JSON string is rejected outright (`expected record, received string`). The marker
is written with a separate, properly typed `PUT /assets/{id}/metadata` call instead.

### 5. `GET /assets/{id}/metadata/{key}` returns 400, not 404, for a missing key

The marker check uses the list endpoint `GET /assets/{id}/metadata`, which cleanly returns
`[]`.

### 6. `rating: 0` is rejected with HTTP 400

*"Rating must be -1 (rejected), 1–5 (starred), or null (unrated); 0 is not valid"*. Unrated
assets report `0` in some payloads, so anything outside `{-1, 1..5}` is dropped rather than
forwarded.

### 7. Metadata extraction races the field update

Immich runs metadata extraction asynchronously ~300 ms after `POST /assets` returns, and
that job **overwrites** `description` and `rating` from the file. Writing before it lands
silently loses them. The pipeline waits for extraction (`post_upload_settle_s`, 30 s) first,
using `dateTimeOriginal` flipping from `null` to a value as the observable signal.

### 8. `POST /workflows` returns `"steps": []`

Even though the steps were persisted. Cosmetic, but it looks like a failure if you only read
the create response. Confirm with `GET /workflows/{id}`.

### 9. `-map_metadata 0 -movflags use_metadata_tags` is not enough on its own

It preserves QuickTime `CreateDate`, GPS, `Make` and `Model`, but drops XMP `Description`,
`Rating` and `Subject` — confirming that the explicit field update is load-bearing.

### 10. x265 ignores the cgroup CPU limit

It sizes its thread pool from the host core count — 8 threads inside a 2-vCPU container.
The service now reads `/sys/fs/cgroup/cpu.max` and pins `pools`/`-threads` to the real
budget. See [hardware.md](hardware.md#the-cpu-budget).

### 11. `cjpegli` does not exist as a package

Debian trixie's `libjxl-tools` 0.11.2 ships only `cjxl`, `djxl` and `jxlinfo`. The stills
preset uses ImageMagick instead — verified: 371 kB → 244 kB with full EXIF/GPS/rating
carry-over via exiftool.

### 12. `AssetMetadataExtraction` fires in bulk, not once per upload

`AssetCreate` fires once per asset. Metadata extraction is a maintenance operation that can
be started at any time from **Administration → Jobs → Extract Metadata**, and every run emits
the trigger again.

Traced through the v3.1.0 server bundle: `handleQueueMetadataExtraction`
(`metadata.service.js`) queues one `AssetExtractMetadata` job per asset with `data: { id }`
and **no `source` field**; `handleMetadataExtraction` then emits `AssetMetadataExtracted`
with `source: undefined`; and `onAssetMetadataExtracted` (`workflow-execution.service.js`)
only returns early for `source === 'sidecar-write'`. So **"Extract Metadata → All" fires the
workflow for every asset in the library.**

This is what `behavior.max_asset_age_hours` exists for: the payload's `createdAt` dates the
upload, so a re-trigger for an asset that has been in the library a while is refused at
ingest. See [operations.md](operations.md#the-metadata-extraction-trap).

### 13. A scoped API key gets 403 on `/users/me`, and that is not an error

Verified on v3.1.0 with two keys on the same instance:

| Key | `GET /users/me` |
|---|---|
| Valid, with the permissions this service needs | `403 {"message": "Missing required permission: user.read"}` |
| Bogus | `401 {"message": "Invalid API key"}` |
| No key at all | `401 {"message": "Authentication required"}` |

So **401 means the key is wrong and 403 means the key is right but scoped.** Immich runs
the permission guard after authenticating, which is also what makes the inert permission
probes in `immich-compressor setup` work at all: a 403 identifies a missing permission, and
the body names it exactly as the API-key editor spells it.

`GET /server/version` cannot stand in for a key check — it answers 200 for a bogus key,
because it needs no authentication.

### 14. Immich ignores the webhook's response status

A 401, 422 or 500 from your service is still logged as *"Workflow … executed successfully"*.
Never diagnose from the Immich side alone. Related: if a workflow stops firing, restart
`immich-server` — execution was observed going quiet after a workflow run threw
`NoResultError`, triggered by an asset being hard-deleted while its workflow was executing.
A restart cleared it.

v3.2.0 added a per-workflow log (`logging: true`,
[immich-app/immich#29878](https://github.com/immich-app/immich/pull/29878)). It does not
change this: in the v3.3.1 source the webhook step still discards the answer, so a run this
service refused is logged as completed there too. Read in the source, not measured.

### 15. `POST /search/large-assets` ignores `type` and `size`

Measured on v3.1.0, on a library of 48 958 photos and 4 717 videos — and not re-measured
since, because nothing here calls this endpoint any more:

| Request | Answer |
|---|---|
| `{minFileSize: 1048576, type: "VIDEO", size: 5}` | 250 items, all `.mp4` |
| `{minFileSize: 1048576, type: "IMAGE", size: 5}` | the **same** 250 items, all `.mp4` |

Both fields are accepted and neither is applied. The consequence is worse than it looks:
without a client-side check, `backfill --type IMAGE` does not merely return nothing useful,
it queues *videos* — so the stills backfill is unreachable, and anybody who believes they
are testing 50 photos re-encodes 50 videos. Harmless while `dry_run` is on; not harmless
from [stage 3](safety.md#stage-3--move-originals-to-the-trash).

`backfill` therefore filters on `item["type"]` itself and says how many foreign-type
results it discarded, rather than reporting an empty run that looks like an empty library.

**What replaced it.** The client-side filter fixes the correctness problem and not the
reachability one: the endpoint answers with one fixed set of results — 250 items on the
library above, every one of them a video — so the stills half of a library cannot be
reached through it at all, whatever the caller filters afterwards. The backfill walks
`POST /search/metadata` instead and keeps its own inventory of what it found.

`/search/metadata` was the open question when this was written; it is
[16](#16-post-searchmetadata-applies-type-size-and-page) now. It
applies all three. The scanner keeps filtering by type and watching for a repeated page
anyway — the defence costs one comparison per item, and the two endpoints in this family
disagree with each other on exactly this point.

### 16. `POST /search/metadata` applies `type`, `size` and `page`

Measured on v3.1.0 on 2026-08-23, against a live library of 49 046 photos and 4 729 videos.
This is the endpoint `backfill scan` walks, and it behaves the way
[15](#15-post-searchlarge-assets-ignores-type-and-size) led nobody to expect.

| Request | Answer |
|---|---|
| `{type: "IMAGE", size: 5, page: 1}` | 5 items, **all `IMAGE`** |
| `{type: "VIDEO", size: 5, page: 1}` | 5 items, **all `VIDEO`** — zero id overlap with the `IMAGE` set |
| `size: 1 / 5 / 17 / 100 / 250 / 1000` | exactly 1 / 5 / 17 / 100 / 250 / 1000 items |
| `size: 1001` | **HTTP 400** `Validation failed`, `{"code": "too_big", "maximum": 1000, "inclusive": true}` |
| `page: 1 / 2 / 3` | three different sets, zero id overlap between consecutive pages |
| `nextPage` | a JSON **string** (`"2"`), and **`null`** on the last page |
| `total` | the items on **this page**, not the library total |
| Ordering | `fileCreatedAt` **descending** |

Three things are worth spelling out.

**`size` has a hard ceiling of 1000, and exceeding it is a 400, not a clamp.** So
`DEFAULT_PAGE_SIZE = 1000` in `backfill.py` sits exactly on the maximum, and a walk of this
library costs 50 requests for the stills and 5 for the videos — 9.42 s and 0.60 s of request
time respectively, 16.5 s wall for a full `backfill scan` including the guard evaluation.

**`total` does not mean what the name suggests.** It was 1000 on every full page and then
729 on the last video page and 46 on the last image page — the size of the page, not the
size of the library. Anything reading it as a library count is wrong; `AssetPage.total` is
parsed but deliberately never used.

**`nextPage` is a string.** `"2"`, not `2`, which is why `_as_page_number` normalises it.
It goes to JSON `null` on the last page, so `paged` plus a null `nextPage` is a real
end-of-library signal here — unlike `/search/large-assets`, which answers with a bare array
and no envelope at all.

The ordering is by `fileCreatedAt` descending: 3 000 items across three pages produced zero
out-of-order transitions, and it is *not* size-ordered. `backfill run --order size` therefore
sorts the inventory itself; there is no server-side order that would save it the work.

**What did not change.** `/search/large-assets` was re-checked on the same instance the same
day and [15](#15-post-searchlarge-assets-ignores-type-and-size) still holds exactly:
`{minFileSize: 1048576, type: "VIDEO", size: 5}` and the same request with `type: "IMAGE"`
both answer with the identical 250 videos. Two endpoints in one family, opposite behaviour
on the same field name — which is the reason the client-side type filter and the
repeated-page check stay where they are.

**Fields this endpoint accepts and ignores.** `isTrashed` and `withDeleted` are both taken
without complaint and neither selects trashed assets; a request carrying them returns the
same live assets as one without. **`trashedAfter`** is the field that works — that is how
the trash view was confirmed to hold a specific asset id.

**On v3.3.1 the fields still work, but they are on notice.** Re-measured on 2026-10-10 on a
throwaway library of six assets: `type` is applied, `page` pages without overlap, `nextPage`
is `"2"`, `"3"` and then `null`, and `size: 1001` is the same `400 too_big`. What changed is
their status. v3.2.0 deprecated `type`, `page` and `order` on the request and `nextPage` on
the response, in favour of `filter`, `orderBy`, `cursor` and `nextCursor`
([immich-app/immich#30179](https://github.com/immich-app/immich/pull/30179)), and the
v3.3.1 source marks the old shape `TODO(v4): remove`. The two shapes do not mix:
`{type, page, size, cursor}` answers `400` with `Deprecated field type cannot be combined
with cursor`. So `backfill scan` runs unchanged on v3.3.1 and has to move to `cursor`
before Immich v4. Ordering and `total` were not re-measured — every asset in that library
carried the same `fileCreatedAt`, and one page held all of them.

### 17. Sync endpoints reject API keys outright, whatever their permissions

Measured on v3.1.0 on 2026-08-26, against a throwaway instance, with a key holding `all`:

| Request | Credential | Result |
|---|---|---|
| `POST /sync/stream` | `x-api-key` | `403 {"message": "Sync endpoints cannot be used with API keys"}` |
| `POST /sync/ack` | `x-api-key` | `403`, same message |
| `POST /sync/stream` | `Authorization: Bearer <session token>` | `200` |
| `GET /users/me` | `x-api-key` holding `all` | `200` — but see note 13 for a scoped key |

This is not a permission that can be granted — the refusal names the *credential type*, and
a key scoped to `all` is refused exactly like a narrow one. Sync is for logged-in clients,
and the phone is a logged-in client.

Three consequences:

**The shim's relaying is unaffected.** On its two proxied routes it is a pipe: it relays
the caller's own credentials verbatim and never substitutes this service's key, so the
phone's session token is what reaches Immich.

**Owner resolution is not**, and only the upload-check direction needs it. It asks
`GET /users/me`, and note 13 above is the one that governs: a session token or a key
holding `user.read` answers 200, but a key scoped to just the permissions this service
needs answers **403**. An unresolved owner makes `translate_upload_check` translate nothing
and forward the body unchanged — fail-open, so nothing breaks, but the upload-check
translation is a silent no-op for that caller. Measured on 2026-08-27 against the
production instance: this service's own key gets 403 on `/users/me`, and a replaced
original's checksum consequently came back `accept` through the shim instead of the
`reject`/`duplicate` its replacement earns. The phone is unaffected — it presents a session
token, and it does not use that route at all ([shim.md](shim.md#limits)). A key-based
client that wants the translation must hold `user.read`.

**The live test was affected**, and silently. `tests/test_e2e_live.py` drove the sync stream
with `E2E_IMMICH_KEY`, got a 403, and called `pytest.skip` — so
`test_live_touch_makes_the_sync_stream_reoffer_an_asset`, the one test standing behind the
claim that a touch re-offers an asset, never executed on any machine. It now takes
`E2E_IMMICH_EMAIL` and `E2E_IMMICH_PASSWORD` and logs in.

### 18. A sync pass is acked per type, not per response

The stream's last line is always `SyncCompleteV1`, and acking it advances no asset
checkpoint. Acking only the final line of a response therefore drains nothing: measured on
v3.1.0, six consecutive passes that acked the last line returned the identical nine lines
every time. Collecting the last `ack` of *each* type and sending them together drained the
same backlog in a single pass, leaving only the `SyncCompleteV1` terminator.

So "the next pass is empty" is never a usable assertion — a pass always carries the
terminator. What a checkpoint actually means is that no asset line comes back, which is
what `_offered_ids` in the live suite checks.

This is what a real client does, and the reason is visible in the ack format itself:
`AssetV2|01a03ecd-6ec1-75f6-906c-9127d50198cc` is `<type>|<checkpoint>`, one cursor per
type rather than one per connection.

### 19. `GET /assets/{id}/original` without `edited` is the original

Between v3.1.0 and v3.3.1 the OpenAPI spec dropped the `default: false` of the `edited`
query parameter, which reads like a change of default. It is not one. Measured on v3.3.1
on 2026-10-10, on a photo given a 90° rotate edit:

| Request | Answer |
|---|---|
| no `edited` parameter | the uploaded bytes, exactly |
| `edited=false` | the uploaded bytes, exactly |
| `edited=true` | the edited rendition — different bytes, about half the size |

The asset's `checksum` stays the SHA-1 of the upload, and `isEdited` turns `true`. The
service downloads without the parameter and skips edited assets at the guard anyway, so it
would not have been affected either way.

## Re-checked on v3.3.1

On 2026-10-10, against a throwaway v3.3.1 instance — `docker-compose.test.yaml` with
`IMMICH_VERSION=v3.3.1`, and a library of a few test assets rather than a real one:

- **The live suite** passed, 5 of 5 with none skipped, the two `/sync/stream` tests included.
- **A real webhook, end to end.** The workflow `immich-compressor setup` creates was fired
  by Immich itself for a video and a photo and both went through the pipeline. Those bodies
  are `tests/fixtures/webhook_video_v3.3.1.json` and `webhook_image_v3.3.1.json`, sent with
  `User-Agent: immich-server/3.3.1`. The payload tests run against both versions'
  captures.
- **`immich-compressor setup`**, with a key holding exactly the nine permissions
  [above](#api-key-permissions): every probe passed.
- **Re-measured, and unchanged:** in the [behaviour table](#behaviour-table), the webhook
  body, the Buffer `checksum` and `base64(sha1(file)) == checksum`, millisecond
  `duration`, tag names, the metadata KV, the negative-lookahead regex, soft delete,
  `force: true` (the asset is gone and cannot be restored), and both restore rows.
  `PUT /assets/copy` carried albums, favourite and the stack. `GET /assets/{id}` still names
  the person on a face, which is what `skip_if_named_people` reads — checked with a face
  added by hand, and the job was skipped. Items [1](#1-exifinfotags-arrives-as-null-not-),
  [12](#12-assetmetadataextraction-fires-in-bulk-not-once-per-upload) — "Extract Metadata →
  All" still fired the workflow for every asset that passes the filters —
  [13](#13-a-scoped-api-key-gets-403-on-usersme-and-that-is-not-an-error) and
  [17](#17-sync-endpoints-reject-api-keys-outright-whatever-their-permissions), and
  [16](#16-post-searchmetadata-applies-type-size-and-page) as far as its last paragraph
  says.
- **New or corrected on this page:** the `tags` field in [the payload](#the-payload), the
  step order and `usePath` in [the workflow system](#the-workflow-system), the restore
  `count` in the behaviour table, the deprecation in 16, the workflow log in
  [14](#14-immich-ignores-the-webhooks-response-status), and
  [19](#19-get-assetsidoriginal-without-edited-is-the-original).
- **Not re-measured:** everything else — in particular 2 to 8, 14 and 15, 18 except
  through the live suite's sync tests, the duplicate-upload response, shared links and the
  sidecar on copy, and anything that needs a second user, machine learning or a real
  library's size.
