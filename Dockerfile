# syntax=docker/dockerfile:1
#
# Multi-arch: linux/amd64 and linux/arm64.
#
# Hardware encoding is not the same on both, and the image does not pretend otherwise:
#
#   amd64   Intel QSV and VAAPI, plus AMD VAAPI, through Intel's non-free iHD driver and
#           the oneVPL runtime. NVENC works when the host has the NVIDIA Container Toolkit
#           (nothing extra is needed in the image — the toolkit injects the driver).
#   arm64   Mesa's VA drivers only. In practice that means CPU encoding on a Raspberry Pi
#           or an Ampere box: the Intel packages do not exist for this architecture, and
#           the SoC video engines that do exist are reached through V4L2 rather than VA-API.
#           `immich-compressor hardware` tests what is actually there and says so.
#
# `immich-compressor hardware` never assumes any of the above — it runs a real one-frame
# encode before selecting anything.

FROM python:3.12-slim AS base

# ffmpeg/ffprobe  -> video presets and the sanity gate
# exiftool        -> metadata carry-over for stills
# imagemagick     -> the stills encoder
# libjxl-tools    -> cjxl/djxl. NOT cjpegli: Debian trixie's 0.11.2 does not ship it,
#                    which is why the stills preset uses ImageMagick.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libimage-exiftool-perl \
        libjxl-tools \
        imagemagick \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*


# --- amd64: Intel QSV/VAAPI + AMD VAAPI ------------------------------------------------
#
# Roughly 70 MB. Drop this stage if you only ever run the CPU preset.
#   intel-media-va-driver-non-free  the iHD VA driver, which lives in Debian's non-free
#   libmfx-gen1.2                   oneVPL GPU runtime, Gen12+ (Tiger Lake and newer, Arc)
#   mesa-va-drivers                 radeonsi, for AMD cards on the same /dev/dri path
#   vainfo                          the only way to diagnose any of this from inside
#
# Debian trixie no longer ships libmfx1, so the legacy MSDK path for Gen9-11 is gone: on
# those chips hevc_qsv fails with "Error creating a MFX session: -9" and hevc_vaapi is
# used instead. The service works that out on its own — measured on an Intel UHD 630.
FROM base AS drivers-amd64
RUN sed -i 's/^Components: main$/Components: main non-free non-free-firmware/' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        intel-media-va-driver-non-free \
        libmfx-gen1.2 \
        libigdgmm12 \
        mesa-va-drivers \
        vainfo \
    && rm -rf /var/lib/apt/lists/*
ENV LIBVA_DRIVER_NAME=iHD


# --- arm64: Mesa VA drivers ------------------------------------------------------------
#
# No LIBVA_DRIVER_NAME here on purpose: there is no single right answer on arm64, so libva
# is left to pick the driver that matches whatever DRM device is present.
FROM base AS drivers-arm64
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        mesa-va-drivers \
        vainfo \
    && rm -rf /var/lib/apt/lists/*


# --- the service ------------------------------------------------------------------------
ARG TARGETARCH
FROM drivers-${TARGETARCH} AS final

# ImageMagick is built with OpenMP and sizes its thread pool from the *host* core count,
# ignoring the container's cgroup CPU limit — the same trap the video preset defuses with
# `pools=2 -threads 2`. Set as ENV rather than as `-limit` inside a preset so it also
# covers a hand-written one. The memory limits matter for large stills: the Q16 pixel
# cache is ~96 MB for a 12 MP image but ~800 MB for a 100 MP panorama.
ENV MAGICK_THREAD_LIMIT=2 \
    MAGICK_MEMORY_LIMIT=512MiB \
    MAGICK_MAP_LIMIT=1GiB

# Non-root: the service never needs to write outside its own two directories.
RUN useradd --system --create-home --uid 10001 compressor \
    && mkdir -p /var/lib/immich-compressor /var/tmp/immich-compressor \
    && chown -R compressor:compressor /var/lib/immich-compressor /var/tmp/immich-compressor

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
# WORKDIR is /app, so `scripts/calibrate.sh` is the same command inside the container
# as it is in a checkout — which is what `immich-compressor hardware` tells you to run.
COPY scripts/calibrate.sh ./scripts/calibrate.sh
RUN pip install --no-cache-dir . \
    && rm -rf /root/.cache

USER compressor

ENV COMPRESSOR_CONFIG=/app/config.yaml \
    PYTHONUNBUFFERED=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"

ENTRYPOINT ["immich-compressor"]
CMD ["serve"]

# Last on purpose: the labels change on every release, and anything after them would lose
# its build cache with each version bump.
# Set by the release workflow and by `make image`; the single source of truth is
# __version__ in src/immich_compressor/__init__.py.
ARG VERSION=0.0.0-dev

LABEL org.opencontainers.image.title="immich-compressor" \
      org.opencontainers.image.description="Out-of-band recompression for Immich assets, driven by a workflow webhook." \
      org.opencontainers.image.source="https://github.com/Navilois/immich-compressor" \
      org.opencontainers.image.documentation="https://github.com/Navilois/immich-compressor#readme" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"
