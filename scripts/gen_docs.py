#!/usr/bin/env python3
"""Generate the configuration reference and JSON schema from the settings model.

Two files, one source of truth:

* ``docs/configuration.md`` — every option, its type, its default and the comment that
  explains it, taken from the pydantic models in ``config.py``.
* ``docs/config.schema.json`` — the same model as a JSON schema, so an editor with
  ``yaml-language-server`` autocompletes and validates ``config.yaml`` in place.

Hand-maintaining either of these next to the code guarantees they drift. ``--check``
re-generates into memory and exits non-zero when the files on disk disagree, which is what
CI runs.

    python scripts/gen_docs.py            # write
    python scripts/gen_docs.py --check    # verify, write nothing
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from immich_compressor import __version__  # noqa: E402
from immich_compressor.config import (  # noqa: E402
    BehaviorSettings,
    HardwareSettings,
    ImmichSettings,
    Preset,
    Settings,
    ShimSettings,
    WebhookSettings,
)

CONFIG_MD = REPO / "docs" / "configuration.md"
SCHEMA_JSON = REPO / "docs" / "config.schema.json"

SECTIONS: tuple[tuple[str, type, str], ...] = (
    (
        "immich",
        ImmichSettings,
        "How to reach the Immich server. `api_key` is **not** read from this file — see [Secrets](#secrets).",
    ),
    (
        "webhook",
        WebhookSettings,
        "The inbound webhook endpoint. `token` is **not** read from this file — see [Secrets](#secrets).",
    ),
    (
        "hardware",
        HardwareSettings,
        "Which encoder to use. The default is to work it out — see [hardware.md](hardware.md).",
    ),
    (
        "shim",
        ShimSettings,
        "The checksum-translation shim, which stops a phone re-uploading an original after "
        "its compressed replacement took over. Off by default and inert until a reverse "
        "proxy routes two paths here — see [shim.md](shim.md).",
    ),
    (
        "behavior",
        BehaviorSettings,
        "Everything that decides whether and how an asset gets touched. The three that "
        "matter most are `dry_run`, `trash_original` and `delete_mode`; "
        "[safety.md](safety.md) explains them in order.",
    ),
)

# Options a reader will look for by name, with the sentence that is actually useful. The
# rest are described from their type and default, which is enough for a knob like
# `timeout_s`.
NOTES: dict[str, str] = {
    "shim.enabled": (
        "Master switch. Off ships inert: the two proxied routes are not mounted at all, and "
        "nothing about what a client sees changes."
    ),
    "shim.upstream_url": (
        "The Immich **origin**, without the `/api` suffix — this is not `immich.base_url`. "
        "The shim forwards the client's whole path, which already begins with `/api`, so a "
        "value ending in `/api` is rejected at startup rather than producing 404s from a "
        "server that is plainly up."
    ),
    "shim.rewrite_sync_stream": (
        "Translate `POST /api/sync/stream`. This is the direction that reaches the mobile "
        "app, and the one that actually stops a re-upload."
    ),
    "shim.rewrite_upload_check": (
        "Translate `POST /api/assets/bulk-upload-check`. The mobile app does not use this "
        "route; the CLI, `immich-go` and the web uploader do."
    ),
    "shim.watch_deletes": (
        "Watch the sync stream for the purge of an original this service replaced, and open "
        "that row's gate when it goes past. With `delete_mode: trash` this is the only way "
        "the service ever learns the retention window expired — the deletion happens inside "
        "Immich, up to a month later, and nothing reports it."
    ),
    "shim.log_only": (
        "Count what would change and change nothing. The first rollout step: it proves the "
        "ledger matches real traffic before a single byte is altered."
    ),
    "shim.ledger_refresh_seconds": "How often the translation maps are rebuilt from the job store.",
    "shim.connect_timeout_s": (
        "Connection timeout for the proxied requests. There is deliberately no read timeout "
        "on the sync stream, which is long-lived by design."
    ),
    "immich.api_key": (
        "**Environment only**, as `IMMICH__API_KEY`. Setting it here makes the service refuse to start."
    ),
    "immich.timeout_s": (
        "Request timeout. Uploads of multi-gigabyte videos go through this, so do not lower it much."
    ),
    "immich.connect_timeout_s": "Connection timeout, separate from the request timeout.",
    "webhook.token": (
        "**Environment only**, as `WEBHOOK__TOKEN`. Setting it here makes the service refuse to start."
    ),
    "behavior.max_attempts": (
        "Failures retry with exponential backoff up to this many attempts, then land in `failed`."
    ),
    "behavior.poll_interval_seconds": "How often a worker looks for a due job.",
    "behavior.work_dir": (
        "Scratch space for the download and the encode. Needs room for both copies of the "
        "largest asset you process — mount a volume, not the container's writable layer."
    ),
    "immich.base_url": (
        "Reachable from inside the container. With both stacks on the same docker network "
        "this is the Immich service name, not `localhost`."
    ),
    "webhook.header_name": "Must equal `headerName` in the workflow's webhook step.",
    "hardware.mode": (
        "`auto` detects and confirms the best encoder this machine can actually run. "
        "`cpu` never considers a GPU. `qsv`, `vaapi` and `nvenc` pin one hardware encoder "
        "— and still fall back to the CPU preset if it fails its one-frame test encode, "
        "because a pinned GPU is a preference, not a promise the machine can keep."
    ),
    "hardware.render_node": (
        "`auto`, or a specific node such as `/dev/dri/renderD129` on a box with more than "
        "one GPU. `immich-compressor hardware` lists what is present."
    ),
    "behavior.dry_run": (
        "**The shipping default.** Nothing is downloaded, encoded, uploaded or deleted; "
        "every asset is recorded as `skipped: dry_run` and shows up in `report`."
    ),
    "behavior.trash_original": (
        "When false the original is never removed, so both versions exist side by side. "
        "Turning it on needs the `asset.delete` permission on the API key."
    ),
    "behavior.delete_mode": (
        "`trash` is a recoverable soft delete — `immich-compressor restore` undoes it "
        "until the Immich trash is emptied. `permanent` calls `DELETE /assets` with "
        "`force=true`, which bypasses the trash entirely: the row is gone and the files "
        "are unlinked. There is no undo other than a backup. Rejected at startup unless "
        "`trash_original: true` and `dry_run: false`."
    ),
    "behavior.max_asset_age_hours": (
        "**The bulk-trigger gate.** Refuse a webhook for an asset that was added to Immich "
        "longer ago than this. The workflow trigger is `AssetMetadataExtraction`, and one "
        "click on **Administration -> Jobs -> Extract Metadata** re-fires it for *every "
        "asset in the library* — this is what stops that click from becoming a "
        "library-wide recompression. Measured from the payload's `createdAt`, which is the "
        "upload time and not the capture date, so a legitimate import of a thousand old "
        "photos still passes. A refused webhook writes no job, so the asset stays reachable "
        "by `immich-compressor backfill`, which is the intentional way through a library. "
        "`null` turns the gate off, and is rejected at startup together with "
        "`delete_mode: permanent`."
    ),
    "behavior.surge_threshold": (
        "**The surge breaker, off by default.** More than this many *new* assets queued from "
        "webhooks inside `surge_window_seconds` latches the service paused: workers stop "
        "claiming, the trash sweeper stops finalising deletes, and further webhooks are "
        "refused until `immich-compressor resume --apply`. The latch is stored in the "
        "database, so restarting the container does not clear it. Counted for webhook-driven "
        "work only, so `backfill` and `reprocess` never trip it. `null` — the default — "
        "switches it off, because the breaker counts assets and knows nothing else about "
        "them: a first phone backup or a camera card import looks exactly like the influx it "
        "exists to stop, and `IMAGE` in `enabled_types` makes that an ordinary day. "
        "`max_asset_age_hours` is the guard that discriminates and it stays on. Set a number "
        "to turn the breaker on; 2000 is a suggested starting point and not a measured one, "
        "chosen to sit above one device's backlog and below a library migration."
    ),
    "behavior.surge_window_seconds": "The window `surge_threshold` is counted over.",
    "behavior.retention_days": (
        "How long a replaced original survives before it is removed. `0` removes it "
        "inline, the moment the verification chain passes."
    ),
    "behavior.initial_delay_seconds": (
        "Wait this long after the webhook before touching the asset, so Immich's own "
        "thumbnail, machine-learning and OCR jobs finish first."
    ),
    "behavior.concurrency": (
        "How many encodes run at once. Derived from the container's CPU budget when it is "
        "not set here; pinned to 1 whenever a GPU preset is in use, because an iGPU has "
        "one encode block and Immich competes for it."
    ),
    "behavior.quality": (
        "Mapped per encoder to the right CRF / `-global_quality` / `-cq` number. "
        "`balanced` reproduces exactly what this project shipped in 1.0.0. Ignored when "
        "`presets:` is written by hand."
    ),
    "behavior.transcode_unsupported_audio": (
        "Re-encode the audio to AAC when the container refuses to carry it as it is, "
        "instead of failing the job. The shipped video presets copy the audio stream, and "
        "MP4 has no mapping for several codecs an old camera or a DVD rip produces — "
        "measured on a live library, `pcm_u8`, `amr_nb` and `pcm_dvd` were 119 of 172 "
        "failures in one backfill run. Off by default: it is a lossy conversion of a "
        "stream that was lossless in the source, on a job that then goes on to delete "
        "that source."
    ),
    "behavior.min_savings_bytes": (
        "How many bytes a job has to actually save to be worth a new asset — a database "
        "row, thumbnails, an embedding, faces, OCR and a timeline entry, all permanent. "
        "It doubles as the pre-download filter, and that half needs no tuning: a file "
        "cannot save more bytes than it has, so an asset below this is skipped as "
        "`too_small` before it is ever downloaded."
    ),
    "behavior.max_ratio": (
        "Reject the result unless it is at most this fraction of the original. Footage "
        "that is *already* HEVC often fails here rather than shrinking — that is the gate "
        "working, not a defect."
    ),
    "behavior.enabled_types": (
        "`VIDEO` and `IMAGE` have built-in presets. Any other type needs an explicit entry under `presets:`."
    ),
    "behavior.metadata_verify": (
        "What a post-encode metadata difference costs. Stills lose **all** metadata on "
        "re-encode and get it back from an `exiftool` copy, so this gate is what proves "
        "the copy worked. `strict` fails the job and never touches the original; `warn` "
        "only logs, and is refused at startup together with `delete_mode: permanent` "
        "because a warning cannot undo a force-deleted original."
    ),
    "behavior.skip_if_named_people": (
        "Never touch an asset with manually named faces: the replacement is a new asset, "
        "and its faces are re-detected from scratch."
    ),
    "behavior.post_upload_settle_s": (
        "How long to wait for Immich's metadata extraction on the freshly uploaded asset "
        "before writing description, rating and GPS. Extraction overwrites those fields, "
        "so writing too early silently loses them."
    ),
    "behavior.duration_tolerance_s": "Sanity gate: how far the output's duration may drift.",
    "behavior.require_same_resolution": (
        "Sanity gate: compare *display* size, so a rotated clip is not rejected for keeping "
        "or baking in its rotation — only for losing it."
    ),
    "behavior.require_date_time_original": (
        "Sanity gate: refuse an output that *lost* the capture date its source had, which "
        "would land at the wrong place in the timeline. A source that never carried one — a "
        "screen recording, a messenger clip, a drone export — is judged on the other gates."
    ),
    "behavior.free_space_factor": (
        "Refuse to start a job unless this multiple of the source size is free in `work_dir`."
    ),
    "behavior.compressed_marker": (
        "Goes into the replacement's filename **and** into the workflow's regex filter. "
        "Changing it means changing the workflow too."
    ),
    "behavior.metadata_key": (
        "The asset-metadata key used as the idempotency marker. This is the hard loop "
        "guard; the filename marker is only the first line of defence."
    ),
    # Preset fields. `_rows(Preset, "")` looks them up unprefixed.
    "extensions": (
        "File extensions this preset accepts, e.g. `[.jpg, .jpeg]`. Empty means any, "
        "which is what the video presets want. For stills it is an **allowlist and not "
        "optional**: Immich files RAW, PNG, GIF, TIFF, WebP and HEIC under type `IMAGE` "
        "exactly like JPEG, and ImageMagick reads DNG/CR2/CR3/NEF/ARW through libraw — "
        "without the list a raw file would be developed into an 8-bit JPEG, pass every "
        "sanity check, and have its original deleted. Anything not on the list is skipped "
        "as `unsupported_format`."
    ),
    "max_ratio": "Overrides `behavior.max_ratio` for this preset. `null` uses the behavior value.",
    "min_savings_bytes": (
        "Overrides `behavior.min_savings_bytes` for this preset. `null` uses the behavior value."
    ),
    "require_date_time_original": (
        "Overrides `behavior.require_date_time_original` for this preset. Off for stills "
        "in the built-in catalog: a replacement's timeline position comes from the "
        "`fileCreatedAt` sent at upload and the explicit `dateTimeOriginal` write "
        "afterwards, not from the file."
    ),
    "transcode_unsupported_audio": (
        "Overrides `behavior.transcode_unsupported_audio` for this preset. `null` uses the "
        "behavior value. Setting it to `true` needs a command that copies the audio "
        "stream, because that copy is what the retry replaces."
    ),
    "min_source_quality": (
        "Skip a still whose source JPEG quality is below this. Quantisation error is "
        "cumulative, so re-encoding an already-compressed image buys a second generation "
        "of artefacts and usually a *larger* file — measured 158 368 -> 190 488 bytes for "
        "a q60 source through the q82 preset. `null` disables the check."
    ),
    "database_path": "SQLite job store. Back up this volume if you care about the report history.",
    "listen_host": "Inside the container. Publish selectively at the host, never on 0.0.0.0.",
    "listen_port": "Inside the container.",
    "log_level": "`DEBUG`, `INFO`, `WARNING` or `ERROR`.",
}

ROOT_FIELDS = ("database_path", "listen_host", "listen_port", "log_level")


def _type_name(schema: dict[str, Any]) -> str:
    if "enum" in schema:
        return " \\| ".join(f"`{value}`" for value in schema["enum"])
    if "const" in schema:
        return f"`{schema['const']}`"
    if "anyOf" in schema:
        parts = [_type_name(item) for item in schema["anyOf"] if item.get("type") != "null"]
        return " \\| ".join(dict.fromkeys(parts))
    kind = schema.get("type")
    if kind == "array":
        return f"list of {_type_name(schema.get('items', {}))}"
    return {
        "string": "string",
        "integer": "integer",
        "number": "number",
        "boolean": "boolean",
        "object": "object",
    }.get(kind or "", kind or "any")


def _yaml_literal(value: object) -> str:
    """Render a default the way it would be written in config.yaml, not in Python."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(_yaml_literal(item) for item in value) + "]"
    return str(value)


def _default(model: type, name: str) -> str:
    field = model.model_fields[name]
    if field.default_factory is not None:  # type: ignore[union-attr]
        return f"`{_yaml_literal(field.default_factory())}`"  # type: ignore[misc,operator]
    default = field.default
    if repr(default) == "PydanticUndefined":
        return "—"
    if hasattr(default, "get_secret_value"):
        return "—"
    # A `None` default is a value, not a missing one: `surge_threshold` ships off, and a
    # `Preset` override left at `null` inherits the behavior setting. Rendering both as the
    # em dash used for "no default" said the opposite of what the model does.
    if default is None:
        return "`null`"
    return f"`{_yaml_literal(default)}`"


def _constraints(schema: dict[str, Any]) -> str:
    bits = []
    for key, label in (
        ("minimum", ">="),
        ("exclusiveMinimum", ">"),
        ("maximum", "<="),
        ("exclusiveMaximum", "<"),
    ):
        if key in schema:
            bits.append(f"{label} {schema[key]}")
    return ", ".join(bits)


def _rows(model: type, prefix: str) -> list[str]:
    schema = model.model_json_schema(by_alias=False)
    rows: list[str] = []
    for name, field_schema in schema.get("properties", {}).items():
        key = f"{prefix}.{name}" if prefix else name
        note = NOTES.get(key, "")
        constraint = _constraints(field_schema)
        if constraint:
            note = f"{note} ({constraint})".strip()
        rows.append(f"| `{name}` | {_type_name(field_schema)} | {_default(model, name)} | {note} |")
    return rows


def render_markdown() -> str:
    lines: list[str] = [
        "<!-- Generated by scripts/gen_docs.py from the pydantic models in",
        "     src/immich_compressor/config.py. Do not edit by hand: CI regenerates this",
        "     file and fails if it differs. Change the model, or the NOTES table in the",
        "     generator, and run `make docs`. -->",
        "",
        "# Configuration reference",
        "",
        f"Generated from the settings model of immich-compressor {__version__}.",
        "",
        "`config.yaml` is optional. Every value below has a default, and the encoder is "
        "detected rather than configured, so the shortest working file is an empty one.",
        "",
        "## Where values come from",
        "",
        "Highest priority first:",
        "",
        "1. **environment variables**, with `__` as the nesting separator "
        "(`BEHAVIOR__DRY_RUN=false`, `IMMICH__BASE_URL=...`);",
        "2. **`config.yaml`**, at `$COMPRESSOR_CONFIG` or `./config.yaml`;",
        "3. the defaults in this document.",
        "",
        "The file losing against the environment is deliberate: it is what makes "
        "`BEHAVIOR__DRY_RUN=false` in compose actually take effect on a deployment whose "
        "`config.yaml` says `dry_run: true`.",
        "",
        "## Secrets",
        "",
        "`immich.api_key` and `webhook.token` are read **only** from the environment, as "
        "`IMMICH__API_KEY` and `WEBHOOK__TOKEN`. Writing either of them into `config.yaml` "
        "makes the service refuse to start, so a secret cannot end up in a repository or "
        "an image layer by accident.",
        "",
        "## Editor support",
        "",
        "`config.example.yaml` carries a `yaml-language-server` modeline pointing at "
        "[`config.schema.json`](config.schema.json), so an editor with the YAML extension "
        "autocompletes keys and flags invalid values as you type. Copy the modeline into "
        "your own `config.yaml` to get the same.",
        "",
    ]

    for name, model, blurb in SECTIONS:
        lines += [
            f"## `{name}`",
            "",
            blurb,
            "",
            "| Option | Type | Default | Notes |",
            "|---|---|---|---|",
            *_rows(model, name),
            "",
        ]

    root_schema = Settings.model_json_schema(by_alias=False)
    lines += [
        "## Top level",
        "",
        "| Option | Type | Default | Notes |",
        "|---|---|---|---|",
    ]
    for name in ROOT_FIELDS:
        field_schema = root_schema["properties"][name]
        lines.append(
            f"| `{name}` | {_type_name(field_schema)} | {_default(Settings, name)} | {NOTES.get(name, '')} |"
        )
    lines += [
        "",
        "## `presets`",
        "",
        "Leave this out and the service builds the preset for you from the detected "
        "hardware — that is the point of [hardware detection](hardware.md). Write it and "
        "it wins over everything, detection is skipped entirely, and `hardware.mode` and "
        "`behavior.quality` no longer apply. Both forms are accepted: a mapping keyed by "
        "name, or a list of objects with a `name` field.",
        "",
        "```yaml",
        "presets:",
        "  video-h265:",
        "    match: { type: VIDEO }",
        "    cmd: >",
        "      ffmpeg -y -loglevel error -noautorotate -i {input}",
        "      -map_metadata 0 -map 0 -movflags use_metadata_tags+faststart",
        "      -c:v libx265 -preset medium -crf 26 -tag:v hvc1",
        "      -x265-params pools=2 -threads 2",
        "      -c:a aac -b:a 128k",
        "      {output}",
        "    suffix: .mp4",
        "  image-jpeg:",
        "    match:",
        "      type: IMAGE",
        "      extensions: [.jpg, .jpeg, .jpe, .jfif]",
        "    cmd: magick {input} -auto-orient -quality 82 -interlace Plane {output}",
        "    suffix: .jpg",
        "    exiftool_copy: true",
        "    normalize_orientation: true",
        "    max_ratio: 0.9",
        "    require_date_time_original: false",
        "    min_source_quality: 86",
        "```",
        "",
        "| Option | Type | Default | Notes |",
        "|---|---|---|---|",
        *_rows(Preset, ""),
        "",
        "`{input}` and `{output}` are both required. Commands run **without a shell** — "
        "they are `shlex.split` at load time and rejected outright if a token is a shell "
        "control operator (`|`, `&&`, `;`), a redirection (`>`, `2>&1`) or a command "
        "substitution (`` ` ``, `$(`). A `|` *inside* a token is fine, because that is "
        "ffmpeg's format-alternation syntax and not a pipe.",
        "",
        "`exiftool_copy` is required for stills, which otherwise lose all metadata on "
        "re-encode. `normalize_orientation` additionally keeps the source `Orientation` out "
        "of that copy and pins the output to 1 — correct only when the command normalises "
        "the pixels itself with `-auto-orient`, and validated at startup.",
        "",
        "Presets are matched in order, and the first one whose `type` **and** "
        "`match.extensions` accept the file wins. A type with a preset but no matching "
        "extension is skipped as `unsupported_format`, which is a different thing from "
        "`no_preset` and reads differently in a report.",
        "",
    ]
    return "\n".join(lines) + "\n"


def render_schema() -> str:
    schema = Settings.model_json_schema(by_alias=False)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = (
        "https://raw.githubusercontent.com/Navilois/immich-compressor/main/docs/config.schema.json"
    )
    schema["title"] = "immich-compressor configuration"
    schema["description"] = (
        "Generated from the settings model by scripts/gen_docs.py. Secrets "
        "(immich.api_key, webhook.token) are rejected in this file and come from the "
        "environment instead."
    )
    return json.dumps(schema, indent=2, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify the files are current; write nothing")
    args = parser.parse_args(argv)

    wanted = {CONFIG_MD: render_markdown(), SCHEMA_JSON: render_schema()}
    stale = [
        path
        for path, body in wanted.items()
        if not path.is_file() or path.read_text(encoding="utf-8") != body
    ]

    if args.check:
        if stale:
            for path in stale:
                print(f"out of date: {path.relative_to(REPO)}")
            print("\nrun `make docs` (or `python scripts/gen_docs.py`) and commit the result")
            return 1
        print(f"generated docs are current ({len(wanted)} file(s))")
        return 0

    CONFIG_MD.parent.mkdir(parents=True, exist_ok=True)
    for path, body in wanted.items():
        path.write_text(body, encoding="utf-8")
        print(f"wrote {path.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
