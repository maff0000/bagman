"""``MinIOObjectStore`` — the ``boto3``-based, MinIO/S3-compatible
implementation of :class:`persistence.objects.store.EvidenceObjectStore`
(CD-3 WI-2, PID §15/§16).

Connection configuration (endpoint URL, access key, secret key, bucket
name) is supplied explicitly by the caller via :class:`MinIOConfig`.
This module never reads ``/srv/bagman-secrets/`` or any particular
env-var scheme itself — that composition-root wiring belongs to a
later CD-3 work item (WI-3), which can construct a ``MinIOConfig``
however it chooses.

No public method here ever lets a raw ``boto3``/``botocore`` exception
escape (PID §57): every one is caught and re-raised as the canonical
:class:`core.errors.StorageError`, :class:`core.errors.NotFoundError`,
:class:`core.errors.IntegrityError`, or
:class:`core.errors.ImmutabilityViolationError`. No exception message
this module raises or logs ever includes ``access_key``/``secret_key``
(PID §31) — botocore's own auth/network error strings do not embed
credentials either, but ``_safe_str`` centralises and caps that
guarantee in one place regardless.
"""
from __future__ import annotations

import dataclasses
from typing import Mapping

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from core.errors import ImmutabilityViolationError, IntegrityError, NotFoundError, StorageError
from persistence.objects.store import (
    EvidenceObjectStore,
    compute_sha256,
    normalize_hash_value,
    object_key,
    parse_object_key,
)

#: botocore ClientError codes meaning "the key/bucket doesn't exist".
#: Real S3 uses "NoSuchKey"/"NoSuchBucket"; MinIO's HEAD responses
#: report a bare "404" (no body to carry a symbolic code).
_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NoSuchBucket", "NotFound"})


def _safe_str(exc: Exception) -> str:
    """A capped, message-only rendering of a boto3/botocore exception
    for inclusion in a canonical error's message — never the exception
    object itself, and truncated defensively so a verbose provider
    error can never balloon a log line or leak more than intended.
    """
    return str(exc)[:500]


@dataclasses.dataclass(frozen=True)
class MinIOConfig:
    """Plain connection configuration for a :class:`MinIOObjectStore`.

    No secret is read from disk or environment by this module itself
    (PID §29/§31) — a later work item's composition root supplies
    these however it obtains them (e.g. a mounted secret file).
    """

    endpoint_url: str
    access_key: str
    secret_key: str
    bucket: str
    region_name: str = "us-east-1"


class MinIOObjectStore(EvidenceObjectStore):
    """``EvidenceObjectStore`` backed by a MinIO (or other
    S3-compatible) endpoint via ``boto3``."""

    def __init__(self, config: MinIOConfig) -> None:
        self._bucket = config.bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=config.endpoint_url,
            aws_access_key_id=config.access_key,
            aws_secret_access_key=config.secret_key,
            region_name=config.region_name,
        )
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        """Idempotently ensure the configured bucket exists — a human
        need not have pre-created it, and this must not fail loudly if
        it already does (PID §15)."""
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in _NOT_FOUND_CODES:
                try:
                    self._client.create_bucket(Bucket=self._bucket)
                except (BotoCoreError, ClientError) as create_exc:
                    raise StorageError(
                        f"could not create bucket '{self._bucket}': {_safe_str(create_exc)}"
                    ) from create_exc
                return
            raise StorageError(
                f"could not verify bucket '{self._bucket}': {_safe_str(exc)}"
            ) from exc
        except BotoCoreError as exc:
            raise StorageError(
                f"could not reach object store to verify bucket '{self._bucket}': "
                f"{_safe_str(exc)}"
            ) from exc

    # ------------------------------------------------------------------
    # EvidenceObjectStore
    # ------------------------------------------------------------------

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

        try:
            existing = self._get_bytes(key)
        except NotFoundError:
            existing = None  # nothing stored yet at this key — the ordinary case

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

        try:
            self._client.put_object(Bucket=self._bucket, Key=key, Body=data)
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"put() failed for key '{key}': {_safe_str(exc)}") from exc

        return key

    def get(self, storage_reference: str) -> bytes:
        data = self._get_bytes(storage_reference)
        parsed = parse_object_key(storage_reference)
        if parsed is not None:
            _evidence_id, expected_hash = parsed
            actual_hash = compute_sha256(data)
            if actual_hash != expected_hash:
                raise IntegrityError(
                    f"corruption detected reading '{storage_reference}': the "
                    f"stored bytes hash to '{actual_hash}' but the reference's "
                    f"own key encodes '{expected_hash}'"
                )
        return data

    def exists(self, storage_reference: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=storage_reference)
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in _NOT_FOUND_CODES:
                return False
            raise StorageError(
                f"exists() failed for '{storage_reference}': {_safe_str(exc)}"
            ) from exc
        except BotoCoreError as exc:
            raise StorageError(
                f"exists() failed for '{storage_reference}': {_safe_str(exc)}"
            ) from exc

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

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _get_bytes(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in _NOT_FOUND_CODES:
                raise NotFoundError(f"no object at storage_reference '{key}'") from exc
            raise StorageError(f"get() failed for key '{key}': {_safe_str(exc)}") from exc
        except BotoCoreError as exc:
            raise StorageError(f"get() failed for key '{key}': {_safe_str(exc)}") from exc

        try:
            return response["Body"].read()
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(
                f"get() failed reading response body for key '{key}': {_safe_str(exc)}"
            ) from exc
