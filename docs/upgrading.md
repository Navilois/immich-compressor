# Upgrading

```bash
docker compose pull
docker compose up -d
```

The compose file pins the **major** tag (`ghcr.io/navilois/immich-compressor:1`), so patch
and minor releases arrive that way and a breaking change never does. The
[CHANGELOG](../CHANGELOG.md) is the authority on what changed.

Job state lives in a volume and survives. Schema changes are applied automatically on open.

## Unreleased

### If a device's sync stopped advancing, this is the upgrade that fixes it

Nothing to configure and no schema change — but read this if you run the shim, because the
symptom is specific and easy to misread as an Immich fault.

A device whose sync batch hit
`SqliteException(2067): UNIQUE constraint failed: remote_asset_entity.owner_id,
remote_asset_entity.checksum` on `updateAssetsV2` stopped making progress: the batch dies
before its ack, so Immich re-sends the same batch and the client never advances its
checkpoint. 1.4.0 already held a translation back once this service had recognised a
returned original — but that recognition is a `re_uploaded` job row, and the job is `queued`
from the moment Immich accepts the upload until a worker reaches it. Behind a backlog that
is minutes to hours, and every sync in between was decided against a job store that did not
yet know the checksum was taken.

The shim now takes the claim off the sync stream instead of waiting for the job. Nothing is
written to your library and nothing is written to the job store; a claim learned this way
is held in memory, and the job row remains the durable half.

**This makes duplicate cleanups safer, and does not make them safe.** The re-upload window a
cleanup opens is unchanged — see the 1.3.1 → 1.4.0 notes below — and the advice there still
stands: run one with device backup switched off, and turn it back on after a sync has
carried the translations. What changes is that a copy which does come back no longer wedges
a device while its job waits in the queue.

**If a device is wedged right now,** upgrade and let it sync. Do not set
`shim.rewrite_sync_stream: false` to clear it: that strips the translation from every open
gate at once and exposes all of them, which is a far larger change than the stall it would
end.

## 1.3.1 → 1.4.0

### The surge breaker is now off by default — check whether you were relying on it

**Read this one if you never wrote `behavior.surge_threshold` into your `config.yaml`.** It
used to default to `200`, so you had a breaker whether you asked for one or not. It now
defaults to `null`, and after this upgrade you have none.

Keeping one is one line:

```yaml
behavior:
  surge_threshold: 2000     # or 200, to keep exactly what you had
  surge_window_seconds: 600
```

The mechanism is untouched — over the threshold in newly queued webhook assets inside the
window still latches the whole service paused, and `immich-compressor resume --apply` is
still what clears it. What changed is only which side of the switch ships. The breaker
counts assets and knows nothing else about them, so a first phone backup or a camera card
import looks exactly like the influx it exists to stop; with `IMAGE` in `enabled_types` that
is an ordinary day. 2000 is a suggested starting point and not a measured one.

**Nothing about your protection against the bulk metadata-extraction trigger changed.**
`behavior.max_asset_age_hours` is the guard for that, it still defaults to 24 hours, and it
is still refused at startup together with `delete_mode: permanent`.

**If your service is paused right now, it stays paused.** The latch lives in the database
and is independent of the threshold; clearing it is still `immich-compressor resume
--apply`.

### The checksum-translation shim (opt-in, off by default)

**Nothing to edit unless you want it.** `shim.enabled` defaults to `false`, and while it is
off the two proxied routes are not mounted at all — the service behaves exactly as before.

Turning it on stops a phone re-uploading an original after its compressed replacement took
over, instead of only recognising the re-upload after the fact. It needs a reverse proxy in
front of Immich that routes `POST /api/sync/stream` and `POST /api/assets/bulk-upload-check`
to this service, and `proxy_buffering off` on the first of them. Start with
`shim.log_only: true`, which counts what would change without changing anything, and read
[shim.md](shim.md) before routing anything — it is a deliberate untruth told to one client,
and the page sets out the trade.

The `jobs` table gains one column, `original_freed_at`, applied automatically the first time
the new version opens the database. Nothing is rewritten and no job changes state. It is
recorded whether or not the shim is enabled, so a deployment that turns it on later has the
history from this version onwards.

One behaviour change even with the shim off: after a **`delete_mode: permanent`** delete the
job now records that the original is gone. No request is made and nothing user-visible
changes; the no-op update that re-offers the replacement to clients is only made when the
shim is actually enabled.

**That record is counted, so `shim_gates_opened_total` climbs on a `permanent` deployment
even with the shim off.** If you are about to alert on "the `shim_*` counters are all zero
while `shim.enabled: false`", that one is not. It follows the record rather than the shim,
because a deployment that turns the shim on later wants the history. The other five stay at
zero until the routes are mounted. `shim_touches_total` is the pair to read against it:
gates ahead of touches means the translation is armed but is not being delivered — expected
while `shim.rewrite_sync_stream` is still `false`, worth investigating once it is `true`.

### If a device's sync is failing on a UNIQUE constraint

**Only concerns a deployment that ran `main` with `shim.rewrite_sync_stream: true` and has
had originals uploaded back to it.** With the shim off, or with nothing re-uploaded, there
is nothing here to do.

The symptom is a device that stops syncing entirely, with:

```
Error: updateAssetsV2 - user
SqliteException(2067): UNIQUE constraint failed:
  remote_asset_entity.owner_id, remote_asset_entity.checksum
```

repeating every few seconds on the same batch. The batch fails before it acknowledges its
checkpoint, so the server sends it again, and the device mirrors nothing new and backs up
nothing new for as long as it lasts.

The cause is the one described under [When the checksum comes
back](shim.md#when-the-checksum-comes-back): a device put an original back after its gate
had opened, and the shim went on presenting that same checksum for the replacement.
**Upgrading is the whole fix.** The shim stops presenting a checksum another live asset
holds, the next batch applies, and no asset has to be deleted for that to happen.

To see whether your library has any — read-only, safe with the service running. It asks
`immich.base_url`, which is Immich itself and not the proxied path, so the answer is
Immich's and not the shim's:

```bash
docker compose exec immich-compressor python3 -c "
import json, sqlite3, urllib.request
from immich_compressor.config import load_settings
settings = load_settings().immich
key, base = settings.api_key.get_secret_value(), settings.base_url
db = sqlite3.connect('file:/var/lib/immich-compressor/state.db?mode=ro', uri=True)
rows = db.execute('SELECT source_checksum, new_asset_id FROM jobs '
                  'WHERE source_checksum IS NOT NULL AND new_asset_id IS NOT NULL '
                  'AND owner_id IS NOT NULL AND original_freed_at IS NOT NULL').fetchall()
by_checksum = {}
for checksum, new_id in rows: by_checksum.setdefault(checksum, []).append(new_id)
checksums, live = list(by_checksum), []
for i in range(0, len(checksums), 500):
    batch = checksums[i:i + 500]
    body = json.dumps({'assets': [{'id': c, 'checksum': c} for c in batch]}).encode()
    req = urllib.request.Request(base + '/assets/bulk-upload-check', data=body, method='POST',
                                 headers={'x-api-key': key, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=120) as response: out = json.load(response)
    for result in out['results']:
        if result.get('assetId'): live.append((result['id'], result['assetId']))
print('open gates:', len(rows), '| collisions:', len(live))
for checksum, asset_id in live:
    print(' ', checksum, 'is live again as', asset_id, '-> was presented for', by_checksum[checksum])
"
```

A healthy library prints `collisions: 0`. Anything above zero is a device whose sync is
about to break or already has, and after the upgrade each of those translations is simply
held back instead.

**Removing the duplicates is optional, and it is what reclaims the space.** They are exact
copies of originals this service already replaced, so nothing unique is in them — but that
is a judgement about your library and not one this page can make for you, and on
`delete_mode: permanent` there is no way back. If you do remove them, delete them
*permanently*: the shim re-arms each translation when it sees that asset's delete go past on
a sync stream, and only a permanent delete produces one. A duplicate moved to the trash
keeps its row and its checksum in the mirror, so the translation correctly stays held back —
that is behaviour of the app's own schema, reproduced in `tests/test_app_mirror.py`, not
something measured against a device.

Three things are worth knowing before you do:

- The re-arm needs a client to actually sync through the shim, because that is where the
  delete is seen. Nothing is lost if none does; the translation is armed the next time one
  does.
- The first batch after a cleanup applies cleanly. Measured on 2026-08-28 against a live
  Immich v3.1.0 and the Android app: 69 duplicates removed permanently in one go, and the
  next sync pass carried all 69 deletes in a single 10,955-byte response that the device
  acked — no constraint violation, no retry. The shim re-armed all 69 translations inside
  1.5 seconds. `shim_gates_opened_total` did not move, as intended, and
  `shim_touches_total` rose by exactly 69.
- **The cleanup itself opens a re-upload window, and this is the part to plan around.**
  The re-arm happens when the shim *sees* the delete, so the corrected replacement can only
  be re-offered on a later pass. Between the two, nothing in the device's mirror holds that
  checksum and the local file is a backup candidate again. Measured in the same run: the
  deletes landed at 10:28:34 and the translations at 10:29:31, and the device's backup scan
  fell in that 57-second gap and began uploading the very files just removed. It is bounded
  — each returning copy is recognised as `re_uploaded`, is never compressed or deleted, and
  correctly holds its translation back again — but it undoes the cleanup. **Do the removal
  with the device's backup switched off, and switch it on again only after a sync has
  carried the translations.** Deleting in small batches does not help; the window is
  structural, not a function of batch size.

### If you already ran the unreleased permanent-delete build

**Only concerns a deployment that ran `main` between the shim landing and this fix, with
`delete_mode: permanent`.** Everyone else has nothing to do, and on a `trash` deployment the
statement below must not be run at all — see the hazard.

On `delete_mode: permanent` with `retention_days: 0` the original is deleted inline, and
that path opened no gate: `original_freed_at` stayed empty on every job it finished, so the
shim went on passing those assets through untranslated. Measured on one live deployment on
2026-08-26: 370 permanently deleted originals, 370 ledger rows, 0 open gates.
`retention_days > 0` was never affected — those deletes are finalised by the trash sweeper,
which opened its gates correctly.

The fix repairs everything from this version on. The gates already missed are recoverable
without asking Immich anything, because `updated_at` on such a job is the moment the delete
happened:

```sql
UPDATE jobs
   SET original_freed_at = updated_at
 WHERE state = 'done'
   AND source_checksum IS NOT NULL
   AND owner_id IS NOT NULL
   AND new_asset_id IS NOT NULL
   AND original_freed_at IS NULL;
```

The image ships no `sqlite3` command line, so it goes through Python. Count first — this
one is read-only and safe to run with the service up:

```bash
docker compose exec immich-compressor python3 -c "
import sqlite3
db = sqlite3.connect('file:/var/lib/immich-compressor/state.db?mode=ro', uri=True)
print(db.execute('SELECT COUNT(*) FROM jobs WHERE state = \'done\' '
                 'AND source_checksum IS NOT NULL AND owner_id IS NOT NULL '
                 'AND new_asset_id IS NOT NULL AND original_freed_at IS NULL').fetchone()[0],
      'gates to open')
"
```

If that number is not the number of originals you permanently deleted, stop and read the
hazard below. Then apply it, with the service stopped so nothing else is writing:

```bash
docker compose stop immich-compressor
docker compose run --rm --entrypoint python3 immich-compressor -c "
import sqlite3
db = sqlite3.connect('/var/lib/immich-compressor/state.db')
rows = db.execute('UPDATE jobs SET original_freed_at = updated_at WHERE state = \'done\' '
                  'AND source_checksum IS NOT NULL AND owner_id IS NOT NULL '
                  'AND new_asset_id IS NOT NULL AND original_freed_at IS NULL').rowcount
db.commit()
print(rows, 'gates opened')
"
docker compose start immich-compressor
```

**Never on `trash`, and not blind after a `delete_mode` switch.** The job store does not
record which mode was in force for a given job. On a `trash` deployment those same rows
describe originals that are still in the trash *still holding their checksums*, and opening
their gates tells the shim it may hand those checksums to clients while the originals exist.
That is the exact write the gate is there to prevent: the phone's mirror allows one row per
`(owner, checksum)`, so it either drops the original's row or aborts the sync batch. If you
switched to `permanent` on a known date, add `AND updated_at >= '2026-01-01T00:00:00'` with
that date to both statements, and satisfy yourself with the count before running the update.

**It opens gates and nothing else.** A gate opened by the service also makes the no-op update
that has the replacement re-offered to clients; a backfill of hundreds of rows does not make
hundreds of live updates, and moves no counter either. The translation is armed for those
assets all the same — a client sees it the next time anything changes that replacement for
another reason. [shim.md](shim.md#why-a-no-op-update-is-needed-at-all) explains why the
update exists.

### A new skip reason, `re_uploaded`, and two new columns

**Nothing to edit.** The `jobs` table gains `source_checksum` and `owner_id`, applied
automatically the first time the new version opens the database. Nothing is rewritten and
no job changes state.

From this version on, every job records what the original hashed to and who owned it before
it touches anything. If an asset later shows up carrying the checksum of an original this
service already replaced — a device that still held the file, uploading it again — the job
stops at `re_uploaded` instead of compressing the same bytes a second time. It is not
downloaded, encoded or deleted, and neither is the earlier replacement.

Two things to expect:

- **`report` may start showing `re_uploaded` counts that were previously invisible.** The
  re-uploads were already happening; what is new is that they are named. A trickle is one
  device that has not caught up. A burst usually means a reinstall or a second client such
  as `immich-go` pointed at the same files.
- **Jobs that ran before this release carry neither column**, and they cannot be
  backfilled — the original whose checksum they would hold is already deleted. An asset
  re-uploaded from one of those goes through the pipeline as before.

The verdict is deliberately stable: `reprocess` and `requeue` re-run the check and reach
`re_uploaded` again, the same way they do for an asset carrying a compressor marker.

### The FAQ's answer on re-uploads was wrong about `trash`

**Nothing to edit**, but if you chose `delete_mode: trash` because the FAQ said the
re-upload "does not arise", read
[the corrected answer](faq.md#will-my-phone-just-re-upload-the-original). Immich's trash
retention defaults to 30 days; when the scheduled purge hard-deletes the original, its
checksum stops being known to the server and the device can upload the file again. `trash`
buys 30 days and the ability to restore, which are both real — it does not prevent the
re-upload.

### The metadata gate stops failing jobs on arithmetic

**Nothing to edit**, and it changes when the gate fires. With
`behavior.metadata_verify: strict` — the default — a job failed when any tag differed
between the original and the re-encode, compared as text. Two tags in the wild print as a
raw decimal long enough to show the re-approximation that copying an EXIF rational always
performs, and those jobs failed on it. Measured on a live library on 2026-08-24: a backfill
batch of the 150 largest JPEGs failed 24 of the 67 images that produced an encode, all on
`EXIF:FocalPlaneYResolution changed: 6734.006734 -> 6734.006711`, and an earlier failure in
the same store was `EXIF:GPSAltitude '339.569 m' -> '339.5690021 m'`.

Numbers are now compared within a relative tolerance of 1e-6, so those pass. Nothing else
loosens: a tag that is missing from the output is still a finding, a value that really moved
is still a finding, a differing unit (`339.569 m` against `339.569 ft`) is still a finding,
and text tags such as `Make` and `Model` still have to match exactly.

Assets that already failed this way stay `failed` — the fix does not requeue anything. Bring
them back in one go with `requeue --failed --error-contains FocalPlaneYResolution --apply`,
which is new in this release; `reprocess <asset_id>` still takes them one id at a time.

### The metadata gate stops failing jobs on a time that gained a `+00:00`

**Nothing to edit**, and it changes when the gate fires again. exiftool writes an explicit
zero UTC offset onto an IPTC time that carried none, and with
`behavior.metadata_verify: strict` the two spellings of the same clock were a difference
like any other. Measured on a live instance on 2026-08-26:

```
IPTC:TimeCreated changed:         '11:24:38' -> '11:24:38+00:00'
IPTC:DigitalCreationTime changed: '11:24:38' -> '11:24:38+00:00'
```

**92 jobs** in a single backfill run failed on that, the first at 2026-08-25T05:18:26Z. The
time is the same time; only its written form changed.

A value that is a time — `HH:MM:SS`, or a full `YYYY:MM:DD HH:MM:SS`, either with an offset
or without — is now compared as a clock plus an offset, and an absent offset and a zero one
(`+00:00`, `Z`) mean the same thing. Everything else is unchanged. A **non-zero** offset is a
different time and still fails, whether it was added (`'15:46:30'` against
`'15:46:30+01:00'`) or changed (`'+01:00'` against `'+02:00'`); a clock that moved fails; a
date that moved fails; and anything that is not a time in that shape still compares
character by character.

`XMP:Orientation` also joins `EXIF:Orientation` on the ignore list, for the same reason that
tag has always been there: `normalize_orientation` pins the rotation to 1 once `-auto-orient`
has baked it into the pixels, and the XMP mirror describes exactly that rotation. Measured on
the same instance: `'Rotate 270 CW'` -> `'Horizontal (normal)'` on 2 jobs.

Assets that already failed this way stay `failed` — as above, nothing is requeued.
`jobs --status failed` lists them with the error each one failed on, and
`requeue --failed --error-contains TimeCreated --apply` brings that set back together.

### Videos that failed on their audio codec can be rescued — but not by default

**Read this one if `jobs --status failed` shows `Could not find tag for codec`.** The video
presets copy the audio stream, and MP4 cannot carry some of what an old camera or a DVD rip
produces, so ffmpeg's muxer refuses the file before a frame is encoded. Measured on a live
library on 2026-08-26, that was **119 of 172** failures in one backfill run — `pcm_u8` (108),
`amr_nb` (9), `pcm_dvd` (2).

**Nothing changes unless you write the setting.** It is off because it turns a job that
cannot finish into one that deletes an original, and the audio it re-encodes was lossless in
the source:

```yaml
behavior:
  transcode_unsupported_audio: true
```

Then bring the jobs that already failed back — every one of them still has its original:

```bash
immich-compressor requeue --failed --error-contains "Could not find tag for codec" --apply
```

The first attempt still copies the audio. Only a run the container refused is retried, with
the audio re-encoded to 128 kbit/s AAC, and the log names every file that happens to.

### The metadata gate stops failing jobs on a re-approximated fraction

**Nothing to edit**, and it is the third change in this release to when the gate fires. A
value that exiftool prints as one whole fraction — `1/100` for an exposure time — was read as
the number in front of the slash with the rest treated as a unit, so two fractions were
compared character by character and the tolerance above never reached them. Measured on a
live library on 2026-08-26:

```
EXIF:ShutterSpeedValue changed: '1/999963365' -> '1/999963296'
```

**6 jobs** failed on that in the same backfill run. Evaluated, the two differ by 6.9e-8.

A value that is one whole fraction is now evaluated and compared like any other number. This
cannot loosen the gate on a real exposure time: two *integer* denominators only land within
1e-6 of each other once the denominator passes a million, so `'1/8000'` against `'1/7999'`
and `'1/100'` against `'1/101'` are both still findings. A fraction with anything after it is
not treated as one at all — `'4/2/2026'` and `'2/1/2026'` are two dates in a caption, and
they still compare exactly.

Assets that already failed this way stay `failed` — as above, nothing is requeued.
`requeue --failed --error-contains ShutterSpeedValue --apply` brings that set back together.

## 1.3.0 → 1.3.1

**Nothing to edit.** One line of output changed, on the command you reach for when something
has already gone wrong.

Under `delete_mode: permanent`, `restore` used to open with a warning that originals removed
by this service *"were not trashed and cannot be restored"* — printed before a single id had
been tried, gated on the mode the deployment is in *now* rather than on the mode that
removed anything, and false for every original that really was sitting in the trash.
Measured on a live stage-4 deployment on 2026-08-23: a run that went on to report
`restored 4 asset(s) from the trash` printed that refusal directly above it.

The line is gone. What stands in its place is the accurate version 1.3.0 already printed
*after* the request — how many ids Immich no longer has, and why — which comes from the
server's own answer rather than from local configuration. Exit codes are unchanged.

## 1.2.0 → 1.3.0

### `restore --all-pending` works on a deployment that has run `delete_mode: permanent`

Nothing to edit, and worth knowing before you need it. Until this release the command sent
the source id of every completed job in a single `POST /trash/restore/assets`, and one id
Immich no longer had refused the **whole** request — so on any deployment that had ever run
stage 4 the rollback restored nothing at all, including the originals that really were
sitting in the trash. Measured on 2026-08-23 against a live v3.1.0 instance: 46 of the 50
ids had been force-deleted by an earlier stage-4 run, and the one recoverable original
stayed trashed.

It now batches the selection and halves a refused batch until each unknown id stands alone,
so a dead id costs only itself. What comes back is restored and counted by the server; what
Immich no longer has is reported as a count on stderr, with the reason.

**The exit code changed.** A run that could not restore everything now exits **3** rather
than **1**, and prints what it did restore. If a script of yours branches on `restore`
failing, this is the line to look at: `0` every id came back, `3` some ids are no longer in
Immich's database, `2` nothing was selected, `1` the call to Immich failed. The per-asset
form `restore <assetId>` goes through the same path and gains the same codes.

### `backfill` has two phases now

Nothing to edit, and the command you know still works: `backfill --type VIDEO --limit 50
--apply` is `backfill run`, which is the default mode. What is new is that a run works from
an inventory the scan builds, so `--limit` counts jobs rather than search results and a
second run continues instead of re-reading the same answer.

```bash
immich-compressor backfill scan      # inventory the library, queue nothing
immich-compressor backfill status    # what is worth compressing, and what is left
immich-compressor backfill run --limit 50 --apply
```

The inventory is a new table in the existing database and is created on open, like every
other schema change. It holds no state you cannot throw away: `backfill scan --rescan`
rebuilds it.

**If you tried `backfill --type IMAGE` before and got nothing**, that was not your library.
The endpoint it used answers with videos whatever you ask it for; the scan walks a different
one. See [operations.md](operations.md#working-through-the-existing-library).

## 1.1.0 → 1.1.1

### Breaking: `behavior.min_size_bytes` is now `behavior.min_savings_bytes`

**This one needs an edit.** The old key guessed from the *input* size whether a job was
worth doing; the new one measures what the encode actually *saved*. Because they mean
different things, the value does not carry across — the default drops from 20 MiB to 1 MiB.

```yaml
behavior:
  min_savings_bytes: 1048576   # 1 MiB — was min_size_bytes: 20971520
```

A config that still says `min_size_bytes` is refused at startup with the replacement named
in the error, rather than with a generic "extra inputs are not permitted".

It also doubles as the pre-download filter, and that half needs no tuning at all: a file
cannot save more bytes than it has, so rejecting an asset smaller than the threshold before
the download is provably free of false negatives.

### New: JPEG stills

`enabled_types: [VIDEO, IMAGE]` and `IMAGE` in the workflow's type filter turn it on;
`setup` now writes both. **JPEG only** — RAW, HEIC, PNG, GIF, TIFF and WebP are all skipped
as `unsupported_format`, motion photos as `embedded_media`, and already-compressed sources
as `source_quality`. Read [safety.md](safety.md#why-only-jpeg-stills) before enabling it on
a library you care about, and note that `behavior.metadata_verify: warn` is refused
together with `delete_mode: permanent`.

To keep a video-only deployment exactly as it was, change nothing: `enabled_types` still
defaults to `[VIDEO]` in the code, and an existing `config.yaml` is not rewritten.

### The image preset changed if you had written one

The generated stills preset is now
`magick {input} -auto-orient -quality 82 -interlace Plane {output}` — `magick` because
`convert` is a deprecated alias in ImageMagick 7, `-interlace Plane` because it is free
(same DCT coefficients, `compare -metric AE` = 0, 3-8 % smaller), and without
`-sampling-factor` because forcing 4:2:0 halves chroma resolution on a 4:4:4 source and no
sanity check would notice. A hand-written `presets:` block is left alone, as always.

### Worker lanes

There is now one worker lane per entry in `enabled_types`, each running
`behavior.concurrency` tasks — so `[VIDEO, IMAGE]` with `concurrency: 1` runs up to two
encodes at once. Jobs queued by an earlier version carry no asset type and stay claimable
from every lane, so the upgrade strands nothing.

## 1.0.0 → 1.1.0

**Nothing is required.** A 1.0.0 deployment keeps working untouched.

### Your `presets:` still win

If your `config.yaml` has a `presets:` block, it is used exactly as before. Hardware
detection is skipped entirely — not consulted and overridden, skipped — so an upgrade cannot
change what your deployment encodes.

To adopt detection instead, delete the `presets:` block and restart. `immich-compressor
hardware` shows what it would choose before you commit to it:

```bash
docker compose exec immich-compressor immich-compressor hardware
```

`behavior.quality: balanced` (the new default) maps to `-crf 26` for x265 and `-quality 82`
for stills, which is exactly what 1.0.0's example presets carried.

### `behavior.concurrency` is derived when you do not set it

If your config sets it, that value is kept. If it does not, the service derives it from the
container's CPU budget and pins it to 1 whenever a GPU preset is selected. `config.example.yaml`
in 1.0.0 shipped `concurrency: 1`, so most deployments already set it explicitly.

### New: local settings belong in an override file

`docker-compose.yaml` now pulls the published image and carries only inert defaults. Move
anything deployment-specific — resource limits, the go-live flags, a locally built image —
into `docker-compose.override.yaml`, which compose loads automatically and which is
gitignored:

```bash
cp docker-compose.override.example.yaml docker-compose.override.yaml
```

To keep building locally instead of pulling:

```yaml
# docker-compose.override.yaml
services:
  immich-compressor:
    build: .
    image: immich-compressor:local
```

### New: `.env`

`immich-compressor setup` writes it. If you were exporting `IMMICH_API_KEY` and
`COMPRESSOR_TOKEN` in your shell, moving them into `.env` (mode 0600) means `docker compose
up -d` works from a clean login.

### Gone: `PLAN.md`

Its verified API findings are now [immich-api-notes.md](immich-api-notes.md); the rest was
superseded by the code.

### Marker v1 → v2 (unchanged from 1.0.0)

The `compressor` marker carries a version. v1 was written by a sanity gate that compared
stored frame sizes and therefore rejected **every rotated video** as a resolution change. v2
treats a v1 marker *without* a `replacedBy` field as stale and gives the asset one more
attempt under the current gate; a marker that records a real replacement always blocks.

The service heals itself for anything triggered again. To sweep the backlog:

```bash
docker compose exec immich-compressor immich-compressor requeue --reason no_gain
docker compose exec immich-compressor immich-compressor requeue --reason no_gain --apply
```

## Downgrading

Pin the previous tag in `docker-compose.override.yaml`:

```yaml
services:
  immich-compressor:
    image: ghcr.io/navilois/immich-compressor:1.0.0
```

The job store is forward-compatible within a major version: columns added by a newer release
are ignored by an older one. Configuration keys an older version does not know (`hardware:`,
`behavior.quality`, `behavior.min_savings_bytes`, `behavior.metadata_verify`) are rejected
with `extra="forbid"`, so remove them when you downgrade — and put `min_size_bytes` back if
you are going below the release that renamed it.
