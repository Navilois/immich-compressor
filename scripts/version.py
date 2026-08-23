#!/usr/bin/env python3
"""Check that `__version__`, the CHANGELOG and the upgrading notes still agree.

`__version__` is the single source of truth, but a release moves three other things by
hand, and nothing has been watching them. Two of those hand edits have already gone
wrong: `docs/upgrading.md` kept its `## Unreleased` heading through two releases, so
behaviour that had shipped was documented as upcoming, and the CHANGELOG's compare links
have never been checked against the sections they belong to.

This is the structural check — offline, no network, no git unless asked:

* `__version__` is a semantic version, and it is the newest one the CHANGELOG names;
* the CHANGELOG has exactly one `Unreleased` section, and it is the first one;
* every version section is dated, ordered newest first, and has a link reference
  definition pointing where its neighbours say it should;
* `docs/upgrading.md` carries no `Unreleased` heading once the CHANGELOG's is empty,
  which is what a release chore renaming one file and forgetting the other looks like.

    python scripts/version.py check
    python scripts/version.py check --strict    # also: every version section has a tag

`--strict` needs the repository's tags to be complete. They are not: v1.0.0 and v1.1.0
were never pushed, from when this repository was private. It belongs in the release
workflow once those two exist, not in CI.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

CHANGELOG = "CHANGELOG.md"
UPGRADING = "docs/upgrading.md"
INIT_PY = "src/immich_compressor/__init__.py"

# Semantic versioning, with the pre-release suffix allowed even though nothing has used
# one yet: rejecting `1.3.0-rc1` here would be this checker inventing a policy.
CORE = r"\d+\.\d+\.\d+"
VERSION_PATTERN = rf"{CORE}(?:-[0-9A-Za-z.-]+)?"

DUNDER_RE = re.compile(rf'^__version__ = "({VERSION_PATTERN})"$', re.MULTILINE)
H2_RE = re.compile(r"^## (.+?)[ \t]*$", re.MULTILINE)
RELEASE_HEADING_RE = re.compile(rf"^\[({VERSION_PATTERN})\](?:[ \t]+-[ \t]+(.*))?$")
LINK_REF_RE = re.compile(r"^\[([^\]]+)\]:[ \t]+(\S+)[ \t]*$", re.MULTILINE)
UPGRADING_UNRELEASED_RE = re.compile(r"^## Unreleased[ \t]*$", re.MULTILINE)

UNRELEASED = "Unreleased"


@dataclass(frozen=True)
class Section:
    """One `##` section of the CHANGELOG."""

    title: str
    line: int
    body: str
    version: str | None = None
    date_text: str | None = None


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def sections_of(changelog: str) -> list[Section]:
    """Every `##` heading with the text that follows it, in file order."""
    heads = list(H2_RE.finditer(changelog))
    found: list[Section] = []
    for index, head in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(changelog)
        title = head.group(1)
        release = RELEASE_HEADING_RE.match(title)
        found.append(
            Section(
                title=title,
                line=line_of(changelog, head.start()),
                body=changelog[head.end() : end],
                version=release.group(1) if release else None,
                date_text=release.group(2) if release else None,
            )
        )
    return found


def sort_key(version: str) -> tuple[int, ...]:
    """Order by the numeric core only. Pre-releases sort with the version they precede."""
    return tuple(int(part) for part in version.split("-")[0].split("."))


def source_url() -> str:
    """The project's own repository URL, from pyproject — not hardcoded, so a fork that
    forgets to rewrite the CHANGELOG links hears about it."""
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    url = data["project"]["urls"]["Source"]
    return url.removesuffix("/").removesuffix(".git")


def expected_link(source: str, version: str, previous: str | None) -> str:
    """Where a section's link reference has to point.

    The oldest section has nothing to be compared against, so Keep a Changelog points it
    at the tag itself.
    """
    if version == UNRELEASED:
        return f"{source}/compare/v{previous}...HEAD" if previous else f"{source}/commits/main"
    if previous is None:
        return f"{source}/releases/tag/v{version}"
    return f"{source}/compare/v{previous}...v{version}"


def audit(*, changelog: str, upgrading: str, init_py: str, source: str) -> list[str]:
    """Every disagreement between the three files, as `file:line: problem` lines."""
    problems: list[str] = []

    dunder = DUNDER_RE.search(init_py)
    if dunder is None:
        problems.append(f'{INIT_PY}: no `__version__ = "X.Y.Z"` line, or it is not a version')
        return problems
    current = dunder.group(1)

    parsed = sections_of(changelog)
    unreleased = [section for section in parsed if section.title == f"[{UNRELEASED}]"]
    releases = [section for section in parsed if section.version is not None]

    if len(unreleased) != 1:
        where = ", ".join(f"line {section.line}" for section in unreleased) or "none found"
        problems.append(f"{CHANGELOG}: expected exactly one `## [{UNRELEASED}]` section ({where})")
    elif parsed and parsed[0] is not unreleased[0]:
        problems.append(
            f"{CHANGELOG}:{unreleased[0].line}: `## [{UNRELEASED}]` must be the first section, "
            f"not below `## {parsed[0].title}`"
        )

    if not releases:
        problems.append(f"{CHANGELOG}: no released version section")
        return problems

    for section in releases:
        if not section.date_text:
            problems.append(f"{CHANGELOG}:{section.line}: `[{section.version}]` has no ` - date`")
            continue
        try:
            date.fromisoformat(section.date_text)
        except ValueError:
            problems.append(f"{CHANGELOG}:{section.line}: `{section.date_text}` is not an ISO-8601 date")

    for older, newer in zip(releases[1:], releases, strict=False):
        if sort_key(newer.version or "") <= sort_key(older.version or ""):
            problems.append(
                f"{CHANGELOG}:{older.line}: `[{older.version}]` is not older than "
                f"`[{newer.version}]` above it; sections run newest first"
            )
        if newer.date_text and older.date_text:
            try:
                if date.fromisoformat(newer.date_text) < date.fromisoformat(older.date_text):
                    problems.append(
                        f"{CHANGELOG}:{newer.line}: `[{newer.version}]` is dated before "
                        f"`[{older.version}]` below it"
                    )
            except ValueError:
                pass  # already reported as unparseable above

    newest = releases[0].version or ""
    if current != newest:
        problems.append(
            f"{INIT_PY}: __version__ is {current}, but the newest CHANGELOG section is "
            f"{newest} (line {releases[0].line})"
        )

    refs = {match.group(1): match for match in LINK_REF_RE.finditer(changelog)}
    ordered = [UNRELEASED, *(section.version or "" for section in releases)]
    for index, version in enumerate(ordered):
        previous = ordered[index + 1] if index + 1 < len(ordered) else None
        ref = refs.get(version)
        if ref is None:
            problems.append(f"{CHANGELOG}: `[{version}]` has no link reference definition")
            continue
        wanted = expected_link(source, version, previous)
        if ref.group(2) != wanted:
            problems.append(
                f"{CHANGELOG}:{line_of(changelog, ref.start())}: `[{version}]` points at "
                f"{ref.group(2)}, expected {wanted}"
            )

    known = set(ordered)
    for name, ref in refs.items():
        if name not in known and re.fullmatch(VERSION_PATTERN, name):
            problems.append(
                f"{CHANGELOG}:{line_of(changelog, ref.start())}: `[{name}]` is defined but has no section"
            )

    problems.extend(_upgrading_problems(unreleased, upgrading))
    return problems


def _upgrading_problems(unreleased: list[Section], upgrading: str) -> list[str]:
    """The heading that outlived its release.

    Checked in one direction only. An empty CHANGELOG `Unreleased` means the release chore
    has run, so an `Unreleased` heading left in the upgrading notes describes shipped
    behaviour as upcoming. The other direction is not a rule: plenty of unreleased changes
    need no note for operators at all.
    """
    problems: list[str] = []
    headings = list(UPGRADING_UNRELEASED_RE.finditer(upgrading))
    if len(headings) > 1:
        problems.append(f"{UPGRADING}: {len(headings)} `## {UNRELEASED}` headings, expected at most one")
    if headings and len(unreleased) == 1 and not unreleased[0].body.strip():
        problems.append(
            f"{UPGRADING}:{line_of(upgrading, headings[0].start())}: `## {UNRELEASED}` is still "
            f"here while the CHANGELOG's is empty; the release chore renames both"
        )
    return problems


def missing_tags(versions: list[str]) -> list[str]:
    listed = subprocess.run(
        ["git", "tag", "--list"],  # noqa: S607
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    tags = set(listed.stdout.split())
    return [f"v{version}" for version in versions if f"v{version}" not in tags]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    checker = sub.add_parser("check", help="the version, the CHANGELOG and the upgrading notes agree")
    checker.add_argument(
        "--strict",
        action="store_true",
        help="also require a git tag for every released section",
    )
    args = parser.parse_args()

    changelog = (REPO / CHANGELOG).read_text(encoding="utf-8")
    problems = audit(
        changelog=changelog,
        upgrading=(REPO / UPGRADING).read_text(encoding="utf-8"),
        init_py=(REPO / INIT_PY).read_text(encoding="utf-8"),
        source=source_url(),
    )

    released = [section.version or "" for section in sections_of(changelog) if section.version]
    if args.strict:
        problems.extend(f"{CHANGELOG}: no git tag {tag}" for tag in missing_tags(released))

    if problems:
        print(f"{len(problems)} version problem(s):")
        for problem in problems:
            print(f"  {problem}")
        return 1

    print(f"version ok: {released[0] if released else '?'}, {len(released)} released section(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
