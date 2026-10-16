"""WO-BAGMAN-117 §6.A‴ (2026-10-16 Architect correction, additive to
§6.A/§6.A′/§6.A″/§12) — §12 acceptance criterion 28:

The planned production deployment fronts `bagman-api` with an additive
Caddy container terminating HTTPS and reverse-proxying over plain HTTP
to `bagman-api:8000` on `bagman-net` (PID §117.D). Because that
internal hop is plain HTTP, Starlette's `request.url.scheme` (which
`app/api/main.py::_origin_is_trusted` already correctly compares an
`Origin` header's scheme against — unchanged by this correction) would
always read `"http"` for every request, even ones a real browser
reached over HTTPS — so every legitimate HTTPS `Origin` check would
wrongly fail once that topology exists. `deployment/docker/api/
entrypoint.sh` now enables Uvicorn's own `ProxyHeadersMiddleware` via
`--proxy-headers --forwarded-allow-ips="${BAGMAN_TRUSTED_PROXY_IP:-}"`
— that middleware rewrites `scope["scheme"]`/`scope["client"]` from
`X-Forwarded-Proto`/`X-Forwarded-For` ONLY when the immediate ASGI
transport peer's address is in `--forwarded-allow-ips`.

These tests exercise the REAL `uvicorn.middleware.proxy_headers.
ProxyHeadersMiddleware` (not merely FastAPI's bare `TestClient`, which
never passes through Uvicorn's own CLI-level middleware at all) by
wrapping the real `app` with it directly and driving it through
Starlette's `TestClient`, whose own `client=(host, port)` constructor
parameter sets `scope["client"]` for every request it sends (see
`starlette/testclient.py`, `_ASGIAdapter.__init__`/`__call__` writing
`"client": self.client` directly into the ASGI scope) — this is the
genuine mechanism Uvicorn inspects, not a synthetic shortcut.

Confirmed by direct inspection of the exact installed library
(`uvicorn==0.52.4`, `uvicorn/middleware/proxy_headers.py`):
* `ProxyHeadersMiddleware.__call__` only rewrites `scope["scheme"]`
  (from `X-Forwarded-Proto`) and `scope["client"]` (from
  `X-Forwarded-For`) when `client_host in self.trusted_hosts` is True,
  where `client_host = scope["client"][0]` — the immediate TCP peer,
  never anything client-supplied.
* `_TrustedHosts.__init__` treats any configured entry containing `"/"`
  as an `ipaddress.ip_network` (CIDR/subnet-wide trust — never used by
  this correction); a bare entry with no `"/"` is parsed with
  `ipaddress.ip_address(...)` and matched in `_compute_trust` by EXACT
  `ip in self.trusted_hosts` set-membership — never a prefix/subnet
  match. `trusted_hosts in ("*", ["*"])` is the one special case that
  trusts everything — never used here.
* An empty string (`""`) is parsed by `_parse_raw_hosts` into a single
  literal host `""`, which `ipaddress.ip_address("")` cannot parse, so
  it is stored in `trusted_literals`. No real `scope["client"]` peer
  address is ever the empty string, so `"" in trusted_literals` can
  never match a genuine connection — this is what "default: trust
  nothing" verifiably means here (independently confirmed interactively
  against the installed library: `_TrustedHosts("").__contains__` is
  `False` for every tested real-looking IP and for `""` itself).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware, _TrustedHosts

from app.api.composition import reset_composition_for_tests
from app.api.main import app

_ENTRYPOINT_PATH = Path(__file__).resolve().parents[2] / "deployment" / "docker" / "api" / "entrypoint.sh"
_TRUSTED_PROXY_IP = "10.10.10.5"
_UNTRUSTED_PEER_IP = "10.10.10.6"
_TEST_PASSWORD = "a genuinely random proxy headers scheme trust test password 9x3"


@pytest.fixture
def proxied_client_factory(monkeypatch, tmp_path):
    """Builds a `TestClient` wrapping the REAL `app` in the REAL
    `ProxyHeadersMiddleware`, over a plain-`http://` base URL (modelling
    the actual Caddy -> bagman-api internal hop, which is plain HTTP),
    with a caller-chosen simulated TCP peer address — never `https://`,
    unlike `tests/security/test_auth_access_control.py`'s `dev_client`,
    which deliberately models the OTHER (already-accepted, HTTPS-native)
    topology. Returns a factory so each test can choose its own peer
    address and trusted-hosts configuration independently.
    """
    monkeypatch.setenv("BAGMAN_RUNTIME_ENV", "development")
    monkeypatch.setenv("BAGMAN_AUTH_ENABLED", "true")

    from core.auth import hash_password

    hash_file = tmp_path / "operator_password_hash"
    hash_file.write_text(hash_password(_TEST_PASSWORD), encoding="utf-8")
    monkeypatch.setenv("BAGMAN_OPERATOR_PASSWORD_HASH_FILE", str(hash_file))

    reset_composition_for_tests()

    def _make(peer_ip: str, trusted_hosts) -> TestClient:
        wrapped = ProxyHeadersMiddleware(app, trusted_hosts=trusted_hosts)
        return TestClient(wrapped, base_url="http://testserver", client=(peer_ip, 12345))

    yield _make
    reset_composition_for_tests()


def _login_and_logout_headers(client: TestClient, *, origin: str, forwarded_proto: str | None) -> dict:
    """Logs in (public route, never Origin/CSRF-gated) and returns the
    headers needed for a subsequent state-changing `/auth/logout`
    request — the session/CSRF cookies are read directly off the login
    response and sent back as an explicit `Cookie` header rather than
    relying on the test HTTP client's own cookie jar, because the
    session cookie is `Secure` and a jar modelling a plain `http://`
    base URL (the real Caddy->bagman-api wire scheme this fixture
    deliberately models) would otherwise withhold it on the next
    request — a test-harness artifact unrelated to what this test
    actually verifies (`request.url.scheme` as seen by the application,
    downstream of `ProxyHeadersMiddleware`)."""
    login = client.post("/auth/login", json={"password": _TEST_PASSWORD})
    assert login.status_code == 200, login.text
    csrf_token = login.cookies.get("__Host-bagman_csrf")
    cookie_header = "; ".join(f"{name}={value}" for name, value in login.cookies.items())
    headers = {
        "x-bagman-csrf-token": csrf_token,
        "origin": origin,
        "cookie": cookie_header,
    }
    if forwarded_proto is not None:
        headers["x-forwarded-proto"] = forwarded_proto
    return headers


def test_trusted_peer_forwarded_https_is_honoured_and_origin_accepted(proxied_client_factory):
    """(a) A request carrying `X-Forwarded-Proto: https` from the
    configured trusted peer address results in `request.url.scheme ==
    "https"` — proven here not by inspecting internals directly but by
    its real observable effect: combined with `_origin_is_trusted`'s
    own (unchanged) logic, a matching `https://...` Origin is then
    correctly ACCEPTED, where it would otherwise be rejected as a
    scheme mismatch against the underlying plain-HTTP transport."""
    client = proxied_client_factory(_TRUSTED_PROXY_IP, _TRUSTED_PROXY_IP)
    headers = _login_and_logout_headers(client, origin="https://testserver", forwarded_proto="https")
    response = client.post("/auth/logout", headers=headers)
    assert response.status_code == 200, response.text


def test_untrusted_peer_forwarded_https_is_genuinely_ignored(proxied_client_factory):
    """(b) The identical `X-Forwarded-Proto: https` header, from ANY
    peer other than the one configured trusted address, is genuinely
    ignored: `request.url.scheme` remains `"http"`, so the identical
    `https://...` Origin now correctly MISMATCHES and is rejected. This
    is the proof that the trust is genuinely narrow — a blanket/default
    acceptance would make this test fail (return 200 instead of 403)."""
    client = proxied_client_factory(_UNTRUSTED_PEER_IP, _TRUSTED_PROXY_IP)
    headers = _login_and_logout_headers(client, origin="https://testserver", forwarded_proto="https")
    response = client.post("/auth/logout", headers=headers)
    assert response.status_code == 403, response.text
    assert response.json()["error_code"] == "ORIGIN_REJECTED"


def test_csrf_check_is_completely_unaffected_by_trusted_scheme_rewrite(proxied_client_factory):
    """(c) Even when the trusted proxy correctly rewrites the scheme
    and the Origin check consequently passes, the independent CSRF
    double-submit check is still fully, separately enforced — proving
    this correction does not weaken or bypass CSRF in any way. A
    deliberately wrong CSRF header, under otherwise-fully-trusted
    conditions, must still be rejected as a CSRF failure, not silently
    waved through by the scheme-trust logic."""
    client = proxied_client_factory(_TRUSTED_PROXY_IP, _TRUSTED_PROXY_IP)
    headers = _login_and_logout_headers(client, origin="https://testserver", forwarded_proto="https")
    headers["x-bagman-csrf-token"] = "deliberately-wrong-csrf-token"
    response = client.post("/auth/logout", headers=headers)
    assert response.status_code == 403, response.text
    assert response.json()["error_code"] == "CSRF_REJECTED"


def test_xero_oauth_public_leg_is_unaffected_from_a_non_trusted_peer(proxied_client_factory):
    """(d) The existing Xero-OAuth Caddy leg is a DIFFERENT, distinct
    address — never the one trusted proxy address this correction
    configures — so from the application's perspective a request
    arriving via that leg is simply an ordinary request from an
    untrusted peer: `ProxyHeadersMiddleware`'s mere presence in the
    stack does not alter its behaviour at all (no `X-Forwarded-Proto`
    is sent on this leg, and even if it were, it would be ignored per
    the previous test). Proven here by exercising a genuine public
    Xero-OAuth-result route (`GET /internal/xero/oauth/result`, no
    authentication required, matching the existing allow-list) through
    the SAME proxied stack from a peer address distinct from the
    trusted one, and confirming it still behaves exactly as before:
    reachable, no session cookie required, normal response."""
    client = proxied_client_factory(_UNTRUSTED_PEER_IP, _TRUSTED_PROXY_IP)
    response = client.get("/internal/xero/oauth/result", params={"ok": "false", "reason": "test"})
    assert response.status_code == 200
    assert "cookie" not in response.headers.get("set-cookie", "").lower() or not response.cookies


def test_default_unset_trusted_proxy_ip_trusts_nothing():
    """The entrypoint's own default-when-unset (`${BAGMAN_TRUSTED_PROXY_IP:-}`
    resolving to the empty string) must trust NOTHING — never uvicorn's
    own built-in `"127.0.0.1"` default, and never a value that could
    ever match a real connecting peer. Proven directly against the
    installed library's own `_TrustedHosts`, and cross-checked against
    a handful of addresses genuinely plausible on a Docker bridge
    network (never `127.0.0.1`, since Docker bridge peers are never the
    loopback address), proving the empty-string default is unconditionally
    inert regardless of deployment topology."""
    trusted = _TrustedHosts("")
    for candidate in ("127.0.0.1", "", "10.0.0.2", "172.17.0.1", "0.0.0.0", "::1"):
        assert candidate not in trusted, f"empty-string default unexpectedly trusts {candidate!r}"


def test_entrypoint_shell_substitution_defaults_to_empty_string_when_unset():
    """Proves the ACTUAL shell substitution used in the Git-tracked
    `entrypoint.sh` (`${BAGMAN_TRUSTED_PROXY_IP:-}`) really does resolve
    to the empty string when the variable is unset — not merely
    asserted by reading the file, but reproduced by actually running
    `sh -c` with the variable unset, exactly mirroring the shell
    (`#!/bin/sh`) this file itself declares."""
    import subprocess

    result = subprocess.run(
        ["sh", "-c", 'unset BAGMAN_TRUSTED_PROXY_IP; printf "%s" "${BAGMAN_TRUSTED_PROXY_IP:-}"'],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == ""


def test_entrypoint_has_no_wildcard_or_hardcoded_subnet_trust():
    """Static, independent proof against the real Git-tracked file:
    no `*` wildcard forwarded-header trust, and no literal CIDR
    (subnet-wide, `/`-bearing) range, appears anywhere in
    `deployment/docker/api/entrypoint.sh` — the hard requirement named
    by both WO §6.A‴'s item 17 and §12 criterion 28. Also confirms the
    trusted value is sourced from the `BAGMAN_TRUSTED_PROXY_IP`
    environment variable (never a hardcoded production IP literal) and
    that `--proxy-headers` is present."""
    content = _ENTRYPOINT_PATH.read_text(encoding="utf-8")
    exec_lines = [line for line in content.splitlines() if "uvicorn" in line and "exec" in line]
    assert exec_lines, "expected the final `exec uvicorn ...` line to still be present"

    assert "--proxy-headers" in content
    assert "BAGMAN_TRUSTED_PROXY_IP" in content
    assert "--forwarded-allow-ips" in content

    # No wildcard forwarded-header trust under any circumstance.
    assert re.search(r"forwarded-allow-ips=.?\*", content) is None
    assert '"*"' not in content.split("exec uvicorn", 1)[-1]

    # No literal, hardcoded CIDR/subnet notation (an IPv4/IPv6 literal
    # immediately followed by "/<digits>") anywhere in the file.
    assert re.search(r"\d{1,3}(?:\.\d{1,3}){3}/\d{1,3}", content) is None

    # No hardcoded production IP literal baked into the
    # --forwarded-allow-ips value itself (the --host 0.0.0.0 bind
    # address above is an unrelated, legitimate literal and is
    # deliberately excluded from this check).
    forwarded_ips_match = re.search(r"--forwarded-allow-ips=\"([^\"]*)\"", content)
    assert forwarded_ips_match is not None
    assert re.search(r"\d{1,3}(?:\.\d{1,3}){3}", forwarded_ips_match.group(1)) is None
