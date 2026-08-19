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
| `too_small` | `min_size_bytes` is 20 MiB. |
| `no_gain` | The encode did not reach `max_ratio` (0.6). Common for footage that is already HEVC. Tune it offline with `immich-compressor encode`, or accept it. |
| `already_compressed` | The `compressor` marker is on the asset. That is the loop guard doing its job. |
| `named_people` | The asset has manually named faces. Deliberate — see [safety.md](safety.md#what-it-never-touches). |
| `wrong_type` | Not in `behavior.enabled_types`, or the workflow's type filter does not match. |
| `external_library`, `live_photo`, `edited`, `locked`, `trashed` | Never touched, by design. |

## Tuning a preset

`encode` runs the preset and the sanity gate against a local file, and never talks to
Immich:

```bash
docker compose exec immich-compressor immich-compressor encode --type VIDEO /path/clip.mp4
```

It prints the ratio, the sanity verdict, and the display size and rotation of both input and
output — which is what you need when the gate rejects something and you want to know why.

## GPU problems

See [hardware.md](hardware.md#troubleshooting). `immich-compressor hardware` answers almost
all of them directly, including "permission denied" (a missing render group, not a broken
driver) and the Gen9–11 QSV failure (expected; VAAPI is used instead).

## The whole library queued at once

Something re-ran Immich's metadata extraction. That trigger fires once per *extraction*, not
once per upload. Disable the workflow, then use `report` to see the extent. Full explanation
in [operations.md](operations.md#the-metadata-extraction-trap).

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
