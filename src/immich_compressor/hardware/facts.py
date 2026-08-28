"""What the machine says about itself: render nodes, and the budget this process has.

Plain data, with no I/O of its own — :mod:`.collection` is what touches the machine and
fills these in. Keeping the two apart is what lets the ranking be tested against captured
tool output for hardware nobody has to own.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


# VA profile/entrypoint pairs that mean "this driver can encode HEVC".
_HEVC_VA_PROFILES = ("VAProfileHEVCMain", "VAProfileHEVCMain10")
_HEVC_VA_ENTRYPOINTS = ("VAEntrypointEncSlice", "VAEntrypointEncSliceLP")

# `nproc` on a machine with more cores than this is almost certainly a shared host; the
# thread count is capped so one encode cannot take the whole box by default.
_MAX_THREADS = 16


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
