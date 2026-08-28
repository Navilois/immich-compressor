# FAQ

## Why not just run ffmpeg in a cron job?

You can, and for a hundred files you probably should. What a script has to grow before it is
safe on a photo library is the interesting part:

| | A shell script | This |
|---|---|---|
| Finding new assets | poll the filesystem, or the API, and remember what you saw | Immich tells you, through a workflow webhook |
| Not doing it twice | a marker file, or a list you maintain | a versioned marker on the asset itself, plus a SQLite job store, plus a filename filter — three independent guards |
| Album, tags, rating, description, GPS | re-implement `PUT /assets/copy`, then discover it does not copy tags | done, and the [gaps are documented](immich-api-notes.md#2-put-assetscopy-moves-tags-description-rating-and-gps--but-through-the-sidecar) |
| Rotated video | 1920x1080+rot90 silently becomes 1080x1920 | `-noautorotate`, and a gate that compares display size |
| HDR | flattened to washed-out SDR without a warning | rejected by the gate |
| Knowing the encode is good | file size | ratio, bytes saved, decodability, display size, bit depth, HDR transfer, duration, audio streams, capture date |
| Deleting the original | `rm`, and hope | a four-step chain against the live server, with a retry instead of a delete when anything is off |
| Which encoder your box can run | you read a table and edit flags | a real one-frame encode picks it, and explains the ones it rejected |
| When it goes wrong at 3am | you find out later | job states, `/stats`, `/metrics`, a `restore` command |

The honest summary: this is a script that has already been wrong in eighteen documented ways
and been fixed each time. That is its whole value — the list is
[immich-api-notes.md](immich-api-notes.md#where-the-original-plan-was-wrong).

## Why not use Immich's own transcoding?

Because it does a different job. Immich transcodes to produce a **streamable playback
version** and keeps your original untouched — that is exactly right for a photo library, and
you should not turn it off.

This recompresses the **original itself**, out of band, and only replaces it once the result
has been verified. Immich has no concept of that, on purpose: replacing an original is a
decision only the library's owner can make.

They coexist. Immich keeps making playback versions; this shrinks what is underneath them.

## Does it phone home?

No. There is no telemetry, no analytics, no update check, and no network traffic to anything
but your Immich server. Grep for it: `httpx` is only ever pointed at `immich.base_url`.

## How much space will I actually save?

Nobody can tell you without your footage, and this project will not invent a number. What
determines it:

- **H.264 → HEVC** is where the gain is. Phone video from before ~2018, screen recordings,
  camcorder output, anything from a drone.
- **Already-HEVC footage** — iPhone 11 and newer — usually will not reach `max_ratio: 0.6`
  and will be skipped as `no_gain`. That is the gate working.
- Run a dry run, then stage 2 on a few dozen files, and read `report`. That is your number.

`scripts/calibrate.sh` sweeps the quality knob over your own clips and prints ratio and SSIM
per setting — see [hardware.md](hardware.md#measuring-instead-of-guessing).

## The asset ID changes. Does that break anything?

Yes, and it cannot be avoided: there is no replace endpoint in the Immich API, so the
compressed version is a new asset with a new id.

- **External deep links to the old asset break.** Shared links are copied to the
  replacement, but a URL someone bookmarked to the old id will not resolve.
- **Faces are re-detected** for the new asset, and manually assigned names can be lost.
  `skip_if_named_people: true` is the default for exactly this reason.
- Album membership, favourite, stack, tags, description, rating, GPS and timeline position
  do survive — that is most of the pipeline's work.

## Will my phone just re-upload the original?

Possibly, and `delete_mode: trash` only delays it. The Immich app decides what to back up
by checksum: it joins the hashes of the files on the device against the assets it has
mirrored from the server. A deleted asset leaves nothing in that mirror, so the file looks
like it was never uploaded and goes up again — as a **new** asset, with a new id and no
compressor marker.

**`trash` is a 30-day reprieve, not a fix.** Immich's own trash retention defaults to 30
days (`trash.days` in the server settings). While the original sits in the trash its
checksum is still known and the device stays quiet. When the scheduled purge hard-deletes
it, the checksum stops being known and the re-upload becomes possible — from the phone's
point of view a purge and a `force` delete are the same event.

**What this service does about it, by default.** It refuses to compress the same bytes
twice. Every job records the checksum and owner of the original before anything is touched,
and an asset that arrives carrying the checksum of an original this service has already
replaced is skipped as `re_uploaded`, naming the earlier asset and its replacement in the
log. Nothing is downloaded, nothing is encoded and nothing is deleted.

**And how to stop the upload happening at all.** The backup decision is made on the device
against the list of assets it has mirrored from the server, so the one place it can be
changed is that list. The optional [shim](shim.md) does exactly that: once the original is
really gone, it substitutes the original's checksum into the replacement's line in the sync
stream, so the phone finds a match for the file it is holding and never queues it. It is off
by default, needs two paths routed to this service through your reverse proxy, and is
plainly a deliberate untruth told to one client — [shim.md](shim.md) sets out the trade in
full.

Two limits worth knowing:

- The ledger only covers jobs that ran **after** the version that introduced it. For an
  original deleted before that, the checksum is gone from both sides and cannot be
  recovered.
- Recognition is not prevention. Without the shim the duplicate is on the server and stays
  there until you remove it; `report` counts the `re_uploaded` skips so a device that keeps
  doing it is visible rather than silent.

If the re-uploads keep coming, the cause is on the device — an app that has not synced, a
second device, or a folder-sync client such as `immich-go` pointed at the same files, none
of which this service can see.

## Can it compress photos too?

Yes, and `setup` enables it: `enabled_types: [VIDEO, IMAGE]` plus `IMAGE` in the workflow's
type filter. **JPEG only** — everything else Immich files under `IMAGE` is skipped as
`unsupported_format`, and that allowlist is what stops a raw file from being developed into
an 8-bit JPEG and losing its original. See
[safety.md](safety.md#why-only-jpeg-stills) for the reason per format.

A JPEG re-encode *is* generationally lossy in a way an H.264 → HEVC video re-encode largely
is not, so three things guard it beyond the normal sanity gate:

- `min_source_quality` leaves an already-compressed source alone. Re-encoding a q60 JPEG at
  q82 was measured at 158 368 -> 190 488 bytes: a second generation of artefacts *and* a
  bigger file.
- `min_savings_bytes` throws away results that are technically smaller but not worth a
  permanent new asset. Photos are small, and ratio is the wrong axis on a cheap encode.
- The [metadata gate](safety.md#the-metadata-gate) compares every EXIF/GPS/XMP/IPTC tag
  before and after, and fails the job rather than the metadata.

To leave stills alone, drop `IMAGE` from `behavior.enabled_types` and from the workflow's
type filter.

## Why does it skip so many of my photos?

Probably one of three deliberate refusals, all visible in `report`:

| Skip reason | Meaning |
|---|---|
| `unsupported_format` | Not a JPEG. RAW, HEIC, PNG, GIF, TIFF and WebP are out by design. |
| `source_quality` | Already at or below the preset's quality target — a re-encode would only add artefacts. |
| `embedded_media` | A motion photo, whose video would be silently dropped by a re-encode. |

`immich-compressor encode <file> --type IMAGE` reproduces the decision for a single file
locally, without touching the server.

## Does it work with Immich 2.x?

No, and it will not. It is built on workflows, which were introduced in v3.0.0.

## Why does it wait five minutes before doing anything?

`initial_delay_seconds`. Immich queues thumbnail generation, machine learning, OCR and smart
search for every new asset; starting an ffmpeg run on top of that on a home server is how
you make the UI unresponsive. It also gives you time to add tags or a rating, which the
pipeline then picks up from the live asset rather than the stale webhook payload.

## Can I run more than one at a time?

`behavior.concurrency`, capped at 4, and derived from the container's CPU budget when you do
not set it. It is pinned to 1 whenever a GPU preset is in use: an iGPU has one
fixed-function encode block and Immich's own transcoding already competes for it.

Note that it counts *per lane*, and there is one lane per entry in `enabled_types`. With
`[VIDEO, IMAGE]` and `concurrency: 1` that is up to two encodes at once. The split is
deliberate: without it a single clip with `timeout_s: 7200` holds the only worker for two
hours while every one-second image job queues up behind it.

## What happens if it crashes mid-job?

Every state transition is persisted before the call that causes it. A crash between upload
and copy resumes from `uploaded` rather than uploading a second time. A crash before the
verification chain leaves the original exactly where it was.

## Where does it keep state, and do I need to back it up?

A SQLite file in the `compressor-state` volume. Back it up if you want the report history;
losing it is not dangerous, because the server-side marker still stops assets being
processed twice.

The backup that actually matters is Immich's: Postgres plus the upload directory. That is
the only rollback for `delete_mode: permanent`.

## Is it safe to upgrade?

Yes — see [upgrading.md](upgrading.md). Configuration is backward compatible, and a
`config.yaml` with hand-written `presets:` keeps behaving exactly as it did.
