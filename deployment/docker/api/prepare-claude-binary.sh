#!/usr/bin/env bash
# CD-5 Gate-2 closure — stages the real `claude` (Claude Code) native
# binary into the bagman-api Docker build context, immediately before
# `docker compose build`.
#
# Why a staged copy rather than a Dockerfile-internal download
# ------------------------------------------------------------------
# `claude` is a self-contained, dynamically-linked-against-glibc-only
# native executable (no Node.js/interpreter runtime needed — verified
# directly: `ldd` shows only librt/libc/libpthread/libdl/libm, all
# present in the `python:3.12-slim` base image) — ~219MB. Rather than
# have the Dockerfile fetch it from a network install script at build
# time (an extra, less-controllable external dependency this WI judged
# out of scope to stand up/verify), this script copies the ALREADY-
# INSTALLED, working binary from wherever `claude` resolves on the
# machine running the build (the same one BAGMAN's own Docker host
# already has, since a BAGMAN engineer/operator session needs a working
# `claude` anyway) into `deployment/docker/api/bin/claude` — a
# `.gitignore`'d path — where the Dockerfile's own `COPY` picks it up.
#
# This is an honestly-flagged, first-pass deployment mechanism, not a
# permanent solution: a real multi-host/CI build would need a proper,
# versioned artifact source for this binary (e.g. Anthropic's own
# official release channel) rather than reusing whatever happens to be
# installed on the build machine. Flagged here, not solved
# speculatively now — same discipline this codebase already applies to
# other known, deferred limitations (e.g. the MANUAL_UPLOAD Source
# multi-replica race note in app/api/composition.py).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
DEST_DIR="${REPO_ROOT}/deployment/docker/api/bin"
DEST="${DEST_DIR}/claude"

CLAUDE_PATH="$(command -v claude || true)"
if [ -z "${CLAUDE_PATH}" ]; then
    echo "prepare-claude-binary.sh: no 'claude' executable found on PATH — install Claude Code on this build host first" >&2
    exit 1
fi

REAL_PATH="$(readlink -f "${CLAUDE_PATH}")"
mkdir -p "${DEST_DIR}"
cp -f "${REAL_PATH}" "${DEST}"
chmod 0755 "${DEST}"

echo "prepare-claude-binary.sh: staged $(du -h "${DEST}" | cut -f1) binary (from ${REAL_PATH}) at ${DEST}"
