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
| Knowing the encode is good | file size | ratio, decodability, display size, bit depth, HDR transfer, duration, audio streams, capture date |
| Deleting the original | `rm`, and hope | a four-step chain against the live server, with a retry instead of a delete when anything is off |
| Which encoder your box can run | you read a table and edit flags | a real one-frame encode picks it, and explains the ones it rejected |
| When it goes wrong at 3am | you find out later | job states, `/stats`, `/metrics`, a `restore` command |

The honest summary: this is a script that has already been wrong in eleven documented ways
and been fixed each time. That is its whole value.

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

Possibly. The Immich app deduplicates by checksum (`POST /assets/bulk-upload-check`). Once
the original is **permanently** deleted, a device that still holds the file can upload it
again, and the service will compress it again — the marker does not help, because it is a
new asset.

Watch for this during the rollout with a real device. There is no clean fix from this side.
With `delete_mode: trash` the checksum is still known to the server, so it does not arise.

## Can it compress photos too?

Yes — `enabled_types: [VIDEO, IMAGE]` plus the type filter in the workflow. Think about
whether you want to. A JPEG re-encode is generationally lossy in a way an H.264 → HEVC video
re-encode largely is not, and photos are small. Most people should leave it off.

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
