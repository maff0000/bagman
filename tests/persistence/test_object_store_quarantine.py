"""`EvidenceObjectStore.put_prefixed()` — quarantine/staging key shapes
(CD-4 WI-2, PID §21/§22).

Exercises BOTH object-store implementations against the same behaviour
contract:

* `InMemoryObjectStore` — always runs, no external dependency.
* `MinIOObjectStore` — a REAL disposable MinIO (never mocked), skipped
  (not failed) when nothing answers at the configured endpoint, exactly
  like `tests/persistence/test_minio_store.py`. See that file's module
  docstring for the disposable-container command.
"""
from __future__ import annotations

import uuid

import pytest

from core.errors import ImmutabilityViolationError, IntegrityError, NotFoundError
from persistence.objects.memory_store import InMemoryObjectStore
from persistence.objects.minio_store import MinIOObjectStore
from persistence.objects.store import (
    compute_sha256,
    object_key,
    parse_prefixed_object_key,
    prefixed_object_key,
    quarantine_object_key,
    staging_object_key,
)
from tests.persistence.conftest import requires_live_minio


def _content_hash(data: bytes) -> dict:
    return {"algorithm": "SHA-256", "value": compute_sha256(data)}


@pytest.fixture
def intake_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------
# Pure key-shape functions — no store needed
# ---------------------------------------------------------------------


def test_quarantine_object_key_shape():
    key = quarantine_object_key("intake-123", "a" * 64)
    assert key == "quarantine/intake-123/" + "a" * 64


def test_staging_object_key_shape():
    key = staging_object_key("intake-123", "a" * 64)
    assert key == "intake-staging/intake-123/" + "a" * 64


def test_quarantine_and_staging_keys_never_collide_with_evidence_keys():
    same_id = "shared-id-0001"
    same_hash = "b" * 64
    assert object_key(same_id, same_hash) == f"evidence/{same_id}/{same_hash}"
    assert quarantine_object_key(same_id, same_hash) == f"quarantine/{same_id}/{same_hash}"
    assert staging_object_key(same_id, same_hash) == f"intake-staging/{same_id}/{same_hash}"
    keys = {
        object_key(same_id, same_hash),
        quarantine_object_key(same_id, same_hash),
        staging_object_key(same_id, same_hash),
    }
    assert len(keys) == 3  # all three shapes are distinguishable by key alone


def test_prefixed_object_key_rejects_arbitrary_prefix():
    with pytest.raises(ValueError):
        prefixed_object_key("not-an-allowed-prefix", "x", "a" * 64)


def test_parse_prefixed_object_key_round_trips():
    key = quarantine_object_key("intake-abc", "c" * 64)
    parsed = parse_prefixed_object_key(key)
    assert parsed == ("quarantine", "intake-abc", "c" * 64)


def test_parse_prefixed_object_key_returns_none_for_foreign_shape():
    assert parse_prefixed_object_key("evidence/x/y") is None
    assert parse_prefixed_object_key("not-a-key-at-all") is None


# ---------------------------------------------------------------------
# Behavioural contract, parametrised across both store implementations
# ---------------------------------------------------------------------


@pytest.fixture(params=["memory", pytest.param("minio", marks=requires_live_minio)])
def any_store(request, minio_config):
    if request.param == "memory":
        return InMemoryObjectStore()
    return MinIOObjectStore(minio_config)


def test_put_prefixed_quarantine_returns_the_documented_key_shape(any_store, intake_id):
    data = b"synthetic quarantined evidence bytes for WI-2"
    content_hash = _content_hash(data)

    key = any_store.put_prefixed("quarantine", intake_id, content_hash, data)

    assert key == quarantine_object_key(intake_id, content_hash["value"])
    assert any_store.get(key) == data


def test_put_prefixed_staging_returns_the_documented_key_shape(any_store, intake_id):
    data = b"synthetic staged ACCEPTED intake bytes for WI-2"
    content_hash = _content_hash(data)

    key = any_store.put_prefixed("intake-staging", intake_id, content_hash, data)

    assert key == staging_object_key(intake_id, content_hash["value"])
    assert any_store.get(key) == data


def test_put_prefixed_same_bytes_twice_is_an_idempotent_noop(any_store, intake_id):
    data = b"identical quarantined bytes, put twice"
    content_hash = _content_hash(data)

    first_key = any_store.put_prefixed("quarantine", intake_id, content_hash, data)
    second_key = any_store.put_prefixed("quarantine", intake_id, content_hash, data)

    assert first_key == second_key
    assert any_store.get(first_key) == data


def test_put_prefixed_different_bytes_at_same_key_raises_immutability_violation(any_store, intake_id):
    """Bypass `put_prefixed()`'s own hash check by writing DIFFERENT
    bytes directly at the key a given (prefix, intake_id, content_hash)
    would derive, exactly as `test_minio_store.py`'s own immutability
    test does for `put()`. Then call `put_prefixed()` with the bytes
    that genuinely match content_hash — since something different
    already sits at that key, this must raise, not silently overwrite
    (PID §17/§21)."""
    real_data = b"the correct, hash-matching quarantined bytes"
    content_hash = _content_hash(real_data)
    key = quarantine_object_key(intake_id, content_hash["value"])
    pre_existing = b"pre-existing different bytes already occupying this key"

    if isinstance(any_store, InMemoryObjectStore):
        any_store._objects[key] = pre_existing  # noqa: SLF001 - test-only
    else:
        any_store._client.put_object(  # noqa: SLF001 - test-only
            Bucket=any_store._bucket, Key=key, Body=pre_existing  # gitleaks:allow
        )

    with pytest.raises(ImmutabilityViolationError):
        any_store.put_prefixed("quarantine", intake_id, content_hash, real_data)


def test_put_prefixed_rejects_mismatched_content_hash(any_store, intake_id):
    data = b"some bytes"
    wrong_hash = _content_hash(b"totally different bytes")
    with pytest.raises(IntegrityError):
        any_store.put_prefixed("quarantine", intake_id, wrong_hash, data)


def test_quarantine_and_staging_are_independent_namespaces_for_the_same_id_and_bytes(any_store, intake_id):
    """The same intake_id/content_hash/bytes stored under BOTH prefixes
    must not collide — proving the prefix genuinely partitions the
    key namespace (PID §22)."""
    data = b"bytes that happen to exist in both a quarantine and staging scenario"
    content_hash = _content_hash(data)

    quarantine_key = any_store.put_prefixed("quarantine", intake_id, content_hash, data)
    staging_key = any_store.put_prefixed("intake-staging", intake_id, content_hash, data)

    assert quarantine_key != staging_key
    assert any_store.get(quarantine_key) == data
    assert any_store.get(staging_key) == data


def test_get_on_quarantine_key_detects_corruption_via_its_own_encoded_hash(any_store, intake_id):
    """`get()` must self-check a quarantine/staging reference exactly
    like it already does for a canonical `evidence/` one (PID §18's
    corruption-detection doctrine extended to the new key shapes)."""
    real_data = b"correct quarantined bytes"
    content_hash = _content_hash(real_data)
    key = any_store.put_prefixed("quarantine", intake_id, content_hash, real_data)

    if isinstance(any_store, InMemoryObjectStore):
        any_store._objects[key] = b"corrupted bytes swapped in directly"  # noqa: SLF001 - test-only
    else:
        # Bypass put_prefixed()'s own check via a raw boto3 call, exactly
        # as test_minio_store.py's immutability test does.
        any_store._client.put_object(  # noqa: SLF001 - test-only
            Bucket=any_store._bucket, Key=key, Body=b"corrupted bytes swapped in directly"
        )

    with pytest.raises(IntegrityError):
        any_store.get(key)


def test_get_missing_prefixed_key_raises_not_found(any_store, intake_id):
    with pytest.raises(NotFoundError):
        any_store.get(quarantine_object_key(intake_id, "f" * 64))
