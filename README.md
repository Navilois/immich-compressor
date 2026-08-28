# immich-compressor

**Recompress the originals in your [Immich](https://immich.app) library, automatically —
and never delete one before its replacement has been verified.**

[![CI](https://github.com/Navilois/immich-compressor/actions/workflows/ci.yml/badge.svg)](https://github.com/Navilois/immich-compressor/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/Navilois/immich-compressor?sort=semver)](https://github.com/Navilois/immich-compressor/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Image](https://img.shields.io/badge/ghcr.io-navilois%2Fimmich--compressor-blue?logo=docker)](https://github.com/Navilois/immich-compressor/pkgs/container/immich-compressor)
[![Immich](https://img.shields.io/badge/Immich-v3.0.0%2B-4250af)](https://immich.app)

---

## 118.66 GiB, one library, zero originals lost

Over three days in August 2026 this service worked through a **259 GB production Immich
library** — 48,958 photos and 4,717 videos, one 452 G partition with nothing to move to, and
the originals backed up on a separate disk outside Immich.

| | |
|---|---|
| **Assets compressed** | 22,586 of 30,790 considered |
| **Reclaimed** | **118.66 GiB** — 174.79 GiB down to 56.13 GiB |
| **Mean ratio** | 0.32 (best single file 0.012) |
| **Disk** | 26 G free → **120 G free**, 95 % used → 73 % used |
| **Originals lost** | **0**, beyond the 22,586 it intentionally replaced |
| **Failed jobs** | 172 — every one left its original untouched |

That run was `delete_mode: permanent`, `retention_days: 0`: every completed job deleted its
original immediately and irreversibly, after a four-step verification chain confirmed the
replacement on the live server. It is the strictest setting this project has, and it is not
where you start. [The numbers in full](#what-that-run-actually-looked-like).

---

## Why this exists

Immich is a Google Photos replacement, and a Google Photos replacement compresses. Immich
deliberately does not: its transcoding produces a **playback copy** and leaves the original
untouched — exactly right for a photo library, and the reason the disk underneath one only
ever grows.

This was written for the case where that trade is the wrong one: **the originals are backed
up elsewhere**, on a separate disk that is not Immich. The copy Immich holds does not have
to be the archival one, so it can be smaller — as long as nothing is lost in making it
smaller. Not a tag, not an album, not a rotation, not a place in the timeline. And as long
as it reaches the library that is already there, not just the next upload.

Everything else — the sanity gate, the metadata diff, the four-step verification chain, the
checksum ledger — is a consequence of that second paragraph.
**[docs/motivation.md](docs/motivation.md)** is the full list of what had to be true before
this could be pointed at a real library.

---

## What it is

An Immich workflow fires a webhook when an asset finishes metadata extraction. This service
downloads the original, recompresses it — video with ffmpeg on whatever encoder your machine
can actually run, JPEG stills with ImageMagick — checks the result nine ways, uploads it,
carries over everything that can be carried over, and proves the metadata survived. Only
then, and only if you have asked it to, does it remove the original.

```
Immich  (Workflow: AssetMetadataExtraction -> filters -> webhook)
   |  POST /webhook   {type, trigger, data.asset}
   v
immich-compressor
   guard -> download -> encode -> metadata gate -> sanity gate -> upload
         -> copy links -> tags & fields -> markers -> (verified) remove original
```

One container, one process, SQLite. No web UI, no Redis, no queue, no telemetry, and no
network traffic to anything but your own Immich server.

---

## Try it in five minutes — it cannot touch anything yet

```bash
git clone https://github.com/Navilois/immich-compressor
cd immich-compressor
./scripts/quickstart.sh
```

`setup` checks your API key against the server and **names any permission it is missing**,
detects your hardware, writes `config.yaml` and `.env`, and creates the Immich workflow.
It never starts the service and never changes your library. Then:

```bash
docker compose up -d
docker compose logs -f immich-compressor
```

Upload a video or a photo over 1 MiB, wait five minutes, and ask what it thinks:

```
$ docker compose exec immich-compressor immich-compressor report

=== immich-compressor report ===
database: /var/lib/immich-compressor/state.db
webhooks: 5 received, 0 rejected (bad or missing token)
jobs total: 5
  done             2
  failed           1
  skipped          2
skip reasons:
  dry_run              1
  no_gain              1
compressed assets: 2
saved: 22.9 MiB (average ratio 0.5263)
failed jobs (1):
  <assetId>   GET /assets/<id>/metadata -> HTTP 400: {"message":"Not found or no asset.read access"}
```

**The shipped configuration is inert**: `dry_run: true`, `trash_original: false`,
`delete_mode: trash`. It downloads nothing, uploads nothing and deletes nothing. Every asset
shows up as `skipped: dry_run` — it saw them, decided it would compress them, and changed
nothing. The `no_gain` line is the gate refusing a result that would not have been worth it;
the failure names the API permission the key is missing, in the error itself.

When you like what the report says, [docs/safety.md](docs/safety.md#going-live-in-four-stages)
walks through going live one reversible stage at a time. Full walkthrough:
**[docs/quickstart.md](docs/quickstart.md)**.

---

## Is this safe?

It deletes originals of photos you cannot re-take. That is the risk, and here is how it is
contained.

**Three separate settings have to change, in that order**, before an original can be
removed — and `delete_mode: permanent` is refused at startup unless the other two already
agree.

**Before an original is ever removed, four things are checked against the live server:**

1. the replacement exists and is not itself in the trash;
2. the server's checksum equals the SHA-1 the encoder computed for the bytes it uploaded;
3. `exifInfo.dateTimeOriginal` is set, so the asset sits at the right place in the timeline;
4. the `compressor` marker is present, so the replacement is traceable back to its source.

If any of them fails, **nothing is deleted** — the job waits an hour and tries again. The
chain runs in both delete modes, so a failing condition surfaces while the delete is still
undoable.

**Before anything is uploaded**, the encode has to pass a
[sanity gate](docs/safety.md#the-sanity-gate): size ratio, bytes saved, decodability,
rotation-aware display size, bit depth, HDR transfer function, duration drift, audio stream
count and capture date. A 10-bit HDR source cannot be silently flattened to washed-out SDR,
and a rotated portrait clip cannot come back sideways.

**It never touches** external libraries, live photos, edited assets, locked-folder assets,
anything already in the trash, anything with manually named faces, or anything it has
processed before. It never empties your trash.
[The full list](docs/safety.md#what-it-never-touches).

**Stills get three more refusals on top of that.** Only JPEG is compressed — RAW, HEIC, PNG,
GIF, TIFF and WebP are all filed under `IMAGE` by Immich and all of them are skipped, because
a raw file run through the encoder would be developed into an 8-bit JPEG, pass every check,
and lose its original. Motion photos are detected and skipped rather than silently losing
their video. An already-compressed source is left alone instead of buying a second generation
of artefacts. And every EXIF/GPS/XMP/IPTC tag is compared before and after: a tag that does
not survive fails the job, with the original untouched.

**You can undo it.** With the default `delete_mode: trash`, `immich-compressor restore
--all-pending` brings every original back — and on a deployment that has run
`delete_mode: permanent` it brings back everything that is still there and tells you how many
ids Immich no longer has (see [safety.md](docs/safety.md#rolling-back)).

**One thing to know before you go live:** Immich's `AssetMetadataExtraction` trigger fires
in bulk, so **Administration → Jobs → Extract Metadata** re-fires the workflow for your
*entire* library, not just new uploads. Every webhook carries `createdAt`, which dates the
upload rather than the exposure, and `behavior.max_asset_age_hours` (24 h by default)
refuses anything older than that — so the button is safe to press, and importing a thousand
photos from 2009 still goes through. It is still not a way to reach a backlog: use
`backfill` for that — it inventories the library, tells you how much of it is worth
compressing, and queues it in batches you choose, biggest first —
[the details, and why](docs/operations.md#the-metadata-extraction-trap).

Full detail, and the four stages of going live: **[docs/safety.md](docs/safety.md)**.

---

## What that run actually looked like

`immich-compressor` 1.3.1 against Immich v3.1.0, VAAPI hardware encode on
`/dev/dri/renderD128`, one worker per lane, 24–26 August 2026.

### The two lanes behave nothing alike

| Lane | Compressed | Skipped | Failed | Before | After | Reclaimed | Ratio |
|---|---:|---:|---:|---:|---:|---:|---:|
| Video | 2,104 | 1,219 | 149 | 104.62 GiB | 28.75 GiB | **75.87 GiB** | 0.2748 |
| Image | 20,482 | 6,813 | 23 | 70.17 GiB | 27.39 GiB | **42.78 GiB** | 0.3903 |
| **Total** | **22,586** | **8,032** | **172** | **174.79 GiB** | **56.13 GiB** | **118.66 GiB** | **0.3212** |

Video was 9 % of the completed jobs and 64 % of the reclaimed space. Both lanes ran
largest-file-first and both ratios drifted upward as the files got smaller — fixed per-job
overhead makes small files a worse deal, which is the clearest argument there is for running
a backfill on the big end of a library and leaving the tail alone.

### A quarter of the library was refused, on purpose

Roughly a quarter of everything considered was skipped. Each one is a guard declining a job,
and every one of them left its original untouched. In order of weight over the run:

| Reason | What it means |
|---|---|
| `no_gain` | Re-encoding saved too little, or would have dropped bit depth 12→8. |
| `embedded_media` | Motion photos — a video payload after the JPEG end marker. |
| `trashed` | Cancelled by hand. |
| `duplicate` | Already represented by a compressed copy. |
| `source_quality` | Source already below the preset's quality target. |
| `named_people` | Manually named faces attached. Never risked. |

`no_gain` and `embedded_media` together were the overwhelming majority: most of what this
service refuses, it refuses because compressing it would not have been worth it.

172 jobs failed. **All 172 originals survived** — a failure stops the job before the delete
step, and it is recoverable at any time. 119 of them were old camera AVIs and DVD rips whose
audio has no MP4 mapping (`pcm_u8`, `amr_nb`, `pcm_dvd`); `transcode_unsupported_audio: true`
is the opt-in that clears those, and [hardware.md](docs/hardware.md#the-audio-caveat) says
why it is off by default.

### What the run cost, stated plainly

The run was not smooth, and pretending otherwise would be the wrong advertisement:

- **The surge breaker latched for seven hours.** 201 assets arrived from webhooks inside ten
  minutes and tripped a threshold set too tight. Workers stopped, the sweeper stopped, and
  13,134 jobs sat idle until a human cleared it. It latches deliberately and survives a
  restart. It now ships **off** — [the reasoning](docs/operations.md#the-surge-breaker-off-by-default).
- **Three bugs turned up in the metadata gate**, all of the same family: exiftool
  re-approximates a value on copy and the comparison was exact. One was failing 36 % of every
  image that reached the encoder. **None of them risked a photo** — a gate that fires wrongly
  costs a failed job and leaves the original alone. That asymmetry is the whole design.
- **The phone re-uploaded 1,254 originals**, 11.18 GiB of them. That one gets its own
  section.

Every claim above came from the job store's own byte records and was confirmed against the
live Immich API rather than inferred from job state — for the first two batches, all 200
assets were checked individually, with no mismatches.

### Audited again two days later

Two days after the run drained, the whole library was measured once more, read-only: every
asset paged off the live server and joined against a snapshot of the job store. **53,855
assets, trash empty.**

**Not one byte-identical pair.** Every checksum in the library is distinct: after the
re-upload wave below was cleaned up, nothing duplicated survived, and every apparent
duplicate left is a collision of filenames rather than of content. That distinction matters
more than it sounds — matching on name alone found 1,736 candidates, name plus capture date
found 12, and name plus checksum found 0. A filename is not a duplicate signal, which is
why the ledger is the authority here and not the name.

**Four originals were never freed**, and that is everything else the audit found. Their
delete gate never opened, so the source and its replacement are both still on disk — 1.36
GiB across four jobs. They date from the day *before* this run and are not among the 22,586;
a gate repair applied during the run did not reach them. It is the gate failing in the
direction it was built to fail in: the original outlived the confirmation that would have
freed it, and both copies are sitting on disk. The cost is disk, not a photograph.

### Your number will be different, and this project will not pretend otherwise

How much you save depends entirely on your material. H.264 from a phone or a drone shrinks a
lot; already-HEVC video from a recent iPhone often will not reach `max_ratio` at all. JPEG
quality does not transfer across content — at q82 a detail-rich 4000x3000 photo measured
ratio 0.38 while a flat 3000x2000 one measured 0.60. Run a dry run, then stage 2 on a few
dozen files, and read your own report.
[How to get your own number](docs/faq.md#how-much-space-will-i-actually-save).

---

## Your phone will try to undo this

This is the objection that matters most on a self-hosted library, and it is real.

The Immich app decides what to back up **entirely offline**: it hashes each local file and
looks that hash up in the list of assets it has mirrored from the server. Once a compressed
replacement exists and the original is gone, the original's checksum exists nowhere — so the
phone concludes the file was never uploaded and sends it again, at full size. `delete_mode:
trash` only delays this by Immich's 30-day trash retention. On the production run it
happened 1,254 times.

There are two answers in the box, and you can use either or both.

**Recognition, on by default.** Every job records the checksum and owner of the original
before anything is touched. An asset that arrives carrying the checksum of an original this
service already replaced is skipped as `re_uploaded`, naming the earlier asset and its
replacement in the log. Nothing is downloaded, encoded or deleted — you do not pay for a
second generation of artefacts, and `report` counts the skips so a misbehaving device is
visible rather than silent.

**Prevention, opt-in.** The [checksum-translation shim](docs/shim.md) changes the one thing
the device actually reads: where the sync stream hands the phone the compressed replacement,
it substitutes the **original's** checksum into that one field. The phone finds a match for
the file it is holding and never queues it. Nothing in Immich is altered — the database, the
web app and every other client see the real checksum.

It is off by default, it needs two paths routed to this service through your reverse proxy,
and it is plainly a deliberate untruth told to one client. Upstream has declined to keep a
registry of deleted hashes server-side, so there is no honest version of this available
today; [docs/shim.md](docs/shim.md) sets out the trade, the staged rollout and the
[limits](docs/shim.md#limits) in full.

**Measured, on the same production instance:** 8,805 translations reached a real Android
device during a two-day field test, 8,409 of them accepted and acknowledged in a single
25.8 MB sync response with the checkpoint advancing. It also broke once, by a route the design had not anticipated —
a device that re-uploads a deleted original puts that checksum back into play — which is
fixed, shipped, and written up in the docs rather than quietly patched. It is the newest
thing here: roll it out in stages.

---

## Hardware, handled for you

You do not configure an encoder. On every start the service enumerates render nodes, reads
the chip ids from sysfs, asks `ffmpeg -encoders` and `vainfo` what they support, and then
**confirms each candidate with a real one-frame encode**. Whatever survives is what it uses.

```bash
$ docker compose exec immich-compressor immich-compressor hardware

Render nodes:
  /dev/dri/renderD128 (intel [0x8086:0x3e98] via i915)
    owned by gid 992, readable by this process
    vainfo: HEVC encode entrypoint present

Encoder choice:
  SELECTED  VAAPI HEVC (Intel Gen9-11, AMD) — hevc_vaapi on /dev/dri/renderD128
            the one-frame test encode succeeded
  rejected  hevc_qsv on /dev/dri/renderD128
            the one-frame test encode failed: Error creating a MFX session: -9.
  rejected  hevc_nvenc
            no NVIDIA device found: /dev/nvidia* is absent and nvidia-smi is not on PATH.
```

That is a real run on an Intel UHD 630. The QSV/VAAPI split that older documentation asked
you to work out from a generation table now resolves itself in about a second — and tells you
why.

| Vendor | Encoder | Verified on this project's hardware |
|---|---|---|
| Intel Gen12+ (Tiger Lake, Alder/Raptor Lake, N100, Arc) | `hevc_qsv` | no |
| Intel Gen9–11 (≤ 10th gen Core, UHD 630) | `hevc_vaapi` | **yes** |
| AMD | `hevc_vaapi` | no |
| NVIDIA | `hevc_nvenc` | no |
| Anything, including arm64 | `libx265` | **yes** |

"No" means the detection logic is unit-tested against captured tool output for that vendor,
but nobody has run it on that silicon. `hardware --json` turns your box into a
[hardware report](https://github.com/Navilois/immich-compressor/issues/new?template=hardware_report.yml),
which is the single most useful thing you can send this project.

Details, calibration and the CPU-budget fix: **[docs/hardware.md](docs/hardware.md)**.

---

## What else is in the box

- **Carries everything across**: album membership, favourite, shared links, stack, sidecar,
  tags, description, rating, GPS, capture date and timeline position. For stills the EXIF,
  GPS, XMP and IPTC blocks are diffed tag by tag afterwards, and a difference fails the job
  rather than the metadata.
- **A backfill that is not a big red button**: `backfill scan` inventories the library and
  tells you how much of it is worth compressing before you queue anything; `backfill run`
  takes a batch size and an order you choose, biggest first, and re-verifies every asset live
  at the moment it is queued.
- **One worker lane per asset type**, so a two-hour clip cannot hold a one-second photo job
  behind it.
- **Three independent loop guards**: a versioned server-side marker, the SQLite job store,
  and the workflow's filename filter.
- **Resumable**: every state transition is persisted, so a crash between upload and linking
  resumes instead of duplicating.
- **Right-sized automatically**: the encoder's thread count comes from the container's own
  cgroup limit, which is what x265 gets wrong on its own.
- **Observable**: JSON at `/stats`, Prometheus at `/metrics` — including six `shim_*`
  counters — and `report`, `jobs` and `backfill status` from the command line. No port is
  published by default, see [operations.md](docs/operations.md#endpoints).
- **No shell, ever**: preset commands are argv lists; the webhook secret is compared in
  constant time; the container runs as a non-root user.

---

## Documentation

| | |
|---|---|
| [quickstart.md](docs/quickstart.md) | Zero to a running dry run |
| [installation.md](docs/installation.md) | Every install option, permissions, networking, sizing |
| [safety.md](docs/safety.md) | The four stages, the verification chain, rollback |
| [hardware.md](docs/hardware.md) | GPU encoding, support matrix, calibration, the CPU budget |
| [workflow-setup.md](docs/workflow-setup.md) | The Immich side, and three things that will confuse you |
| [configuration.md](docs/configuration.md) | Every option — generated from the code |
| [operations.md](docs/operations.md) | CLI, endpoints, job states, backfill |
| [shim.md](docs/shim.md) | The checksum-translation shim, and the trade it makes |
| [troubleshooting.md](docs/troubleshooting.md) | When nothing happens |
| [architecture.md](docs/architecture.md) | The ten steps, idempotency, module map |
| [immich-api-notes.md](docs/immich-api-notes.md) | **Verified Immich v3 API behaviour** — worth reading for any Immich integration |
| [motivation.md](docs/motivation.md) | The problem it was built for, and the ten things that had to be true |
| [faq.md](docs/faq.md) | Including why not to just run ffmpeg in a cron job |
| [upgrading.md](docs/upgrading.md) | Version to version |

---

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) has the development setup. Bug reports, hardware reports
and documentation fixes are all welcome; the
[hardware report template](https://github.com/Navilois/immich-compressor/issues/new?template=hardware_report.yml)
is the single most useful thing you can send.

Security issues: [SECURITY.md](SECURITY.md). Licence: [MIT](LICENSE).
