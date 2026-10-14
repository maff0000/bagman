#!/usr/bin/env python3
"""scripts/set_operator_password.py — WO-BAGMAN-117 §6.A.9, PID §117.F.

The ONE governed CLI procedure for both initial operator-credential
creation and reset (PID §117.F: "One governed CLI procedure ... for
both initial credential creation and reset"). There is no separate
"create" vs "reset" mode — running this tool always does the same
thing: hash a freshly-provided password with Argon2id, write only that
hash to the secret-file path BAGMAN's existing host-managed
Docker-secret convention already uses
(``BAGMAN_OPERATOR_PASSWORD_HASH_FILE``, mirroring
``BAGMAN_DB_PASSWORD_FILE``/``BAGMAN_LITELLM_API_KEY_FILE`` —
``persistence/postgres/session.py``/``app/api/composition.py``), and
revoke every existing operator session (PID §117.F: "a reset must
revoke all existing sessions ... calling into core/auth.py's own
revocation logic, not reimplementing it" — this script calls
``core.auth.SessionRepository.revoke_all_sessions`` via the current
``RuntimeComposition``, exactly the same seam every other operational
script in this directory already uses to reach a real repository, see
e.g. ``scripts/process_background_job_overflow.py``'s own
``get_composition()`` usage).

Never logs, echoes, or persists the plaintext password anywhere
------------------------------------------------------------------------
* The interactive path (``getpass.getpass``) never echoes input to the
  terminal.
* The plaintext is held in a local variable only long enough to hash
  it, then immediately discarded (rebound to ``None``) — it is never
  written to a file, an environment variable VALUE, a log line, this
  run's own stdout/stderr, or the database.
* ``--password-stdin`` (reads one line from stdin instead of an
  interactive prompt) exists ONLY for scripted/test invocations where
  no real TTY exists (this delivery's own test suite uses it) — it is
  not a convenience for routinely piping a password from another
  command's output in production use; the operator should still type
  it interactively for a real credential change.

Mirrors ``scripts/process_background_job_overflow.py``'s own
conventions (argparse, ``RawDescriptionHelpFormatter``,
``main(argv=None) -> int``,
``if __name__ == "__main__": raise SystemExit(main())``).
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path
from typing import Optional

#: PID §117.F doesn't specify a minimum length; a modest, defensible
#: floor for a single, high-value operator credential — not a
#: speculative full password-policy engine (out of this WO's scope).
_MIN_PASSWORD_LENGTH = 12


def _parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--password-hash-file",
        default=None,
        help=(
            "overrides BAGMAN_OPERATOR_PASSWORD_HASH_FILE for this invocation "
            "(default: read from that environment variable, or "
            "core.auth.DEFAULT_OPERATOR_PASSWORD_HASH_FILE if unset)"
        ),
    )
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help=(
            "read the new password from stdin (one line, no confirmation prompt) instead of an "
            "interactive getpass prompt — for scripted/test invocations ONLY where no real TTY "
            "exists; prefer the interactive prompt for a real production credential change"
        ),
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def _read_new_password(*, from_stdin: bool) -> Optional[str]:
    if from_stdin:
        line = sys.stdin.readline()
        return line.rstrip("\n") if line else None

    first = getpass.getpass("New BAGMAN operator password: ")
    second = getpass.getpass("Confirm password: ")
    if first != second:
        print("[set_operator_password] passwords did not match; aborting.", file=sys.stderr)
        return None
    return first


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)

    # Imported here (not at module top) so this script never requires
    # app/api/composition.py's own production dependencies (boto3,
    # psycopg, ...) to be importable merely to run `--help` — mirrors
    # scripts/process_background_job_overflow.py's own local-import
    # convention for the exact same reason.
    from core.auth import DEFAULT_OPERATOR_PASSWORD_HASH_FILE, hash_password

    plaintext = _read_new_password(from_stdin=args.password_stdin)
    if plaintext is None:
        return 1
    if len(plaintext) < _MIN_PASSWORD_LENGTH:
        print(
            f"[set_operator_password] password must be at least {_MIN_PASSWORD_LENGTH} characters; aborting.",
            file=sys.stderr,
        )
        return 1

    hashed = hash_password(plaintext)
    plaintext = None  # discard as soon as it is no longer needed; never logged above either

    hash_file_path = Path(
        args.password_hash_file
        or os.environ.get("BAGMAN_OPERATOR_PASSWORD_HASH_FILE", DEFAULT_OPERATOR_PASSWORD_HASH_FILE)
    )
    hash_file_path.parent.mkdir(parents=True, exist_ok=True)
    hash_file_path.write_text(hashed, encoding="utf-8")
    # Owner-read/write only — this is a credential hash, not a secret
    # requiring the same protection as the raw password, but there is
    # no reason for it to be group/world-readable either.
    hash_file_path.chmod(0o600)

    # PID §117.F: "a reset must revoke all existing sessions" — calling
    # into core.auth.SessionRepository.revoke_all_sessions via the
    # current RuntimeComposition, never reimplementing revocation here.
    from app.api.composition import get_composition

    composition = get_composition()
    revoked_count = composition.operator_session_repository.revoke_all_sessions()

    print(
        f"[set_operator_password] operator password hash written to {hash_file_path} "
        f"(revoked {revoked_count} existing session(s))"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
