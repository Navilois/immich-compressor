"""The commit convention guard.

The rules come from CLAUDE.md: conventional commits, English, imperative, with scopes
taken from what is already in `git log` rather than invented. Every case here is either a
rule or a way an earlier commit in this repository broke one.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("commits_script", REPO / "scripts" / "check-commits.py")
assert _spec and _spec.loader
commits_script = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = commits_script
_spec.loader.exec_module(commits_script)

SCOPES = {"cli", "ci", "deps", "encoder", "pipeline", "tests"}


def problems(subject: str, *, is_bot: bool = False) -> list[str]:
    return commits_script.problems_in(subject, scopes=SCOPES, is_bot=is_bot)


# --- subjects that are fine ---------------------------------------------------------


@pytest.mark.parametrize(
    "subject",
    [
        "feat(cli): add a jobs subcommand",
        "fix(encoder): measure the capture-date gate against the source",
        "docs: write down the shipping conventions",
        "chore(deps): raise the dependency floors",
        "feat(pipeline)!: drop the v1 marker",
        "fix(cli)!: rename the flag",
        "refactor(tests): extract the payload builder",
        "perf(encoder): skip the second probe",
        "revert: undo the surge breaker",
    ],
)
def test_accepted(subject: str) -> None:
    assert problems(subject) == []


@pytest.mark.parametrize(
    "subject",
    [
        "fix(encoder): GPU-Passthrough into an optional overlay",
        "feat(encoder): JPEG stills, and the savings criterion",
        "fix(pipeline): SQLite journal mode on open",
        "feat(cli): TZ is passed to the container",
        "fix(ci): 3.14 is what the image ships",
    ],
)
def test_an_acronym_or_compound_first_word_is_not_sentence_case(subject: str) -> None:
    """Lowercasing `GPU-Passthrough` would be worse than the rule is worth."""
    assert problems(subject) == []


# --- grammar ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subject",
    [
        "fixed the thing",
        "fix stuff",
        "fix:",
        "fix: ",
        "",
        "Merge pull request #19 from Navilois/x",
        "FEAT(cli): shout",
    ],
)
def test_not_a_conventional_subject(subject: str) -> None:
    assert problems(subject) == ["not a conventional commit subject: expected `type(scope): summary`"]


@pytest.mark.parametrize("kind", ["bugfix", "feature", "hotfix", "wip", "update"])
def test_unknown_type(kind: str) -> None:
    assert any("is not a commit type" in problem for problem in problems(f"{kind}(cli): a thing"))


def test_unknown_scope() -> None:
    """The scope list is the rule: invention is what this catches."""
    assert problems("fix(worker): a thing") == [
        "`worker` is not a known scope; add it to scripts/commit-scopes.txt if it is real"
    ]


def test_the_scope_split_this_guard_exists_for() -> None:
    """`fix(test)` and `fix(tests)` are both in the history and are the same scope."""
    assert problems("fix(tests): a thing") == []
    assert any("`test` is not a known scope" in problem for problem in problems("fix(test): a thing"))


def test_empty_scope() -> None:
    assert problems("fix(): a thing") == ["empty scope: write `type: summary` when there is no scope"]


# --- prose --------------------------------------------------------------------------


def test_trailing_full_stop() -> None:
    assert problems("fix(cli): a thing.") == ["subject ends with a full stop"]


@pytest.mark.parametrize(
    "subject",
    [
        "chore(deps): Record why the compose files are not an ecosystem",
        "chore(ci): Test on Python 3.14",
        "feat(pipeline): Verworfene Originale entfernen",
    ],
)
def test_sentence_case(subject: str) -> None:
    """Four commits in this repository's history open with a capitalised ordinary word."""
    assert problems(subject) == ["subject is sentence case; the convention is imperative, lower case"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("Record why", True, id="plain-word"),
        pytest.param("record why", False, id="already-lower"),
        pytest.param("GPU-Passthrough", False, id="hyphenated-acronym"),
        pytest.param("JPEG stills", False, id="acronym"),
        pytest.param("SQLite journal", False, id="internal-capital"),
        pytest.param("3.14 is the floor", False, id="digits"),
        pytest.param("", False, id="empty"),
    ],
)
def test_is_sentence_case(text: str, expected: bool) -> None:
    assert commits_script.is_sentence_case(text) is expected


def test_length_limit() -> None:
    at_limit = "fix(cli): " + "a" * (commits_script.MAX_SUBJECT - len("fix(cli): "))
    assert len(at_limit) == commits_script.MAX_SUBJECT
    assert problems(at_limit) == []
    assert problems(at_limit + "a") == [
        f"subject is {commits_script.MAX_SUBJECT + 1} characters, the limit is {commits_script.MAX_SUBJECT}"
    ]


# --- bots ---------------------------------------------------------------------------


def test_dependabot_may_capitalise() -> None:
    """`chore(deps): Bump …` is what Dependabot writes, and rewriting it every week
    would cost a merge conflict per dependency update."""
    assert problems("chore(deps): Bump pydantic from 2.13 to 2.14", is_bot=True) == []


def test_a_bot_is_still_held_to_the_grammar() -> None:
    assert any(
        "is not a known scope" in problem
        for problem in problems("chore(vendor): Bump something", is_bot=True)
    )


# --- the scope list itself ----------------------------------------------------------


def test_the_scope_file_parses() -> None:
    scopes = commits_script.known_scopes()
    assert "backfill" in scopes
    assert "encoder" in scopes
    assert not any(scope.startswith("#") for scope in scopes)


def test_the_scope_file_is_sorted_and_unique() -> None:
    """It is read by people as often as by this script."""
    lines = (REPO / commits_script.SCOPES_FILE).read_text(encoding="utf-8").splitlines()
    listed = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    assert listed == sorted(listed)
    assert len(listed) == len(set(listed))


def test_the_singular_test_scope_is_deliberately_absent() -> None:
    assert "tests" in commits_script.known_scopes()
    assert "test" not in commits_script.known_scopes()
