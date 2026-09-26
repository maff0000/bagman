"""``scripts/migrate_object_store.py`` regression tests (CD-6 migration-
safety hardening delta, 2026-09-25) — REAL disposable S3-compatible
containers (two independent SeaweedFS 4.47 instances, source and
destination) and the REAL, already-migrated disposable Postgres this
directory's own ``postgres_container``/``_clean_tables`` fixtures
already provide — never mocks, never a fake in-memory S3.

The defect this hardening delta closes: the original checkpoint
implementation treated ``key in checkpoint_done`` as skip authority —
a checkpointed key was never re-verified against the destination, so a
destination object lost or altered out-of-band between runs would be
silently missed forever. Every test below exercises the corrected
doctrine: a checkpoint is only ever a revalidation HINT, never proof
that the destination still holds a key now (see
``scripts/migrate_object_store.py``'s own module docstring, "Checkpoint
doctrine").

Most tests below use an EMPTY database (zero ``DB_REFERENCED_KEYS``) —
deliberately, since these tests are about source/destination bucket
reconciliation and checkpoint semantics, not about the DB-reference
inventory (which the full disposable production-shape proof,
``test_disposable_production_shape_reproof_all_reconciliation_fields``,
exercises with real seeded rows instead).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Iterator

import boto3
import pytest

from persistence.postgres import session as pg_session

pytestmark = pytest.mark.usefixtures("postgres_container")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MIGRATE_SCRIPT = REPO_ROOT / "scripts" / "migrate_object_store.py"

# `scripts/` has no `__init__.py` (a plain script directory, not a
# package) — inserted onto sys.path the same way the script itself
# inserts REPO_ROOT for ITS OWN sibling imports, so the one test below
# that needs to call `run()` in-process (to deterministically
# monkeypatch `list_all_keys` — impossible across the subprocess
# boundary every other test in this file uses) can `import
# migrate_object_store` as a bare module.
sys.path.insert(0, str(REPO_ROOT / "scripts"))

SEAWEEDFS_IMAGE = (
    "ghcr.io/chrislusf/seaweedfs:4.47"
    "@sha256:ce9e796f1fe6f06968f4c04bdaf8f678dad9c8acdfef3d244133d71bfa6bf882"
)

SOURCE_CONTAINER = "bagman-test-migrate-source-wi"
DEST_CONTAINER = "bagman-test-migrate-dest-wi"
SOURCE_HOST_PORT = "19030"
DEST_HOST_PORT = "19031"


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def _wait_for_s3(port: str, timeout_s: float = 60.0) -> None:
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                if response.status in (200, 403):
                    return
        except urllib.error.HTTPError as exc:
            if exc.code in (200, 403):
                return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    raise RuntimeError(f"disposable SeaweedFS on port {port} did not become ready within {timeout_s}s")


def _start_seaweedfs(tmp_path_factory: pytest.TempPathFactory, name: str, host_port: str, access_key: str, secret_key: str) -> None:
    _docker("rm", "-f", name, check=False)
    secret_dir = tmp_path_factory.mktemp(f"{name}-secret")
    s3_config = secret_dir / "s3.json"
    s3_config.write_text(
        json.dumps(
            {
                "identities": [
                    {
                        "name": name,
                        "credentials": [{"accessKey": access_key, "secretKey": secret_key}],
                        "actions": ["Admin", "Read", "List", "Write"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    s3_config.chmod(0o644)

    _docker(
        "run", "-d", "--name", name,
        "-v", f"{s3_config}:/etc/seaweedfs/s3.json:ro",
        "-p", f"127.0.0.1:{host_port}:9000",
        SEAWEEDFS_IMAGE,
        "server", "-dir=/data", "-ip=127.0.0.1", "-ip.bind=127.0.0.1",
        "-master=true", "-volume=true", "-filer=true", "-s3=true",
        "-s3.port=9000", "-s3.ip.bind=0.0.0.0", "-s3.config=/etc/seaweedfs/s3.json",
        "-s3.port.iceberg=0", "-s3.port.lance=0", "-volume.max=0", "-webdav=false",
    )
    _wait_for_s3(host_port)


@pytest.fixture(scope="session")
def migrate_test_stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict]:
    """Two independent, disposable SeaweedFS 4.47 containers (source +
    destination) for the life of this test session — buckets are
    created fresh per-test (see ``fresh_buckets`` below) so tests never
    share object-store state despite sharing these two containers."""
    source_access_key, source_secret_key = "migsrc", "migsrc-" + uuid.uuid4().hex
    dest_access_key, dest_secret_key = "migdst", "migdst-" + uuid.uuid4().hex

    _start_seaweedfs(tmp_path_factory, SOURCE_CONTAINER, SOURCE_HOST_PORT, source_access_key, source_secret_key)
    _start_seaweedfs(tmp_path_factory, DEST_CONTAINER, DEST_HOST_PORT, dest_access_key, dest_secret_key)

    secret_dir = tmp_path_factory.mktemp("migrate-tool-secrets")
    paths = {}
    for label, value in (
        ("source_access_key", source_access_key), ("source_secret_key", source_secret_key),
        ("dest_access_key", dest_access_key), ("dest_secret_key", dest_secret_key),
    ):
        p = secret_dir / label
        p.write_text(value, encoding="utf-8")
        p.chmod(0o600)
        paths[label] = p

    try:
        yield {
            "source_endpoint": f"http://127.0.0.1:{SOURCE_HOST_PORT}",
            "dest_endpoint": f"http://127.0.0.1:{DEST_HOST_PORT}",
            "source_access_key": source_access_key, "source_secret_key": source_secret_key,
            "dest_access_key": dest_access_key, "dest_secret_key": dest_secret_key,
            "paths": paths,
        }
    finally:
        _docker("rm", "-f", SOURCE_CONTAINER, check=False)
        _docker("rm", "-f", DEST_CONTAINER, check=False)


@pytest.fixture
def fresh_buckets(migrate_test_stack: dict) -> Iterator[dict]:
    """A fresh, uniquely-named bucket pair (source + destination) for
    one test — created here, never reused, so tests are independent
    despite sharing the two long-lived containers above."""
    bucket = f"migtest-{uuid.uuid4().hex[:12]}"
    source_client = boto3.client(
        "s3", endpoint_url=migrate_test_stack["source_endpoint"],
        aws_access_key_id=migrate_test_stack["source_access_key"],
        aws_secret_access_key=migrate_test_stack["source_secret_key"],
        region_name="us-east-1",
    )
    dest_client = boto3.client(
        "s3", endpoint_url=migrate_test_stack["dest_endpoint"],
        aws_access_key_id=migrate_test_stack["dest_access_key"],
        aws_secret_access_key=migrate_test_stack["dest_secret_key"],
        region_name="us-east-1",
    )
    source_client.create_bucket(Bucket=bucket)
    dest_client.create_bucket(Bucket=bucket)
    yield {
        "bucket": bucket,
        "source_client": source_client,
        "dest_client": dest_client,
        **migrate_test_stack,
    }


@pytest.fixture
def db_dsn_file(tmp_path: Path) -> Path:
    """A DSN file pointing at the SAME disposable Postgres
    ``postgres_container``/``_clean_tables`` already provide, migrated
    to head and truncated clean before this test — this tool's own
    ``--db-dsn-file`` mechanism, never a raw DSN on a command line.

    ``pg_session.get_database_url()`` returns a SQLAlchemy-style URL
    (``postgresql+psycopg://...``, a SQLAlchemy driver spec); plain
    ``psycopg.connect()`` — which is what this migration tool uses,
    deliberately, since it is a small standalone script with no
    SQLAlchemy dependency of its own — expects a bare ``postgresql://``
    URL. Strip the ``+psycopg`` driver suffix, nothing else changes.
    """
    dsn_path = tmp_path / "db_dsn"
    sqlalchemy_url = pg_session.get_database_url()
    psycopg_dsn = sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)
    dsn_path.write_text(psycopg_dsn, encoding="utf-8")
    return dsn_path


def _run_migrate(fresh_buckets: dict, db_dsn_file: Path, checkpoint_file: Path) -> tuple[int, dict]:
    env = dict(os.environ)
    result = subprocess.run(
        [
            sys.executable, str(MIGRATE_SCRIPT),
            "--source-endpoint-url", fresh_buckets["source_endpoint"],
            "--source-access-key-file", str(fresh_buckets["paths"]["source_access_key"]),
            "--source-secret-key-file", str(fresh_buckets["paths"]["source_secret_key"]),
            "--source-bucket", fresh_buckets["bucket"],
            "--destination-endpoint-url", fresh_buckets["dest_endpoint"],
            "--destination-access-key-file", str(fresh_buckets["paths"]["dest_access_key"]),
            "--destination-secret-key-file", str(fresh_buckets["paths"]["dest_secret_key"]),
            "--destination-bucket", fresh_buckets["bucket"],
            "--db-dsn-file", str(db_dsn_file),
            "--checkpoint-file", str(checkpoint_file),
        ],
        capture_output=True, text=True, env=env, timeout=60,
    )
    # Progress lines go to stderr (`file=sys.stderr` in the tool); the
    # JSON report is the ONLY thing the tool ever prints to stdout, so
    # stdout is parsed as-is, not line-scraped.
    assert result.stdout.strip(), f"expected a JSON report on stdout; stderr was:\n{result.stderr}"
    report = json.loads(result.stdout)
    return result.returncode, report


def _put(client, bucket: str, key: str, data: bytes) -> None:
    client.put_object(Bucket=bucket, Key=key, Body=data)


def _get(client, bucket: str, key: str) -> bytes:
    return client.get_object(Bucket=bucket, Key=key)["Body"].read()


def _evidence_key(evidence_id: str, data: bytes) -> str:
    """A genuine `object_key()`-shaped canonical key — `evidence/<id>/
    <hash>`, where `<hash>` is the REAL sha256 of `data`. Using a
    placeholder like `evidence/a/aaaa` (an obviously-fake hash) would
    make `expected_hash_from_reference()` correctly flag every put as
    "source-side corruption", since that function has no way to know
    the placeholder was never meant to be a real hash — these tests
    are about checkpoint/reconciliation semantics, not about that
    (already separately tested) corruption-detection path, so every
    key here must be genuinely well-formed."""
    import hashlib

    return f"evidence/{evidence_id}/{hashlib.sha256(data).hexdigest()}"


# ---------------------------------------------------------------------
# §10 — checkpoint destination loss: the exact defect this delta closes
# ---------------------------------------------------------------------


def test_checkpoint_destination_loss_is_detected_and_recopied(fresh_buckets, db_dsn_file, tmp_path):
    src, dst, bucket = fresh_buckets["source_client"], fresh_buckets["dest_client"], fresh_buckets["bucket"]
    key_a, data_a = _evidence_key("a", b"payload-a"), b"payload-a"
    key_b, data_b = _evidence_key("b", b"payload-b"), b"payload-b"
    key_c, data_c = _evidence_key("c", b"payload-c"), b"payload-c"
    _put(src, bucket, key_a, data_a)
    _put(src, bucket, key_b, data_b)
    _put(src, bucket, key_c, data_c)

    checkpoint = tmp_path / "checkpoint.jsonl"
    rc, report = _run_migrate(fresh_buckets, db_dsn_file, checkpoint)
    assert rc == 0, report
    assert report["migrated_count"] == 3
    assert report["all_key_sets_equal"] is True

    # Delete B from the destination OUTSIDE the migration tool.
    dst.delete_object(Bucket=bucket, Key=key_b)

    # Rerun using the SAME checkpoint (which still claims B is done).
    rc2, report2 = _run_migrate(fresh_buckets, db_dsn_file, checkpoint)
    assert rc2 == 0, report2
    assert report2["failure_count"] == 0
    # A and C were checkpointed and found still byte-identical.
    assert report2["verified_via_checkpoint_count"] == 2
    # B was checkpointed but found MISSING at the destination — must be
    # transparently recopied, not silently skipped.
    assert report2["remigrated_checkpoint_missing_count"] == 1
    assert report2["migrated_count"] == 0
    assert report2["all_key_sets_equal"] is True
    assert report2["missing_from_destination_count"] == 0
    assert report2["extra_in_destination_count"] == 0

    assert _get(dst, bucket, key_b) == data_b


# ---------------------------------------------------------------------
# §11 — checkpoint conflicting destination: never overwrite
# ---------------------------------------------------------------------


def test_checkpoint_conflicting_destination_fails_never_overwrites(fresh_buckets, db_dsn_file, tmp_path):
    src, dst, bucket = fresh_buckets["source_client"], fresh_buckets["dest_client"], fresh_buckets["bucket"]
    key_a = _evidence_key("a", b"original-payload-a")
    _put(src, bucket, key_a, b"original-payload-a")

    checkpoint = tmp_path / "checkpoint.jsonl"
    rc, report = _run_migrate(fresh_buckets, db_dsn_file, checkpoint)
    assert rc == 0, report
    assert report["migrated_count"] == 1

    # Alter the destination copy to different bytes, outside the tool.
    dst.put_object(Bucket=bucket, Key=key_a, Body=b"TAMPERED-different-bytes")

    rc2, report2 = _run_migrate(fresh_buckets, db_dsn_file, checkpoint)
    assert rc2 != 0
    assert report2["failure_count"] == 1
    assert key_a in {f["key"] for f in report2["failures"]}
    reason = next(f["reason"] for f in report2["failures"] if f["key"] == key_a)
    assert "DIFFERENT bytes" in reason

    # Never overwritten — the tampered bytes remain exactly as tampered.
    assert _get(dst, bucket, key_a) == b"TAMPERED-different-bytes"


# ---------------------------------------------------------------------
# §12 — destination extra key: never silently garbage-collected
# ---------------------------------------------------------------------


def test_destination_extra_key_fails_migration(fresh_buckets, db_dsn_file, tmp_path):
    src, dst, bucket = fresh_buckets["source_client"], fresh_buckets["dest_client"], fresh_buckets["bucket"]
    _put(src, bucket, _evidence_key("a", b"payload-a"), b"payload-a")
    _put(src, bucket, _evidence_key("b", b"payload-b"), b"payload-b")
    # A key that exists ONLY at the destination, never in source — the
    # migration tool never GETs/hash-verifies a destination-only key
    # (it is only ever consulted for the final set-reconciliation
    # diff), so this one deliberately keeps a fake, non-hash-shaped
    # value — it must never be processed as if it were a real object.
    _put(dst, bucket, "evidence/extra/eeee", b"unexpected-preexisting-destination-object")

    checkpoint = tmp_path / "checkpoint.jsonl"
    rc, report = _run_migrate(fresh_buckets, db_dsn_file, checkpoint)

    assert rc != 0
    assert report["extra_in_destination_count"] == 1
    assert report["extra_in_destination_keys"] == ["evidence/extra/eeee"]
    assert report["all_key_sets_equal"] is False
    # A and B still migrated correctly — this is a set-reconciliation
    # failure, not a per-key copy failure.
    assert report["failure_count"] == 0
    assert report["migrated_count"] == 2
    # The extra object itself is untouched, never deleted.
    assert _get(dst, bucket, "evidence/extra/eeee") == b"unexpected-preexisting-destination-object"


# ---------------------------------------------------------------------
# §13 — source changes during the run
# ---------------------------------------------------------------------


def test_source_changed_during_migration_fails_even_if_copies_were_correct(fresh_buckets, db_dsn_file, tmp_path, monkeypatch):
    """Runs the tool IN-PROCESS (not via subprocess, unlike every other
    test in this file) — this is the one scenario that needs a
    deterministic mid-run source mutation, which is only possible by
    monkeypatching `list_all_keys` in the same process that calls
    `run()`; a real timing race across the subprocess boundary every
    other test uses would be flaky by construction."""
    import argparse

    import migrate_object_store as tool

    src, bucket = fresh_buckets["source_client"], fresh_buckets["bucket"]
    _put(src, bucket, _evidence_key("a", b"payload-a"), b"payload-a")

    real_list_all_keys = tool.list_all_keys
    call_count = {"n": 0}

    def _fake_list_all_keys(client, bkt):
        call_count["n"] += 1
        keys = real_list_all_keys(client, bkt)
        if call_count["n"] >= 2 and bkt == bucket and client is not fresh_buckets["dest_client"]:
            # Only perturb SOURCE enumerations (endpoint matches
            # source), and only from the 2nd source call onward (the
            # 1st is the initial enumeration, which must stay
            # unperturbed so the migration itself proceeds normally).
            return keys | {"evidence/injected-after-initial/ffff"}
        return keys

    monkeypatch.setattr(tool, "list_all_keys", _fake_list_all_keys)

    checkpoint = tmp_path / "checkpoint.jsonl"
    args = argparse.Namespace(
        source_endpoint_url=fresh_buckets["source_endpoint"],
        source_access_key_file=str(fresh_buckets["paths"]["source_access_key"]),
        source_secret_key_file=str(fresh_buckets["paths"]["source_secret_key"]),
        source_bucket=bucket, source_region="us-east-1",
        destination_endpoint_url=fresh_buckets["dest_endpoint"],
        destination_access_key_file=str(fresh_buckets["paths"]["dest_access_key"]),
        destination_secret_key_file=str(fresh_buckets["paths"]["dest_secret_key"]),
        destination_bucket=bucket, destination_region="us-east-1",
        db_dsn_file=str(db_dsn_file), checkpoint_file=str(checkpoint),
    )
    rc, report = tool.run(args)

    assert rc != 0
    assert report["source_changed_during_migration"] is True
    assert report["source_key_count_initial"] == 1
    assert report["source_key_count_final"] == 2
    # No false success merely because the one real key copied fine.
    assert report["failure_count"] == 0
    assert report["migrated_count"] == 1


# ---------------------------------------------------------------------
# §14 — normal interrupted/resume proof
# ---------------------------------------------------------------------


def test_partially_migrated_destination_plus_checkpoint_safely_completes(fresh_buckets, db_dsn_file, tmp_path):
    src, dst, bucket = fresh_buckets["source_client"], fresh_buckets["dest_client"], fresh_buckets["bucket"]
    key_a, data_a = _evidence_key("a", b"payload-a"), b"payload-a"
    key_b, data_b = _evidence_key("b", b"payload-b"), b"payload-b"
    key_c, data_c = _evidence_key("c", b"payload-c"), b"payload-c"
    _put(src, bucket, key_a, data_a)
    _put(src, bucket, key_b, data_b)
    _put(src, bucket, key_c, data_c)

    # Simulate a crash mid-run: A was already copied+checkpointed by a
    # prior (interrupted) invocation; B and C were never copied.
    checkpoint = tmp_path / "checkpoint.jsonl"
    dst.put_object(Bucket=bucket, Key=key_a, Body=data_a)
    checkpoint.write_text(json.dumps({"key": key_a, "status": "done", "outcome": "migrated", "bytes": len(data_a)}) + "\n", encoding="utf-8")

    rc, report = _run_migrate(fresh_buckets, db_dsn_file, checkpoint)
    assert rc == 0, report
    assert report["failure_count"] == 0
    assert report["verified_via_checkpoint_count"] == 1  # A, revalidated
    assert report["migrated_count"] == 2  # B and C, freshly copied
    assert report["all_key_sets_equal"] is True
    for key, payload in ((key_a, data_a), (key_b, data_b), (key_c, data_c)):
        assert _get(dst, bucket, key) == payload


# ---------------------------------------------------------------------
# §15 — fresh checkpoint against an already-fully-migrated destination
# ---------------------------------------------------------------------


def test_fresh_checkpoint_against_already_migrated_destination_is_a_safe_noop(fresh_buckets, db_dsn_file, tmp_path):
    src, dst, bucket = fresh_buckets["source_client"], fresh_buckets["dest_client"], fresh_buckets["bucket"]
    key_a, data_a = _evidence_key("a", b"payload-a"), b"payload-a"
    key_b, data_b = _evidence_key("b", b"payload-b"), b"payload-b"
    _put(src, bucket, key_a, data_a)
    _put(src, bucket, key_b, data_b)

    checkpoint1 = tmp_path / "checkpoint1.jsonl"
    rc, report = _run_migrate(fresh_buckets, db_dsn_file, checkpoint1)
    assert rc == 0, report
    assert report["migrated_count"] == 2

    # A completely FRESH checkpoint file (no prior knowledge at all).
    checkpoint2 = tmp_path / "checkpoint2.jsonl"
    rc2, report2 = _run_migrate(fresh_buckets, db_dsn_file, checkpoint2)
    assert rc2 == 0, report2
    assert report2["failure_count"] == 0
    assert report2["migrated_count"] == 0
    assert report2["already_identical_count"] == 2
    assert report2["verified_via_checkpoint_count"] == 0
    assert report2["all_key_sets_equal"] is True
    # Zero destructive writes — both objects still byte-identical.
    assert _get(dst, bucket, key_a) == data_a
    assert _get(dst, bucket, key_b) == data_b


# ---------------------------------------------------------------------
# MISSING_FROM_SOURCE still hard-stops before any copying (regression
# guard — this behavior predates this delta and must not regress).
# ---------------------------------------------------------------------


def test_missing_from_source_still_hard_stops_zero_copying(fresh_buckets, db_dsn_file, tmp_path):
    engine = pg_session.get_engine()
    import sqlalchemy as sa

    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO governed_entities (entity_id, entity_type, canonical_name, display_name, status, created_at, metadata) "
            "VALUES (gen_random_uuid(), 'PERSON', 'x', 'x', 'ACTIVE', now(), '{}')"
        ))
        entity_id = conn.execute(sa.text("SELECT entity_id FROM governed_entities LIMIT 1")).scalar()
        conn.execute(sa.text(
            "INSERT INTO sources (source_id, source_type, provider, status, metadata) "
            "VALUES (gen_random_uuid(), 'MANUAL_UPLOAD', 'test', 'ACTIVE', '{}')"
        ))
        source_id = conn.execute(sa.text("SELECT source_id FROM sources LIMIT 1")).scalar()
        conn.execute(sa.text(
            "INSERT INTO evidence_items "
            "(evidence_id, entity_id, evidence_type, source_id, observed_at, received_at, "
            " content_hash, mime_type, size_bytes, status, created_at, storage_reference, metadata) "
            "VALUES (gen_random_uuid(), :entity_id, 'DOCUMENT', :source_id, now(), now(), "
            " '{}', 'application/octet-stream', 1, 'REGISTERED', now(), "
            " 'evidence/does-not-exist/deadbeef', '{}')"
        ), {"entity_id": entity_id, "source_id": source_id})

    checkpoint = tmp_path / "checkpoint.jsonl"
    rc, report = _run_migrate(fresh_buckets, db_dsn_file, checkpoint)
    assert rc != 0
    assert report["error"] == "MISSING_FROM_SOURCE"
    assert report["missing_reference_keys"] == ["evidence/does-not-exist/deadbeef"]
    # Zero copying performed.
    dest_keys = [o["Key"] for o in fresh_buckets["dest_client"].list_objects_v2(Bucket=fresh_buckets["bucket"]).get("Contents", [])]
    assert dest_keys == []


# ---------------------------------------------------------------------
# §16 — disposable production-shape re-proof (all reconciliation fields)
# ---------------------------------------------------------------------


def test_disposable_production_shape_reproof_all_reconciliation_fields(fresh_buckets, db_dsn_file, tmp_path):
    """The full four-key shape: canonical, staged/intake, quarantine/
    prefixed, and an unreferenced canonical key — proving every
    reconciliation field the hardening delta added, end to end, in one
    real run against real disposable containers."""
    import hashlib

    import sqlalchemy as sa

    src = fresh_buckets["source_client"]
    bucket = fresh_buckets["bucket"]

    def seeded(label: str, key_fn):
        data = f"prod-shape-{label}-".encode() + b"p" * 24
        h = hashlib.sha256(data).hexdigest()
        key = key_fn(h)
        src.put_object(Bucket=bucket, Key=key, Body=data)
        return key, h, data

    canonical_key, canonical_hash, _ = seeded("canonical-referenced", lambda h: f"evidence/evid-x1/{h}")
    unreferenced_key, _, _ = seeded("canonical-unreferenced", lambda h: f"evidence/evid-x2/{h}")
    staged_key, staged_hash, _ = seeded("staged-referenced", lambda h: f"intake-staging/intake-x1/{h}")
    quarantine_key, _, _ = seeded("quarantine-unreferenced", lambda h: f"quarantine/intake-x2/{h}")

    engine = pg_session.get_engine()
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO governed_entities (entity_id, entity_type, canonical_name, display_name, status, created_at, metadata) "
            "VALUES (gen_random_uuid(), 'PERSON', 'x', 'x', 'ACTIVE', now(), '{}')"
        ))
        entity_id = conn.execute(sa.text("SELECT entity_id FROM governed_entities LIMIT 1")).scalar()
        conn.execute(sa.text(
            "INSERT INTO sources (source_id, source_type, provider, status, metadata) "
            "VALUES (gen_random_uuid(), 'MANUAL_UPLOAD', 'test', 'ACTIVE', '{}')"
        ))
        source_id = conn.execute(sa.text("SELECT source_id FROM sources LIMIT 1")).scalar()
        conn.execute(sa.text(
            "INSERT INTO evidence_items "
            "(evidence_id, entity_id, evidence_type, source_id, observed_at, received_at, "
            " content_hash, mime_type, size_bytes, status, created_at, storage_reference, metadata) "
            "VALUES (gen_random_uuid(), :entity_id, 'DOCUMENT', :source_id, now(), now(), "
            " :content_hash, 'application/octet-stream', 1, 'REGISTERED', now(), :storage_reference, '{}')"
        ), {"entity_id": entity_id, "source_id": source_id,
            "content_hash": json.dumps({"algorithm": "SHA-256", "value": canonical_hash}),
            "storage_reference": canonical_key})
        conn.execute(sa.text(
            "INSERT INTO intake_records (intake_id, source_id, status, received_at, content_hash, correlation_id, metadata) "
            "VALUES (gen_random_uuid(), :source_id, 'ACCEPTED', now(), :content_hash, gen_random_uuid(), :metadata)"
        ), {"source_id": source_id,
            "content_hash": json.dumps({"algorithm": "SHA-256", "value": staged_hash}),
            "metadata": json.dumps({"storage_reference": staged_key})})

    checkpoint = tmp_path / "checkpoint.jsonl"
    rc, report = _run_migrate(fresh_buckets, db_dsn_file, checkpoint)

    assert rc == 0, report
    assert report["source_key_count_initial"] == 4
    assert report["source_key_count_final"] == 4
    assert report["source_changed_during_migration"] is False
    assert report["destination_key_count"] == 4
    assert report["canonical_reference_count"] == 2
    assert report["missing_reference_count"] == 0
    assert report["missing_from_destination_count"] == 0
    assert report["extra_in_destination_count"] == 0
    assert report["all_key_sets_equal"] is True
    assert report["all_destination_bytes_verified"] is True
    assert set(report["unreferenced_source_keys"]) == {unreferenced_key, quarantine_key}
    assert report["migrated_count"] == 4
    assert report["failure_count"] == 0

    dst = fresh_buckets["dest_client"]
    for key in (canonical_key, unreferenced_key, staged_key, quarantine_key):
        src_bytes = _get(src, bucket, key)
        dst_bytes = _get(dst, bucket, key)
        assert src_bytes == dst_bytes
        assert hashlib.sha256(src_bytes).hexdigest() == hashlib.sha256(dst_bytes).hexdigest()
