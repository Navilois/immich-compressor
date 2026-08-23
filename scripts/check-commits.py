#!/usr/bin/env python3
"""Check that commit subjects follow the convention this repository documents.

CLAUDE.md asks for conventional commits, English, imperative, with scopes taken from what
is already in `git log` rather than invented. None of that was checked, and the drift is
visible: `fix(test)` and `fix(tests)` are both in the history, and they are the same scope.

Because this repository merges rather than squashes, the individual subjects are what
land on `main` — so the commits are what this checks, not the pull request title.

    python scripts/check-commits.py                    # origin/main..HEAD
    python scripts/check-commits.py v1.2.0..HEAD       # any range
    python scripts/check-commits.py --message FILE     # one message, for the hook

The hook mode is the point. A subject that only fails in CI can be fixed only by
rewriting a branch that has already been pushed, and this project does not force-push, so
the check has to be able to run before the commit exists:

    ln -s ../../scripts/check-commits.py .git/hooks/commit-msg

`make hooks` does that. Commits authored by a bot are exempt from the prose rules and not
from the grammar: Dependabot writes `chore(deps): Bump …`, which is capitalised, and
rewriting its subjects is not worth a merge conflict on every dependency update.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCOPES_FILE = "scripts/commit-scopes.txt"

# The Conventional Commits set. This is deliberately the standard list rather than only
# the types already used: a first `refactor:` is legitimate, a `bugfix:` is a typo.
TYPES = frozenset(
    {"build", "chore", "ci", "docs", "feat", "fix", "perf", "refactor", "revert", "style", "test"}
)

SUBJECT_RE = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[^()]*)\))?(?P<breaking>!)?: (?P<text>.+)$")

# 80, not the 72 the git convention suggests: the tenth percentile of this repository's
# history is already 73, and a rule most of the existing commits break is not this
# repository's rule. It catches a runaway subject, which is what it is for.
MAX_SUBJECT = 80


def known_scopes() -> set[str]:
    lines = (REPO / SCOPES_FILE).read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}


def problems_in(subject: str, *, scopes: set[str], is_bot: bool = False) -> list[str]:
    """Everything wrong with one commit subject."""
    parsed = SUBJECT_RE.match(subject)
    if parsed is None:
        return ["not a conventional commit subject: expected `type(scope): summary`"]

    found: list[str] = []
    if parsed.group("type") not in TYPES:
        found.append(f"`{parsed.group('type')}` is not a commit type ({', '.join(sorted(TYPES))})")

    scope = parsed.group("scope")
    if scope is not None:
        if not scope:
            found.append("empty scope: write `type: summary` when there is no scope")
        elif scope not in scopes:
            found.append(f"`{scope}` is not a known scope; add it to {SCOPES_FILE} if it is real")

    text = parsed.group("text")
    if len(subject) > MAX_SUBJECT:
        found.append(f"subject is {len(subject)} characters, the limit is {MAX_SUBJECT}")
    if text.endswith("."):
        found.append("subject ends with a full stop")
    if not is_bot and is_sentence_case(text):
        found.append("subject is sentence case; the convention is imperative, lower case")
    return found


def is_sentence_case(text: str) -> bool:
    """Whether the subject opens with an ordinary capitalised word.

    Only a plain `Word` counts. A first word carrying an acronym, a digit or a hyphen is
    left alone, because `GPU-Passthrough`, `JPEG-Kompression` and `SQLite` are spelled the
    way they are spelled — lowercasing them would be worse than the rule is worth. What
    remains is the real case: `Record why…`, `Raise the floors…`, `Test on Python 3.14`.
    """
    first = text.split(maxsplit=1)[0] if text.split() else ""
    return first.isalpha() and first[:1].isupper() and first[1:].islower()


def commits_in(span: str) -> list[tuple[str, str, str]]:
    """(short hash, author, subject) for every non-merge commit in the range."""
    if not re.fullmatch(r"[A-Za-z0-9_./^~-]*\.\.[A-Za-z0-9_./^~-]+", span):
        print(f"{span} is not a revision range", file=sys.stderr)
        raise SystemExit(2)
    listed = subprocess.run(  # noqa: S603
        ["git", "log", "--no-merges", "--format=%h%x1f%an%x1f%s", span],  # noqa: S607
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if listed.returncode != 0:
        print(f"git cannot read {span}: {listed.stderr.strip()}", file=sys.stderr)
        raise SystemExit(2)
    found = []
    for line in listed.stdout.splitlines():
        if line:
            short, author, subject = line.split("\x1f", 2)
            found.append((short, author, subject))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("range", nargs="?", default="origin/main..HEAD", help="revision range")
    parser.add_argument("--message", metavar="FILE", help="check one commit message file")
    args = parser.parse_args()

    scopes = known_scopes()
    if not scopes:
        print(f"{SCOPES_FILE} lists no scopes", file=sys.stderr)
        return 1

    if args.message:
        # A commit-msg hook sees the file before the commit exists. Comment lines are
        # git's own template and are stripped before the message is stored.
        body = Path(args.message).read_text(encoding="utf-8")
        lines = [line for line in body.splitlines() if not line.startswith("#")]
        subject = next((line for line in lines if line.strip()), "")
        problems = problems_in(subject, scopes=scopes)
        if problems:
            print(f"commit refused: {subject}", file=sys.stderr)
            for problem in problems:
                print(f"  {problem}", file=sys.stderr)
            return 1
        return 0

    commits = commits_in(args.range)
    refused = [
        (short, subject, problems)
        for short, author, subject in commits
        if (problems := problems_in(subject, scopes=scopes, is_bot="[bot]" in author))
    ]
    if refused:
        print(f"{len(refused)} commit(s) do not follow the convention:")
        for short, subject, problems in refused:
            print(f"  {short} {subject}")
            for problem in problems:
                print(f"      {problem}")
        return 1

    print(f"commits ok: {len(commits)} checked in {args.range}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
