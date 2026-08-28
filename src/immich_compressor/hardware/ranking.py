"""Choosing an encoder, and keeping the reason every other candidate lost.

A pure function of :mod:`.facts` and the :mod:`.catalog`, up to the final confirmation —
which runs a real one-frame encode, because nothing counts as available until one has
succeeded.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from ..encoder import probe_hardware_encoder
from .catalog import MODE_ENCODERS, VIDEO_ENCODERS, EncoderSpec, HardwareMode
from .facts import HostFacts, RenderNode

logger = logging.getLogger(__name__)


# One-frame test encode: (encoder, device) -> None on success, else the reason.
ProbeFn = Callable[[str, str], Awaitable[str | None]]


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
            "encode HEVC through VAAPI. Note that Immich's own transcoding uses H.264, "
            "which is a different entrypoint — a chip can do one and not the other, so a "
            "GPU that works for Immich can still land here"
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
