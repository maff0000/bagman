"""`MinIOObjectStore` tests against a REAL disposable MinIO (CD-3 WI-2,
PID §41/§42) — never mocks. See `tests/persistence/conftest.py` for
how the disposable instance is expected to be reachable, and for why
these tests skip (rather than fail) when it is not.
"""
from __future__ import annotations

import pytest

from core.errors import ImmutabilityViolationError, IntegrityError, StorageError
from persistence.objects.minio_store import MinIOConfig, MinIOObjectStore
from persistence.objects.store import compute_sha256, object_key
from tests.persistence.conftest import requires_live_minio

pytestmark = requires_live_minio


def _content_hash(data: bytes) -> dict:
    return {"algorithm": "SHA-256", "value": compute_sha256(data)}


# ---------------------------------------------------------------------
# put() — deterministic key, idempotent re-put, immutability guard
# ---------------------------------------------------------------------


def test_put_returns_deterministic_key_matching_documented_scheme(store, evidence_id):
    data = b"synthetic evidence bytes for WI-2 acceptance"
    content_hash = _content_hash(data)

    key = store.put(evidence_id, content_hash, data)

    assert key == object_key(evidence_id, content_hash["value"])
    assert key == f"evidence/{evidence_id}/{content_hash['value']}"


def test_put_same_bytes_twice_is_an_idempotent_noop(store, evidence_id):
    data = b"identical bytes, put twice"
    content_hash = _content_hash(data)

    first_key = store.put(evidence_id, content_hash, data)
    second_key = store.put(evidence_id, content_hash, data)

    assert first_key == second_key
    assert store.get(first_key) == data


def test_put_different_bytes_forced_to_same_key_raises_immutability_violation(
    store, raw_s3_client, evidence_id, bucket_name
):
    """Prove the immutability guard actually fires: bypass `put()`'s own
    hash check by writing DIFFERENT bytes directly at the key that a
    given (evidence_id, content_hash) pair would derive, via a raw
    `boto3` call. Then call `store.put()` with the bytes that genuinely
    match that content_hash — since something different already sits
    at that key, this must raise, not silently overwrite (PID §17).
    """
    real_data = b"the correct, hash-matching bytes"
    content_hash = _content_hash(real_data)
    key = object_key(evidence_id, content_hash["value"])

    # Bypass put() entirely: force different bytes at the key the
    # correct data will hash to.
    raw_s3_client.put_object(
        Bucket=bucket_name,
        Key=key,
        Body=b"some other, pre-existing bytes that do not match",
    )

    with pytest.raises(ImmutabilityViolationError):
        store.put(evidence_id, content_hash, real_data)

    # And the pre-existing (different) bytes must remain untouched —
    # no silent overwrite occurred.
    assert store_get_raw(raw_s3_client, bucket_name, key) == (
        b"some other, pre-existing bytes that do not match"
    )


def store_get_raw(raw_s3_client, bucket_name: str, key: str) -> bytes:
    response = raw_s3_client.get_object(Bucket=bucket_name, Key=key)
    return response["Body"].read()


def test_put_hash_mismatch_rejected_and_nothing_stored(store, evidence_id):
    data = b"bytes that do not match the claimed hash"
    wrong_hash = {"algorithm": "SHA-256", "value": compute_sha256(b"totally different bytes")}

    with pytest.raises(IntegrityError):
        store.put(evidence_id, wrong_hash, data)

    key = object_key(evidence_id, wrong_hash["value"])
    assert store.exists(key) is False


# ---------------------------------------------------------------------
# get() / exists()
# ---------------------------------------------------------------------


def test_get_retrieves_exact_bytes(store, evidence_id):
    data = bytes(range(256)) * 4  # not just text — exercise arbitrary binary content
    content_hash = _content_hash(data)

    key = store.put(evidence_id, content_hash, data)
    retrieved = store.get(key)

    assert retrieved == data
    assert isinstance(retrieved, bytes)


def test_exists_reports_presence_and_absence(store, evidence_id):
    data = b"exists() presence check"
    content_hash = _content_hash(data)
    key = object_key(evidence_id, content_hash["value"])

    assert store.exists(key) is False

    store.put(evidence_id, content_hash, data)

    assert store.exists(key) is True


# ---------------------------------------------------------------------
# verify_hash() — happy path and corruption detection
# ---------------------------------------------------------------------


def test_verify_hash_succeeds_for_genuinely_correct_content(store, evidence_id):
    data = b"genuinely correct content"
    content_hash = _content_hash(data)

    key = store.put(evidence_id, content_hash, data)

    assert store.verify_hash(key, content_hash) is True


def test_verify_hash_and_get_detect_real_corruption_and_raise_integrity_error(
    store, raw_s3_client, evidence_id, bucket_name
):
    """Store genuine content through `put()`, then corrupt the stored
    bytes via a SEPARATE raw `boto3` call (bypassing `put()` entirely,
    simulating real bit-rot/corruption at rest), and prove both
    `get()` and `verify_hash()` detect it and raise `IntegrityError`
    rather than silently returning corrupted/mismatched data.
    """
    data = b"the original, uncorrupted bytes"
    content_hash = _content_hash(data)
    key = store.put(evidence_id, content_hash, data)

    # Simulate corruption: overwrite the stored object's bytes directly,
    # without going anywhere near MinIOObjectStore.put().
    corrupted = b"the original, UNCORRUPTED bytes"  # same length, different content
    assert corrupted != data
    raw_s3_client.put_object(Bucket=bucket_name, Key=key, Body=corrupted)

    with pytest.raises(IntegrityError):
        store.get(key)

    with pytest.raises(IntegrityError):
        store.verify_hash(key, content_hash)


# ---------------------------------------------------------------------
# StorageError — unreachable backend never leaks a raw boto3 exception
# ---------------------------------------------------------------------


def test_storage_error_raised_for_unreachable_endpoint():
    unreachable_config = MinIOConfig(
        endpoint_url="http://127.0.0.1:59999",  # nothing listens here
        access_key="irrelevant",
        secret_key="irrelevant",
        bucket="irrelevant-bucket",
    )

    with pytest.raises(StorageError):
        MinIOObjectStore(unreachable_config)
