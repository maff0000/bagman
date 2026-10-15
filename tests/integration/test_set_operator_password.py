"""`scripts/set_operator_password.py` failure-safety proofs —
WO-BAGMAN-117 §6.A'.13 (Architect correction, 2026-10-14), §12
acceptance criterion 25.

Exercises `main()` directly (mirrors `tests/integration/
test_process_background_job_overflow.py`'s own "exercise this script's
own functions directly, never a subprocess" convention) against a
disposable `tmp_path` hash file and a stand-in `operator_session_repository`
— never a real Postgres engine (that repository's own real behaviour is
separately proven by `tests/persistence/
test_operator_session_repository.py`; this module proves `main()`'s own
ORDERING/failure-reporting contract, which is independent of which
concrete `SessionRepository` is behind it).

The core property under test throughout: a reset must NEVER report
success unless BOTH (a) every existing session has genuinely been
revoked and (b) the new password hash is durably on disk — and each of
the ways that can fail must be surfaced as a DISTINCT, loud failure,
never silently treated as success.
"""
from __future__ import annotations

import io
import os
import stat

import pytest

import scripts.set_operator_password as sop
from core.auth import hash_password, verify_operator_password
from core.errors import PersistenceError

_OLD_PASSWORD = "the genuinely original operator password 1a2b3c"
_NEW_PASSWORD = "a brand new operator password for this reset 9z8y7x"


class _StubSessionRepository:
    """A minimal stand-in for `core.auth.SessionRepository` — only
    `revoke_all_sessions` is exercised by this script, so only it is
    implemented for real; everything else is deliberately absent
    (never called)."""

    def __init__(self, *, revoked_count=3, raise_exc=None):
        self._revoked_count = revoked_count
        self._raise_exc = raise_exc
        self.revoke_all_sessions_call_count = 0

    def revoke_all_sessions(self, *, now=None):
        self.revoke_all_sessions_call_count += 1
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._revoked_count


class _StubComposition:
    def __init__(self, repository):
        self.operator_session_repository = repository


def _patch_composition(monkeypatch, repository) -> _StubComposition:
    composition = _StubComposition(repository)
    monkeypatch.setattr("app.api.composition.get_composition", lambda: composition)
    return composition


def _run_main(monkeypatch, *, hash_file, password=_NEW_PASSWORD) -> tuple[int, str, str]:
    """Runs `main()` with `--password-stdin` against the given hash
    file path, capturing stdout/stderr via `capsys`-free plain
    redirection (so this helper can be reused across tests without a
    `capsys` fixture argument threading requirement)."""
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{password}\n"))
    import contextlib

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = sop.main(["--password-stdin", "--password-hash-file", str(hash_file)])
    return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------
# Baseline — a clean reset with no faults: both steps actually happen,
# in the right order, before success is ever reported.
# ---------------------------------------------------------------------


def test_successful_reset_revokes_sessions_and_writes_new_hash(monkeypatch, tmp_path):
    hash_file = tmp_path / "operator_password_hash"
    hash_file.write_text(hash_password(_OLD_PASSWORD), encoding="utf-8")
    repository = _StubSessionRepository(revoked_count=4)
    _patch_composition(monkeypatch, repository)

    rc, out, err = _run_main(monkeypatch, hash_file=hash_file)

    assert rc == 0, err
    assert err == ""
    assert "revoked 4 existing session(s)" in out
    assert repository.revoke_all_sessions_call_count == 1

    new_hash = hash_file.read_text(encoding="utf-8")
    assert verify_operator_password(_NEW_PASSWORD, new_hash)
    assert not verify_operator_password(_OLD_PASSWORD, new_hash)
    # Restrictive permissions from creation, preserved through the
    # atomic rename into place.
    mode = stat.S_IMODE(hash_file.stat().st_mode)
    assert mode == 0o600


# ---------------------------------------------------------------------
# Failure matrix item 1 — session-store unavailable: must abort BEFORE
# the hash file is touched at all, and must be a distinct, loud
# failure, never success.
# ---------------------------------------------------------------------


def test_session_store_unavailable_aborts_before_writing_hash(monkeypatch, tmp_path):
    hash_file = tmp_path / "operator_password_hash"
    old_hash = hash_password(_OLD_PASSWORD)
    hash_file.write_text(old_hash, encoding="utf-8")
    repository = _StubSessionRepository(raise_exc=PersistenceError("simulated session store outage"))
    _patch_composition(monkeypatch, repository)

    rc, out, err = _run_main(monkeypatch, hash_file=hash_file)

    assert rc != 0
    assert rc == 2
    assert "ABORTED" in err
    assert "session store" in err.lower()
    assert "success" not in out.lower()  # no success line printed at all
    # The hash file is completely untouched — byte-identical to the OLD
    # hash, never a half-rotated credential.
    assert hash_file.read_text(encoding="utf-8") == old_hash
    assert verify_operator_password(_OLD_PASSWORD, hash_file.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------
# Failure matrix item 2 — password-hash file-write failure: sessions
# ARE already revoked by this point (failure-safe ordering means this
# is still a SAFE failure — the OLD hash remains live), but it must
# still be reported as a distinct, loud failure, never success.
# ---------------------------------------------------------------------


def test_file_write_failure_after_sessions_already_revoked_is_reported_distinctly(monkeypatch, tmp_path):
    hash_file = tmp_path / "operator_password_hash"
    old_hash = hash_password(_OLD_PASSWORD)
    hash_file.write_text(old_hash, encoding="utf-8")
    repository = _StubSessionRepository(revoked_count=2)
    _patch_composition(monkeypatch, repository)

    def _boom(path, hashed):
        raise OSError("simulated disk failure (file-write)")

    monkeypatch.setattr(sop, "_write_hash_atomically", _boom)

    rc, out, err = _run_main(monkeypatch, hash_file=hash_file)

    assert rc != 0
    assert rc == 3
    assert "ABORTED" in err
    assert "2 existing session(s)" in err  # sessions WERE revoked — named explicitly
    assert "success" not in out.lower()
    assert repository.revoke_all_sessions_call_count == 1
    # The OLD hash is still exactly what is on disk — never corrupted,
    # never partially written, never silently "upgraded".
    assert hash_file.read_text(encoding="utf-8") == old_hash


def test_file_write_failure_via_a_real_filesystem_fault_not_just_a_mock(monkeypatch, tmp_path):
    """Same failure class as above, but triggered by a GENUINE OSError
    from the real `_write_hash_atomically` (never monkeypatched) —
    proves the real atomic-write code path's own exception handling,
    not merely `main()`'s handling of an injected exception. The fault:
    the hash file's PARENT path is itself an ordinary file, so
    `Path.mkdir(parents=True, exist_ok=True)` genuinely raises
    `NotADirectoryError` (an `OSError` subclass)."""
    not_a_directory = tmp_path / "this-is-a-file-not-a-directory"
    not_a_directory.write_text("", encoding="utf-8")
    hash_file = not_a_directory / "operator_password_hash"

    repository = _StubSessionRepository(revoked_count=1)
    _patch_composition(monkeypatch, repository)

    rc, out, err = _run_main(monkeypatch, hash_file=hash_file)

    assert rc == 3
    assert "ABORTED" in err
    assert "success" not in out.lower()
    assert repository.revoke_all_sessions_call_count == 1
    assert not hash_file.exists()


# ---------------------------------------------------------------------
# Failure matrix item 3 — any OTHER incomplete-reset condition (an
# unexpected, non-`PersistenceError` exception from the session store)
# must never be silently swallowed into a false success either — it
# propagates loudly (a real, visible failure), and the hash file is
# never touched.
# ---------------------------------------------------------------------


def test_an_unexpected_non_persistence_error_from_the_session_store_is_never_swallowed_into_success(
    monkeypatch, tmp_path
):
    hash_file = tmp_path / "operator_password_hash"
    old_hash = hash_password(_OLD_PASSWORD)
    hash_file.write_text(old_hash, encoding="utf-8")
    repository = _StubSessionRepository(raise_exc=RuntimeError("a genuinely unexpected bug, not PersistenceError"))
    _patch_composition(monkeypatch, repository)

    monkeypatch.setattr("sys.stdin", io.StringIO(f"{_NEW_PASSWORD}\n"))
    with pytest.raises(RuntimeError):
        sop.main(["--password-stdin", "--password-hash-file", str(hash_file)])

    # Never reached a "success" state, and the hash file was never
    # touched — the crash itself IS the loud, non-silent failure.
    assert hash_file.read_text(encoding="utf-8") == old_hash


# ---------------------------------------------------------------------
# `_write_hash_atomically` — direct unit proof of the atomicity/
# permissions contract itself.
# ---------------------------------------------------------------------


def test_write_hash_atomically_creates_a_0600_file_with_the_exact_content(tmp_path):
    hash_file = tmp_path / "operator_password_hash"
    sop._write_hash_atomically(hash_file, "some-hash-value")

    assert hash_file.read_text(encoding="utf-8") == "some-hash-value"
    assert stat.S_IMODE(hash_file.stat().st_mode) == 0o600
    # No leftover temp file.
    leftovers = [p for p in tmp_path.iterdir() if p.name != hash_file.name]
    assert leftovers == []


def test_write_hash_atomically_replaces_an_existing_file_without_a_visible_partial_state(tmp_path):
    hash_file = tmp_path / "operator_password_hash"
    hash_file.write_text("old-hash-value", encoding="utf-8")
    hash_file.chmod(0o600)

    sop._write_hash_atomically(hash_file, "new-hash-value")

    assert hash_file.read_text(encoding="utf-8") == "new-hash-value"
    assert stat.S_IMODE(hash_file.stat().st_mode) == 0o600


def test_write_hash_atomically_leaves_the_original_file_untouched_on_failure(tmp_path, monkeypatch):
    hash_file = tmp_path / "operator_password_hash"
    hash_file.write_text("old-hash-value", encoding="utf-8")

    def _boom(*args, **kwargs):
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(os, "fsync", _boom)

    with pytest.raises(OSError):
        sop._write_hash_atomically(hash_file, "new-hash-value")

    assert hash_file.read_text(encoding="utf-8") == "old-hash-value"
    # The failed temp file must not be left behind either.
    leftovers = [p for p in tmp_path.iterdir() if p.name != hash_file.name]
    assert leftovers == []
