"""Test B — configuration examples (PID.md §15, Test B / §5 / §9).

Verifies every committed configuration file under `config/` contains only
secret *references* (`credential_ref: ...`, `<SET_VIA_...>`-style
placeholders, `*_FILE=/run/secrets/...` patterns) and never a value that
looks like an actual credential.

Two file shapes are checked, matching what's actually under `config/`:

- YAML files (`config/base/*.yaml`) — walked recursively; any key that
  looks credential-shaped must hold a reference/placeholder, not a value.
- `.env`-style files (`config/examples/*.env`) — parsed as `KEY=VALUE`
  lines; any key containing PASSWORD/TOKEN/SECRET must point at a mounted
  secret file (`/run/secrets/...`) or an obvious placeholder, never a
  literal value.
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

import pytest
import yaml

# Key name fragments that mark a value as "credential-shaped" and therefore
# subject to the reference-only rule. `credential_ref` itself is exempt: it
# is the reference mechanism, not a secret.
CREDENTIAL_KEY_PATTERN = re.compile(r"(password|_token|_secret|secret_|api_key)", re.IGNORECASE)
REF_KEY_EXEMPTIONS = {"credential_ref"}

# A value is an "obvious placeholder" if it looks like one of these.
PLACEHOLDER_VALUE_RE = re.compile(r"^<[A-Z0-9_]+>$")
SECRET_FILE_MOUNT_PREFIX = "/run/secrets/"


def _is_safe_credential_value(value: str) -> bool:
    if not isinstance(value, str):
        return False
    if value.startswith(SECRET_FILE_MOUNT_PREFIX):
        return True
    if PLACEHOLDER_VALUE_RE.match(value):
        return True
    return False


def _walk_yaml(node, path=()):
    """Yield (dotted_path, key, value) for every scalar leaf in a parsed
    YAML structure."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk_yaml(value, path + (str(key),))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk_yaml(value, path + (str(index),))
    else:
        if path:
            yield (".".join(path), path[-1], node)


def _check_yaml_config(text: str, label: str) -> list[str]:
    violations = []
    parsed = yaml.safe_load(text)
    for dotted_path, key, value in _walk_yaml(parsed):
        if key in REF_KEY_EXEMPTIONS:
            # credential_ref must hold a reference NAME, not a secret-like
            # value (e.g. not something containing spaces, "://", or long
            # random-looking content) — it should read as an identifier.
            if not isinstance(value, str) or not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", value):
                violations.append(
                    f"{label}: {dotted_path} = {value!r} is not a plain "
                    f"identifier-style credential_ref name"
                )
            continue
        if CREDENTIAL_KEY_PATTERN.search(key):
            if not _is_safe_credential_value(value):
                violations.append(
                    f"{label}: {dotted_path} = {value!r} looks like a "
                    f"credential-shaped key with a non-reference value"
                )
    return violations


ENV_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _check_env_config(text: str, label: str) -> list[str]:
    violations = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = ENV_LINE_RE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if CREDENTIAL_KEY_PATTERN.search(key):
            if not _is_safe_credential_value(value):
                violations.append(
                    f"{label}:{lineno}: {key}={value!r} is credential-shaped "
                    f"but not a /run/secrets/ reference or placeholder"
                )
    return violations


def _config_files(repo_root: Path, tracked_files: list[str]):
    for rel_path in tracked_files:
        parts = PurePosixPath(rel_path).parts
        if not parts or parts[0] != "config":
            continue
        if rel_path.endswith((".yaml", ".yml", ".env")):
            yield rel_path


def test_config_examples_contain_only_secret_references(repo_root, tracked_files):
    all_violations = []
    checked_any = False
    for rel_path in _config_files(repo_root, tracked_files):
        checked_any = True
        text = (repo_root / rel_path).read_text()
        if rel_path.endswith((".yaml", ".yml")):
            all_violations.extend(_check_yaml_config(text, rel_path))
        elif rel_path.endswith(".env"):
            all_violations.extend(_check_env_config(text, rel_path))

    assert checked_any, "expected at least one config/ file to be checked"
    assert not all_violations, (
        "Configuration file(s) under config/ contain credential-shaped "
        "values that are not secret references/placeholders:\n"
        + "\n".join(f"  - {v}" for v in all_violations)
    )


@pytest.mark.parametrize(
    "text,expect_violation",
    [
        ("credential_ref: BAGMAN_MAIL_NOUSTAI\n", False),
        ("password: hunter2\n", True),
        # Note: this fake value is deliberately low-entropy/hyphenated (not a
        # plausible-looking real key) so it does not itself trip Test D's
        # gitleaks scan once this test file is committed.
        ("api_key: not-a-real-secret-test-value\n", True),
        ("db_password_file: /run/secrets/db_password\n", False),
        ("token: <SET_VIA_ENVIRONMENT>\n", False),
    ],
)
def test_yaml_matcher_catches_real_and_allows_safe_values(text, expect_violation):
    # Sanity check on the matcher itself — proves the check is not
    # vacuously permissive (would flag nothing regardless of content).
    violations = _check_yaml_config(text, "synthetic")
    assert bool(violations) == expect_violation


@pytest.mark.parametrize(
    "line,expect_violation",
    [
        ("BAGMAN_DB_PASSWORD_FILE=/run/secrets/bagman_db_password", False),
        ("BAGMAN_DB_PASSWORD=hunter2", True),
        # Deliberately low-entropy/repetitive fake value — see note above.
        ("SOME_API_TOKEN=obviously-fake-test-value", True),
        ("BAGMAN_ENV=development", False),
    ],
)
def test_env_matcher_catches_real_and_allows_safe_values(line, expect_violation):
    violations = _check_env_config(line + "\n", "synthetic")
    assert bool(violations) == expect_violation
