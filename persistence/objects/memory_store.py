"""``InMemoryObjectStore`` — a narrow in-memory reference implementation
of :class:`persistence.objects.store.EvidenceObjectStore` (CD-3 WI-3).

Added by WI-3 for exactly the same reason ``core/`` and
``services/evidence/`` already ship an ``InMemory*`` repository
alongside every durable one (PID §21 Option A): the composition root
(``app/api/composition.py``) needs *something* to hand a
development/test-mode ``BagmanCanonicalAPI`` for its evidence-store
dependency, without requiring a real MinIO endpoint to be reachable
just to run the app or its test suite locally.

This is a plain process-local dict keyed by the same content-addressed
object key :func:`persistence.objects.store.object_key` produces — it
reuses every one of that module's shared helpers
(``compute_sha256``/``normalize_hash_value``/``object_key``/
``parse_object_key``) rather than re-deriving any hashing or key-shape
logic of its own, so its behaviour (immutability, hash verification on
``put()``/``get()``/``verify_hash()``) is identical to
:class:`persistence.objects.minio_store.MinIOObjectStore` in every way
that matters to a caller — durability across process restart is the
only thing this implementation does not provide, exactly mirroring how
``core.entity.InMemoryEntityRepository`` compares to
``persistence.postgres.entity_repository.PostgresEntityRepository``.

Not used in ``BAGMAN_RUNTIME_ENV=production`` — see
``app/api/composition.py``'s module docstring for the hard "no
fallback" invariant this must never violate: production composition
never substitutes this for a real object store, under any failure
condition.
"""
from __future__ import annotations

from typing import Mapping

from core.errors import ImmutabilityViolationError, IntegrityError, NotFoundError
from persistence.objects.store import (
    EvidenceObjectStore,
    compute_sha256,
    expected_hash_from_reference,
    normalize_hash_value,
    object_key,
    prefixed_object_key,
)


class InMemoryObjectStore(EvidenceObjectStore):
    """Process-local, non-durable ``EvidenceObjectStore`` for
    development/test composition only."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def put(self, evidence_id: str, content_hash: Mapping[str, str], data: bytes) -> str:
        expected_value = normalize_hash_value(content_hash)
        actual_value = compute_sha256(data)
        if actual_value != expected_value:
            raise IntegrityError(
                f"put() rejected for evidence_id '{evidence_id}': the SHA-256 of "
                f"the bytes given ('{actual_value}') does not match the "
                f"caller-supplied content_hash value ('{expected_value}') — "
                "nothing was stored"
            )

        key = object_key(evidence_id, expected_value)
        existing = self._objects.get(key)
        if existing is not None:
            if existing == data:
                return key  # idempotent no-op: identical bytes already stored
            raise ImmutabilityViolationError(
                f"refusing to overwrite existing object at '{key}' with different "
                "bytes — original evidence storage is immutable (PID §17); if "
                "corrected content has arrived, it must be registered as new "
                "evidence with its own evidence_id/content_hash, not written "
                "over this key"
            )

        self._objects[key] = data
        return key

    def put_prefixed(self, prefix: str, object_id: str, content_hash: Mapping[str, str], data: bytes) -> str:
        expected_value = normalize_hash_value(content_hash)
        actual_value = compute_sha256(data)
        if actual_value != expected_value:
            raise IntegrityError(
                f"put_prefixed() rejected for prefix '{prefix}'/object_id '{object_id}': "
                f"the SHA-256 of the bytes given ('{actual_value}') does not match the "
                f"caller-supplied content_hash value ('{expected_value}') — nothing was stored"
            )

        key = prefixed_object_key(prefix, object_id, expected_value)
        existing = self._objects.get(key)
        if existing is not None:
            if existing == data:
                return key  # idempotent no-op: identical bytes already stored
            raise ImmutabilityViolationError(
                f"refusing to overwrite existing object at '{key}' with different "
                "bytes — quarantine/staging storage is immutable in exactly the "
                "same sense as canonical evidence storage (PID §17/§21)"
            )

        self._objects[key] = data
        return key

    def get(self, storage_reference: str) -> bytes:
        try:
            data = self._objects[storage_reference]
        except KeyError:
            raise NotFoundError(f"no object at storage_reference '{storage_reference}'") from None

        expected_hash = expected_hash_from_reference(storage_reference)
        if expected_hash is not None:
            actual_hash = compute_sha256(data)
            if actual_hash != expected_hash:
                raise IntegrityError(
                    f"corruption detected reading '{storage_reference}': the "
                    f"stored bytes hash to '{actual_hash}' but the reference's "
                    f"own key encodes '{expected_hash}'"
                )
        return data

    def exists(self, storage_reference: str) -> bool:
        return storage_reference in self._objects

    def verify_hash(self, storage_reference: str, expected_content_hash: Mapping[str, str]) -> bool:
        expected_value = normalize_hash_value(expected_content_hash)
        data = self.get(storage_reference)  # already raises IntegrityError on self-corruption
        actual_value = compute_sha256(data)
        if actual_value != expected_value:
            raise IntegrityError(
                f"verify_hash() mismatch for '{storage_reference}': recomputed "
                f"'{actual_value}' but expected '{expected_value}'"
            )
        return True
