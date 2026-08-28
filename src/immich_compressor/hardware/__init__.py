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

Collection (I/O) and decision (pure) are deliberately separate, and that separation is now
the file layout: :mod:`.collection` touches the machine, everything after it is a pure
function of :class:`.facts.HostFacts`, so the ranking can be tested against captured tool
output for hardware nobody has to own.

* :mod:`.facts` — what the machine says about itself, as plain data
* :mod:`.catalog` — the encoders this project knows how to drive, and their flags
* :mod:`.parsing` — pure parsers for what the tools print
* :mod:`.collection` — the half that does the I/O
* :mod:`.ranking` — the choice, and the reason every other candidate lost
* :mod:`.report` — the answer, and how it is explained

This module re-exports the whole public surface, so ``from .hardware import X`` keeps
working exactly as it did when this was one file.
"""

from __future__ import annotations

from .catalog import (
    IMAGE_ENCODER,
    MODE_ENCODERS,
    VIDEO_ENCODERS,
    EncoderSpec,
    HardwareMode,
    QualityLevel,
)
from .collection import (
    CGROUP_CPU_MAX,
    CGROUP_MEMORY_MAX,
    DRI_DIR,
    DRM_CLASS_DIR,
    VENDOR_IDS,
    collect_host_facts,
    read_cpu_budget,
)
from .facts import CpuBudget, HostFacts, RenderNode
from .parsing import (
    parse_cpu_max,
    parse_ffmpeg_encoders,
    parse_memory_max,
    parse_pci_id,
    parse_vainfo,
)
from .ranking import Candidate, CandidateStatus, ProbeFn, confirm_candidates, rank_candidates
from .report import (
    HardwareReport,
    apply_to_settings,
    build_presets,
    detect,
    detect_sync,
    format_report,
)

__all__ = [
    "CGROUP_CPU_MAX",
    "CGROUP_MEMORY_MAX",
    "DRI_DIR",
    "DRM_CLASS_DIR",
    "IMAGE_ENCODER",
    "MODE_ENCODERS",
    "VENDOR_IDS",
    "VIDEO_ENCODERS",
    "Candidate",
    "CandidateStatus",
    "CpuBudget",
    "EncoderSpec",
    "HardwareMode",
    "HardwareReport",
    "HostFacts",
    "ProbeFn",
    "QualityLevel",
    "RenderNode",
    "apply_to_settings",
    "build_presets",
    "collect_host_facts",
    "confirm_candidates",
    "detect",
    "detect_sync",
    "format_report",
    "parse_cpu_max",
    "parse_ffmpeg_encoders",
    "parse_memory_max",
    "parse_pci_id",
    "parse_vainfo",
    "rank_candidates",
    "read_cpu_budget",
]
