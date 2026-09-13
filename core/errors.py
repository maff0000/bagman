"""Canonical BAGMAN error vocabulary (PID §35).

A small, closed set of domain errors that every public core/services
API method uses instead of leaking raw ``jsonschema`` or other
third-party exceptions. Each error carries a stable ``error_code``
string attribute matching the PID's vocabulary, plus a human-readable
message.

These six error types are deliberately kept as flat, direct subclasses
of :class:`BagmanError` (rather than a deeper hierarchy) — PID §35
lists them as one flat vocabulary, and no later section asks for
catch-by-category behaviour beyond catching the exact error a caller
expects.
"""
from __future__ import annotations


class BagmanError(Exception):
    """Base class for every canonical BAGMAN domain error.

    Not itself one of the PID §35 vocabulary entries — callers should
    raise/catch one of the concrete subclasses below.
    """

    error_code: str = "BAGMAN_ERROR"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return f"{type(self).__name__}(error_code={self.error_code!r}, message={self.message!r})"


class ValidationError(BagmanError):
    """A construction/registration path rejected its input.

    Raised for JSON Schema contract-validation failures (wrapping the
    underlying ``jsonschema`` message rather than letting it escape)
    and for other domain-level input validation failures.
    """

    error_code = "VALIDATION_ERROR"


class ConflictError(BagmanError):
    """A write conflicts with existing canonical state."""

    error_code = "CONFLICT"


class NotFoundError(BagmanError):
    """A referenced canonical object does not exist."""

    error_code = "NOT_FOUND"


class DuplicateExternalReferenceError(BagmanError):
    """The same (provider, source_id, resource_type, external_id) tuple
    already exists and maps to a *different* canonical object (PID
    §10/§34) — a genuine conflict, distinct from an idempotent replay.
    """

    error_code = "DUPLICATE_EXTERNAL_REFERENCE"


class InvalidProvenanceError(BagmanError):
    """A provenance edge references an ``evidence_id`` that does not
    exist — invalid/orphan provenance is rejected (PID §31).
    """

    error_code = "INVALID_PROVENANCE"


class ImmutabilityViolationError(BagmanError):
    """An attempt was made to change a field or record that is
    immutable by contract (PID §7, §35) — e.g. re-registering an
    existing ``entity_id``, changing an ``EvidenceItem``'s immutable
    fields, or reassigning an already-resolved ``entity_id``. Also
    raised by ``persistence.objects.EvidenceObjectStore.put()`` (CD-3
    WI-2) when different bytes are given for a storage key that
    already holds different content (PID §17) — original evidence
    bytes are immutable in exactly the same sense this error already
    names, so that case reuses this error rather than introducing a
    new one.
    """

    error_code = "IMMUTABILITY_VIOLATION"


class StorageError(BagmanError):
    """The object-store backend is unreachable or misbehaving — a
    network error, missing/inaccessible bucket, credential failure,
    etc. (PID §57, CD-3 WI-2).

    ``persistence.objects.minio_store.MinIOObjectStore`` never lets a
    raw ``boto3``/``botocore`` exception escape any of its public
    methods; it catches and re-raises as this instead.
    """

    error_code = "STORAGE_ERROR"


class IntegrityError(BagmanError):
    """A SHA-256 content-hash mismatch (PID §18, CD-3 WI-2): either the
    caller-supplied ``content_hash`` did not match the bytes actually
    about to be stored (``EvidenceObjectStore.put()``), or a
    ``verify_hash()``/``get()``-time re-hash of retrieved bytes did not
    match the expected value — corruption or mismatch detected on
    read.
    """

    error_code = "INTEGRITY_ERROR"


class InvalidStateTransitionError(BagmanError):
    """An attempted state-machine transition is not one of the
    deterministic transitions the owning state machine allows (CD-4
    WI-1, PID §7) — e.g. attempting to move an ``IntakeRecord`` from a
    terminal state (``REGISTERED``, ``REJECTED``, ``QUARANTINED``,
    ``FAILED``) to any other state, or skipping a required
    intermediate state. Distinct from ``ValidationError`` (malformed
    input shape) and ``ImmutabilityViolationError`` (an already-
    resolved immutable field being changed again) — this is
    specifically about an edge that the state machine's own transition
    table does not contain.
    """

    error_code = "INVALID_STATE_TRANSITION"


class IdempotencyConflictError(BagmanError):
    """The same idempotency key (PID §25/§35) was reused for a request
    whose identifying content differs from the first request that
    established it — a genuine conflict, distinct from an idempotent
    replay (which resolves to the existing record instead of raising).
    See ``services.evidence.intake.intake`` module docstring for the
    exact "same content" test CD-4 WI-1 applies at intake-creation
    time.
    """

    error_code = "IDEMPOTENCY_CONFLICT"


class FileTooLargeError(BagmanError):
    """An intake upload exceeded a configured size limit (PID §12/§35,
    CD-4 WI-2) — either the per-file limit or the request-level limit.
    Raised by ``services.evidence.intake.streaming.spool_stream`` the
    moment the running total exceeds the limit (never after buffering
    the whole oversized stream first), and turned into an intake
    ``REJECTED`` transition (never ``QUARANTINED``) by
    ``services.evidence.intake.validation_pipeline`` — an oversized
    request carries no investigative value worth retaining.
    """

    error_code = "FILE_TOO_LARGE"


class UnsupportedContentTypeError(BagmanError):
    """Intake content's detected type is not one this policy accepts,
    and policy says to reject rather than quarantine it (PID §16/§17/
    §34/§35, CD-4 WI-2) — e.g. an archive/unsupported binary format
    under the default policy. Distinct from a scanner verdict: this is
    a content-TYPE policy decision, made before any malware scan runs.
    """

    error_code = "UNSUPPORTED_CONTENT_TYPE"


class ContentTypeMismatchError(BagmanError):
    """The uploader-reported MIME type and the byte-sniffed detected
    MIME type disagree, and the active
    ``services.evidence.intake.policy.IntakePolicy.mime_mismatch_policy``
    is the strict ``"REJECT"`` setting rather than the default
    ``"OBSERVE"`` (PID §15/§35, CD-4 WI-2). Under the default policy, a
    mismatch is recorded/observable (both MIME fields are always
    populated distinctly) rather than raised — see
    ``services.evidence.intake.validation_pipeline`` module docstring.
    """

    error_code = "CONTENT_TYPE_MISMATCH"


class MalwareDetectedError(BagmanError):
    """Reserved canonical error for a confirmed malware verdict (PID
    §20/§35, CD-4 WI-2). CD-4 WI-2's own pipeline does not raise this
    itself — a scanner ``MALICIOUS``/``SUSPICIOUS`` verdict is routed
    to intake ``QUARANTINED`` (retained for investigation, PID §21)
    rather than treated as a hard failure — but the vocabulary entry is
    declared here per PID §35 for any future caller that needs to
    signal this condition as a raised error rather than a quarantined
    intake state.
    """

    error_code = "MALWARE_DETECTED"


class ScanFailedError(BagmanError):
    """The content-safety scanner could not produce a verdict at all —
    unreachable, timed out, or returned ``SCAN_ERROR`` (PID §19/§20/
    §35, CD-4 WI-2). This is an INFRASTRUCTURE failure, distinct from a
    content verdict: ``services.evidence.intake.validation_pipeline``
    fails closed by routing this to intake ``FAILED`` (retryable
    infrastructure failure), never to ``ACCEPTED`` — see that module's
    docstring for the full fail-closed contract and why a scanner
    ``UNSUPPORTED`` verdict is routed to ``QUARANTINED`` instead of
    this error (it is content ambiguity, not an infrastructure fault).
    """

    error_code = "SCAN_FAILED"


class PersistenceError(BagmanError):
    """A general persistence-layer failure distinct from a constraint
    violation — e.g. a database connectivity/operational failure (PID
    §57's ``PERSISTENCE_ERROR``).

    Added by CD-3 WI-1 (PostgreSQL persistence): every
    ``persistence/postgres/*_repository.py`` implementation catches
    generic ``sqlalchemy.exc.OperationalError``/``DatabaseError`` (and
    any other non-constraint driver/SQLAlchemy failure) and re-raises
    it as this canonical error rather than letting a raw psycopg/
    SQLAlchemy exception escape as the public contract (PID §57). A
    genuine constraint violation (unique/foreign-key) is still
    translated to the more specific existing error it corresponds to
    (``ImmutabilityViolationError``, ``DuplicateExternalReferenceError``,
    ``InvalidProvenanceError``) rather than this generic one.
    """

    error_code = "PERSISTENCE_ERROR"
