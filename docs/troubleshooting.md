# Troubleshooting

Start here:

```bash
docker compose logs -f immich-compressor
docker compose exec immich-compressor immich-compressor check
docker compose exec immich-compressor immich-compressor hardware
```

`check` validates the configuration, reaches the Immich API and confirms the encoder with a
real one-frame encode. `hardware` explains every encoder decision.

## Nothing happens at all

| Symptom | Where to look |
|---|---|
| Immich says the workflow ran, nothing happens here | **`report`, first line.** `webhooks: 0 received, 7 rejected` means the workflow's `headerValue` and `WEBHOOK__TOKEN` disagree; `0 received, 0 rejected` means nothing reached the service at all. Immich discards the webhook response, so a 401 or a 422 is invisible on its side — both are also logged here, at WARNING and ERROR, and the 401 line names the length and first characters of the token that arrived next to the one expected. |
| Nothing at all in the log | Immich cannot reach the service. Test from inside the Immich container: `docker exec immich_server curl -s -o /dev/null -w '%{http_code}' http://immich-compressor:8080/healthz` |
| The workflow used to fire and stopped | Restart `immich-server`. Execution was observed going quiet after a workflow run threw `NoResultError`. |
| Webhook arrives, the job never runs | `initial_delay_seconds` is 300 by default. `GET /jobs/{id}` shows `run_after`. |

## Everything is skipped

| Skip reason | What it means |
|---|---|
| `dry_run` | The shipped default. Set `BEHAVIOR__DRY_RUN=false` when you are ready — see [safety.md](safety.md). |
| `too_small` | The asset is smaller than `min_savings_bytes` (1 MiB), so it cannot possibly save that much. Rejected before the download. |
| `no_gain` | The encode did not reach `max_ratio`, or saved fewer than `min_savings_bytes`. Common for footage that is already HEVC. Tune it offline with `immich-compressor encode`, or accept it. |
| `already_compressed` | The `compressor` marker is on the asset. That is the loop guard doing its job. |
| `named_people` | The asset has manually named faces. Deliberate — see [safety.md](safety.md#what-it-never-touches). |
| `wrong_type` | Not in `behavior.enabled_types`, or the workflow's type filter does not match. |
| `unsupported_format` | The type is covered but no preset accepts this extension. The `IMAGE` preset is a JPEG allowlist: RAW, HEIC, PNG, GIF, TIFF and WebP are out by design — see [safety.md](safety.md#why-only-jpeg-stills). |
| `source_quality` | The still is already below the preset's `min_source_quality`. Re-encoding it would add a second generation of artefacts and usually *enlarge* the file — measured 158 368 -> 190 488 bytes for a q60 source through the q82 preset. |
| `embedded_media` | A motion photo: a JPEG with a video glued on behind the end-of-image marker. Re-encoding would silently drop the video — see [safety.md](safety.md#why-motion-photos-are-skipped). |
| `re_uploaded` | These exact bytes were an original this service already replaced, and a device that still holds the file has uploaded them again under a new asset id. Nothing is downloaded, encoded or deleted — see [the FAQ](faq.md#will-my-phone-just-re-upload-the-original). |
| `external_library`, `live_photo`, `edited`, `locked`, `trashed` | Never touched, by design. |

## Stills fail with `metadata carry-over incomplete`

The [metadata gate](safety.md#the-metadata-gate) found a tag the `exiftool` copy did not
carry. **The original was not touched** — the job is in `failed` and the asset is still
whole. Get the exact list of tags without involving the server:

```bash
docker compose exec immich-compressor immich-compressor encode /path/photo.jpg --type IMAGE
```

The `metadata_differences` array names every tag that moved. If it is a MakerNotes quirk
specific to your camera, `behavior.metadata_verify: warn` downgrades the gate to a log line
— but only while `delete_mode` is `trash`, because a warning cannot undo a force-deleted
original. The startup validation enforces that pairing.

## Videos fail with `Could not find tag for codec`

```
preset 'video-h265' exited 234: [mp4 @ 0x...] Could not find tag for codec pcm_u8 in
stream #1, codec not currently supported in container
```

The video presets copy the audio stream instead of re-encoding it, and MP4 has no mapping
for some of what an old camera or a DVD rip produces. ffmpeg's muxer refuses the file while
writing the header, before a frame is encoded, so the job fails and the original is
untouched. Measured on a live library on 2026-08-26, this was **119 of 172** failures in one
backfill run: `pcm_u8` (108), `amr_nb` (9) and `pcm_dvd` (2).

```yaml
behavior:
  transcode_unsupported_audio: true
```

The first attempt is still a stream copy; only a run the container refused is retried, with
the audio re-encoded to 128 kbit/s AAC. Afterwards, bring the jobs that already failed back:

```bash
immich-compressor requeue --failed --error-contains "Could not find tag for codec"
immich-compressor requeue --failed --error-contains "Could not find tag for codec" --apply
```

**It ships off because it is lossy.** `pcm_u8` and `pcm_dvd` are uncompressed in the source
and are not after this, and nothing downstream can see the difference — the sanity gate
counts audio streams, it does not listen to them. On a job that goes on to delete the
original, that is a decision to take deliberately. The container log names every file it
happens to.

## Tuning a preset

`encode` runs the preset and the sanity gate against a local file, and never talks to
Immich:

```bash
docker compose exec immich-compressor immich-compressor encode --type VIDEO /path/clip.mp4
docker compose exec immich-compressor immich-compressor encode --type IMAGE /path/photo.jpg
```

It prints the ratio, the sanity verdict, and the display size and rotation of both input and
output — which is what you need when the gate rejects something and you want to know why.
For stills it additionally prints `source_quality`, `embedded_media` and
`metadata_differences`, so every still-specific decision the pipeline would make is visible
before it makes it.

## GPU problems

See [hardware.md](hardware.md#troubleshooting). `immich-compressor hardware` answers almost
all of them directly, including "permission denied" (a missing render group, not a broken
driver) and the Gen9–11 QSV failure (expected; VAAPI is used instead).

## Webhooks are refused as `too_old`

```
WARNING refused AssetMetadataExtraction asset=… (too_old): added to Immich 712.4 h ago,
        past max_asset_age_hours 24 — this is a re-trigger, not a new upload
```

Something re-ran Immich's metadata extraction. That trigger fires once per *extraction*, not
once per upload, so it re-fires for every asset in the library; `behavior.max_asset_age_hours`
is the gate that refuses those. This is the guard working. Nothing was queued and nothing was
written, so the assets stay reachable by `backfill`, which is the intentional way through a
library. Full explanation in [operations.md](operations.md#the-metadata-extraction-trap).

If a *genuine* upload is being refused, its metadata extraction sat in Immich's queue for
longer than the window — raise `max_asset_age_hours`.

## The service has stopped doing anything

```
WARNING starting PAUSED since 2026-08-21T09:14:22+00:00: 2001 assets queued from webhooks
        within 600s, over surge_threshold 2000
```

The surge breaker latched. Nothing is queued, processed or deleted until it is cleared, and
that is deliberate — it fires when far more work arrived than anybody asked for. `report`
says so on its first line, `/healthz` reports `"status": "paused"`, and:

```bash
docker compose exec immich-compressor immich-compressor resume
```

prints the reason without changing anything. `--apply` clears it. If your normal traffic
trips it, raise `behavior.surge_threshold` or set it back to `null`.
Full explanation in [operations.md](operations.md#the-surge-breaker-off-by-default).

The breaker is off unless you turned it on, so on a default configuration this is not why
the service is idle — check `dry_run`, then the queue with `report`. A latch set by an
earlier version does survive an upgrade: the threshold no longer being configured does not
clear a pause that is already in the database, and `resume --apply` is still what clears it.

## The whole library queued at once

Only possible with `max_asset_age_hours: null`, which turns the gate above off. Set it back
to a number, then use `report` to see the extent.

## `backfill` queues nothing

```bash
docker compose exec immich-compressor immich-compressor backfill status
```

The inventory answers this, per type. In order of how often it is the cause:

- **`0 candidate(s)`, everything under `rejected:`** — the guards refused the library, and
  the counts name the reason. `too_small` means the assets are below
  `behavior.min_savings_bytes`; `unsupported_format` on stills means they are not JPEG
  (RAW, HEIC, PNG, GIF, TIFF and WebP are all skipped by design); `wrong_type` means the
  type is not in `behavior.enabled_types`.
- **`not scanned yet`** — run `backfill scan`, or just `backfill run`, which scans first.
- **`walk interrupted, resumes at page N`** — the last scan stopped early. Run it again; it
  continues from there. If it says the server *does not apply `page`*, that Immich cannot be
  walked page by page and only the first page is reachable.
- **Everything already `queued`** — the assets are in the job store, not in the inventory
  any more. `report` and `jobs` are where they show up now, and while `dry_run` is on they
  all end as `skipped: dry_run`. `requeue --reason dry_run --apply` brings them back after
  going live.

`backfill run` also refuses to be quiet about two states that make a successful run look
useless an hour later: `behavior.dry_run` being on, and the surge breaker being latched.

## Disk fills up

Two different causes:

- **`work_dir`** holds the source and the encode at the same time. The service refuses to
  start a job unless 3× the source size is free, but a large concurrency or a very large
  asset can still be tight. Give the `compressor-work` volume real space.
- **`delete_mode: trash`** means space is only reclaimed when the Immich trash is emptied.
  Until then you are using *more* space, not less. That is the price of the undo.

## Jobs stuck in `pending_delete`

The [verification chain](safety.md#the-verification-chain) refused to delete an original and
backed off for an hour. `GET /jobs/{assetId}` shows `last_error` with the failing condition
— usually a checksum mismatch or a replacement that never got its capture date. Nothing is
deleted while this persists, which is the intended outcome.

## Jobs in `failed`

```bash
docker compose exec immich-compressor immich-compressor jobs --status failed
```

`last_error` carries the reason. The same rows come out of `GET /jobs?status=failed`, if
you have published a port and have an HTTP client on the host — the image has none. After
fixing the cause, one asset at a time, or every job that failed the same way at once:

```bash
docker compose exec immich-compressor immich-compressor reprocess <assetId>
docker compose exec immich-compressor immich-compressor requeue --failed \
    --error-contains "Could not find tag for codec" --apply
```

Note that this does **not** remove the `compressor` marker on the server, so an
already-replaced asset is simply skipped again — by design.

## The shim does nothing

Symptoms in the order they are worth checking. The whole page is
[shim.md](shim.md); this is the part that goes wrong during setup.

| Symptom | What it means |
|---|---|
| `shim_requests_total` is 0 while `shim.enabled: true` | Your reverse proxy is not routing to this service. Nothing downstream of this matters until it moves — everything else here assumes traffic is arriving. |
| The proxy will not start: `host not found in upstream "immich-compressor"` | nginx resolves a literal upstream name when it **parses** the configuration, so a proxy that is not on this service's docker network fails at start, not at request time. Join it to the network named by `IMMICH_NETWORK`. |
| You have no reverse proxy at all | A stock Immich does not come with one — it publishes `immich-server` on `2283` and serves the API itself. [shim.md](shim.md#what-has-to-be-true-first) covers what adding one changes. |
| `shim_requests_total` climbs, but a phone still re-uploads | That client is probably not going through the proxy. A LAN address, a VPN address or a second hostname pointed straight at `:2283` bypasses the shim entirely — [coverage follows routing](shim.md#limits). |
| Everything is routed, `shim_gates_opened_total` is 0 | On `delete_mode: trash` this is normal for up to 30 days: a gate opens when Immich *purges* an original, not when this service trashes it. |
| Gates open, `shim_touches_total` stays 0 | Expected at rollout step 3 on `delete_mode: permanent`, and only there. Anywhere else it means the translation is armed but no client will ever be offered the rewritten line. |
| `shim_hashes_translated_total` climbs at rollout step 3 | `rewrite_upload_check` is still at its default of `true`. Step 3 has to name both rewrite flags. |
| `shim_passthrough_errors_total` is above 0 | Immich was unreachable from this service and clients got a `502`. With the documented `error_page` line the proxy retries those at Immich, so it is not necessarily a failure anyone saw — but the count is real. |
| A re-upload the shim should have caught, from `immich-go` or the CLI | Its ledger lookup needs an owner, so the shim calls `GET /users/me` with the caller's own credential first. A key without `user.read` answers 403, the owner is unresolved, and the translation silently does nothing. |

Whether the two paths reach this service at all is one request each, from anywhere the proxy
serves:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://photos.example.com/api/sync/stream
```

Then read `shim_requests_total` again. If it did not move, the request did not come here,
whatever the status code said.

## Getting help

Open an issue with the output of:

```bash
docker compose exec immich-compressor immich-compressor hardware --json
docker compose exec immich-compressor immich-compressor report --json
docker compose logs --tail 200 immich-compressor
```

The [hardware report template](https://github.com/Navilois/immich-compressor/issues/new?template=hardware_report.yml)
asks for exactly the first of those. Redact your API key if it appears anywhere; it should
not.
