"""Test A — repository hygiene (PID.md §15, Test A).

Fails if a prohibited, obviously secret/runtime filename is *tracked* by
git — i.e. actually committed, not merely present on disk (untracked files
are already the `.gitignore`'s job, and are irrelevant here).

This test is deliberately independent of `.gitignore` (that is Test C's
job for the production-data directories): it inspects `git ls-files`
directly, so it still catches a violation even if someone weakens or
removes an ignore rule.
"""
from __future__ import annotations

import fnmatch
from pathlib import PurePosixPath

# Filename glob patterns that must never be tracked, anywhere in the tree.
# Matched against the file's basename unless the pattern contains a "/".
PROHIBITED_FILENAME_GLOBS = [
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.secret",
    "credentials.json",
    "token.json",
    "*.sqlite",
    "*.sqlite3",
    "*.db",
]

# Explicit exceptions: filenames that would otherwise match a prohibited
# glob but are known-safe committed templates/examples.
ALLOWED_EXCEPTIONS = {
    ".env.example",
}

# Directory names that must never contain tracked files, at any depth.
PROHIBITED_DIRECTORY_NAMES = {
    "secrets",
    "runtime",
    "data",
    "backups",
}


def _violation_reason(path: str) -> str | None:
    """Return a human-readable reason `path` is prohibited, or None if it
    is fine. `path` is a repo-relative, forward-slash-separated path as
    returned by `git ls-files`.
    """
    if path in ALLOWED_EXCEPTIONS:
        return None

    parts = PurePosixPath(path).parts
    basename = parts[-1] if parts else path

    for directory in parts[:-1]:
        if directory in PROHIBITED_DIRECTORY_NAMES:
            return (
                f"tracked under prohibited directory '{directory}/' "
                f"(production/runtime/secret data must never be committed)"
            )

    for glob in PROHIBITED_FILENAME_GLOBS:
        if fnmatch.fnmatch(basename, glob):
            return f"filename matches prohibited pattern '{glob}'"

    return None


def test_no_prohibited_secret_or_runtime_filenames_are_tracked(tracked_files):
    violations = {}
    for path in tracked_files:
        reason = _violation_reason(path)
        if reason is not None:
            violations[path] = reason

    assert not violations, (
        "Prohibited secret/runtime filenames are tracked by git:\n"
        + "\n".join(f"  - {path}: {reason}" for path, reason in sorted(violations.items()))
    )


def test_dotenv_example_style_files_are_not_flagged():
    # Sanity check on the matcher itself: known-safe example/template files
    # must not be flagged as violations (guards against an overly broad
    # pattern silently blocking the Layer-2 config contract).
    safe_examples = [
        "config/examples/dev.example.env",
        "config/examples/prod.example.env",
        ".env.example",
    ]
    for path in safe_examples:
        assert _violation_reason(path) is None, f"expected {path} to be allowed"


def test_matcher_flags_known_bad_examples():
    # Sanity check on the matcher itself: obviously-bad paths must be
    # flagged. This does not touch git state; it only proves the matching
    # logic used by the tracked-files test above is not vacuously permissive.
    bad_examples = [
        ".env",
        ".env.local",
        "id_rsa.pem",
        "server.key",
        "credentials.json",
        "token.json",
        "app.sqlite",
        "app.db",
        "secrets/whatever.txt",
        "runtime/state.json",
        "data/export.csv",
        "backups/dump.sql",
    ]
    for path in bad_examples:
        assert _violation_reason(path) is not None, f"expected {path} to be flagged"
