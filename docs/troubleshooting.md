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
| Immich says the workflow ran, nothing happens here | **The service's log.** Immich discards the webhook response, so a 401 (wrong `headerValue`) or a 422 (payload it could not parse) is invisible on the Immich side. Both are logged here, at WARNING and ERROR. |
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
| `source_quality` | The still is already at or below the preset's `min_source_quality`. Re-encoding it would add a second generation of artefacts and usually *enlarge* the file — measured 158 368 -> 190 488 bytes for a q60 source through the q82 preset. |
| `embedded_media` | A motion photo: a JPEG with a video glued on behind the end-of-image marker. Re-encoding would silently drop the video — see [safety.md](safety.md#why-motion-photos-are-skipped). |
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
WARNING starting PAUSED since 2026-08-21T09:14:22+00:00: 201 assets queued from webhooks
        within 600s, over surge_threshold 200
```

The surge breaker latched. Nothing is queued, processed or deleted until it is cleared, and
that is deliberate — it fires when far more work arrived than anybody asked for. `report`
says so on its first line, `/healthz` reports `"status": "paused"`, and:

```bash
docker compose exec immich-compressor immich-compressor resume
```

prints the reason without changing anything. `--apply` clears it. If your normal traffic
trips it — a phone backup of a few hundred photos will — raise `behavior.surge_threshold`.
Full explanation in [operations.md](operations.md#the-surge-breaker).

## The whole library queued at once

Only possible with `max_asset_age_hours: null`, which turns the gate above off. Set it back
to a number, then use `report` to see the extent.

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
docker compose exec immich-compressor immich-compressor report
curl 'localhost:8080/jobs?status=failed'
```

`last_error` carries the reason. After fixing the cause:

```bash
docker compose exec immich-compressor immich-compressor reprocess <assetId>
```

Note that this does **not** remove the `compressor` marker on the server, so an
already-replaced asset is simply skipped again — by design.

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
