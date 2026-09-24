#!/usr/bin/env python3
"""scripts/migrate_object_store.py — bounded, resumable, safe-to-rerun
S3-to-S3 object-store migration tool (CD-6 MinIO withdrawal WO).

Context: BAGMAN's production object store was MinIO
(`quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z.hotfix.7aa24e772`),
now withdrawn upstream. This tool copies every object from a SOURCE
S3-compatible endpoint (the old MinIO) to a DESTINATION S3-compatible
endpoint (the new SeaweedFS 4.47), independent of the actual production
cutover, which is a separate, later step this tool does not perform.

Design constraints (all deliberate, not oversights):

* Credentials are NEVER accepted as raw CLI argument values — only as
  paths to files containing them (``--source-access-key-file`` etc.),
  matching BAGMAN's own PID §29-31 secret-file discipline. No
  credential value is ever written to a log line, an exception
  message, or a progress print — only file paths, bucket names, and
  endpoint URLs (which are not secrets) ever appear in output.

* Migration policy is "copy everything currently in the source
  bucket" — never filtered down to only what the database currently
  references. `DB_REFERENCED_KEYS` exists to (a) hard-stop before any
  copying if the database references a key the source bucket does not
  actually have (a serious pre-existing integrity problem this tool
  must never paper over), and (b) report (never skip/exclude)
  unreferenced source objects — quarantine objects in particular are
  expected to always be unreferenced (see PL's DB object-reference
  inventory in the work order); that is correct, not a bug.

* Resumable and safe to rerun: a checkpoint file (append-only
  JSON-lines) records one line per key that reached a verified
  terminal success (either "already identical at the destination" or
  "freshly migrated and read back byte-identical"). A crash mid-run
  loses at most the one in-flight key — everything already flushed to
  the checkpoint is skipped on the next run. Rerunning against an
  already-fully-migrated destination is a safe no-op even with a
  *fresh* checkpoint: the per-key HEAD-then-compare-bytes logic itself
  (not merely the checkpoint) is what makes this idempotent.

* Never overwrites a destination object that already holds different
  bytes than the source — the exact same immutability doctrine
  `persistence.objects.minio_store.MinIOObjectStore.put` already
  enforces, applied here to a bulk copy tool instead of a single
  logical write.

* Read-only against BAGMAN's canonical database — this tool issues
  exactly three `SELECT` statements and nothing else; it never writes
  to any BAGMAN canonical table.

* No hard-coded expected counts anywhere in this file — every number
  in the final report is computed from what was actually observed
  this run, per the work order's explicit instruction.

Usage
-----
    python3 scripts/migrate_object_store.py \\
        --source-endpoint-url http://127.0.0.1:19000 \\
        --source-access-key-file /path/to/source_access_key \\
        --source-secret-key-file /path/to/source_secret_key \\
        --source-bucket bagman-evidence \\
        --destination-endpoint-url http://127.0.0.1:19100 \\
        --destination-access-key-file /path/to/dest_access_key \\
        --destination-secret-key-file /path/to/dest_secret_key \\
        --destination-bucket bagman-evidence \\
        --db-dsn-file /path/to/db_dsn \\
        --checkpoint-file /path/to/checkpoint.jsonl

``--db-dsn-file`` names a file containing a single PostgreSQL
connection string (e.g. ``postgresql://user:password@host:5432/dbname``)
— never a literal DSN on the command line. The database is only ever
read (three plain `SELECT`s, no writes).

Prints one JSON report to stdout as this script's last line. Exits
non-zero if `MISSING_FROM_SOURCE` is non-empty (before any copying) or
if any key ended in FAILURE during the copy phase.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import boto3
import psycopg
from botocore.exceptions import BotoCoreError, ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from persistence.objects.store import compute_sha256, expected_hash_from_reference  # noqa: E402

_DEFAULT_REGION = "us-east-1"


# ---------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------


def _read_file(label: str, path: str) -> str:
    """Read a small text file (a secret, or a DSN) and strip trailing
    whitespace. Never echoes the file's *contents* anywhere — only this
    function's own error message, which names only the path, never
    what the file contained."""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"error: could not read {label} from {path!r}: {exc}") from exc


def _safe_str(exc: Exception) -> str:
    """A capped, message-only rendering of an exception — same
    discipline as `persistence/objects/minio_store.py`'s own
    `_safe_str`: never the raw exception object, truncated
    defensively so a verbose provider error can never balloon a log
    line. Relies (like that module) on botocore/psycopg never
    embedding raw credential values in their own error strings; this
    tool additionally never interpolates an access/secret key into any
    string it builds itself."""
    return str(exc)[:500]


def _build_client(endpoint_url: str, access_key: str, secret_key: str, bucket: str, region: str):
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
    ), bucket


def list_all_keys(client, bucket: str) -> set[str]:
    """Enumerate every key in `bucket` via paginated `list_objects_v2`
    (loops on `ContinuationToken` until `IsTruncated` is false)."""
    keys: set[str] = set()
    continuation_token: str | None = None
    while True:
        kwargs: dict = {"Bucket": bucket}
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token
        response = client.list_objects_v2(**kwargs)
        for obj in response.get("Contents", []):
            keys.add(obj["Key"])
        if not response.get("IsTruncated"):
            break
        continuation_token = response.get("NextContinuationToken")
        if not continuation_token:
            break
    return keys


def query_db_referenced_keys(dsn: str) -> set[str]:
    """The union of every storage_reference/storage key BAGMAN's
    canonical database currently references, per the PL's inventory:

    * `evidence_items.storage_reference` (the primary canonical key)
    * `intake_records.metadata->>'storage_reference'` (an ACCEPTED
      intake's staged bytes — may become orphaned but is still real
      and must never be treated as missing/garbage)
    * `audit_events.payload->>'storage_reference'` for
      `EVIDENCE_STORED` events (redundant with `evidence_items` in the
      common case, included anyway for defense-in-depth)

    Strictly read-only: exactly these three `SELECT`s, nothing else —
    this tool must never write to a BAGMAN canonical table.
    """
    keys: set[str] = set()
    with psycopg.connect(dsn) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute("SELECT storage_reference FROM evidence_items WHERE storage_reference IS NOT NULL")
            keys.update(row[0] for row in cur.fetchall() if row[0])

            cur.execute("SELECT metadata->>'storage_reference' FROM intake_records WHERE metadata ? 'storage_reference'")
            keys.update(row[0] for row in cur.fetchall() if row[0])

            cur.execute(
                "SELECT payload->>'storage_reference' FROM audit_events "
                "WHERE event_type = 'EVIDENCE_STORED' AND payload ? 'storage_reference'"
            )
            keys.update(row[0] for row in cur.fetchall() if row[0])
    return keys


# ---------------------------------------------------------------------
# Checkpoint (append-only JSON-lines, one line per verified-done key)
# ---------------------------------------------------------------------


def load_checkpoint(path: Path) -> dict[str, dict]:
    done: dict[str, dict] = {}
    if not path.is_file():
        return done
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # A partially-written final line from a crash mid-flush —
                # ignore it; the key it describes was therefore never
                # confirmed done and will simply be reprocessed.
                continue
            key = record.get("key")
            if key:
                done[key] = record
    return done


def append_checkpoint(path: Path, record: dict) -> None:
    """Append one JSON-line and force it to durable storage
    immediately, so a crash right after this call still leaves the
    checkpoint file consistent (at most the in-flight key, which never
    reached this call, is lost)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


# ---------------------------------------------------------------------
# Per-key migration
# ---------------------------------------------------------------------


@dataclass
class RunResult:
    source_key_count: int = 0
    destination_key_count: int = 0
    total_bytes_migrated: int = 0
    canonical_reference_count: int = 0
    unreferenced_source_count: int = 0
    unreferenced_source_keys: list[str] = field(default_factory=list)
    missing_reference_count: int = 0
    missing_reference_keys: list[str] = field(default_factory=list)
    migrated_count: int = 0
    already_identical_count: int = 0
    skipped_via_checkpoint_count: int = 0
    failure_count: int = 0
    failures: list[dict] = field(default_factory=list)


def _head_exists(client, bucket: str, key: str) -> bool:
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def migrate_key(
    *,
    key: str,
    source_client,
    source_bucket: str,
    dest_client,
    dest_bucket: str,
    checkpoint_path: Path,
    checkpoint_done: dict[str, dict],
    result: RunResult,
) -> None:
    if key in checkpoint_done:
        result.skipped_via_checkpoint_count += 1
        return

    try:
        source_obj = source_client.get_object(Bucket=source_bucket, Key=key)
        source_bytes = source_obj["Body"].read()
    except (BotoCoreError, ClientError) as exc:
        result.failure_count += 1
        result.failures.append({"key": key, "reason": f"could not GET from source: {_safe_str(exc)}"})
        return

    expected_hash = expected_hash_from_reference(key)
    if expected_hash is not None:
        actual_hash = compute_sha256(source_bytes)
        if actual_hash != expected_hash:
            result.failure_count += 1
            result.failures.append(
                {
                    "key": key,
                    "reason": (
                        f"source-side corruption: bytes hash to '{actual_hash}' but the key "
                        f"itself encodes '{expected_hash}'"
                    ),
                }
            )
            return

    try:
        dest_exists = _head_exists(dest_client, dest_bucket, key)
    except (BotoCoreError, ClientError) as exc:
        result.failure_count += 1
        result.failures.append({"key": key, "reason": f"could not HEAD destination: {_safe_str(exc)}"})
        return

    if dest_exists:
        try:
            dest_obj = dest_client.get_object(Bucket=dest_bucket, Key=key)
            dest_bytes = dest_obj["Body"].read()
        except (BotoCoreError, ClientError) as exc:
            result.failure_count += 1
            result.failures.append({"key": key, "reason": f"could not GET destination for comparison: {_safe_str(exc)}"})
            return

        if dest_bytes == source_bytes:
            result.already_identical_count += 1
            record = {"key": key, "status": "done", "outcome": "already_identical", "bytes": len(source_bytes)}
            append_checkpoint(checkpoint_path, record)
            checkpoint_done[key] = record
            return

        result.failure_count += 1
        result.failures.append(
            {
                "key": key,
                "reason": (
                    "destination already holds DIFFERENT bytes at this key — refusing to "
                    "overwrite (same immutability doctrine as MinIOObjectStore.put)"
                ),
            }
        )
        return

    try:
        dest_client.put_object(Bucket=dest_bucket, Key=key, Body=source_bytes)
    except (BotoCoreError, ClientError) as exc:
        result.failure_count += 1
        result.failures.append({"key": key, "reason": f"PUT to destination failed: {_safe_str(exc)}"})
        return

    try:
        readback_obj = dest_client.get_object(Bucket=dest_bucket, Key=key)
        readback_bytes = readback_obj["Body"].read()
    except (BotoCoreError, ClientError) as exc:
        result.failure_count += 1
        result.failures.append({"key": key, "reason": f"post-write readback GET failed: {_safe_str(exc)}"})
        return

    if readback_bytes != source_bytes:
        result.failure_count += 1
        result.failures.append({"key": key, "reason": "post-write readback bytes did not match source bytes"})
        return

    result.migrated_count += 1
    result.total_bytes_migrated += len(source_bytes)
    record = {"key": key, "status": "done", "outcome": "migrated", "bytes": len(source_bytes)}
    append_checkpoint(checkpoint_path, record)
    checkpoint_done[key] = record


# ---------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------


def run(args: argparse.Namespace) -> tuple[int, dict]:
    source_access_key = _read_file("--source-access-key-file", args.source_access_key_file)
    source_secret_key = _read_file("--source-secret-key-file", args.source_secret_key_file)
    dest_access_key = _read_file("--destination-access-key-file", args.destination_access_key_file)
    dest_secret_key = _read_file("--destination-secret-key-file", args.destination_secret_key_file)
    db_dsn = _read_file("--db-dsn-file", args.db_dsn_file)

    source_client, source_bucket = _build_client(
        args.source_endpoint_url, source_access_key, source_secret_key, args.source_bucket, args.source_region
    )
    dest_client, dest_bucket = _build_client(
        args.destination_endpoint_url, dest_access_key, dest_secret_key, args.destination_bucket, args.destination_region
    )

    print(f"[migrate_object_store] enumerating source keys at {args.source_endpoint_url} bucket={source_bucket}", file=sys.stderr)
    source_keys = list_all_keys(source_client, source_bucket)
    print(f"[migrate_object_store] {len(source_keys)} source key(s) found", file=sys.stderr)

    print(f"[migrate_object_store] querying DB-referenced keys via {args.db_dsn_file}", file=sys.stderr)
    db_referenced_keys = query_db_referenced_keys(db_dsn)
    print(f"[migrate_object_store] {len(db_referenced_keys)} DB-referenced key(s) found", file=sys.stderr)

    missing_from_source = sorted(db_referenced_keys - source_keys)
    if missing_from_source:
        report = {
            "error": "MISSING_FROM_SOURCE",
            "missing_reference_count": len(missing_from_source),
            "missing_reference_keys": missing_from_source,
            "message": (
                "one or more DB-referenced storage_reference values do not exist in the "
                "source bucket at all — this is a serious pre-existing integrity problem, "
                "not something this tool may paper over; NO COPYING WAS PERFORMED"
            ),
        }
        print(json.dumps(report, indent=2, sort_keys=True))
        return 1, report

    unreferenced_source = sorted(source_keys - db_referenced_keys)

    result = RunResult(
        source_key_count=len(source_keys),
        canonical_reference_count=len(db_referenced_keys),
        unreferenced_source_count=len(unreferenced_source),
        unreferenced_source_keys=unreferenced_source,
        missing_reference_count=0,
        missing_reference_keys=[],
    )

    checkpoint_path = Path(args.checkpoint_file)
    checkpoint_done = load_checkpoint(checkpoint_path)

    for key in sorted(source_keys):
        migrate_key(
            key=key,
            source_client=source_client,
            source_bucket=source_bucket,
            dest_client=dest_client,
            dest_bucket=dest_bucket,
            checkpoint_path=checkpoint_path,
            checkpoint_done=checkpoint_done,
            result=result,
        )

    destination_keys = list_all_keys(dest_client, dest_bucket)
    result.destination_key_count = len(destination_keys)

    report = {
        "source_key_count": result.source_key_count,
        "destination_key_count": result.destination_key_count,
        "total_bytes_migrated": result.total_bytes_migrated,
        "canonical_reference_count": result.canonical_reference_count,
        "unreferenced_source_count": result.unreferenced_source_count,
        "unreferenced_source_keys": result.unreferenced_source_keys,
        "missing_reference_count": result.missing_reference_count,
        "migrated_count": result.migrated_count,
        "already_identical_count": result.already_identical_count,
        "skipped_via_checkpoint_count": result.skipped_via_checkpoint_count,
        "failure_count": result.failure_count,
        "failures": result.failures,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return (1 if result.failure_count else 0), report


def _parse_args(argv: Iterable[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)

    parser.add_argument("--source-endpoint-url", required=True)
    parser.add_argument("--source-access-key-file", required=True)
    parser.add_argument("--source-secret-key-file", required=True)
    parser.add_argument("--source-bucket", required=True)
    parser.add_argument("--source-region", default=_DEFAULT_REGION)

    parser.add_argument("--destination-endpoint-url", required=True)
    parser.add_argument("--destination-access-key-file", required=True)
    parser.add_argument("--destination-secret-key-file", required=True)
    parser.add_argument("--destination-bucket", required=True)
    parser.add_argument("--destination-region", default=_DEFAULT_REGION)

    parser.add_argument("--db-dsn-file", required=True, help="file containing a single PostgreSQL DSN — never a literal DSN on the command line")
    parser.add_argument("--checkpoint-file", required=True, help="append-only JSON-lines checkpoint path, for resumability")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        exit_code, _report = run(args)
        return exit_code
    except (BotoCoreError, ClientError) as exc:
        print(f"error: object-store operation failed: {_safe_str(exc)}", file=sys.stderr)
        return 1
    except psycopg.Error as exc:
        print(f"error: database operation failed: {_safe_str(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
