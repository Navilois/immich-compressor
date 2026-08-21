#!/usr/bin/env python3
"""Check that every internal Markdown link and anchor in the repository resolves.

Dead links are the fastest way for documentation to stop being trusted, and they are
exactly the thing nobody notices by reading. External URLs are not fetched — this is a
structural check that runs offline and in CI:

* a link to a file must point at a file that exists;
* a link to `page.md#anchor` must point at a heading that actually exists in that page,
  slugified the way GitHub does it;
* `#anchor` on its own must exist in the same file.

    python scripts/check-links.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# [text](target) — but not ![image](...), and not a reference-style definition.
LINK_RE = re.compile(r"(?<!\!)\[(?:[^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$", re.MULTILINE)
# Fenced code blocks hold example links that are not this repository's problem.
FENCE_RE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
HTML_ANCHOR_RE = re.compile(r'<a\s+(?:id|name)="([^"]+)"', re.IGNORECASE)


def slugify(heading: str) -> str:
    """GitHub's heading-to-anchor rule, as far as this project needs it.

    Note the last step: each individual space becomes a hyphen, and runs are *not*
    collapsed. "Stage 3 — move originals" loses the em dash and keeps both surrounding
    spaces, so the anchor is `stage-3--move-originals` with two hyphens. Collapsing them
    here would make this checker pass links that GitHub then renders as dead.
    """
    text = re.sub(r"`([^`]*)`", r"\1", heading)  # inline code keeps its text
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links keep their text
    text = re.sub(r"[*_~]", "", text)  # emphasis markers vanish
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)  # punctuation vanishes
    return re.sub(r"\s", "-", text)


def anchors_of(path: Path) -> set[str]:
    body = path.read_text(encoding="utf-8")
    found = {slugify(match.group(2)) for match in HEADING_RE.finditer(body)}
    found |= set(HTML_ANCHOR_RE.findall(body))
    return found


def tracked_markdown() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "*.md"],  # noqa: S607
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    return [REPO / line for line in listed.stdout.split() if line]


def main() -> int:
    pages = tracked_markdown()
    anchors = {page: anchors_of(page) for page in pages}
    problems: list[str] = []

    for page in pages:
        body = FENCE_RE.sub("", page.read_text(encoding="utf-8"))
        for match in LINK_RE.finditer(body):
            target = match.group(1)
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            where = f"{page.relative_to(REPO)}"

            path_part, _, anchor = target.partition("#")
            if not path_part:
                if anchor and anchor not in anchors[page]:
                    problems.append(f"{where}: #{anchor} does not exist in this file")
                continue

            resolved = (page.parent / path_part).resolve()
            if not resolved.exists():
                problems.append(f"{where}: {target} -> {path_part} does not exist")
                continue
            if anchor and resolved.suffix == ".md":
                known = anchors.get(resolved) or anchors_of(resolved)
                if anchor not in known:
                    problems.append(f"{where}: {target} -> no such heading in {path_part}")

    if problems:
        print(f"{len(problems)} broken link(s):")
        for problem in problems:
            print(f"  {problem}")
        return 1

    total = sum(len(LINK_RE.findall(page.read_text(encoding="utf-8"))) for page in pages)
    print(f"links ok: {len(pages)} file(s), {total} link(s) checked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
