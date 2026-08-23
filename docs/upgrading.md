# Upgrading

```bash
docker compose pull
docker compose up -d
```

The compose file pins the **major** tag (`ghcr.io/navilois/immich-compressor:1`), so patch
and minor releases arrive that way and a breaking change never does. The
[CHANGELOG](../CHANGELOG.md) is the authority on what changed.

Job state lives in a volume and survives. Schema changes are applied automatically on open.

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
