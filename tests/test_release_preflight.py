"""The release workflow's preflight, which once blocked a release that would have worked.

`release-prepare.yml` opens with a step that reads one repository setting and refuses to go
on when it is off. On 2026-08-27 it refused while the setting was **on** — the read did not
answer `true`, and everything that was not `true` was treated as proof of `false`. 1.4.0 was
released by hand because of it.

The shell is lifted out of the workflow file and run against a stub `gh`, so the branch that
matters is exercised here rather than discovered on the next release.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release-prepare.yml"

# Absolute, because the step under test is shell and ruff is right that resolving a command
# through PATH is how a test picks up something other than what it meant to run.
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not installed")


def _preflight() -> str:
    """The `run:` block of the permission check, straight out of the workflow file."""
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in document["jobs"]["prepare"]["steps"]:
        if str(step.get("name", "")).startswith("GitHub Actions is allowed"):
            return str(step["run"])
    raise AssertionError(f"no permission-check step in {WORKFLOW}")


def _run(tmp_path: Path, *, stdout: str = "", stderr: str = "", exit_code: int = 0):
    """Run the preflight with a `gh` that answers exactly what a test says it does."""
    stub = tmp_path / "gh"
    # shlex.quote, not repr: Python renders a newline as the two characters \\n, which a
    # shell would print literally — and then `false\\n` never equals `false` and the guard
    # silently stops guarding. Caught by this file's own first run.
    stub.write_text(
        "#!/bin/sh\n"
        f"printf '%s' {shlex.quote(stdout)}\n"
        f"printf '%s' {shlex.quote(stderr)} >&2\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    # `-e` because that is the shell GitHub runs a `run:` block with, and it is the reason
    # the failing read has to be caught rather than left to fall through.
    return subprocess.run(  # noqa: S603 - the argv is this repository's own workflow file
        [str(BASH), "-e", "-c", _preflight()],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_REPOSITORY": "owner/repo",
            "GH_TOKEN": "not-a-token",
        },
    )


def test_the_setting_being_on_lets_the_release_run(tmp_path: Path) -> None:
    result = _run(tmp_path, stdout="true\n")
    assert result.returncode == 0
    assert "reads as 'true'" in result.stdout


def test_the_setting_being_off_stops_the_release(tmp_path: Path) -> None:
    """The one answer that is evidence, and the whole reason the step exists."""
    result = _run(tmp_path, stdout="false\n")
    assert result.returncode == 1
    assert "::error::" in result.stdout
    assert "create and approve pull requests" in result.stdout


@pytest.mark.parametrize(
    ("stdout", "stderr", "exit_code", "why"),
    [
        # The key is absent from the answer: `--jq` prints the JSON null, which is what a
        # narrowed token gets back and is not a `false`.
        ("null\n", "", 0, "an absent key"),
        ("", "", 0, "an empty answer"),
        # The real one, and the only one that happens in this repository: `GITHUB_TOKEN`
        # has no `administration: read`, so the endpoint answers 403 every time. Measured
        # on run 33095211048. `gh` prints the body on stdout and its own line on stderr,
        # which is how the old step ended up with both the body and the word `unknown`.
        (
            '{"message":"Resource not accessible by integration","status":"403"}',
            "gh: Resource not accessible by integration (HTTP 403)\n",
            1,
            "a refused read",
        ),
    ],
)
def test_anything_that_is_not_false_continues(
    tmp_path: Path, stdout: str, stderr: str, exit_code: int, why: str
) -> None:
    """None of these say the setting is off, and the old step stopped the release on all
    three. Continuing costs a run that fails later with the real reason; refusing costs a
    release."""
    result = _run(tmp_path, stdout=stdout, stderr=stderr, exit_code=exit_code)
    assert result.returncode == 0, f"{why} must not stop the release"
    assert "::error::" not in result.stdout


def test_the_step_says_what_it_read(tmp_path: Path) -> None:
    """The old step printed nothing, so a false negative could not be told from a real one."""
    refused = _run(tmp_path, stderr="gh: Resource not accessible by integration (HTTP 403)\n", exit_code=1)
    assert "could not read the setting" in refused.stdout
    assert "Resource not accessible by integration" in refused.stdout
