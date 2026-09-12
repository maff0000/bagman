"""BAGMAN evidence object storage (CD-3 WI-2, PID §15/§16).

:class:`persistence.objects.store.EvidenceObjectStore` is the abstract
storage contract; :class:`persistence.objects.minio_store.MinIOObjectStore`
is its ``boto3``-based, MinIO/S3-compatible implementation.
"""
from __future__ import annotations

from persistence.objects.store import EvidenceObjectStore, compute_sha256, object_key

__all__ = ["EvidenceObjectStore", "compute_sha256", "object_key"]
