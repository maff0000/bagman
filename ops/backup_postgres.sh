#!/usr/bin/env bash
# ops/backup_postgres.sh — repeatable logical PostgreSQL backup (PID §34).
#
# Runs `pg_dump` INSIDE the running `bagman-db` container via
# `docker compose exec` (per the Forge Engineer contract's suggested
# invocation), reading the database password from the SAME secret
# file the container already has mounted at
# /run/secrets/postgres_password (PID §29-31) — the password is never
# a literal on any command line, host- or container-side: it is read
# from disk entirely inside the container's own shell and handed to
# pg_dump via the PGPASSWORD environment variable set in that same
# shell invocation only. It is never echoed, printed, or logged by
# this script.
#
# Output: a single UTC-timestamped, schema+data, --clean/--if-exists
# plain-SQL dump. `--clean --if-exists` means the same dump restores
# cleanly both into a database that already holds this data (existing
# objects are dropped first) and into a freshly-migrated EMPTY
# database (the DROP ... IF EXISTS statements are simply no-ops there)
# — see restore_postgres.sh.
#
# The output directory defaults to backups/postgres/ (relative to the
# repo root), which the CD-1 `.gitignore`'s existing `backups/` entry
# already excludes from Git — confirmed, not re-added, per this work
# item's scope.
#
# Usage:
#   ops/backup_postgres.sh [output_dir]
#
# Prints ONLY the resulting dump file's absolute path on stdout (all
# progress/diagnostic messages go to stderr), so callers can do:
#   DUMP_FILE="$(ops/backup_postgres.sh)"
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/deployment/compose/docker-compose.yml"
COMPOSE=(docker compose -p bagman -f "$COMPOSE_FILE")

OUTPUT_DIR="${1:-$REPO_ROOT/backups/postgres}"
mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUTPUT_FILE="$OUTPUT_DIR/bagman-postgres-${TIMESTAMP}.sql"

echo "[backup_postgres] $(date -u +%Y-%m-%dT%H:%M:%SZ) dumping bagman-db -> $OUTPUT_FILE" >&2

"${COMPOSE[@]}" exec -T bagman-db sh -c '
  set -e
  PGPASSWORD="$(cat /run/secrets/postgres_password)" \
    exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --clean --if-exists
' > "$OUTPUT_FILE"

LINES="$(wc -l < "$OUTPUT_FILE" | tr -d ' ')"
echo "[backup_postgres] $(date -u +%Y-%m-%dT%H:%M:%SZ) done: ${LINES} lines written to $OUTPUT_FILE" >&2

echo "$OUTPUT_FILE"
