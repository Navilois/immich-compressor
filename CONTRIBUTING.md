# Contributing

Bug reports, hardware reports and documentation fixes are all welcome. The single most
useful thing you can send is the output of `immich-compressor hardware --json` from a
machine this project has never run on — that is how the compatibility matrix gets filled in.

## Development setup

```bash
git clone https://github.com/Navilois/immich-compressor
cd immich-compressor
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

On Debian and Ubuntu this needs the `python3-venv` package; without it `venv` cannot
bootstrap pip. `make dev` runs the same two commands. `make` itself is optional — every
target is a one-line shell command you can run by hand.

```bash
make lint     # ruff, the English-only, link, workflow and version guards
make test     # the unit suite: mocked HTTP, no network, no GPU
make check    # everything CI runs
```

Install `ffmpeg`, `ffprobe`, `exiftool` and ImageMagick to unlock the encoder tests — about
a quarter of the suite skips without them, and CI runs with them installed.

## The live suite

`tests/test_e2e_live.py` drives the whole pipeline against a real Immich. It is marked
`live` and skipped unless the environment points at one:

```bash
mkdir -p testinstance
cp testinstance/example.env testinstance/.env   # set a DB_PASSWORD of your own
docker compose --env-file testinstance/.env -f docker-compose.test.yaml up -d

export E2E_IMMICH_URL=http://127.0.0.1:2283/api
export E2E_IMMICH_KEY=<api key from that instance>
make test-live
```

`docker-compose.test.yaml` brings up a complete Immich stack under the separate compose
project `immich-test`. Machine learning sits behind the `ml` profile because it costs about
2 GB of RAM.

That stack follows the floating `v3` tag, inherited from `testinstance/example.env`, so the
live suite runs against whatever Immich 3.x is current on the day — **not** against v3.1.0,
which is what `tests/fixtures/` and [docs/immich-api-notes.md](docs/immich-api-notes.md)
were captured from. Pin it in `testinstance/.env` when you need a documented behaviour
reproduced exactly:

```bash
IMMICH_VERSION=v3.1.0
```

A live failure that appears right after an upstream Immich release is therefore worth
reading as a possible upstream change before assuming this code broke.

**Never point the live suite at a real library.** It uploads throwaway assets, drives the
full pipeline including trash and restore, and cleans up after itself — on an instance that
exists for that purpose.

## What not to change

`pipeline.py`, the sanity gate in `encoder.py` and the four-step verification chain in front
of every delete are load-bearing, and were verified against a live Immich v3.1.0 instance.
Several of their apparent oddities are fixes for real, reproduced bugs — the rotation-aware
display-size comparison, the wait for metadata extraction, the explicit field and tag
carry-over after `PUT /assets/copy`. They are documented in
[docs/immich-api-notes.md](docs/immich-api-notes.md).

Refactor around them. If you must change one, the pull request has the test that proves the
new behaviour, in the same commit.

## House rules

**Safe defaults are not negotiable.** The shipped configuration is inert: `dry_run: true`,
`trash_original: false`, `delete_mode: trash`. Nothing that can delete a photo may become a
default.

**English only** in every tracked file — code, comments, docstrings, log messages, CLI help,
error text, YAML comments, docs and commit messages. `scripts/check-language.sh` enforces it
in CI. (The project's first fifteen commit *subjects* are German; those are history and stay
as they are.)

**Only verified claims in the docs.** This project's credibility rests on "measured against
a live instance". Do not write a performance number, a compatibility claim or an API
behaviour you have not seen yourself. If it is a guess, leave it out or mark it explicitly
as unverified — [docs/hardware.md](docs/hardware.md) does exactly that for the vendors
nobody here owns.

**No new runtime dependencies.** One container, one process, SQLite. Anything sixty lines of
standard library can do does not get a dependency. Python 3.12 is the floor.

**Add the test with the behaviour.** A user-facing change and its test land in the same
commit.

**Secrets never enter tracked files.** Use obvious placeholders in docs and examples.

## Generated files

`docs/configuration.md` and `docs/config.schema.json` are generated from the pydantic models
by `scripts/gen_docs.py`. Change the model (or the notes table in the generator), then:

```bash
make docs
```

CI fails if they are out of date.

## Commits and pull requests

[Conventional commits](https://www.conventionalcommits.org/), English, imperative:

```
feat(hardware): rank NVENC above QSV on machines with both
fix(config): reject a preset whose suffix has no dot
docs: explain why the VAAPI preset drops subtitle tracks
```

The type, the scope, the length and the imperative mood are checked, by `make commits` and
by CI on every pull request. Scopes are not free text: `scripts/commit-scopes.txt` lists the
ones this repository uses, and a new one is a deliberate line added to that file in the same
commit as the change that needs it. `fix(test)` and `fix(tests)` are both in the history and
were always the same scope — that is what the list prevents.

Install the hook once and the check runs before the commit exists:

```bash
make hooks
```

That matters more than it sounds: a subject that only fails in CI can be fixed only by
rewriting a branch that has already been pushed, and this project does not force-push.

Commits by Dependabot are held to the grammar but not the prose rules — `chore(deps): Bump …`
is what it writes, and rewriting that every week would cost a merge conflict per update.

One branch per topic. Say in the pull request what you changed, why, and how you checked it.
`make check` should be green before you open it.

## Releasing (maintainers)

1. Write the `Unreleased` section of `CHANGELOG.md`, and the `Unreleased` section of
   `docs/upgrading.md` if an operator has to do anything.
2. Run the chore:

```bash
python scripts/version.py set auto --check    # what it would write
python scripts/version.py set auto            # write it
```

`auto` is the version the conventional commits since the newest tag ask for — `feat` a
minor, `fix` and `perf` a patch, a `!` or a `BREAKING CHANGE:` footer a major. `next` prints
it on its own. Pass `major`, `minor`, `patch` or an explicit `X.Y.Z` to decide yourself.

The chore bumps `__version__`, dates the CHANGELOG section and opens an empty one, moves
both compare links, renames the upgrading heading to `<previous> → <new>`, and then checks
its own output with `version.py check`. It refuses to run when the `Unreleased` section is
empty, when the version does not come after the current one, or when that section already
exists.

3. Commit, tag `vX.Y.Z`, push the tag.

### Or let the workflow do all three

Run **Prepare a release** from the Actions tab (`workflow_dispatch`), with `auto`, `major`,
`minor` or `patch`. It runs the same chore, checks the result, and opens
`chore(release): X.Y.Z` as a pull request with the release notes in the body.

**It needs one repository setting**, off by default and easy to miss because nothing about
the failure names it: **Settings ▸ Actions ▸ General ▸ Workflow permissions ▸ "Allow GitHub
Actions to create and approve pull requests"**. Without it every step succeeds and the last
one fails with `GitHub Actions is not permitted to create or approve pull requests`, leaving
a correct `chore/release-X.Y.Z` branch pushed and no pull request. The workflow asks for the
setting before doing any work, but reading it needs repository admin rights that
`GITHUB_TOKEN` does not have, so under the default token the pre-flight step reports that it
could not read it and continues. What it does guarantee is the second half: the step that
opens the pull request catches its own failure and prints the command that opens it by hand
from the branch it already pushed. On a fork, this setting is the first thing to turn on.

```bash
gh api repos/OWNER/REPO/actions/permissions/workflow --jq .can_approve_pull_request_reviews
```

**Merging that pull request is the release.** `release-tag.yml` then notices a
`__version__` on `main` that nothing has tagged, tags it, and calls `release.yml`.

Two things about that pull request are worth knowing:

- **It arrives with no checks.** GitHub does not trigger `on: pull_request` for anything
  opened with `GITHUB_TOKEN`. The lint, language, link, generated-doc and version guards all
  run in the job that opens it; the unit suite is not repeated, because it passed on that
  exact tree when it was merged and dating a heading cannot change that. Closing and
  reopening the pull request is a human action and does trigger the full matrix.
- **The tag is pushed by the workflow, so it cannot trigger `release.yml` by itself** —
  GitHub does not fire `on: push` for a `GITHUB_TOKEN` push. That is why `release-tag.yml`
  calls `release.yml` through `workflow_call` instead of leaving it to a tag event that
  would never arrive.

The release workflow refuses to publish if the tag disagrees with `__version__`, if the
CHANGELOG has no section for it, if any released section has no tag, or if the tag is not
the newest version — publishing an older one would move `latest` and the major tag
backwards. It then builds the multi-arch image, pushes it to ghcr.io with provenance and an
SBOM, and creates the GitHub release from the CHANGELOG section.

[docs/maintainers/launch-checklist.md](docs/maintainers/launch-checklist.md) covers the
parts no workflow can do: repository metadata, making the published package public, the
social preview in [docs/assets/](docs/assets/README.md), and where to announce.
