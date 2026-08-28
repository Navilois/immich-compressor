"""The guided setup: it must be safe to run twice and must never leak a secret into git."""

from __future__ import annotations

import json
import re
import stat
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import respx
import yaml

from immich_compressor.config import ConfigError, load_settings
from immich_compressor.hardware import Candidate, CpuBudget, HardwareReport, HostFacts, RenderNode
from immich_compressor.setup_cmd import (
    PERMISSION_PROBES,
    SetupOptions,
    check_key,
    check_permissions,
    compose_file_value,
    create_workflow,
    ensure_compose_override,
    read_env_file,
    render_config,
    render_env,
    run_setup,
    workflow_json,
    write_secret_file,
)

BASE = "http://immich-test:2283/api"
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _no_real_hardware(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setup must be testable on a machine with no ffmpeg, no GPU and no network."""
    import immich_compressor.hardware as hardware

    async def facts() -> HostFacts:
        return HostFacts(
            ffmpeg_path="/usr/bin/ffmpeg",
            ffmpeg_encoders=frozenset({"libx265"}),
            cpu=CpuBudget(cores=2.0, source="cgroup v2 cpu.max", host_cores=8),
        )

    monkeypatch.setattr(hardware, "collect_host_facts", facts)


# ------------------------------------------------------------------------- workflow


def test_the_workflow_filter_excludes_our_own_uploads() -> None:
    """Without it the compressed upload re-triggers the workflow — confirmed to happen.

    `assetFileFilter` has no `inverse` option, so the exclusion has to be a negative
    lookahead. Immich's regex engine supports them; that was verified against v3.1.0.
    """
    body = workflow_json(webhook_url="http://immich-compressor:8080/webhook", token="s3cret")
    pattern = body["steps"][1]["config"]["pattern"]
    assert pattern == r"^(?!.*\.cmp\.).*$"

    import re

    compiled = re.compile(pattern)
    assert compiled.match("holiday.mov")
    assert not compiled.match("holiday.cmp.mp4")


def test_the_workflow_uses_the_metadata_extraction_trigger() -> None:
    """AssetCreate fires before exifInfo exists, and exifInfo is the whole point."""
    body = workflow_json(webhook_url="http://x/webhook", token="t")
    assert body["trigger"] == "AssetMetadataExtraction"
    assert body["steps"][2]["config"]["headerName"] == "X-Compressor-Token"
    assert body["steps"][2]["config"]["headerValue"] == "t"


def test_a_custom_marker_is_escaped_into_the_regex() -> None:
    body = workflow_json(webhook_url="http://x/webhook", token="t", marker=".sml")
    assert body["steps"][1]["config"]["pattern"] == r"^(?!.*\.sml\.).*$"


# ---------------------------------------------------------------------- permissions


@respx.mock
def test_permissions_are_reported_by_name() -> None:
    """A 403 is the server saying "not granted"; anything else means the guard let us in."""
    for probe in PERMISSION_PROBES:
        status = 403 if probe.permission in {"asset.delete", "tag.create"} else 404
        respx.route(method=probe.method, url=f"{BASE}{probe.url}").mock(return_value=httpx.Response(status))
    with httpx.Client(base_url=BASE, headers={"x-api-key": "k"}) as client:
        results = check_permissions(client, include_delete=True)
    missing = {r.probe.permission for r in results if not r.granted}
    assert missing == {"asset.delete", "tag.create"}
    assert all("403" in r.detail for r in results if not r.granted)


@respx.mock
def test_the_delete_permission_is_only_required_when_deleting() -> None:
    for probe in PERMISSION_PROBES:
        respx.route(method=probe.method, url=f"{BASE}{probe.url}").mock(return_value=httpx.Response(404))
    with httpx.Client(base_url=BASE, headers={"x-api-key": "k"}) as client:
        checked = {r.probe.permission for r in check_permissions(client, include_delete=False)}
    assert "asset.delete" not in checked
    assert "asset.read" in checked


def test_every_permission_probe_is_inert() -> None:
    """A "may I?" question must never be able to change somebody's library."""
    for probe in PERMISSION_PROBES:
        if probe.method in {"POST", "PUT", "DELETE"}:
            body = probe.json_body or {}
            targets = [v for v in body.values() if isinstance(v, list)]
            assert all(target == [] for target in targets), probe.permission
            for value in body.values():
                if isinstance(value, str):
                    # Only ever the id that cannot exist.
                    assert value.startswith("00000000-"), probe.permission


@respx.mock
def test_a_rejected_key_is_reported_before_anything_else_happens() -> None:
    respx.get(f"{BASE}/users/me").mock(return_value=httpx.Response(401, json={"message": "Invalid API key"}))
    with httpx.Client(base_url=BASE, headers={"x-api-key": "bad"}) as client:
        ok, message = check_key(client)
    assert ok is False
    assert "Invalid API key" in message


@respx.mock
def test_a_correctly_scoped_key_is_not_mistaken_for_a_bad_one() -> None:
    """403 on /users/me is the *expected* answer, and used to abort setup entirely.

    Measured against a live v3.1.0: a valid key without `user.read` gets
    403 "Missing required permission: user.read", while a bogus key gets 401
    "Invalid API key". This service deliberately never asks for `user.read`, so every
    correctly configured deployment hits the 403 — and treating it as a failure meant
    setup refused to run against a real server. A stub that answered 200 hid it.
    """
    respx.get(f"{BASE}/users/me").mock(
        return_value=httpx.Response(403, json={"message": "Missing required permission: user.read"})
    )
    with httpx.Client(base_url=BASE, headers={"x-api-key": "good-but-scoped"}) as client:
        ok, message = check_key(client)
    assert ok is True
    assert "user.read" in message


@respx.mock
def test_a_missing_permission_is_reported_in_the_servers_own_words() -> None:
    """Immich names the permission exactly as the API-key editor spells it."""
    for probe in PERMISSION_PROBES:
        body = {"message": f"Missing required permission: {probe.permission}"}
        status = 403 if probe.permission == "asset.copy" else 404
        respx.route(method=probe.method, url=f"{BASE}{probe.url}").mock(
            return_value=httpx.Response(status, json=body if status == 403 else {})
        )
    with httpx.Client(base_url=BASE, headers={"x-api-key": "k"}) as client:
        results = check_permissions(client, include_delete=True)
    missing = [r for r in results if not r.granted]
    assert [r.probe.permission for r in missing] == ["asset.copy"]
    assert missing[0].detail == "Missing required permission: asset.copy"


@respx.mock
def test_workflow_creation_prefers_a_session_token() -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers.get("authorization", ""))
        return httpx.Response(201, json={"id": "wf-1", "steps": []})

    respx.post(f"{BASE}/workflows").mock(side_effect=handler)
    ok, detail = create_workflow(
        BASE, workflow_json(webhook_url="http://x", token="t"), api_key="k", session_token="sess"
    )
    assert ok is True
    assert captured == ["Bearer sess"]
    # POST /workflows answers "steps": [] even though they were saved — say so.
    assert "GET /workflows/wf-1" in detail


@respx.mock
def test_workflow_creation_prefers_a_throwaway_key_over_a_session_token() -> None:
    """A session token carries the user's *full* access; a key scoped to `workflow.create`
    carries one permission. When both are offered the narrower one wins."""
    captured: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers)
        return httpx.Response(201, json={"id": "wf-2", "steps": []})

    respx.post(f"{BASE}/workflows").mock(side_effect=handler)
    ok, _ = create_workflow(
        BASE,
        workflow_json(webhook_url="http://x", token="t"),
        api_key="service-key",
        session_token="sess",
        workflow_key="throwaway",
    )
    assert ok is True
    assert captured[0]["x-api-key"] == "throwaway"
    assert "authorization" not in captured[0]


@respx.mock
def test_a_forbidden_workflow_create_says_which_permission_is_missing() -> None:
    respx.post(f"{BASE}/workflows").mock(return_value=httpx.Response(403))
    ok, detail = create_workflow(
        BASE, workflow_json(webhook_url="http://x", token="t"), api_key="k", session_token=None
    )
    assert ok is False
    assert "workflow.create" in detail


# ---------------------------------------------------------------------------- files


def test_env_round_trip_keeps_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("# a comment\nIMMICH_API_KEY=abc\n\nSOMETHING_ELSE=1\n", encoding="utf-8")
    values = read_env_file(path)
    assert values == {"IMMICH_API_KEY": "abc", "SOMETHING_ELSE": "1"}
    assert "SOMETHING_ELSE=1" in render_env(values)


def test_a_secret_file_is_never_world_readable(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    write_secret_file(path, "IMMICH_API_KEY=abc\n")
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_a_secret_file_is_tightened_before_the_secret_goes_into_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chmod after write leaves the new secret world-readable for the length of the write.

    Only reachable when the file already exists — a 0644 `.env` from a version that wrote
    it with plain `write_text` — so the spy watches what the file holds at chmod time.
    """
    path = tmp_path / ".env"
    path.write_text("IMMICH_API_KEY=stale\n", encoding="utf-8")
    path.chmod(0o644)

    held_at_chmod: list[str] = []
    real_chmod = Path.chmod

    def spy(self: Path, mode: int) -> None:
        held_at_chmod.append(self.read_text(encoding="utf-8"))
        real_chmod(self, mode)

    monkeypatch.setattr(Path, "chmod", spy)
    write_secret_file(path, "IMMICH_API_KEY=fresh\n")

    assert held_at_chmod == [""], "the mode was set after the secret was already on disk"
    assert path.read_text(encoding="utf-8") == "IMMICH_API_KEY=fresh\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_generated_config_loads_and_carries_no_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The config setup writes must survive the same fail-fast validation as any other."""
    import immich_compressor.hardware as hardware

    _, report = hardware.apply_to_settings(load_settings_stub := _stub_settings(), always_detect=True)
    assert load_settings_stub is not None
    body = render_config(report, network_hint="immich_default")
    assert "api_key" not in body
    assert "token" not in body.replace("WEBHOOK__TOKEN", "").replace("COMPRESSOR_TOKEN", "")

    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setenv("IMMICH__API_KEY", "k")
    monkeypatch.setenv("WEBHOOK__TOKEN", "t")
    settings = load_settings(path)
    assert settings.behavior.dry_run is True
    assert settings.behavior.trash_original is False
    assert settings.behavior.delete_mode == "trash"


def _stub_settings():
    from immich_compressor.config import Settings

    return Settings(immich={"api_key": "k"}, webhook={"token": "t"})


def test_a_secret_in_the_generated_config_would_be_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMMICH__API_KEY", "k")
    monkeypatch.setenv("WEBHOOK__TOKEN", "t")
    path = tmp_path / "config.yaml"
    path.write_text("immich:\n  api_key: leaked\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"must not be set in config\.yaml"):
        load_settings(path)


# ------------------------------------------------------------------------ end to end


def _mock_server(*, forbidden: set[str] = frozenset()) -> None:
    respx.get(f"{BASE}/users/me").mock(
        return_value=httpx.Response(200, json={"email": "someone@example.com"})
    )
    respx.get(f"{BASE}/server/version").mock(
        return_value=httpx.Response(200, json={"major": 3, "minor": 1, "patch": 0})
    )
    for probe in PERMISSION_PROBES:
        status = 403 if probe.permission in forbidden else 404
        respx.route(method=probe.method, url=f"{BASE}{probe.url}").mock(return_value=httpx.Response(status))
    respx.post(f"{BASE}/workflows").mock(return_value=httpx.Response(201, json={"id": "wf-1", "steps": []}))


def _options(tmp_path: Path, **overrides: object) -> SetupOptions:
    body: dict[str, object] = {
        "base_url": BASE,
        "api_key": "the-key",
        "network": "immich_default",
        "directory": tmp_path,
        "non_interactive": True,
    }
    body.update(overrides)
    return SetupOptions(**body)  # type: ignore[arg-type]


@respx.mock
def test_setup_writes_a_complete_deployment(tmp_path: Path) -> None:
    _mock_server()
    assert run_setup(_options(tmp_path)) == 0

    env = read_env_file(tmp_path / ".env")
    assert env["IMMICH_API_KEY"] == "the-key"
    assert len(env["COMPRESSOR_TOKEN"]) >= 32
    assert env["IMMICH_BASE_URL"] == BASE
    assert (tmp_path / "config.yaml").is_file()
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600


@respx.mock
def test_setup_is_safe_to_run_twice(tmp_path: Path) -> None:
    """Re-running must not rotate the webhook secret out from under a live workflow."""
    _mock_server()
    run_setup(_options(tmp_path))
    first_token = read_env_file(tmp_path / ".env")["COMPRESSOR_TOKEN"]
    (tmp_path / "config.yaml").write_text("# edited by hand\n", encoding="utf-8")

    run_setup(_options(tmp_path))
    assert read_env_file(tmp_path / ".env")["COMPRESSOR_TOKEN"] == first_token
    assert (tmp_path / "config.yaml").read_text() == "# edited by hand\n"


@respx.mock
def test_force_regenerates_the_config_but_says_so(tmp_path: Path, capsys) -> None:
    _mock_server()
    run_setup(_options(tmp_path))
    (tmp_path / "config.yaml").write_text("# edited by hand\n", encoding="utf-8")
    run_setup(_options(tmp_path, force=True))
    assert "# edited by hand" not in (tmp_path / "config.yaml").read_text()
    assert "regenerated" in capsys.readouterr().out


@respx.mock
def test_setup_stops_before_touching_anything_without_a_key(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 2, and not one file written.

    The first step is the only one that can end the run without anything to show for it,
    and the exit code is what a script reads. Writing a `.env` on the way out would leave a
    generated webhook token behind for an installation that does not exist.
    """
    monkeypatch.delenv("IMMICH_API_KEY", raising=False)
    assert run_setup(_options(tmp_path, api_key="")) == 2
    assert "no API key given" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


@respx.mock
def test_setup_stops_before_touching_anything_on_a_rejected_key(tmp_path: Path, capsys) -> None:
    """Exit 1, likewise with nothing written.

    A key the server refuses cannot produce a working deployment, and going on would
    detect hardware and write a config for one anyway.
    """
    respx.get(f"{BASE}/users/me").mock(return_value=httpx.Response(401, json={"message": "Invalid"}))
    # Still reached: the version is read and printed before the key verdict is acted on, so
    # somebody looking at a refusal can see which server refused them.
    respx.get(f"{BASE}/server/version").mock(
        return_value=httpx.Response(200, json={"major": 3, "minor": 1, "patch": 0})
    )
    assert run_setup(_options(tmp_path)) == 1
    assert "Server" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


@respx.mock
def test_missing_permissions_make_setup_exit_non_zero_and_name_them(tmp_path: Path, capsys) -> None:
    _mock_server(forbidden={"asset.copy"})
    assert run_setup(_options(tmp_path)) == 1
    out = capsys.readouterr().out
    assert "MISSING asset.copy" in out
    assert "Account Settings -> API Keys" in out


@respx.mock
def test_a_workflow_that_cannot_be_created_is_written_out_with_the_curl(tmp_path: Path, capsys) -> None:
    _mock_server()
    respx.post(f"{BASE}/workflows").mock(return_value=httpx.Response(403))
    run_setup(_options(tmp_path))
    body = json.loads((tmp_path / "immich-workflow.json").read_text())
    assert body["trigger"] == "AssetMetadataExtraction"
    out = capsys.readouterr().out
    assert "curl -X POST" in out
    assert "SESSION_TOKEN" in out


@respx.mock
def test_the_workflow_json_carries_the_token_that_landed_in_env(tmp_path: Path) -> None:
    """A mismatch here is a 401 that Immich reports as "executed successfully"."""
    _mock_server()
    respx.post(f"{BASE}/workflows").mock(return_value=httpx.Response(403))
    run_setup(_options(tmp_path))
    body = json.loads((tmp_path / "immich-workflow.json").read_text())
    assert body["steps"][2]["config"]["headerValue"] == read_env_file(tmp_path / ".env")["COMPRESSOR_TOKEN"]


@respx.mock
def test_the_workflow_json_is_never_world_readable(tmp_path: Path, capsys) -> None:
    """It carries COMPRESSOR_TOKEN in clear text, so it gets the same mode as .env."""
    _mock_server()
    respx.post(f"{BASE}/workflows").mock(return_value=httpx.Response(403))
    run_setup(_options(tmp_path))
    assert stat.S_IMODE((tmp_path / "immich-workflow.json").stat().st_mode) == 0o600
    out = capsys.readouterr().out
    assert "mode 0600" in out
    assert "delete immich-workflow.json" in out


@respx.mock
def test_the_throwaway_workflow_key_is_never_written_anywhere(tmp_path: Path, capsys) -> None:
    """It exists for exactly one request. Writing it down would turn a deliberately
    short-lived credential into a second long-lived one sitting next to the first."""
    _mock_server()

    assert run_setup(_options(tmp_path, workflow_key="throwaway-workflow-key")) == 0

    written = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert written, "setup must have written something for this test to mean anything"
    for path in written:
        assert "throwaway-workflow-key" not in path.read_text(encoding="utf-8"), path
    # And the user is told to get rid of it, because setup cannot: deleting a key needs a
    # permission this one deliberately does not have.
    assert "delete that API key in Immich now" in capsys.readouterr().out


def test_the_workflow_json_is_gitignored() -> None:
    """git status must not offer to stage the file the shared webhook token lives in."""
    root = Path(__file__).resolve().parent.parent
    ignored = {
        line.strip()
        for line in (root / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert "immich-workflow.json" in ignored


# ------------------------------------------------------------------- the compose file


def _checkout(directory: Path) -> Path:
    """The one tracked file setup copies. Tests run in an empty dir; deployments do not."""
    template = directory / "docker-compose.override.example.yaml"
    template.write_text(
        (REPO / "docker-compose.override.example.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return template


def _select_gpu(monkeypatch: pytest.MonkeyPatch, encoder: str) -> None:
    """Make setup see a machine whose chosen encoder needs a compose overlay.

    The real detection runs first, so the presets and the CPU budget in the report stay
    exactly what setup would have written; only the verdict is swapped.
    """
    import immich_compressor.hardware as hardware

    node = RenderNode(path="/dev/dri/renderD128", vendor="intel", group="render", gid=992)
    spec = next(s for s in hardware.VIDEO_ENCODERS if s.encoder == encoder)
    nvidia = encoder == "hevc_nvenc"
    real = hardware.detect_sync

    def detect(**kwargs: object) -> HardwareReport:
        report = real(**kwargs)
        chosen = Candidate(
            encoder=spec.encoder,
            label=spec.label,
            device=None if nvidia else node.path,
            spec=spec,
            status="selected",
        )
        facts = replace(report.facts, render_nodes=() if nvidia else (node,))
        return replace(report, facts=facts, candidates=[chosen])

    monkeypatch.setattr(hardware, "detect_sync", detect)


def test_the_local_override_is_listed_last_so_it_still_wins(tmp_path: Path) -> None:
    """COMPOSE_FILE replaces compose's default list, and the override is only in it by default.

    Naming an overlay and nothing else unloads docker-compose.override.yaml silently,
    taking the go-live flags, the resource limits and any local image pin with it.
    """
    (tmp_path / "docker-compose.override.yaml").write_text("services: {}\n", encoding="utf-8")
    assert compose_file_value(tmp_path, "docker-compose.gpu.yaml") == (
        "docker-compose.yaml:docker-compose.gpu.yaml:docker-compose.override.yaml"
    )


def test_an_override_that_does_not_exist_is_left_out(tmp_path: Path) -> None:
    """Compose exits 1 on a file it cannot stat, so listing an absent one breaks every command."""
    assert compose_file_value(tmp_path, "docker-compose.gpu.yaml") == (
        "docker-compose.yaml:docker-compose.gpu.yaml"
    )


@respx.mock
def test_the_gpu_wiring_keeps_the_override_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_server()
    (tmp_path / "docker-compose.override.yaml").write_text("services: {}\n", encoding="utf-8")
    _select_gpu(monkeypatch, "hevc_vaapi")

    assert run_setup(_options(tmp_path)) == 0

    env = read_env_file(tmp_path / ".env")
    assert env["RENDER_GID"] == "992"
    assert env["COMPOSE_FILE"] == "docker-compose.yaml:docker-compose.gpu.yaml:docker-compose.override.yaml"


@respx.mock
def test_the_nvidia_wiring_keeps_the_override_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_server()
    (tmp_path / "docker-compose.override.yaml").write_text("services: {}\n", encoding="utf-8")
    _select_gpu(monkeypatch, "hevc_nvenc")

    assert run_setup(_options(tmp_path)) == 0

    assert read_env_file(tmp_path / ".env")["COMPOSE_FILE"] == (
        "docker-compose.yaml:docker-compose.gpu-nvidia.yaml:docker-compose.override.yaml"
    )


@respx.mock
def test_setup_creates_the_override_so_the_line_can_name_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The override is written at go-live, long after setup ran — too late for COMPOSE_FILE.

    Creating it up front is what makes the line complete for everyone rather than for
    whoever happened to write their override first.
    """
    _mock_server()
    template = _checkout(tmp_path)
    _select_gpu(monkeypatch, "hevc_vaapi")

    assert run_setup(_options(tmp_path)) == 0

    override = tmp_path / "docker-compose.override.yaml"
    assert override.read_text() == template.read_text()
    assert read_env_file(tmp_path / ".env")["COMPOSE_FILE"].endswith(":docker-compose.override.yaml")


@respx.mock
def test_an_existing_override_survives_force(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regenerating it would put a live deployment back into dry run without saying so."""
    _mock_server()
    _checkout(tmp_path)
    _select_gpu(monkeypatch, "hevc_vaapi")
    live = 'services:\n  immich-compressor:\n    environment:\n      BEHAVIOR__DRY_RUN: "false"\n'
    (tmp_path / "docker-compose.override.yaml").write_text(live, encoding="utf-8")

    run_setup(_options(tmp_path, force=True))

    assert (tmp_path / "docker-compose.override.yaml").read_text() == live


def test_an_override_is_only_created_from_the_tracked_template(tmp_path: Path) -> None:
    """Nothing is invented: with no template there is no file, and COMPOSE_FILE says so."""
    assert ensure_compose_override(tmp_path) is False
    assert not (tmp_path / "docker-compose.override.yaml").exists()


@respx.mock
def test_a_checkout_without_the_template_says_what_to_add(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _mock_server()
    _select_gpu(monkeypatch, "hevc_vaapi")

    run_setup(_options(tmp_path))

    out = capsys.readouterr().out
    assert "docker-compose.override.yaml" not in read_env_file(tmp_path / ".env")["COMPOSE_FILE"]
    assert "add :docker-compose.override.yaml to that line yourself" in out


# The settings in the template are commented-out YAML; the prose around them is not. A
# reader uncomments the former, so that is what this turns on.
_A_SETTING = re.compile(r"^\s*([\w.-]+:(\s|$)|- )")


def _uncomment(template: str) -> str:
    out = []
    for line in template.splitlines(True):
        bare = re.sub(r"^(\s*)# ?", r"\1", line, count=1)
        out.append(bare if line.lstrip().startswith("#") and _A_SETTING.match(bare) else line)
    return "".join(out)


def test_the_override_template_survives_its_first_edit() -> None:
    """It used to end in a `{}`, which turned the first uncommented block into a parse error.

    `setup` hands this file to every GPU deployment now, and the first thing anyone does to
    it is uncomment a block — usually `BEHAVIOR__DRY_RUN` at stage 2 of docs/safety.md. That
    edit has to stand on its own, with no second line to remember to delete.
    """
    template = (REPO / "docker-compose.override.example.yaml").read_text(encoding="utf-8")

    # A real key rather than nothing: a null service would rest on compose tolerating one,
    # which is only known to hold for the version this was tested against.
    assert yaml.safe_load(template)["services"]["immich-compressor"]

    edited = yaml.safe_load(_uncomment(template))["services"]["immich-compressor"]
    assert edited["environment"]["BEHAVIOR__DRY_RUN"] == "false"
    assert edited["cpus"] == 2
    assert edited["ports"] == ["127.0.0.1:8080:8080"]
    # Nothing but real settings came out of the comments: prose that reads like `key: value`
    # is prose a reader would uncomment too, and compose rejects what it does not know.
    assert set(edited) <= {"restart", "environment", "cpus", "mem_limit", "build", "image", "ports"}


def test_every_flag_env_example_documents_is_one_compose_passes_through() -> None:
    """`.env` is compose's substitution file, not an env_file: it reaches the container only
    through names docker-compose.yaml lists.

    The two drifted apart once already — `.env.example` documented four `BEHAVIOR__` flags
    that the compose file passed on to nobody, so a deployment that went live through `.env`
    silently stayed in dry run.
    """
    # The `BEHAVIOR__` flags and TZ: the names `.env.example` shows commented out because
    # leaving them unset is meaningful, which is exactly what a bare pass-through means.
    documented = set(
        re.findall(r"^# (BEHAVIOR__\w+|TZ)=", (REPO / ".env.example").read_text(encoding="utf-8"), re.M)
    )
    service = yaml.safe_load((REPO / "docker-compose.yaml").read_text(encoding="utf-8"))["services"]
    environment = service["immich-compressor"]["environment"]
    assert isinstance(environment, list), "only the list form can carry a bare pass-through name"
    # A bare name is "pass this on only if it is set"; a NAME=value entry is a value the
    # compose file supplies itself, which is a different thing.
    passed = {e for e in environment if "=" not in e}

    assert documented, "these are commented examples in .env.example; keep them there"
    assert documented == passed


def test_quickstart_hands_the_api_key_to_the_container() -> None:
    """`setup` tells you to export IMMICH_API_KEY. The script has to pass it on.

    Without `-e IMMICH_API_KEY` the advice points straight back into the dead end the
    reader is already standing in: the variable is set on the host, and the container
    that prints the message cannot see it. Reproduced against 1.1.1.
    """
    script = (REPO / "scripts" / "quickstart.sh").read_text(encoding="utf-8")
    setup_source = (REPO / "src" / "immich_compressor" / "setup_cmd.py").read_text(encoding="utf-8")

    assert "IMMICH_API_KEY in the environment" in setup_source, "the message this test guards"
    assert re.search(r"^\s*-e IMMICH_API_KEY\b", script, re.M)


def test_quickstart_does_not_repeat_what_setup_already_printed() -> None:
    """One closing block, not two. `set -e` means the script only reaches its own tail
    after a successful setup, and `run_setup` has already printed the same three commands
    by then — a second copy reads like something went wrong."""
    script = (REPO / "scripts" / "quickstart.sh").read_text(encoding="utf-8")

    assert "==> Next" not in script
    assert "docker compose up -d" not in script


@respx.mock
def test_the_generated_env_offers_the_sizing_knobs(tmp_path: Path) -> None:
    """`.env.example` documents COMPRESSOR_CPUS and COMPRESSOR_MEMORY, and nobody who takes
    the documented route ever opens it — `setup` writes them a real `.env`, and a template
    stops being read the moment a real file exists."""
    _mock_server()
    assert run_setup(_options(tmp_path)) == 0

    body = (tmp_path / ".env").read_text(encoding="utf-8")
    assert re.search(r"^# COMPRESSOR_CPUS=\d+$", body, re.M)
    assert re.search(r"^# COMPRESSOR_MEMORY=\S+$", body, re.M)
    # Inert: commented out, so the shipped defaults still stand.
    assert not re.search(r"^COMPRESSOR_CPUS=", body, re.M)
    # And the question the two mechanisms raise is answered where they are offered.
    assert "docker-compose.override.yaml beat COMPRESSOR_CPUS" in body


def test_a_value_already_set_is_not_offered_again(tmp_path: Path) -> None:
    """Somebody who uncommented the line must not find a commented copy underneath it."""
    body = render_env({"COMPRESSOR_CPUS": "6"}, suggested={"COMPRESSOR_CPUS": "2", "COMPRESSOR_MEMORY": "2g"})
    assert "COMPRESSOR_CPUS=6" in body
    assert "# COMPRESSOR_CPUS=" not in body
    assert "# COMPRESSOR_MEMORY=2g" in body


@respx.mock
def test_a_granted_asset_delete_is_pointed_out(tmp_path: Path, capsys) -> None:
    """The quickstart says to leave it out for the first run, because without it the
    service physically cannot remove an original. Granted anyway it printed an `ok` in the
    same shape as every other permission, and the guarantee went away unannounced."""
    _mock_server()
    assert run_setup(_options(tmp_path)) == 0

    out = capsys.readouterr().out
    assert "asset.delete is granted" in out
    assert "from stage 3 on" in out


@respx.mock
def test_the_container_gets_the_hosts_timezone_when_the_host_has_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Immich's compose file hands its containers TZ; this one had neither TZ nor
    /etc/localtime, so the two logged two hours apart — while troubleshooting.md asks
    people to correlate them line by line."""
    _mock_server()
    monkeypatch.setenv("TZ", "Europe/Vienna")

    assert run_setup(_options(tmp_path)) == 0
    assert "TZ=Europe/Vienna" in (tmp_path / ".env").read_text(encoding="utf-8")


@respx.mock
def test_a_host_without_a_timezone_is_offered_the_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_server()
    monkeypatch.delenv("TZ", raising=False)

    assert run_setup(_options(tmp_path)) == 0

    body = (tmp_path / ".env").read_text(encoding="utf-8")
    assert re.search(r"^# TZ=", body, re.M)
    assert "Copy the value from" in body or "copy the value from" in body


def test_a_backup_of_env_is_ignored_too() -> None:
    """The entry was the bare name. Everything beside it was committable: `.env.bak` from a
    `setup --force`, `.env.local`, `.env.prod` — each carrying the same API key and the
    same webhook token as the original. It happened during the audit.

    Read out of the file rather than measured with `git check-ignore`: the test containers
    are `python:3.x-slim`, which carries no git binary, and a test that skips itself where
    it cannot run is exactly how the encoder tests went unnoticed for a whole release.
    """
    lines = [
        stripped
        for line in (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
        if (stripped := line.strip()) and not stripped.startswith("#")
    ]

    assert ".env*" in lines, "the glob: .env.bak and .env.local carry the same secrets as .env"
    assert ".env" not in lines, "the bare entry is the narrow one the glob replaced"
    assert "!.env.example" in lines, "the template stays tracked, or a fresh clone has none"
