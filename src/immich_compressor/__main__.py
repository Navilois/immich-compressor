"""The command line: one function per subcommand, and the parser that wires them up.

The user-facing description is :data:`_DESCRIPTION` rather than this docstring. A module
docstring is written for whoever opens the file; it went out through ``--help`` verbatim
once, RST backticks and all, listing seven of the twelve commands.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__, backfill
from .api import ImmichClient, ImmichError
from .config import ConfigError, Settings, load_settings
from .encoder import (
    EncodeError,
    MediaProbe,
    check_sanity,
    embedded_media_reason,
    encode,
    jpeg_quality,
    probe,
    probe_hardware_encoder,
    verify_metadata,
)
from .hardware import HardwareReport, apply_to_settings, format_report
from .models import JobState, PauseState, SkipReason
from .setup_cmd import (
    DEFAULT_BASE_URL,
    DEFAULT_NETWORK,
    DEFAULT_WEBHOOK_URL,
    SetupOptions,
    run_setup,
)
from .store import WEBHOOKS_RECEIVED, WEBHOOKS_REJECTED, JobStore

logger = logging.getLogger("immich_compressor")


def _configure_logging(level: str) -> None:
    resolved = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=resolved,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # `basicConfig` configures the root logger once and is a no-op on every later call,
    # level included. `serve` calls this twice on purpose — see `cmd_serve` — so the level
    # is set here rather than left to whichever call happened to come first.
    logging.getLogger().setLevel(resolved)


def _load(args: argparse.Namespace, *, require_secrets: bool = True, autodetect: bool = True) -> Settings:
    return load_settings(
        Path(args.config) if args.config else None,
        require_secrets=require_secrets,
        autodetect=autodetect,
    )


# ---------------------------------------------------------------------------- commands


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .server import create_app

    # Logging first, settings second. Loading the settings is what runs hardware
    # detection, and detection logs the encoder it picked along with the reason every
    # other candidate was rejected — the explanation docs/quickstart.md points at. With no
    # handler configured yet those lines went out through `logging.lastResort`, which
    # drops everything below WARNING, so the whole explanation was discarded on every
    # single start and the log opened on a preset probe with no "why" anywhere near it.
    #
    # LOG_LEVEL is read straight from the environment because the settings that would
    # carry it are precisely what has not been loaded yet.
    _configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    settings = _load(args)
    _configure_logging(settings.log_level)
    logger.info(
        "starting immich-compressor on %s:%d (dry_run=%s, trash_original=%s, delete_mode=%s)",
        settings.listen_host,
        settings.listen_port,
        settings.behavior.dry_run,
        settings.behavior.trash_original,
        settings.behavior.delete_mode,
    )
    uvicorn.run(
        create_app(settings),
        host=settings.listen_host,
        port=settings.listen_port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )
    return 0


def _webhook_summary(counters: dict[str, int]) -> list[str]:
    """The line that separates "nothing has happened yet" from "nothing can happen".

    A shared secret that does not match leaves no other trace anywhere. Immich discards the
    401 and logs the workflow as executed successfully, no job row is written, and every
    other line of `check` and `report` reads exactly like a healthy installation with an
    empty queue. Without this the only evidence is one WARNING in the container log, which
    only somebody already running `logs -f` will ever see.
    """
    received = counters.get(WEBHOOKS_RECEIVED, 0)
    rejected = counters.get(WEBHOOKS_REJECTED, 0)
    lines = [f"webhooks: {received} received, {rejected} rejected (bad or missing token)"]
    if rejected and not received:
        lines.append(
            "  not one webhook has been accepted: the workflow's `headerValue` and "
            "WEBHOOK__TOKEN disagree — see docs/workflow-setup.md"
        )
    elif rejected:
        lines.append("  some were refused; the container log names the token that arrived")
    return lines


async def _webhook_lines(settings: Settings) -> list[str]:
    """Read the counters, but never create the database just to report on it.

    `check` is meant to be usable before anything has run, and on a host where the state
    directory may not exist or may not be writable.
    """
    if not settings.database_path.is_file():
        return []
    async with JobStore(settings.database_path) as store:
        return _webhook_summary(await store.counters())


async def _check(settings: Settings) -> int:
    async with ImmichClient(
        settings.immich.base_url,
        settings.immich.api_key.get_secret_value(),
        timeout_s=settings.immich.timeout_s,
    ) as client:
        version = await client.server_version()
    print(f"Immich reachable, version {version}")
    print(f"presets: {', '.join(f'{p.name}({p.match_type})' for p in settings.presets) or 'none'}")
    print(f"dry_run={settings.behavior.dry_run} trash_original={settings.behavior.trash_original}")
    for line in await _webhook_lines(settings):
        print(line)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Config, connectivity and hardware in one command.

    The hardware half is the `hardware` command: everything a GPU problem needs is there,
    and duplicating a shorter version of it here only invited the two to disagree.
    """
    settings = _load(args)
    _configure_logging(settings.log_level)
    result = asyncio.run(_check(settings))

    report = _hardware_report(settings)
    selected = report.selected
    if report.explicit_presets:
        print("encoder: presets come from config.yaml — see `immich-compressor hardware`")
    elif selected is None:
        print("encoder: NONE confirmed, falling back to CPU — run `immich-compressor hardware`")
        result = result or 1
    else:
        where = f" on {selected.device}" if selected.device else ""
        print(f"encoder: {selected.encoder}{where} confirmed by a one-frame test encode")

    # A hand-written preset that names a GPU still deserves the probe `check` always did.
    for preset in settings.presets:
        encoder_name = preset.hardware_encoder
        if encoder_name is None:
            continue
        problem = asyncio.run(probe_hardware_encoder(encoder_name, preset.render_node))
        if problem is None:
            print(f"  {preset.name}: {encoder_name} on {preset.render_node} ok")
        else:
            print(f"  {preset.name}: {encoder_name} on {preset.render_node} UNUSABLE — {problem}")
            result = result or 1
    return result


def _hardware_report(settings: Settings) -> HardwareReport:
    _, report = apply_to_settings(settings, always_detect=True)
    return report


def cmd_setup(args: argparse.Namespace) -> int:
    """Guided first-run setup. Deliberately does not load config.yaml — it writes one."""
    _configure_logging("WARNING")
    return run_setup(
        SetupOptions(
            base_url=args.url,
            api_key=args.api_key or "",
            session_token=args.session_token,
            workflow_key=args.workflow_key,
            network=args.network,
            webhook_url=args.webhook_url,
            directory=Path(args.directory),
            non_interactive=args.non_interactive,
            force=args.force,
            skip_workflow=args.no_workflow,
        )
    )


def cmd_hardware(args: argparse.Namespace) -> int:
    """Explain, in one command, which encoder this machine gets and why.

    Deliberately usable before anything else is configured: no API key, no reachable
    server, no config file. Somebody evaluating the project should be able to run this
    against their box and see the answer.
    """
    settings = _load(args, require_secrets=False, autodetect=False)
    _configure_logging("WARNING")
    report = _hardware_report(settings)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(format_report(report))
    return 0


async def _encode_local(settings: Settings, path: Path, asset_type: str) -> int:
    # Matched by name too, so `encode photo.png` reports the same "no preset" the pipeline
    # would rather than silently running the JPEG recipe against it.
    preset = settings.preset_for(asset_type, path.name)
    if preset is None:
        print(f"no {asset_type} preset accepts {path.name}", file=sys.stderr)
        return 2
    work_dir = settings.behavior.work_dir
    work_dir.mkdir(parents=True, exist_ok=True)
    is_video = asset_type == "VIDEO"

    # The two still guards report here instead of aborting: this command exists to tell you
    # what the pipeline *would* do with a file, and "it would refuse this one" is the most
    # useful answer it can give. It is also the cheapest way to check the metadata gate
    # against real camera material before the gate starts failing jobs for real.
    embedded = None if is_video else await embedded_media_reason(path)
    source_quality = None if is_video else await jpeg_quality(path)

    source_probe = await probe(path, is_still=not is_video)
    result = await encode(
        path,
        preset,
        work_dir,
        transcode_unsupported_audio=preset.effective_transcode_unsupported_audio(settings.behavior),
    )
    metadata_differences = await verify_metadata(path, result.output_path) if preset.exiftool_copy else []
    sanity = await check_sanity(
        source=path,
        result=result,
        source_probe=source_probe,
        behavior=settings.behavior,
        preset=preset,
        is_video=asset_type == "VIDEO",
    )
    print(
        json.dumps(
            {
                "preset": preset.name,
                "input": str(path),
                "output": str(result.output_path),
                "orig_bytes": result.orig_bytes,
                "new_bytes": result.new_bytes,
                "ratio": round(result.ratio, 4),
                "source_probe": _probe_summary(source_probe),
                "output_probe": _probe_summary(result.probe),
                "source_quality": source_quality,
                "embedded_media": embedded,
                "metadata_differences": metadata_differences,
                "sanity_ok": sanity.ok,
                "sanity_failures": sanity.failures,
            },
            indent=2,
        )
    )
    return 0 if sanity.ok and not metadata_differences and embedded is None else 1


def _probe_summary(probe_result: MediaProbe) -> dict[str, Any]:
    """The fields you actually need when tuning a preset by hand."""
    width, height = probe_result.display_size
    return {
        "display_size": f"{width}x{height}",
        "stored_size": f"{probe_result.width}x{probe_result.height}",
        "rotation": probe_result.rotation,
        "pix_fmt": probe_result.pix_fmt,
        "bit_depth": probe_result.bit_depth,
        "color_transfer": probe_result.color_transfer,
        "audio_streams": probe_result.audio_streams,
        "duration_s": probe_result.duration_s,
        "has_date_time_original": probe_result.has_date_time_original,
    }


def cmd_encode(args: argparse.Namespace) -> int:
    """Run a preset against a local file — the offline way to tune a preset."""
    settings = _load(args)
    _configure_logging(settings.log_level)
    path = Path(args.path)
    if not path.is_file():
        print(f"no such file: {path}", file=sys.stderr)
        return 2
    try:
        return asyncio.run(_encode_local(settings, path, args.type))
    except EncodeError as exc:
        print(f"encode failed: {exc}", file=sys.stderr)
        return 1


async def _report(settings: Settings, as_json: bool) -> int:
    async with JobStore(settings.database_path) as store:
        stats = await store.stats()
        jobs = await store.list_jobs(limit=1000)
        latched = await store.pause_state()
        counters = await store.counters()
        inventory = await store.inventory_stats()
    if as_json:
        stats["paused"] = {"since": latched.since.isoformat(), "reason": latched.reason} if latched else None
        stats["webhooks"] = {
            "received": counters[WEBHOOKS_RECEIVED],
            "rejected": counters[WEBHOOKS_REJECTED],
        }
        stats["backfill"] = inventory
        print(json.dumps(stats, indent=2))
        return 0
    print("=== immich-compressor report ===")
    print(f"database: {settings.database_path}")
    # First line after the header when it applies: every number below is frozen until
    # somebody resumes, and reading them without knowing that is misleading.
    if latched is not None:
        print(f"PAUSED since {latched.since.isoformat()}: {latched.reason}")
        print("  nothing is queued, processed or deleted — `immich-compressor resume --apply`")
    # Above the job counts, because "0 received, 7 rejected" is the explanation for every
    # zero underneath it.
    for line in _webhook_summary(counters):
        print(line)
    print(f"jobs total: {stats['total']}")
    for state, count in sorted(stats["by_state"].items()):
        print(f"  {state:16s} {count}")
    if stats["by_skip_reason"]:
        print("skip reasons:")
        for reason, count in sorted(stats["by_skip_reason"].items()):
            print(f"  {reason:20s} {count}")
    saved_mb = stats["saved_bytes"] / (1024 * 1024)
    print(f"compressed assets: {stats['compressed_assets']}")
    # `average_ratio` is None until something has been compressed, and Python's None is not
    # a word to show a user — least of all in the first command the quickstart runs.
    ratio = stats["average_ratio"]
    print(f"saved: {saved_mb:.1f} MiB (average ratio {ratio if ratio is not None else '—'})")
    # Only once a scan has run: on an installation that has never backfilled, a line of
    # zeroes would be one more number to explain rather than one fewer.
    if inventory["scanned"]:
        print(
            f"backfill: {inventory['candidates']} candidate(s) waiting "
            f"({_human_bytes(inventory['candidate_bytes'])}), {inventory['queued']} queued so far"
            " — `immich-compressor backfill status`"
        )
    failed = [job for job in jobs if job.state == JobState.FAILED]
    if failed:
        print(f"failed jobs ({len(failed)}):")
        for job in failed[:20]:
            print(f"  {job.source_asset_id}  {job.last_error}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    settings = _load(args)
    _configure_logging("WARNING")
    return asyncio.run(_report(settings, args.json))


async def _jobs(settings: Settings, status: str | None, limit: int, as_json: bool) -> int:
    """What `GET /jobs` answers, without needing a published port or an HTTP client.

    The documented route to `last_error` was `curl 'localhost:8080/jobs?status=failed'`,
    which runs nowhere in a default install: no port is published, and the image ships
    neither curl nor wget. `docker compose exec` is how every other command is reached, so
    this one goes there too.
    """
    state: JobState | None = None
    if status is not None:
        # The parser already restricts `--status` to the known states; this is the same
        # check for anything that calls the function directly, which must not see a
        # ValueError come out of a reporting command.
        try:
            state = JobState(status)
        except ValueError:
            known = ", ".join(member.value for member in JobState)
            print(f"unknown status {status!r} — one of: {known}", file=sys.stderr)
            return 2
    async with JobStore(settings.database_path) as store:
        # Clamped like `GET /jobs` does it. SQLite reads a negative LIMIT as "no limit",
        # so `--limit -1` would quietly dump the whole table.
        found = await store.list_jobs(state=state, limit=min(max(limit, 1), 1000))

    if as_json:
        print(json.dumps([job.model_dump(mode="json", exclude={"payload"}) for job in found], indent=2))
        return 0
    if not found:
        print(f"no jobs in state {status}" if status else "no jobs")
        return 0
    for job in found:
        print(f"{job.source_asset_id}  {job.state.value:<15s} {job.updated_at.isoformat(timespec='seconds')}")
        if job.skip_reason is not None:
            print(f"    skipped: {job.skip_reason.value}")
        if job.last_error:
            print(f"    error: {job.last_error}")
    print(f"\n{len(found)} job(s)" + (f" in state {status}" if status else ""))
    return 0


def cmd_jobs(args: argparse.Namespace) -> int:
    settings = _load(args)
    _configure_logging("WARNING")
    return asyncio.run(_jobs(settings, args.status, args.limit, args.json))


async def _reprocess(settings: Settings, asset_id: str) -> int:
    async with JobStore(settings.database_path) as store:
        if await store.reset(asset_id):
            print(f"{asset_id} re-queued")
            return 0
    # The name reads like "process this asset"; it means "re-queue a job I already have".
    # Somebody reaching for it on an asset the webhook never delivered needs the other
    # command, and this is the moment they need to hear about it.
    print(f"{asset_id} is not in the store", file=sys.stderr)
    print(
        "  no webhook ever arrived for it. `backfill` is the way in for assets that are "
        "already in the library",
        file=sys.stderr,
    )
    return 1


def cmd_reprocess(args: argparse.Namespace) -> int:
    settings = _load(args)
    _configure_logging("WARNING")
    return asyncio.run(_reprocess(settings, args.asset_id))


def _print_requeue_plan(asset_ids: list[str], description: str) -> None:
    """What a dry run of either requeue mode prints."""
    for asset_id in asset_ids[:20]:
        print(f"[dry] would re-queue {asset_id}")
    if len(asset_ids) > 20:
        print(f"[dry] ... and {len(asset_ids) - 20} more")
    print(f"{len(asset_ids)} job(s) {description} — pass --apply to re-queue")


async def _requeue(settings: Settings, reason: SkipReason, apply: bool) -> int:
    """Re-run assets that a *previous* version of a guard or the sanity gate rejected."""
    async with JobStore(settings.database_path) as store:
        asset_ids = await store.skipped_asset_ids(reason)
        if not asset_ids:
            print(f"no jobs skipped as {reason.value}")
            return 0
        if not apply:
            _print_requeue_plan(asset_ids, f"skipped as {reason.value}")
            return 0
        await store.requeue_skipped(reason)
    print(f"re-queued {len(asset_ids)} job(s) previously skipped as {reason.value}")
    return 0


async def _requeue_failed(settings: Settings, error_contains: str | None, apply: bool) -> int:
    """Re-run jobs a *previous* version of a gate, the encoder or the server failed.

    The counterpart of :func:`_requeue` for the other terminal state. A failed job has spent
    its attempts, so nothing brings it back on its own, and a gate fix without this is
    recovered one `reprocess` call at a time.
    """
    described = "failed" if error_contains is None else f"failed with {error_contains!r} in the error"
    async with JobStore(settings.database_path) as store:
        asset_ids = await store.failed_asset_ids(error_contains=error_contains)
        if not asset_ids:
            print(f"no jobs {described}")
            return 0
        if not apply:
            _print_requeue_plan(asset_ids, described)
            return 0
        await store.requeue_failed(error_contains=error_contains)
    print(f"re-queued {len(asset_ids)} job(s) that {described}")
    return 0


def cmd_requeue(args: argparse.Namespace) -> int:
    # Before the config is read: a usage error is not a reason to also demand an API key.
    if args.error_contains is not None and not args.failed:
        print("--error-contains selects failed jobs by their error; it needs --failed", file=sys.stderr)
        return 2
    settings = _load(args)
    _configure_logging(settings.log_level)
    if args.failed:
        return asyncio.run(_requeue_failed(settings, args.error_contains, args.apply))
    return asyncio.run(_requeue(settings, SkipReason(args.reason or SkipReason.NO_GAIN.value), args.apply))


async def _resume(settings: Settings, apply: bool) -> int:
    """Report the surge breaker's latch, and clear it when told to."""
    async with JobStore(settings.database_path) as store:
        latched = await store.pause_state()
        if latched is None:
            print("the service is not paused")
            return 0
        print(f"PAUSED since {latched.since.isoformat()}")
        print(f"  reason: {latched.reason}")
        if not apply:
            print("\nNothing is queued, processed or deleted while this stands.")
            print("Check `immich-compressor report` for what is waiting, then pass --apply.")
            return 0
        await store.resume()
    print("\nresumed — workers pick up where they left off on the next poll")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    settings = _load(args)
    _configure_logging("WARNING")
    return asyncio.run(_resume(settings, args.apply))


def _human_bytes(value: int) -> str:
    """A byte count in the unit a person would use. Binary, like every other size here."""
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(size) < 1024.0:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TiB"


def _verdict_line(counts: dict[str, int]) -> str:
    """`too_small 3401 · unsupported_format 402`, biggest group first."""
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return " · ".join(f"{name} {count}" for name, count in ordered)


def _backfill_warnings(settings: Settings, latched: PauseState | None) -> list[str]:
    """What a queue run has to say before it queues anything.

    Both of these turn a successful-looking run into a confusing one an hour later: jobs
    that all end up `skipped: dry_run`, or jobs that never move at all because the surge
    breaker is latched.
    """
    lines: list[str] = []
    if latched is not None:
        lines.append(
            f"the service is PAUSED since {latched.since.isoformat()} — queued jobs will "
            "sit until `immich-compressor resume --apply`"
        )
    if settings.behavior.dry_run:
        lines.append(
            "behavior.dry_run is on: every job queued here ends as `skipped: dry_run`. "
            "After going live, `immich-compressor requeue --reason dry_run --apply` "
            "puts them back in the queue"
        )
    return lines


async def _backfill_scan(settings: Settings, asset_type: str | None, rescan: bool, page_size: int) -> int:
    """Walk the library and write down what could be compressed."""
    types = backfill.resolve_types(settings, asset_type)
    if not types:
        print("no asset types are enabled — see behavior.enabled_types", file=sys.stderr)
        return 1
    async with (
        ImmichClient(
            settings.immich.base_url,
            settings.immich.api_key.get_secret_value(),
            timeout_s=settings.immich.timeout_s,
        ) as client,
        JobStore(settings.database_path) as store,
    ):
        summaries = await backfill.scan(
            client, store, settings, asset_types=types, page_size=page_size, rescan=rescan
        )
    for summary in summaries:
        if summary.resumed_from > 1:
            print(f"{summary.asset_type}: resumed at page {summary.resumed_from}")
        print(
            f"{summary.asset_type}: {summary.seen} asset(s) in {summary.pages} page(s) — "
            f"{summary.candidates} candidate(s) ({_human_bytes(summary.candidate_bytes)}), "
            f"{summary.recorded - summary.candidates} rejected by the guards"
        )
        if summary.by_verdict:
            print(f"  rejected: {_verdict_line(summary.by_verdict)}")
        if summary.foreign:
            print(
                f"  ignored {summary.foreign} result(s) that were not {summary.asset_type}: "
                "this Immich answers /search/metadata without applying the type filter"
            )
        if summary.stopped_because:
            print(f"  incomplete: {summary.stopped_because}")
        elif not summary.completed:  # pragma: no cover - every exit sets one or the other
            print("  incomplete")
    print("\nnext: `immich-compressor backfill run --limit 50` (dry) to see what it would queue")
    return 0


async def _backfill_run(
    settings: Settings,
    asset_type: str | None,
    limit: int,
    order: str,
    apply: bool,
    verify: bool,
    page_size: int,
) -> int:
    """Queue candidates out of the inventory, scanning first when there is none."""
    types = backfill.resolve_types(settings, asset_type)
    if not types:
        print("no asset types are enabled — see behavior.enabled_types", file=sys.stderr)
        return 1
    async with (
        ImmichClient(
            settings.immich.base_url,
            settings.immich.api_key.get_secret_value(),
            timeout_s=settings.immich.timeout_s,
        ) as client,
        JobStore(settings.database_path) as store,
    ):
        stats = await store.inventory_stats()
        unscanned = [name for name in types if name not in stats["types"]]
        if unscanned:
            # The two-step is honest about what it costs, but nobody should have to read
            # the manual to get the first job queued.
            print(f"no inventory for {', '.join(unscanned)} yet — scanning first")
            for summary in await backfill.scan(client, store, settings, asset_types=unscanned):
                print(
                    f"{summary.asset_type}: {summary.seen} asset(s) scanned, "
                    f"{summary.candidates} candidate(s) ({_human_bytes(summary.candidate_bytes)})"
                )
        for line in _backfill_warnings(settings, await store.pause_state()):
            print(f"note: {line}")
        summary = await backfill.queue_candidates(
            client,
            store,
            settings,
            asset_types=types,
            limit=limit,
            order=order,
            apply=apply,
            verify=verify,
        )
        remaining = await store.inventory_stats()
    left = sum(entry["candidates"] for name, entry in remaining["types"].items() if name in types)
    for queued in summary.queued[:20]:
        prefix = "queued" if apply else "[dry] would queue"
        print(f"{prefix} {queued.asset_id}  {queued.filename}  {_human_bytes(queued.size_bytes)}")
    if len(summary.queued) > 20:
        print(f"  ... and {len(summary.queued) - 20} more")
    if not summary.queued:
        print("nothing to queue: no candidate is waiting for these types")
        print("  `immich-compressor backfill status` says what the scan found, and why")
        return 0
    if apply:
        print(
            f"\nqueued {len(summary.queued)} job(s), {_human_bytes(summary.queued_bytes)} of "
            f"originals — {left} candidate(s) left"
        )
    else:
        print(f"\n{len(summary.queued)} job(s) would be queued out of {left} candidate(s) — pass --apply")
    if summary.downgraded:
        print(f"  dropped by the live re-check: {_verdict_line(summary.downgraded)}")
    if summary.exhausted and apply:
        print("  the inventory is empty for these types — `backfill scan` again for what is new")
    return 0


async def _backfill_status(settings: Settings, as_json: bool) -> int:
    """What the scan found, per type, and how much of it is still waiting."""
    async with JobStore(settings.database_path) as store:
        stats = await store.inventory_stats()
        cursors = {
            asset_type: await store.get_state(backfill.scan_state_key(asset_type))
            for asset_type in sorted(set(stats["types"]) | set(settings.behavior.enabled_types))
        }
    if as_json:
        print(json.dumps({"inventory": stats, "scans": cursors}, indent=2))
        return 0
    print("=== backfill inventory ===")
    print(f"database: {settings.database_path}")
    if not stats["types"]:
        print("nothing scanned yet — `immich-compressor backfill scan`")
        return 0
    for asset_type, entry in sorted(stats["types"].items()):
        print(
            f"{asset_type}: {entry['scanned']} scanned ({_human_bytes(entry['scanned_bytes'])}) · "
            f"{entry['candidates']} candidate(s) ({_human_bytes(entry['candidate_bytes'])}) · "
            f"{entry['queued']} queued"
        )
        cursor = cursors.get(asset_type) or {}
        if cursor.get("completed_at"):
            print(f"  last complete walk: {cursor['completed_at']}")
        elif cursor.get("next_page"):
            print(f"  walk interrupted, resumes at page {cursor['next_page']}")
        if entry["by_verdict"]:
            print(f"  rejected: {_verdict_line(entry['by_verdict'])}")
    for asset_type in settings.behavior.enabled_types:
        if asset_type not in stats["types"]:
            print(f"{asset_type}: not scanned yet")
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    settings = _load(args)
    _configure_logging(settings.log_level if args.mode != "status" else "WARNING")
    if args.mode == "scan":
        return asyncio.run(_backfill_scan(settings, args.type, args.rescan, args.page_size))
    if args.mode == "status":
        return asyncio.run(_backfill_status(settings, args.json))
    return asyncio.run(
        _backfill_run(
            settings,
            args.type,
            args.limit,
            args.order,
            args.apply,
            not args.no_verify,
            args.page_size,
        )
    )


# What `restore` exits with. A rollback's exit code ends up in somebody's script, so a
# rollback that could not roll everything back must not look like a clean success — and it
# must not look like a failed call either, because the two ask for different reactions.
RESTORE_INCOMPLETE = 3


async def _restore(settings: Settings, asset_ids: list[str]) -> int:
    async with ImmichClient(
        settings.immich.base_url,
        settings.immich.api_key.get_secret_value(),
        timeout_s=settings.immich.timeout_s,
    ) as client:
        try:
            outcome = await client.restore_assets_best_effort(asset_ids)
        except ImmichError as exc:
            # Anything that reaches here is not a dead id — the client isolates those. It
            # is auth, the network, or a server error that outlived the retries.
            print(f"restore failed: {exc}", file=sys.stderr)
            return 1
    # The server's own count, not len(asset_ids): ids it never had are not restorations,
    # and this line is the only number the operator gets.
    print(f"restored {outcome.restored} asset(s) from the trash")
    if not outcome.missing:
        return 0
    print(
        f"{len(outcome.missing)} of {len(asset_ids)} id(s) are no longer in Immich's "
        "database and could not be restored",
        file=sys.stderr,
    )
    # Gated on the server's answer, not on the current `delete_mode`. The mode that
    # removed these originals is not necessarily the mode this deployment runs today —
    # on the deployment where this was measured it was already back to 'trash'.
    print(
        "An id disappears from that database when the original was deleted with "
        "force=true — a run with delete_mode: permanent — or when Immich's trash was "
        "emptied. Neither is undoable here; the only rollback for those originals is a "
        "backup of Postgres and the upload directory.",
        file=sys.stderr,
    )
    return RESTORE_INCOMPLETE


def cmd_restore(args: argparse.Namespace) -> int:
    """Rollback helper: pull originals back out of the trash."""
    settings = _load(args)
    _configure_logging("WARNING")
    # Deliberately no warning about `delete_mode: permanent` here. One used to fire on the
    # mode the deployment is in *now*, before a single id had been tried, and it claimed
    # that originals "cannot be restored" — which a run that then restored every one of
    # them printed alongside its own success. `_restore` says the true version afterwards,
    # naming the ids the server actually refused and why.
    ids: list[str] = list(args.asset_id)
    if args.all_pending:

        async def collect() -> list[str]:
            async with JobStore(settings.database_path) as store:
                return await store.replaced_source_asset_ids()

        ids.extend(asyncio.run(collect()))
    if not ids:
        print("nothing to restore", file=sys.stderr)
        return 2
    return asyncio.run(_restore(settings, ids))


# ------------------------------------------------------------------------------- parser


# What a user reads at the top of `--help`. Kept short: the subcommand list underneath is
# generated and complete, so this only has to say what the thing is and where to start.
_DESCRIPTION = (
    "Out-of-band recompression for Immich, driven by a workflow webhook. "
    "Start with `setup`, then `check` and `hardware`; `report` and `jobs` tell you what "
    "happened, and `restore` and `resume` are what you reach for when something went wrong."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="immich-compressor", description=_DESCRIPTION)
    parser.add_argument("--version", action="version", version=f"immich-compressor {__version__}")
    parser.add_argument("-c", "--config", help="path to config.yaml (default: $COMPRESSOR_CONFIG)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="run the webhook service").set_defaults(func=cmd_serve)
    sub.add_parser("check", help="validate config, reach the Immich API, confirm the encoder").set_defaults(
        func=cmd_check
    )

    setup_parser = sub.add_parser(
        "setup", help="guided first-run setup: keys, permissions, hardware, config, workflow"
    )
    setup_parser.add_argument("--url", default=DEFAULT_BASE_URL, help="Immich API base URL")
    setup_parser.add_argument("--api-key", help="Immich API key (or set IMMICH_API_KEY)")
    setup_parser.add_argument(
        "--session-token", help="browser session token, used only to create the workflow"
    )
    setup_parser.add_argument(
        "--workflow-key",
        help="throwaway API key with only workflow.create; used once, never stored",
    )
    setup_parser.add_argument(
        "--network", default=DEFAULT_NETWORK, help="docker network your Immich stack uses"
    )
    setup_parser.add_argument("--webhook-url", default=DEFAULT_WEBHOOK_URL, help="URL Immich should call")
    setup_parser.add_argument("--directory", default=".", help="where to write config.yaml and .env")
    setup_parser.add_argument(
        "--non-interactive", action="store_true", help="never prompt; use flags and defaults"
    )
    setup_parser.add_argument(
        "--force", action="store_true", help="overwrite config.yaml and replace stored secrets"
    )
    setup_parser.add_argument("--no-workflow", action="store_true", help="do not create the Immich workflow")
    setup_parser.set_defaults(func=cmd_setup)

    hardware_parser = sub.add_parser(
        "hardware", help="show which encoder this machine gets, and why the others were not"
    )
    hardware_parser.add_argument(
        "--json", action="store_true", help="machine-readable report (attach this to bug reports)"
    )
    hardware_parser.set_defaults(func=cmd_hardware)

    encode_parser = sub.add_parser("encode", help="run a preset on a local file (offline dry run)")
    encode_parser.add_argument("path")
    encode_parser.add_argument("--type", default="VIDEO", choices=["VIDEO", "IMAGE", "AUDIO", "OTHER"])
    encode_parser.set_defaults(func=cmd_encode)

    report_parser = sub.add_parser(
        "report", help="job statistics, and how many webhooks arrived or were refused"
    )
    report_parser.add_argument("--json", action="store_true")
    report_parser.set_defaults(func=cmd_report)

    jobs_parser = sub.add_parser("jobs", help="list jobs, with the error of any that failed")
    jobs_parser.add_argument("--status", choices=[state.value for state in JobState], help="only this state")
    jobs_parser.add_argument("--limit", type=int, default=100)
    jobs_parser.add_argument("--json", action="store_true")
    jobs_parser.set_defaults(func=cmd_jobs)

    reprocess_parser = sub.add_parser("reprocess", help="re-queue one asset")
    reprocess_parser.add_argument("asset_id")
    reprocess_parser.set_defaults(func=cmd_reprocess)

    requeue_parser = sub.add_parser(
        "requeue", help="re-queue jobs a previous version of a guard, a gate or the encoder rejected"
    )
    # Skipped and failed are two terminal states and a run selects one of them. `--reason`
    # defaults to None rather than to no_gain so that argparse can see it was passed at all:
    # a value equal to the default is not a conflict to it, and `--failed --reason no_gain`
    # would slip through the group. `cmd_requeue` resolves the None, so `requeue` on its own
    # still means what it always did.
    requeue_target = requeue_parser.add_mutually_exclusive_group()
    requeue_target.add_argument(
        "--reason",
        default=None,
        choices=[reason.value for reason in SkipReason],
        help=f"re-queue jobs skipped for this reason (default: {SkipReason.NO_GAIN.value})",
    )
    requeue_target.add_argument(
        "--failed", action="store_true", help="re-queue failed jobs instead of skipped ones"
    )
    requeue_parser.add_argument(
        "--error-contains",
        metavar="TEXT",
        help="with --failed: only jobs whose recorded error contains TEXT",
    )
    requeue_parser.add_argument("--apply", action="store_true", help="actually re-queue (default: dry)")
    requeue_parser.set_defaults(func=cmd_requeue)

    resume_parser = sub.add_parser("resume", help="show or clear the surge breaker's pause")
    resume_parser.add_argument("--apply", action="store_true", help="actually resume (default: report)")
    resume_parser.set_defaults(func=cmd_resume)

    backfill_parser = sub.add_parser(
        "backfill", help="work through the assets that were in the library before this service"
    )
    # A positional mode rather than nested subparsers: a subparser parses into its own
    # namespace and copies *its* defaults over the parent's, which would silently drop the
    # `--limit` in `backfill --limit 10 run`. One parser has no such corner.
    backfill_parser.add_argument(
        "mode",
        nargs="?",
        default="run",
        choices=["run", "scan", "status"],
        help="scan: inventory the library. run (default): queue from that inventory. status: what is left",
    )
    backfill_parser.add_argument(
        "--type", choices=["VIDEO", "IMAGE"], help="one lane only (default: every enabled type)"
    )
    backfill_parser.add_argument("--limit", type=int, default=50, help="run: how many jobs to queue")
    backfill_parser.add_argument(
        "--order",
        default="size",
        choices=["size", "scanned"],
        help="run: biggest first (default), or the order the library came back in",
    )
    backfill_parser.add_argument("--apply", action="store_true", help="run: actually queue (default: dry)")
    backfill_parser.add_argument(
        "--no-verify",
        action="store_true",
        help="run: skip the live re-check of each asset before it is queued",
    )
    backfill_parser.add_argument(
        "--rescan", action="store_true", help="scan: drop the inventory and walk the library again"
    )
    backfill_parser.add_argument(
        "--page-size", type=int, default=backfill.DEFAULT_PAGE_SIZE, help="scan: assets per request"
    )
    backfill_parser.add_argument("--json", action="store_true", help="status: machine-readable")
    backfill_parser.set_defaults(func=cmd_backfill)

    restore_parser = sub.add_parser(
        "restore",
        help="pull originals back out of the trash",
        # A rollback's exit code ends up in a script, so it is part of the interface.
        epilog=(
            "exit codes: 0 every id came back, 3 some ids are no longer in Immich's "
            "database, 2 nothing selected, 1 the call to Immich failed"
        ),
    )
    restore_parser.add_argument("asset_id", nargs="*")
    restore_parser.add_argument(
        "--all-pending", action="store_true", help="restore every original this service trashed"
    )
    restore_parser.set_defaults(func=cmd_restore)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result: int = args.func(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return result


if __name__ == "__main__":
    sys.exit(main())
