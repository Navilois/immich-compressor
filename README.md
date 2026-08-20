# immich-compressor

**Recompress the originals in your [Immich](https://immich.app) library, automatically —
and never delete one before its replacement has been verified.**

[![CI](https://github.com/Navilois/immich-compressor/actions/workflows/ci.yml/badge.svg)](https://github.com/Navilois/immich-compressor/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/Navilois/immich-compressor?sort=semver)](https://github.com/Navilois/immich-compressor/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Image](https://img.shields.io/badge/ghcr.io-navilois%2Fimmich--compressor-blue?logo=docker)](https://github.com/Navilois/immich-compressor/pkgs/container/immich-compressor)
[![Immich](https://img.shields.io/badge/Immich-v3.0.0%2B-4250af)](https://immich.app)

An Immich workflow fires a webhook when an asset finishes metadata extraction. This service
downloads the original, recompresses it — video with ffmpeg on whatever encoder your machine
can actually run, JPEG stills with ImageMagick — checks the result eight ways, uploads it,
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

---

## Is this safe?

It deletes originals of photos you cannot re-take. That is the risk, and here is how it is
contained.

**Out of the box it deletes nothing, uploads nothing, and downloads nothing.** The shipped
configuration is `dry_run: true`, `trash_original: false`, `delete_mode: trash`. Three
separate settings have to change, in that order, before an original can be removed — and the
last one is refused at startup unless the other two already agree.

**Before an original is ever removed, four things are checked against the live server:**

1. the replacement exists and is not itself in the trash;
2. the server's checksum equals the SHA-1 the encoder computed for the bytes it uploaded;
3. `exifInfo.dateTimeOriginal` is set, so the asset sits at the right place in the timeline;
4. the `compressor` marker is present, so the replacement is traceable back to its source.

If any of them fails, **nothing is deleted** — the job waits an hour and tries again. The
chain runs in both delete modes, so a failing condition surfaces while the delete is still
undoable.

**Before anything is uploaded**, the encode has to pass a sanity gate: size ratio,
decodability, rotation-aware display size, bit depth, HDR transfer function, duration drift,
audio stream count and capture date. A 10-bit HDR source cannot be silently flattened to
washed-out SDR, and a rotated portrait clip cannot come back sideways.

**It never touches** external libraries, live photos, edited assets, locked-folder assets,
anything already in the trash, anything with manually named faces, or anything it has
processed before. It never empties your trash.

**Stills get three more refusals on top of that.** Only JPEG is compressed — RAW, HEIC, PNG,
GIF, TIFF and WebP are all filed under `IMAGE` by Immich and all of them are skipped, because
a raw file run through the encoder would be developed into an 8-bit JPEG, pass every check,
and lose its original. Motion photos are detected and skipped rather than silently losing
their video. An already-compressed source is left alone instead of buying a second generation
of artefacts. And every EXIF/GPS/XMP/IPTC tag is compared before and after: a tag that does
not survive fails the job, with the original untouched.

**You can undo it.** With the default `delete_mode: trash`, `immich-compressor restore
--all-pending` brings every original back. There is no telemetry, no phone-home, and no
network traffic to anything but your own Immich server.

**One thing to know before you go live:** Immich's `AssetMetadataExtraction` trigger fires
in bulk, so re-running **Administration → Jobs → Extract Metadata** queues your *entire*
library at once, not just new uploads. Work through a backlog with `backfill --limit`
instead, and disable the workflow before running extraction —
[the details, and why](docs/operations.md#the-metadata-extraction-trap).

Full detail, and the four stages of going live: **[docs/safety.md](docs/safety.md)**.

---

## Quickstart

```bash
git clone https://github.com/Navilois/immich-compressor
cd immich-compressor
./scripts/quickstart.sh
```

`setup` checks your API key against the server and **names any permission it is missing**,
detects your hardware, writes `config.yaml` and `.env`, and creates the Immich workflow.
Then:

```bash
docker compose up -d
docker compose logs -f immich-compressor
```

Upload a video or a photo over 1 MiB, wait five minutes, and:

```bash
docker compose exec immich-compressor immich-compressor report
```

Every asset shows up as `skipped: dry_run`. It saw them, decided it would compress them, and
changed nothing. When you are ready, [docs/safety.md](docs/safety.md) walks through going
live one reversible stage at a time.

Full walkthrough: **[docs/quickstart.md](docs/quickstart.md)**.

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
[hardware report](https://github.com/Navilois/immich-compressor/issues/new?template=hardware_report.yml).

Details, calibration and the CPU-budget fix: **[docs/hardware.md](docs/hardware.md)**.

---

## What you get

A real report, from the deployment this project is developed against:

```
$ docker compose exec immich-compressor immich-compressor report

=== immich-compressor report ===
database: /var/lib/immich-compressor/state.db
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

Five jobs, two of them compressed to 53 % of the original. One skipped as `no_gain`,
because the source was already efficient enough that the gate refused the result — that is
the gate working. One failed with the API key missing a permission, named in the error.

**Your number will be different, and this project will not pretend otherwise.** How much you
save depends entirely on your material: H.264 from a phone or a drone shrinks a lot,
already-HEVC video from a recent iPhone often will not reach `max_ratio` at all, and JPEG
quality does not transfer across content — at q82 a detail-rich 4000x3000 photo measured
ratio 0.38 while a flat 3000x2000 one measured 0.60. Run a dry run, then stage 2 on a few
dozen files, and read your own report.

The same data is available as JSON at `/stats` and in Prometheus format at `/metrics`. No
port is published by default — see [operations.md](docs/operations.md#endpoints).

- **Carries everything across**: album membership, favourite, shared links, stack, sidecar,
  tags, description, rating, GPS, capture date and timeline position. For stills the EXIF,
  GPS, XMP and IPTC blocks are diffed tag by tag afterwards, and a difference fails the job
  rather than the metadata.
- **One worker lane per asset type**, so a two-hour clip cannot hold a one-second photo job
  behind it.
- **Three independent loop guards**: a versioned server-side marker, the SQLite job store,
  and the workflow's filename filter.
- **Resumable**: every state transition is persisted, so a crash between upload and linking
  resumes instead of duplicating.
- **Right-sized automatically**: the encoder's thread count comes from the container's own
  cgroup limit, which is what x265 gets wrong on its own.
- **No shell, ever**: preset commands are argv lists; the webhook secret is compared in
  constant time; the container runs as a non-root user.
- **One container, one process, SQLite.** No web UI, no Redis, no queue, no telemetry.

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
| [troubleshooting.md](docs/troubleshooting.md) | When nothing happens |
| [architecture.md](docs/architecture.md) | The ten steps, idempotency, module map |
| [immich-api-notes.md](docs/immich-api-notes.md) | **Verified Immich v3 API behaviour** — worth reading for any Immich integration |
| [faq.md](docs/faq.md) | Including why not to just run ffmpeg in a cron job |
| [upgrading.md](docs/upgrading.md) | Version to version |

---

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) has the development setup. Bug reports, hardware reports
and documentation fixes are all welcome; the
[hardware report template](https://github.com/Navilois/immich-compressor/issues/new?template=hardware_report.yml)
is the single most useful thing you can send.

Security issues: [SECURITY.md](SECURITY.md). Licence: [MIT](LICENSE).
