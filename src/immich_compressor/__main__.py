"""CLI: ``serve``, ``check``, ``encode``, ``report``, ``reprocess``, ``requeue``, ``backfill``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from . import __version__
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
from .models import JobState, SkipReason
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
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


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
    result = await encode(path, preset, work_dir)
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
    if as_json:
        stats["paused"] = {"since": latched.since.isoformat(), "reason": latched.reason} if latched else None
        stats["webhooks"] = {
            "received": counters[WEBHOOKS_RECEIVED],
            "rejected": counters[WEBHOOKS_REJECTED],
        }
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
    print(f"saved: {saved_mb:.1f} MiB (average ratio {stats['average_ratio']})")
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


async def _reprocess(settings: Settings, asset_id: str) -> int:
    async with JobStore(settings.database_path) as store:
        if await store.reset(asset_id):
            print(f"{asset_id} re-queued")
            return 0
    print(f"{asset_id} is not in the store", file=sys.stderr)
    return 1


def cmd_reprocess(args: argparse.Namespace) -> int:
    settings = _load(args)
    _configure_logging("WARNING")
    return asyncio.run(_reprocess(settings, args.asset_id))


async def _requeue(settings: Settings, reason: SkipReason, apply: bool) -> int:
    """Re-run assets that a *previous* version of a guard or the sanity gate rejected."""
    async with JobStore(settings.database_path) as store:
        asset_ids = await store.skipped_asset_ids(reason)
        if not asset_ids:
            print(f"no jobs skipped as {reason.value}")
            return 0
        if not apply:
            for asset_id in asset_ids[:20]:
                print(f"[dry] would re-queue {asset_id}")
            if len(asset_ids) > 20:
                print(f"[dry] ... and {len(asset_ids) - 20} more")
            print(f"{len(asset_ids)} job(s) skipped as {reason.value} — pass --apply to re-queue")
            return 0
        await store.requeue_skipped(reason)
    print(f"re-queued {len(asset_ids)} job(s) previously skipped as {reason.value}")
    return 0


def cmd_requeue(args: argparse.Namespace) -> int:
    settings = _load(args)
    _configure_logging(settings.log_level)
    return asyncio.run(_requeue(settings, SkipReason(args.reason), args.apply))


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


async def _backfill(settings: Settings, asset_type: str, limit: int, apply: bool) -> int:
    """Queue existing large assets, as if a webhook had arrived for each.

    The type filter is applied here rather than trusted to the server. Measured against a
    live v3.1.0: ``POST /search/large-assets`` ignores the ``type`` field in the request
    body — ``IMAGE`` and ``VIDEO`` answer with the identical set of videos — and ignores
    ``size`` as well. Without a client-side check the stills backfill is not merely broken
    but unreachable, and anybody who thinks they are testing 50 photos re-encodes 50
    videos instead. Harmless in stage 1; not harmless from stage 3 on.
    """
    queued = 0
    seen = 0
    foreign = 0
    async with (
        ImmichClient(
            settings.immich.base_url,
            settings.immich.api_key.get_secret_value(),
            timeout_s=settings.immich.timeout_s,
        ) as client,
        JobStore(settings.database_path) as store,
    ):
        async for item in client.search_large_assets(
            min_file_size=settings.behavior.min_savings_bytes,
            asset_type=asset_type,
            size=min(limit, 200),
        ):
            if item.get("type") != asset_type:
                foreign += 1
                continue
            if seen >= limit:
                break
            seen += 1
            asset_id = item.get("id")
            if not asset_id:
                continue
            if not apply:
                print(f"[dry] would queue {asset_id} ({item.get('originalFileName')})")
                continue
            payload = {
                "type": "AssetV1",
                "trigger": "Backfill",
                "data": {"asset": item},
            }
            if await store.enqueue(asset_id, payload, delay_seconds=0):
                queued += 1
    print(f"scanned {seen} assets, queued {queued}" + ("" if apply else " (dry run — pass --apply)"))
    # Said out loud, because silence here looks like an empty library rather than a filter
    # the server declined to apply.
    if foreign:
        print(
            f"  ignored {foreign} result(s) that were not {asset_type}: this Immich answers "
            "/search/large-assets without applying the type filter"
        )
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    settings = _load(args)
    _configure_logging(settings.log_level)
    return asyncio.run(_backfill(settings, args.type, args.limit, args.apply))


async def _restore(settings: Settings, asset_ids: list[str]) -> int:
    async with ImmichClient(
        settings.immich.base_url,
        settings.immich.api_key.get_secret_value(),
        timeout_s=settings.immich.timeout_s,
    ) as client:
        try:
            await client.restore_assets(asset_ids)
        except ImmichError as exc:
            # Verified on a live v3.1.0: restoring an asset that was force-deleted answers
            # HTTP 400 "Not found", and the whole batch fails with it. Say why instead of
            # letting the traceback out.
            print(f"restore failed: {exc}", file=sys.stderr)
            if settings.behavior.delete_mode == "permanent":
                print(
                    "delete_mode is 'permanent' — these originals were deleted with "
                    "force=true and never entered the trash. Nothing can restore them; "
                    "the only rollback is a backup of Postgres and the upload directory.",
                    file=sys.stderr,
                )
            return 1
    print(f"restored {len(asset_ids)} asset(s) from the trash")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    """Rollback helper: pull originals back out of the trash."""
    settings = _load(args)
    _configure_logging("WARNING")
    if settings.behavior.delete_mode == "permanent":
        print(
            "warning: delete_mode is 'permanent' — originals removed by this service were "
            "not trashed and cannot be restored.",
            file=sys.stderr,
        )
    ids: list[str] = list(args.asset_id)
    if args.all_pending:

        async def collect() -> list[str]:
            async with JobStore(settings.database_path) as store:
                jobs = await store.list_jobs(state=JobState.DONE, limit=10_000)
                return [job.source_asset_id for job in jobs if job.new_asset_id]

        ids.extend(asyncio.run(collect()))
    if not ids:
        print("nothing to restore", file=sys.stderr)
        return 2
    return asyncio.run(_restore(settings, ids))


# ------------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="immich-compressor", description=__doc__)
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

    report_parser = sub.add_parser("report", help="print job statistics")
    report_parser.add_argument("--json", action="store_true")
    report_parser.set_defaults(func=cmd_report)

    reprocess_parser = sub.add_parser("reprocess", help="re-queue one asset")
    reprocess_parser.add_argument("asset_id")
    reprocess_parser.set_defaults(func=cmd_reprocess)

    requeue_parser = sub.add_parser("requeue", help="re-queue every job that was skipped for one reason")
    requeue_parser.add_argument(
        "--reason",
        default=SkipReason.NO_GAIN.value,
        choices=[reason.value for reason in SkipReason],
    )
    requeue_parser.add_argument("--apply", action="store_true", help="actually re-queue (default: dry)")
    requeue_parser.set_defaults(func=cmd_requeue)

    resume_parser = sub.add_parser("resume", help="show or clear the surge breaker's pause")
    resume_parser.add_argument("--apply", action="store_true", help="actually resume (default: report)")
    resume_parser.set_defaults(func=cmd_resume)

    backfill_parser = sub.add_parser("backfill", help="queue existing large assets")
    backfill_parser.add_argument("--type", default="VIDEO", choices=["VIDEO", "IMAGE"])
    backfill_parser.add_argument("--limit", type=int, default=50)
    backfill_parser.add_argument("--apply", action="store_true", help="actually queue (default: dry)")
    backfill_parser.set_defaults(func=cmd_backfill)

    restore_parser = sub.add_parser("restore", help="pull originals back out of the trash")
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
