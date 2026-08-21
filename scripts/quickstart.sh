#!/usr/bin/env bash
# Quickstart: pull the image, run the guided setup, tell you what comes next.
#
#   git clone https://github.com/Navilois/immich-compressor
#   cd immich-compressor
#   ./scripts/quickstart.sh
#
# Deliberately NOT a `curl | bash` installer. Read it first — it is short, and it runs on
# your photo library's machine.
#
# What it does:
#   1. pulls ghcr.io/navilois/immich-compressor:1
#   2. runs `immich-compressor setup` inside that image — attached to your Immich network
#      so it can reach the server by name, with this directory mounted so the generated
#      config.yaml and .env land here, and with /dev/dri passed through when the host has
#      it so hardware detection tests the real GPU
#   3. leaves the rest to `setup`, which ends with the commands that start the service
#
# Set NETWORK if your Immich stack does not use the default network name:
#
#   NETWORK=myimmich_default ./scripts/quickstart.sh
#
# It never starts the service and never changes your Immich library.
set -euo pipefail

IMAGE="${IMAGE:-ghcr.io/navilois/immich-compressor:1}"
# The docker network your Immich stack uses. `docker network ls` shows it.
NETWORK="${NETWORK:-immich_default}"
cd "$(dirname "$0")/.."
PROJECT_DIR="$PWD"

command -v docker >/dev/null || { echo "docker is not installed" >&2; exit 1; }

echo "==> Pulling $IMAGE"
if ! docker pull "$IMAGE"; then
  # A local tag (IMAGE=immich-compressor:local, say) has nothing to pull from. Carry on if
  # the image is already here, and only fail when there is genuinely nothing to run.
  docker image inspect "$IMAGE" >/dev/null 2>&1 || {
    echo "could not pull $IMAGE and no local copy exists" >&2
    exit 1
  }
  echo "==> Pull failed; using the local copy of $IMAGE"
fi

# Hand the container the GPU if the host has one, so detection is not guessing. A host
# without /dev/dri simply skips this and gets the CPU preset.
gpu_args=()
if [ -d /dev/dri ]; then
  gpu_args+=(--device /dev/dri:/dev/dri)
  if render_gid=$(getent group render | cut -d: -f3) && [ -n "$render_gid" ]; then
    gpu_args+=(--group-add "$render_gid")
  fi
  echo "==> Found /dev/dri, passing it through so hardware detection can test it"
fi

# Join the Immich network, or setup cannot resolve `immich-server` at all — the default
# bridge is a different network entirely, and the failure looks like the server being down.
net_args=()
if docker network inspect "$NETWORK" >/dev/null 2>&1; then
  net_args+=(--network "$NETWORK")
  echo "==> Joining the $NETWORK network, so setup can reach Immich by name"
else
  echo "==> No docker network called '$NETWORK'." >&2
  echo "    docker network ls | grep -i immich   shows what yours is called;" >&2
  echo "    then re-run as:  NETWORK=<that name> ./scripts/quickstart.sh" >&2
  echo "    Continuing without it — setup can still reach Immich by IP." >&2
fi

# Interactive only when there is a terminal to be interactive with, so the same script
# works from a provisioning tool.
tty_args=()
if [ -t 0 ] && [ -t 1 ]; then
  tty_args+=(-it)
else
  set -- "$@" --non-interactive
fi

echo "==> Running setup"
# --network comes before "$@" so anything the caller passes still wins.
# `-e NAME` with no value passes the variable through only when the host actually has it
# set, so an unset key still reaches setup's own prompt. IMMICH_API_KEY is the variable
# setup's own error message tells you to export, and without this line that advice sent
# you straight back into the dead end you were already standing in. TZ puts the container's
# log timestamps on the same clock as Immich's, which is what makes the two comparable.
docker run --rm "${tty_args[@]}" "${gpu_args[@]}" "${net_args[@]}" \
  --user "$(id -u):$(id -g)" \
  -e IMMICH_API_KEY \
  -e TZ \
  -v "$PROJECT_DIR:/work" \
  -w /work \
  "$IMAGE" setup --network "$NETWORK" "$@"

# Nothing is printed after this on purpose: `setup` ends with its own numbered "Next"
# block, and a second copy of the same three commands read like something had gone wrong.
