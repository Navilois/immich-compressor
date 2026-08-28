"""The half that touches the machine: sysfs, ``/dev/dri``, ``vainfo``, ``ffmpeg``.

Everything here does I/O and returns :mod:`.facts`. Nothing here decides anything — that is
:mod:`.ranking`, which is a pure function of what this collected.
"""

from __future__ import annotations

import grp
import logging
import os
import shutil
from pathlib import Path

from ..encoder import run_command
from .facts import CpuBudget, HostFacts, RenderNode
from .parsing import (
    parse_cpu_max,
    parse_ffmpeg_encoders,
    parse_memory_max,
    parse_pci_id,
    parse_vainfo,
)

logger = logging.getLogger(__name__)


# PCI vendor ids, as sysfs reports them under /sys/class/drm/*/device/vendor.
VENDOR_IDS: dict[int, str] = {0x8086: "intel", 0x1002: "amd", 0x10DE: "nvidia"}

DRI_DIR = Path("/dev/dri")
DRM_CLASS_DIR = Path("/sys/class/drm")
CGROUP_CPU_MAX = Path("/sys/fs/cgroup/cpu.max")
CGROUP_MEMORY_MAX = Path("/sys/fs/cgroup/memory.max")


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
