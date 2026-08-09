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
