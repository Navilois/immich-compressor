"""The answer, and the way it is explained.

:class:`HardwareReport` is what `immich-compressor hardware` prints and what `setup` writes
a config from. It carries the rejected candidates as well as the chosen one, because that
list is the most useful output of the command and what a hardware bug report should contain.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace

from ..config import Preset, Settings
from .catalog import IMAGE_ENCODER, MODE_ENCODERS, VIDEO_ENCODERS, HardwareMode, QualityLevel
from .collection import collect_host_facts
from .facts import HostFacts
from .ranking import Candidate, ProbeFn, confirm_candidates, rank_candidates

logger = logging.getLogger(__name__)


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
        """How to measure the quality number on this machine's own footage."""
        selected = self.selected
        encoder = selected.encoder if selected else "libx265"
        threads = self.facts.cpu.threads
        # THREADS in both, or the container variant falls back to calibrate.sh's own
        # default of 2 and the sweep is measured against half the threads the encoder will
        # really get — which tunes the quality number against the wrong machine.
        return (
            f"ENCODER={encoder} THREADS={threads} scripts/calibrate.sh /path/to/your/clip.mov\n"
            f"  in the container:  docker compose exec -e ENCODER={encoder} -e THREADS={threads} "
            "immich-compressor scripts/calibrate.sh /path/clip.mov"
        )

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
                {
                    "name": p.name,
                    "type": p.match_type,
                    "cmd": p.cmd,
                    "suffix": p.suffix,
                    "extensions": p.extensions,
                }
                for p in self.presets
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
        # The stills preset only accepts JPEG, and that allowlist is the difference
        # between skipping a raw file and developing it into an 8-bit JPEG. Say so.
        if preset.extensions:
            lines.append(f"    accepts only {', '.join(preset.extensions)}")
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
