# Launch checklist

Everything a human still has to do, in the order it makes sense to do it. Nothing here can
be automated from inside the repository, which is why it is written down.

Done so far, so nobody repeats it: the **repository is public** since 2026-08-22, the
**ghcr.io package is public**, and the newest release is **1.3.1**. **All of section 1 is
done** — description, homepage, fourteen topics, Discussions and private vulnerability
reporting were all confirmed set on 2026-08-24 with the probes below. The only item nobody
can check from a terminal is the social preview, because GitHub exposes no API for it.

Section 2 is the one that has changed since this page was written: releases now go through
the **Prepare a release** workflow, and the hand-rolled tag is the escape hatch.

## 0. Make the repository public

The one that blocks everything else, and it blocks more than it looks like:

- the quickstart starts from `git clone`, so a stranger cannot follow a single documented
  step while the repository is closed — the image being public does not help;
- **provenance attestation is refused on a user-owned private repository.** The release
  workflow's `image` job then fails *after* pushing the image, which skips the dependent
  `release` job — leaving a published image, a tag, and no GitHub release. This happened to
  both 1.1.1 and 1.2.0, and each had to be finished by hand with
  `gh release create <tag> --notes-file <(./scripts/changelog-section.sh <tag>) --verify-tag`;
- **CodeQL's SARIF upload is refused** for the same reason, so the security workflow is red
  on every push and pull request while telling you nothing.

Verify it rather than trusting the settings page — the same probe answers before and after:

```bash
GIT_TERMINAL_PROMPT=0 git ls-remote https://github.com/Navilois/immich-compressor
```

A private repository asks for credentials and, with prompts disabled, fails. A public one
prints refs.

## 1. Repository metadata

The description and topics are what GitHub search and "related repositories" run on. An
unset description is the single cheapest thing to fix.

```bash
gh repo edit Navilois/immich-compressor \
  --description "Recompress the originals in your Immich library, automatically, without ever losing one. Dry run by default, GPU detected for you, four-step verification before anything is deleted." \
  --homepage "https://github.com/Navilois/immich-compressor#readme" \
  --add-topic immich \
  --add-topic self-hosted \
  --add-topic selfhosted \
  --add-topic homelab \
  --add-topic ffmpeg \
  --add-topic transcoding \
  --add-topic hevc \
  --add-topic h265 \
  --add-topic qsv \
  --add-topic vaapi \
  --add-topic nvenc \
  --add-topic docker \
  --add-topic photos \
  --add-topic python
```

Then, in the web UI (no CLI equivalent):

- **Settings → General → Social preview** — upload `docs/assets/social-preview.png`.
- **Settings → General → Features** — enable **Discussions**; the issue template config
  already points questions there.
- **Settings → Code security** — enable **private vulnerability reporting**, which is what
  `SECURITY.md` tells people to use.
- **Settings → Actions → General** — confirm workflows may write packages, so the release
  workflow can push to ghcr.io, and that **"Allow GitHub Actions to create and approve pull
  requests"** is on, which is what the release workflow in section 2 needs.

Everything but the social preview can be read back rather than trusted to the settings page:

```bash
gh repo view Navilois/immich-compressor --json description,homepageUrl,repositoryTopics,hasDiscussionsEnabled
gh api repos/Navilois/immich-compressor/private-vulnerability-reporting
gh api repos/Navilois/immich-compressor/actions/permissions/workflow --jq .can_approve_pull_request_reviews
```

All of them answered as they should on 2026-08-24.

## 2. Cutting a release

Write the `Unreleased` section of `CHANGELOG.md` first, and the `Unreleased` section of
`docs/upgrading.md` if an operator has to do anything. Then run **Prepare a release** from
the Actions tab (`workflow_dispatch`, input `auto` / `major` / `minor` / `patch`). It runs
the chore — `python scripts/version.py set auto`, which bumps `__version__`, dates the
CHANGELOG section, opens an empty one, moves both compare links, renames the upgrading
heading and rebuilds the generated docs — and opens `chore(release): X.Y.Z` as a pull
request.

**Merging that pull request is the release.** `release-tag.yml` sees a `__version__` on
`main` that nothing has tagged, tags it, and calls `release.yml`. Any other merge to `main`
deploys nothing. [CONTRIBUTING.md](../../CONTRIBUTING.md#releasing-maintainers) has the two
surprises in that pull request — it arrives with no checks, and its tag is pushed by a
workflow, which is why `release-tag.yml` calls `release.yml` rather than waiting for a tag
event that never comes.

By hand, when the workflow is unavailable. `release.yml` keeps its `on: push: tags` trigger
for exactly this:

```bash
python scripts/version.py set auto   # or major | minor | patch | X.Y.Z
make check                           # lint, tests, generated docs, compose overlays
git commit -am "chore(release): X.Y.Z"
git push origin main

git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin vX.Y.Z
```

Read the CHANGELOG section for the version once more before merging or tagging.
`scripts/changelog-section.sh` turns it into the body of the GitHub release, so anything
wrong in it is published as the announcement rather than sitting in a file.

Either way `release.yml` does the publishing. It refuses a tag that disagrees with
`__version__`, that has no CHANGELOG section, that leaves an earlier section untagged, or
that is not the newest version; then it builds `linux/amd64` and `linux/arm64`, pushes to
ghcr.io as `X.Y.Z` / `X.Y` / `X` / `latest` with provenance and an SBOM, and creates the
GitHub release from the CHANGELOG section.

Afterwards, once — **already done for this package**: **Packages → immich-compressor →
Package settings → Change visibility → Public**. A package created by Actions is private by
default, and every `docker pull` in the README fails until this is done. It is a *separate*
switch from the repository's own visibility, which is why the two were confused for a day:
the package had been public for a while when the repository still was not.

Verify from a machine that has never seen the repository:

```bash
docker pull ghcr.io/navilois/immich-compressor:1
docker run --rm ghcr.io/navilois/immich-compressor:1 --version
docker run --rm ghcr.io/navilois/immich-compressor:1 hardware
gh attestation verify oci://ghcr.io/navilois/immich-compressor:X.Y.Z --repo Navilois/immich-compressor
```

The last command only has anything to verify from the first release cut **after** the
repository went public, which is 1.3.0: 1.1.1 and 1.2.0 carry no attestation, because the
step that would have created it is the step that failed.

## 3. Announce

In roughly this order. Each one is a different audience, and the first is the one that
matters.

| Where | What to say |
|---|---|
| **Immich Discord**, `#community-projects` | One paragraph, one screenshot of `immich-compressor hardware`. This community will find the bugs, and they know what `PUT /assets/copy` does and does not copy. |
| **[r/selfhosted](https://reddit.com/r/selfhosted)** | Lead with the safety story, not the compression ratio. "Deletes your originals" is what people react to; "dry run by default, four-step verification, restore command" is the answer. Post your own measured numbers and say they are yours. |
| **[r/immich](https://reddit.com/r/immich)** | Shorter. This audience already knows what Immich is; tell them what workflows made possible. |
| **[awesome-selfhosted](https://github.com/awesome-selfhosted/awesome-selfhosted)** | Needs an active project with a licence, docs and a release. Read `CONTRIBUTING.md` there first; the format is strict and the maintainers reject on format. |
| **awesome-immich**, if it exists by then | Same. |
| **Immich's own docs** | If `docs/immich-api-notes.md` turns out to be right about things the official docs get wrong, that is worth a pull request upstream on its own — separately from advertising this project. |

Do not post to all of them the same day. Fix what the first audience finds first.

## 4. The first ten issues

The predictable ones, and the answer:

| What they will say | What to answer |
|---|---|
| "Nothing happens." | Almost always the dry run — it is the default, and it is in the log. Point at [safety.md](../safety.md). If not, it is the webhook secret: Immich reports a 401 as "executed successfully". |
| "It says `no_gain` for everything." | Their footage is already HEVC. That is the gate working. `immich-compressor encode` shows the ratio for a single file. |
| "My GPU is not being used." | `immich-compressor hardware`. It has already answered this, in their own log, at startup. |
| "Permission denied on renderD128." | The render group. `hardware` names the gid to add. |
| "Can it just replace the file in place?" | No — there is no replace endpoint in the Immich API. [faq.md](../faq.md) explains what changes when the id does. |
| "Will you support Immich 2.x?" | No. Workflows do not exist there. |
| "Add a web UI." | No. Say so kindly and point at `/stats`, `/metrics` and the CLI. |
| "It deleted my photos." | Take this one seriously every single time, whatever the configuration was. Ask for `report --json` and the log first, and check the four-step chain before assuming user error. |
| A hardware report for a chip nobody here owns | The most valuable issue type. Update the matrix in [hardware.md](../hardware.md) and thank them by name in the CHANGELOG. |
| "Does it phone home?" | No. Point at the README line and invite them to grep. |

## 5. After a week

- Move any hardware report into the support matrix, and change "not verified" to "yes"
  where somebody has actually run it.
- Whatever three questions came up most belong in [faq.md](../faq.md) or
  [troubleshooting.md](../troubleshooting.md). If a question was asked twice, the
  documentation is wrong, not the reader.
- Cut a patch release for anything that stopped someone installing it. A project that
  answers its first bug in days reads very differently from one that does not.
