"""Shared fixtures for `tests/persistence/` (CD-3 WI-2, PID §41/§42).

These tests exercise `persistence.objects.minio_store.MinIOObjectStore`
against a REAL, disposable MinIO instance — never a mock — per the
WI-2 contract's explicit instruction. They are NOT run as part of the
ordinary `pytest tests/ -q` sweep unless that disposable instance is
up: every test here is skipped (not failed) if nothing answers at the
configured endpoint, so a plain `pytest tests/` in an environment with
no Docker/MinIO available still passes cleanly (this is the same
posture `.github/workflows/security.yml`'s docstring describes for
CD-2 — no live external dependency required by the ordinary suite).

Disposable MinIO container used for this work item's own verification
(started and torn down outside pytest, by hand, as WI-2's own
Forge-Engineer verification step — see the WI-2 delivery report for
the exact transcript):

    docker network create bagman-test-net-wi2

    docker run -d --name bagman-test-minio-wi2 \\
      --network bagman-test-net-wi2 \\
      -p 127.0.0.1:19000:9000 -p 127.0.0.1:19001:9001 \\
      -e MINIO_ROOT_USER=<CHOOSE-YOUR-OWN-THROWAWAY-USER> \\
      -e MINIO_ROOT_PASSWORD=<CHOOSE-YOUR-OWN-THROWAWAY-PASSWORD> \\
      quay.io/minio/minio server /data --console-address ":9001"

    # then export the same throwaway values so this file's fixtures pick
    # them up: BAGMAN_TEST_MINIO_ACCESS_KEY / BAGMAN_TEST_MINIO_SECRET_KEY

Judgment call: the WI-2 contract names the "official `minio/minio`
image"; `docker.io/minio/minio` pull was denied on this host ("pull
access denied ... repository does not exist or may require
'docker login'") — this is MinIO's own current Docker Hub posture for
that org, not a mistake in the pull command (confirmed: `alpine`
pulled from `docker.io` normally in the same environment). MinIO's own
documentation now names `quay.io/minio/minio` as its primary published
registry, so this substitutes that image, unchanged otherwise
(RELEASE tag omitted; `:latest` used since no specific point release
was mandated). Flagged here for the PL rather than silently swapped.

Host port 9000 was already in use on this host (nginx) per the WI-2
contract's own warning; 19000/19001 were checked free before use and
are only bound to 127.0.0.1, not exposed beyond the host.
"""
from __future__ import annotations

import os
import secrets
import urllib.error
import urllib.request
import uuid

import boto3
import pytest

from persistence.objects.minio_store import MinIOConfig, MinIOObjectStore

#: Throwaway credentials for this disposable test instance only — never
#: real BAGMAN runtime credentials, never read from
#: `/srv/bagman-secrets/` (WI-2 contract §4/§5).
#:
#: PL correction (post-WI-2, pre-merge-to-main): the original version of
#: this file hardcoded a literal fallback access/secret key
#: ("bagmantestwi2" / "bagmantestwi2secretpw"). Gitleaks correctly
#: flags any credential-shaped literal regardless of how low-stakes it
#: actually is (CD-1 security doctrine: no credential-shaped value ever
#: enters Git, full stop — deleting it from a later commit does not
#: remove it from history). Fixed here, before this branch was ever
#: pushed, by generating a fresh throwaway secret at import time
#: instead of writing one as a string literal — this is also exactly
#: the "ephemeral credentials generated within isolated CI/runtime
#: contexts" pattern PID §30 already sanctions.
_ENDPOINT_URL = os.environ.get("BAGMAN_TEST_MINIO_ENDPOINT", "http://127.0.0.1:19000")
_ACCESS_KEY = os.environ.get("BAGMAN_TEST_MINIO_ACCESS_KEY", "bagman-test-wi2")
_SECRET_KEY = os.environ.get("BAGMAN_TEST_MINIO_SECRET_KEY") or secrets.token_urlsafe(24)
_BUCKET = "bagman-test-wi2-objects"


def _minio_is_up() -> bool:
    try:
        with urllib.request.urlopen(f"{_ENDPOINT_URL}/minio/health/live", timeout=2) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


requires_live_minio = pytest.mark.skipif(
    not _minio_is_up(),
    reason=(
        f"no disposable MinIO answering at {_ENDPOINT_URL} — these tests "
        "require a REAL object store, not a mock (WI-2 contract); start the "
        "bagman-test-minio-wi2 container documented in this file's docstring"
    ),
)


@pytest.fixture
def bucket_name() -> str:
    return _BUCKET


@pytest.fixture
def minio_config() -> MinIOConfig:
    return MinIOConfig(
        endpoint_url=_ENDPOINT_URL,
        access_key=_ACCESS_KEY,
        secret_key=_SECRET_KEY,
        bucket=_BUCKET,
    )


@pytest.fixture
def store(minio_config: MinIOConfig) -> MinIOObjectStore:
    return MinIOObjectStore(minio_config)


@pytest.fixture
def raw_s3_client(minio_config: MinIOConfig):
    """A bare `boto3` S3 client against the same disposable instance,
    used ONLY to simulate real corruption/pre-existing-content
    scenarios by writing bytes directly, bypassing
    `MinIOObjectStore.put()`'s own hash/immutability checks entirely —
    exactly as the WI-2 contract's test plan requires."""
    return boto3.client(
        "s3",
        endpoint_url=minio_config.endpoint_url,
        aws_access_key_id=minio_config.access_key,
        aws_secret_access_key=minio_config.secret_key,
        region_name=minio_config.region_name,
    )


@pytest.fixture
def evidence_id() -> str:
    """A fresh, unique evidence_id per test so object keys never
    collide across tests sharing the one disposable bucket."""
    return str(uuid.uuid4())
