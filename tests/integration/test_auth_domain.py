"""Domain tests for ``core.auth`` (WO-BAGMAN-117 §6.A.1/§6.A.11,
acceptance criteria 19-22's "new, independent domain tests").

Pure in-memory domain behaviour — no database, no HTTP layer (see
``tests/app_api/test_auth_endpoints.py`` for the HTTP-level proof and
``tests/persistence/test_operator_session_repository.py`` for the real
Postgres durability proof).
"""
from __future__ import annotations

import os
import time
from datetime import timedelta

import pytest
from argon2 import Type

from core.auth import (
    DEFAULT_OPERATOR_PASSWORD_HASH_FILE,
    InMemorySessionRepository,
    SessionStatus,
    evaluate_session_token,
    generate_csrf_token,
    generate_session_token,
    hash_password,
    hash_token,
    read_operator_password_hash,
    session_max_lifetime,
    tokens_match,
    verify_operator_password,
)
from core.errors import PersistenceError
from core.timestamps import utc_now


# ---------------------------------------------------------------------
# Argon2id password hashing — PID §117.C "Argon2id, never bcrypt"
# ---------------------------------------------------------------------


def test_hash_password_uses_argon2id_not_bcrypt():
    hashed = hash_password("correct horse battery staple")
    # argon2-cffi's own hash encoding names the variant used — proves
    # this is genuinely Argon2id (`$argon2id$...`), never Argon2i/
    # Argon2d/bcrypt, by inspecting the real produced hash rather than
    # merely trusting the library's documented default.
    assert hashed.startswith("$argon2id$")
    assert "bcrypt" not in hashed
    assert "$2a$" not in hashed and "$2b$" not in hashed  # bcrypt's own hash-prefix shape


def test_hash_password_default_hasher_type_is_argon2id():
    from core.auth import _password_hasher  # noqa: SLF001 - test-only introspection

    assert _password_hasher.type is Type.ID


def test_verify_operator_password_round_trip_succeeds_for_the_correct_password():
    hashed = hash_password("a genuinely random test password 8f3k2")
    assert verify_operator_password("a genuinely random test password 8f3k2", hashed) is True


def test_verify_operator_password_rejects_the_wrong_password():
    hashed = hash_password("the real password")
    assert verify_operator_password("a guess", hashed) is False


def test_verify_operator_password_rejects_a_malformed_stored_hash_without_raising():
    assert verify_operator_password("anything", "not-a-real-argon2-hash") is False


# ---------------------------------------------------------------------
# Session/CSRF token generation — PID §117.C
# ---------------------------------------------------------------------


def test_generate_session_token_is_random_and_url_safe():
    a = generate_session_token()
    b = generate_session_token()
    assert a != b
    assert len(a) >= 32
    assert all(c.isalnum() or c in "-_" for c in a)


def test_generate_csrf_token_is_independent_of_session_token():
    session_token = generate_session_token()
    csrf_token = generate_csrf_token()
    assert session_token != csrf_token


def test_hash_token_is_a_one_way_sha256_hex_digest():
    raw = generate_session_token()
    digest = hash_token(raw)
    assert digest != raw
    assert len(digest) == 64
    int(digest, 16)  # valid hex
    # Deterministic (same input -> same digest, needed for lookup).
    assert hash_token(raw) == digest


def test_tokens_match_is_a_plain_equality_check_with_no_raised_exception_on_length_mismatch():
    assert tokens_match("abc", "abc") is True
    assert tokens_match("abc", "abd") is False
    assert tokens_match("abc", "abcd") is False


# ---------------------------------------------------------------------
# session_max_lifetime — PID §117.C "configurable 12-hour absolute
# maximum session lifetime"
# ---------------------------------------------------------------------


def test_session_max_lifetime_defaults_to_twelve_hours(monkeypatch):
    monkeypatch.delenv("BAGMAN_SESSION_MAX_LIFETIME_HOURS", raising=False)
    assert session_max_lifetime() == timedelta(hours=12)


def test_session_max_lifetime_is_configurable(monkeypatch):
    monkeypatch.setenv("BAGMAN_SESSION_MAX_LIFETIME_HOURS", "1")
    assert session_max_lifetime() == timedelta(hours=1)


# ---------------------------------------------------------------------
# read_operator_password_hash — PID §117.F secret-file convention
# ---------------------------------------------------------------------


def test_read_operator_password_hash_reads_the_configured_file(tmp_path, monkeypatch):
    hash_file = tmp_path / "operator_password_hash"
    hash_file.write_text("$argon2id$v=19$m=1,t=1,p=1$c29tZXNhbHQ$aGFzaA\n", encoding="utf-8")
    monkeypatch.setenv("BAGMAN_OPERATOR_PASSWORD_HASH_FILE", str(hash_file))

    assert read_operator_password_hash() == "$argon2id$v=19$m=1,t=1,p=1$c29tZXNhbHQ$aGFzaA"


def test_read_operator_password_hash_explicit_path_overrides_env(tmp_path, monkeypatch):
    env_file = tmp_path / "env_hash"
    env_file.write_text("env-value", encoding="utf-8")
    explicit_file = tmp_path / "explicit_hash"
    explicit_file.write_text("explicit-value", encoding="utf-8")
    monkeypatch.setenv("BAGMAN_OPERATOR_PASSWORD_HASH_FILE", str(env_file))

    assert read_operator_password_hash(str(explicit_file)) == "explicit-value"


def test_read_operator_password_hash_raises_a_clear_error_when_no_credential_was_ever_bootstrapped(
    tmp_path, monkeypatch
):
    missing_file = tmp_path / "never-written"
    monkeypatch.setenv("BAGMAN_OPERATOR_PASSWORD_HASH_FILE", str(missing_file))

    with pytest.raises(RuntimeError, match="set_operator_password"):
        read_operator_password_hash()


def test_default_operator_password_hash_file_matches_the_documented_docker_secret_path():
    assert DEFAULT_OPERATOR_PASSWORD_HASH_FILE == "/run/secrets/operator_password_hash"


# ---------------------------------------------------------------------
# InMemorySessionRepository + evaluate_session_token — explicit,
# distinct session states (PID §117.E)
# ---------------------------------------------------------------------


@pytest.fixture
def repository():
    return InMemorySessionRepository()


def test_create_and_get_session_round_trips(repository):
    now = utc_now()
    raw_token = generate_session_token()
    digest = hash_token(raw_token)
    csrf_token = generate_csrf_token()

    created = repository.create_session(
        token_digest=digest, csrf_token=csrf_token, created_at=now, expires_at=now + timedelta(hours=1)
    )
    fetched = repository.get_by_token_digest(digest)

    assert fetched == created
    assert fetched.token_digest == digest
    assert fetched.csrf_token == csrf_token
    assert fetched.revoked_at is None


def test_get_by_token_digest_returns_none_for_an_unknown_digest_never_raises(repository):
    assert repository.get_by_token_digest("0" * 64) is None


def test_evaluate_session_token_is_missing_when_no_cookie_was_presented(repository):
    evaluation = evaluate_session_token(None, repository=repository)
    assert evaluation.status is SessionStatus.MISSING
    assert evaluation.is_authenticated is False


def test_evaluate_session_token_is_missing_for_an_empty_string_token(repository):
    evaluation = evaluate_session_token("", repository=repository)
    assert evaluation.status is SessionStatus.MISSING


def test_evaluate_session_token_is_missing_for_an_unknown_forged_token(repository):
    evaluation = evaluate_session_token("some-token-that-was-never-issued", repository=repository)
    assert evaluation.status is SessionStatus.MISSING


def test_evaluate_session_token_is_valid_for_a_real_unexpired_unrevoked_session(repository):
    now = utc_now()
    raw_token = generate_session_token()
    repository.create_session(
        token_digest=hash_token(raw_token),
        csrf_token=generate_csrf_token(),
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )

    evaluation = evaluate_session_token(raw_token, repository=repository, now=now)

    assert evaluation.status is SessionStatus.VALID
    assert evaluation.is_authenticated is True
    assert evaluation.session is not None


def test_evaluate_session_token_is_expired_distinctly_from_missing_or_revoked(repository):
    now = utc_now()
    raw_token = generate_session_token()
    repository.create_session(
        token_digest=hash_token(raw_token),
        csrf_token=generate_csrf_token(),
        created_at=now - timedelta(hours=2),
        expires_at=now - timedelta(hours=1),
    )

    evaluation = evaluate_session_token(raw_token, repository=repository, now=now)

    assert evaluation.status is SessionStatus.EXPIRED
    assert evaluation.is_authenticated is False


def test_evaluate_session_token_is_revoked_distinctly_from_missing_or_expired(repository):
    now = utc_now()
    raw_token = generate_session_token()
    digest = hash_token(raw_token)
    repository.create_session(
        token_digest=digest, csrf_token=generate_csrf_token(), created_at=now, expires_at=now + timedelta(hours=1)
    )
    repository.revoke_session(digest)

    evaluation = evaluate_session_token(raw_token, repository=repository, now=now)

    assert evaluation.status is SessionStatus.REVOKED
    assert evaluation.is_authenticated is False


def test_revoke_session_is_idempotent_for_an_unknown_digest(repository):
    # Must never raise — logout must always succeed from the caller's
    # perspective (core.auth.SessionRepository's own abstract contract).
    repository.revoke_session("digest-that-was-never-created")


def test_revoke_session_is_idempotent_when_called_twice(repository):
    now = utc_now()
    raw_token = generate_session_token()
    digest = hash_token(raw_token)
    repository.create_session(
        token_digest=digest, csrf_token=generate_csrf_token(), created_at=now, expires_at=now + timedelta(hours=1)
    )
    repository.revoke_session(digest)
    first_revoked_at = repository.get_by_token_digest(digest).revoked_at

    repository.revoke_session(digest)  # second call: must not raise, must not change revoked_at
    assert repository.get_by_token_digest(digest).revoked_at == first_revoked_at


def test_revoke_all_sessions_revokes_every_active_session_and_returns_the_count(repository):
    now = utc_now()
    digests = []
    for _ in range(3):
        raw_token = generate_session_token()
        digest = hash_token(raw_token)
        repository.create_session(
            token_digest=digest, csrf_token=generate_csrf_token(), created_at=now, expires_at=now + timedelta(hours=1)
        )
        digests.append(digest)
    # One already revoked beforehand — must not be double-counted or error.
    repository.revoke_session(digests[0])

    revoked_count = repository.revoke_all_sessions(now=now)

    assert revoked_count == 2  # only the two that were still active
    for digest in digests:
        assert repository.get_by_token_digest(digest).revoked_at is not None


def test_revoke_all_sessions_on_an_empty_repository_revokes_nothing(repository):
    assert repository.revoke_all_sessions() == 0


def test_session_rotation_two_logins_never_reuse_the_same_token(repository):
    """PID §117.C: 'session rotation on successful authentication — a
    fresh token issued at login, never reused across login events.'"""
    now = utc_now()
    first_raw = generate_session_token()
    second_raw = generate_session_token()
    assert first_raw != second_raw

    repository.create_session(
        token_digest=hash_token(first_raw), csrf_token=generate_csrf_token(), created_at=now,
        expires_at=now + timedelta(hours=1),
    )
    repository.create_session(
        token_digest=hash_token(second_raw), csrf_token=generate_csrf_token(), created_at=now,
        expires_at=now + timedelta(hours=1),
    )

    first_eval = evaluate_session_token(first_raw, repository=repository, now=now)
    second_eval = evaluate_session_token(second_raw, repository=repository, now=now)
    assert first_eval.status is SessionStatus.VALID
    assert second_eval.status is SessionStatus.VALID
    assert first_eval.session.token_digest != second_eval.session.token_digest
