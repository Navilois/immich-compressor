"""Pure parsers for what the tools print.

Separate from :mod:`.collection` so that every one of them can be tested against captured
output — which is the only way to cover hardware that is not in the room.
"""

from __future__ import annotations

import re


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
