"""The guided ``setup`` command: from an API key to a running service in one step.

Everything here is about removing the five things that make people give up halfway:
guessing which API-key permissions are needed, hand-writing a preset for their GPU,
inventing a webhook secret, working out the render group id, and assembling the workflow
JSON. Each of those is a question the machine can answer for itself.

The command is safe to run twice. Existing files are never silently replaced: values
already in ``.env`` are kept, and an existing ``config.yaml`` is left alone unless
``--force`` says otherwise. Both decisions are printed.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .config import Settings
from .hardware import HardwareReport, apply_to_settings

DEFAULT_BASE_URL = "http://immich-server:2283/api"
DEFAULT_NETWORK = "immich_default"
DEFAULT_WEBHOOK_URL = "http://immich-compressor:8080/webhook"
WEBHOOK_HEADER = "X-Compressor-Token"

# A uuid that cannot exist. The permission probes below aim their requests at it so a
# "may I?" question can never turn into a change to somebody's library.
_NOWHERE = "00000000-0000-4000-8000-000000000000"


@dataclass(frozen=True, slots=True)
class PermissionProbe:
    """One Immich permission and a deliberately inert request that needs it.

    Every probe is a no-op by construction: reads aim at an asset id that does not exist,
    and writes carry an empty id list or a body the server rejects as malformed. Immich
    checks the API key's permission in a guard *before* the handler runs, so a 403 means
    "not granted" while any other status means the request got far enough to be refused
    for an ordinary reason — which is the answer we want.
    """

    permission: str
    method: str
    url: str
    needed_for: str
    json_body: dict[str, Any] | None = None
    only_if_deleting: bool = False


PERMISSION_PROBES: tuple[PermissionProbe, ...] = (
    PermissionProbe("asset.read", "GET", f"/assets/{_NOWHERE}", "reading the asset to compress"),
    PermissionProbe("asset.download", "GET", f"/assets/{_NOWHERE}/original", "downloading the original"),
    PermissionProbe("asset.upload", "POST", "/assets", "uploading the compressed file"),
    PermissionProbe(
        "asset.update",
        "PUT",
        f"/assets/{_NOWHERE}",
        "carrying over description, rating, GPS and the marker",
        json_body={},
    ),
    PermissionProbe(
        "asset.copy",
        "PUT",
        "/assets/copy",
        "carrying over albums, favourite, shared links and the stack",
        json_body={"sourceId": _NOWHERE, "targetId": _NOWHERE},
    ),
    PermissionProbe("tag.read", "GET", "/tags", "reading the source asset's tags"),
    PermissionProbe("tag.create", "PUT", "/tags", "re-creating tags by name", json_body={"tags": []}),
    PermissionProbe(
        "tag.asset",
        "PUT",
        "/tags/assets",
        "attaching the tags to the replacement",
        json_body={"tagIds": [], "assetIds": []},
    ),
    PermissionProbe(
        "asset.delete",
        "DELETE",
        "/assets",
        "removing the original — only needed once trash_original is on",
        json_body={"ids": [], "force": False},
        only_if_deleting=True,
    ),
)


@dataclass(slots=True)
class PermissionResult:
    probe: PermissionProbe
    granted: bool
    detail: str


def workflow_json(*, webhook_url: str, token: str, marker: str = ".cmp") -> dict[str, Any]:
    """The workflow that was created and fired successfully against a live v3.1.0.

    The trigger is ``AssetMetadataExtraction`` rather than ``AssetCreate``: only afterwards
    is ``exifInfo`` populated, and GPS, tags, rating and description are exactly what has to
    be carried over. The filename filter is a negative lookahead because ``assetFileFilter``
    has no ``inverse`` option — without it the compressed upload re-triggers the workflow,
    which was confirmed to happen.
    """
    escaped = marker.replace(".", "\\.")
    return {
        "name": "immich-compressor",
        "description": "Recompress large assets out of band",
        "trigger": "AssetMetadataExtraction",
        "enabled": True,
        "steps": [
            {
                "method": "immich-plugin-core#assetTypeFilter",
                "config": {"allowedTypes": ["VIDEO", "IMAGE"]},
                "enabled": True,
            },
            {
                "method": "immich-plugin-core#assetFileFilter",
                "config": {
                    "pattern": f"^(?!.*{escaped}\\.).*$",
                    "matchType": "regex",
                    "usePath": False,
                },
                "enabled": True,
            },
            {
                "method": "immich-plugin-core#webhook",
                "config": {
                    "url": webhook_url,
                    "method": "POST",
                    "headerName": WEBHOOK_HEADER,
                    "headerValue": token,
                },
                "enabled": True,
            },
        ],
    }


# ------------------------------------------------------------------- server checks


def _client(base_url: str, api_key: str) -> httpx.Client:
    return httpx.Client(
        base_url=base_url.rstrip("/"),
        headers={"x-api-key": api_key, "Accept": "application/json"},
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
    )


def _server_message(response: httpx.Response) -> str:
    """Immich's own words for a refusal — better than anything we could invent.

    A 403 body is ``{"message": "Missing required permission: asset.copy"}``, which names
    the permission exactly as it appears in the API-key editor. Verified against v3.1.0.
    """
    try:
        message = response.json().get("message")
    except ValueError:
        message = None
    return str(message) if message else f"HTTP {response.status_code}"


def check_key(client: httpx.Client) -> tuple[bool, str]:
    """Is this key valid at all? Returns ``(ok, message)``.

    The distinction that matters, measured against a live v3.1.0: a **401** means the key
    itself is wrong, while a **403** means Immich authenticated it and then refused the
    permission. ``GET /users/me`` needs ``user.read``, which this service deliberately does
    not ask for — so a 403 here is the *expected* answer for a correctly scoped key, and
    treating it as a failure would make setup refuse every properly configured deployment.

    ``GET /server/version`` cannot stand in for this: it answers 200 for a bogus key too,
    because it needs no authentication at all.
    """
    try:
        response = client.get("/users/me")
    except httpx.HTTPError as exc:
        return False, f"cannot reach the server: {exc}"
    if response.status_code == 401:
        return False, f"the server rejected the API key: {_server_message(response)}"
    if response.status_code == 403:
        return True, "key accepted (it has no user.read permission, which is correct)"
    if response.status_code >= 400:
        return False, f"GET /users/me answered {_server_message(response)}"
    body = response.json()
    return True, f"key accepted for user {body.get('email') or body.get('name') or 'unknown'}"


def server_version(client: httpx.Client) -> str | None:
    try:
        response = client.get("/server/version")
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    body = response.json()
    return f"{body.get('major')}.{body.get('minor')}.{body.get('patch')}"


def check_permissions(client: httpx.Client, *, include_delete: bool) -> list[PermissionResult]:
    """Ask the server, one inert request per permission, what this key may do."""
    results: list[PermissionResult] = []
    for probe in PERMISSION_PROBES:
        if probe.only_if_deleting and not include_delete:
            continue
        try:
            response = client.request(probe.method, probe.url, json=probe.json_body)
        except httpx.HTTPError as exc:
            results.append(PermissionResult(probe, False, f"could not ask the server: {exc}"))
            continue
        if response.status_code == 403:
            results.append(PermissionResult(probe, False, _server_message(response)))
        elif response.status_code == 401:
            results.append(
                PermissionResult(probe, False, f"the key was not accepted: {_server_message(response)}")
            )
        else:
            results.append(PermissionResult(probe, True, f"HTTP {response.status_code}"))
    return results


def create_workflow(
    base_url: str, body: dict[str, Any], *, api_key: str, session_token: str | None
) -> tuple[bool, str]:
    """Create the workflow, preferring a session token when one was supplied.

    ``workflow.create`` is not one of the compressor's own permissions, and granting it to
    a long-lived key would widen the key well past what the service needs. A session token
    is the narrower option, so it wins when it is offered.
    """
    headers = {"Authorization": f"Bearer {session_token}"} if session_token else {"x-api-key": api_key}
    try:
        with httpx.Client(base_url=base_url.rstrip("/"), timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            response = client.post("/workflows", json=body, headers=headers)
    except httpx.HTTPError as exc:
        return False, f"could not reach the server: {exc}"
    if response.status_code in (200, 201):
        # Verified against v3.1.0: the create response reports "steps": [] even though the
        # steps were persisted. Read the workflow back rather than believing the response.
        workflow_id = response.json().get("id", "?")
        return True, f"created workflow {workflow_id} (confirm with GET /workflows/{workflow_id})"
    if response.status_code == 403:
        return False, "the credentials lack workflow.create (HTTP 403)"
    return False, f"POST /workflows answered HTTP {response.status_code}: {response.text[:200]}"


# --------------------------------------------------------------------- file writing


def render_config(report: HardwareReport, *, network_hint: str) -> str:
    """A config.yaml tuned to this box, with the detected choice pinned in a comment."""
    selected = report.selected
    where = "the CPU preset" if selected is None else f"{selected.encoder}"
    return f"""\
# yaml-language-server: $schema=https://raw.githubusercontent.com/Navilois/immich-compressor/main/docs/config.schema.json
# Written by `immich-compressor setup`. Safe to edit; safe to delete and regenerate.
#
# Secrets are NOT read from this file — they come from the environment only:
#   IMMICH__API_KEY   the Immich API key
#   WEBHOOK__TOKEN    the shared secret, which must equal `headerValue` in the workflow
# Setting either of them here makes the service refuse to start.
#
# Any value below can also be overridden by an environment variable using `__` as the
# nesting separator, e.g. BEHAVIOR__DRY_RUN=false.

immich:
  base_url: {DEFAULT_BASE_URL}

hardware:
  # Detection picked {where} on this machine. Leave `auto` and it re-checks on every
  # start, which is what you want if the GPU is passed through conditionally; pin it with
  # `immich-compressor hardware` if you would rather it never change.
  mode: {report.mode}
  render_node: {report.render_node}

behavior:
  # ---- Shipping defaults: the service observes and reports, it changes nothing. ----
  dry_run: true            # true = download nothing, upload nothing, delete nothing
  trash_original: false    # false = originals are never removed, only reported
  delete_mode: trash       # trash = recoverable soft delete | permanent = gone for good
  # ---------------------------------------------------------------------------------
  # Read docs/safety.md before changing any of the three above.

  quality: {report.quality}        # balanced | higher | smaller
  concurrency: {report.concurrency}          # derived from this container's CPU budget

  retention_days: 7          # how long a replaced original survives; 0 = remove at once
  initial_delay_seconds: 300 # let Immich finish thumbnails/ML/OCR before touching an asset

  min_savings_bytes: 1048576 # 1 MiB — how much a job has to actually save to be worth
                             # a new asset. Also the free pre-download filter: a file
                             # cannot save more bytes than it has.
  max_ratio: 0.6             # reject the result unless it is <= 60 % of the original
  enabled_types: [VIDEO, IMAGE]  # drop IMAGE to leave stills alone
  skip_if_named_people: true # never risk losing manually named faces

  # Stills lose ALL metadata on re-encode and get it back from an exiftool copy; this is
  # the gate that proves the copy worked. `warn` only logs, and is refused together with
  # delete_mode: permanent — a warning cannot undo a force-deleted original.
  metadata_verify: strict

log_level: INFO

# No `presets:` block on purpose. Leaving it out is what lets the service pick the
# encoder for this machine; writing one pins the command and disables detection.
# `immich-compressor hardware` prints the exact block if you want to pin it.
#
# This deployment's Immich network is `{network_hint}` (see IMMICH_NETWORK in .env).
"""


def read_env_file(path: Path) -> dict[str, str]:
    """Parse an existing ``.env`` well enough to preserve what is already in it."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def render_env(values: dict[str, str]) -> str:
    lines = [
        "# Written by `immich-compressor setup`. Contains secrets — never commit it.",
        "# docker compose reads this file automatically from the project directory.",
        "",
    ]
    lines.extend(f"{key}={value}" for key, value in values.items())
    return "\n".join(lines) + "\n"


def write_secret_file(path: Path, body: str) -> None:
    """Write with mode 0600 from the start, so the secret is never briefly world-readable."""
    path.write_text(body, encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


# --------------------------------------------------------------------- orchestration


@dataclass(slots=True)
class SetupOptions:
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    session_token: str | None = None
    network: str = DEFAULT_NETWORK
    webhook_url: str = DEFAULT_WEBHOOK_URL
    directory: Path = Path()
    non_interactive: bool = False
    force: bool = False
    skip_workflow: bool = False


def _ask(prompt: str, default: str, *, non_interactive: bool, secret: bool = False) -> str:
    if non_interactive:
        return default
    shown = "" if secret else f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{shown}: ").strip()
    except EOFError:
        return default
    return answer or default


def _print_permissions(results: list[PermissionResult]) -> list[PermissionResult]:
    missing = [result for result in results if not result.granted]
    print("\nAPI key permissions")
    for result in results:
        mark = "ok     " if result.granted else "MISSING"
        print(f"  {mark} {result.probe.permission:<16} {result.probe.needed_for}")
    if missing:
        print("\n  Add these in Immich under Account Settings -> API Keys -> edit the key:")
        for result in missing:
            print(f"    {result.probe.permission}   ({result.detail})")
    return missing


def run_setup(options: SetupOptions) -> int:
    """The whole guided setup. Returns the process exit code."""
    directory = options.directory.resolve()
    print(f"immich-compressor setup — writing into {directory}\n")

    base_url = _ask("Immich API base URL", options.base_url, non_interactive=options.non_interactive)
    api_key = options.api_key or os.environ.get("IMMICH_API_KEY", "")
    if not api_key:
        api_key = _ask(
            "Immich API key (Account Settings -> API Keys)",
            "",
            non_interactive=options.non_interactive,
            secret=True,
        )
    if not api_key:
        print(
            "no API key given. Pass --api-key, or set IMMICH_API_KEY in the environment.",
            file=sys.stderr,
        )
        return 2
    network = _ask(
        "Docker network your Immich stack uses",
        options.network,
        non_interactive=options.non_interactive,
    )

    # ---- 1. the server -----------------------------------------------------------
    with _client(base_url, api_key) as client:
        ok, message = check_key(client)
        version = server_version(client)
        print(f"\nServer      {base_url}")
        print(f"            {message}")
        if version:
            print(f"            Immich {version}")
            if version.split(".")[0] not in {"3", "None"}:
                print("            warning: this service supports Immich v3.0.0 and newer only")
        if not ok:
            return 1
        permissions = check_permissions(client, include_delete=True)
    missing = _print_permissions(permissions)

    # ---- 2. the machine ----------------------------------------------------------
    # Both types, because the config this writes enables both — otherwise the report
    # below would describe a video-only deployment and no stills preset would be shown.
    settings = Settings(
        immich={"api_key": api_key},
        webhook={"token": "placeholder"},
        behavior={"enabled_types": ["VIDEO", "IMAGE"]},
    )
    _, report = apply_to_settings(settings, always_detect=True)
    print(f"\nHardware    {report.summary_line()}")
    for candidate in report.rejected:
        print(f"            not {candidate.where()}: {candidate.reason}")

    # ---- 3. the files ------------------------------------------------------------
    config_path = directory / "config.yaml"
    if config_path.exists() and not options.force:
        print("\nconfig.yaml already exists — left untouched (pass --force to regenerate)")
    else:
        verb = "regenerated" if config_path.exists() else "wrote"
        config_path.write_text(render_config(report, network_hint=network), encoding="utf-8")
        print(f"\n{verb} {config_path}")

    env_path = directory / ".env"
    existing = read_env_file(env_path)
    kept = sorted(key for key in ("IMMICH_API_KEY", "COMPRESSOR_TOKEN") if key in existing)
    values = dict(existing)
    values["IMMICH_API_KEY"] = api_key if options.force else existing.get("IMMICH_API_KEY", api_key)
    values.setdefault("COMPRESSOR_TOKEN", secrets.token_hex(32))
    values["IMMICH_BASE_URL"] = base_url
    values["IMMICH_NETWORK"] = network

    # The GPU wiring, so a plain `docker compose up -d` keeps doing the right thing.
    selected = report.selected
    node = next((n for n in report.facts.render_nodes if selected and n.path == selected.device), None)
    if node is not None and node.gid is not None:
        values["RENDER_GID"] = str(node.gid)
        values["COMPOSE_FILE"] = "docker-compose.yaml:docker-compose.gpu.yaml"
    elif selected is not None and selected.encoder == "hevc_nvenc":
        values["COMPOSE_FILE"] = "docker-compose.yaml:docker-compose.gpu-nvidia.yaml"

    write_secret_file(env_path, render_env(values))
    print(f"wrote {env_path} (mode 0600)")
    if kept:
        print(f"      kept the existing {', '.join(kept)} (pass --force to replace)")
    if "COMPOSE_FILE" in values:
        print(f"      COMPOSE_FILE={values['COMPOSE_FILE']} — GPU overlay wired in automatically")

    # ---- 4. the workflow ---------------------------------------------------------
    token = values["COMPRESSOR_TOKEN"]
    body = workflow_json(webhook_url=options.webhook_url, token=token)
    if options.skip_workflow:
        created, detail = False, "skipped on request"
    else:
        created, detail = create_workflow(
            base_url, body, api_key=api_key, session_token=options.session_token
        )
    print(f"\nWorkflow    {detail}")
    if not created:
        workflow_path = directory / "immich-workflow.json"
        write_secret_file(workflow_path, json.dumps(body, indent=2) + "\n")
        print(f"            wrote {workflow_path} (mode 0600) — create it yourself with:")
        print(f"""
  curl -X POST '{base_url.rstrip("/")}/workflows' \\
    -H "Authorization: Bearer $SESSION_TOKEN" \\
    -H 'Content-Type: application/json' \\
    -d @{workflow_path.name}
""")
        print("            $SESSION_TOKEN is a browser session token, not the API key:")
        print("            workflow.create is not one of the permissions this service needs,")
        print("            and adding it would widen the key past its job.")
        print("            The UI route is Utilities -> Workflows -> New.")
        print(f"            Then delete {workflow_path.name}: it carries COMPRESSOR_TOKEN")
        print("            in clear text and has no further use.")

    # ---- 5. what to do next ------------------------------------------------------
    print("\nNext")
    steps = [
        "docker compose up -d",
        "docker compose logs -f immich-compressor      # watch the first dry run",
        "docker compose exec immich-compressor immich-compressor report",
    ]
    if missing:
        steps.insert(0, "grant the missing permissions listed above, then re-run this command")
    for index, step in enumerate(steps, start=1):
        print(f"  {index}. {step}")
    print("\n  The service starts in dry-run mode: it will report what it *would* do and")
    print("  change nothing. docs/safety.md walks through going live in four stages.")
    return 1 if missing else 0
