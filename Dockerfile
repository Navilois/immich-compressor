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
