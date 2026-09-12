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
