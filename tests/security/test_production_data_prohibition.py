"""Test C — production evidence prohibition (PID.md §15, Test C / §8).

Verifies the known production-data directory names (`runtime/`, `data/`,
`backups/`) are:

1. actually excluded via `.gitignore` (git would refuse to track a new
   file dropped into them), and
2. not currently tracked by git.
"""
from __future__ import annotations

import subprocess
from pathlib import PurePosixPath

import pytest

PRODUCTION_DATA_DIRECTORIES = ["runtime", "data", "backups"]


def test_no_production_data_directories_are_currently_tracked(tracked_files):
    violations = [
        path
        for path in tracked_files
        if PurePosixPath(path).parts
        and PurePosixPath(path).parts[0] in PRODUCTION_DATA_DIRECTORIES
    ]
    assert not violations, (
        "Production-data directories must not contain tracked files:\n"
        + "\n".join(f"  - {p}" for p in violations)
    )


@pytest.mark.parametrize("directory", PRODUCTION_DATA_DIRECTORIES)
def test_gitignore_excludes_production_data_directory(repo_root, directory):
    # Probe with `git check-ignore` against a synthetic candidate path.
    # This proves the *effective* ignore behaviour (what `.gitignore`
    # actually does), rather than merely grepping the file's text.
    candidate = f"{directory}/example-generated-file.txt"
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", candidate],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"expected '{candidate}' to be ignored by .gitignore, but "
        f"`git check-ignore` exited {result.returncode} "
        f"(stderr: {result.stderr.strip()!r})"
    )


def test_gitignore_file_contains_expected_patterns(repo_root):
    gitignore_text = (repo_root / ".gitignore").read_text()
    for directory in PRODUCTION_DATA_DIRECTORIES:
        assert f"{directory}/" in gitignore_text, (
            f".gitignore is missing the '{directory}/' pattern required by "
            f"PID.md §9"
        )
