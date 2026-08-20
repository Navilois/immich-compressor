# Quickstart

Five minutes, and nothing is deleted at the end of them.

## Before you start

- Immich **v3.0.0 or newer** (workflows were introduced there; developed and verified
  against v3.1.0).
- Docker, and shell access to the host running Immich.
- The name of the docker network your Immich stack uses — `docker network ls`, usually
  `immich_default`.

## 1. Get an API key

In Immich: **Account Settings → API Keys → New**. Grant exactly these:

`asset.read`, `asset.download`, `asset.upload`, `asset.update`, `asset.copy`, `tag.read`,
`tag.create`, `tag.asset`

**Leave `asset.delete` out for now.** Without it the service physically cannot remove
anything, which is a useful guarantee for the first run. You add it at
[stage 3](safety.md#stage-3--move-originals-to-the-trash).

## 2. Run setup

```bash
git clone https://github.com/Navilois/immich-compressor
cd immich-compressor
./scripts/quickstart.sh
```

The script pulls the image and runs `immich-compressor setup` inside it, with this directory
mounted and `/dev/dri` passed through when the host has it. Setup then:

- checks the key against your server and **names any permission it is missing**;
- detects the hardware and picks the encoder — see [hardware.md](hardware.md);
- writes a `config.yaml` tuned to your box;
- generates the webhook secret and writes `.env` at mode 0600;
- writes `RENDER_GID` and `COMPOSE_FILE` so the GPU overlay loads by itself afterwards;
- creates the Immich workflow, or writes out the exact JSON and curl command if the
  credentials do not allow it.

It is safe to run again. An existing `config.yaml` is left alone and the stored webhook
secret is kept, because rotating it would break the workflow that already carries it.

<details>
<summary>Without the script</summary>

```bash
docker run --rm -it \
  --device /dev/dri:/dev/dri --group-add "$(getent group render | cut -d: -f3)" \
  --user "$(id -u):$(id -g)" -v "$PWD:/work" -w /work \
  ghcr.io/navilois/immich-compressor:1 setup
```

Drop the two GPU flags on a machine without `/dev/dri`.
</details>

## 3. Create the workflow

If setup could not create it, do it in the UI: **Utilities → Workflows → New**, or POST the
`immich-workflow.json` it wrote. [workflow-setup.md](workflow-setup.md) has the full JSON
and the gotchas.

## 4. Start it

```bash
docker compose up -d
docker compose logs -f immich-compressor
```

The first log line tells you which encoder it chose and why. Nothing is uploaded and nothing
is deleted: the shipped configuration is a dry run.

## 5. Watch a dry run

Upload a video or a photo larger than 1 MiB, wait five minutes (`initial_delay_seconds` lets Immich
finish its own thumbnail and machine-learning jobs first), then:

```bash
docker compose exec immich-compressor immich-compressor report
```

```
=== immich-compressor report ===
database: /var/lib/immich-compressor/state.db
jobs total: 1
  skipped          1
skip reasons:
  dry_run              1
compressed assets: 0
saved: 0.0 MiB (average ratio None)
```

That is the whole point of stage 1: it saw the asset, decided it would compress it, and did
nothing.

## 6. Go live

Read [safety.md](safety.md) first — it is short, and it is about your photos. Then, in
`docker-compose.override.yaml`:

```yaml
services:
  immich-compressor:
    environment:
      BEHAVIOR__DRY_RUN: "false"
```

```bash
docker compose up -d
```

Now both versions exist side by side. Nothing is removed until you also set
`BEHAVIOR__TRASH_ORIGINAL`, which is [stage 3](safety.md#stage-3--move-originals-to-the-trash).

## Working through the existing library

The webhook only fires for assets moving through Immich's pipeline. Everything already in
the library is invisible to it. Use `backfill`, which is dry until you say otherwise:

```bash
docker compose exec immich-compressor immich-compressor backfill --type VIDEO --limit 50
docker compose exec immich-compressor immich-compressor backfill --type VIDEO --limit 50 --apply
```

**Do not re-run Immich's metadata extraction to reach them.** See
[the metadata-extraction trap](operations.md#the-metadata-extraction-trap).

## Next

- [installation.md](installation.md) — every install option, and what setup does by hand
- [hardware.md](hardware.md) — GPU encoding, the support matrix, calibration
- [safety.md](safety.md) — the four stages, the verification chain, rollback
- [operations.md](operations.md) — CLI, endpoints, job states, backfill
- [troubleshooting.md](troubleshooting.md) — when something does not happen
