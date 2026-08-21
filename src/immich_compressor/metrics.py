"""Prometheus exposition, hand-rolled.

Homelab users build dashboards out of whatever a service exposes, and the whole surface
here is a handful of counters that already exist in the job store. A client library would
be a runtime dependency, a metrics registry and a second place for state to live, in
exchange for about sixty lines. So: no dependency.

The format is the text exposition format, version 0.0.4 — the one every Prometheus,
VictoriaMetrics and OpenTelemetry collector reads:
https://prometheus.io/docs/instrumenting/exposition_formats/
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

PREFIX = "immich_compressor"

# Seconds. Chosen for what an encode on a home server actually takes: a phone clip on a
# GPU lands in the first bucket, a long 4K clip on a CPU preset in the last two.
DURATION_BUCKETS: tuple[float, ...] = (10.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0, 3600.0)


@dataclass(slots=True)
class Histogram:
    """A cumulative histogram, kept as plain counters.

    Values are per process and reset when the container restarts, which is exactly what
    Prometheus expects of a counter — it detects the reset and carries on.
    """

    buckets: tuple[float, ...] = DURATION_BUCKETS
    counts: list[int] = field(default_factory=lambda: [0] * len(DURATION_BUCKETS))
    total: float = 0.0
    observations: int = 0

    def observe(self, value: float) -> None:
        """Record one observation.

        ``counts`` holds *per-bucket* counts, so only the first bucket the value fits in
        is incremented; :meth:`cumulative` adds them up on the way out. Incrementing every
        matching bucket here and accumulating again there would double-count.

        A value larger than the last edge lands in no bucket at all, and shows up only in
        the ``+Inf`` bucket, which is emitted from ``observations``.
        """
        self.total += value
        self.observations += 1
        for index, edge in enumerate(self.buckets):
            if value <= edge:
                self.counts[index] += 1
                return

    def cumulative(self) -> list[tuple[float, int]]:
        """``le`` boundaries with cumulative counts, which is what the format wants."""
        running = 0
        rows: list[tuple[float, int]] = []
        for edge, count in zip(self.buckets, self.counts, strict=True):
            running += count
            rows.append((edge, running))
        return rows


def _number(value: float) -> str:
    """Render a sample value without silently rounding it.

    ``%g`` looked right and is not: it keeps six significant digits and switches to
    scientific notation, so a byte counter of 50710662 goes out as 5.07107e+07 — valid
    exposition text, wrong number. Integers stay integers; a genuinely fractional value
    falls back to ``repr``, which round-trips.
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if value.is_integer() and abs(value) < 2**53:
        return str(int(value))
    return repr(value)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _metric(name: str, value: float, labels: dict[str, str] | None = None) -> str:
    if not labels:
        return f"{PREFIX}_{name} {_number(value)}"
    rendered = ",".join(f'{key}="{_escape(val)}"' for key, val in sorted(labels.items()))
    return f"{PREFIX}_{name}{{{rendered}}} {_number(value)}"


def _block(name: str, help_text: str, kind: str, lines: list[str]) -> list[str]:
    """A metric family. Emitted even when empty, so a dashboard's query never 404s."""
    return [f"# HELP {PREFIX}_{name} {help_text}", f"# TYPE {PREFIX}_{name} {kind}", *lines, ""]


def render(
    *,
    store_stats: dict[str, Any],
    counters: dict[str, int],
    session: dict[str, Any],
    encode_seconds: Histogram,
    config: dict[str, Any],
    version: str,
) -> str:
    """Render everything the service knows into the text exposition format."""
    lines: list[str] = []

    lines += _block(
        "build_info",
        "Version of the running service, as a label on a constant 1.",
        "gauge",
        [_metric("build_info", 1, {"version": version})],
    )

    by_state: dict[str, int] = store_stats.get("by_state", {})
    lines += _block(
        "jobs",
        "Jobs in the store, by state.",
        "gauge",
        [_metric("jobs", count, {"state": state}) for state, count in sorted(by_state.items())],
    )

    by_reason: dict[str, int] = store_stats.get("by_skip_reason", {})
    lines += _block(
        "jobs_skipped",
        "Skipped jobs, by the reason they were skipped.",
        "gauge",
        [_metric("jobs_skipped", count, {"reason": reason}) for reason, count in sorted(by_reason.items())],
    )

    lines += _block(
        "jobs_total",
        "Jobs in the store, all states.",
        "gauge",
        [_metric("jobs_total", store_stats.get("total", 0))],
    )
    lines += _block(
        "compressed_assets",
        "Assets with a verified replacement.",
        "gauge",
        [_metric("compressed_assets", store_stats.get("compressed_assets", 0))],
    )
    lines += _block(
        "original_bytes",
        "Total size of the originals that were replaced.",
        "gauge",
        [_metric("original_bytes", store_stats.get("original_bytes", 0))],
    )
    lines += _block(
        "compressed_bytes",
        "Total size of their replacements.",
        "gauge",
        [_metric("compressed_bytes", store_stats.get("compressed_bytes", 0))],
    )
    lines += _block(
        "saved_bytes",
        "Difference between the two. Space is only reclaimed once the original is gone.",
        "gauge",
        [_metric("saved_bytes", store_stats.get("saved_bytes", 0))],
    )

    # Webhook counters, from the database rather than from this process: a mismatched
    # shared secret writes nothing else anywhere, so `rate(webhooks_rejected_total[5m]) > 0`
    # is the only alert that can catch it. These survive a restart, unlike the session
    # counters below — Prometheus copes with either.
    for name, help_text in (
        ("webhooks_received_total", "Webhooks that passed the shared-secret check."),
        (
            "webhooks_rejected_total",
            "Webhooks refused for a bad or missing shared secret. Anything above zero "
            "means the workflow's headerValue and WEBHOOK__TOKEN disagree.",
        ),
    ):
        source_key = name.removesuffix("_total")
        lines += _block(name, help_text, "counter", [_metric(name, counters.get(source_key, 0))])

    # Session counters: reset on restart, which is what a counter is allowed to do.
    for name, help_text in (
        ("processed", "Assets compressed and uploaded since this process started."),
        ("skipped", "Assets skipped since this process started."),
        ("failed", "Jobs that failed since this process started."),
        ("deleted", "Originals removed since this process started."),
    ):
        lines += _block(
            f"session_{name}_total",
            help_text,
            "counter",
            [_metric(f"session_{name}_total", session.get(name, 0))],
        )
    lines += _block(
        "session_bytes_saved_total",
        "Bytes saved since this process started.",
        "counter",
        [_metric("session_bytes_saved_total", session.get("bytes_saved", 0))],
    )

    histogram = [
        _metric("encode_duration_seconds_bucket", count, {"le": f"{edge:g}"})
        for edge, count in encode_seconds.cumulative()
    ]
    histogram += [
        _metric("encode_duration_seconds_bucket", encode_seconds.observations, {"le": "+Inf"}),
        _metric("encode_duration_seconds_sum", encode_seconds.total),
        _metric("encode_duration_seconds_count", encode_seconds.observations),
    ]
    lines += _block(
        "encode_duration_seconds",
        "Wall-clock time of the encoder command, since this process started.",
        "histogram",
        histogram,
    )

    # The three settings worth alerting on: a deployment that quietly went live, or one
    # that quietly did not.
    lines += _block(
        "config_dry_run",
        "1 when the service is in dry-run mode and changes nothing.",
        "gauge",
        [_metric("config_dry_run", int(bool(config.get("dry_run"))))],
    )
    lines += _block(
        "config_trash_original",
        "1 when verified originals are removed.",
        "gauge",
        [_metric("config_trash_original", int(bool(config.get("trash_original"))))],
    )
    lines += _block(
        "config_permanent_delete",
        "1 when originals bypass the trash and cannot be restored.",
        "gauge",
        [_metric("config_permanent_delete", int(config.get("delete_mode") == "permanent"))],
    )

    return "\n".join(lines).rstrip("\n") + "\n"
