# Operating

## Endpoints

```bash
curl localhost:8080/healthz            # liveness, and whether Immich is reachable
curl localhost:8080/stats              # state counts, skip reasons, bytes saved
curl localhost:8080/metrics            # the same, in Prometheus text format
curl 'localhost:8080/jobs?status=failed'
curl localhost:8080/jobs/<assetId>
curl -X POST -H "X-Compressor-Token: $COMPRESSOR_TOKEN" localhost:8080/reprocess/<assetId>
```

No port is published by default. `/stats`, `/metrics` and `/jobs` are unauthenticated; only
`/webhook` and `/reprocess` require the shared secret. Publish deliberately, and never on
`0.0.0.0`:

```yaml
# docker-compose.override.yaml
services:
  immich-compressor:
    ports:
      - '127.0.0.1:8080:8080'
```

## `/metrics`

Prometheus text format, hand-rolled, no extra dependency:

```
immich_compressor_jobs{state="done"} 42
immich_compressor_jobs_skipped{reason="no_gain"} 7
immich_compressor_bytes_saved_total 1.28e+10
immich_compressor_compressed_assets_total 42
immich_compressor_dry_run 0
```

Scrape it from inside the docker network — no port needs publishing for Prometheus if it
runs there too.

## CLI

Everything runs inside the container:

```bash
docker compose exec immich-compressor immich-compressor <command>
```

| Command | What it does |
|---|---|
| `setup` | guided first-run setup; safe to re-run |
| `hardware [--json]` | which encoder this machine gets, and why every other was rejected |
| `check` | config, connectivity to Immich, and a real one-frame encode through the chosen encoder |
| `encode <file> [--type]` | run the preset against a local file and print ratio, sanity verdict, rotation and display size. Never talks to Immich — this is how you tune a preset |
| `report [--json]` | job statistics |
| `reprocess <assetId>` | re-queue one asset |
| `requeue --reason <r> [--apply]` | re-queue everything skipped for one reason. Dry until `--apply` |
| `backfill --type VIDEO --limit N [--apply]` | queue existing large assets. Dry until `--apply` |
| `restore <assetId>… \| --all-pending` | pull originals back out of the trash |
| `--version` | |

## Job states

```
queued → running → uploaded → linked → pending_delete → done
```

plus `skipped` (with a reason) and `failed`. Every transition is persisted in SQLite, so a
crash between upload and linking resumes rather than duplicating work.

Skip reasons: `already_compressed`, `too_small`, `wrong_type`, `no_gain`, `duplicate`,
`named_people`, `edited`, `external_library`, `live_photo`, `locked`, `trashed`,
`no_preset`, `dry_run`.

Failures retry with exponential backoff up to `max_attempts` (3), then land in `failed` and
show up in `report` and `/stats`.

## Working through the existing library

The webhook only fires for assets moving through Immich's pipeline. The backlog already in
the library is invisible to it. `backfill` queues a batch whose size you choose:

```bash
immich-compressor backfill --type VIDEO --limit 50            # look first
immich-compressor backfill --type VIDEO --limit 50 --apply
```

### The metadata-extraction trap

**Do not re-run Immich's metadata extraction to reach the backlog.**

`AssetCreate` fires once per asset. `AssetMetadataExtraction` is a maintenance operation
that can be started at any time from **Administration → Jobs → Extract Metadata**, and every
run emits the trigger again — for *every asset in the library*, unbounded, with no way to
stop it other than disabling the workflow. This was traced through the v3.1.0 server bundle;
see [immich-api-notes.md](immich-api-notes.md#12-assetmetadataextraction-fires-in-bulk-not-once-per-upload).

Replays of an asset the service has already seen are harmless: `ON CONFLICT DO NOTHING`
makes anything already recorded permanently immune, in *any* state including `skipped`.
Assets it has never seen have no such protection — and that is the whole library until you
have worked through it.

- With `dry_run: false`, one click starts compressing every asset over `min_size_bytes`,
  one at a time, until the disk runs out.
- With `dry_run: true`, the same click records them all as `skipped: dry_run`. They are
  immune from then on, so clearing the dry run later will never pick them up. Recoverable
  with `requeue --reason dry_run --apply` — but only if you notice.

**Disable the workflow before running metadata extraction**, unless the disk is already
sized for a full pass over the library.

## Re-queueing after a change

A changed guard or sanity gate leaves its old verdicts behind: those assets sit in `skipped`
locally and no webhook will fire for them again.

```bash
immich-compressor requeue --reason no_gain           # look first
immich-compressor requeue --reason no_gain --apply
```

## Backups

The service's own state is a SQLite file in the `compressor-state` volume. Back it up if you
care about the report history; losing it means assets already processed are re-evaluated,
and the `compressor` marker on the server stops them anyway.

What actually needs backing up is **Immich**: Postgres plus the upload directory. That is
the only rollback for `delete_mode: permanent`.

## Logs

```bash
docker compose logs -f immich-compressor
```

The first lines after a restart tell you the encoder that was chosen, every candidate that
was rejected, and a loud warning if `delete_mode: permanent` is on. Rejected webhooks are
logged at WARNING (bad secret) and ERROR (unparseable payload) — which matters, because
Immich reports both as success on its side.
