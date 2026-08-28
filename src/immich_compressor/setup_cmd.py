"""The guided ``setup`` command: from an API key to a running service in one step.

Everything here is about removing the five things that make people give up halfway:
guessing which API-key permissions are needed, hand-writing a preset for their GPU,
inventing a webhook secret, working out the render group id, and assembling the workflow
JSON. Each of those is a question the machine can answer for itself.

The command is safe to run twice. Existing files are never silently replaced: values
already in ``.env`` are kept, an existing ``config.yaml`` is left alone unless ``--force``
says otherwise, and an existing ``docker-compose.override.yaml`` is left alone even then,
because that is where the go-live flags live. Each decision is printed.
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

from .config import Settings, workflow_file_pattern
from .hardware import HardwareReport, apply_to_settings

DEFAULT_BASE_URL = "http://immich-server:2283/api"
DEFAULT_NETWORK = "immich_default"
DEFAULT_WEBHOOK_URL = "http://immich-compressor:8080/webhook"
WEBHOOK_HEADER = "X-Compressor-Token"
COMPOSE_OVERRIDE = "docker-compose.override.yaml"
COMPOSE_OVERRIDE_TEMPLATE = "docker-compose.override.example.yaml"

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
                    "pattern": workflow_file_pattern(marker),
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
    base_url: str,
    body: dict[str, Any],
    *,
    api_key: str,
    session_token: str | None,
    workflow_key: str | None = None,
) -> tuple[bool, str]:
    """Create the workflow with the narrowest credential that was offered.

    ``workflow.create`` is not one of the compressor's own permissions, and granting it to
    the long-lived service key would widen that key well past what the service needs. That
    reasoning is right; what did not follow from it is that the permission may not be used
    at all. There are three ways in, ranked here by how much each can do if it leaks:

    1. ``--workflow-key``: a second API key carrying ``workflow.create`` and nothing else,
       created for this one call and deleted straight afterwards. The narrowest of the
       three, and the only one that needs neither the browser's developer tools nor a
       64-character secret typed into a web form.
    2. ``--session-token``: a browser session, which carries the user's *full* access.
    3. the service key, which only works if somebody widened it — and should not have.
    """
    if workflow_key:
        headers = {"x-api-key": workflow_key}
    elif session_token:
        headers = {"Authorization": f"Bearer {session_token}"}
    else:
        headers = {"x-api-key": api_key}
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


def render_env(values: dict[str, str], *, suggested: dict[str, str] | None = None) -> str:
    """Render the file. ``suggested`` becomes commented-out lines with a real value in them.

    `.env.example` documents `COMPRESSOR_CPUS` and `COMPRESSOR_MEMORY`, and nobody who
    takes the documented route ever reads it: `setup` writes a real `.env`, and a template
    stops being opened the moment a real file exists. The knobs therefore go in here as
    well — inert, but carrying the numbers this machine actually has.
    """
    lines = [
        "# Written by `immich-compressor setup`. Contains secrets — never commit it.",
        "# docker compose reads this file automatically from the project directory.",
        "",
    ]
    lines.extend(f"{key}={value}" for key, value in values.items())
    offered = {key: value for key, value in (suggested or {}).items() if key not in values}
    if offered:
        lines += [
            "",
            "# Optional, commented out so the shipped defaults stand. The values are what",
            "# this machine suggests.",
            "#",
            "# `cpus:` and `mem_limit:` in docker-compose.override.yaml beat COMPRESSOR_CPUS",
            "# and COMPRESSOR_MEMORY here — compose merges the override on top of the base",
            "# file, and a value set there wins over one substituted from this one.",
            "#",
            "# TZ is the clock this container's log timestamps with. Copy the value from",
            "# your Immich .env, or the two logs cannot be read side by side.",
        ]
        lines.extend(f"# {key}={value}" for key, value in offered.items())
    return "\n".join(lines) + "\n"


def optional_settings(report: HardwareReport) -> dict[str, str]:
    """The knobs worth putting in front of somebody, with real numbers rather than defaults.

    Half the host's cores, which is the rule of thumb everywhere else in the project:
    Immich's own thumbnailing, machine learning and transcoding want the other half.
    """
    return {
        "COMPRESSOR_CPUS": str(max(1, report.facts.cpu.host_cores // 2)),
        "COMPRESSOR_MEMORY": "2g",
        "TZ": "UTC",
    }


def write_secret_file(path: Path, body: str) -> None:
    """Write with mode 0600 from the start, so the secret is never briefly world-readable.

    `O_CREAT` applies its mode to a new file only, and umask can narrow it further, so the
    explicit chmod stays. It just has to run while the file is still empty: rewriting a
    0644 `.env` left by an older version would otherwise put the new secret in a
    world-readable file for as long as the write takes.
    """
    mode = stat.S_IRUSR | stat.S_IWUSR
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        path.chmod(mode)
        handle.write(body)


def compose_file_value(directory: Path, overlay: str) -> str:
    """The ``COMPOSE_FILE`` list that loads ``overlay`` without dropping the user's override.

    ``COMPOSE_FILE`` *replaces* compose's default file list, and
    ``docker-compose.override.yaml`` is only in that list by default. Naming an overlay
    here and nothing else therefore unloads the override silently, taking the go-live
    flags, the resource limits and any local image pin with it. It goes back on the end
    because the last file wins. Compose refuses to run at all on a file it cannot stat,
    so the entry is only added when the file is actually there.
    """
    parts = ["docker-compose.yaml", overlay]
    if (directory / COMPOSE_OVERRIDE).is_file():
        parts.append(COMPOSE_OVERRIDE)
    return ":".join(parts)


def ensure_compose_override(directory: Path) -> bool:
    """Give the deployment its own compose override, copied from the tracked template.

    ``COMPOSE_FILE`` can only name a file that exists, so on a GPU host the override has to
    be created *before* the line is rendered — otherwise the line is complete only for
    people who happened to write their override first, and the go-live flags everyone else
    adds later are read by nobody. The template is inert: every block in it is commented
    out, so creating it changes nothing about what the service does.

    An existing override is never touched, ``--force`` or not — it is where the go-live
    flags live, and regenerating it would quietly put a live deployment back into dry run.
    Returns whether a file was created.
    """
    target = directory / COMPOSE_OVERRIDE
    template = directory / COMPOSE_OVERRIDE_TEMPLATE
    if target.exists() or not template.is_file():
        return False
    target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    return True


# --------------------------------------------------------------------- orchestration


@dataclass(slots=True)
class SetupOptions:
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    session_token: str | None = None
    # A throwaway key holding `workflow.create` and nothing else. Used for exactly one
    # request and never written anywhere.
    workflow_key: str | None = None
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
    # The quickstart tells people to leave asset.delete out on purpose: without it the
    # service physically cannot remove an original, which is a guarantee worth having for
    # a first run. Granting it anyway looked identical to every other `ok` above, so the
    # guarantee went away without anybody being told.
    if any(result.probe.permission == "asset.delete" and result.granted for result in results):
        print("\n  Note: asset.delete is granted. This configuration never uses it —")
        print("  trash_original is off — but the key can remove assets from stage 3 on.")
    return missing


@dataclass(frozen=True, slots=True)
class _Connection:
    """The three answers every step after the first one needs."""

    base_url: str
    api_key: str
    network: str


def _ask_for_connection(options: SetupOptions) -> _Connection | None:
    """Where Immich is, the key to reach it with, and the network to join it on.

    ``None`` when there is no API key, which is the one answer this command can neither
    invent nor carry on without.
    """
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
        return None
    network = _ask(
        "Docker network your Immich stack uses",
        options.network,
        non_interactive=options.non_interactive,
    )
    return _Connection(base_url=base_url, api_key=api_key, network=network)


def _report_on_the_server(connection: _Connection) -> list[PermissionResult] | None:
    """Step 1: what the server says about this key, and which permissions it is missing.

    ``None`` means the key itself is unusable and there is nothing further to do. An empty
    list is the good case: usable, and nothing missing.
    """
    with _client(connection.base_url, connection.api_key) as client:
        ok, message = check_key(client)
        version = server_version(client)
        print(f"\nServer      {connection.base_url}")
        print(f"            {message}")
        if version:
            print(f"            Immich {version}")
            if version.split(".")[0] not in {"3", "None"}:
                print("            warning: this service supports Immich v3.0.0 and newer only")
        if not ok:
            return None
        permissions = check_permissions(client, include_delete=True)
    return _print_permissions(permissions)


def _report_on_the_machine(api_key: str) -> HardwareReport:
    """Step 2: the encoder this host gets, and every candidate that was rejected."""
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
    return report


def _write_the_files(
    options: SetupOptions, directory: Path, connection: _Connection, report: HardwareReport
) -> dict[str, str]:
    """Step 3: ``config.yaml`` and ``.env``, plus the GPU overlay when there is a GPU.

    Returns the environment values it wrote, because the next step needs the webhook token
    out of them — freshly generated here, or kept from an existing file.
    """
    config_path = directory / "config.yaml"
    if config_path.exists() and not options.force:
        print("\nconfig.yaml already exists — left untouched (pass --force to regenerate)")
    else:
        verb = "regenerated" if config_path.exists() else "wrote"
        config_path.write_text(render_config(report, network_hint=connection.network), encoding="utf-8")
        print(f"\n{verb} {config_path}")

    env_path = directory / ".env"
    existing = read_env_file(env_path)
    kept = sorted(key for key in ("IMMICH_API_KEY", "COMPRESSOR_TOKEN") if key in existing)
    values = dict(existing)
    values["IMMICH_API_KEY"] = (
        connection.api_key if options.force else existing.get("IMMICH_API_KEY", connection.api_key)
    )
    values.setdefault("COMPRESSOR_TOKEN", secrets.token_hex(32))
    values["IMMICH_BASE_URL"] = connection.base_url
    values["IMMICH_NETWORK"] = connection.network

    # Immich's compose file hands its container TZ and /etc/localtime; this one had
    # neither, so the two services timestamped their logs two hours apart while
    # troubleshooting.md asks people to correlate them line by line. `quickstart.sh` passes
    # TZ through, so it lands here when the host exported it; otherwise it is offered as a
    # commented line to fill in from Immich's own .env.
    if host_tz := os.environ.get("TZ", "").strip():
        values["TZ"] = host_tz

    # The GPU wiring, so a plain `docker compose up -d` keeps doing the right thing.
    selected = report.selected
    node = next((n for n in report.facts.render_nodes if selected and n.path == selected.device), None)
    overlay = None
    if node is not None and node.gid is not None:
        values["RENDER_GID"] = str(node.gid)
        overlay = "docker-compose.gpu.yaml"
    elif selected is not None and selected.encoder == "hevc_nvenc":
        overlay = "docker-compose.gpu-nvidia.yaml"
    if overlay is not None:
        if ensure_compose_override(directory):
            print(f"wrote {directory / COMPOSE_OVERRIDE} — put deployment-specific settings here")
        values["COMPOSE_FILE"] = compose_file_value(directory, overlay)

    write_secret_file(env_path, render_env(values, suggested=optional_settings(report)))
    print(f"wrote {env_path} (mode 0600)")
    if kept:
        print(f"      kept the existing {', '.join(kept)} (pass --force to replace)")
    if "COMPOSE_FILE" in values:
        print(f"      COMPOSE_FILE={values['COMPOSE_FILE']} — GPU overlay wired in automatically")
        if not (directory / COMPOSE_OVERRIDE).is_file():
            print(f"      no {COMPOSE_OVERRIDE} and no {COMPOSE_OVERRIDE_TEMPLATE} to copy —")
            print(f"      add :{COMPOSE_OVERRIDE} to that line yourself if you write one,")
            print("      because that line replaces the list compose loads by default")
    return values


def _create_the_workflow(options: SetupOptions, directory: Path, connection: _Connection, token: str) -> None:
    """Step 4: create the Immich workflow, or write it out with the curl that would."""
    body = workflow_json(webhook_url=options.webhook_url, token=token)
    if options.skip_workflow:
        created, detail = False, "skipped on request"
    else:
        created, detail = create_workflow(
            connection.base_url,
            body,
            api_key=connection.api_key,
            session_token=options.session_token,
            workflow_key=options.workflow_key,
        )
    print(f"\nWorkflow    {detail}")
    if created and options.workflow_key:
        # It is never written to .env, to config.yaml or to immich-workflow.json — but it
        # still exists in Immich, and it has no second use.
        print("            delete that API key in Immich now: it was needed for one call,")
        print("            and nothing here has stored it.")
    if not created:
        workflow_path = directory / "immich-workflow.json"
        write_secret_file(workflow_path, json.dumps(body, indent=2) + "\n")
        print(f"            wrote {workflow_path} (mode 0600) — create it yourself with:")
        print(f"""
  curl -X POST '{connection.base_url.rstrip("/")}/workflows' \\
    -H "Authorization: Bearer $SESSION_TOKEN" \\
    -H 'Content-Type: application/json' \\
    -d @{workflow_path.name}
""")
        print("            $SESSION_TOKEN is a browser session token, not the API key:")
        print("            workflow.create is not one of the permissions this service needs,")
        print("            and adding it would widen the key past its job.")
        print("            Or make a second API key with only workflow.create and re-run:")
        print("              immich-compressor setup --workflow-key <that key>")
        print("            then delete it again — it is used for this one request.")
        print("            The UI route is Utilities -> Workflows -> New.")
        print("            docs/workflow-setup.md has the full JSON and the gotchas.")
        print(f"            Then delete {workflow_path.name}: it carries COMPRESSOR_TOKEN")
        print("            in clear text and has no further use.")


def _print_next_steps(missing: list[PermissionResult]) -> None:
    """Step 5: the three commands that follow, and the permission fix when one is due."""
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


def run_setup(options: SetupOptions) -> int:
    """The whole guided setup, in the five steps it prints. Returns the process exit code.

    Each step is where its own output comes from, and the order is the order a person reads
    it in: what the server says, what the machine can do, what was written, what Immich now
    has, and what to run next.
    """
    directory = options.directory.resolve()
    print(f"immich-compressor setup — writing into {directory}\n")

    connection = _ask_for_connection(options)
    if connection is None:
        return 2
    missing = _report_on_the_server(connection)
    if missing is None:
        return 1
    report = _report_on_the_machine(connection.api_key)
    values = _write_the_files(options, directory, connection, report)
    _create_the_workflow(options, directory, connection, values["COMPRESSOR_TOKEN"])
    _print_next_steps(missing)
    return 1 if missing else 0
