---
name: run-immich-compressor
description: Build, run and drive immich-compressor. Use when asked to start or serve the app, launch it against a real Immich, fire a webhook, run the pipeline end to end, exercise the encoder, run the live E2E suite, or confirm a change works in the running service rather than only in tests.
---

A headless webhook service: Immich fires `POST /webhook`, the service downloads the
original, recompresses it, checks it nine ways, uploads the replacement and — only if
asked — removes the original. There is **no UI**, so the handle is
`.claude/skills/run-immich-compressor/driver.py`: it brings up a throwaway Immich in
Docker, starts `immich-compressor serve` against it, uploads a real clip, fires the
webhook and polls the job to a terminal state.

All paths are relative to the repository root.

## Prerequisites

One script installs the media toolchain and the venv. It is idempotent, and you **will**
have to re-run it: `/opt` and `/usr/local/bin` do not survive a container recreate here.

```bash
.claude/skills/run-immich-compressor/setup-host.sh
```

It installs `ffmpeg` from apt, ImageMagick **7** from the upstream AppImage into
`/opt/imagemagick7`, and **exiftool 13.x** from the upstream tarball into `/opt/exiftool`,
both behind wrappers in `/usr/local/bin`. Ubuntu's own packages are not good enough and
the reasons are in the script's header — in short, Ubuntu 24.04 has no `magick` binary at
all, and its exiftool 12.76 fails exactly one test. Docker is also required, for the
throwaway Immich.

## Run (agent path)

From cold, the whole thing — Immich up, service up, one asset through the pipeline:

```bash
.venv/bin/python .claude/skills/run-immich-compressor/driver.py all
```

Measured at **55 seconds** on 2026-08-27 from a purged state — no `testinstance/.env`, an
empty database, no admin — with the Immich images already in the local cache; the very
first run also pulls them. It ends with `PASS:` and the job JSON.

The steps are separately callable, and every one of them is idempotent:

| Command | What it does |
|---|---|
| `up` | `docker compose -f docker-compose.test.yaml up -d`, wait for `/server/ping`, create the admin, mint an API key into `testinstance/driver/env.json` |
| `serve` | start `immich-compressor serve` in the background on `127.0.0.1:18080`, wait for `/healthz` |
| `smoke` | build a fat MPEG-4 clip, upload it, wait for metadata extraction, `POST /webhook`, poll `/jobs/{id}`, assert the replacement is smaller |
| `api` | dump `/healthz`, `/stats`, `/jobs` and the first 20 `/metrics` samples |
| `thumbnail` | save Immich's preview of the newest replacement to a JPEG and print its geometry |
| `live` | the project's `live`-marked pytest suite, all four `E2E_*` variables wired in |
| `cli ARGS...` | any `immich-compressor` subcommand with the live environment wired in |
| `logs` / `stop` / `status` / `down` | tail the service log, stop it, show what is up, tear it all down |

Useful flags — they work before or after the command:

```bash
.venv/bin/python .claude/skills/run-immich-compressor/driver.py smoke --dry-run
```

`--dry-run` runs the pipeline in `dry_run` mode: the job ends `skipped / dry_run` and
nothing is uploaded. `--trash` additionally removes the original after the four-step
verification chain — only ever against the throwaway instance. `--service-port N` moves
the listener, `--port N` moves Immich.

State lives in `testinstance/driver/` (already gitignored): `env.json`, `serve.log`,
`serve.pid`, `state.db`, the clip and any thumbnails.

Tear down when finished. `--purge` also deletes Immich's library and Postgres data, so the
next `up` starts from an empty library:

```bash
.venv/bin/python .claude/skills/run-immich-compressor/driver.py down --purge
```

### Direct invocation — no Immich, no service

Most changes here land in `encoder.py`, `pipeline.py` or `shim.py`. The encoder is
reachable on its own, and this is the fastest loop for anything touching presets, the
sanity gate or metadata carry-over. It runs a real ffmpeg encode and prints both probes,
the metadata diff and the gate verdict:

```bash
env IMMICH__API_KEY=dummy WEBHOOK__TOKEN=dummy .venv/bin/immich-compressor encode /path/to/clip.mp4
```

Add `--type IMAGE` for the stills path. The command is documented as an offline dry run
and it is — but it still builds the full `Settings` object first, so both secrets have to
be present in the environment or it exits with `configuration error:` before touching the
file. Any placeholder will do.

`immich-compressor hardware` needs the same two variables and runs a real one-frame encode
per candidate encoder before reporting which one it picked and why it rejected the others.

## Run (human path)

`docker compose up -d` after `./scripts/quickstart.sh` is the operator path from the
README. It expects an existing Immich stack on a shared docker network and a
`config.yaml`, so it is not the way to drive the app from a cold checkout here — use the
driver.

## Test

```bash
.venv/bin/python -m pytest -m 'not live' -q -rs
```

**690 passed, 5 deselected** on 2026-08-27 with the toolchain above. Keep `-rs`: the
encoder tests skip themselves when ffmpeg, ImageMagick or exiftool is missing, and a bare
`-q` reports 53 of those skips as a green summary line.

The live suite needs a real Immich, which the driver already has:

```bash
.venv/bin/python .claude/skills/run-immich-compressor/driver.py live
```

**5 passed, 690 deselected**, no skips — including the two `/sync/stream` tests, which no
API key can open and which therefore skip on most setups. The driver's admin account owns
the key it mints, so it can supply the password those two need.

`make check` is the full gate and needs `make`, which this container does not have; run
the pieces by hand (`ruff check .`, `ruff format --check .`, `./scripts/check-language.sh`,
`python scripts/check-links.py`, `python scripts/gen_docs.py --check`).

## Gotchas

- **A smoke run looks hung for five minutes.** `behavior.initial_delay_seconds` ships as
  `300`: a queued job is not claimed by a worker until then. The driver overrides it to
  `0` and prints that it did. `--initial-delay 300` restores the shipped timing.
- **`--trash` on its own removes nothing you can see.** `behavior.retention_days` ships as
  `7`, so the job parks in state `pending_delete` with `delete_after` a week out and
  `original_freed_at` still `null`. The driver overrides it to `0`, which trashes the
  original inline and lands the job on `done` with `isTrashed=true` on the source.
- **`pending_delete` is where a poller hangs.** It is a resting state, not a transient
  one. The states that mean "will not move on its own" are `done`, `skipped`, `failed`
  and `pending_delete`; `uploaded` and `linked` are mid-pipeline.
- **Port 8080 is already taken in this container** by an unrelated stack, and uvicorn
  logs `Application startup complete` *before* it fails to bind — so the failure reads
  like a success. The driver checks the port first and defaults to `18080`.
- **Everything after `cli` (or `thumbnail`, or `live`) goes to the subprocess**, so put
  driver flags before those words. For every other command they work in either position.
- **Never pipe the driver through `tail`** for a long run — the pipe buffers and a
  five-minute run produces no output at all until it ends. Let it print, or use `logs`.
- **Immich deduplicates by checksum.** ffmpeg's synthetic sources are deterministic, so
  two identical clips upload as `status: "duplicate"` and you then drive an asset you did
  not create. The driver's clip carries a random noise overlay and a random audio
  frequency for exactly this reason.
- **No API key can open Immich's `/sync` routes** — every one answers
  `403 {"message": "Sync endpoints cannot be used with API keys"}` however the key is
  scoped. That is why the live suite wants an email and password too.
- **The compose project is `immich-test` and sets no `container_name`,** so it coexists
  with a real Immich on the same host. Do not point the driver at a real library: it
  uploads, and with `--trash` it deletes.
- **`.claude/` is gitignored in this repo** (deliberately — "never part of the public
  repo"), so this skill and its driver are local-only. `ruff` honours `.gitignore`, so
  `ruff check .` does not lint `driver.py` and the project's lint stays green. To track
  the skill you would need a `!.claude/skills/` negation in `.gitignore` *and* a
  `per-file-ignores` entry for `T20` in `pyproject.toml`, because the driver prints.
- **Bringing the instance up once breaks a unit test, permanently.** The compose file
  bind-mounts `testinstance/library` and `testinstance/postgres`, and the containers
  create them as `root` and as uid `999` mode `700`. `test_version.py` copies the working
  tree with `shutil.copytree`, so from then on
  `test_the_chore_leaves_a_tree_that_passes_the_checks_ci_runs` fails with
  `[Errno 13] Permission denied: '.../testinstance/postgres'` — reproduced on 2026-08-27.
  `driver.py down --purge` removes them (with `sudo`, which is why it needs it).
  `22b2aa3 fix(tests): exclude testinstance from the version chore tree copy` adds
  `testinstance` to that test's `ignore_patterns`; on a checkout without it, purge before
  running the unit suite.
- **`/opt` and `/usr/local/bin` are wiped by a container recreate.** Only `~/workspace`,
  `~/.ssh`, `~/.claude` and `/var/lib/docker` survive. Re-run `setup-host.sh`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `configuration error: IMMICH__API_KEY is not set` | Every subcommand builds `Settings` first, `encode` and `hardware` included. Set `IMMICH__API_KEY` and `WEBHOOK__TOKEN` to anything, or go through `driver.py cli`. |
| `[Errno 98] error while attempting to bind on address ('127.0.0.1', 8080)` | Another stack owns 8080. Use the driver's default `18080`, or `--service-port N`. |
| `magick: no decode delegate for this image format 'magick'` | The `/usr/local/bin/magick` wrapper is passing `magick` as an argument. `AppRun` only dispatches on `$ARGV0` when `$APPIMAGE` is set; the wrapper must be `exec /opt/imagemagick7/AppRun "$@"`. |
| `AssertionError: Warning: Tag 'XMP-GCamera:MotionPhoto' is not defined` | Ubuntu's exiftool 12.76 is on `PATH`. It knows the `XMP-GCamera` group but not that tag. Install 13.x — `setup-host.sh` does. |
| 53 encoder tests skip and the suite is still green | ImageMagick 7 or exiftool is missing. Run `setup-host.sh`, and keep `-rs` on pytest. |
| `driver.py up` hangs on `waiting for http://127.0.0.1:2283/api` | First start migrates the database. It polls for five minutes; `docker compose --env-file testinstance/.env -f docker-compose.test.yaml logs immich-server` shows progress. |
| `smoke` fails with `Immich called the upload a duplicate` | A previous identical clip is still in the library. `down --purge` and start over. |
| Job sits in `queued` and never moves | Either the five-minute `initial_delay_seconds`, or the surge breaker latched. `driver.py cli resume --apply` clears the latch; `/healthz` reports `paused`. |
| `driver.py serve` exits immediately | It prints the last 3 kB of `testinstance/driver/serve.log` to stderr when that happens. Read it — a bad `IMMICH__BASE_URL` and a refused API key both surface there. |
| `shutil.Error: [Errno 13] Permission denied: '.../testinstance/postgres'` in `test_version.py` | The test instance has been up and left root-owned bind mounts behind. `driver.py down --purge`. |
| `docker compose ... env file testinstance/.env not found` | Only `example.env` is tracked. `driver.py up` copies it for you; by hand it is `cp testinstance/example.env testinstance/.env`. |
