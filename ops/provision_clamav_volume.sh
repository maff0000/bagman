#!/usr/bin/env bash
# ops/provision_clamav_volume.sh — idempotent prerequisite step for
# deployment/compose/docker-compose.mac-production.yml's external
# `bagman-clamav-data` Docker volume (CD-6 ClamAV reliability
# hardening, governance delta, 2026-09-23).
#
# `external: true` volumes are never created or destroyed by compose
# itself (deliberately — see that file's own comment) — this script
# is the one governed place that creates the volume, and only if it
# does not already exist. A populated production volume is NEVER
# recreated or destroyed by this script: `docker volume create` on an
# already-existing volume is itself a documented no-op (Docker returns
# the existing volume unchanged, does not touch its contents), but
# this script still checks first and prints which case occurred, so a
# run against a live appliance is never ambiguous about whether
# anything happened to already-downloaded virus definitions.
#
# Usage (run once per appliance, before the very first
#   docker compose -f docker-compose.yml -f docker-compose.mac-production.yml up -d
# on that host — safe to re-run any time afterward, including on every
# deploy, since it is a no-op once the volume exists):
#
#   ops/provision_clamav_volume.sh
#
# Ownership/permissions inside the volume are established by the
# scanner container itself on first start (the official
# `clamav/clamav-debian` image's own entrypoint creates its expected
# files under /var/lib/clamav as its own non-root `clamav` user,
# uid:gid 1000:1000) — this script only ensures the empty volume
# exists; it never writes into it directly.
set -euo pipefail

VOLUME_NAME="bagman-clamav-data"

if docker volume inspect "$VOLUME_NAME" >/dev/null 2>&1; then
  echo "ops/provision_clamav_volume.sh: '$VOLUME_NAME' already exists — left untouched (no recreate, no destroy)." >&2
else
  docker volume create "$VOLUME_NAME" >/dev/null
  echo "ops/provision_clamav_volume.sh: created new empty volume '$VOLUME_NAME' — freshclam will populate it from cold on first scanner start." >&2
fi

docker volume inspect "$VOLUME_NAME" --format 'ops/provision_clamav_volume.sh: {{.Name}} driver={{.Driver}} mountpoint={{.Mountpoint}}' >&2
