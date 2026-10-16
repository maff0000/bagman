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

# WO-BAGMAN-117 §6.A‴ (2026-10-16 Architect correction, item 17): the
# planned production HTTPS reverse proxy (PID §117.D's additive Caddy
# container, reached only over plain HTTP on `bagman-net`) means
# `request.url.scheme` as seen by the ASGI app is always "http", even
# for a request a real browser reached over HTTPS — `_origin_is_trusted`
# (app/api/main.py, unchanged by this correction) would therefore wrongly
# reject every legitimate HTTPS Origin once that topology exists. Uvicorn's
# own `ProxyHeadersMiddleware` (enabled here via --proxy-headers) is the
# standard, narrow fix: it rewrites scope["scheme"]/scope["client"] from
# X-Forwarded-Proto/X-Forwarded-For ONLY when the immediate TCP peer's
# address is in --forwarded-allow-ips (confirmed by direct inspection of
# uvicorn 0.52.4's installed
# uvicorn/middleware/proxy_headers.py::_TrustedHosts — a bare address with
# no "/" is matched by EXACT ipaddress.ip_address equality, never CIDR/
# subnet containment, unless the configured value itself contains "/").
#
# The trusted address is read from BAGMAN_TRUSTED_PROXY_IP, an
# environment variable — never a hardcoded production IP in this
# Git-tracked file. The real production Caddy container's address is a
# Stage D/E production-deployment detail (this WO's repository scope
# does not include the host-local, non-Git-tracked production compose
# file). Hard requirement (§6.A‴): no wildcard ("*") and no subnet-wide
# range may ever appear here.
#
# Default-when-unset is the EMPTY STRING, not uvicorn's own built-in
# "127.0.0.1" default: an empty --forwarded-allow-ips value parses to a
# single non-IP literal ("") that can never equal a real connecting
# peer's address (scope["client"] is always a genuine IP, never empty),
# so it trusts nothing, unconditionally, from any topology — including
# every test/development/CI invocation of this exact entrypoint, which
# never sets BAGMAN_TRUSTED_PROXY_IP at all. This is deliberately safer
# than relying on "nothing currently connects from 127.0.0.1" being true
# in every present and future deployment/test topology.
exec uvicorn app.api.main:app --host 0.0.0.0 --port 8000 \
    --proxy-headers \
    --forwarded-allow-ips="${BAGMAN_TRUSTED_PROXY_IP:-}"
