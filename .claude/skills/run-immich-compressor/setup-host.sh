#!/usr/bin/env bash
# Install the media toolchain the encoder and its tests need, then build the venv.
#
#   .claude/skills/run-immich-compressor/setup-host.sh
#
# Idempotent. Safe to re-run, and you will have to: /opt and /usr/local/bin do not
# survive a container recreate here, only ~/workspace, ~/.ssh, ~/.claude and
# /var/lib/docker do.
#
# Why not just `apt-get install imagemagick libimage-exiftool-perl`:
#
#   magick    Ubuntu 24.04 ships ImageMagick 6, which has no `magick` binary at all —
#             `convert` is the IM6 name and IM7 renamed it. The stills preset in
#             hardware.py calls `magick` literally, so IM6 is not a fallback, it is a
#             missing binary. The Dockerfile builds on python:3.14-slim (Debian trixie),
#             which does ship IM7; this script closes that gap with the AppImage.
#   exiftool  Ubuntu ships 12.76, which knows the XMP-GCamera *group* but not the
#             XMP-GCamera:MotionPhoto *tag*. With it,
#             test_motion_photo_markers_are_flagged_without_a_trailer fails with
#             "Tag 'XMP-GCamera:MotionPhoto' is not defined" and nothing else does.
#             Measured on 2026-08-27: 689 passed / 1 failed with 12.76, 690 passed with
#             13.59.
#   ffmpeg    Ubuntu's 6.1.1 is fine. libx264 and libx265 are both built in.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "==> apt: ffmpeg"
sudo apt-get update -qq
sudo apt-get install -y -qq ffmpeg curl

if ! magick -version 2>/dev/null | grep -q 'ImageMagick 7'; then
  echo "==> ImageMagick 7 (AppImage -> /opt/imagemagick7)"
  IM_TAG="$(curl -fsSL https://api.github.com/repos/ImageMagick/ImageMagick/releases/latest \
    | grep -m1 '"tag_name"' | cut -d'"' -f4)"
  curl -fsSL -o "$TMP/im.AppImage" \
    "https://github.com/ImageMagick/ImageMagick/releases/download/${IM_TAG}/ImageMagick-${IM_TAG}-gcc-x86_64.AppImage"
  chmod +x "$TMP/im.AppImage"
  # --appimage-extract rather than running it directly: the AppImage needs FUSE, which
  # this container does not have.
  (cd "$TMP" && ./im.AppImage --appimage-extract >/dev/null)
  sudo rm -rf /opt/imagemagick7
  sudo mv "$TMP/squashfs-root" /opt/imagemagick7
  # AppRun dispatches on $ARGV0 only when $APPIMAGE is set, which it is not here, so it
  # always execs `magick "$@"`. `AppRun magick -version` therefore runs
  # `magick magick -version` and dies with "no decode delegate for image format `magick'".
  # The magick wrapper must pass its arguments straight through; identify is reached as
  # IM7's `magick identify` subcommand.
  printf '#!/bin/sh\nexec /opt/imagemagick7/AppRun "$@"\n' | sudo tee /usr/local/bin/magick >/dev/null
  printf '#!/bin/sh\nexec /opt/imagemagick7/AppRun identify "$@"\n' | sudo tee /usr/local/bin/identify >/dev/null
  sudo chmod +x /usr/local/bin/magick /usr/local/bin/identify
fi

if ! exiftool -ver 2>/dev/null | grep -q '^13\.'; then
  echo "==> exiftool 13.x (-> /opt/exiftool)"
  ET_TAG="$(curl -fsSL https://api.github.com/repos/exiftool/exiftool/tags | grep -m1 '"name"' | cut -d'"' -f4)"
  curl -fsSL -o "$TMP/exiftool.tar.gz" \
    "https://github.com/exiftool/exiftool/archive/refs/tags/${ET_TAG}.tar.gz"
  sudo rm -rf /opt/exiftool
  sudo mkdir -p /opt/exiftool
  sudo tar -xzf "$TMP/exiftool.tar.gz" -C /opt/exiftool --strip-components=1
  printf '#!/bin/sh\nexec /opt/exiftool/exiftool "$@"\n' | sudo tee /usr/local/bin/exiftool >/dev/null
  sudo chmod +x /usr/local/bin/exiftool
fi

if [ ! -x "$REPO/.venv/bin/immich-compressor" ]; then
  echo "==> venv"
  python3 -m venv "$REPO/.venv"
  "$REPO/.venv/bin/python" -m pip install --upgrade -q pip
  "$REPO/.venv/bin/python" -m pip install -q -e "$REPO[dev]"
fi

echo
echo "ffmpeg    $(ffmpeg -version | head -1 | cut -d' ' -f1-3)"
echo "magick    $(magick -version | head -1 | cut -d' ' -f1-3)"
echo "exiftool  $(exiftool -ver)"
echo "package   $("$REPO/.venv/bin/immich-compressor" --version)"
