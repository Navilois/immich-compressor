"""The built-in presets: every encoder this project knows how to drive, and its flags.

A table, deliberately. The comments on each entry are the reasoning for the flags it
carries, and several of them record a bug the flag fixes — read them before changing one.
"""

from __future__ import annotations

from dataclasses import dataclass
from string import Template
from typing import Literal

from ..config import AssetType, Preset

HardwareMode = Literal["auto", "cpu", "qsv", "vaapi", "nvenc"]
QualityLevel = Literal["balanced", "higher", "smaller"]


@dataclass(frozen=True, slots=True)
class EncoderSpec:
    """One recipe in the built-in catalog, before a device is filled in."""

    encoder: str
    label: str
    match_type: AssetType
    template: str
    quality: dict[str, int]
    suffix: str = ".mp4"
    vendors: tuple[str, ...] = ()  # empty means CPU, no device needed
    needs_va_encode: bool = False
    exiftool_copy: bool = False
    normalize_orientation: bool = False
    timeout_s: float = 7200.0
    # Passed straight through to the generated `Preset`. Empty/None means "no opinion",
    # which is what every video spec wants — the stills spec is the one that constrains
    # the formats it accepts and overrides the gate for a cheap encode.
    extensions: tuple[str, ...] = ()
    max_ratio: float | None = None
    require_date_time_original: bool | None = None
    # Keyed by quality level like `quality` above, because the threshold only means
    # anything relative to what the preset itself encodes at.
    min_source_quality: dict[str, int] | None = None

    @property
    def is_hardware(self) -> bool:
        return bool(self.vendors)

    def render(self, *, node: str | None, quality: str, threads: int) -> str:
        return " ".join(
            Template(self.template)
            .substitute(
                node=node or "",
                q=self.quality[quality],
                threads=threads,
            )
            .split()
        )

    def build(self, *, node: str | None, quality: str, threads: int, name: str) -> Preset:
        return Preset(
            name=name,
            type=self.match_type,
            extensions=list(self.extensions),
            cmd=self.render(node=node, quality=quality, threads=threads),
            suffix=self.suffix,
            exiftool_copy=self.exiftool_copy,
            normalize_orientation=self.normalize_orientation,
            timeout_s=self.timeout_s,
            max_ratio=self.max_ratio,
            require_date_time_original=self.require_date_time_original,
            min_source_quality=(self.min_source_quality[quality] if self.min_source_quality else None),
        )


# Why the quality numbers are what they are: `balanced` reproduces exactly the settings
# this project shipped and ran in production before the catalog existed, so upgrading
# changes nothing you can see. `higher` and `smaller` step in the direction their name
# says. They are starting points, not benchmarks — `scripts/calibrate.sh` measures your
# own footage, which is the only measurement that means anything.
#
# The candidates are ordered as they appear here. NVENC first because a discrete NVIDIA
# GPU is the fastest of the three and has the most rate-control machinery; QSV before
# VAAPI on Intel because oneVPL exposes lookahead and extended bitrate control that the
# VAAPI path does not; VAAPI last among the hardware paths because it is the lowest
# common denominator that every Intel and AMD driver implements. This is a policy, not a
# measurement: if it picks wrong for your footage, pin `hardware.mode`.
VIDEO_ENCODERS: tuple[EncoderSpec, ...] = (
    EncoderSpec(
        encoder="hevc_nvenc",
        label="NVIDIA NVENC HEVC",
        match_type="VIDEO",
        vendors=("nvidia",),
        quality={"higher": 25, "balanced": 28, "smaller": 32},
        # No `-hwaccel cuda`: decoding stays in software, which costs a little speed and
        # buys a fallback for the sources the chip cannot decode (MJPEG-AVI, old DivX,
        # 10-bit VP9) instead of failing the job outright. Same trade-off as the QSV
        # preset below. `-b:v 0` is what turns `-cq` into true constant quality.
        template="""
            ffmpeg -y -loglevel error -noautorotate -i {input}
            -map 0 -map_metadata 0 -movflags use_metadata_tags+faststart
            -c:v hevc_nvenc -preset p6 -tune hq -rc vbr -cq $q -b:v 0
            -bf 3 -g 250 -tag:v hvc1
            -c:a copy
            {output}
        """,
    ),
    EncoderSpec(
        encoder="hevc_qsv",
        label="Intel Quick Sync (oneVPL) HEVC",
        match_type="VIDEO",
        vendors=("intel",),
        quality={"higher": 23, "balanced": 26, "smaller": 30},
        # -global_quality is ICQ, the QSV equivalent of CRF. -extbrc 1 is not optional:
        # -look_ahead_depth is a no-op without extended bitrate control. -preset slower is
        # nearly free on an iGPU, where the fixed-function block is the limit rather than
        # compute time. No -pix_fmt, so an 8-bit source produces Main and a 10-bit/HDR
        # source produces Main10 instead of being flattened.
        template="""
            ffmpeg -y -loglevel error -noautorotate
            -hwaccel qsv -qsv_device $node -i {input}
            -map 0 -map_metadata 0 -movflags use_metadata_tags+faststart
            -c:v hevc_qsv -preset slower -global_quality $q
            -extbrc 1 -look_ahead_depth 40 -adaptive_i 1 -adaptive_b 1 -b_strategy 1
            -bf 3 -g 250 -tag:v hvc1
            -c:a copy
            {output}
        """,
    ),
    EncoderSpec(
        encoder="hevc_vaapi",
        label="VAAPI HEVC (Intel Gen9-11, AMD)",
        match_type="VIDEO",
        vendors=("intel", "amd"),
        needs_va_encode=True,
        quality={"higher": 23, "balanced": 26, "smaller": 30},
        # Note the missing `-map 0`: the filter chain does not survive extra streams, so
        # subtitle and data tracks are dropped on this path. That is a real difference
        # from the QSV and CPU presets and is documented in docs/hardware.md.
        template="""
            ffmpeg -y -loglevel error -noautorotate
            -hwaccel vaapi -hwaccel_device $node -i {input}
            -vf format=nv12|vaapi,hwupload
            -map_metadata 0 -movflags use_metadata_tags+faststart
            -c:v hevc_vaapi -rc_mode ICQ -global_quality $q -bf 3 -g 250 -tag:v hvc1
            -c:a copy
            {output}
        """,
    ),
    EncoderSpec(
        encoder="libx265",
        label="CPU x265",
        match_type="VIDEO",
        quality={"higher": 23, "balanced": 26, "smaller": 29},
        # `-noautorotate` is not cosmetic: without it ffmpeg bakes a portrait clip's
        # display matrix into the pixels and drops the matrix, so 1920x1080+rot90 comes out
        # as 1080x1920+rot0 — and the sanity gate correctly rejects that as a resolution
        # change. pools/-threads come from the cgroup budget, see CpuBudget.
        template="""
            ffmpeg -y -loglevel error -noautorotate -i {input}
            -map_metadata 0 -map 0 -movflags use_metadata_tags+faststart
            -c:v libx265 -preset medium -crf $q -tag:v hvc1
            -x265-params pools=$threads -threads $threads
            -c:a aac -b:a 128k
            {output}
        """,
    ),
)

# JPEG only, and an allowlist rather than a denylist — this is the entry that keeps the
# stills path from destroying data. Immich files RAW, PNG, GIF, TIFF, WebP and HEIC under
# type IMAGE exactly like JPEG, and ImageMagick reads DNG/CR2/CR3/NEF/ARW through libraw.
# Without the list a raw file would be developed into an 8-bit JPEG, pass every sanity
# check, and have its original deleted — 14-bit linear sensor data, gone. HEIC is out for a
# different reason: libheif is read-only in this image, so it would be written back as
# JPEG, and HEVC-intra beats JPEG by roughly 2x — the "compressed" file would be larger.
#
# Stills are CPU-only on purpose. A GPU JPEG encoder exists but produces visibly worse
# output at the same size than a competent software encoder, and stills are small enough
# that the wall-clock saving is irrelevant.
#
# ImageMagick rather than cjpegli: Debian and Ubuntu package libjxl-tools *without* the
# cjpegli binary (0.11.2 in trixie ships only cjxl, djxl and jxlinfo). `-auto-orient`
# together with normalize_orientation fixes a real trap — the HEIC decoder may already have
# applied the rotation while the JPEG decoder has not, and copying the source Orientation
# back onto already-upright pixels rotates the image a second time.
#
# Why the flags are what they are:
#   magick, not convert  `convert` is a deprecated alias in ImageMagick 7.
#   -interlace Plane     progressive JPEG. Free: it reorders the same DCT coefficients, so
#                        the decoded pixels are bit-identical (verified with
#                        `compare -metric AE` = 0) while the file shrinks 3-8 %.
#   no -sampling-factor  ImageMagick then *inherits* the source's chroma subsampling
#                        (verified: a 4:4:4 source stays 4:4:4 even at q82). Forcing 4:2:0
#                        would halve chroma resolution on every 4:4:4 source — visible on
#                        saturated edges, and no sanity check would notice.
IMAGE_ENCODER = EncoderSpec(
    encoder="magick",
    label="ImageMagick JPEG",
    match_type="IMAGE",
    suffix=".jpg",
    quality={"higher": 88, "balanced": 82, "smaller": 75},
    exiftool_copy=True,
    normalize_orientation=True,
    timeout_s=900.0,
    extensions=(".jpg", ".jpeg", ".jpe", ".jfif"),
    # A still costs about a second to encode, so the ratio is the wrong economic axis:
    # ratio 0.75 on a 12 MB photo saves 3 MB, ratio 0.60 on a 371 KB photo saves 147 KB.
    # Here max_ratio is only a "something went badly wrong" net, and `min_savings_bytes`
    # from the behavior block decides whether the result is worth an asset at all.
    max_ratio=0.9,
    # Off for stills on purpose: the replacement's timeline position comes from the
    # `fileCreatedAt` sent at upload and from the explicit `dateTimeOriginal` write in
    # step 8, not from the file. Requiring the tag would reject scans and EXIF-stripped
    # exports after a full download and encode, for no gain in correctness.
    require_date_time_original=False,
    # Quantisation error is cumulative. Running a q60 source through the q82 preset was
    # measured at 158 368 -> 190 488 bytes — 20 % *larger*, for a second generation of
    # artefacts. So: leave a source alone unless it sits meaningfully above what this
    # preset would write it back at. Four points above the target in each column, which
    # makes `balanced` the q86 threshold this was measured with; a fixed number instead
    # would make `smaller` (q75) refuse exactly the q76-q85 sources it shrinks best.
    min_source_quality={"higher": 92, "balanced": 86, "smaller": 79},
    template="magick {input} -auto-orient -quality $q -interlace Plane {output}",
)

# `hardware.mode` values that pin a specific hardware encoder.
MODE_ENCODERS: dict[str, str] = {
    "qsv": "hevc_qsv",
    "vaapi": "hevc_vaapi",
    "nvenc": "hevc_nvenc",
}
