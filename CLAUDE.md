# CLAUDE.md

Conventions for anyone — human or agent — changing this repository.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

On Debian/Ubuntu this needs the `python3-venv` package; without it `venv` cannot bootstrap
pip. `make dev` does the same two steps. `make` itself is not required — every target is a
one-line shell command you can run by hand.

## The loop

```bash
make lint     # ruff, the English-only, link, workflow and version guards
make test     # unit suite, mocked HTTP, no network and no GPU required
make check    # everything CI runs
```

`make test-live` runs the end-to-end suite against a real Immich. It needs
`E2E_IMMICH_URL` and `E2E_IMMICH_KEY` pointing at a throwaway instance — never a real
library — plus `E2E_IMMICH_EMAIL` and `E2E_IMMICH_PASSWORD` for the two sync-stream tests,
which cannot use an API key because Immich refuses one on every `/sync` route. Without them
those two skip, so read the summary and not just the exit code.
`docker-compose.test.yaml` brings one up under the separate compose project `immich-test`.

## Rules

**Do not rewrite the pipeline.** `pipeline.py`, the sanity gate in `encoder.py` and the
four-step verification chain in front of every delete are load-bearing and were verified
against a live Immich v3.1.0 instance. Several of their apparent oddities — the
rotation-aware display-size comparison, the wait for metadata extraction, the explicit
field and tag carry-over after `PUT /assets/copy` — are fixes for real, reproduced bugs
and are documented in `docs/immich-api-notes.md`. Refactor around them. If you must change
one, a test proves the new behaviour in the same commit.

**Safe defaults are not negotiable.** The shipped configuration is inert:
`dry_run: true`, `trash_original: false`, `delete_mode: trash`. Nothing that can delete a
photo may become a default, and `delete_mode: permanent` stays rejected at startup unless
the configuration explicitly means it.

**Secrets come from the environment only.** `IMMICH__API_KEY` and `WEBHOOK__TOKEN` are
rejected in `config.yaml`. Never commit a key, and use obvious placeholders in docs.

**English only** in every tracked file: code, comments, docstrings, log messages, CLI
help, error text, YAML comments, docs and commit messages. `scripts/check-language.sh`
enforces this in CI.

**Only verified claims in the docs.** This project's credibility rests on "measured
against a live instance". Do not write a performance number, a compatibility claim or an
API behaviour you have not seen yourself or that is not already recorded here as verified.
If it is a guess, leave it out or mark it as unverified.

**No new runtime dependencies.** One container, one process, SQLite. Anything 60 lines of
standard library can do does not get a dependency. Python 3.12 is the floor.

**Add the test with the behaviour.** A user-facing change and its test land in the same
commit.

## Commits

Conventional commits, English, imperative: `feat(hardware): …`, `fix(config): …`,
`docs: …`, `chore(ci): …`. One branch per topic, merged into `main`. Scopes come from what
is already in `git log`, not from invention — `scripts/commit-scopes.txt` is that list, and
`scripts/check-commits.py` enforces it along with the type, the length and the imperative
mood. `make commits` runs it over the branch; CI runs it on every pull request.

A new scope is a deliberate line added to that file in the same commit as the change that
needs it. That is the point: `fix(test)` and `fix(tests)` are both in this history, and
they were always the same scope.

Install the hook once, and the check runs before the commit exists rather than after it has
been pushed:

```bash
make hooks
```

Atomic commits: implementation and its test together, two unrelated changes in two
commits. Agent commits carry a `Co-Authored-By:` trailer naming the model that wrote them.

## Shipping

Base branch is `main`; branches are `<type>/<slug>`, lowercase and hyphenated.

```bash
make check                                        # the gate — never push without it
git push -u origin <branch>
gh pr create --base main
gh pr checks <number> --watch                     # ten checks, all required in practice
gh pr merge <number> --merge --delete-branch      # merge commit, never squash or rebase
git switch main && git pull --ff-only origin main
```

CI runs the same things `make check` does, plus the image build, CodeQL and the unit suite
on Python 3.12, 3.13 and 3.14 — a suite that only passes on the local 3.12 is not green.
Never `--no-verify`, never force-push, and never weaken a check to get past it: a check
that is wrong is its own change.

The PR body carries what changed, why, and the test evidence — which suite ran, where, and
with what result. Anything that could not be measured against a live Immich is named as
unverified there too, not quietly asserted.

**Unreleased work is documented as unreleased.** User-visible changes go under
`## [Unreleased]` in `CHANGELOG.md`, and anything an operator has to know about goes under
`## Unreleased` in `docs/upgrading.md`. A release chore renames **both** headings — the
upgrading one was missed once and spent two releases describing shipped behaviour as
upcoming.

## Releases

Write the `Unreleased` sections first, then run the **Prepare a release** workflow
(`workflow_dispatch`, input `auto`/`major`/`minor`/`patch`). It performs the chore with
`version.py set` and opens `chore(release): X.Y.Z` as a pull request.

**Merging that pull request is the release.** `release-tag.yml` sees a `__version__` on
main that nothing has tagged, tags it, and calls `release.yml` to publish. Any other merge
to `main` still deploys nothing.

The chore itself — `python scripts/version.py set auto` — bumps `__version__`, dates the
CHANGELOG section and opens an empty one, moves both compare links, renames the upgrading
heading, rebuilds the generated docs, and checks its own output. `next` prints the derived
version on its own. Running it by hand and pushing a tag still works and is the escape
hatch; `release.yml` keeps its `on: push: tags` trigger for exactly that.

`release.yml` refuses to publish a tag that disagrees with `__version__`, that has no
CHANGELOG section, that leaves an earlier section untagged, or that is not the newest
version.

## Layout

| Path | What |
|---|---|
| `src/immich_compressor/config.py` | settings model, preset validation, fail-fast startup |
| `src/immich_compressor/hardware.py` | device detection, preset catalog, CPU budget |
| `src/immich_compressor/models.py` | webhook payload and REST DTOs |
| `src/immich_compressor/api.py` | typed async Immich client |
| `src/immich_compressor/store.py` | SQLite job store (WAL), plus the backfill inventory |
| `src/immich_compressor/metrics.py` | the Prometheus exposition behind `/metrics` |
| `src/immich_compressor/encoder.py` | preset execution, exiftool, sanity gate |
| `src/immich_compressor/pipeline.py` | the ten steps, worker loop, trash sweeper |
| `src/immich_compressor/backfill.py` | library scan, candidate inventory, queue run |
| `src/immich_compressor/server.py` | FastAPI endpoints |
| `src/immich_compressor/setup_cmd.py` | the guided `setup` command |
| `docs/` | everything the README links to; `configuration.md` is generated |
| `scripts/gen_docs.py` | regenerates `docs/configuration.md` and `docs/config.schema.json` |

## Version

`__version__` in `src/immich_compressor/__init__.py` is the single source of truth. The
package version, `--version`, the OpenAPI document and the image label all read from it.
