FROM python:3.12-slim

# ffmpeg/ffprobe  -> video presets and the sanity gate
# exiftool        -> metadata carry-over for stills
# libjxl-tools    -> cjpegli (present from libjxl 0.9; Debian trixie ships 0.11)
# imagemagick     -> fallback still encoder
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libimage-exiftool-perl \
        libjxl-tools \
        imagemagick \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Intel GPU encoding (QSV / VAAPI). Roughly 70 MB; drop this layer if you only ever run
# the CPU presets.
#   intel-media-va-driver-non-free  the iHD VA driver, lives in Debian's non-free
#   libmfx-gen1.2                   oneVPL GPU runtime, Gen12+ (Tiger Lake and newer, Arc)
#   vainfo                          the only way to diagnose this from inside the container
# Debian trixie no longer ships libmfx1, so the legacy MSDK path for Gen9-11 is gone:
# on those chips use the hevc_vaapi preset instead of hevc_qsv. ffmpeg here is built
# --disable-libmfx --enable-libvpl, so it reaches the GPU through oneVPL only.
RUN sed -i 's/^Components: main$/Components: main non-free non-free-firmware/' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        intel-media-va-driver-non-free \
        libmfx-gen1.2 \
        libigdgmm12 \
        vainfo \
    && rm -rf /var/lib/apt/lists/*

ENV LIBVA_DRIVER_NAME=iHD

# Non-root: the service never needs to write outside its own two directories.
RUN useradd --system --create-home --uid 10001 compressor \
    && mkdir -p /var/lib/immich-compressor /var/tmp/immich-compressor \
    && chown -R compressor:compressor /var/lib/immich-compressor /var/tmp/immich-compressor

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
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
