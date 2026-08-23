"""The version guard: `__version__`, the CHANGELOG and the upgrading notes agree.

Every case here is a way the release chore has gone wrong or could go wrong. The one that
already happened has its own test: `test_upgrading_heading_outliving_its_release`.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# scripts/ is not a package and is not meant to become one — it is loaded the way a
# script is, by path. The sys.modules line is not optional: @dataclass resolves its
# annotations through sys.modules[cls.__module__], which is None for a module that has
# been created but not registered.
_spec = importlib.util.spec_from_file_location("version_script", REPO / "scripts" / "version.py")
assert _spec and _spec.loader
version_script = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = version_script
_spec.loader.exec_module(version_script)

SOURCE = "https://github.com/Navilois/immich-compressor"
DEFAULT_SECTIONS = (("1.2.0", "2026-08-21"), ("1.1.0", "2026-08-19"))
FILLED = "\n### Added\n\n- A new thing.\n"
EMPTY = "\n"


def default_refs(sections: Sequence[tuple[str, str]]) -> str:
    versions = [version for version, _ in sections]
    lines = [f"[Unreleased]: {SOURCE}/compare/v{versions[0]}...HEAD"]
    for index, version in enumerate(versions):
        if index + 1 < len(versions):
            lines.append(f"[{version}]: {SOURCE}/compare/v{versions[index + 1]}...v{version}")
        else:
            lines.append(f"[{version}]: {SOURCE}/releases/tag/v{version}")
    return "\n".join(lines) + "\n"


def build_changelog(
    *,
    unreleased: str = FILLED,
    sections: Sequence[tuple[str, str]] = DEFAULT_SECTIONS,
    refs: str | None = None,
    preamble: str = "# Changelog\n\nAll notable changes are documented in this file.\n",
) -> str:
    parts = [preamble, "\n## [Unreleased]\n", unreleased]
    for version, released_on in sections:
        dated = f"] - {released_on}" if released_on else "]"
        parts.append(f"\n## [{version}{dated}\n\n### Fixed\n\n- Something else.\n")
    parts.append("\n" + (default_refs(sections) if refs is None else refs))
    return "".join(parts)


def audit(
    *,
    changelog: str | None = None,
    upgrading: str = "# Upgrading\n\n## 1.1.0 → 1.2.0\n\nNothing to do.\n",
    version: str = "1.2.0",
) -> list[str]:
    return version_script.audit(
        changelog=build_changelog() if changelog is None else changelog,
        upgrading=upgrading,
        init_py=f'"""Docstring."""\n\n__version__ = "{version}"\n',
        source=SOURCE,
    )


def test_a_consistent_repository_has_no_problems() -> None:
    assert audit() == []


def test_the_repository_itself_passes() -> None:
    """Not a synthetic case: the files as they are on disk right now."""
    assert (
        version_script.audit(
            changelog=(REPO / version_script.CHANGELOG).read_text(encoding="utf-8"),
            upgrading=(REPO / version_script.UPGRADING).read_text(encoding="utf-8"),
            init_py=(REPO / version_script.INIT_PY).read_text(encoding="utf-8"),
            source=version_script.source_url(),
        )
        == []
    )


def test_source_url_comes_from_pyproject() -> None:
    assert version_script.source_url() == SOURCE


# --- __version__ itself -------------------------------------------------------------


def test_version_behind_the_newest_section() -> None:
    problems = audit(version="1.1.0")
    assert any("__version__ is 1.1.0" in problem and "1.2.0" in problem for problem in problems)


def test_version_ahead_of_the_changelog() -> None:
    """The bump landed without the section: the release notes would be empty."""
    problems = audit(version="1.3.0")
    assert any("__version__ is 1.3.0" in problem for problem in problems)


@pytest.mark.parametrize(
    "init_py",
    [
        pytest.param('"""Docstring."""\n', id="absent"),
        pytest.param('__version__ = "1.2"\n', id="not-semver"),
        pytest.param("__version__ = '1.2.0'\n", id="single-quoted"),
    ],
)
def test_unreadable_version(init_py: str) -> None:
    problems = version_script.audit(
        changelog=build_changelog(), upgrading="# Upgrading\n", init_py=init_py, source=SOURCE
    )
    assert problems == [
        'src/immich_compressor/__init__.py: no `__version__ = "X.Y.Z"` line, or it is not a version'
    ]


def test_a_pre_release_is_a_version() -> None:
    sections = (("1.3.0-rc1", "2026-08-23"), *DEFAULT_SECTIONS)
    assert audit(changelog=build_changelog(sections=sections), version="1.3.0-rc1") == []


# --- the Unreleased section ---------------------------------------------------------


def test_two_unreleased_sections() -> None:
    doubled = build_changelog().replace("## [Unreleased]", "## [Unreleased]\n\n## [Unreleased]", 1)
    problems = audit(changelog=doubled)
    assert any("expected exactly one" in problem for problem in problems)


def test_no_unreleased_section() -> None:
    """What the release chore leaves behind when it renames the heading and stops."""
    without = build_changelog().replace("## [Unreleased]\n", "", 1)
    problems = audit(changelog=without)
    assert any("expected exactly one" in problem and "none found" in problem for problem in problems)


def test_unreleased_below_a_released_section() -> None:
    moved = build_changelog(preamble="# Changelog\n\n## [1.2.0] - 2026-08-21\n\n- Out of place.\n")
    problems = audit(changelog=moved)
    assert any("must be the first section" in problem for problem in problems)


# --- dates and ordering -------------------------------------------------------------


def test_undated_release_section() -> None:
    problems = audit(changelog=build_changelog(sections=(("1.2.0", ""), ("1.1.0", "2026-08-19"))))
    assert any("has no ` - date`" in problem for problem in problems)


def test_date_that_is_not_a_date() -> None:
    problems = audit(changelog=build_changelog(sections=(("1.2.0", "21-08-2026"), ("1.1.0", "2026-08-19"))))
    assert any("is not an ISO-8601 date" in problem for problem in problems)


def test_impossible_date() -> None:
    problems = audit(changelog=build_changelog(sections=(("1.2.0", "2026-02-31"), ("1.1.0", "2026-08-19"))))
    assert any("is not an ISO-8601 date" in problem for problem in problems)


def test_sections_out_of_version_order() -> None:
    sections = (("1.1.0", "2026-08-21"), ("1.2.0", "2026-08-19"))
    problems = version_script.audit(
        changelog=build_changelog(sections=sections),
        upgrading="# Upgrading\n",
        init_py='__version__ = "1.1.0"\n',
        source=SOURCE,
    )
    assert any("sections run newest first" in problem for problem in problems)


def test_a_release_dated_before_the_one_under_it() -> None:
    sections = (("1.2.0", "2026-08-01"), ("1.1.0", "2026-08-19"))
    problems = audit(changelog=build_changelog(sections=sections))
    assert any("is dated before" in problem for problem in problems)


def test_two_releases_on_one_day_are_fine() -> None:
    """1.1.1 and 1.2.0 both went out on 2026-08-21."""
    sections = (("1.2.0", "2026-08-21"), ("1.1.1", "2026-08-21"))
    assert audit(changelog=build_changelog(sections=sections)) == []


# --- link references ----------------------------------------------------------------


def test_missing_link_reference() -> None:
    changelog = build_changelog(refs=f"[Unreleased]: {SOURCE}/compare/v1.2.0...HEAD\n")
    problems = audit(changelog=changelog)
    assert "CHANGELOG.md: `[1.2.0]` has no link reference definition" in problems
    assert "CHANGELOG.md: `[1.1.0]` has no link reference definition" in problems


def test_link_reference_left_on_the_previous_release() -> None:
    """The compare link nobody re-points, which is how v1.1.0's came to 404."""
    stale = default_refs(DEFAULT_SECTIONS).replace(
        f"[Unreleased]: {SOURCE}/compare/v1.2.0...HEAD",
        f"[Unreleased]: {SOURCE}/compare/v1.1.0...HEAD",
    )
    problems = audit(changelog=build_changelog(refs=stale))
    assert any("`[Unreleased]` points at" in problem and "v1.2.0...HEAD" in problem for problem in problems)


def test_oldest_section_points_at_the_tag_not_a_comparison() -> None:
    wrong = default_refs(DEFAULT_SECTIONS).replace(
        f"[1.1.0]: {SOURCE}/releases/tag/v1.1.0",
        f"[1.1.0]: {SOURCE}/compare/v1.0.0...v1.1.0",
    )
    problems = audit(changelog=build_changelog(refs=wrong))
    assert any("releases/tag/v1.1.0" in problem for problem in problems)


def test_link_reference_to_another_repository() -> None:
    forked = default_refs(DEFAULT_SECTIONS).replace(SOURCE, "https://github.com/someone/fork")
    problems = audit(changelog=build_changelog(refs=forked))
    assert len(problems) == 3  # Unreleased and both releases


def test_orphan_link_reference() -> None:
    orphan = default_refs(DEFAULT_SECTIONS) + f"[0.9.0]: {SOURCE}/releases/tag/v0.9.0\n"
    problems = audit(changelog=build_changelog(refs=orphan))
    assert any("`[0.9.0]` is defined but has no section" in problem for problem in problems)


def test_unrelated_link_references_are_left_alone() -> None:
    """The CHANGELOG is prose and may define any link it likes."""
    extra = default_refs(DEFAULT_SECTIONS) + f"[the API notes]: {SOURCE}/blob/main/docs/immich-api-notes.md\n"
    assert audit(changelog=build_changelog(refs=extra)) == []


# --- the upgrading notes ------------------------------------------------------------


def test_upgrading_heading_outliving_its_release() -> None:
    """The bug this guard exists for: 1.1.1 and 1.2.0 both shipped with the upgrading
    notes still calling shipped behaviour `Unreleased`."""
    problems = audit(
        changelog=build_changelog(unreleased=EMPTY),
        upgrading="# Upgrading\n\n## Unreleased\n\nSomething operators must do.\n",
    )
    assert problems == [
        "docs/upgrading.md:3: `## Unreleased` is still here while the CHANGELOG's is empty; "
        "the release chore renames both"
    ]


def test_both_headings_renamed_together() -> None:
    assert (
        audit(
            changelog=build_changelog(unreleased=EMPTY),
            upgrading="# Upgrading\n\n## 1.1.0 → 1.2.0\n\nSomething operators must do.\n",
        )
        == []
    )


def test_unreleased_work_needs_no_upgrading_note() -> None:
    """One-directional on purpose: most changes need nothing from an operator."""
    assert audit(changelog=build_changelog(unreleased=FILLED), upgrading="# Upgrading\n") == []


def test_two_upgrading_unreleased_headings() -> None:
    upgrading = "# Upgrading\n\n## Unreleased\n\nOne.\n\n## Unreleased\n\nTwo.\n"
    problems = audit(upgrading=upgrading)
    assert any("expected at most one" in problem for problem in problems)


# --- tags ---------------------------------------------------------------------------


ALL_TAGS = ["v1.0.0", "v1.1.0", "v1.1.1", "v1.2.0"]


def test_missing_tags_names_what_was_never_pushed() -> None:
    assert version_script.missing_tags(["1.2.0", "1.1.1", "1.1.0"], ["v1.2.0", "v1.1.1"]) == ["v1.1.0"]


def test_missing_tags_is_quiet_when_they_all_exist() -> None:
    assert version_script.missing_tags(["1.2.0", "1.1.1"], ALL_TAGS) == []


# --- publishing an old tag ----------------------------------------------------------


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        pytest.param(ALL_TAGS, "v1.2.0", id="highest-wins"),
        pytest.param(["v1.9.0", "v1.10.0"], "v1.10.0", id="not-lexicographic"),
        pytest.param([*ALL_TAGS, "nightly", "v1"], "v1.2.0", id="ignores-non-versions"),
        pytest.param(["v1.3.0-rc1", "v1.3.0"], "v1.3.0", id="release-beats-its-pre-release"),
        pytest.param(["v1.2.0", "v1.3.0-rc1"], "v1.3.0-rc1", id="pre-release-beats-older-release"),
        pytest.param([], None, id="none-at-all"),
    ],
)
def test_newest_tag(tags: list[str], expected: str | None) -> None:
    assert version_script.newest_tag(tags) == expected


def test_the_newest_tag_may_publish() -> None:
    assert version_script.stale_tag_problems("v1.2.0", ALL_TAGS) == []


def test_a_tag_ahead_of_every_other_may_publish() -> None:
    """The normal case: the tag has just been created and is the highest there is."""
    assert version_script.stale_tag_problems("v1.3.0", ALL_TAGS) == []


def test_republishing_an_old_tag_is_refused() -> None:
    """Exactly what pushing the backfilled v1.1.0 set in motion: `latest` and the `1`
    tag that docker-compose.yaml pins would both have moved back to 1.1.0."""
    problems = version_script.stale_tag_problems("v1.1.0", ALL_TAGS)
    assert problems == [
        "v1.1.0 is not the newest tag (v1.2.0); publishing it would move `latest` and the major tag backwards"
    ]


@pytest.mark.parametrize("tag", ["1.2.0", "v1.2", "latest", "v1.2.0-", ""])
def test_a_tag_that_is_not_a_version_is_refused(tag: str) -> None:
    assert version_script.stale_tag_problems(tag, ALL_TAGS) == [f"{tag} is not a vX.Y.Z tag"]


# --- deriving the next version ------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        pytest.param("feat(cli): add a jobs subcommand", "minor", id="feat"),
        pytest.param("fix(config): reject a bad preset", "patch", id="fix"),
        pytest.param("perf(encoder): skip the second probe", "patch", id="perf"),
        pytest.param("feat!: drop the v1 marker", "major", id="bang"),
        pytest.param("fix(api)!: rename the field", "major", id="scoped-bang"),
        pytest.param("feat: x\n\nBREAKING CHANGE: the key moved", "major", id="footer"),
        pytest.param("feat: x\n\nBREAKING-CHANGE: the key moved", "major", id="footer-hyphen"),
        pytest.param("docs: write down the shipping conventions", None, id="docs"),
        pytest.param("chore(ci): bump the actions", None, id="chore"),
        pytest.param("refactor(store): extract a helper", None, id="refactor"),
        pytest.param("test(pipeline): cover the sweeper", None, id="test"),
        pytest.param("Merge pull request #19 from Navilois/x", None, id="merge-subject"),
        pytest.param("fixed the thing", None, id="not-conventional"),
        pytest.param("fix stuff", None, id="no-colon"),
        pytest.param("", None, id="empty"),
    ],
)
def test_bump_of(message: str, expected: str | None) -> None:
    assert version_script.bump_of(message) == expected


def test_dependabot_commits_do_not_release() -> None:
    """`chore(deps)` is the prefix .github/dependabot.yml is configured to use."""
    assert version_script.bump_of("chore(deps): Bump pydantic from 2.13 to 2.14") is None


def test_a_breaking_body_needs_the_footer_not_the_words() -> None:
    """Prose about a breaking change is not a footer, and must not silently major-bump."""
    assert version_script.bump_of("fix: x\n\nThis is a BREAKING CHANGE for anyone who...") == "patch"


@pytest.mark.parametrize(
    ("messages", "expected"),
    [
        pytest.param(["docs: a", "fix: b", "feat: c"], "minor", id="feat-wins-over-fix"),
        pytest.param(["fix: a", "feat!: b"], "major", id="breaking-wins"),
        pytest.param(["fix: a", "perf: b"], "patch", id="both-patch"),
        pytest.param(["docs: a", "chore: b"], None, id="nothing-releasable"),
        pytest.param([], None, id="no-commits"),
    ],
)
def test_bump_from_commits(messages: list[str], expected: str | None) -> None:
    assert version_script.bump_from_commits(messages)[0] == expected


def test_only_releasing_commits_are_given_as_reasons() -> None:
    _, reasons = version_script.bump_from_commits(["docs: a", "fix: b", "chore: c", "feat: d"])
    assert reasons == [("patch", "fix: b"), ("minor", "feat: d")]


@pytest.mark.parametrize(
    ("current", "bump", "expected"),
    [
        pytest.param("1.2.0", "minor", "1.3.0", id="minor"),
        pytest.param("1.2.0", "major", "2.0.0", id="major"),
        pytest.param("1.2.3", "patch", "1.2.4", id="patch"),
        pytest.param("1.2.3", "minor", "1.3.0", id="minor-resets-patch"),
        pytest.param("1.2.3", "major", "2.0.0", id="major-resets-both"),
        pytest.param("1.3.0-rc1", "patch", "1.3.1", id="from-a-pre-release"),
        pytest.param("0.9.0", "minor", "0.10.0", id="ten-not-one"),
    ],
)
def test_next_version(current: str, bump: str, expected: str) -> None:
    assert version_script.next_version(current, bump) == expected


# --- performing the release chore ---------------------------------------------------

TODAY = date(2026, 8, 23)


def release(
    changelog: str, upgrading: str, init_py: str, version: str, previous: str
) -> tuple[str, str, str]:
    return (
        version_script.release_changelog(
            changelog, version=version, previous=previous, today=TODAY, source=SOURCE
        ),
        version_script.release_upgrading(upgrading, version=version, previous=previous),
        version_script.release_init_py(init_py, version=version),
    )


@pytest.mark.parametrize("version", ["1.2.1", "1.3.0", "2.0.0"])
def test_the_chore_produces_something_check_accepts(version: str) -> None:
    """The property that matters: whatever `set` writes, `check` has to pass on it."""
    changelog, upgrading, init_py = release(
        build_changelog(),
        "# Upgrading\n\n## Unreleased\n\nSomething operators must do.\n",
        '__version__ = "1.2.0"\n',
        version,
        "1.2.0",
    )
    assert (
        version_script.audit(changelog=changelog, upgrading=upgrading, init_py=init_py, source=SOURCE) == []
    )


def test_the_chore_dates_the_section_and_opens_an_empty_one() -> None:
    changelog = version_script.release_changelog(
        build_changelog(), version="1.3.0", previous="1.2.0", today=TODAY, source=SOURCE
    )
    assert "## [Unreleased]\n\n## [1.3.0] - 2026-08-23\n" in changelog
    sections = version_script.sections_of(changelog)
    assert sections[0].title == "[Unreleased]"
    assert not sections[0].body.strip()
    assert sections[1].version == "1.3.0"


def test_the_chore_moves_the_links() -> None:
    changelog = version_script.release_changelog(
        build_changelog(), version="1.3.0", previous="1.2.0", today=TODAY, source=SOURCE
    )
    assert f"[Unreleased]: {SOURCE}/compare/v1.3.0...HEAD" in changelog
    assert f"[1.3.0]: {SOURCE}/compare/v1.2.0...v1.3.0" in changelog
    assert f"[Unreleased]: {SOURCE}/compare/v1.2.0...HEAD" not in changelog


def test_the_chore_refuses_a_changelog_with_no_unreleased_heading() -> None:
    without = build_changelog().replace("## [Unreleased]\n", "", 1)
    with pytest.raises(version_script.VersionError, match="no `## \\[Unreleased\\]` heading"):
        version_script.release_changelog(
            without, version="1.3.0", previous="1.2.0", today=TODAY, source=SOURCE
        )


def test_the_chore_refuses_when_the_link_is_not_where_it_should_be() -> None:
    """A hand-edited link means the file is not in the shape this rewrite assumes."""
    stale = build_changelog().replace(
        f"[Unreleased]: {SOURCE}/compare/v1.2.0...HEAD", "[Unreleased]: elsewhere"
    )
    with pytest.raises(version_script.VersionError, match="line to move"):
        version_script.release_changelog(stale, version="1.3.0", previous="1.2.0", today=TODAY, source=SOURCE)


def test_the_chore_renames_the_upgrading_heading() -> None:
    upgrading = version_script.release_upgrading(
        "# Upgrading\n\n## Unreleased\n\nDo the thing.\n", version="1.3.0", previous="1.2.0"
    )
    assert "## 1.2.0 → 1.3.0" in upgrading
    assert "## Unreleased" not in upgrading


def test_upgrading_notes_without_a_heading_are_left_alone() -> None:
    before = "# Upgrading\n\n## 1.1.0 → 1.2.0\n\nOld news.\n"
    assert version_script.release_upgrading(before, version="1.3.0", previous="1.2.0") == before


def test_the_chore_only_touches_the_first_unreleased_heading() -> None:
    """`## Unreleased` inside a code block or a quoted example is not the heading."""
    upgrading = version_script.release_upgrading(
        "# Upgrading\n\n## Unreleased\n\nText.\n\n## 1.0.0 → 1.1.0\n\nOlder.\n",
        version="1.3.0",
        previous="1.2.0",
    )
    assert upgrading.count("## 1.2.0 → 1.3.0") == 1
    assert "## 1.0.0 → 1.1.0" in upgrading


def test_the_version_line_is_rewritten_once() -> None:
    init_py = version_script.release_init_py(
        '"""Doc."""\n\n__version__ = "1.2.0"\n\nMARKER = \'__version__ = "1.2.0"\'\n',
        version="1.3.0",
    )
    assert '__version__ = "1.3.0"' in init_py
    assert init_py.count('"1.3.0"') == 1
