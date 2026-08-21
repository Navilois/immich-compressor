# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **A bulk-trigger gate** (`behavior.max_asset_age_hours`, 24 h by default). Immich's
  `AssetMetadataExtraction` trigger is a maintenance operation: one click on
  **Administration → Jobs → Extract Metadata** re-fires the workflow for every asset in the
  library. Assets already recorded were immune; assets never seen were not, which was the
  whole library until it had been worked through. Every webhook carries `createdAt`, which
  dates the *upload* rather than the exposure, so a re-trigger is now refused at ingest
  while a legitimate import of a thousand old photos still passes — something a rate limit
  could not distinguish. A refusal writes no job, deliberately: `backfill` enqueues through
  the same `ON CONFLICT DO NOTHING`, and a row recorded here would put the asset permanently
  out of its reach. `max_asset_age_hours: null` turns the gate off and is refused at startup
  together with `delete_mode: permanent`.
- **JPEG stills are compressed too.** `enabled_types: [VIDEO, IMAGE]` and `IMAGE` in the
  workflow's type filter are what `setup` now writes. The encoder path already existed;
  what was missing was the decision logic around it.
- **Format allowlist** (`Preset.match.extensions`). Immich files RAW, PNG, GIF, TIFF, WebP
  and HEIC under type `IMAGE` exactly like JPEG, and ImageMagick reads DNG/CR2/CR3/NEF/ARW
  through libraw — without the list a raw file would be developed into an 8-bit JPEG, pass
  every sanity check, and have its original deleted. Anything not on the list is skipped as
  `unsupported_format`, which is deliberately a different reason from `no_preset`.
- **A metadata gate.** After the encode, source and output are compared with
  `exiftool -G -EXIF:all -GPS:all -XMP:all -IPTC:all`; any tag that is missing or changed is
  a finding. `behavior.metadata_verify` decides whether that fails the job (`strict`, the
  default) or only logs (`warn`), and `warn` is refused at startup together with
  `delete_mode: permanent` — a warning cannot undo a force-deleted original.
- **Motion photos are detected and skipped** as `embedded_media`. A Samsung or Google motion
  photo is a JPEG with an MP4 behind the end-of-image marker; a re-encode drops the video
  while every other check reports success. Two independent signals: the XMP markers, and
  payload after the EOI marker found by walking the JPEG's segment structure rather than
  searching for the last `FFD9`.
- `Preset.min_source_quality` — skip a still that is already at or below the preset's own
  quality target, since quantisation error is cumulative and a re-encode usually produces a
  *larger* file (measured 158 368 -> 190 488 bytes for a q60 source through the q82 preset).
  Skipped as `source_quality`.
- Per-preset overrides of `max_ratio`, `min_savings_bytes` and `require_date_time_original`,
  because video and stills have opposite economics.
- **One worker lane per enabled asset type**, backed by a new `asset_type` column on the job
  store (migrated automatically). Without it a single clip with `timeout_s: 7200` holds the
  only worker for two hours while every one-second image job queues up behind it. Rows
  written before the column existed carry `NULL` and stay claimable from every lane.
- `immich-compressor encode` additionally reports `source_quality`, `embedded_media` and
  `metadata_differences`, so every still-specific decision is visible before the pipeline
  makes it — without touching the server.
- `MAGICK_THREAD_LIMIT`, `MAGICK_MEMORY_LIMIT` and `MAGICK_MAP_LIMIT` in the image.
  ImageMagick is built with OpenMP and sizes its thread pool from the host core count,
  ignoring the container's cgroup limit — the same trap the video preset defuses with
  `pools=2 -threads 2`.

### Changed

- **Breaking: `behavior.min_size_bytes` is replaced by `behavior.min_savings_bytes`**
  (default 1 MiB, was 20 MiB). The old threshold guessed from the input size whether the
  work was worth doing; the new one measures whether it *was*. It also serves as the
  pre-download filter, and that half needs no calibration: a file cannot save more bytes
  than it has. A config that still carries the old key is refused at startup with the
  replacement named in the error. See [docs/upgrading.md](docs/upgrading.md).
- The generated stills preset is now
  `magick {input} -auto-orient -quality 82 -interlace Plane {output}`. `magick` because
  `convert` is a deprecated alias in ImageMagick 7; `-interlace Plane` because it is free
  (the same DCT coefficients reordered — `compare -metric AE` returns 0 — for 3-8 % less
  size); and no `-sampling-factor`, because ImageMagick then inherits the source's chroma
  subsampling instead of halving it on every 4:4:4 source, which no sanity check would
  notice.
- `immich-compressor hardware` lists the extensions a preset accepts, and `--json` carries
  them.

### Fixed

- **The metadata gate rejected every geotagged camera JPEG.** EXIF stores rationals, and
  copying a tag re-approximates the fraction, so a carry-over that loses nothing still moves
  the float: measured on a phone JPEG through the shipped preset, `ExposureTime` went
  `2497831/250000000` -> `1/100` and the GPS latitude seconds `16316639/1000000` ->
  `39421/2416`. Both print identically. Values are now compared as exiftool *presents* them,
  and the offset tags (`ThumbnailOffset`, `PreviewImageStart`, `OtherImageStart`,
  `StripOffsets`) are ignored because they are file positions, not content — the matching
  `*Length` tags stay compared, since a thumbnail length that moves is a truncated thumbnail.
- **`setup` unloaded `docker-compose.override.yaml`.** The `COMPOSE_FILE` line it writes for
  a detected GPU replaces compose's *default* file list, and the override is only ever in
  that default list — so naming an overlay there dropped the override entirely, taking the
  go-live flags (`BEHAVIOR__DRY_RUN`, `BEHAVIOR__TRASH_ORIGINAL`, `BEHAVIOR__DELETE_MODE`),
  the resource limits and any local image pin with it, in exact contradiction of the docs
  telling people to keep all of that there. Measured with `docker compose config`:
  `BEHAVIOR__DRY_RUN` resolved to nothing and the image fell back to
  `ghcr.io/navilois/immich-compressor:1`. The override is now appended last, where it wins,
  and only when the file exists — compose exits 1 on a file it cannot stat. One written
  afterwards, which is what `docs/safety.md` has you do at go-live, still has to be added to
  the line by hand; `setup` now says so, and `docs/safety.md` says it at the step where it
  matters.
- `.dockerignore` matched `__pycache__/` and `*.pyc` at the context root only, so
  `src/immich_compressor/__pycache__/` was copied into the image — 12 stale `.pyc` files on
  a measured rebuild, three of them orphans from a branch that was not even checked out.
  Harmless at runtime, but it made the image depend on which branch was last built.

## [1.1.0] - 2026-08-19

The "someone else can install this" release. The pipeline is unchanged; everything
around it — hardware selection, setup, packaging, docs — was rebuilt for people who did
not write it.

### Added

- **Automatic hardware detection** (`hardware.py`). Render nodes are enumerated from
  `/dev/dri`, vendor and device ids are read from sysfs, `ffmpeg -encoders` and `vainfo`
  are asked what they support, and every candidate is confirmed with a real one-frame
  encode before it is chosen. Intel Gen9–11 versus Gen12+ (VAAPI versus QSV) now resolves
  itself instead of being a documentation step.
- `immich-compressor hardware [--json]` — prints the detected devices, the preset chosen
  per asset type, every rejected candidate with the reason it was rejected, the CPU budget
  derived from the container's cgroup, and the YAML to paste if you want to pin the choice.
- **Built-in preset catalog** for `hevc_qsv`, `hevc_vaapi`, `hevc_nvenc`, CPU `libx265`
  and the ImageMagick stills preset. Presets no longer have to be written by hand.
- `hardware.mode` (`auto` | `cpu` | `qsv` | `vaapi` | `nvenc`) and `hardware.render_node`
  for pinning the choice, and `behavior.quality` (`balanced` | `higher` | `smaller`) for
  tuning quality without knowing ffmpeg's per-encoder quality flags.
- **CPU budget from cgroup v2.** `/sys/fs/cgroup/cpu.max` decides the x265 thread pool and
  the worker concurrency, which fixes x265 sizing its pool from the host core count and
  ignoring the container limit.
- `immich-compressor setup [--non-interactive]` — validates the API key against the
  server, names the permissions it is missing, runs hardware detection, writes a tuned
  `config.yaml`, generates a webhook token, writes `.env` with mode 0600, and creates the
  Immich workflow when the credentials allow it (otherwise prints the exact JSON and curl).
- `/metrics` in Prometheus text format: jobs by state, skip reasons, bytes saved, session
  counters and an encode-duration histogram, plus three `config_*` gauges so a deployment
  that quietly went live — or quietly did not — is visible on a dashboard. Hand-rolled;
  no new dependency.
- Published multi-arch image (`linux/amd64`, `linux/arm64`) at
  `ghcr.io/navilois/immich-compressor`, with OCI labels, provenance and an SBOM.
- `docker-compose.build.yaml`, `docker-compose.gpu-nvidia.yaml` and
  `docker-compose.override.example.yaml` overlays; `.env.example`; `scripts/quickstart.sh`.
- `scripts/check-links.py`, which verifies every internal link and heading anchor offline,
  and `scripts/check-language.sh`, an English-only guard — both wired into `make lint` and CI.
- A social preview image in `docs/assets/`, and `docs/maintainers/launch-checklist.md`.
- `docs/` tree: quickstart, installation, configuration (generated from the settings
  model), hardware, workflow setup, safety, operations, troubleshooting, architecture,
  the verified Immich API notes, FAQ, upgrading and a comparison with the alternatives.
- `docs/config.schema.json` plus a `yaml-language-server` modeline in
  `config.example.yaml`, so editors autocomplete and validate the config.
- Project health files: `LICENSE` (MIT), `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`,
  `SECURITY.md`, issue and pull request templates, `CHANGELOG.md`.
- CI on GitHub Actions: ruff, pytest on 3.12 and 3.13, compose validation, a language
  guard, an image build, CodeQL and Dependabot; a tag-triggered release workflow.
- `immich-compressor --version`.

### Fixed

- `setup` aborted against a correctly configured Immich. It validated the API key with
  `GET /users/me`, which needs `user.read` — a permission this service deliberately never
  requests. A live v3.1.0 answers **403** there for a valid key and **401** for a bogus one;
  403 now counts as valid, and the distinction is recorded in `docs/immich-api-notes.md`.
- `scripts/quickstart.sh` ran the setup container on the default bridge network, so the
  documented `http://immich-server:2283/api` could never resolve. It now joins the Immich
  network (`NETWORK=`, default `immich_default`).
- `docker-compose.test.yaml` hard-coded `container_name: immich_server`, `immich_postgres`
  and `immich_redis` — the same names Immich's own compose file uses — so the test stack
  could not start on any host already running Immich. The names are gone and the host port
  is configurable through `COMPOSE_HOST_PORT`.
- `probe_hardware_encoder` reported libva's startup banner instead of the actual failure,
  because the first five lines of ffmpeg's stderr are `libva info:` chatter and the
  component prefix carries a per-run heap address.

### Changed

- `docker-compose.yaml` pulls the published image instead of building locally, and
  deployment-specific settings belong in a gitignored `docker-compose.override.yaml`.
- `check` now delegates its hardware section to the new detection code.
- `README.md` shrank from 712 lines to a front page; nothing was lost, it moved to `docs/`.
- `config.example.yaml` is a minimal working file; the commented GPU presets moved to
  `docs/hardware.md` now that they are selected automatically.
- Every human-readable string in the repository is English.

### Removed

- `PLAN.md`. Its verified API findings live on in `docs/immich-api-notes.md`; the rest was
  superseded by the code.

## [1.0.0] - 2026-08-19

First working release, developed and verified against a live Immich v3.1.0 instance.

### Added

- Webhook-driven service: FastAPI endpoint, SQLite job store (WAL), asyncio worker and a
  trash sweeper. `POST /webhook`, `GET /healthz`, `/stats`, `/jobs`, `/jobs/{id}`,
  `POST /reprocess/{id}`.
- Ten-step pipeline: guards, download, encode, sanity gate, upload, `PUT /assets/copy`,
  explicit field and tag carry-over, versioned markers on both assets, deferred removal of
  the original.
- Typed Immich v3 client covering assets, metadata KV, tags, copy, trash and restore, with
  retries on transport errors and 5xx.
- Preset system with shell-free execution: commands are `shlex.split` at load time and
  rejected if they contain shell control operators, redirections or command substitution.
- Sanity gate: size ratio, decodability, rotation-aware display size, bit depth, HDR
  transfer, duration drift, audio stream count and capture date.
- Four-step verification chain in front of every delete: replacement present and not
  trashed, checksum equal to the uploaded bytes, `dateTimeOriginal` set, marker written.
- `delete_mode: permanent` for reclaiming space immediately, rejected at startup unless
  `trash_original: true` and `dry_run: false`.
- CLI: `serve`, `check`, `encode`, `report`, `reprocess`, `requeue`, `backfill`, `restore`.
- Configuration through `config.yaml` plus `__`-nested environment overrides, with secrets
  read from the environment only and rejected in the file.
- GPU encoding through an optional `docker-compose.gpu.yaml` overlay, with a one-frame
  hardware probe at startup and in `check`.
- Marker v2: a v1 marker without a `replacedBy` field is retried once, because the v1
  sanity gate compared stored frame sizes and rejected every rotated video.
- Test suite: unit tests with mocked HTTP plus a `live`-marked end-to-end suite against a
  full Immich v3.1.0 stack (`docker-compose.test.yaml`).

[Unreleased]: https://github.com/Navilois/immich-compressor/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/Navilois/immich-compressor/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/Navilois/immich-compressor/releases/tag/v1.0.0
