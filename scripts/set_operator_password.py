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

Failure-safe ordering (WO-BAGMAN-117 §6.A'.13, Architect correction,
2026-10-14)
------------------------------------------------------------------------
The ORIGINAL version of this script wrote the new password-hash file
FIRST and revoked existing sessions SECOND — if revocation then failed
(e.g. the session store was briefly unreachable), the new password was
already live while old, pre-reset sessions remained valid, a genuine
half-rotated security gap. This version revokes every existing session
FIRST, and only writes the new password-hash file once that revocation
has ACTUALLY, VERIFIABLY completed:

* If session revocation fails (session store unavailable, or any other
  error), this script aborts BEFORE touching the password-hash file at
  all — the OLD password/hash remains exactly as it was (not a
  half-rotated state: the file on disk never changes), and the
  failure is reported loudly (non-zero exit, clear stderr message),
  never swallowed into a false "success".
* The new hash file is then written ATOMICALLY (temp file in the same
  directory, created with restrictive `0600` permissions from the
  moment it is created, then renamed into place with `os.replace` —
  never a plain `write_text` followed by a LATER `chmod`, which leaves
  a real window where the file exists, default/umask-permissioned,
  content possibly partially written). If this step fails, the
  failure is likewise reported loudly and distinctly — sessions have
  already been revoked by this point (a safe, reportable, retryable
  outcome: the OLD password/hash file is still untouched and still
  what is actually live, so the operator can still log in with the
  OLD password and simply re-run this tool) — never reported as
  success either.
* A successful run therefore guarantees: every previously-active
  session has genuinely been revoked, AND the new password hash is
  durably, correctly, and exclusively on disk — both, never just one,
  before this script ever prints its own success line or returns 0.

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
import tempfile
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


class SessionRevocationFailed(RuntimeError):
    """Raised by `_revoke_all_existing_sessions` for ANY failure to
    reach/use the session store — never let a bare, undifferentiated
    exception type stand in for "I could not verify this actually
    happened" (WO-BAGMAN-117 §6.A'.13: session-store unavailability
    must be its own distinctly-surfaced failure)."""


def _revoke_all_existing_sessions() -> int:
    """PID §117.F: "a reset must revoke all existing sessions ...
    calling into core/auth.py's own revocation logic, not
    reimplementing it" — this is the ONE call site, via the current
    `RuntimeComposition`, exactly the same seam every other operational
    script in this directory already uses to reach a real repository.

    Deliberately called BEFORE the new password hash is ever written
    (WO-BAGMAN-117 §6.A'.13) — see this module's own docstring for why:
    a reset must never report success, nor ever install the new
    credential, unless this has genuinely, verifiably completed.
    Raises `SessionRevocationFailed` (never lets a raw
    `core.errors.PersistenceError` — or anything else — propagate
    un-translated) so `main()` has exactly one exception type to catch
    for "the session store could not be reached/used".
    """
    # Imported here (not at module top) so this script never requires
    # app/api/composition.py's own production dependencies (boto3,
    # psycopg, ...) to be importable merely to run `--help` — mirrors
    # scripts/process_background_job_overflow.py's own local-import
    # convention for the exact same reason.
    from app.api.composition import get_composition
    from core.errors import PersistenceError

    try:
        composition = get_composition()
        return composition.operator_session_repository.revoke_all_sessions()
    except PersistenceError as exc:
        raise SessionRevocationFailed(str(exc.message)) from exc


def _write_hash_atomically(hash_file_path: Path, hashed: str) -> None:
    """Write `hashed` to `hash_file_path` atomically and with
    restrictive permissions from the moment the file is created
    (WO-BAGMAN-117 §6.A'.13).

    A temp file is created in the SAME directory as `hash_file_path`
    (so the final rename is a same-filesystem, atomic `os.replace` —
    never a cross-filesystem copy that could itself fail partway) via
    `tempfile.mkstemp`, which creates the file `0600`
    (owner-read/write-only) from the very first `os.open` call — never
    a plain `Path.write_text` followed by a LATER `.chmod(0o600)`,
    which leaves a real window where the file exists at the
    directory's default/umask-derived permissions (and with
    partially-written content) until that later call lands. The
    content is flushed and `fsync`'d before the rename, so the rename
    itself only ever makes a FULLY-written file visible at the final
    path — there is no observable state where `hash_file_path` is
    briefly empty or partially written.

    On ANY failure (directory creation, file creation, write, fsync,
    or rename), the temp file is removed and the original exception
    propagates — `hash_file_path` itself is left completely untouched
    in that case (either it still holds whatever it held before this
    call, or it still does not exist), never a corrupted/partial
    write.
    """
    hash_file_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(hash_file_path.parent),
        prefix=f".{hash_file_path.name}.",
        suffix=".tmp",
    )
    try:
        # Belt-and-suspenders: `mkstemp` already creates the file mode
        # `0600` via its own `os.open(..., 0o600)` call, but make that
        # restrictive-from-creation intent explicit and independent of
        # any host-specific umask configuration.
        os.chmod(tmp_name, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(hashed)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, hash_file_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)

    # Imported here (not at module top) — see `_revoke_all_existing_sessions`'s
    # own docstring for why.
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

    # Step 1 — revoke every existing session FIRST (WO-BAGMAN-117
    # §6.A'.13's failure-safe ordering — see this module's own
    # docstring). If this fails for ANY reason, abort HERE: the
    # password-hash file is not touched at all, so the OLD password/
    # hash remains exactly, unambiguously what is live — a clean,
    # reportable, retryable failure, never a half-rotated state.
    try:
        revoked_count = _revoke_all_existing_sessions()
    except SessionRevocationFailed as exc:
        print(
            "[set_operator_password] ABORTED: the session store could not be reached/used, so "
            f"existing sessions were NOT revoked ({exc}). The operator password hash was NOT "
            "changed (still the OLD hash, untouched). Fix session-store connectivity and re-run "
            "this tool to complete the reset.",
            file=sys.stderr,
        )
        return 2

    # Step 2 — only now write the new hash, atomically and
    # restrictively-permissioned from creation. If THIS fails, sessions
    # have already been revoked (above) but the credential itself was
    # never changed — still a safe, reportable failure: the OLD
    # password/hash file is untouched and still what is actually live.
    try:
        _write_hash_atomically(hash_file_path, hashed)
    except OSError as exc:
        print(
            f"[set_operator_password] ABORTED: {revoked_count} existing session(s) were revoked, "
            f"but the new password hash could NOT be written to {hash_file_path} ({exc}). The "
            "OLD password hash remains in effect (unchanged) — log in with the OLD password and "
            "re-run this tool to complete the reset. This is NOT a successful reset.",
            file=sys.stderr,
        )
        return 3

    print(
        f"[set_operator_password] operator password hash written to {hash_file_path} "
        f"(revoked {revoked_count} existing session(s))"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
