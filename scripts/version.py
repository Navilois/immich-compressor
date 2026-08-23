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
    python scripts/version.py next              # the version the commits since the tag ask for
    python scripts/version.py set 1.3.0         # perform the release chore
    python scripts/version.py set auto --check  # ...and show what it would write instead

`check` is what CI runs. `check --strict --tag vX.Y.Z` is what the release workflow runs:
it additionally requires a tag per released section, and refuses a tag that is not the
newest version, because publishing an older one would move `latest` and the major tag
backwards.

`next` reads the conventional commits since the newest tag. `set` performs the whole
release chore in one go and then checks its own output with `check`.
"""

from __future__ import annotations

import argparse
import difflib
import re
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
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
UNRELEASED_HEADING_RE = re.compile(r"^## \[Unreleased\][ \t]*$", re.MULTILINE)
UPGRADING_UNRELEASED_RE = re.compile(r"^## Unreleased[ \t]*$", re.MULTILINE)

UNRELEASED = "Unreleased"

# Conventional commits. Only the types that move a version are listed; everything else —
# docs, chore, refactor, test, ci, build, style — is deliberately not a release on its own.
BUMP_BY_TYPE = {"feat": "minor", "fix": "patch", "perf": "patch"}
BUMP_RANK = {"patch": 1, "minor": 2, "major": 3}

SUBJECT_RE = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]+)\))?(?P<breaking>!)?: (?P<subject>.+)$")
BREAKING_FOOTER_RE = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)


class VersionError(Exception):
    """A release chore that must not be completed as asked."""


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


def sort_key(version: str) -> tuple[tuple[int, ...], int, str]:
    """Semantic version order, including the rule that 1.3.0-rc1 comes before 1.3.0."""
    core, _, pre = version.partition("-")
    numbers = tuple(int(part) for part in core.split("."))
    return (numbers, 0, pre) if pre else (numbers, 1, "")


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


def repository_tags() -> list[str]:
    listed = subprocess.run(
        ["git", "tag", "--list"],  # noqa: S607
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    return listed.stdout.split()


def missing_tags(versions: list[str], tags: list[str] | None = None) -> list[str]:
    known = set(repository_tags() if tags is None else tags)
    return [f"v{version}" for version in versions if f"v{version}" not in known]


def newest_tag(tags: list[str]) -> str | None:
    """The highest `vX.Y.Z` tag, in semantic version order."""
    versions = [tag for tag in tags if re.fullmatch(rf"v{VERSION_PATTERN}", tag)]
    return max(versions, key=lambda tag: sort_key(tag[1:]), default=None)


def stale_tag_problems(tag: str, tags: list[str]) -> list[str]:
    """Refuse to publish a tag that is not the newest version.

    The release workflow's tag set includes `latest` and the bare major, and
    docker-compose.yaml pins the major, so re-pushing an old tag would move both
    backwards and downgrade every deployment on its next pull. This is demonstrated
    rather than theorised: pushing a retroactively created v1.1.0 started the release
    workflow, and only the cancel beat the image job to the registry.

    When a maintenance branch for an older major becomes real, the fix is to make
    `latest` and `{{major}}` conditional in the workflow, not to drop this check.
    """
    if not re.fullmatch(rf"v{VERSION_PATTERN}", tag):
        return [f"{tag} is not a vX.Y.Z tag"]
    newest = newest_tag([*tags, tag])
    if newest is not None and tag != newest:
        return [
            f"{tag} is not the newest tag ({newest}); publishing it would move `latest` "
            f"and the major tag backwards"
        ]
    return []


# --- deriving the next version -------------------------------------------------------


def commits_since(tag: str | None) -> list[str]:
    """Every non-merge commit message since `tag`, newest first.

    Merges are excluded because they carry no conventional type of their own — this
    repository merges rather than squashes, so the commits inside a merge are the real
    history and are all present here.
    """
    if tag is not None and not re.fullmatch(rf"v{VERSION_PATTERN}", tag):
        raise VersionError(f"{tag} is not a vX.Y.Z tag, and will not be handed to git")
    span = f"{tag}..HEAD" if tag else "HEAD"
    # No shell, and `span` is built from a tag this function has just matched against the
    # version pattern, so it cannot be anything but a revision range.
    listed = subprocess.run(  # noqa: S603
        ["git", "log", "--no-merges", "--format=%B%x00", span],  # noqa: S607
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    return [chunk.strip() for chunk in listed.stdout.split("\0") if chunk.strip()]


def bump_of(message: str) -> str | None:
    """What one commit message asks for, or None when it asks for no release at all."""
    lines = message.splitlines()
    parsed = SUBJECT_RE.match(lines[0]) if lines else None
    if parsed is None:
        return None
    if parsed.group("breaking") or BREAKING_FOOTER_RE.search(message):
        return "major"
    return BUMP_BY_TYPE.get(parsed.group("type"))


def bump_from_commits(messages: Sequence[str]) -> tuple[str | None, list[tuple[str, str]]]:
    """The highest bump the commits ask for, and the subject behind each one.

    A commit that is not a conventional commit, or whose type is not one that releases,
    contributes nothing. That is the whole rule: a branch of `docs:` and `chore:` commits
    is not a release, however many there are.
    """
    reasons = [(bump, message.splitlines()[0]) for message in messages if (bump := bump_of(message))]
    highest = max((bump for bump, _ in reasons), key=lambda bump: BUMP_RANK[bump], default=None)
    return highest, reasons


def next_version(current: str, bump: str) -> str:
    major, minor, patch = (int(part) for part in current.partition("-")[0].split("."))
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


# --- performing the release chore ----------------------------------------------------


def release_init_py(init_py: str, *, version: str) -> str:
    return DUNDER_RE.sub(f'__version__ = "{version}"', init_py, count=1)


def release_changelog(changelog: str, *, version: str, previous: str, today: date, source: str) -> str:
    """Date the Unreleased section, open an empty one above it, and move the links."""
    heading = f"## [{UNRELEASED}]"
    if not UNRELEASED_HEADING_RE.search(changelog):
        raise VersionError(f"{CHANGELOG} has no `{heading}` heading")
    text = UNRELEASED_HEADING_RE.sub(f"{heading}\n\n## [{version}] - {today.isoformat()}", changelog, count=1)

    stale = f"[{UNRELEASED}]: {expected_link(source, UNRELEASED, previous)}"
    if stale not in text:
        raise VersionError(f"{CHANGELOG} has no `{stale}` line to move")
    fresh = (
        f"[{UNRELEASED}]: {expected_link(source, UNRELEASED, version)}\n"
        f"[{version}]: {expected_link(source, version, previous)}"
    )
    return text.replace(stale, fresh, 1)


def release_upgrading(upgrading: str, *, version: str, previous: str) -> str:
    """Rename the operator note's heading, if there is one.

    Not every release has one, and that is legitimate — most changes need nothing from an
    operator. What is never legitimate is leaving it named `Unreleased`, which is what
    `check` refuses.
    """
    if not UPGRADING_UNRELEASED_RE.search(upgrading):
        return upgrading
    return UPGRADING_UNRELEASED_RE.sub(f"## {previous} \u2192 {version}", upgrading, count=1)


def regenerate_docs() -> None:
    """Rebuild the generated documentation, which carries `__version__` in its header.

    Without this the release commit fails the very CI it has to pass: `gen_docs.py --check`
    compares `docs/configuration.md` against the settings model, and the header line reads
    "Generated from the settings model of immich-compressor X.Y.Z".

    Shelled out rather than imported. `version.py` imports nothing from the package on
    purpose — that is what lets the `version` job in CI run with no install at all — and
    `gen_docs` needs pydantic.
    """
    generated = subprocess.run(  # noqa: S603
        [sys.executable, str(REPO / "scripts" / "gen_docs.py")],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if generated.returncode != 0:
        detail = generated.stderr.strip() or generated.stdout.strip()
        raise VersionError(
            f"the version was written, but the generated documentation could not be rebuilt:\n  {detail}"
        )


def unified(before: str, after: str, name: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=name,
            tofile=name,
        )
    )


def current_version() -> str:
    found = DUNDER_RE.search((REPO / INIT_PY).read_text(encoding="utf-8"))
    if found is None:
        raise VersionError(f'{INIT_PY} has no `__version__ = "X.Y.Z"` line')
    return found.group(1)


def run_check(args: argparse.Namespace) -> int:
    changelog = (REPO / CHANGELOG).read_text(encoding="utf-8")
    problems = audit(
        changelog=changelog,
        upgrading=(REPO / UPGRADING).read_text(encoding="utf-8"),
        init_py=(REPO / INIT_PY).read_text(encoding="utf-8"),
        source=source_url(),
    )

    released = [section.version or "" for section in sections_of(changelog) if section.version]
    if args.strict or args.tag:
        tags = repository_tags()
        if args.strict:
            problems.extend(f"{CHANGELOG}: no git tag {tag}" for tag in missing_tags(released, tags))
        if args.tag:
            problems.extend(stale_tag_problems(args.tag, tags))

    if problems:
        print(f"{len(problems)} version problem(s):")
        for problem in problems:
            print(f"  {problem}")
        return 1

    print(f"version ok: {released[0] if released else '?'}, {len(released)} released section(s)")
    return 0


def derive() -> tuple[str, list[tuple[str, str]]]:
    """The version the commits since the newest tag ask for."""
    current = current_version()
    tag = newest_tag(repository_tags())
    bump, reasons = bump_from_commits(commits_since(tag))
    if bump is None:
        since = f"since {tag}" if tag else "in this repository"
        raise VersionError(
            f"nothing to release: no feat, fix or perf commit {since}. "
            f"{len(commits_since(tag))} commit(s) looked at, all of them types that do not release."
        )
    return next_version(current, bump), reasons


def run_next(args: argparse.Namespace) -> int:
    version, reasons = derive()
    for bump, subject in reasons:
        print(f"  {bump:5}  {subject}", file=sys.stderr)
    print(version)
    return 0


def run_set(args: argparse.Namespace) -> int:
    current = current_version()
    version = derive()[0] if args.version == "auto" else args.version
    if version in {"major", "minor", "patch"}:
        version = next_version(current, version)
    if not re.fullmatch(VERSION_PATTERN, version):
        raise VersionError(f"{version} is not a version, and not one of auto, major, minor, patch")
    if sort_key(version) <= sort_key(current):
        raise VersionError(f"{version} does not come after the current {current}")

    changelog = (REPO / CHANGELOG).read_text(encoding="utf-8")
    upgrading = (REPO / UPGRADING).read_text(encoding="utf-8")
    init_py = (REPO / INIT_PY).read_text(encoding="utf-8")
    source = source_url()

    parsed = sections_of(changelog)
    if any(section.version == version for section in parsed):
        raise VersionError(f"{CHANGELOG} already has a section for {version}")
    unreleased = [section for section in parsed if section.title == f"[{UNRELEASED}]"]
    if len(unreleased) != 1 or not unreleased[0].body.strip():
        raise VersionError(
            f"{CHANGELOG} has nothing under `## [{UNRELEASED}]`. A release documents what "
            f"is in it; write the section first."
        )

    today = datetime.now(UTC).date()
    written = {
        INIT_PY: (init_py, release_init_py(init_py, version=version)),
        CHANGELOG: (
            changelog,
            release_changelog(changelog, version=version, previous=current, today=today, source=source),
        ),
        UPGRADING: (upgrading, release_upgrading(upgrading, version=version, previous=current)),
    }

    # The chore checks its own work with the same guard CI runs. A rewrite that does not
    # validate is a bug here, not something to hand to a reviewer.
    problems = audit(
        changelog=written[CHANGELOG][1],
        upgrading=written[UPGRADING][1],
        init_py=written[INIT_PY][1],
        source=source,
    )
    if problems:
        raise VersionError(
            "the rewrite does not pass `check`, which is a bug in this script:\n  " + "\n  ".join(problems)
        )

    if args.check:
        for name, (before, after) in written.items():
            if before != after:
                print(unified(before, after, name), end="")
        sys.stdout.flush()
        print(
            f"would release {version} ({today.isoformat()}), from {current}, "
            f"and rebuild the generated documentation",
            file=sys.stderr,
        )
        return 0

    for name, (before, after) in written.items():
        if before != after:
            (REPO / name).write_text(after, encoding="utf-8")
    regenerate_docs()
    unchanged = [name for name, (before, after) in written.items() if before == after]
    print(f"released {version} ({today.isoformat()}), from {current}")
    for name in unchanged:
        print(f"  {name} needed no change")
    print("  generated documentation rebuilt")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    checker = sub.add_parser("check", help="the version, the CHANGELOG and the upgrading notes agree")
    checker.add_argument(
        "--strict",
        action="store_true",
        help="also require a git tag for every released section",
    )
    checker.add_argument(
        "--tag",
        metavar="vX.Y.Z",
        help="the tag being released; refuse if it is not the newest one",
    )
    checker.set_defaults(run=run_check)

    later = sub.add_parser("next", help="the version the commits since the newest tag ask for")
    later.set_defaults(run=run_next)

    setter = sub.add_parser("set", help="perform the release chore")
    setter.add_argument("version", metavar="X.Y.Z|auto|major|minor|patch")
    setter.add_argument("--check", action="store_true", help="print the diff, write nothing")
    setter.set_defaults(run=run_set)

    args = parser.parse_args()
    try:
        return args.run(args)
    except VersionError as refused:
        print(refused, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
