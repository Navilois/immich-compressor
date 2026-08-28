# Why this exists

## The situation it was written for

Two things are true about the library this started with, and both of them have to hold
before any of it is a good idea.

**The originals are backed up on a separate disk, outside Immich.** Immich is not the
archive; it is the way the archive is looked at. That single fact is what makes replacing an
original a reasonable thing to want rather than a reckless one, and it is the assumption
underneath every default in this project. If Immich holds the only copy of your photographs,
stop here and read [safety.md](safety.md) before anything else — the four stages exist so
that you can find out what this does without betting the library on it.

**Immich is the Google Photos replacement, and that includes compression.** The service it
replaces shrank what you uploaded and got on with it. Immich, deliberately, does not:
transcoding produces a playback copy and the original is never touched. That is the right
call for a photo library and you should not turn it off — see
[Why not use Immich's own transcoding?](faq.md#why-not-use-immichs-own-transcoding) — but
it leaves the growth of the disk as an exercise for the reader. This is that exercise. The
compression half is all it does: Immich's own previews and playback versions are left alone,
and it never touches thumbnails, previews or the machine-learning cache.

## What had to be true

Ten conditions, all of them older than the code. Nothing here was designed and then
justified; each row is a thing that had to work before the next one was worth starting.

| | The condition | What answers it |
|---|---|---|
| **R1** | It cannot be a change to Immich | Compression as an option has been declined upstream, so this is a separate service that speaks only the public REST API — [why a separate service](architecture.md#why-a-separate-service) |
| **R2** | Both the next upload *and* the years already there | Two entry points on one pipeline: the workflow webhook for new assets, `backfill` for the library that exists — [working through the existing library](operations.md#working-through-the-existing-library) |
| **R3** | No metadata may be lost | Album, favourite, shared links, stack, sidecar, tags, description, rating, GPS, capture date and timeline position are carried over, and every EXIF/XMP/IPTC tag is compared before and after — a tag that does not survive fails the job — [the metadata gate](safety.md#the-metadata-gate) |
| **R4** | Rotation must never become my problem | `-noautorotate` on every video template, and a gate that compares *display* size rather than stored size, so a portrait clip cannot come back sideways and pass — [rotation and orientation](architecture.md#rotation-and-orientation) |
| **R5** | It uses the hardware in this house | Render nodes enumerated, chip ids read from sysfs, `ffmpeg -encoders` and `vainfo` asked, and every candidate confirmed by a real one-frame encode before it is chosen — [why detection instead of a table](hardware.md#why-detection-instead-of-a-table) |
| **R6** | It must not create duplicates | Three independent guards against processing the same asset twice — a versioned marker on the asset, the SQLite job store, and a filename filter — plus a checksum ledger that recognises an original coming back — [idempotency](architecture.md#idempotency) |
| **R7** | The phone must not undo the work | An arriving asset carrying a replaced original's checksum is skipped as `re_uploaded`, on by default — [Will my phone just re-upload the original?](faq.md#will-my-phone-just-re-upload-the-original) |
| **R8** | Immich's own workflows are the trigger | One workflow, one webhook, no polling and no filesystem watching; the endpoint verifies the secret, writes one row and answers `202` — [why each step is what it is](workflow-setup.md#why-each-step-is-what-it-is) |
| **R9** | Setting it up must be easy | A guided `setup` command that writes the configuration and can create the workflow, a shipped configuration that cannot delete anything, and a dry run as the default state — [quickstart.md](quickstart.md) |
| **R10** | No proxy in front of Immich | Not fully met. See below. |

## R10, and the one thing that is not solved

Everything above is answered by the service sitting *beside* Immich and talking to its API.
Nothing intercepts a request, and nothing needs to.

R7 is the exception. The Immich mobile app decides what to back up **offline**, by comparing
checksums it computed on the device against what it believes the server has. It never asks
the server "have you seen this file before?" in a way that a replaced original can answer, so
recognising a re-upload after the fact — which is what R7 does — cannot prevent the upload
itself. Preventing it means answering one specific Immich API call differently, and answering
an API call differently means standing in front of it.

That is the [checksum-translation shim](shim.md), and it is deliberately the smallest thing
that could work: **off by default**, one route, and it only ever translates a checksum whose
original this service has verifiably removed — [the one rule that makes it
work](shim.md#the-one-rule-that-makes-it-work). It is a workaround with a documented cost,
not a feature, and [what it is not](shim.md#what-it-is-not) is the part worth reading before
turning it on.

The honest version of R10 is upstream:
[immich-app/immich#29922](https://github.com/immich-app/immich/pull/29922) teaches the app to
track server-side deletions, which would make the shim unnecessary. As of **2026-08-28** that
pull request is open, has no reviews and conflicts with `main`; there is no timeline, and
nothing in this project depends on it landing.

## What this deliberately does not solve

The list above is short on purpose, and the things missing from it are missing for reasons:
downscaling 4K to 1080p is refused rather than unimplemented, the asset id changes and
[that has consequences](faq.md#the-asset-id-changes-does-that-break-anything), finding the
duplicates you *already* have is a different program, and one API key means one owner.
[What it deliberately is not](architecture.md#what-it-deliberately-is-not) has the full set.
