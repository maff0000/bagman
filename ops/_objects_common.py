"""Shared helpers for ``ops/backup_objects.py`` and
``ops/restore_objects.py`` (CD-3 WI-4, PID §34-35).

Connection configuration mirrors ``app/api/composition.py``'s
production wiring exactly — the same ``BAGMAN_OBJECT_STORE_*``
environment variables, the same secret-file-never-a-literal discipline
(PID §29-31), read from the same files the live ``bagman-api``
container already has mounted. Both operational scripts are meant to
run *inside* the ``bagman-api`` image (e.g. via a throwaway
``docker compose run`` container — see the repo-root ``Makefile``'s
``backup``/``restore`` targets), which already has these environment
variables and secret files provisioned exactly like the live server,
and already has ``boto3`` installed.

This module deliberately does **not** import
``persistence.objects.minio_store`` (out of this work item's authority
to modify, and its ``EvidenceObjectStore``-shaped ``put()``/``get()``
enforce domain semantics — immutability, hash-check-before-write —
that a bulk backup/restore operational tool does not want: a backup
must download *every* object regardless of hash correctness, so
corruption is detected and reported rather than silently refused, and
a restore must be able to repopulate a bucket that has just been
recreated empty, a case ``put()``'s "identical bytes -> no-op,
different bytes -> ``ImmutabilityViolationError``" logic is not shaped
for). It DOES reuse ``persistence.objects.store``'s small,
dependency-free helpers (``compute_sha256``, ``parse_object_key``) for
the same key-shape/hash-integrity checks, rather than reimplementing
them — see ``backup_objects.py``/``restore_objects.py``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import boto3

# Makes `persistence.*` importable when this module is run from a
# plain repo checkout on the host with no PYTHONPATH set (e.g. a
# developer pointing BAGMAN_OBJECT_STORE_ENDPOINT_URL at the
# docker-compose.debug-ports.yml opt-in host port). A harmless no-op
# when running inside the bagman-api container image, where
# PYTHONPATH=/app is already set explicitly by the Makefile/acceptance
# script invocation instead (see deployment/docker/api/Dockerfile's
# WORKDIR /app).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#: botocore ClientError codes meaning "the key/bucket doesn't exist" —
#: same set persistence/objects/minio_store.py uses.
NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NoSuchBucket", "NotFound"})


def read_secret_file(env_var: str, default_path: str) -> str:
    """Read a secret value from the file named by ``env_var`` (default
    ``default_path``) — never a literal in code or on a command line,
    mirroring ``app/api/composition.py``'s ``_read_secret_file``."""
    path = Path(os.environ.get(env_var, default_path))
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(
            f"could not read required secret from {env_var}={str(path)!r}: {exc}. "
            "Point the environment variable at a readable file (e.g. the same "
            "mounted Docker/Compose secret bagman-api uses)."
        ) from exc


def build_client_and_bucket():
    """Build a ``boto3`` S3 client + bucket name from the SAME
    ``BAGMAN_OBJECT_STORE_*`` environment variables and secret files
    ``app/api/composition.py``'s production composition uses — never a
    separate/duplicated credential scheme (PID §29-31)."""
    endpoint_url = os.environ["BAGMAN_OBJECT_STORE_ENDPOINT_URL"]
    access_key = read_secret_file(
        "BAGMAN_OBJECT_STORE_ACCESS_KEY_FILE", "/run/secrets/minio_root_user"
    )
    secret_key = read_secret_file(
        "BAGMAN_OBJECT_STORE_SECRET_KEY_FILE", "/run/secrets/minio_root_user_password"
    )
    region_name = os.environ.get("BAGMAN_OBJECT_STORE_REGION", "us-east-1")
    bucket = os.environ.get("BAGMAN_OBJECT_STORE_BUCKET", "bagman-evidence")

    client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region_name,
    )
    return client, bucket


def ensure_bucket(client, bucket: str) -> None:
    """Idempotently ensure ``bucket`` exists — mirrors
    ``MinIOObjectStore._ensure_bucket`` so ``restore_objects.py`` can
    run safely against a genuinely fresh/empty object store (PID §35's
    restore-into-clean-target scenario), even before ``bagman-api``'s
    own composition has necessarily created the bucket itself yet
    (composition/bucket-creation is lazy, triggered by the first
    request that touches it — see ``app/api/composition.py``)."""
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        client.head_bucket(Bucket=bucket)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in NOT_FOUND_CODES:
            client.create_bucket(Bucket=bucket)
            return
        raise
    except BotoCoreError:
        raise
