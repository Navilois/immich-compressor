# Installation

[quickstart.md](quickstart.md) is the fast path. This page is what it does, and what to do
when your setup does not match its assumptions.

## Requirements

- **Immich v3.0.0 or newer.** Workflows were introduced there. Developed and verified
  against v3.1.0; there is no support for Immich 2.x and there will not be.
- **Docker**, on the host that runs Immich, or on one that shares a network with it.
- **Network reachability in both directions**: Immich must reach the service's `/webhook`,
  and the service must reach Immich's API.
- Optionally, a **GPU** — see [hardware.md](hardware.md). Everything works without one.

The image bundles `ffmpeg`, `ffprobe`, `exiftool` and ImageMagick. There is nothing to
install on the host.

## The image

```
ghcr.io/navilois/immich-compressor:1
```

Tagged `X.Y.Z`, `X.Y`, `X` and `latest`. The compose file pins the **major** tag, so patch
and minor releases arrive with a `docker compose pull` and a breaking change never does.
Built for `linux/amd64` and `linux/arm64`, with provenance and an SBOM attached.

## Networking

The service has to sit on the same docker network as Immich, so the workflow can reach it by
name:

```bash
docker network ls | grep -i immich
```

Put that name in `.env` as `IMMICH_NETWORK`. The compose file joins it as an external
network — it never creates one, and it never touches your Immich stack's definition.

If Immich runs on a different host, publish the service's port deliberately and point the
workflow's webhook URL at it. `/stats` and `/metrics` are unauthenticated, so put it behind
your reverse proxy or bind it to a private interface — never `0.0.0.0`.

## API key permissions

Create the key under **Account Settings → API Keys**. Immich v3 has granular permissions;
grant exactly these:

| Permission | Needed for |
|---|---|
| `asset.read` | reading the asset and its metadata |
| `asset.download` | downloading the original |
| `asset.upload` | uploading the compressed file |
| `asset.update` | carrying over description, rating, GPS, and writing the marker |
| `asset.copy` | carrying over albums, favourite, shared links, stack |
| `tag.read` | reading the source's tags |
| `tag.create` | re-creating tags by name |
| `tag.asset` | attaching them to the replacement |
| `asset.delete` | **only** when `trash_original: true`; covers both delete modes |

The key is sent as `x-api-key` and is read **only** from the environment
(`IMMICH__API_KEY`). Putting it into `config.yaml` makes the service refuse to start.

`immich-compressor setup` asks the server one deliberately inert request per permission —
reads aim at an asset id that cannot exist, writes carry empty id lists — and names any that
come back forbidden.

## Doing it without `setup`

```bash
cp .env.example .env && chmod 600 .env
# fill in IMMICH_API_KEY, COMPRESSOR_TOKEN (openssl rand -hex 32) and IMMICH_NETWORK
cp config.example.yaml config.yaml       # optional: every value has a default
docker compose up -d
```

Then create the workflow by hand — [workflow-setup.md](workflow-setup.md).

## Compose layout

| File | What it is |
|---|---|
| `docker-compose.yaml` | The service. Pulls the published image. **Do not edit it.** |
| `docker-compose.override.yaml` | Yours. Compose loads it automatically; it is gitignored. Start from `docker-compose.override.example.yaml`. |
| `docker-compose.build.yaml` | Build from this checkout instead of pulling. |
| `docker-compose.gpu.yaml` | `/dev/dri` passthrough for Intel and AMD. |
| `docker-compose.gpu-nvidia.yaml` | NVIDIA runtime and device reservation. |
| `docker-compose.test.yaml` | A full Immich stack for the live test suite. Not for production. |

Everything deployment-specific belongs in `docker-compose.override.yaml`. Editing the
tracked file turns every `git pull` into a merge conflict.

Overlays can be loaded without flags by putting them in `.env`:

```
COMPOSE_FILE=docker-compose.yaml:docker-compose.gpu.yaml:docker-compose.override.yaml
```

That list **replaces** the one compose uses by default, and `docker-compose.override.yaml`
is only in that default list — so name it here too, or it silently stops being loaded and
takes the go-live flags, the resource limits and any local image pin with it. It goes last,
because the last file wins. Leave the entry out if you have no override: compose refuses to
run at all on a file it cannot find.

`setup` writes that line for you when it finds a usable GPU, and appends the override when
one already exists. If you write your override afterwards — which is what
[safety.md](safety.md) has you do at go-live — add it to the line yourself.

## Sizing

```yaml
# docker-compose.override.yaml
services:
  immich-compressor:
    cpus: 4
    mem_limit: 3g
```

Leave Immich at least half the host's cores. The encoder reads this limit from its own
cgroup and sizes its thread pool to match, so there is no second number to keep in sync —
see [the CPU budget](hardware.md#the-cpu-budget).

`work_dir` needs room for the source and the encode at the same time; the service refuses to
start a job unless `free_space_factor` (3×) times the source size is free.

## Building from source

```bash
docker compose -f docker-compose.yaml -f docker-compose.build.yaml up -d --build
```

Or `make image`. See [CONTRIBUTING.md](../CONTRIBUTING.md) for the development setup.

## Upgrading

See [upgrading.md](upgrading.md).
