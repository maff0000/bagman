#!/bin/sh
# bagman-api container entrypoint (CD-3 WI-3, PID §36).
#
# Migration is an explicit, VISIBLE step before the application starts
# — never hidden inside apparent app startup. `set -e` means a failed
# migration aborts the container (exit non-zero) rather than silently
# starting the application against an un-migrated/half-migrated schema.
set -e

echo "[bagman-api entrypoint] $(date -u +%Y-%m-%dT%H:%M:%SZ) running migrations: alembic upgrade head"
alembic upgrade head
echo "[bagman-api entrypoint] $(date -u +%Y-%m-%dT%H:%M:%SZ) migrations complete; starting application"

exec uvicorn app.api.main:app --host 0.0.0.0 --port 8000
