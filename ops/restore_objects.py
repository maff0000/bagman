#!/usr/bin/env python3
"""ops/restore_objects.py — re-uploads a previously-exported local
directory's contents (as produced by ``backup_objects.py``) back into
the ``bagman-evidence`` bucket, preserving keys exactly (PID §34-35).

Walks ``<input_dir>`` recursively; each file's path relative to
``<input_dir>`` becomes its restored object key verbatim (so
``backups/objects/<ts>/evidence/<id>/<hash>`` restores to the key
``evidence/<id>/<hash>``, unchanged). Where a key is shaped like a
canonical ``persistence.objects.store.object_key()`` value, the local
file's bytes are re-hashed and compared against the hash the key
itself encodes before upload — reusing
``persistence.objects.store.compute_sha256``/``parse_object_key`` —
and the restore refuses (raises) rather than uploading a backup file
that has itself become corrupt.

Ensures the target bucket exists first (PID §35's restore-into-clean-
target scenario: a freshly recreated object store has no bucket yet
until something creates one).

Run inside the ``bagman-api`` image — see ``backup_objects.py``'s
module docstring / the repo-root ``Makefile``'s ``restore`` target for
the exact invocation shape.

Usage:
    restore_objects.py <input_dir>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

from _objects_common import build_client_and_bucket, ensure_bucket
from persistence.objects.store import compute_sha256, parse_object_key


def restore(input_dir: Path) -> int:
    if not input_dir.is_dir():
        print(f"error: input directory not found: {input_dir}", file=sys.stderr)
        return 1

    client, bucket = build_client_and_bucket()
    ensure_bucket(client, bucket)

    count = 0
    for path in sorted(p for p in input_dir.rglob("*") if p.is_file()):
        key = path.relative_to(input_dir).as_posix()
        data = path.read_bytes()

        parsed = parse_object_key(key)
        if parsed is not None:
            _evidence_id, expected_hash = parsed
            actual_hash = compute_sha256(data)
            if actual_hash != expected_hash:
                raise RuntimeError(
                    f"restore integrity check failed for '{key}': local backup "
                    f"file hashes to '{actual_hash}' but the key itself encodes "
                    f"'{expected_hash}' — refusing to restore a corrupt backup"
                )

        client.put_object(Bucket=bucket, Key=key, Body=data)
        count += 1
        print(f"[restore_objects] restored {key} ({len(data)} bytes)", file=sys.stderr)

    print(f"[restore_objects] {count} object(s) restored from {input_dir}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "input_dir", help="local export directory previously produced by backup_objects.py"
    )
    args = parser.parse_args(argv)

    try:
        return restore(Path(args.input_dir))
    except (BotoCoreError, ClientError) as exc:
        print(f"error: object store restore failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
