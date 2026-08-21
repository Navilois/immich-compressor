# Handover — the 1.1.0 public-release work

Everything below happened locally. **Nothing was pushed, tagged, or published to any
registry**, and no pull request was opened. The commands you still have to run yourself are
in [What is left for you](#what-is-left-for-you).

Written 2026-08-19, against `main` at the merge of `chore/phase-7-verification`.

## Your live deployment is untouched

The working tree had uncommitted changes to `docker-compose.yaml` carrying this host's real
settings. Those moved to a **gitignored `docker-compose.override.yaml`**, which compose
loads automatically, so `docker compose up -d` keeps doing exactly what it did.

`docker compose config` was captured before any change and compared again at the end of
every phase. It is **byte-identical**, including after the tracked compose file was
rewritten to pull the published image — the override pins `build: .` and
`image: immich-compressor:1.0.0`, so this host keeps running its locally built image until
you decide otherwise. The container was never stopped, restarted or recreated.

To move this host onto the published image once it exists, delete those two lines from
`docker-compose.override.yaml` and run `docker compose up -d`.

## What shipped, per phase

| Phase | Branch | What |
|---|---|---|
| 0 | `chore/phase-0-baseline` | Local settings moved into a gitignored override; `docker-compose.override.example.yaml` as the pattern for everyone else; `.claude/` ignored; baseline recorded (ruff clean, 108 tests). |
| 1 | `chore/phase-1-hygiene` | MIT `LICENSE`, `CHANGELOG.md`, `.editorconfig`, `.gitattributes`, `Makefile`, `CLAUDE.md`; `ruff format` adopted; version single-sourced from `__version__` and bumped to 1.1.0. |
| 2 | `feat/phase-2-hardware-autodetect` | `hardware.py`: render-node enumeration, sysfs ids, `ffmpeg -encoders`, `vainfo`, a one-frame confirming encode, the cgroup CPU budget, a ranked preset catalog, `hardware.mode`, `behavior.quality`, and `immich-compressor hardware [--json]`. |
| 3 | `feat/phase-3-setup` | `immich-compressor setup`; four compose files; `.env.example`; `scripts/quickstart.sh`. |
| 4 | `ci/phase-4-image-and-ci` | Multi-arch Dockerfile (amd64 Intel/AMD drivers, arm64 Mesa), CI, release, CodeQL, Dependabot. |
| 5 | `docs/phase-5-documentation` | README 712 → 220 lines; twelve `docs/` pages; generated configuration reference and JSON schema; community health files and issue templates; `PLAN.md` retired; language and link guards. |
| 6 | `feat/phase-6-adoption` | `/metrics`, social preview, launch checklist, the honest comparison in the FAQ. |
| 7 | `chore/phase-7-verification` | Live E2E against a real Immich v3.1.0, stranger review, skeptic review, this document. |

Every phase was merged into `main` with `--no-ff` after its acceptance checks passed.

## What was verified, and how

**Against a real Immich v3.1.0**, brought up as the separate compose project `immich-test`
on `127.0.0.1:12283`, torn down afterwards with `down -v`:

- the full `live` suite — three tests, all passing: the end-to-end pipeline, permanent
  delete, and the assertion that a dry run changes nothing;
- `immich-compressor setup` end to end, in the container, with the GPU passed through:
  key validation, permission probing, hardware detection, file writing, and **an actual
  workflow created** on the server;
- the permission probes, with two API keys on the same instance — one complete, one missing
  `asset.copy` — which reported exactly that one as missing, in Immich's own words;
- `POST /workflows` really does answer `"steps": []` while persisting all three steps.

**On this host's hardware** (Intel UHD 630, `0x8086:0x3e98`):

- `hevc_vaapi` selected after a real one-frame encode;
- `hevc_qsv` rejected with the actual driver error, `Error creating a MFX session: -9`;
- the cgroup budget: `--cpus 2` produces 2 encoder threads, matching what you had tuned by
  hand as `pools=2 -threads 2`.

**Both architectures**: `docker buildx build --platform linux/amd64,linux/arm64` completes,
and the arm64 image runs under emulation and reports its own hardware correctly.

**Mechanically**: 198 unit tests, ruff check and format, the English-only guard over 71
tracked files, 92 internal links and anchors, the generated-docs check, seven compose
overlay combinations, and `actionlint` over all three workflows.

## Three bugs the verification found

Each is fixed, with a regression test.

1. **`setup` aborted against every correctly configured server.** It validated the key with
   `GET /users/me`, which needs `user.read` — a permission this service deliberately never
   asks for. A real Immich answers **403** there for a *valid* key and **401** for a bogus
   one; the stub used during development answered 200 and hid it. Now 403 counts as a valid
   key, and the distinction is documented as finding 13 in `docs/immich-api-notes.md`.
2. **`scripts/quickstart.sh` could never reach Immich.** It ran the setup container on the
   default bridge, so the documented `http://immich-server:2283/api` did not resolve. It now
   joins your Immich network (`NETWORK=`, default `immich_default`) and says what to do when
   that network does not exist.
3. **`docker-compose.test.yaml` could not start on a host that runs Immich.** It hard-coded
   `container_name: immich_server`, `immich_postgres` and `immich_redis` — the same names
   Immich's own compose file uses, so on the machine anyone would develop this on it
   collided outright. The names are gone; compose derives them from the project, and the
   host port is now `COMPOSE_HOST_PORT`.

Two more were caught by tests as they were written: the `/metrics` histogram double-counted
its buckets, and `%g` formatting silently rounded byte counters (50710662 rendered as
5.07107e+07).

## What is left for you

### 1. Review, then push

```bash
git log --oneline main
git diff --stat 61dac86..main
git push origin main
```

### 2. Tag and release

```bash
git tag -a v1.1.0 -m "1.1.0"
git push origin v1.1.0
```

That starts `.github/workflows/release.yml`, which refuses to publish if the tag disagrees
with `__version__` or the CHANGELOG has no section for it, then builds both architectures,
pushes to `ghcr.io/navilois/immich-compressor` as `1.1.0` / `1.1` / `1` / `latest` with
provenance and an SBOM, and creates the GitHub release from the CHANGELOG section.

### 3. Make the package public — do not skip this

A package created by Actions is **private by default**. Every `docker pull` in the README
fails until you flip it:

**Packages → immich-compressor → Package settings → Change visibility → Public**

Then check from anywhere:

```bash
docker pull ghcr.io/navilois/immich-compressor:1
docker run --rm ghcr.io/navilois/immich-compressor:1 hardware
```

### 4. Repository metadata, social preview, announcements

All of it, with the exact commands, is in
[launch-checklist.md](launch-checklist.md). The parts only a human can do:

- upload `docs/assets/social-preview.png` under **Settings → General → Social preview**;
- set the description and topics (there is a ready-made `gh repo edit` command);
- enable **Discussions** and **private vulnerability reporting**;
- post to the Immich Discord, r/selfhosted and r/immich, in that order.

### 5. Optional, at your discretion

- **The README's example report shows this deployment's real numbers** (5 jobs, 2
  compressed, 22.9 MiB saved). They are yours and they are what makes the page credible,
  but if you would rather not publish them, replace that block in `README.md`.
- **Move this host onto the published image** by deleting `build:` and `image:` from
  `docker-compose.override.yaml`.
- **This host now has qemu-aarch64 binfmt handlers registered**, from
  `docker run --privileged --rm tonistiigi/binfmt --install arm64`, so multi-arch builds
  work locally. They vanish on reboot; `--uninstall arm64` removes them sooner.

## What was skipped, and why

- **Nothing was pushed, tagged, published or opened as a pull request**, per instruction.
  The workflows are complete and validated but have never run.
- **No `gh` on this host**, so the repository metadata could not be set. The commands are
  written out in the launch checklist instead.
- **`make` is not installed on this host.** The Makefile was validated by running
  `make -n` inside a container. Its recipes are all one-line shell commands that were also
  executed directly.
- **No NVIDIA, no AMD and no Intel Gen12+ hardware here.** Those presets and the NVIDIA
  compose overlay follow the vendors' documented setup and the ranking is unit-tested
  against captured tool output, but nobody has run them on that silicon. The support matrix
  in `docs/hardware.md` and the README both say so rather than implying parity — and the
  hardware-report issue template exists to close the gap.
- **`docker-compose.yaml` has no `healthcheck:` block.** The image already ships one, and a
  second copy would only be a second thing to keep in sync. The comment in the file says so.
- **The published-image reference is unresolvable until step 3 above.** Every documented
  `docker pull ghcr.io/navilois/immich-compressor:1` fails until the package exists and is
  public.

## The three things most likely to draw the first wave of issues

### 1. "I followed the README and `docker pull` fails."

**Cause:** the package is private, or the release has not run. This will be the first issue
if step 3 above is missed, and it makes the project look broken to everyone who tries it in
the first hour.

**Pre-emptive fix:** do step 3 immediately after the release workflow finishes, and verify
with `docker pull` from a machine that is not logged in to ghcr.io. Consider making the
first announcement only after that check passes.

### 2. "It says my GPU is not usable, but it works in Immich."

**Cause:** Immich transcodes H.264, this project encodes HEVC, and the two need different
VA entrypoints. Plenty of chips do one and not the other — this host's UHD 630 is exactly
that case, and its host driver stack reports HEVC decode only while the container's reports
encode as well. Users will read "not usable" as a bug in the detection.

**Pre-emptive fix: already applied.** The rejection reason now ends with "Immich's own
transcoding uses H.264, which is a different entrypoint — a chip can do one and not the
other, so a GPU that works for Immich can still land here", and
`docs/hardware.md#troubleshooting` has a matching row. It cost two lines and it turns the
bug report into a non-event.

### 3. "It queued my entire library and filled the disk."

**Cause:** somebody re-ran **Administration → Jobs → Extract Metadata**. That trigger fires
per extraction, not per upload, so one click queues everything. This is documented in three
places and now on the README front page, and it will still happen — it is one click, in
Immich's UI, far from any of this project's documentation.

**Pre-emptive fix:** make the service defend itself instead of relying on the reader. A
`behavior.max_queue_per_hour` (or a burst detector that logs loudly and pauses when more
than N jobs are enqueued in a short window) would turn a disk-filling incident into a
warning in the log. That is a real feature, not a doc fix, and it is the first thing worth
building for 1.2.0.
