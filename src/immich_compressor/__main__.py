"""CLI entry point: ``serve``, ``check``, ``encode``, ``report``, ``reprocess``, ``backfill``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .api import ImmichClient
from .config import ConfigError, Settings, load_settings
from .encoder import EncodeError, check_sanity, encode, probe
from .models import JobState
from .store import JobStore

logger = logging.getLogger("immich_compressor")


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def _load(args: argparse.Namespace) -> Settings:
    return load_settings(Path(args.config) if args.config else None)


# ---------------------------------------------------------------------------- commands


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .server import create_app

    settings = _load(args)
    _configure_logging(settings.log_level)
    logger.info(
        "starting immich-compressor on %s:%d (dry_run=%s, trash_original=%s)",
        settings.listen_host,
        settings.listen_port,
        settings.behavior.dry_run,
        settings.behavior.trash_original,
    )
    uvicorn.run(
        create_app(settings),
        host=settings.listen_host,
        port=settings.listen_port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )
    return 0


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
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    settings = _load(args)
    _configure_logging(settings.log_level)
    return asyncio.run(_check(settings))


async def _encode_local(settings: Settings, path: Path, asset_type: str) -> int:
    preset = settings.preset_for(asset_type)
    if preset is None:
        print(f"no preset for type {asset_type}", file=sys.stderr)
        return 2
    work_dir = settings.behavior.work_dir
    work_dir.mkdir(parents=True, exist_ok=True)
    source_probe = await probe(path)
    result = await encode(path, preset, work_dir)
    sanity = await check_sanity(
        source=path,
        result=result,
        source_probe=source_probe,
        behavior=settings.behavior,
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
                "sanity_ok": sanity.ok,
                "sanity_failures": sanity.failures,
            },
            indent=2,
        )
    )
    return 0 if sanity.ok else 1


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
    if as_json:
        print(json.dumps(stats, indent=2))
        return 0
    print("=== immich-compressor report ===")
    print(f"database: {settings.database_path}")
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


async def _backfill(settings: Settings, asset_type: str, limit: int, apply: bool) -> int:
    """Queue existing large assets, as if a webhook had arrived for each."""
    queued = 0
    seen = 0
    async with (
        ImmichClient(
            settings.immich.base_url,
            settings.immich.api_key.get_secret_value(),
            timeout_s=settings.immich.timeout_s,
        ) as client,
        JobStore(settings.database_path) as store,
    ):
        async for item in client.search_large_assets(
            min_file_size=settings.behavior.min_size_bytes,
            asset_type=asset_type,
            size=min(limit, 200),
        ):
            seen += 1
            if seen > limit:
                break
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
        await client.restore_assets(asset_ids)
    print(f"restored {len(asset_ids)} asset(s) from the trash")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    """Rollback helper: pull originals back out of the trash."""
    settings = _load(args)
    _configure_logging("WARNING")
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
    parser.add_argument("-c", "--config", help="path to config.yaml (default: $COMPRESSOR_CONFIG)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="run the webhook service").set_defaults(func=cmd_serve)
    sub.add_parser("check", help="validate config and reach the Immich API").set_defaults(
        func=cmd_check
    )

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
