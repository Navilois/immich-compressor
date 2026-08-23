"""The version guard: `__version__`, the CHANGELOG and the upgrading notes agree.

Every case here is a way the release chore has gone wrong or could go wrong. The one that
already happened has its own test: `test_upgrading_heading_outliving_its_release`.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Sequence
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


def test_missing_tags_reports_what_was_never_pushed() -> None:
    """v1.0.0 and v1.1.0 date from when this repository was private."""
    assert version_script.missing_tags(["1.2.0", "1.1.1", "1.1.0", "1.0.0"]) == ["v1.1.0", "v1.0.0"]


def test_missing_tags_is_quiet_when_they_all_exist() -> None:
    assert version_script.missing_tags(["1.2.0", "1.1.1"]) == []
