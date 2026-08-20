# Prompt — turn `immich-compressor` into a public repo people actually adopt

Paste this whole file into a fresh Claude Code session in `/home/b-r-a-i-n/immich-compressor`,
or run: `Read docs/public-release.md and execute it end to end.`

---

## Mission

`immich-compressor` works, is well engineered and is verified against a live Immich v3.1.0
instance — but today it is a personal deployment that happens to live in git. Refactor it,
iteratively and with green checks at every step, into a project a stranger with an Immich
server can install in five minutes, trust with their photo library, and adapt to their
hardware without reading 700 lines of README.

Optimise every decision for one measurable outcome: **a self-hosted user finds this repo,
understands it in 30 seconds, has it running in 5 minutes, and stars it.**

Work autonomously to completion. Do not ask questions. Where a decision is genuinely open,
pick the option that best serves the outcome above, write one sentence of rationale into the
commit message or an ADR under `docs/adr/`, and move on.

## The five levers (rank every trade-off against these)

1. **Zero-friction install.** Prebuilt multi-arch image, copy-paste compose, one setup command.
   Anything that forces a user to read source, edit ffmpeg flags or compute a group id is a bug.
2. **Trust.** This tool deletes originals of irreplaceable photos. The safety story — dry run by
   default, verification chain, restore path, "we never touch X" — must be visible before the
   install instructions, not after.
3. **Hardware, handled for the user.** Detection and preset selection happen automatically, are
   explainable on demand, and degrade to CPU without failing. See Phase 2 — this is the single
   biggest differentiator versus "run ffmpeg in a cron job".
4. **Docs that answer the next question.** A short README that sells and gets you running, plus
   a `docs/` tree that goes deep. Nothing unverified, nothing German, no dead anchors.
5. **Signals of a maintained project.** CI badges, releases, changelog, licence, issue templates,
   security policy, fast paths for contributors.

## Ground truth about this repo (verified 2026-08-19 — re-check, do not re-derive)

- Python 3.12+, FastAPI + uvicorn, pydantic-settings, aiosqlite, httpx. `src/immich_compressor/`:
  `__main__.py` (CLI), `config.py`, `models.py`, `api.py`, `store.py`, `encoder.py`, `pipeline.py`,
  `server.py`. ~2.6k LOC source, ~2.5k LOC tests, 8 test modules, one of them `live`-marked.
- The pipeline is **load-bearing and empirically verified** (rotation/display-size handling, the
  sanity gate, the sidecar/tag findings, the four-step delete verification chain). Do not rewrite
  it, do not "clean up" its semantics. Refactor around it; if you must touch it, a test proves it.
- `encoder.py::probe_hardware_encoder()` already does a one-frame probe of a GPU encoder, and
  `server.py::_warn_about_unusable_hardware()` calls it at startup. Build hardware autodetection
  on that foothold instead of a new mechanism.
- Config: `config.example.yaml` (176 lines, half of it commented GPU presets), loaded via YAML
  source ranked *below* env vars, `__` as nesting delimiter, secrets env-only and rejected in YAML.
- Deployment: `docker-compose.yaml` (builds locally, no published image), `docker-compose.gpu.yaml`
  (Intel `/dev/dri` + `RENDER_GID` overlay), `docker-compose.test.yaml` (full Immich v3.1.0 stack,
  compose project `immich-test`, ML behind the `ml` profile).
- Missing entirely: LICENCE, CI, `.github/`, CHANGELOG, CONTRIBUTING, SECURITY, issue/PR templates,
  published image, `.env.example`, `CLAUDE.md`.
- `README.md` is 712 lines / 36 KB — the entire project knowledge in one scroll. It is excellent
  raw material and a terrible front page.
- `PLAN.md` (302 lines) is the original German design doc. Its verified-API section is genuinely
  valuable; the rest is superseded by the code.
- Remote is `git@github.com:Navilois/immich-compressor.git`, branch `main`, no tags, 15 commits,
  German commit subjects in the history (leave history alone; write English from now on).
- Host: 8 cores, `/dev/dri/renderD128` present, `render` gid 992. `docker` + `buildx` available.
  **No `gh`, no `uv`, no host `ffmpeg`; host python is 3.13.** Create `.venv` with
  `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`. If PyPI is unreachable, run the
  suite inside the built image instead and say so in the final report.

## Hard constraints (violating any of these fails the task)

- **Do not break the running production deployment.** The working tree has uncommitted changes to
  `docker-compose.yaml` that are the user's live settings (`restart: unless-stopped`, `DRY_RUN=false`,
  `TRASH_ORIGINAL=true`, `DELETE_MODE=permanent`, `RETENTION_DAYS=0`, `cpus: 2`). Before anything
  else, snapshot `docker compose config` to a scratch file, move those local values into a
  **gitignored `docker-compose.override.yaml`** (compose auto-loads it, so the user's `docker compose
  up -d` keeps working unchanged), restore the tracked file to safe public defaults, and diff
  `docker compose config` again. Any remaining difference must be intentional and listed in the
  final report.
- **Never run `docker compose up/down/restart/stop` against the user's live stack**, and never touch
  their Immich server, their `.env`, their `config.yaml`, or `testinstance/` contents. `docker
  compose config`, `build`, and read-only commands are fine.
- **Public defaults stay inert**: `dry_run: true`, `trash_original: false`, `delete_mode: trash`.
  Never ship a default that can delete a user's photo.
- **Secrets never enter tracked files.** `.env`, API keys and tokens stay out of git and out of docs
  and examples (use obvious placeholders). Do not print the contents of `.env` into any file.
- **English only** for every human-readable string in tracked files: docs, comments, docstrings, log
  messages, CLI help, error text, YAML comments, commit messages from here on. Past commit subjects
  stay as they are.
- **No new runtime dependencies** beyond what `pyproject.toml` already declares, unless a phase below
  explicitly allows it. Python 3.12 floor stays. One container, one process, SQLite.
- **Never invent evidence.** This project's credibility comes from "measured against a live
  instance". Every performance number, compatibility claim or API behaviour you write into docs must
  be something you verified in this session or that already exists in the repo as a verified claim.
  If you cannot verify it, either leave it out or mark it explicitly as unverified.
- **Do not push, do not force-push, do not create PRs, do not `gh repo edit`, do not publish an
  image to any registry.** Commit locally on branches and merge to `main` locally. Publishing is the
  user's call; leave them a one-page handover with the exact commands.
- Existing configs must keep working. A user upgrading from 1.0.0 with explicit `presets:` in
  `config.yaml` sees identical behaviour.

## Operating rules

- Work in phases, in order. One branch per phase (`feat/…`, `docs/…`, `ci/…`), small conventional
  commits in English, merge to `main` when the phase's acceptance checks pass.
- **Acceptance gate after every phase** (all must pass before the merge):
  `.venv/bin/python -m ruff check . && .venv/bin/python -m ruff format --check .` (add the formatter
  config in Phase 1 if you adopt it), `.venv/bin/python -m pytest -m 'not live' -q`,
  `docker compose config -q` for every compose file and overlay combination, and `docker build .`
  when the Dockerfile or packaging changed.
- Keep a running task list and update it as you go. If a phase turns out to be bigger than expected,
  split it — never silently drop scope. Anything you deliberately skip goes into the final report
  with a reason.
- Prefer deleting to adding. Prefer generating docs from the code over hand-maintaining both. Prefer
  measured defaults over configurable knobs, and a knob over a fork.
- When you add a user-facing behaviour, add the test that proves it in the same commit.
- Re-read what you wrote as a stranger would, not as its author.

---

## Phase 0 — Baseline and safety net

**Goal:** know the starting state, and make the user's live deployment independent of the repo's
public defaults.

- Snapshot `docker compose config > /tmp/…/compose-before.yaml` (scratchpad, not the repo).
- Create `docker-compose.override.yaml` carrying every local-only value from the working tree diff,
  with an English header explaining what it is and that compose loads it automatically. Add it to
  `.gitignore` next to `config.yaml` and `.env`. Restore `docker-compose.yaml` to the safe tracked
  defaults.
- Add `docker-compose.override.example.yaml` (tracked) showing the same pattern for new users —
  this is how everyone should customise, instead of editing the tracked file.
- Add `.claude/` to `.gitignore` — this prompt and any session scratch stay out of the public repo.
  (The tracked contributor-facing `CLAUDE.md` in Phase 1 is a different file and does belong in git.)
- Create `.venv`, install `-e '.[dev]'`, run ruff + `pytest -m 'not live'`, record the baseline.
- Confirm `docker compose config` is byte-identical to the snapshot except for intended changes.

**Acceptance:** baseline suite green; effective compose config for the live service unchanged;
nothing secret newly tracked.

## Phase 1 — Package hygiene

**Goal:** the boring foundation everything else hangs off.

- `LICENSE`: MIT, `Copyright (c) 2026 Alois Krichmayr` (take the name from `git config user.name`).
  Add the SPDX identifier to `pyproject.toml` and the OCI labels.
- `CHANGELOG.md` in Keep-a-Changelog format. Reconstruct 1.0.0 from the git history, in English;
  everything you add in this task lands under a new `1.1.0` entry.
- Bump the version to `1.1.0` in `pyproject.toml` and anywhere it is duplicated (compose image tag);
  add a single source of truth (`__version__` in `__init__.py`, read by the CLI's `--version`).
- Add `.editorconfig`, `.gitattributes` (LF, `linguist-*` where useful), `ruff format` config, and a
  short `Makefile` (`make dev lint test image docs`) so contributors have one obvious entry point.
- Add `CLAUDE.md`: how to set up the venv, run lint/tests/live tests, the repo's conventions
  (English only, safe defaults, verified-claims-only docs, don't rewrite the pipeline).

**Acceptance:** `make lint test` green from a clean checkout; `immich-compressor --version` prints
1.1.0.

## Phase 2 — Hardware autodetection (the differentiator)

**Goal:** a user with any common homelab box gets the best encoder their machine can actually run,
without configuring anything — and can find out exactly why, in one command.

- New module `src/immich_compressor/hardware.py`:
  - Enumerate DRM render nodes (`/dev/dri/renderD*`), read vendor/device ids from
    `/sys/class/drm/*/device/{vendor,device}` to distinguish Intel / AMD / other, and detect NVIDIA
    via the presence of the nvidia devices or `nvidia-smi`.
  - Ask the tools, do not guess from tables: `ffmpeg -hide_banner -encoders` for what the binary
    supports, `vainfo --display drm --device <node>` for VA entrypoints, then **confirm with the
    existing one-frame `probe_hardware_encoder()`** — an encoder counts as available only if a real
    one-frame encode succeeded. This is what makes the Gen9–11 vs Gen12+ QSV/VAAPI split
    self-resolving instead of documentation the user has to apply by hand.
  - Detect the container's *effective* CPU budget from cgroup v2 (`/sys/fs/cgroup/cpu.max`) with a
    `nproc` fallback, plus the memory limit. This finally fixes the documented x265 bug (it sizes its
    pool from host cores and ignores the cgroup limit) automatically: derive `pools`/`-threads` and
    `concurrency` from the real budget.
  - Return a structured `HardwareReport` (dataclass/pydantic), including *rejected* candidates with
    the reason each one failed — the reason strings are a documentation feature, write them well.
- Built-in ranked preset catalog **in code** (not YAML): `hevc_qsv`, `hevc_vaapi` (Intel + AMD),
  `hevc_nvenc`, `libx265` CPU fallback, and the stills preset. Each entry is a template rendered with
  the resolved render node, quality number and thread budget.
- Config: `hardware.mode: auto` (new default) | `cpu` | `qsv` | `vaapi` | `nvenc`, plus
  `hardware.render_node: auto`. Resolution order: explicit `presets:` in config always wins →
  `hardware.mode` if pinned → autodetect → CPU. Log the choice at startup in one clear line, and log
  *why* the better candidates were rejected at INFO.
- `behavior.quality: balanced` (default) | `higher` | `smaller`, mapped per encoder to the right
  CRF/`-global_quality`/`-cq` number, so users tune quality without knowing ffmpeg. Raw presets
  remain the escape hatch, and `max_ratio` still gates the result.
- CLI: `immich-compressor hardware` prints the full report — detected devices, chosen preset per
  asset type, rejected candidates with reasons, the derived CPU budget, the exact YAML to paste if
  the user wants to pin the choice, and the `ENCODER=… scripts/calibrate.sh …` line for their
  hardware. Add `--json`. Fold the hardware section of `check` into it and have `check` call it.
- Startup must never fail because a GPU disappeared: fall back to CPU, log a warning, keep serving.
- Tests: unit-test the detection and ranking against captured fixture output (`vainfo`,
  `ffmpeg -encoders`, `cpu.max`, sysfs ids) for at least Intel Gen9-11, Intel Gen12+, AMD, NVIDIA and
  a headless CPU-only box. No test may require a GPU.

**Acceptance:** on this host, `immich-compressor hardware` correctly identifies the render node and
either selects a GPU preset that passes the one-frame probe or explains precisely why it did not;
a CPU-only container still starts and encodes; full suite green.

## Phase 3 — Five-minute setup

**Goal:** install without editing YAML by hand.

- `immich-compressor setup` (and `--non-interactive` for scripted use): validates the API key against
  the server, reports which of the required permissions are missing by name, runs hardware detection,
  writes a `config.yaml` tuned to the box, generates a webhook token, writes `.env` (0600, never
  tracked), and — when the key or a supplied session token allows `workflow.create` — creates the
  Immich workflow itself. Otherwise it prints the exact workflow JSON and the curl command.
  It must be re-runnable and must never overwrite an existing file without saying so.
- The setup writes the GPU wiring it detected into `.env`: the resolved `RENDER_GID`
  (from the render node's group, not hard-coded) and `COMPOSE_FILE=docker-compose.yaml:docker-compose.gpu.yaml`
  so plain `docker compose up -d` keeps doing the right thing afterwards.
- Compose files, public-facing:
  - `docker-compose.yaml`: `image: ghcr.io/navilois/immich-compressor:1`, no `build:`, safe defaults,
    healthcheck, resource limits with a comment on how to size them, no published host port.
  - `docker-compose.build.yaml`: the `build: .` overlay for contributors.
  - `docker-compose.gpu.yaml`: `/dev/dri` passthrough (Intel **and** AMD), keeping the existing
    excellent explanation of why it is not in the base file.
  - `docker-compose.gpu-nvidia.yaml`: NVIDIA runtime / device reservations.
- `.env.example` with every supported variable, commented, secrets as placeholders.
- `scripts/quickstart.sh` (tracked, documented, **not** a `curl | bash`): pull image, run setup,
  print next steps.

**Acceptance:** following only the README quickstart in a scratch directory produces a running
container against a stubbed or the test Immich instance; `docker compose config -q` passes for base,
+build, +gpu and +gpu-nvidia; setup twice in a row is safe.

## Phase 4 — Image and CI/CD

**Goal:** a published, trustworthy artefact and visible green checks.

- Dockerfile: multi-arch `linux/amd64` + `linux/arm64` via `TARGETARCH` (the Intel non-free driver
  layer is amd64-only; arm64 gets the mesa VA drivers instead), OCI labels (source, licence,
  description, version), build cache friendliness, keep the non-root user and healthcheck. Document
  per-arch hardware support honestly.
- `.github/workflows/ci.yml`: ruff (check + format), pytest on 3.12 and 3.13, compose config
  validation, the language guard from Phase 5, and a single-arch image build on PRs.
- `.github/workflows/release.yml`: on a `v*` tag — multi-arch buildx to `ghcr.io`, tags
  `X.Y.Z` / `X.Y` / `X` / `latest`, provenance + SBOM attestations, and a GitHub Release whose body
  is the CHANGELOG section. Uses `GITHUB_TOKEN` only.
- `.github/workflows/codeql.yml` (python) and `.github/dependabot.yml` (pip, docker, actions).
- Everything must be runnable but **not triggered by you**: no pushes, no tags, no registry writes.
  Validate workflow YAML locally (`actionlint` if fetchable, otherwise a strict YAML parse plus a
  careful read).

**Acceptance:** workflows parse and are internally consistent (job names, permissions blocks, least
privilege, concurrency groups); `docker buildx build --platform linux/amd64,linux/arm64` succeeds
locally without pushing.

## Phase 5 — Documentation rebuild

**Goal:** the README sells and starts you; `docs/` answers everything else. Nothing is lost from the
current README — it is redistributed.

- `README.md`, target ≤ 250 lines:
  1. One-sentence pitch + badge row (CI, release, licence, image, Immich version compatibility).
  2. What it does, in a 6-line diagram or the existing flow block.
  3. **"Is this safe?"** — dry-run default, the verification chain, what it never touches, how to
     restore. Before the install section, deliberately.
  4. **Quickstart**: pull, `setup`, create the workflow, watch a dry run, go live. Copy-pasteable,
     no placeholders that are not obviously placeholders.
  5. Hardware support matrix (vendor × arch × preset × how it is detected).
  6. A real `report`/`/stats` output block — measured, not invented.
  7. Feature list, then links into `docs/`.
- `docs/`: `quickstart.md`, `installation.md`, `configuration.md` (**generated** from the pydantic
  models by `scripts/gen_docs.py`, with a `--check` mode wired into CI), `hardware.md` (the GPU
  matrix, detection, calibration, the vainfo/QSV/VAAPI troubleshooting that exists today),
  `workflow-setup.md`, `safety.md` (going live in four stages, the delete modes, rollback),
  `operations.md` (CLI, endpoints, job states, backfill/requeue, the metadata-extraction bulk-trigger
  warning), `troubleshooting.md`, `architecture.md` (pipeline steps, idempotency, state machine),
  `immich-api-notes.md` (the "verified API behaviour" + "where the plan was wrong" tables — this is
  the most linkable content in the repo, keep it prominent and dated), `faq.md`, `upgrading.md`.
- Generate `docs/config.schema.json` from the settings model and add the
  `# yaml-language-server: $schema=` modeline to `config.example.yaml`, so editors autocomplete and
  validate the config. Slim `config.example.yaml` to a *minimal* working file; the commented GPU
  presets move to `docs/hardware.md` now that autodetection handles them.
- Community health: `CONTRIBUTING.md` (dev setup, tests incl. the live suite, conventional commits,
  English-only rule, what not to change in the pipeline), `CODE_OF_CONDUCT.md` (Contributor Covenant
  2.1), `SECURITY.md` (supported versions, private reporting, the threat model: webhook secret, API
  key scope, no shell, non-root), `.github/ISSUE_TEMPLATE/` with `bug_report.yml`,
  `feature_request.yml`, `hardware_report.yml` (asks for `immich-compressor hardware --json` —
  turns user reports into a compatibility database) and `config.yml` links,
  `.github/pull_request_template.md`.
- **Language sweep:** translate/retire `PLAN.md` (fold its verified facts into
  `docs/immich-api-notes.md`, delete the rest), translate every German comment (`docker-compose.yaml`,
  anywhere else), and add `scripts/check-language.sh` — a grep guard for German stopwords across
  tracked text files — wired into CI.
- Every internal link and anchor must resolve. Verify mechanically, not by eye.

**Acceptance:** link check passes; `scripts/check-language.sh` passes; `gen_docs.py --check` passes;
README reads well to someone who has never seen the project (do an explicit stranger-review pass and
fix what you trip over).

## Phase 6 — Adoption polish

- `/metrics` in Prometheus text format (hand-rolled, no new dependency): jobs by state, bytes
  saved, skip reasons, encode duration histogram-ish counters. Homelab users build dashboards from
  this and it costs ~60 lines. Document it.
- A `docs/assets/` social-preview SVG/PNG and a short note that it must be uploaded in repo settings.
- `docs/maintainers/launch-checklist.md`: repo description and topics (`immich`, `self-hosted`,
  `ffmpeg`, `transcoding`, `hevc`, `qsv`, `vaapi`, `nvenc`, `docker`, `homelab`, `photos`), the
  exact `gh` commands to set them, release steps, where to announce (Immich Discord community
  projects, r/selfhosted, awesome-selfhosted / awesome-immich PRs), and what to answer to the first
  ten issues.
- A `docs/comparison.md`-style paragraph (inside the README or FAQ) that honestly positions this
  against "just run ffmpeg" and against Immich's own transcoding — why out-of-band recompression of
  *originals* is a different job.

## Phase 7 — Verification and handover

- Full suite, ruff, format check, `docker build` for both arches, all compose combinations.
- Run the live E2E suite if it can be brought up safely: separate compose project (`immich-test`),
  its own network, `testinstance/.env`, never the production stack. If it cannot run, say so
  explicitly in the report instead of claiming coverage.
- Stranger review: follow your own README top to bottom in a scratch directory and fix every snag.
- Skeptic review: read the repo as a self-hosted user who has been burned by a tool that ate their
  photos. Every objection they raise must have an answer in the docs.
- Confirm the user's live deployment is still intact: `docker compose config` matches the Phase 0
  snapshot except for the documented intentional differences.
- Write `HANDOVER.md` (gitignored or in `docs/maintainers/`): what changed, per phase; what was
  skipped and why; the exact commands the user runs to push, tag `v1.1.0`, publish the first image
  and set the repo metadata; and anything that needs a human (social preview upload, repo
  description, announcement posts).

## Definition of done

- [ ] A stranger can go from zero to a running dry-run compressor with the README alone, in ≤ 5 minutes.
- [ ] No hand-editing of ffmpeg commands is needed on Intel, AMD, NVIDIA or CPU-only hosts.
- [ ] `immich-compressor hardware` explains the choice it made and every choice it rejected.
- [ ] Public defaults cannot delete a photo; the safety story is on the front page.
- [ ] README ≤ 250 lines; `docs/` covers the rest with no dead links and no lost content.
- [ ] LICENCE, CHANGELOG, CONTRIBUTING, CODE_OF_CONDUCT, SECURITY, issue/PR templates present.
- [ ] CI, release and CodeQL workflows exist, are least-privileged, and parse cleanly.
- [ ] Multi-arch image builds locally; nothing was pushed anywhere.
- [ ] Every human-readable string in tracked files is English.
- [ ] Existing 1.0.0 configs still work; upgrade path documented.
- [ ] Ruff, format, and the full non-live test suite are green; new behaviour has tests.
- [ ] The user's live deployment still runs with its own settings, via a gitignored override.

## Anti-goals

No web UI. No new services, no Postgres, no Redis, no message queue. No telemetry or phone-home,
and say so in the README. No `curl | bash` installer. No rewrite of the pipeline, the sanity gate or
the delete verification chain. No support for Immich < 3.0. No marketing claims you did not measure.
No dependency added for something 60 lines of stdlib can do.

## Final report

End with a compact report: what shipped per phase, the commits/branches, what you skipped and why,
every command the user still has to run themselves, and the three things most likely to draw the
first wave of issues — with your suggested pre-emptive fix for each.
