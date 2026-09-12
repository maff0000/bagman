"""Shared fixtures for the BAGMAN security test suite (PID.md §15).

All tests in this package are deterministic and operate against the actual
git-tracked state of the repository they run in (via `git ls-files`), not
against a hardcoded file listing. This means the suite proves properties
about whatever is *actually committed*, at any point in the repository's
life — which is the point of a hygiene/security gate.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Absolute path to the repository root the test suite is running in.

    Resolved via `git rev-parse --show-toplevel` rather than assumed from
    `__file__`, so this works correctly whether the suite is run from a
    clone, a worktree, or CI's checkout.
    """
    here = Path(__file__).resolve().parent
    top = _git("rev-parse", "--show-toplevel", cwd=here).strip()
    return Path(top)


@pytest.fixture(scope="session")
def tracked_files(repo_root: Path) -> list[str]:
    """All file paths git currently tracks (i.e. `git ls-files`), relative
    to `repo_root`. This is the authoritative "what is actually committed"
    view — it does not care what merely exists on disk (ignored/untracked
    files are irrelevant to a hygiene test).
    """
    output = _git("ls-files", cwd=repo_root)
    return [line for line in output.splitlines() if line]
