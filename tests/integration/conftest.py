"""Shared fixtures for `tests/integration/` domain-behavior and
runtime-proof tests (PID §31 domain proofs / §43 required runtime
proof).

Unlike `tests/contract/`, these tests exercise real domain behaviour —
through `core.api.BagmanCanonicalAPI`, the way a real caller would use
it — not just raw schema shape.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pytest

from core.api import BagmanCanonicalAPI

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES_ROOT = REPO_ROOT / "tests" / "fixtures"

#: Default actor identity used across these tests (PID §14: a specific
#: actor_id, never a bare "system" literal).
ACTOR_TYPE = "SYSTEM"
ACTOR_ID = "bagman-integration-tests"


@pytest.fixture
def api() -> BagmanCanonicalAPI:
    """A fresh, isolated `BagmanCanonicalAPI` (fresh in-memory
    repositories) for each test — no state leaks between tests."""
    return BagmanCanonicalAPI()


@pytest.fixture
def utc_now() -> Callable[[], datetime]:
    return lambda: datetime.now(timezone.utc)


@pytest.fixture
def synthetic_invoice_path() -> Path:
    """The CD-2 synthetic invoice fixture (PID §30): a fabricated
    invoice from Synthetic Cloud Services Ltd to NoustAI Limited. See
    `tests/fixtures/README.md`."""
    path = FIXTURES_ROOT / "invoices" / "synthetic_cloud_services_invoice_to_noustai.txt"
    assert path.is_file(), f"expected synthetic fixture at {path}"
    return path


def sha256_hex(path: Path) -> str:
    """Lowercase hex SHA-256 digest of a file's bytes — exactly the
    shape `contracts/evidence/bagman.evidence.v1.schema.json`'s
    `content_hash.value` requires."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def synthetic_invoice_content_hash(synthetic_invoice_path: Path) -> str:
    return sha256_hex(synthetic_invoice_path)
