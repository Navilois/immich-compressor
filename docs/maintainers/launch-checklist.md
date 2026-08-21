# Launch checklist

Everything a human still has to do, in the order it makes sense to do it. Nothing here can
be automated from inside the repository, which is why it is written down.

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
  workflow can push to ghcr.io.

## 2. First release

```bash
# 1. The CHANGELOG's Unreleased section becomes a dated version section.
# 2. Bump __version__ in src/immich_compressor/__init__.py. That is the only place.
make check                      # lint, tests, generated docs, compose overlays
git commit -am "chore: release 1.1.0"
git push origin main

git tag -a v1.1.0 -m "1.1.0"
git push origin v1.1.0
```

The tag starts `release.yml`, which refuses to publish if the tag disagrees with
`__version__` or the CHANGELOG has no section for it, then builds `linux/amd64` and
`linux/arm64`, pushes to ghcr.io as `1.1.0` / `1.1` / `1` / `latest` with provenance and an
SBOM, and creates the GitHub release from the CHANGELOG section.

Afterwards, once: **Packages → immich-compressor → Package settings → Change visibility →
Public**. A package created by Actions is private by default, and every `docker pull` in the
README fails until this is done. This is the single most likely reason the first ten people
cannot install it.

Verify from a machine that has never seen the repository:

```bash
docker pull ghcr.io/navilois/immich-compressor:1
docker run --rm ghcr.io/navilois/immich-compressor:1 --version
docker run --rm ghcr.io/navilois/immich-compressor:1 hardware
gh attestation verify oci://ghcr.io/navilois/immich-compressor:1.1.0 --repo Navilois/immich-compressor
```

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
