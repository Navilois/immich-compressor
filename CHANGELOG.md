# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
