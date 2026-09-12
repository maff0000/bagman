#!/usr/bin/env python3
"""ops/backup_objects.py — repeatable logical export of every object
currently in the ``bagman-evidence`` bucket (PID §34).

Lists every object in the configured bucket and downloads it (via
``boto3``), mirroring the ``evidence/<evidence_id>/<content_hash>`` key
structure exactly into a local ``<output_dir>/`` directory — so
``restore_objects.py`` can later re-upload the same tree and reproduce
every key byte-for-byte.

Where every object's key is shaped like a canonical
``persistence.objects.store.object_key()`` value (the normal case),
this script also re-hashes the downloaded bytes and compares them
against the hash the key itself encodes — reusing
``persistence.objects.store.compute_sha256``/``parse_object_key``
rather than reinventing that check — and flags (but still backs up)
any object whose bytes no longer match, so backup-time corruption is
caught and reported rather than silently propagated into the backup.

Run inside the ``bagman-api`` image, which already has ``boto3`` and
the real ``BAGMAN_OBJECT_STORE_*`` configuration/secrets a live
``bagman-api`` container uses — see the repo-root ``Makefile``'s
``backup`` target for the exact invocation, e.g.:

    docker compose -p bagman -f deployment/compose/docker-compose.yml \\
      run --rm --no-deps -e PYTHONPATH=/app \\
      -v "$(pwd)/ops:/host-ops:ro" \\
      -v "$(pwd)/backups:/host-backups" \\
      --entrypoint python3 bagman-api \\
      /host-ops/backup_objects.py /host-backups/objects/<UTC-timestamp>

Usage:
    backup_objects.py <output_dir>

Prints the resolved output directory on stdout as its last line (all
progress/diagnostic messages go to stderr); exits non-zero (after
still completing the backup) if any object was found corrupt.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

from _objects_common import build_client_and_bucket, ensure_bucket
from persistence.objects.store import compute_sha256, parse_object_key


def backup(output_dir: Path) -> int:
    client, bucket = build_client_and_bucket()
    ensure_bucket(client, bucket)
    output_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    corrupt: list[str] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            response = client.get_object(Bucket=bucket, Key=key)
            data = response["Body"].read()

            parsed = parse_object_key(key)
            if parsed is not None:
                _evidence_id, expected_hash = parsed
                actual_hash = compute_sha256(data)
                if actual_hash != expected_hash:
                    corrupt.append(key)
                    print(
                        f"[backup_objects] WARNING: '{key}' downloaded bytes hash "
                        f"to '{actual_hash}' but the key itself encodes "
                        f"'{expected_hash}' — backed up anyway, flagged as corrupt",
                        file=sys.stderr,
                    )

            dest = output_dir / key  # preserves evidence/<id>/<hash> exactly
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            count += 1
            print(f"[backup_objects] backed up {key} ({len(data)} bytes)", file=sys.stderr)

    summary = f"[backup_objects] {count} object(s) backed up to {output_dir}"
    if corrupt:
        summary += f" — {len(corrupt)} CORRUPT (see warnings above): {corrupt}"
    print(summary, file=sys.stderr)

    print(str(output_dir))
    return 1 if corrupt else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "output_dir", help="local directory to export objects into (created if missing)"
    )
    args = parser.parse_args(argv)

    try:
        return backup(Path(args.output_dir))
    except (BotoCoreError, ClientError) as exc:
        print(f"error: object store backup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
