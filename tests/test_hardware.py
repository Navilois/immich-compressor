"""Hardware detection: the ranking must be right for machines nobody here owns.

Collection (I/O) and decision (pure) are separate in ``hardware.py`` precisely so this
suite can build a :class:`HostFacts` for an Intel Gen9, an Intel Gen12, an AMD card, an
NVIDIA card and a headless CPU-only box and assert what each of them gets. **No test here
touches a GPU.**

Where a fixture is real it says so. ``ffmpeg-encoders-debian-trixie.txt``,
``vainfo-intel-gen9-uhd630.txt``, ``vainfo-intel-gen9-decode-only.txt`` and
``qsv-probe-failure-gen9.txt`` are verbatim captures from the project's own image on an
Intel UHD 630 (Coffee Lake, 0x8086:0x3e98). The per-machine ``HostFacts`` below are
constructed, because what separates a Gen9 from a Gen12 for our purposes is not the shape
of vainfo's output — it is whether the QSV probe succeeds.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from immich_compressor.config import Settings
from immich_compressor.encoder import first_diagnostic_line
from immich_compressor.hardware import (
    IMAGE_ENCODER,
    VIDEO_ENCODERS,
    Candidate,
    CpuBudget,
    HostFacts,
    RenderNode,
    apply_to_settings,
    build_presets,
    confirm_candidates,
    detect,
    format_report,
    parse_cpu_max,
    parse_ffmpeg_encoders,
    parse_memory_max,
    parse_pci_id,
    parse_vainfo,
    rank_candidates,
)

FIXTURES = Path(__file__).parent / "fixtures" / "hardware"

HEVC_ENCODE_PAIRS = frozenset(
    {
        "VAProfileHEVCMain:VAEntrypointVLD",
        "VAProfileHEVCMain:VAEntrypointEncSlice",
        "VAProfileHEVCMain10:VAEntrypointEncSlice",
    }
)
HEVC_DECODE_ONLY_PAIRS = frozenset({"VAProfileHEVCMain:VAEntrypointVLD"})


def _encoders() -> frozenset[str]:
    return parse_ffmpeg_encoders((FIXTURES / "ffmpeg-encoders-debian-trixie.txt").read_text())


def _budget(cores: float = 2.0) -> CpuBudget:
    return CpuBudget(cores=cores, source="cgroup v2 cpu.max", host_cores=8)


def _node(
    vendor: str = "intel",
    *,
    device_id: int = 0x3E98,
    va_pairs: frozenset[str] = HEVC_ENCODE_PAIRS,
    path: str = "/dev/dri/renderD128",
    readable: bool = True,
    va_error: str | None = None,
) -> RenderNode:
    vendor_ids = {"intel": 0x8086, "amd": 0x1002, "nvidia": 0x10DE}
    return RenderNode(
        path=path,
        vendor=vendor,
        vendor_id=vendor_ids.get(vendor),
        device_id=device_id,
        driver={"intel": "i915", "amd": "amdgpu"}.get(vendor),
        group="render",
        gid=992,
        readable=readable,
        va_pairs=va_pairs,
        va_error=va_error,
    )


def _facts(**overrides: object) -> HostFacts:
    base: dict[str, object] = {
        "render_nodes": (),
        "nvidia_present": False,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "ffmpeg_encoders": _encoders(),
        "vainfo_path": "/usr/bin/vainfo",
        "cpu": _budget(),
    }
    base.update(overrides)
    return HostFacts(**base)  # type: ignore[arg-type]


# The five machines this project claims to support.
INTEL_GEN9 = _facts(render_nodes=(_node("intel", device_id=0x3E98),))
INTEL_GEN12 = _facts(render_nodes=(_node("intel", device_id=0x46A6),))
AMD = _facts(render_nodes=(_node("amd", device_id=0x1638),))
NVIDIA = _facts(nvidia_present=True, nvidia_source="/dev/nvidiactl")
CPU_ONLY = _facts()


def _probe_stub(*failing: str):
    """A stand-in for the one-frame encode: the named encoders fail, the rest succeed."""

    async def probe(encoder: str, device: str) -> str | None:
        return f"{encoder} did not work" if encoder in failing else None

    return probe


async def _select(facts: HostFacts, *failing: str, **kwargs: object) -> list[Candidate]:
    return await confirm_candidates(
        rank_candidates(facts, **kwargs),  # type: ignore[arg-type]
        probe=_probe_stub(*failing),
    )


def _selected(candidates: list[Candidate]) -> Candidate | None:
    return next((c for c in candidates if c.status == "selected"), None)


def _reason_for(candidates: list[Candidate], encoder: str) -> str:
    return next(c.reason or "" for c in candidates if c.encoder == encoder)


# --------------------------------------------------------------------------- parsing


def test_parse_ffmpeg_encoders_reads_the_real_output() -> None:
    encoders = _encoders()
    assert {"libx265", "hevc_qsv", "hevc_vaapi", "hevc_nvenc"} <= encoders
    # The legend above the separator must not leak in as encoder names.
    assert "encoders:" not in encoders
    assert "V....." not in encoders


def test_parse_vainfo_finds_the_hevc_encode_entrypoint() -> None:
    """Captured inside this project's image, with the non-free iHD driver."""
    pairs = parse_vainfo((FIXTURES / "vainfo-intel-gen9-uhd630.txt").read_text())
    assert "VAProfileHEVCMain:VAEntrypointEncSlice" in pairs
    assert "VAProfileHEVCMain:VAEntrypointVLD" in pairs


def test_parse_vainfo_distinguishes_decode_only() -> None:
    """The same chip on the host, with a driver stack that only decodes HEVC.

    This is why detection has to run inside the container and why a chip-generation table
    is not enough: same silicon, different answer.
    """
    pairs = parse_vainfo((FIXTURES / "vainfo-intel-gen9-decode-only.txt").read_text())
    assert "VAProfileHEVCMain:VAEntrypointVLD" in pairs
    assert "VAProfileHEVCMain:VAEntrypointEncSlice" not in pairs


@pytest.mark.parametrize(
    ("text", "expected"),
    [("200000 100000", 2.0), ("max 100000", None), ("50000 100000", 0.5), ("garbage", None)],
)
def test_parse_cpu_max(text: str, expected: float | None) -> None:
    assert parse_cpu_max(text) == expected


def test_parse_cpu_max_reads_the_captured_cgroup_files() -> None:
    assert parse_cpu_max((FIXTURES / "cgroup-cpu-max-2cores.txt").read_text()) == 2.0
    assert parse_cpu_max((FIXTURES / "cgroup-cpu-max-unlimited.txt").read_text()) is None


def test_parse_memory_max() -> None:
    assert parse_memory_max("2147483648") == 2 * 1024**3
    assert parse_memory_max("max") is None
    assert parse_memory_max("9223372036854771712") is None


def test_parse_pci_id() -> None:
    assert parse_pci_id("0x8086\n") == 0x8086
    assert parse_pci_id("") is None
    assert parse_pci_id("not-a-number") is None


# ---------------------------------------------------------------------- cpu budget


def test_the_cgroup_limit_beats_the_host_core_count() -> None:
    """x265 sizes its pool from the host and starves the box; this is the fix."""
    budget = CpuBudget(cores=2.0, source="cgroup v2 cpu.max", host_cores=8)
    assert budget.threads == 2
    assert budget.concurrency == 1


@pytest.mark.parametrize(
    ("cores", "threads", "concurrency"),
    [(0.5, 1, 1), (1.0, 1, 1), (2.0, 2, 1), (8.0, 8, 1), (16.0, 16, 2), (64.0, 16, 4)],
)
def test_thread_and_concurrency_budget(cores: float, threads: int, concurrency: int) -> None:
    budget = CpuBudget(cores=cores, source="test", host_cores=64)
    assert budget.threads == threads
    assert budget.concurrency == concurrency


# ------------------------------------------------------------------ the five machines


async def test_intel_gen12_gets_quick_sync() -> None:
    candidates = await _select(INTEL_GEN12)
    selected = _selected(candidates)
    assert selected is not None
    assert selected.encoder == "hevc_qsv"
    assert selected.device == "/dev/dri/renderD128"
    # VAAPI would also work here, but there is no reason to probe it once QSV is in.
    assert _reason_for(candidates, "hevc_vaapi").startswith("hevc_qsv on")


async def test_intel_gen9_falls_back_to_vaapi_when_qsv_fails() -> None:
    """The whole point of probing instead of consulting a table.

    Debian trixie dropped libmfx1, so oneVPL cannot open a session on Gen9-11 and the real
    failure is `Error creating a MFX session: -9` — captured from this project's image on
    an Intel UHD 630. VAAPI on the same chip and the same device works.
    """
    mfx_error = first_diagnostic_line((FIXTURES / "qsv-probe-failure-gen9.txt").read_text())
    assert mfx_error == "Error creating a MFX session: -9."

    async def probe(encoder: str, device: str) -> str | None:
        return mfx_error if encoder == "hevc_qsv" else None

    candidates = await confirm_candidates(rank_candidates(INTEL_GEN9), probe=probe)
    selected = _selected(candidates)
    assert selected is not None
    assert selected.encoder == "hevc_vaapi"
    assert "MFX session" in _reason_for(candidates, "hevc_qsv")
    assert "one-frame test encode failed" in _reason_for(candidates, "hevc_qsv")


async def test_amd_gets_vaapi_and_is_never_offered_quick_sync() -> None:
    candidates = await _select(AMD)
    selected = _selected(candidates)
    assert selected is not None
    assert selected.encoder == "hevc_vaapi"
    assert "needs a intel GPU" in _reason_for(candidates, "hevc_qsv")


async def test_nvidia_gets_nvenc_without_a_render_node() -> None:
    candidates = await _select(NVIDIA)
    selected = _selected(candidates)
    assert selected is not None
    assert selected.encoder == "hevc_nvenc"
    assert selected.device is None
    assert "no DRM render node" in _reason_for(candidates, "hevc_vaapi")


async def test_a_headless_cpu_only_box_still_gets_a_working_preset() -> None:
    candidates = await _select(CPU_ONLY)
    selected = _selected(candidates)
    assert selected is not None
    assert selected.encoder == "libx265"
    assert "no NVIDIA device found" in _reason_for(candidates, "hevc_nvenc")


# -------------------------------------------------------------------- failure modes


async def test_every_gpu_failing_its_probe_still_leaves_a_usable_service() -> None:
    """Startup must never fail because a GPU disappeared."""
    candidates = await _select(INTEL_GEN12, "hevc_qsv", "hevc_vaapi", "hevc_nvenc")
    selected = _selected(candidates)
    assert selected is not None
    assert selected.encoder == "libx265"


async def test_a_decode_only_chip_is_rejected_before_anything_is_probed() -> None:
    facts = _facts(render_nodes=(_node("intel", va_pairs=HEVC_DECODE_ONLY_PAIRS),))
    candidates = rank_candidates(facts)
    reason = _reason_for(candidates, "hevc_vaapi")
    assert "no HEVC encode entrypoint" in reason
    assert "VAEntrypointEncSlice" in reason
    # The predictable follow-up question is "but my GPU works for Immich". Immich
    # transcodes H.264, which is a different entrypoint, so answer it in the reason
    # itself rather than waiting for the issue.
    assert "H.264" in reason


async def test_an_unreadable_render_node_names_the_group_to_add() -> None:
    """ "Permission denied" on renderD128 reads like a driver bug. It is a group problem."""
    node = _node(
        "intel",
        readable=False,
        va_pairs=frozenset(),
        va_error=(
            "cannot open /dev/dri/renderD128: permission denied. The process needs the "
            'render group — add `group_add: ["992"]` to the service.'
        ),
    )
    candidates = rank_candidates(_facts(render_nodes=(node,)))
    assert "group_add" in _reason_for(candidates, "hevc_vaapi")


def test_a_missing_ffmpeg_rejects_everything_with_one_clear_reason() -> None:
    facts = _facts(ffmpeg_path=None, ffmpeg_encoders=frozenset(), ffmpeg_error="ffmpeg is not installed")
    candidates = rank_candidates(facts)
    assert all(c.status == "rejected" for c in candidates)
    assert all("ffmpeg is not installed" in (c.reason or "") for c in candidates)


def test_an_ffmpeg_without_the_encoder_says_so() -> None:
    facts = _facts(render_nodes=(_node("intel"),), ffmpeg_encoders=frozenset({"libx265"}))
    assert "no hevc_qsv encoder" in _reason_for(rank_candidates(facts), "hevc_qsv")


# ------------------------------------------------------------------------- pinning


async def test_mode_cpu_never_considers_a_gpu() -> None:
    candidates = await _select(INTEL_GEN12, mode="cpu")
    selected = _selected(candidates)
    assert selected is not None and selected.encoder == "libx265"
    assert "hardware.mode is 'cpu'" in _reason_for(candidates, "hevc_qsv")


async def test_mode_vaapi_skips_quick_sync_even_where_it_would_work() -> None:
    candidates = await _select(INTEL_GEN12, mode="vaapi")
    selected = _selected(candidates)
    assert selected is not None and selected.encoder == "hevc_vaapi"
    assert "only hevc_vaapi was considered" in _reason_for(candidates, "hevc_qsv")


async def test_a_pinned_mode_that_fails_still_falls_back_to_cpu() -> None:
    """Pinning a GPU is a preference, not a promise the machine can keep."""
    candidates = await _select(INTEL_GEN12, "hevc_qsv", mode="qsv")
    selected = _selected(candidates)
    assert selected is not None and selected.encoder == "libx265"
    assert "one-frame test encode failed" in _reason_for(candidates, "hevc_qsv")


def test_a_pinned_render_node_that_does_not_exist_is_explained() -> None:
    candidates = rank_candidates(INTEL_GEN12, render_node="/dev/dri/renderD129")
    assert "pinned to /dev/dri/renderD129, which does not exist" in _reason_for(candidates, "hevc_qsv")


def test_a_pinned_render_node_selects_only_that_device() -> None:
    facts = _facts(
        render_nodes=(
            _node("intel", path="/dev/dri/renderD128"),
            _node("intel", path="/dev/dri/renderD129"),
        )
    )
    devices = [c.device for c in rank_candidates(facts, render_node="/dev/dri/renderD129")]
    assert "/dev/dri/renderD128" not in devices
    assert "/dev/dri/renderD129" in devices


# ------------------------------------------------------------------------- presets


@pytest.mark.parametrize("spec", [*VIDEO_ENCODERS, IMAGE_ENCODER])
@pytest.mark.parametrize("quality", ["higher", "balanced", "smaller"])
def test_every_catalog_entry_builds_a_valid_preset(spec: object, quality: str) -> None:
    """Preset validation is the same fail-fast gate a hand-written preset goes through."""
    preset = spec.build(node="/dev/dri/renderD128", quality=quality, threads=2, name="p")  # type: ignore[attr-defined]
    argv = preset.argv(Path("/tmp/in.mov"), Path("/tmp/out"))
    assert "/tmp/in.mov" in argv
    assert "/tmp/out" in argv
    # Both placeholders were substituted, and nothing shell-like survived.
    assert not any("{input}" in token or "{output}" in token for token in argv)
    assert not any(token in {"|", ">", "<", ";", "&&"} for token in argv)


def test_balanced_reproduces_the_numbers_this_project_shipped_before() -> None:
    """Upgrading must not silently change anybody's output quality.

    crf 26 for x265 and quality 82 for the stills preset are what config.example.yaml
    carried in 1.0.0, so `quality: balanced` is a no-op for an existing deployment.
    """
    video = next(s for s in VIDEO_ENCODERS if s.encoder == "libx265")
    assert "-crf 26" in video.render(node=None, quality="balanced", threads=2)
    assert "-quality 82" in IMAGE_ENCODER.render(node=None, quality="balanced", threads=2)


def test_quality_levels_move_the_number_in_the_direction_they_promise() -> None:
    video = next(s for s in VIDEO_ENCODERS if s.encoder == "libx265")
    assert video.quality["higher"] < video.quality["balanced"] < video.quality["smaller"]
    # JPEG quality runs the other way round.
    assert IMAGE_ENCODER.quality["higher"] > IMAGE_ENCODER.quality["smaller"]


def test_the_cpu_preset_is_pinned_to_the_cgroup_thread_budget() -> None:
    video = next(s for s in VIDEO_ENCODERS if s.encoder == "libx265")
    rendered = video.render(node=None, quality="balanced", threads=2)
    assert "pools=2" in rendered
    assert "-threads 2" in rendered


def test_the_generated_preset_exposes_its_encoder_and_device() -> None:
    spec = next(s for s in VIDEO_ENCODERS if s.encoder == "hevc_vaapi")
    preset = spec.build(node="/dev/dri/renderD129", quality="balanced", threads=2, name="p")
    assert preset.hardware_encoder == "hevc_vaapi"
    assert preset.render_node == "/dev/dri/renderD129"


def test_an_asset_type_without_a_recipe_says_what_to_do() -> None:
    with pytest.raises(ValueError, match="no built-in preset for asset type AUDIO"):
        build_presets([], enabled_types=["AUDIO"], quality="balanced", threads=2)


# -------------------------------------------------------------------------- report


async def test_detect_reports_the_full_picture() -> None:
    report = await detect(facts=INTEL_GEN9, enabled_types=["VIDEO", "IMAGE"], probe=_probe_stub("hevc_qsv"))
    assert report.selected is not None
    assert report.selected.encoder == "hevc_vaapi"
    assert report.uses_gpu is True
    # A GPU has one encode block; two concurrent jobs fight over it.
    assert report.concurrency == 1
    assert {p.match_type for p in report.presets} == {"VIDEO", "IMAGE"}
    assert "hevc_vaapi" in report.summary_line()
    assert "mode: vaapi" in report.pin_yaml()
    assert "ENCODER=hevc_vaapi" in report.calibrate_hint()
    assert len(report.rejected) >= 1


async def test_both_calibration_commands_carry_the_same_thread_budget() -> None:
    """calibrate.sh falls back to THREADS=2 on its own.

    Left out of the container variant, the sweep therefore measures with half the threads
    the encoder will really get, and the quality number comes out tuned against a machine
    that does not exist.
    """
    report = await detect(facts=INTEL_GEN9, probe=_probe_stub("hevc_qsv"))
    hint = report.calibrate_hint()

    threads = f"THREADS={report.facts.cpu.threads}"
    assert hint.count(threads) == 2, hint
    assert f"-e {threads}" in hint


async def test_the_json_report_is_serialisable_and_names_every_rejection() -> None:
    report = await detect(facts=INTEL_GEN9, probe=_probe_stub("hevc_qsv", "hevc_vaapi"))
    body = json.loads(json.dumps(report.to_dict()))
    assert body["cpu"]["threads"] == 2
    assert body["render_nodes"][0]["vendor"] == "intel"
    assert body["render_nodes"][0]["vendor_id"] == "0x8086"
    assert all(c["reason"] for c in body["candidates"] if c["status"] != "selected")


async def test_the_human_report_mentions_every_candidate() -> None:
    report = await detect(facts=INTEL_GEN9, probe=_probe_stub("hevc_qsv"))
    text = format_report(report)
    for spec in VIDEO_ENCODERS:
        assert spec.encoder in text
    assert "/dev/dri/renderD128" in text
    assert "scripts/calibrate.sh" in text


async def test_a_cpu_only_box_reports_concurrency_from_its_budget() -> None:
    report = await detect(facts=_facts(cpu=CpuBudget(cores=16.0, source="t", host_cores=16)))
    assert report.selected is not None and report.selected.encoder == "libx265"
    assert report.concurrency == 2


# ----------------------------------------------------------------- settings wiring


def _settings(**overrides: object) -> Settings:
    body: dict[str, object] = {
        "immich": {"api_key": "k"},
        "webhook": {"token": "t"},
    }
    body.update(overrides)
    return Settings(**body)  # type: ignore[arg-type]


def test_explicit_presets_beat_autodetection(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 1.0.0 config with hand-written presets must behave exactly as it did."""

    def explode(**_: object) -> None:
        raise AssertionError("detection must not run when presets are written by hand")

    monkeypatch.setattr("immich_compressor.hardware.detect_sync", explode)
    settings = _settings(
        presets=[
            {
                "name": "mine",
                "type": "VIDEO",
                "cmd": "ffmpeg -i {input} -c:v libx265 -crf 20 {output}",
                "suffix": ".mp4",
            }
        ]
    )
    resolved, report = apply_to_settings(settings)
    assert [p.name for p in resolved.presets] == ["mine"]
    assert report.explicit_presets is True
    assert "config.yaml" in report.summary_line()


def test_autodetection_fills_in_presets_and_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    import immich_compressor.hardware as hardware

    monkeypatch.setattr(hardware, "collect_host_facts", _async_facts(INTEL_GEN12))
    monkeypatch.setattr(hardware, "probe_hardware_encoder", _probe_stub())
    resolved, report = apply_to_settings(_settings())
    assert [p.name for p in resolved.presets] == ["auto-video-hevc-qsv"]
    assert resolved.behavior.concurrency == 1
    assert report.explicit_presets is False


def test_an_explicit_concurrency_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    import immich_compressor.hardware as hardware

    monkeypatch.setattr(
        hardware,
        "collect_host_facts",
        _async_facts(_facts(cpu=CpuBudget(cores=32.0, source="t", host_cores=32))),
    )
    monkeypatch.setattr(hardware, "probe_hardware_encoder", _probe_stub())
    resolved, report = apply_to_settings(_settings(behavior={"concurrency": 1}))
    assert resolved.behavior.concurrency == 1
    assert report.concurrency == 1


def _async_facts(facts: HostFacts):
    async def collect() -> HostFacts:
        return facts

    return collect
