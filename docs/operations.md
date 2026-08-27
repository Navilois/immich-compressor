# Operating

## Endpoints

These need a **published port and an HTTP client on the host** — the image contains
neither `curl` nor `wget`, so `docker compose exec … curl` is not a route. Everything the
read-only endpoints answer is also a CLI command, which needs neither:

```bash
curl localhost:8080/healthz            # liveness, and whether Immich is reachable
curl localhost:8080/stats              # state counts, skip reasons, bytes saved
curl localhost:8080/metrics            # the same, in Prometheus text format
curl 'localhost:8080/jobs?status=failed'          # or: immich-compressor jobs --status failed
curl localhost:8080/jobs/<assetId>
curl -X POST -H "X-Compressor-Token: $COMPRESSOR_TOKEN" localhost:8080/reprocess/<assetId>
curl -X POST -H "X-Compressor-Token: $COMPRESSOR_TOKEN" localhost:8080/resume
```

No port is published by default. `/healthz`, `/stats`, `/metrics` and `/jobs` are
unauthenticated; only `/webhook`, `/reprocess` and `/resume` require the shared secret —
`/resume` re-arms a service that deletes originals, so it is not an anonymous action.
Publish deliberately, and never on `0.0.0.0`:

```yaml
# docker-compose.override.yaml
services:
  immich-compressor:
    ports:
      - '127.0.0.1:8080:8080'
```

## `/metrics`

Prometheus text exposition format, hand-rolled, no extra dependency. Every family carries
`HELP` and `TYPE`, and is emitted even when empty so a dashboard query never disappears:

```
immich_compressor_build_info{version="1.3.1"} 1
immich_compressor_jobs{state="done"} 2
immich_compressor_jobs{state="failed"} 1
immich_compressor_jobs_skipped{reason="no_gain"} 1
immich_compressor_jobs_total 5
immich_compressor_compressed_assets 2
immich_compressor_original_bytes 50710662
immich_compressor_compressed_bytes 26686614
immich_compressor_saved_bytes 24024048
immich_compressor_webhooks_received_total 12
immich_compressor_webhooks_rejected_total 0
immich_compressor_session_processed_total 2
immich_compressor_session_skipped_total 2
immich_compressor_session_failed_total 1
immich_compressor_session_deleted_total 2
immich_compressor_session_bytes_saved_total 24024048
immich_compressor_encode_duration_seconds_bucket{le="60"} 3
immich_compressor_encode_duration_seconds_bucket{le="+Inf"} 4
immich_compressor_encode_duration_seconds_sum 418
immich_compressor_encode_duration_seconds_count 4
immich_compressor_config_dry_run 0
immich_compressor_config_trash_original 1
immich_compressor_config_permanent_delete 1
```

Gauges come from the job store and survive a restart. The `session_*` counters and the
encode histogram are per process and reset when the container does, which is what
Prometheus expects of a counter.

The three `config_*` gauges are the ones worth alerting on. `config_dry_run 1` on a
deployment you thought was live means nothing has been compressed for however long that has
been true; `config_permanent_delete 1` means originals are being removed with no undo.

`webhooks_rejected_total` deserves an alert of its own. It counts webhooks refused for a
bad or missing shared secret, and anything above zero means the workflow's `headerValue`
and `WEBHOOK__TOKEN` disagree — a state that is otherwise invisible, because Immich logs a
401 as *"executed successfully"*. Both webhook counters live in the database and survive a
restart, unlike the `session_*` ones.

Scrape it from inside the docker network — if your Prometheus runs there too, no port needs
publishing at all.

## CLI

Everything runs inside the container:

```bash
docker compose exec immich-compressor immich-compressor <command>
```

| Command | What it does |
|---|---|
| `serve` | run the webhook service. The image's own `CMD`, so the container already runs it |
| `setup` | guided first-run setup; safe to re-run |
| `hardware [--json]` | which encoder this machine gets, and why every other was rejected |
| `check` | config, connectivity to Immich, and a real one-frame encode through the chosen encoder |
| `encode <file> [--type]` | run the preset against a local file and print ratio, sanity verdict, rotation and display size. Never talks to Immich — this is how you tune a preset |
| `report [--json]` | job statistics, and how many webhooks arrived or were refused |
| `jobs [--status S] [--limit N] [--json]` | list jobs and, for the failed ones, `last_error` |
| `reprocess <assetId>` | re-queue one asset |
| `requeue --reason <r> [--apply]` | re-queue everything skipped for one reason. Dry until `--apply` |
| `requeue --failed [--error-contains TEXT] [--apply]` | re-queue everything that failed, or only the jobs whose error contains `TEXT`. Dry until `--apply` |
| `backfill [scan\|run\|status]` | work through the library that was there before this service. `run` is the default and is dry until `--apply` |
| `resume [--apply]` | show why the surge breaker paused the service, and clear it. Reports until `--apply` |
| `restore <assetId>… \| --all-pending` | pull originals back out of the trash. Restores what Immich still has and counts the ids it no longer knows; exits 3 when some could not come back |
| `--version` | |

## Job states

```
queued → running → uploaded → linked → pending_delete → done
```

plus `skipped` (with a reason) and `failed`. Every transition is persisted in SQLite, so a
crash between upload and linking resumes rather than duplicating work.

Skip reasons: `already_compressed`, `too_small`, `wrong_type`, `unsupported_format`,
`embedded_media`, `source_quality`, `no_gain`, `duplicate`, `named_people`, `edited`,
`external_library`, `live_photo`, `locked`, `trashed`, `re_uploaded`, `no_preset`,
`dry_run`. The three stills-only ones — a format that is not JPEG, a motion photo, and a
source already at or below the preset's quality target — are explained in
[troubleshooting.md](troubleshooting.md#everything-is-skipped).

`re_uploaded` is the one worth watching: it means a device put an original this service had
already replaced back onto the server. A steady trickle is one phone that has not caught
up; a burst is a reinstall or a second client. Neither is something this service can fix
from its side — see [the FAQ](faq.md#will-my-phone-just-re-upload-the-original).

Jobs are claimed by a worker lane, one per entry in `enabled_types`, so a long video job
never blocks a queue of image jobs.

Failures retry with exponential backoff up to `max_attempts` (3), then land in `failed` and
show up in `report` and `/stats`.

## Working through the existing library

The webhook only fires for assets moving through Immich's pipeline. The backlog already in
the library is invisible to it, and re-running metadata extraction is not a way in — see
[the metadata-extraction trap](#the-metadata-extraction-trap) below. `backfill` is.

```bash
immich-compressor backfill scan                     # what is in there?
immich-compressor backfill status                   # how much of it is left?
immich-compressor backfill run --limit 50           # what would the next 50 jobs be?
immich-compressor backfill run --limit 50 --apply   # queue them
```

**`scan`** walks the library once per enabled asset type and writes one row per asset into
an inventory table in the job store, each with the verdict the *worker's own guards* reach
from the payload: too small, unsupported format, external library, already compressed. It
queues nothing and encodes nothing. The cursor lives in the database, so an interrupted walk
resumes where it stopped instead of starting over, and running it again refreshes the
inventory without forgetting what has already been queued.

**`run`** takes candidates out of that inventory — biggest first, which is where the savings
are — re-checks each one against the live server, and enqueues it as if a webhook had
arrived for it. With no inventory yet, it scans first.

**`status`** prints what the scan found per type: how much was scanned, how many candidates
are waiting and how big they are, how many have been queued, and why the rest were rejected.

| Flag | Command | What |
|---|---|---|
| `--type VIDEO\|IMAGE` | all | one lane only. Default: every type in `enabled_types` |
| `--limit N` | `run` | how many **jobs to queue** (default 50) |
| `--order size\|scanned` | `run` | biggest first (default), or the order the library came back in |
| `--apply` | `run` | actually queue. Without it the run is a dry one and writes nothing |
| `--no-verify` | `run` | skip the live re-check of each asset before it is queued |
| `--rescan` | `scan` | drop the inventory for those types and walk the library again |
| `--page-size N` | `scan` | assets per request (default 1000) |
| `--json` | `status` | machine-readable |

Four things worth knowing:

- **`--limit` counts jobs, not search results.** An asset that was deleted, trashed or given
  a named face between the scan and the run is recorded as such — `missing`, `trashed` or
  `named_people` — and the run moves on to the next candidate. Running it again continues
  rather than re-reading the same answer, so working through a library is `run --apply`,
  wait, `run --apply`. Two verdicts exist only in the inventory and never reach a job row:
  `missing`, for an asset the server no longer has, and `already_known`, for one the job
  store already holds — which is what a half-worked-through library looks like, not an
  error.
- **The guards run twice, deliberately.** Once during the scan, from the payload, and again
  in the worker. A backfilled job is an ordinary job: same guards, same sanity gate, same
  [verification chain](safety.md#the-verification-chain) before anything is deleted. What
  the scan buys is that fifty queued jobs are fifty jobs worth having.
- **`dry_run: true` swallows the whole run.** Every job queued while the shipped default is
  in place ends as `skipped: dry_run` — that is the point of
  [stage 1](safety.md#stage-1--dry-run-the-default), but those jobs do not come back on their own after
  going live: `immich-compressor requeue --reason dry_run --apply`. `run` says so before it
  queues anything.
- **A paused service still accepts a backfill.** The surge breaker never trips on one
  (it counts webhooks only), but no worker will claim what a run queued until
  `resume --apply`. `run` says that too.

The inventory is one table in the same SQLite database as the jobs, deliberately separate
from them: a job row is a decision this service has taken and is immune to replay forever,
while an inventory row has to stay re-scannable. Dropping it costs nothing but a re-scan.

### The metadata-extraction trap

`AssetCreate` fires once per asset. `AssetMetadataExtraction` is a maintenance operation
that can be started at any time from **Administration → Jobs → Extract Metadata**, and every
run emits the trigger again — for *every asset in the library*, unbounded, with no way to
stop it from the Immich side other than disabling the workflow. This was traced through the
v3.1.0 server bundle; see
[immich-api-notes.md](immich-api-notes.md#12-assetmetadataextraction-fires-in-bulk-not-once-per-upload).

Replays of an asset the service has already seen were always harmless: `ON CONFLICT DO
NOTHING` makes anything already recorded permanently immune, in *any* state including
`skipped`. Assets it has never seen had no such protection — and that is the whole library
until you have worked through it.

`behavior.max_asset_age_hours` is what closes that gap. Every webhook carries `createdAt`,
the moment Immich created the asset's database row, which dates the **upload** rather than
the exposure. A genuine upload reaches this service seconds old; a re-trigger carries
whatever age the asset already had. Anything past the window is refused at ingest:

```
WARNING refused AssetMetadataExtraction asset=… type=IMAGE (too_old): added to Immich
        712.4 h ago, past max_asset_age_hours 24 — this is a re-trigger, not a new upload;
        use `immich-compressor backfill` if it was meant
```

Three properties are worth knowing:

- **It does not fire on a bulk upload.** Importing a thousand photos from 2009 is a thousand
  assets whose `createdAt` is today. A rate limit would have refused them; this does not.
- **A refusal writes no job.** That is deliberate: `backfill` enqueues through the same `ON
  CONFLICT DO NOTHING`, so a row recorded here — in any state — would put the asset
  permanently out of `backfill`'s reach. Refused assets stay exactly as reachable as they
  were.
- **It cannot be switched off where it matters.** `max_asset_age_hours: null` together with
  `delete_mode: permanent` is refused at startup.

So the button is no longer a hazard, and the answer to *"can I re-run metadata extraction?"*
is yes. It is still not a way to reach the backlog — every one of those assets is refused,
by design. `backfill` is the way to reach the backlog.

### The surge breaker (off by default)

The freshness gate answers a known question. The breaker is the backstop for the one nobody
asked: a trigger this project has not seen, a re-uploaded library, a workflow pointed at the
wrong endpoint. More than `surge_threshold` **new** assets queued from webhooks inside
`surge_window_seconds` latches the whole service paused.

**It ships off — `surge_threshold: null` — and turning it on is a deliberate choice.** The
breaker counts assets and knows nothing else about them, so a first phone backup, a camera
card import and a holiday upload all look exactly like the influx it exists to stop. With
`IMAGE` in `enabled_types` that is an ordinary day rather than an unusual one, and a
backstop that fires on ordinary use teaches its operator to clear it unread. The gate above
is the guard that can actually tell a re-trigger from an upload, and it is still on.

Turn the breaker on by writing a number:

```yaml
behavior:
  surge_threshold: 2000
  surge_window_seconds: 600
```

2000 is a suggested starting point and not a measured one — above one device's backlog,
below a library migration, which is the shape of event worth pausing for. Pick your own from
what your library actually does: `immich_compressor_webhooks_received_total` in
[`/metrics`](#metrics) is the arrival rate to size it against, bearing in mind that the
breaker counts only the subset that queues a new job.

```
ERROR SURGE BREAKER TRIPPED: 2001 assets queued from webhooks within 600s, over
      surge_threshold 2000. Nothing further is queued, processed or deleted until
      `immich-compressor resume --apply`.
```

While it stands: workers claim nothing, the trash sweeper finalises nothing, and further
webhooks are refused as `paused`. Jobs already in the queue keep their state and wait. The
latch lives in the database, not in memory — restarting the container is the first thing an
operator reaches for, and it must not be the thing that clears a pause.

```bash
docker compose exec immich-compressor immich-compressor resume
```

That prints why it paused and changes nothing. Add `--apply` to clear it, or `POST /resume`
with the webhook token. Workers pick up where they left off on the next poll.

**Whatever number you pick has a false-positive rate.** A big enough legitimate upload is a
surge by this definition, and no threshold separates the two — that is why the breaker only
*pauses*. Nothing is lost, one command resumes, and erring towards a stop is the right way
round for a service that deletes originals. Raise `surge_threshold` if your normal traffic
trips it, or set it back to `null` to switch it off.

Assets whose webhook was refused while paused are not recorded anywhere, so they stay
reachable by `backfill` once you have resumed.

## Re-queueing after a change

A changed guard or sanity gate leaves its old verdicts behind: those assets sit in `skipped`
locally and no webhook will fire for them again.

```bash
immich-compressor requeue --reason no_gain           # look first
immich-compressor requeue --reason no_gain --apply
```

A changed encoder or metadata gate leaves the same problem in the other terminal state. A
failed job has used up its attempts, so the worker's backoff never returns to it:

```bash
immich-compressor requeue --failed --error-contains ShutterSpeedValue
immich-compressor requeue --failed --error-contains ShutterSpeedValue --apply
```

`--error-contains` matches a plain substring of the `last_error` that `jobs --status failed`
prints — not a pattern, so `%` and `_` in an ffmpeg message mean themselves. Without it,
every failed job comes back, which is rarely what you want: the ones that failed on a broken
source file will simply fail again.

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
