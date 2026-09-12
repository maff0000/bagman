#!/usr/bin/env bash
# ops/restore_postgres.sh — restores a `.sql` dump produced by
# ops/backup_postgres.sh into the currently-running `bagman-db` (PID
# §34-35).
#
# Works identically whether `bagman-db` already holds this data (the
# dump's own `--clean --if-exists` statements drop existing objects
# first) or is a freshly-migrated EMPTY database (the restore-into-
# clean-target proof, PID §35/§60 steps 17-19) — in the empty case the
# DROP ... IF EXISTS statements are simply no-ops (nothing exists yet
# to drop) and the dump's own CREATE TABLE + data statements populate
# the schema and data from scratch, including the `alembic_version`
# table (an ordinary table in the dump like any other), so the restored
# database also reports the correct schema_migration_version.
#
# Same secret-file discipline as backup_postgres.sh: PGPASSWORD is
# read from /run/secrets/postgres_password INSIDE the container only,
# never a literal on any command line.
#
# Usage:
#   ops/restore_postgres.sh <dump_file.sql>
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/deployment/compose/docker-compose.yml"
COMPOSE=(docker compose -p bagman -f "$COMPOSE_FILE")

if [ $# -ne 1 ]; then
  echo "usage: $0 <dump_file.sql>" >&2
  exit 2
fi

DUMP_FILE="$1"
if [ ! -f "$DUMP_FILE" ]; then
  echo "error: dump file not found: $DUMP_FILE" >&2
  exit 1
fi

echo "[restore_postgres] $(date -u +%Y-%m-%dT%H:%M:%SZ) restoring $DUMP_FILE -> bagman-db" >&2

"${COMPOSE[@]}" exec -T bagman-db sh -c '
  set -e
  PGPASSWORD="$(cat /run/secrets/postgres_password)" \
    exec psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"
' < "$DUMP_FILE"

echo "[restore_postgres] $(date -u +%Y-%m-%dT%H:%M:%SZ) restore complete" >&2
