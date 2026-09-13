"""``EvidenceObjectStore`` — the abstract contract for durable storage
of original evidence bytes (CD-3 WI-2, PID §15/§16).

Canonical/domain code (``core/``, ``services/evidence/``) depends only
on this interface, never on a storage provider SDK directly (PID
§53). ``persistence/objects/minio_store.py`` is this module's one
concrete, ``boto3``-based implementation.

Object key doctrine (PID §19)
------------------------------
A stored object's key is::

    evidence/<evidence_id>/<content_hash_value>

deterministic and content-addressed, never derived from a
user-supplied filename. Both components are validated (see
``_validate_key_component``) before being joined into a key, so the
key can never contain a directory-traversal segment or other unsafe
character, even though today's only two callers (a UUIDv7
``evidence_id`` and a lowercase hex SHA-256 digest) already only ever
produce safe values.

Content addressing and verification (PID §18)
----------------------------------------------
``put()`` computes the SHA-256 of the bytes it is given itself and
verifies that against the caller-supplied ``content_hash`` *before*
accepting the write; a mismatch raises
:class:`core.errors.IntegrityError` and nothing is stored.

Because the storage key embeds the canonical content hash, ``get()``
re-hashes retrieved bytes against the hash encoded in the reference it
was asked to fetch, and raises :class:`core.errors.IntegrityError` if
they disagree. Corruption at rest is therefore detected on every read
that goes through a canonical (``object_key``-shaped) reference, not
only when a caller explicitly calls ``verify_hash()`` — this is a
judgment call beyond the PID's literal ask, made because the object
key already carries the information needed to self-check, and PID §18
asks for corruption/mismatch to be detected, not merely detectable on
request.

Immutability (PID §17)
-----------------------
``put()`` never silently overwrites an existing object at the same
key: a byte-for-byte identical re-put is a safe, idempotent no-op, but
different bytes discovered at an already-occupied key raise
:class:`core.errors.ImmutabilityViolationError` — the existing PID
§7/§35 vocabulary for "an attempt was made to change something
immutable", reused here rather than introduced as a new error type,
since this is exactly that situation applied to stored evidence bytes.
"""
from __future__ import annotations

import abc
import hashlib
import re
from typing import Mapping, Optional, Tuple

#: Safe characters for one key path component: letters, digits, dot,
#: underscore, hyphen. No `/`, no control characters. Combined with
#: the explicit `..` rejection below, this defends PID §19's "no
#: directory traversal" requirement.
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")

#: Matches exactly the key shape `object_key()` produces, so
#: `parse_object_key` can recover the two components back out of a
#: canonical storage_reference.
_KEY_PATTERN = re.compile(r"^evidence/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)$")


def compute_sha256(data: bytes) -> str:
    """Lowercase hex SHA-256 digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def _validate_key_component(label: str, value: str) -> str:
    if not isinstance(value, str) or not value or ".." in value or not _SAFE_COMPONENT.match(value):
        raise ValueError(
            f"{label} must be a non-empty string of [A-Za-z0-9._-] characters "
            f"with no '..' segment (got {value!r}); it becomes part of an "
            "object-storage key and must never allow directory traversal "
            "(PID §19)"
        )
    return value


def object_key(evidence_id: str, content_hash_value: str) -> str:
    """The deterministic, content-addressed storage key for the pair
    (``evidence_id``, ``content_hash_value``) — PID §19's own suggested
    form: ``evidence/<evidence_id>/<content_hash_value>``. Never derive
    a key from a user-supplied filename.
    """
    evidence_id = _validate_key_component("evidence_id", evidence_id)
    content_hash_value = _validate_key_component("content_hash_value", content_hash_value.lower())
    return f"evidence/{evidence_id}/{content_hash_value}"


def parse_object_key(storage_reference: str) -> Optional[Tuple[str, str]]:
    """Recover ``(evidence_id, content_hash_value)`` from a reference
    produced by :func:`object_key`, or ``None`` if ``storage_reference``
    is not shaped that way (e.g. a foreign/legacy reference) — callers
    degrade to skipping the self-check that depends on this rather
    than raising, since ``get()`` must remain usable for any key its
    backend can address.
    """
    match = _KEY_PATTERN.match(storage_reference)
    if match is None:
        return None
    return match.group(1), match.group(2)


# ---------------------------------------------------------------------
# Quarantine / staging keys (CD-4 WI-2, PID §21/§22)
# ---------------------------------------------------------------------
#
# Quarantined evidence (PID §21/§22) and an ACCEPTED-but-not-yet-
# REGISTERED intake's validated bytes (PID §68 WI-2's "stored... ready
# for WI-3 to pick up and register") both need a storage key that is
# NEVER shaped like `object_key()`'s `evidence/<id>/<hash>` — mixing
# them into the same key namespace as normal available evidence would
# make quarantined/staged bytes indistinguishable BY KEY SHAPE from
# canonical available evidence, which PID §22 explicitly requires stay
# "logically separable". Rather than add two near-duplicate sibling
# functions, both cases share one small, closed-vocabulary
# `prefixed_object_key()` below — the prefix itself is restricted to
# `_ALLOWED_PREFIXES` (never an arbitrary caller-supplied string), so
# the full set of key shapes any BAGMAN object store can ever produce
# stays small, fixed, and auditable.

#: The only non-`evidence/` key prefixes CD-4 WI-2 introduces:
#: `quarantine/<intake_id>/<hash>` (PID §21/§22) and
#: `intake-staging/<intake_id>/<hash>` (an ACCEPTED intake's bytes,
#: staged for WI-3's registration step to pick up — never itself a
#: canonical `evidence/` key, since WI-2 never registers an
#: `EvidenceItem`).
_ALLOWED_PREFIXES = frozenset({"quarantine", "intake-staging"})

_PREFIXED_KEY_PATTERN = re.compile(
    r"^(quarantine|intake-staging)/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)$"
)


def prefixed_object_key(prefix: str, object_id: str, content_hash_value: str) -> str:
    """The deterministic, content-addressed storage key
    ``<prefix>/<object_id>/<content_hash_value>`` for a non-canonical
    (quarantine/staging) object (PID §21/§22). ``prefix`` must be one
    of :data:`_ALLOWED_PREFIXES` — never an arbitrary string — so the
    set of key shapes a BAGMAN object store can ever hold stays small
    and fixed. ``object_id`` is an ``intake_id`` for both of today's
    callers, but is named generically since a key under one of these
    prefixes is never keyed by a canonical ``evidence_id``.
    """
    if prefix not in _ALLOWED_PREFIXES:
        raise ValueError(
            f"prefix must be one of {sorted(_ALLOWED_PREFIXES)}, got {prefix!r} — "
            "object-store key shapes are a small, fixed, auditable set (PID §22)"
        )
    object_id = _validate_key_component("object_id", object_id)
    content_hash_value = _validate_key_component("content_hash_value", content_hash_value.lower())
    return f"{prefix}/{object_id}/{content_hash_value}"


def parse_prefixed_object_key(storage_reference: str) -> Optional[Tuple[str, str, str]]:
    """Recover ``(prefix, object_id, content_hash_value)`` from a
    reference produced by :func:`prefixed_object_key`, or ``None`` if
    ``storage_reference`` is not shaped that way."""
    match = _PREFIXED_KEY_PATTERN.match(storage_reference)
    if match is None:
        return None
    return match.group(1), match.group(2), match.group(3)


def quarantine_object_key(intake_id: str, content_hash_value: str) -> str:
    """``quarantine/<intake_id>/<content_hash_value>`` (PID §21/§22) —
    the sibling of :func:`object_key` for quarantined material."""
    return prefixed_object_key("quarantine", intake_id, content_hash_value)


def staging_object_key(intake_id: str, content_hash_value: str) -> str:
    """``intake-staging/<intake_id>/<content_hash_value>`` — an
    ACCEPTED intake's validated bytes, stored ready for WI-3's
    registration step to hand off to canonical evidence storage (CD-4
    WI-2, PID §68). Deliberately never shaped like :func:`object_key`'s
    `evidence/` prefix: WI-2 never registers an `EvidenceItem` itself."""
    return prefixed_object_key("intake-staging", intake_id, content_hash_value)


def expected_hash_from_reference(storage_reference: str) -> Optional[str]:
    """The content-hash value encoded in ``storage_reference``,
    regardless of whether it is a canonical :func:`object_key` or a
    :func:`prefixed_object_key` (quarantine/staging) reference — or
    ``None`` if it matches neither shape. Used by every
    ``EvidenceObjectStore`` implementation's ``get()``/``_get_bytes()``
    to self-check retrieved bytes against the hash the key itself
    encodes, uniformly across all three key shapes."""
    parsed = parse_object_key(storage_reference)
    if parsed is not None:
        return parsed[1]
    prefixed = parse_prefixed_object_key(storage_reference)
    if prefixed is not None:
        return prefixed[2]
    return None


def normalize_hash_value(content_hash: Mapping[str, str]) -> str:
    """Extract and lower-case the ``value`` field of a
    ``{"algorithm": ..., "value": ...}`` content-hash mapping (the
    shape ``services.evidence.evidence.EvidenceItem.content_hash``
    uses). Raises ``ValueError`` for a malformed mapping — callers
    translate that into the canonical ``IntegrityError``/``ValueError``
    as appropriate for their own contract.
    """
    try:
        value = content_hash["value"]
    except (TypeError, KeyError) as exc:
        raise ValueError(
            "content_hash must be a mapping with a 'value' key, e.g. "
            '{"algorithm": "SHA-256", "value": "<64 lowercase hex chars>"} '
            f"(got {content_hash!r})"
        ) from exc
    if not isinstance(value, str) or not value:
        raise ValueError(f"content_hash['value'] must be a non-empty string (got {value!r})")
    return value.lower()


class EvidenceObjectStore(abc.ABC):
    """Storage abstraction for original evidence bytes (PID §16)."""

    @abc.abstractmethod
    def put(self, evidence_id: str, content_hash: Mapping[str, str], data: bytes) -> str:
        """Store ``data`` under a deterministic, content-addressed key
        derived from ``evidence_id`` and ``content_hash`` (see
        :func:`object_key`).

        Computes the SHA-256 of ``data`` itself and verifies it
        matches ``content_hash["value"]`` (case-insensitively; stored
        and compared lowercase) *before* accepting the write — raises
        :class:`core.errors.IntegrityError` and stores nothing on a
        mismatch.

        Immutable (PID §17): if an object already exists at the
        derived key, a byte-for-byte identical re-put is a safe
        no-op; different bytes at that key raise
        :class:`core.errors.ImmutabilityViolationError` rather than
        silently overwriting.

        Returns the storage key/reference actually used — this
        becomes the ``storage_reference`` field of a persisted
        ``EvidenceItem`` (wiring that persistence in is a different
        work item's responsibility).
        """
        raise NotImplementedError

    @abc.abstractmethod
    def put_prefixed(self, prefix: str, object_id: str, content_hash: Mapping[str, str], data: bytes) -> str:
        """Same contract as :meth:`put` (hash verification before
        accepting the write; a byte-identical re-put at the same key is
        an idempotent no-op; different bytes at an already-occupied key
        raise :class:`core.errors.ImmutabilityViolationError`), but
        stores under :func:`prefixed_object_key`'s
        ``<prefix>/<object_id>/<hash>`` shape instead of :meth:`put`'s
        ``evidence/<evidence_id>/<hash>`` (PID §21/§22, CD-4 WI-2) —
        used for quarantined material and for an ACCEPTED intake's
        validated-but-not-yet-registered bytes. ``prefix`` must be one
        of :func:`prefixed_object_key`'s allowed values.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get(self, storage_reference: str) -> bytes:
        """Retrieve the raw bytes for a previously-stored object.

        Raises :class:`core.errors.NotFoundError` if nothing exists at
        ``storage_reference``, and :class:`core.errors.IntegrityError`
        if the retrieved bytes do not re-hash to the value encoded in
        ``storage_reference`` (see the module docstring).
        """
        raise NotImplementedError

    @abc.abstractmethod
    def exists(self, storage_reference: str) -> bool:
        """Whether an object currently exists at ``storage_reference``."""
        raise NotImplementedError

    @abc.abstractmethod
    def verify_hash(self, storage_reference: str, expected_content_hash: Mapping[str, str]) -> bool:
        """Retrieve the object at ``storage_reference``, recompute its
        SHA-256, and compare it against
        ``expected_content_hash["value"]`` (case-insensitive; compared
        lowercase). This is PID §18's "retrieved bytes can be
        re-hashed, hash equality proven" step: it actually reads the
        bytes back and hashes them, never merely comparing stored
        metadata.

        Returns ``True`` on a match. Raises
        :class:`core.errors.IntegrityError` — rather than returning
        ``False`` — on any mismatch, so a caller can never mistake a
        missed check for a passed one; this also covers the mismatch
        ``get()`` itself may already have raised while retrieving the
        bytes.
        """
        raise NotImplementedError
