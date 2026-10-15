"""Single-operator authentication domain model (PID §117.C) —
``SessionRecord``/``SessionRepository`` plus the Argon2id password
primitives and the explicit, distinct session-state evaluation WO-
BAGMAN-117 §6.A.1/§117.C/§117.E require.

Structural shape: mirrors ``core.entity.EntityRepository``'s own ABC +
``InMemory*`` reference implementation pattern exactly (per WO-
BAGMAN-117 §6.A.1's explicit instruction) — an abstract repository,
one narrow in-memory implementation here, and a Postgres-backed
implementation in ``persistence.postgres.operator_session_repository``
(mirroring ``persistence.postgres.entity_repository.PostgresEntityRepository``'s
own session-handling/error-translation discipline).

Deliberately NOT a JSON-Schema-``contracts/*``-backed domain object
(a documented judgment call, following the closer-in-spirit precedent
already established by ``services.xero.oauth_state.OAuthState`` — see
that module's own docstring): a session is ephemeral security
plumbing, created at login and discarded at logout/expiry/revocation,
never itself a durable business record the GUI lists or an operator
edits. ``core.entity.GovernedEntity`` earns full contract validation
because it is exactly that kind of durable, API-visible record;
sessions are not.

Hard security invariants this module enforces or is relied upon to
enforce (PID §117.C, binding, do not relax):

* **Argon2id only** — never bcrypt. :func:`hash_password`/
  :func:`verify_operator_password` use ``argon2.PasswordHasher``,
  whose default ``type`` is ``argon2.Type.ID`` (Argon2id) — verified
  directly in this delivery's own test suite
  (``tests/persistence/test_operator_session_repository.py`` and the
  Stage B round-trip proof recorded in the delivery report), not
  merely assumed from the library's documentation.
* **Only a SHA-256 digest of the session token is ever persisted**
  (:func:`hash_token`) — the raw token (``secrets.token_urlsafe(32)``,
  :func:`generate_session_token`) exists only in the browser's cookie
  and the transient request context for that call; it is never written
  to ``SessionRecord``/``SessionRepository`` or logged.
* **Fail-closed on session-store unavailability**: this module never
  itself swallows a ``core.errors.PersistenceError`` raised by a
  ``SessionRepository`` implementation — :func:`evaluate_session_token`
  lets it propagate, and the caller (the auth-enforcement middleware,
  ``app/api/main.py``) is the one place that translates it into a
  502/503 "access denied" response, never a silent allow.
* **Explicit, distinct session states** (§117.E) — :class:`SessionStatus`
  never conflates "no cookie presented", "unknown/garbled token",
  "expired", and "revoked" into one generic branch; callers that need
  the distinction (logging, the auth middleware, ``GET /auth/status``)
  get it from :func:`evaluate_session_token`'s returned
  :class:`SessionEvaluation`.
"""
from __future__ import annotations

import abc
import enum
import hmac
import os
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from core.timestamps import utc_now

#: One shared, process-wide Argon2id hasher. ``argon2.PasswordHasher``'s
#: own default ``type`` is ``argon2.Type.ID`` (Argon2id) — the
#: Architect's explicit correction to an earlier bcrypt draft (PID
#: §117.C) — left at the library's own default cost parameters (no
#: tuning requested by the WO, and this codebase's own convention
#: elsewhere is to prefer a well-established library's own sane
#: defaults over speculative tuning no one asked for).
_password_hasher = PasswordHasher()

#: PID §117.C: "Configurable 12-hour absolute maximum session lifetime
#: as the initial value." ``BAGMAN_SESSION_MAX_LIFETIME_HOURS``
#: overrides it (e.g. for a shorter-lived test session) — never a
#: literal elsewhere in this codebase.
DEFAULT_SESSION_MAX_LIFETIME_HOURS = 12


def session_max_lifetime() -> timedelta:
    """The configured absolute maximum session lifetime (PID §117.C),
    read fresh on every call (mirrors
    ``persistence.postgres.session.get_database_url``'s own "read on
    every call, never memoized" discipline) so a test that changes the
    environment mid-run is never fighting a stale cached value."""
    hours = float(os.environ.get("BAGMAN_SESSION_MAX_LIFETIME_HOURS", str(DEFAULT_SESSION_MAX_LIFETIME_HOURS)))
    return timedelta(hours=hours)


#: PID §117.F: "writes only the Argon2id hash to a secret file path"
#: mirroring ``BAGMAN_DB_PASSWORD_FILE``/``BAGMAN_LITELLM_API_KEY_FILE``'s
#: own existing secret-file convention
#: (``persistence/postgres/session.py``/``app/api/composition.py``).
DEFAULT_OPERATOR_PASSWORD_HASH_FILE = "/run/secrets/operator_password_hash"


def hash_password(plaintext_password: str) -> str:
    """Argon2id-hash ``plaintext_password``. Never logs or returns the
    plaintext anywhere else; the caller is responsible for discarding
    it immediately after this call (see ``scripts/set_operator_password.py``)."""
    return _password_hasher.hash(plaintext_password)


def verify_operator_password(plaintext_password: str, stored_hash: str) -> bool:
    """``True`` iff ``plaintext_password`` matches the Argon2id
    ``stored_hash``. Never raises for a wrong password or a malformed/
    corrupt stored hash — both are simply "not a match", so a caller
    can treat every failure path identically (PID §117.C's
    authentication model has exactly one operator/one password; there
    is no second credential shape to distinguish)."""
    try:
        return _password_hasher.verify(stored_hash, plaintext_password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def read_operator_password_hash(password_hash_file: Optional[str] = None) -> str:
    """Read the operator's Argon2id password hash from the file named
    by ``BAGMAN_OPERATOR_PASSWORD_HASH_FILE`` (default
    :data:`DEFAULT_OPERATOR_PASSWORD_HASH_FILE`) — never a literal in
    code or an environment variable VALUE (PID §117.F's "follows
    BAGMAN's existing host-managed Docker-secret convention exactly").
    Raises a plain ``RuntimeError`` (not a ``core.errors.BagmanError`` —
    this is startup/config wiring the same way
    ``persistence.postgres.session._read_password`` already treats its
    own, equivalent failure) if the file cannot be read — e.g. no
    operator credential has been bootstrapped yet via
    ``scripts/set_operator_password.py``.
    """
    path = Path(
        password_hash_file
        if password_hash_file is not None
        else os.environ.get("BAGMAN_OPERATOR_PASSWORD_HASH_FILE", DEFAULT_OPERATOR_PASSWORD_HASH_FILE)
    )
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(
            "could not read the BAGMAN operator password hash from "
            f"BAGMAN_OPERATOR_PASSWORD_HASH_FILE={str(path)!r}: {exc}. No operator "
            "credential has been bootstrapped yet — run "
            "`scripts/set_operator_password.py` first (PID §117.F)."
        ) from exc


def generate_session_token() -> str:
    """A fresh, cryptographically random, opaque session token (PID
    §117.C: ``secrets.token_urlsafe(32)``, stdlib only) — never reused
    across login events (session rotation on every successful login)."""
    return secrets.token_urlsafe(32)


def generate_csrf_token() -> str:
    """A fresh, cryptographically random double-submit CSRF token (PID
    §117.E), independent of the session token itself — leaking one must
    never imply the other."""
    return secrets.token_urlsafe(32)


def hash_token(raw_token: str) -> str:
    """One-way SHA-256 hex digest of a raw session token (PID §117.C:
    "Only a one-way SHA-256 digest of the token is persisted"). Never
    invertible, never logged with its raw input nearby."""
    return sha256(raw_token.encode("utf-8")).hexdigest()


def tokens_match(a: str, b: str) -> bool:
    """Constant-time string comparison for two token-shaped values
    (CSRF double-submit comparison, and anywhere else a timing side
    channel on a secret-shaped string would matter) — ``hmac.compare_digest``
    rather than ``==``."""
    return hmac.compare_digest(a, b)


@dataclass(frozen=True)
class SessionRecord:
    """A persisted operator session (PID §117.C). ``token_digest`` is
    both the natural identifier (this repository's own "primary key",
    mirroring ``services.xero.oauth_state.OAuthState``'s own
    "the token-shaped value itself is the lookup key" precedent) and
    the one and only thing persisted about the token — the raw token
    is never a field here, by construction, so there is no field to
    accidentally log or serialise.
    """

    token_digest: str
    csrf_token: str
    created_at: datetime
    expires_at: datetime
    revoked_at: Optional[datetime] = None

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def is_expired(self, *, at: datetime) -> bool:
        return at >= self.expires_at


class SessionRepository(abc.ABC):
    """Repository abstraction for :class:`SessionRecord` — mirrors
    ``core.entity.EntityRepository``'s own ABC + in-memory + Postgres
    repository shape exactly (WO-BAGMAN-117 §6.A.1)."""

    @abc.abstractmethod
    def create_session(
        self,
        *,
        token_digest: str,
        csrf_token: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> SessionRecord:
        """Persist a brand-new, not-yet-revoked session record."""
        raise NotImplementedError

    @abc.abstractmethod
    def get_by_token_digest(self, token_digest: str) -> Optional[SessionRecord]:
        """Read-only lookup — ``None`` if ``token_digest`` was never
        minted (never ``core.errors.NotFoundError``: an absent/unknown
        session is an expected, routine outcome on almost every
        unauthenticated request, exactly the same "a query, not a
        fetch-by-id, never raises" doctrine
        ``core.entity.EntityRepository.find_by_canonical_name`` and
        ``services.xero.oauth_state.OAuthStateRepository.get_state``
        already establish)."""
        raise NotImplementedError

    @abc.abstractmethod
    def revoke_session(self, token_digest: str) -> None:
        """Stamp ``revoked_at`` on this session if it exists and is not
        already revoked. Idempotent and never raises for an unknown or
        already-revoked ``token_digest`` — logout must always succeed
        from the caller's perspective (PID §117.C: "explicit logout
        immediately revokes the server-side session record"), and a
        double-logout or a reset-ordering race must never surface as a
        500."""
        raise NotImplementedError

    @abc.abstractmethod
    def revoke_all_sessions(self, *, now: Optional[datetime] = None) -> int:
        """Revoke every currently-active (not already revoked) session.
        Returns the count actually revoked. This is the ONE revocation
        entry point ``scripts/set_operator_password.py``'s password-
        reset path calls into (PID §117.F: "password reset revokes all
        existing sessions ... calling into core/auth.py, not
        reimplementing revocation logic")."""
        raise NotImplementedError


class InMemorySessionRepository(SessionRepository):
    """Narrow in-memory reference implementation (PID §21 Option A).

    Guarded by a real ``threading.Lock`` — mirrors
    ``services.xero.oauth_state.InMemoryOAuthStateRepository``'s own
    documented reasoning exactly: an HTTP handler this codebase already
    runs via ``run_in_threadpool`` may execute across genuine OS
    threads even within one process, so this is not merely
    single-process/GIL-serialised with nothing real to defend against.
    """

    def __init__(self) -> None:
        self._by_token_digest: dict[str, SessionRecord] = {}
        self._lock = threading.Lock()

    def create_session(
        self,
        *,
        token_digest: str,
        csrf_token: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> SessionRecord:
        record = SessionRecord(
            token_digest=token_digest,
            csrf_token=csrf_token,
            created_at=created_at,
            expires_at=expires_at,
            revoked_at=None,
        )
        with self._lock:
            self._by_token_digest[token_digest] = record
        return record

    def get_by_token_digest(self, token_digest: str) -> Optional[SessionRecord]:
        with self._lock:
            return self._by_token_digest.get(token_digest)

    def revoke_session(self, token_digest: str) -> None:
        with self._lock:
            current = self._by_token_digest.get(token_digest)
            if current is None or current.is_revoked:
                return
            self._by_token_digest[token_digest] = SessionRecord(
                token_digest=current.token_digest,
                csrf_token=current.csrf_token,
                created_at=current.created_at,
                expires_at=current.expires_at,
                revoked_at=utc_now(),
            )

    def revoke_all_sessions(self, *, now: Optional[datetime] = None) -> int:
        resolved_now = now if now is not None else utc_now()
        revoked_count = 0
        with self._lock:
            for digest, current in list(self._by_token_digest.items()):
                if current.is_revoked:
                    continue
                self._by_token_digest[digest] = SessionRecord(
                    token_digest=current.token_digest,
                    csrf_token=current.csrf_token,
                    created_at=current.created_at,
                    expires_at=current.expires_at,
                    revoked_at=resolved_now,
                )
                revoked_count += 1
        return revoked_count


class SessionStatus(enum.Enum):
    """Explicit, distinct session states (PID §117.E) — never
    conflated into one generic "unauthenticated" branch where the
    distinction matters for diagnosis."""

    #: No session cookie was presented at all, OR the presented token's
    #: digest matches no known session (an unknown/garbled/forged
    #: token is indistinguishable from "no session" to the caller —
    #: there is nothing more specific to tell them without handing an
    #: attacker a token-guessing oracle).
    MISSING = "MISSING"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    VALID = "VALID"


@dataclass(frozen=True)
class SessionEvaluation:
    status: SessionStatus
    session: Optional[SessionRecord] = None

    @property
    def is_authenticated(self) -> bool:
        return self.status is SessionStatus.VALID


def evaluate_session_token(
    raw_token: Optional[str],
    *,
    repository: SessionRepository,
    now: Optional[datetime] = None,
) -> SessionEvaluation:
    """The one, shared session-evaluation seam both the auth-enforcement
    middleware (``app/api/main.py``) and ``GET /auth/status``
    (``app/api/routers/auth.py``) call — never two independently
    reimplemented checks that could silently drift apart.

    Raises whatever ``core.errors.PersistenceError`` the repository
    itself raises on a genuine session-store connectivity failure —
    deliberately NOT caught here (see this module's own docstring,
    "Fail-closed on session-store unavailability"): the caller decides
    how to render that as a denied response; this function never turns
    an infrastructure failure into a silent ``MISSING``/allow.
    """
    if not raw_token:
        return SessionEvaluation(SessionStatus.MISSING)

    digest = hash_token(raw_token)
    record = repository.get_by_token_digest(digest)
    if record is None:
        return SessionEvaluation(SessionStatus.MISSING)

    resolved_now = now if now is not None else utc_now()
    if record.is_revoked:
        return SessionEvaluation(SessionStatus.REVOKED)
    if record.is_expired(at=resolved_now):
        return SessionEvaluation(SessionStatus.EXPIRED)
    return SessionEvaluation(SessionStatus.VALID, session=record)
