#!/usr/bin/env python3
"""Check that every workflow file parses and names only permission scopes that exist.

GitHub validates a workflow when it is *used*, not when it is pushed. An invalid
`permissions:` key is not a warning on that one line — the whole file fails to parse, and
every trigger on it dies with it. A `workflow_dispatch` answers HTTP 422, a `push` trigger
silently never runs, and nothing in the Actions tab says why. This repository shipped
exactly that: `administration: read`, which reads like a scope and is not one, took the
release workflow out entirely between 1.3.0 and the fix.

So: parse every workflow, and check the scope names against the documented set.

    python scripts/check-workflows.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO / ".github" / "workflows"

# https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#permissions
# Note what is not here: `administration`, `members`, `metadata` and the rest of the
# fine-grained-PAT scopes. GITHUB_TOKEN cannot be granted them, and naming one is the
# parse error this guard exists for.
SCOPES = frozenset(
    {
        "actions",
        "attestations",
        "checks",
        "contents",
        "deployments",
        "discussions",
        "id-token",
        "issues",
        "models",
        "packages",
        "pages",
        "pull-requests",
        "repository-projects",
        "security-events",
        "statuses",
    }
)
LEVELS = frozenset({"read", "write", "none"})
SHORTHANDS = frozenset({"read-all", "write-all"})


def line_of(source: str, key: str) -> int:
    """The first line that opens `key:`, for output a terminal can jump to."""
    for number, line in enumerate(source.splitlines(), start=1):
        if line.strip().startswith(f"{key}:"):
            return number
    return 0


def check_block(block: object, source: str, where: str) -> list[str]:
    """Validate one `permissions:` value — a shorthand string, or a scope mapping."""
    if block is None:  # `permissions:` with nothing under it means "none for all".
        return []
    if isinstance(block, str):
        if block not in SHORTHANDS:
            return [f"{where}: permissions: {block} is not {' or '.join(sorted(SHORTHANDS))}"]
        return []
    if not isinstance(block, dict):
        return [f"{where}: permissions: expected a mapping or a shorthand string"]

    problems = []
    for scope, level in block.items():
        if scope not in SCOPES:
            problems.append(f"{where}:{line_of(source, str(scope))}: '{scope}' is not a permission scope")
        elif level not in LEVELS:
            problems.append(
                f"{where}:{line_of(source, str(scope))}: '{scope}: {level}' is not "
                f"{', '.join(sorted(LEVELS))}"
            )
    return problems


def main() -> int:
    files = sorted(p for p in WORKFLOWS.glob("*.y*ml"))
    if not files:
        print(f"no workflows under {WORKFLOWS.relative_to(REPO)}")
        return 1

    problems: list[str] = []
    blocks = 0
    for path in files:
        where = str(path.relative_to(REPO))
        source = path.read_text(encoding="utf-8")
        try:
            document = yaml.safe_load(source)
        except yaml.YAMLError as error:
            problems.append(f"{where}: does not parse: {error}")
            continue
        if not isinstance(document, dict):
            problems.append(f"{where}: is not a workflow")
            continue

        if "permissions" in document:
            blocks += 1
            problems += check_block(document["permissions"], source, where)
        jobs = document.get("jobs") or {}
        if not isinstance(jobs, dict):
            problems.append(f"{where}: jobs: expected a mapping")
            continue
        for name, job in jobs.items():
            if isinstance(job, dict) and "permissions" in job:
                blocks += 1
                problems += check_block(job["permissions"], source, f"{where} ({name})")

    if problems:
        print(f"{len(problems)} workflow problem(s):")
        for problem in problems:
            print(f"  {problem}")
        return 1

    print(f"workflows ok: {len(files)} file(s), {blocks} permissions block(s) checked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
