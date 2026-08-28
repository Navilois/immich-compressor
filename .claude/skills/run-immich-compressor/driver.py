#!/usr/bin/env python3
"""Launch and drive immich-compressor end to end against a throwaway Immich.

This is agent tooling, not product surface. It exists so a future agent can see the
ten-step pipeline actually run — download, encode, sanity gate, upload, carry-over — and
not just read about it.

    .venv/bin/python .claude/skills/run-immich-compressor/driver.py all

Every command is idempotent and safe to re-run. State lives in ``testinstance/driver/``,
which is already gitignored.

Commands
--------
  up          start the throwaway Immich (docker-compose.test.yaml), wait for it, create
              the admin account and an API key, persist them to testinstance/driver/env.json
  serve       start `immich-compressor serve` in the background against that Immich
  smoke       upload a clip, fire the webhook, poll the job to a terminal state, print it
  api         probe /healthz, /stats, /metrics, /jobs on the running service
  thumbnail   save Immich's preview of a replacement to a JPEG and print its geometry —
              the closest thing this headless service has to looking at the result
  live        run the project's own `live`-marked pytest suite against this instance,
              with all four E2E_* variables wired in (all five tests, no skips)
  cli ARGS... run any immich-compressor subcommand with the live environment wired in
  logs        tail the service log
  stop        stop the background service
  down        stop the service and the Immich stack (--purge also deletes Immich's data)
  status      what is up right now
  all         up + serve + smoke, the whole thing from cold

Flags
-----
  --dry-run          run the pipeline in dry_run mode (it decides, then changes nothing)
  --trash            also remove the original after the four-step verification chain.
                     Off by default. Only ever point this at the throwaway instance.
  --port N           host port of the throwaway Immich (default 2283)
  --service-port N   port the compressor listens on (default 18080)
  --purge            with `down`: also delete testinstance/library and testinstance/postgres
  --initial-delay N  behavior.initial_delay_seconds. Ships as 300 — every job waits five
                     minutes before a worker claims it. The driver defaults to 0.
  --retention-days N behavior.retention_days. Ships as 7 — the original is trashed a week
                     after the replacement verifies. The driver defaults to 0, which
                     trashes it inline so --trash has something to observe.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

# --------------------------------------------------------------------------------------
# Layout. Paths are anchored to the repository root, which is three levels up from here
# (.claude/skills/run-immich-compressor/driver.py), so the driver works from any cwd.
# --------------------------------------------------------------------------------------
REPO = Path(__file__).resolve().parents[3]
RUN = REPO / "testinstance" / "driver"
ENV_FILE = RUN / "env.json"
PID_FILE = RUN / "serve.pid"
LOG_FILE = RUN / "serve.log"
DB_FILE = RUN / "state.db"
WORK_DIR = RUN / "work"
COMPOSE = ["docker", "compose", "--env-file", "testinstance/.env", "-f", "docker-compose.test.yaml"]

ADMIN_EMAIL = "e2e@example.invalid"
ADMIN_PASSWORD = "e2e-throwaway-pw"  # noqa: S105 - a throwaway instance, by design
WEBHOOK_TOKEN = "driver-token"  # noqa: S105 - same
MARKER = "driver"


def log(message: str) -> None:
    print(f"==> {message}", flush=True)


def fail(message: str) -> None:
    print(f"!!! {message}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - every call site passes a literal argv, never a shell string
        cmd,
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
        **kwargs,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------------------
# The throwaway Immich
# --------------------------------------------------------------------------------------
def immich_base(args: argparse.Namespace) -> str:
    return f"http://127.0.0.1:{args.port}/api"


def wait_for_immich(base: str, timeout_s: int = 300) -> str:
    """Poll /server/ping until the server answers, then return its version."""
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base}/server/ping", timeout=5)
            if response.status_code == 200:
                version = httpx.get(f"{base}/server/version", timeout=5).json()
                return f"{version['major']}.{version['minor']}.{version['patch']}"
        except httpx.HTTPError as exc:  # not up yet, or still migrating the database
            last = type(exc).__name__
        time.sleep(5)
    fail(f"Immich did not answer {base}/server/ping within {timeout_s}s (last: {last})")
    raise AssertionError("unreachable")


def bootstrap(base: str) -> dict[str, str]:
    """Create the admin account (once) and mint an API key. Idempotent."""
    signup = httpx.post(
        f"{base}/auth/admin-sign-up",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "name": "Driver Admin"},
        timeout=30,
    )
    if signup.status_code == 201:
        log(f"created admin {ADMIN_EMAIL}")
    elif signup.status_code == 400:
        log("admin already exists")  # "The server already has an admin"
    else:
        fail(f"admin-sign-up answered {signup.status_code}: {signup.text[:300]}")

    login = httpx.post(
        f"{base}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=30
    )
    if login.status_code != 201:
        fail(f"login answered {login.status_code}: {login.text[:300]}")
    token = login.json()["accessToken"]

    key = httpx.post(
        f"{base}/api-keys",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": f"driver-{uuid.uuid4().hex[:8]}", "permissions": ["all"]},
        timeout=30,
    )
    if key.status_code != 201:
        fail(f"api-keys answered {key.status_code}: {key.text[:300]}")
    log("minted an API key with permissions: [all]")
    return {"base_url": base, "api_key": key.json()["secret"], "session_token": token}


def load_env() -> dict[str, str]:
    if not ENV_FILE.exists():
        fail(f"{ENV_FILE} is missing — run `driver.py up` first")
    return json.loads(ENV_FILE.read_text())


def cmd_up(args: argparse.Namespace) -> None:
    RUN.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    # testinstance/.env is gitignored — only example.env is tracked — so on a fresh clone
    # the --env-file the compose command names does not exist yet, and compose fails with
    # a bare "env file not found". This is the `cp` the compose file's own header asks for.
    compose_env = REPO / "testinstance" / ".env"
    if not compose_env.exists():
        compose_env.write_text((REPO / "testinstance" / "example.env").read_text())
        log(f"created {compose_env.relative_to(REPO)} from example.env")
    env = os.environ | {"COMPOSE_HOST_PORT": str(args.port)}
    log("starting the throwaway Immich (immich-test)")
    result = subprocess.run(  # noqa: S603
        [*COMPOSE, "up", "-d"], cwd=REPO, text=True, env=env, check=False
    )
    if result.returncode != 0:
        fail("docker compose up failed")
    base = immich_base(args)
    log(f"waiting for {base}")
    version = wait_for_immich(base)
    log(f"Immich {version} is up")
    ENV_FILE.write_text(json.dumps(bootstrap(base), indent=2) + "\n")
    log(f"credentials written to {ENV_FILE.relative_to(REPO)}")


# --------------------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------------------
def service_env(args: argparse.Namespace) -> dict[str, str]:
    """The full environment `immich-compressor` needs to talk to the throwaway Immich.

    CONFIG_PATH is pointed at a file that does not exist on purpose: the repo has no
    config.yaml, but a developer's leftover one would otherwise silently steer the run.
    """
    stored = load_env()
    return os.environ | {
        "IMMICH__BASE_URL": stored["base_url"],
        "IMMICH__API_KEY": stored["api_key"],
        "WEBHOOK__TOKEN": WEBHOOK_TOKEN,
        "CONFIG_PATH": str(RUN / "no-such-config.yaml"),
        "DATABASE_PATH": str(DB_FILE),
        "BEHAVIOR__WORK_DIR": str(WORK_DIR),
        "BEHAVIOR__DRY_RUN": "true" if args.dry_run else "false",
        "BEHAVIOR__TRASH_ORIGINAL": "true" if getattr(args, "trash", False) else "false",
        # Two production defaults that make a smoke run unobservable, overridden here on
        # purpose and printed by `serve` so nobody mistakes them for the shipped values:
        #   initial_delay_seconds 300 — every job waits five minutes before a worker
        #     claims it, so the default turns a 40-second run into a 5.5-minute one.
        #   retention_days 7 — the original is trashed a week after the replacement is
        #     verified, so --trash would otherwise leave nothing to observe. 0 means
        #     "inline in the job, as soon as the verification chain passes".
        "BEHAVIOR__INITIAL_DELAY_SECONDS": str(args.initial_delay),
        "BEHAVIOR__RETENTION_DAYS": str(args.retention_days),
        # The webhook the driver fires carries a fresh createdAt, but Immich's own
        # workflow would too — the gate is about bulk re-triggers, not about us.
        "BEHAVIOR__MAX_ASSET_AGE_HOURS": "24",
        "LISTEN_HOST": "127.0.0.1",
        "LISTEN_PORT": str(args.service_port),
        "LOG_LEVEL": "INFO",
    }


def service_url(args: argparse.Namespace) -> str:
    return f"http://127.0.0.1:{args.service_port}"


def port_in_use(port: int) -> bool:
    """uvicorn's own failure for a taken port arrives after startup has already logged
    'Application startup complete', which reads like a success. Check first."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def service_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
    except (ValueError, ProcessLookupError, PermissionError):
        return None
    return pid


def cmd_serve(args: argparse.Namespace) -> None:
    if (pid := service_pid()) is not None:
        log(f"service already running (pid {pid}); restarting it")
        cmd_stop(args)
    RUN.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    if port_in_use(args.service_port):
        fail(
            f"port {args.service_port} is already bound by something else — "
            f"pick another with --service-port N (this container already runs other stacks)"
        )
    binary = REPO / ".venv" / "bin" / "immich-compressor"
    if not binary.exists():
        fail(
            f"{binary} is missing — run `make dev`, or "
            f"`python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`"
        )
    handle = LOG_FILE.open("wb")
    process = subprocess.Popen(  # noqa: S603
        [str(binary), "serve"],
        cwd=REPO,
        env=service_env(args),
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    PID_FILE.write_text(f"{process.pid}\n")
    url = service_url(args)
    log(f"started `immich-compressor serve` as pid {process.pid}, log: {LOG_FILE.relative_to(REPO)}")
    log(
        f"overrides: dry_run={args.dry_run} trash_original={getattr(args, 'trash', False)} "
        f"initial_delay_seconds={args.initial_delay} (ships as 300) "
        f"retention_days={args.retention_days} (ships as 7)"
    )

    # Hardware detection runs a real one-frame encode per candidate before the socket is
    # bound, so the first start is slow even on a machine with no GPU at all.
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if process.poll() is not None:
            print(LOG_FILE.read_text()[-3000:], file=sys.stderr)
            fail(f"the service exited with code {process.returncode} before binding {url}")
        try:
            health = httpx.get(f"{url}/healthz", timeout=5)
            if health.status_code == 200:
                body = health.json()
                log(f"healthz: {json.dumps(body)}")
                return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    fail(f"{url}/healthz never answered 200")


def cmd_stop(args: argparse.Namespace) -> None:
    pid = service_pid()
    if pid is None:
        log("no service running")
        PID_FILE.unlink(missing_ok=True)
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        time.sleep(0.5)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
    else:
        os.kill(pid, signal.SIGKILL)
    PID_FILE.unlink(missing_ok=True)
    log(f"stopped pid {pid}")


def cmd_logs(args: argparse.Namespace) -> None:
    if not LOG_FILE.exists():
        fail(f"{LOG_FILE} does not exist yet")
    print(LOG_FILE.read_text()[-args.bytes :], end="")


# --------------------------------------------------------------------------------------
# The smoke test: one asset all the way through the pipeline
# --------------------------------------------------------------------------------------
def make_fat_clip(path: Path) -> Path:
    """A deliberately over-bitrate MPEG-4 clip, so h265 has an easy win.

    The noise overlay and the random audio frequency make every call's bytes unique:
    ffmpeg's synthetic sources are deterministic and Immich deduplicates by checksum, so
    a second identical clip comes back as ``status: "duplicate"`` and the smoke test then
    drives an asset it did not upload. Lifted from tests/test_e2e_live.py.
    """
    nonce = uuid.uuid4().hex
    frequency = 400 + (int(nonce[:4], 16) % 200)
    result = run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=640x480:rate=25:duration=6",
            "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration=6",
            "-f", "lavfi", "-i",
            "nullsrc=size=64x64:rate=25:duration=6,geq=random(1)*255:128:128,format=yuv420p",
            "-filter_complex", "[0:v][2:v]overlay=x=0:y=0[v]",
            "-map", "[v]", "-map", "1:a",
            "-c:v", "mpeg4", "-b:v", "9000k",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            "-metadata", "creation_time=2024-06-15T12:30:00Z",
            "-metadata", f"comment={nonce}",
            str(path),
        ]
    )  # fmt: skip
    if result.returncode != 0:
        fail(f"ffmpeg failed: {result.stderr[-500:]}")
    if shutil.which("exiftool"):
        # Bake GPS in the way a camera does. Immich derives timeZone from GPS and
        # localDateTime from dateTimeOriginal in that zone; setting GPS through the API
        # after upload leaves localDateTime stale.
        run(["exiftool", "-quiet", "-overwrite_original", "-api", "QuickTimeUTC=1",
             "-Keys:GPSCoordinates=48.2082, 16.3738", str(path)])  # fmt: skip
    return path


def upload_clip(base: str, key: str, clip: Path) -> str:
    stamp = datetime.now(UTC).strftime("%H%M%S")
    name = f"{MARKER}-{stamp}-{uuid.uuid4().hex[:6]}.mp4"
    response = httpx.post(
        f"{base}/assets",
        headers={"x-api-key": key},
        files={"assetData": (name, clip.read_bytes(), "video/mp4")},
        data={
            "fileCreatedAt": "2024-06-15T12:30:00.000Z",
            "fileModifiedAt": "2024-06-15T12:30:00.000Z",
            "filename": name,
            "duration": "6000",
        },
        timeout=300,
    )
    if response.status_code not in (200, 201):
        fail(f"upload answered {response.status_code}: {response.text[:300]}")
    body = response.json()
    if body.get("status") == "duplicate":
        fail("Immich called the upload a duplicate — the clip bytes were not unique")
    return body["id"]


def wait_for_metadata(base: str, key: str, asset_id: str, timeout_s: int = 120) -> dict:
    """Wait until Immich has run metadata extraction over the asset.

    The webhook the real workflow sends fires *after* this. Firing it earlier means the
    payload carries an exifInfo Immich has not filled in yet, and the pipeline's own
    carry-over then compares against nothing.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        response = httpx.get(f"{base}/assets/{asset_id}", headers={"x-api-key": key}, timeout=30)
        if response.status_code == 200:
            asset = response.json()
            if (asset.get("exifInfo") or {}).get("dateTimeOriginal"):
                return asset
        time.sleep(2)
    fail(f"metadata extraction did not finish for {asset_id} within {timeout_s}s")
    raise AssertionError("unreachable")


def webhook_payload(asset: dict) -> dict:
    """The AssetV1 envelope Immich's workflow sends, filled in from the live asset.

    tests/fixtures/webhook_video.json is a payload captured verbatim from a live v3.1.0
    instance; using it as the template keeps this in step with the shape the ingest path
    was written against, rather than with a shape invented here.
    """
    template = json.loads((REPO / "tests" / "fixtures" / "webhook_video.json").read_text())
    template["data"]["asset"] = asset
    return template


def poll_job(service: str, asset_id: str, timeout_s: int = 900) -> dict:
    """Poll /jobs/{asset_id} until the job reaches a state it will not leave on its own.

    The states are models.JobState. `pending_delete` belongs in this set and is easy to
    miss: with the shipped retention_days=7 a --trash job parks there for a week waiting
    for the sweeper, so a poller that only waits for `done` never returns. `uploaded` and
    `linked` are mid-pipeline and do move on.
    """
    terminal = {"done", "failed", "skipped", "pending_delete"}
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        response = httpx.get(f"{service}/jobs/{asset_id}", timeout=30)
        if response.status_code == 200:
            last = response.json()
            state = str(last.get("state", "")).lower()
            if state in terminal:
                return last
        time.sleep(3)
    fail(f"job for {asset_id} never reached a terminal state (last: {json.dumps(last)[:400]})")
    raise AssertionError("unreachable")


def cmd_smoke(args: argparse.Namespace) -> None:
    stored = load_env()
    base, key = stored["base_url"], stored["api_key"]
    service = service_url(args)
    if service_pid() is None:
        fail("no service running — run `driver.py serve` first")

    clip = RUN / "clip.mp4"
    log("building a fat MPEG-4 clip")
    make_fat_clip(clip)
    log(f"clip is {clip.stat().st_size} bytes")

    asset_id = upload_clip(base, key, clip)
    log(f"uploaded asset {asset_id}")
    asset = wait_for_metadata(base, key, asset_id)
    log(f"metadata extracted: {asset['originalFileName']}, {asset['exifInfo']['dateTimeOriginal']}")

    response = httpx.post(
        f"{service}/webhook",
        headers={"X-Compressor-Token": WEBHOOK_TOKEN, "Content-Type": "application/json"},
        json=webhook_payload(asset),
        timeout=60,
    )
    log(f"POST /webhook -> {response.status_code} {response.text[:200]}")
    if response.status_code not in (200, 202):
        fail("the webhook was refused")

    log("polling the job")
    job = poll_job(service, asset_id)
    print(json.dumps(job, indent=2))

    state = str(job.get("state", "")).lower()
    if state == "failed":
        print(LOG_FILE.read_text()[-3000:], file=sys.stderr)
        fail(f"the pipeline failed: {job.get('last_error')}")
    if state == "skipped":
        log(f"the pipeline skipped this asset: {job.get('skip_reason')}")
    if args.dry_run:
        log(f"dry run finished in state {state} — nothing was uploaded, by design")
        return

    # The replacement is a *new* asset. Find it and prove it is smaller and playable.
    replacement = job.get("new_asset_id") or job.get("replacement_id")
    if not replacement:
        log(f"job finished in state {state} with no replacement id — see the job above")
        return
    new = httpx.get(f"{base}/assets/{replacement}", headers={"x-api-key": key}, timeout=30).json()
    old_size = (asset.get("exifInfo") or {}).get("fileSizeInByte")
    new_size = (new.get("exifInfo") or {}).get("fileSizeInByte")
    log(f"replacement {replacement}: {new['originalFileName']}")
    log(f"original {old_size} bytes -> replacement {new_size} bytes")
    if old_size and new_size and new_size >= old_size:
        fail("the replacement is not smaller than the original")

    # Was the original removed? Only with --trash, and only once the four-step
    # verification chain passed. `delete_after` in the future means it is parked for the
    # sweeper; `original_freed_at` set means it is gone from the timeline.
    source = httpx.get(f"{base}/assets/{asset_id}", headers={"x-api-key": key}, timeout=30)
    trashed = source.status_code == 200 and bool(source.json().get("isTrashed"))
    log(f"original: state={state} original_freed_at={job.get('original_freed_at')} "
        f"delete_after={job.get('delete_after')} isTrashed={trashed}")  # fmt: skip
    if getattr(args, "trash", False) and not trashed and not job.get("original_freed_at"):
        log("note: the original is still in the timeline — raise --retention-days 0 or "
            "wait for the sweeper")  # fmt: skip
    log("PASS: the pipeline compressed, verified and uploaded a real asset")


# --------------------------------------------------------------------------------------
# Poking the running service
# --------------------------------------------------------------------------------------
def cmd_api(args: argparse.Namespace) -> None:
    service = service_url(args)
    if service_pid() is None:
        fail("no service running — run `driver.py serve` first")
    for path in ("/healthz", "/stats", "/jobs"):
        response = httpx.get(f"{service}{path}", timeout=30)
        body = (
            json.dumps(response.json(), indent=2)
            if response.headers.get("content-type", "").startswith("application/json")
            else response.text
        )
        print(f"--- GET {path} -> {response.status_code}\n{body[:2000]}")
    metrics = httpx.get(f"{service}/metrics", timeout=30)
    lines = [line for line in metrics.text.splitlines() if line and not line.startswith("#")]
    print(f"--- GET /metrics -> {metrics.status_code}, {len(lines)} sample lines")
    print("\n".join(lines[:20]))


def cmd_thumbnail(args: argparse.Namespace) -> None:
    """Save Immich's own preview of an asset to a JPEG, and print its geometry.

    This service has no UI of its own, so this is as close to "look at the result" as it
    gets: a thumbnail that decodes at the right size proves the replacement is a real,
    playable, correctly-oriented video and not a corrupt container that merely has the
    right byte count. With no id, it picks the newest job's replacement.
    """
    stored = load_env()
    base, key = stored["base_url"], stored["api_key"]
    asset_id = args.rest[0] if args.rest else None
    if asset_id is None:
        jobs = httpx.get(f"{service_url(args)}/jobs", timeout=30).json().get("jobs", [])
        replacements = [job["new_asset_id"] for job in jobs if job.get("new_asset_id")]
        if not replacements:
            fail("no job has a replacement yet — run `driver.py smoke` first")
        asset_id = replacements[0]
        log(f"newest replacement: {asset_id}")
    out = RUN / f"thumbnail-{asset_id[:8]}.jpg"
    response = httpx.get(
        f"{base}/assets/{asset_id}/thumbnail",
        params={"size": "preview"},
        headers={"x-api-key": key},
        timeout=60,
    )
    if response.status_code != 200:
        fail(f"thumbnail answered {response.status_code}: {response.text[:200]}")
    out.write_bytes(response.content)
    log(f"wrote {out} ({len(response.content)} bytes)")
    if shutil.which("identify"):
        print(run(["identify", str(out)]).stdout.strip())


def cmd_live(args: argparse.Namespace) -> None:
    """Run the project's own `live`-marked suite against the throwaway instance.

    Two of its five tests drive POST /sync/stream, which no API key can open — Immich
    answers 403 on every /sync route — so they need the password of the account that owns
    the key. The driver's admin is that account, so all five run here. `-rs` because a
    skip in this suite is not a pass: read the summary, not the exit code.
    """
    stored = load_env()
    env = os.environ | {
        "E2E_IMMICH_URL": stored["base_url"],
        "E2E_IMMICH_KEY": stored["api_key"],
        "E2E_IMMICH_EMAIL": ADMIN_EMAIL,
        "E2E_IMMICH_PASSWORD": ADMIN_PASSWORD,
    }
    result = subprocess.run(  # noqa: S603
        [str(REPO / ".venv" / "bin" / "python"), "-m", "pytest", "-m", "live", "-q", "-rs", *args.rest],
        cwd=REPO,
        env=env,
        check=False,
    )
    raise SystemExit(result.returncode)


def cmd_cli(args: argparse.Namespace) -> None:
    binary = REPO / ".venv" / "bin" / "immich-compressor"
    result = subprocess.run(  # noqa: S603
        [str(binary), *args.rest], cwd=REPO, env=service_env(args), check=False
    )
    raise SystemExit(result.returncode)


def cmd_status(args: argparse.Namespace) -> None:
    pid = service_pid()
    print(f"service:  {'running, pid ' + str(pid) if pid else 'stopped'}  ({service_url(args)})")
    print(f"env file: {'present' if ENV_FILE.exists() else 'missing'}  ({ENV_FILE})")
    result = run([*COMPOSE, "ps", "--format", "{{.Service}}\t{{.State}}"])
    print("immich:   " + (result.stdout.strip().replace("\n", "\n          ") or "not running"))


def cmd_down(args: argparse.Namespace) -> None:
    cmd_stop(args)
    env = os.environ | {"COMPOSE_HOST_PORT": str(args.port)}
    subprocess.run([*COMPOSE, "down"], cwd=REPO, env=env, check=False)  # noqa: S603
    if args.purge:
        # Immich's data is written by root inside the container, so this needs sudo.
        for path in (REPO / "testinstance" / "library", REPO / "testinstance" / "postgres", RUN):
            subprocess.run(["sudo", "rm", "-rf", str(path)], check=False)  # noqa: S603, S607
        log("purged testinstance/library, testinstance/postgres and testinstance/driver")


def cmd_all(args: argparse.Namespace) -> None:
    cmd_up(args)
    cmd_serve(args)
    cmd_smoke(args)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--port", type=int, default=2283, help="host port of the throwaway Immich")
    parser.add_argument("--service-port", type=int, default=18080, help="port the compressor listens on")
    parser.add_argument("--dry-run", action="store_true", help="run the pipeline in dry_run mode")
    parser.add_argument("--trash", action="store_true", help="also remove the original (throwaway only)")
    parser.add_argument("--purge", action="store_true", help="with `down`: delete Immich's data too")
    parser.add_argument("--bytes", type=int, default=8000, help="with `logs`: how much tail to print")
    parser.add_argument(
        "--initial-delay", type=int, default=0,
        help="behavior.initial_delay_seconds (ships as 300; 0 so a smoke run finishes)",
    )  # fmt: skip
    parser.add_argument(
        "--retention-days", type=int, default=0,
        help="behavior.retention_days (ships as 7; 0 trashes the original inline)",
    )  # fmt: skip
    parser.add_argument("command", choices=[
        "up", "serve", "smoke", "api", "thumbnail", "live", "cli", "logs", "stop",
        "down", "status", "all",
    ])  # fmt: skip

    # `cli` has to forward arbitrary flags (`cli restore --all-pending`) to
    # immich-compressor, and argparse.REMAINDER is the only way to get them through
    # untouched — but REMAINDER also swallows the driver's own flags when they come after
    # the command, so `driver.py serve --trash` silently ran without --trash. Split the
    # argv by hand instead: everything after a literal `cli` is the passthrough, and the
    # driver's flags then parse from either side of the command name.
    argv = sys.argv[1:]
    rest: list[str] = []
    for passthrough in ("cli", "thumbnail", "live"):
        if passthrough in argv:
            index = argv.index(passthrough)
            argv, rest = [*argv[:index], passthrough], argv[index + 1 :]
            break
    args = parser.parse_args(argv)
    args.rest = rest
    globals()[f"cmd_{args.command}"](args)


if __name__ == "__main__":
    main()
