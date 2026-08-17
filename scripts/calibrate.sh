#!/usr/bin/env bash
#
# Sweep the quality knob of an encoder over real files and print size ratio + SSIM, so the
# number in config.yaml is measured instead of guessed.
#
#   scripts/calibrate.sh /path/clip1.mov /path/clip2.mp4
#   ENCODER=hevc_vaapi scripts/calibrate.sh /path/clip.mp4
#   ENCODER=libx265 QUALITIES="24 26 28" scripts/calibrate.sh /path/clip.mp4
#
# Rule of thumb for the result: take the *highest* quality number that still holds
# SSIM >= 0.98 and a ratio <= your `max_ratio` (default 0.6).
#
# Each file is cut to SECONDS_LIMIT first and every measurement is made against that cut,
# so the ratios are comparable and one long clip does not dominate the run time.

set -euo pipefail

ENCODER="${ENCODER:-hevc_qsv}"
QUALITIES="${QUALITIES:-22 24 26 28 30}"
FFPRESET="${FFPRESET:-slower}"
DEVICE="${DEVICE:-/dev/dri/renderD128}"
SECONDS_LIMIT="${SECONDS_LIMIT:-60}"
WORK="${WORK:-$(mktemp -d)}"

if [ "$#" -eq 0 ]; then
    echo "usage: $0 <video> [video ...]" >&2
    exit 2
fi

mkdir -p "$WORK"

encoder_args() {
    local quality="$1"
    case "$ENCODER" in
        *_qsv)
            printf '%s\n' -c:v "$ENCODER" -preset "$FFPRESET" -global_quality "$quality" \
                -extbrc 1 -look_ahead_depth 40 -adaptive_i 1 -adaptive_b 1 -b_strategy 1 \
                -bf 3 -g 250 -tag:v hvc1
            ;;
        *_vaapi)
            printf '%s\n' -vf "format=nv12|vaapi,hwupload" \
                -c:v "$ENCODER" -rc_mode ICQ -global_quality "$quality" -bf 3 -g 250 -tag:v hvc1
            ;;
        *_nvenc)
            printf '%s\n' -c:v "$ENCODER" -preset p6 -tune hq -rc vbr -cq "$quality" -b:v 0 \
                -rc-lookahead 32 -spatial-aq 1 -bf 3 -g 250 -tag:v hvc1
            ;;
        *)
            printf '%s\n' -c:v "$ENCODER" -preset "$FFPRESET" -crf "$quality" \
                -x265-params pools=2 -threads 2 -tag:v hvc1
            ;;
    esac
}

input_args() {
    case "$ENCODER" in
        *_qsv)   printf '%s\n' -hwaccel qsv -qsv_device "$DEVICE" ;;
        *_vaapi) printf '%s\n' -hwaccel vaapi -hwaccel_device "$DEVICE" ;;
        *)       ;;
    esac
}

ssim_of() {
    # ffmpeg prints "... All:0.991234 (20.6)" on stderr.
    ffmpeg -hide_banner -loglevel info -i "$1" -i "$2" -lavfi ssim -f null - 2>&1 \
        | grep -o 'All:[0-9.]*' | tail -1 | cut -d: -f2
}

echo "encoder: $ENCODER   qualities: $QUALITIES   device: $DEVICE   work: $WORK"

for source in "$@"; do
    name="$(basename "$source")"
    reference="$WORK/${name%.*}.ref.mp4"
    ffmpeg -hide_banner -loglevel error -y -noautorotate -i "$source" \
        -t "$SECONDS_LIMIT" -c copy -map 0:v:0 -map '0:a:0?' "$reference"
    reference_bytes="$(stat -c %s "$reference")"

    printf '\n%s  (%s MiB reference cut)\n' "$name" \
        "$(awk -v b="$reference_bytes" 'BEGIN { printf "%.1f", b / 1048576 }')"
    printf '%-8s %12s %8s %8s %8s\n' quality bytes ratio ssim seconds

    smallest="" largest=""
    for quality in $QUALITIES; do
        output="$WORK/${name%.*}.q${quality}.mp4"
        started="$(date +%s)"
        # shellcheck disable=SC2046  # word splitting is how the arg lists are assembled
        if ! ffmpeg -hide_banner -loglevel error -y -noautorotate $(input_args) -i "$reference" \
            -map 0 -map_metadata 0 -movflags use_metadata_tags+faststart \
            $(encoder_args "$quality") -c:a copy "$output" 2> "$WORK/err.log"; then
            printf '%-8s %12s  encode failed: %s\n' "$quality" - "$(tail -1 "$WORK/err.log")"
            continue
        fi
        elapsed=$(( $(date +%s) - started ))
        bytes="$(stat -c %s "$output")"
        ratio="$(awk -v o="$bytes" -v r="$reference_bytes" 'BEGIN { printf "%.3f", o / r }')"
        printf '%-8s %12s %8s %8s %8s\n' \
            "$quality" "$bytes" "$ratio" "$(ssim_of "$reference" "$output")" "${elapsed}s"
        [ -z "$smallest" ] && smallest="$bytes"
        largest="$bytes"
    done

    # The check that matters on Intel: in low-power (VDENC) mode some chips ignore ICQ
    # entirely, and then every row above is the same file with a different name.
    if [ -n "$smallest" ] && [ -n "$largest" ]; then
        awk -v a="$smallest" -v b="$largest" 'BEGIN {
            if (a > 0 && (a > b ? (a - b) / a : (b - a) / b) < 0.02)
                print "  WARNING: size barely moved across the sweep — the encoder is " \
                      "ignoring the quality setting. Try -low_power 0, or switch to VBR."
        }'
    fi
done

echo
echo "artifacts in $WORK — delete when done"
