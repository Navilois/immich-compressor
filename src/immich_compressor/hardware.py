"""Hardware detection, the built-in preset catalog, and the choice between them.

The goal is that nobody has to write an ffmpeg command line. On any common homelab box
the service works out which encoder the machine can *actually* run and builds the preset
itself; ``immich-compressor hardware`` explains the choice and every choice it rejected.

Three rules shape the design:

* **Ask the tools, do not guess from tables.** ``ffmpeg -encoders`` says what the binary
  supports, ``vainfo`` says what the driver exposes, sysfs says which chip is present.
  Chip-generation tables go stale; these three do not.
* **Nothing counts as available until a real encode succeeded.** Every surviving candidate
  is confirmed with :func:`~immich_compressor.encoder.probe_hardware_encoder`, a one-frame
  encode through the actual encoder on the actual device. This is what makes the Intel
  Gen9-11 versus Gen12+ split resolve itself: Debian dropped ``libmfx1``, so ``hevc_qsv``
  simply fails its probe on the older chips and ``hevc_vaapi`` is chosen instead.
* **Rejections are documentation.** Every candidate that does not make it keeps the reason
  it did not, in a sentence a user can act on. That list is the most useful output of
  ``immich-compressor hardware``, and it is what a hardware bug report should contain.

Collection (I/O) and decision (pure) are deliberately separate: :func:`collect_host_facts`
touches the machine, everything after it is a pure function of :class:`HostFacts`, so the
ranking can be tested against captured tool output for hardware nobody has to own.
"""

from __future__ import annotations

import asyncio
import grp
import logging
import os
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from string import Template
from typing import Literal

from .config import AssetType, Preset, Settings
from .encoder import probe_hardware_encoder, run_command

logger = logging.getLogger(__name__)

# PCI vendor ids, as sysfs reports them under /sys/class/drm/*/device/vendor.
VENDOR_IDS: dict[int, str] = {0x8086: "intel", 0x1002: "amd", 0x10DE: "nvidia"}

HardwareMode = Literal["auto", "cpu", "qsv", "vaapi", "nvenc"]
# One-frame test encode: (encoder, device) -> None on success, else the reason.
ProbeFn = Callable[[str, str], Awaitable[str | None]]
QualityLevel = Literal["balanced", "higher", "smaller"]

DRI_DIR = Path("/dev/dri")
DRM_CLASS_DIR = Path("/sys/class/drm")
CGROUP_CPU_MAX = Path("/sys/fs/cgroup/cpu.max")
CGROUP_MEMORY_MAX = Path("/sys/fs/cgroup/memory.max")

# VA profile/entrypoint pairs that mean "this driver can encode HEVC".
_HEVC_VA_PROFILES = ("VAProfileHEVCMain", "VAProfileHEVCMain10")
_HEVC_VA_ENTRYPOINTS = ("VAEntrypointEncSlice", "VAEntrypointEncSliceLP")

# `nproc` on a machine with more cores than this is almost certainly a shared host; the
# thread count is capped so one encode cannot take the whole box by default.
_MAX_THREADS = 16


# ---------------------------------------------------------------------------- facts


@dataclass(frozen=True, slots=True)
class RenderNode:
    """One DRM render node, plus what the machine says about it."""

    path: str
    vendor: str  # "intel" | "amd" | "nvidia" | "unknown"
    vendor_id: int | None = None
    device_id: int | None = None
    driver: str | None = None
    group: str | None = None
    gid: int | None = None
    readable: bool = True
    va_pairs: frozenset[str] = frozenset()
    va_error: str | None = None

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def can_encode_hevc_va(self) -> bool:
        """Whether ``vainfo`` reported an HEVC *encode* entrypoint for this device."""
        return any(
            f"{profile}:{entrypoint}" in self.va_pairs
            for profile in _HEVC_VA_PROFILES
            for entrypoint in _HEVC_VA_ENTRYPOINTS
        )

    def describe(self) -> str:
        ids = ""
        if self.vendor_id is not None and self.device_id is not None:
            ids = f" [{self.vendor_id:#06x}:{self.device_id:#06x}]"
        driver = f" via {self.driver}" if self.driver else ""
        return f"{self.path} ({self.vendor}{ids}{driver})"


@dataclass(frozen=True, slots=True)
class CpuBudget:
    """What this *process* may use, which on a container is not what the host has.

    x265 sizes its thread pool from the host core count and ignores the cgroup limit — an
    8-thread pool inside a 2-core container starves everything else on the box, Immich
    included. Reading the real budget here and pinning ``pools``/``-threads`` to it is the
    fix, and it is why this is detected rather than configured.
    """

    cores: float
    source: str
    host_cores: int
    memory_bytes: int | None = None
    memory_source: str | None = None

    @property
    def threads(self) -> int:
        return max(1, min(int(self.cores), _MAX_THREADS))

    @property
    def concurrency(self) -> int:
        """How many encodes may run at once.

        One per eight effective cores, never more than four: Immich's own thumbnailing,
        machine learning and transcoding compete for the same machine, and a compressor
        that wins that fight is a compressor people uninstall.
        """
        return max(1, min(int(self.cores // 8), 4))

    def describe(self) -> str:
        memory = ""
        if self.memory_bytes:
            memory = f", {self.memory_bytes / (1024**3):.1f} GiB memory ({self.memory_source})"
        return f"{self.cores:g} effective core(s) from {self.source} (host has {self.host_cores}){memory}"


@dataclass(frozen=True, slots=True)
class HostFacts:
    """Everything read from the machine. Pure functions below take it from here."""

    render_nodes: tuple[RenderNode, ...] = ()
    nvidia_present: bool = False
    nvidia_source: str | None = None
    ffmpeg_path: str | None = None
    ffmpeg_encoders: frozenset[str] = frozenset()
    ffmpeg_error: str | None = None
    vainfo_path: str | None = None
    cpu: CpuBudget = field(default_factory=lambda: CpuBudget(cores=1.0, source="fallback", host_cores=1))

    def nodes_for(self, vendor: str) -> tuple[RenderNode, ...]:
        return tuple(node for node in self.render_nodes if node.vendor == vendor)


# ------------------------------------------------------------------------- catalog


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
            cmd=self.render(node=node, quality=quality, threads=threads),
            suffix=self.suffix,
            exiftool_copy=self.exiftool_copy,
            normalize_orientation=self.normalize_orientation,
            timeout_s=self.timeout_s,
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

# Stills are CPU-only on purpose. A GPU JPEG encoder exists but produces visibly worse
# output at the same size than a competent software encoder, and stills are small enough
# that the wall-clock saving is irrelevant.
#
# ImageMagick rather than cjpegli: Debian and Ubuntu package libjxl-tools *without* the
# cjpegli binary (0.11.2 in trixie ships only cjxl, djxl and jxlinfo). `-auto-orient`
# together with normalize_orientation fixes a real trap — the HEIC decoder may already have
# applied the rotation while the JPEG decoder has not, and copying the source Orientation
# back onto already-upright pixels rotates the image a second time.
IMAGE_ENCODER = EncoderSpec(
    encoder="convert",
    label="ImageMagick JPEG",
    match_type="IMAGE",
    suffix=".jpg",
    quality={"higher": 88, "balanced": 82, "smaller": 75},
    exiftool_copy=True,
    normalize_orientation=True,
    timeout_s=900.0,
    template="convert {input} -auto-orient -quality $q -sampling-factor 4:2:0 {output}",
)

# `hardware.mode` values that pin a specific hardware encoder.
MODE_ENCODERS: dict[str, str] = {
    "qsv": "hevc_qsv",
    "vaapi": "hevc_vaapi",
    "nvenc": "hevc_nvenc",
}


# ------------------------------------------------------------------------ parsing


def parse_pci_id(text: str) -> int | None:
    """``"0x8086\\n"`` -> ``0x8086``."""
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return int(stripped, 16)
    except ValueError:
        return None


def parse_ffmpeg_encoders(text: str) -> frozenset[str]:
    """Encoder names out of ``ffmpeg -hide_banner -encoders``.

    Lines look like ``` V....D hevc_vaapi           H.265/HEVC (VAAPI)```: six capability
    flags, then the name. Anything before the ``------`` separator is a legend.
    """
    names: set[str] = set()
    body = text.split("------", 1)[-1]
    for line in body.splitlines():
        match = re.match(r"\s*[VASFXBD.]{6}\s+(\S+)", line)
        if match:
            names.add(match.group(1))
    return frozenset(names)


def parse_vainfo(text: str) -> frozenset[str]:
    """``VAProfileHEVCMain : VAEntrypointEncSlice`` pairs, joined with a colon."""
    pairs: set[str] = set()
    for line in text.splitlines():
        match = re.match(r"\s*(VAProfile\S+)\s*:\s*(VAEntrypoint\S+)", line)
        if match:
            pairs.add(f"{match.group(1)}:{match.group(2)}")
    return frozenset(pairs)


def parse_cpu_max(text: str) -> float | None:
    """cgroup v2 ``cpu.max``: ``"200000 100000"`` -> ``2.0``, ``"max 100000"`` -> ``None``.

    ``None`` means "no limit set", which is not the same as "one core" — the caller falls
    back to the host core count rather than throttling itself to nothing.
    """
    parts = text.split()
    if len(parts) != 2 or parts[0] == "max":
        return None
    try:
        quota, period = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if quota <= 0 or period <= 0:
        return None
    return quota / period


def parse_memory_max(text: str) -> int | None:
    stripped = text.strip()
    if not stripped or stripped == "max":
        return None
    try:
        value = int(stripped)
    except ValueError:
        return None
    # Unlimited is reported as a number close to 2**63 on some kernels.
    return value if 0 < value < 2**60 else None


# ---------------------------------------------------------------------- collection


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def read_cpu_budget() -> CpuBudget:
    """The effective CPU and memory budget of this process."""
    host_cores = os.cpu_count() or 1
    cores = float(host_cores)
    source = "nproc"

    raw = _read(CGROUP_CPU_MAX)
    if raw is not None:
        limit = parse_cpu_max(raw)
        if limit is not None:
            cores, source = limit, "cgroup v2 cpu.max"
        else:
            source = "nproc (cgroup v2 sets no cpu limit)"

    memory_bytes = None
    memory_source = None
    raw_memory = _read(CGROUP_MEMORY_MAX)
    if raw_memory is not None:
        memory_bytes = parse_memory_max(raw_memory)
        memory_source = "cgroup v2 memory.max" if memory_bytes else None

    return CpuBudget(
        cores=cores,
        source=source,
        host_cores=host_cores,
        memory_bytes=memory_bytes,
        memory_source=memory_source,
    )


def _node_group(path: Path) -> tuple[str | None, int | None]:
    """The group that owns the render node — the ``RENDER_GID`` a container needs.

    Without that group the container's non-root user gets "Permission denied" opening the
    node, which in the logs is indistinguishable from a broken driver. It is not.
    """
    try:
        gid = path.stat().st_gid
    except OSError:
        return None, None
    try:
        return grp.getgrgid(gid).gr_name, gid
    except (KeyError, OSError):
        return None, gid


def _detect_nvidia() -> tuple[bool, str | None]:
    """NVIDIA does not publish a DRM render node for its proprietary driver's encoder."""
    for device in ("/dev/nvidiactl", "/dev/nvidia0", "/dev/nvidia-uvm"):
        if Path(device).exists():
            return True, device
    if shutil.which("nvidia-smi"):
        return True, "nvidia-smi"
    return False, None


async def _vainfo(node: str, *, vainfo_path: str | None) -> tuple[frozenset[str], str | None]:
    """VA profiles and entrypoints for one device, or the reason we could not ask.

    ``--display drm`` is mandatory on a headless host: plain ``vainfo`` tries X11 first and
    fails with "can't connect to X server", which says nothing at all about the GPU.
    """
    if vainfo_path is None:
        return frozenset(), "vainfo is not installed"
    code, stdout, stderr = await run_command(
        [vainfo_path, "--display", "drm", "--device", node], timeout_s=30.0
    )
    pairs = parse_vainfo(stdout)
    if pairs:
        return pairs, None
    first_line = next((line for line in stderr.strip().splitlines() if line.strip()), "")
    return frozenset(), first_line[:200] or f"vainfo exited {code} without reporting a profile"


async def collect_host_facts() -> HostFacts:
    """Read the machine. Every failure degrades to "unknown", never to an exception."""
    ffmpeg_path = shutil.which("ffmpeg")
    vainfo_path = shutil.which("vainfo")

    encoders: frozenset[str] = frozenset()
    ffmpeg_error: str | None = None
    if ffmpeg_path is None:
        ffmpeg_error = "ffmpeg is not installed"
    else:
        code, stdout, stderr = await run_command([ffmpeg_path, "-hide_banner", "-encoders"], timeout_s=60.0)
        if code == 0:
            encoders = parse_ffmpeg_encoders(stdout)
        else:
            ffmpeg_error = (stderr.strip().splitlines() or ["ffmpeg -encoders failed"])[0][:200]

    nodes: list[RenderNode] = []
    for path in sorted(DRI_DIR.glob("renderD*")) if DRI_DIR.is_dir() else []:
        sysfs = DRM_CLASS_DIR / path.name / "device"
        vendor_id = parse_pci_id(_read(sysfs / "vendor") or "")
        device_id = parse_pci_id(_read(sysfs / "device") or "")
        driver_link = sysfs / "driver"
        driver = driver_link.resolve().name if driver_link.is_symlink() else None
        group, gid = _node_group(path)
        readable = os.access(path, os.R_OK | os.W_OK)
        va_pairs: frozenset[str] = frozenset()
        va_error: str | None = None
        if readable:
            va_pairs, va_error = await _vainfo(str(path), vainfo_path=vainfo_path)
        else:
            va_error = (
                f"cannot open {path}: permission denied. The process needs the "
                f'{group or "render"} group — add `group_add: ["{gid}"]` to the service.'
            )
        nodes.append(
            RenderNode(
                path=str(path),
                vendor=VENDOR_IDS.get(vendor_id or -1, "unknown"),
                vendor_id=vendor_id,
                device_id=device_id,
                driver=driver,
                group=group,
                gid=gid,
                readable=readable,
                va_pairs=va_pairs,
                va_error=va_error,
            )
        )

    nvidia_present, nvidia_source = _detect_nvidia()
    return HostFacts(
        render_nodes=tuple(nodes),
        nvidia_present=nvidia_present,
        nvidia_source=nvidia_source,
        ffmpeg_path=ffmpeg_path,
        ffmpeg_encoders=encoders,
        ffmpeg_error=ffmpeg_error,
        vainfo_path=vainfo_path,
        cpu=read_cpu_budget(),
    )


# ------------------------------------------------------------------------ ranking


CandidateStatus = Literal["selected", "rejected", "not_tried"]


@dataclass(frozen=True, slots=True)
class Candidate:
    """One encoder on one device, and what became of it."""

    encoder: str
    label: str
    device: str | None
    spec: EncoderSpec
    status: CandidateStatus = "not_tried"
    reason: str | None = None

    def where(self) -> str:
        return f"{self.encoder} on {self.device}" if self.device else self.encoder

    def with_status(self, status: CandidateStatus, reason: str | None = None) -> Candidate:
        return Candidate(
            encoder=self.encoder,
            label=self.label,
            device=self.device,
            spec=self.spec,
            status=status,
            reason=reason,
        )


def _static_rejection(spec: EncoderSpec, facts: HostFacts, mode: str, render_node: str) -> str | None:
    """Why this encoder is out before any device is even considered."""
    if mode == "cpu" and spec.is_hardware:
        return "hardware.mode is 'cpu', so hardware encoders were not considered"
    if mode in MODE_ENCODERS and spec.is_hardware and spec.encoder != MODE_ENCODERS[mode]:
        return f"hardware.mode is {mode!r}, so only {MODE_ENCODERS[mode]} was considered"
    if facts.ffmpeg_path is None:
        return f"{facts.ffmpeg_error} — no encoder could be checked"
    if spec.encoder not in facts.ffmpeg_encoders:
        return f"this ffmpeg build has no {spec.encoder} encoder"
    if spec.encoder == "hevc_nvenc" and not facts.nvidia_present:
        return (
            "no NVIDIA device found: /dev/nvidia* is absent and nvidia-smi is not on PATH. "
            "Add the NVIDIA container runtime (docker-compose.gpu-nvidia.yaml) if the host "
            "has a card."
        )
    if spec.is_hardware and spec.encoder != "hevc_nvenc":
        vendors = ", ".join(spec.vendors)
        if not facts.render_nodes:
            return (
                "no DRM render node under /dev/dri. Pass the device through with "
                "docker-compose.gpu.yaml if the host has a GPU."
            )
        if render_node != "auto" and all(node.path != render_node for node in facts.render_nodes):
            return f"hardware.render_node is pinned to {render_node}, which does not exist"
        if not any(node.vendor in spec.vendors for node in facts.render_nodes):
            found = ", ".join(sorted({node.vendor for node in facts.render_nodes}))
            return f"needs a {vendors} GPU; the render nodes present are {found}"
    return None


def _device_rejection(spec: EncoderSpec, node: RenderNode) -> str | None:
    """Why this encoder is out *on this particular device*."""
    if not node.readable:
        return node.va_error
    if spec.needs_va_encode and not node.can_encode_hevc_va:
        if node.va_error:
            return f"vainfo could not query {node.path}: {node.va_error}"
        return (
            f"vainfo reports no HEVC encode entrypoint on {node.path} "
            f"(VAProfileHEVCMain : VAEntrypointEncSlice is missing), so this chip cannot "
            "encode HEVC through VAAPI"
        )
    return None


def rank_candidates(
    facts: HostFacts, *, mode: HardwareMode = "auto", render_node: str = "auto"
) -> list[Candidate]:
    """Every candidate in preference order, each already carrying its static verdict.

    Nothing here runs a subprocess: given the same :class:`HostFacts` this always produces
    the same answer, which is what makes the whole ranking testable for hardware nobody in
    the room owns.
    """
    candidates: list[Candidate] = []
    for spec in VIDEO_ENCODERS:
        static = _static_rejection(spec, facts, mode, render_node)
        if static is not None:
            candidates.append(
                Candidate(
                    encoder=spec.encoder,
                    label=spec.label,
                    device=None,
                    spec=spec,
                    status="rejected",
                    reason=static,
                )
            )
            continue

        if not spec.is_hardware or spec.encoder == "hevc_nvenc":
            candidates.append(Candidate(encoder=spec.encoder, label=spec.label, device=None, spec=spec))
            continue

        for node in facts.render_nodes:
            if node.vendor not in spec.vendors:
                continue
            if render_node != "auto" and node.path != render_node:
                continue
            reason = _device_rejection(spec, node)
            candidates.append(
                Candidate(
                    encoder=spec.encoder,
                    label=spec.label,
                    device=node.path,
                    spec=spec,
                    status="rejected" if reason else "not_tried",
                    reason=reason,
                )
            )
    return candidates


async def confirm_candidates(
    candidates: list[Candidate],
    *,
    probe: ProbeFn | None = None,
) -> list[Candidate]:
    """Run the one-frame encode down the ranked list and stop at the first that works.

    An encoder that ``ffmpeg -encoders`` lists and ``vainfo`` blesses can still fail: the
    driver may be missing inside the container, the render node may not be openable, or the
    chip may simply not implement the codec. Only a real encode settles it, and doing it
    here means the first *job* is not the thing that discovers the problem an hour later.
    """
    run_probe = probe if probe is not None else probe_hardware_encoder
    settled: list[Candidate] = []
    chosen: Candidate | None = None
    for candidate in candidates:
        if candidate.status == "rejected":
            settled.append(candidate)
            continue
        if chosen is not None:
            settled.append(candidate.with_status("not_tried", f"{chosen.where()} was selected first"))
            continue
        if not candidate.spec.is_hardware:
            chosen = candidate.with_status("selected", "the universal fallback")
            settled.append(chosen)
            continue
        problem = await run_probe(candidate.encoder, candidate.device or "")
        if problem:
            settled.append(candidate.with_status("rejected", f"the one-frame test encode failed: {problem}"))
            continue
        chosen = candidate.with_status("selected", "the one-frame test encode succeeded")
        settled.append(chosen)
    return settled


# ------------------------------------------------------------------------- report


@dataclass(frozen=True, slots=True)
class HardwareReport:
    """What was found, what was chosen, and why everything else was not."""

    facts: HostFacts
    candidates: list[Candidate]
    presets: list[Preset]
    mode: HardwareMode
    quality: QualityLevel
    render_node: str
    concurrency: int
    explicit_presets: bool = False

    @property
    def selected(self) -> Candidate | None:
        return next((c for c in self.candidates if c.status == "selected"), None)

    @property
    def rejected(self) -> list[Candidate]:
        return [c for c in self.candidates if c.status == "rejected"]

    @property
    def uses_gpu(self) -> bool:
        selected = self.selected
        return bool(selected and selected.spec.is_hardware)

    def preset_for(self, asset_type: str) -> Preset | None:
        return next((p for p in self.presets if p.match_type == asset_type), None)

    def summary_line(self) -> str:
        """The one line the service logs at startup."""
        if self.explicit_presets:
            names = ", ".join(p.name for p in self.presets) or "none"
            return f"encoder: presets from config.yaml ({names}) — autodetection not used"
        selected = self.selected
        if selected is None:
            # Never fatal: a GPU that vanished, or an ffmpeg that is not on PATH, must not
            # keep the service from starting. The CPU preset is built regardless and the
            # job fails loudly if it really cannot run.
            return (
                "encoder: no candidate could be confirmed — falling back to the CPU preset. "
                "Run `immich-compressor hardware` for the reason each one was rejected."
            )
        where = f" on {selected.device}" if selected.device else ""
        return (
            f"encoder: {selected.label} ({selected.encoder}{where}), quality={self.quality}, "
            f"{self.facts.cpu.threads} thread(s), concurrency={self.concurrency}"
        )

    def pin_yaml(self) -> str:
        """The config.yaml snippet that pins exactly what was detected."""
        selected = self.selected
        mode = "cpu"
        if selected is not None and selected.spec.is_hardware:
            mode = next((name for name, enc in MODE_ENCODERS.items() if enc == selected.encoder), "auto")
        node = selected.device if selected and selected.device else "auto"
        return (
            "hardware:\n"
            f"  mode: {mode}\n"
            f"  render_node: {node}\n"
            "behavior:\n"
            f"  quality: {self.quality}\n"
            f"  concurrency: {self.concurrency}\n"
        )

    def calibrate_hint(self) -> str:
        selected = self.selected
        encoder = selected.encoder if selected else "libx265"
        return f"ENCODER={encoder} scripts/calibrate.sh /path/to/your/clip.mov"

    def to_dict(self) -> dict[str, object]:
        """The ``--json`` shape. Also what a hardware bug report should carry."""
        return {
            "mode": self.mode,
            "quality": self.quality,
            "render_node": self.render_node,
            "explicit_presets": self.explicit_presets,
            "cpu": {
                "effective_cores": self.facts.cpu.cores,
                "source": self.facts.cpu.source,
                "host_cores": self.facts.cpu.host_cores,
                "memory_bytes": self.facts.cpu.memory_bytes,
                "threads": self.facts.cpu.threads,
                "concurrency": self.concurrency,
            },
            "ffmpeg": {
                "path": self.facts.ffmpeg_path,
                "error": self.facts.ffmpeg_error,
                "hevc_encoders": sorted(
                    name for name in self.facts.ffmpeg_encoders if "hevc" in name or "265" in name
                ),
            },
            "nvidia": {
                "present": self.facts.nvidia_present,
                "source": self.facts.nvidia_source,
            },
            "render_nodes": [
                {
                    "path": node.path,
                    "vendor": node.vendor,
                    "vendor_id": None if node.vendor_id is None else f"{node.vendor_id:#06x}",
                    "device_id": None if node.device_id is None else f"{node.device_id:#06x}",
                    "driver": node.driver,
                    "group": node.group,
                    "gid": node.gid,
                    "readable": node.readable,
                    "hevc_encode_entrypoint": node.can_encode_hevc_va,
                    "va_error": node.va_error,
                }
                for node in self.facts.render_nodes
            ],
            "candidates": [
                {
                    "encoder": c.encoder,
                    "label": c.label,
                    "device": c.device,
                    "status": c.status,
                    "reason": c.reason,
                }
                for c in self.candidates
            ],
            "presets": [
                {"name": p.name, "type": p.match_type, "cmd": p.cmd, "suffix": p.suffix} for p in self.presets
            ],
        }


def build_presets(
    report_candidates: list[Candidate],
    *,
    enabled_types: list[str],
    quality: QualityLevel,
    threads: int,
) -> list[Preset]:
    """Turn the winning candidate into the presets the pipeline consumes."""
    selected = next((c for c in report_candidates if c.status == "selected"), None)
    presets: list[Preset] = []
    for asset_type in enabled_types:
        if asset_type == "VIDEO":
            spec = selected.spec if selected else VIDEO_ENCODERS[-1]
            device = selected.device if selected else None
            presets.append(
                spec.build(
                    node=device,
                    quality=quality,
                    threads=threads,
                    name=f"auto-video-{spec.encoder.replace('_', '-')}",
                )
            )
        elif asset_type == "IMAGE":
            presets.append(
                IMAGE_ENCODER.build(node=None, quality=quality, threads=threads, name="auto-image-jpeg")
            )
        else:
            raise ValueError(
                f"there is no built-in preset for asset type {asset_type} — write one "
                "explicitly under `presets:` in config.yaml, or drop the type from "
                "behavior.enabled_types"
            )
    return presets


async def detect(
    *,
    mode: HardwareMode = "auto",
    render_node: str = "auto",
    quality: QualityLevel = "balanced",
    enabled_types: list[str] | None = None,
    facts: HostFacts | None = None,
    probe: ProbeFn | None = None,
) -> HardwareReport:
    """Full detection: read the machine, rank, confirm, and build the presets."""
    collected = facts if facts is not None else await collect_host_facts()
    candidates = await confirm_candidates(
        rank_candidates(collected, mode=mode, render_node=render_node), probe=probe
    )
    types = enabled_types if enabled_types is not None else ["VIDEO"]
    presets = build_presets(candidates, enabled_types=types, quality=quality, threads=collected.cpu.threads)
    selected = next((c for c in candidates if c.status == "selected"), None)
    # A GPU has exactly one fixed-function encode block, and Immich's own transcoding
    # competes for it. Running two encodes against it is slower than running one.
    concurrency = 1 if (selected and selected.spec.is_hardware) else collected.cpu.concurrency
    return HardwareReport(
        facts=collected,
        candidates=candidates,
        presets=presets,
        mode=mode,
        quality=quality,
        render_node=render_node,
        concurrency=concurrency,
    )


def detect_sync(**kwargs: object) -> HardwareReport:
    """:func:`detect` from synchronous code, including from inside a running loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(detect(**kwargs))  # type: ignore[arg-type]

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(detect(**kwargs))).result()  # type: ignore[arg-type]


def format_report(report: HardwareReport) -> str:
    """The human-readable ``immich-compressor hardware`` output."""
    facts = report.facts
    lines: list[str] = ["=== immich-compressor hardware report ===", ""]

    lines.append(f"CPU budget:   {facts.cpu.describe()}")
    lines.append(f"              -> {facts.cpu.threads} encoder thread(s), concurrency {report.concurrency}")
    lines.append(
        f"ffmpeg:       {facts.ffmpeg_path or 'not found'}"
        + (f" ({facts.ffmpeg_error})" if facts.ffmpeg_error else "")
    )
    lines.append(f"vainfo:       {facts.vainfo_path or 'not found'}")
    lines.append(
        "NVIDIA:       "
        + (f"present (found via {facts.nvidia_source})" if facts.nvidia_present else "not present")
    )
    lines.append("")

    lines.append("Render nodes:")
    if not facts.render_nodes:
        lines.append("  none under /dev/dri — CPU encoding only")
    for node in facts.render_nodes:
        lines.append(f"  {node.describe()}")
        # The group name is often absent inside the container even though the gid is
        # right — the image has no matching /etc/group entry. The gid is what compose
        # needs, so lead with it.
        owner = f"gid {node.gid}" if node.gid is not None else "unknown owner"
        if node.group:
            owner += f" ({node.group})"
        lines.append(
            f"    owned by {owner}, "
            f"{'readable by this process' if node.readable else 'NOT readable by this process'}"
        )
        if node.can_encode_hevc_va:
            lines.append("    vainfo: HEVC encode entrypoint present")
        elif node.va_error:
            lines.append(f"    vainfo: {node.va_error}")
        else:
            lines.append("    vainfo: no HEVC encode entrypoint")
    lines.append("")

    if report.explicit_presets:
        lines.append("Encoder choice: config.yaml declares `presets:`, so nothing was detected.")
        lines.append("Remove that block to let the service choose for you.")
    else:
        selected = report.selected
        lines.append("Encoder choice:")
        if selected is None:
            lines.append(
                "  NONE confirmed — the CPU preset below is used anyway so the service still starts."
            )
            lines.append("  Fix one of the reasons underneath to get hardware encoding.")
        else:
            where = f" on {selected.device}" if selected.device else ""
            lines.append(f"  SELECTED  {selected.label} — {selected.encoder}{where}")
            lines.append(f"            {selected.reason}")
        for candidate in report.candidates:
            if candidate.status == "selected":
                continue
            mark = "rejected " if candidate.status == "rejected" else "not tried"
            lines.append(f"  {mark} {candidate.where()}")
            lines.append(f"            {candidate.reason}")
    lines.append("")

    lines.append("Presets in use:")
    for preset in report.presets:
        lines.append(f"  {preset.name} ({preset.match_type}) -> {preset.suffix}")
        lines.append(f"    {preset.cmd}")
    lines.append("")

    lines.append("To pin this choice, put it in config.yaml:")
    lines.extend(f"  {line}" for line in report.pin_yaml().rstrip("\n").splitlines())
    lines.append("")
    lines.append("To tune the quality number against your own footage:")
    lines.append(f"  {report.calibrate_hint()}")
    return "\n".join(lines)


def apply_to_settings(settings: Settings, *, always_detect: bool = False) -> tuple[Settings, HardwareReport]:
    """Resolve the encoder question against a loaded configuration.

    Resolution order, highest first:

    1. ``presets:`` written by hand in ``config.yaml`` — always wins, and detection is
       skipped entirely so an upgrade cannot change what an existing deployment does.
    2. ``hardware.mode`` when it is pinned to something other than ``auto``.
    3. autodetection.
    4. the CPU preset, which is always the last candidate and always available.

    ``behavior.concurrency`` is derived from the CPU budget only when the configuration
    does not set it: an explicit value, from the file or from the environment, is the
    user's decision and is left alone. ``always_detect`` makes the report describe the
    machine even when the configuration overrides what it found, which is what
    ``immich-compressor hardware`` wants to show.
    """

    def _detect() -> HardwareReport:
        return detect_sync(
            mode=settings.hardware.mode,
            render_node=settings.hardware.render_node,
            quality=settings.behavior.quality,
            enabled_types=list(settings.behavior.enabled_types),
        )

    if settings.presets:
        detected = _detect() if always_detect else None
        return settings, HardwareReport(
            facts=detected.facts if detected else HostFacts(),
            candidates=detected.candidates if detected else [],
            presets=list(settings.presets),
            mode=settings.hardware.mode,
            quality=settings.behavior.quality,
            render_node=settings.hardware.render_node,
            concurrency=settings.behavior.concurrency,
            explicit_presets=True,
        )

    report = _detect()
    if "concurrency" in settings.behavior.model_fields_set:
        report = replace(report, concurrency=settings.behavior.concurrency)
        return settings.model_copy(update={"presets": report.presets}), report

    behavior = settings.behavior.model_copy(update={"concurrency": report.concurrency})
    return settings.model_copy(update={"presets": report.presets, "behavior": behavior}), report
