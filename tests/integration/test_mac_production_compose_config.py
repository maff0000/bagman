"""Deterministic proof that the governed, tracked compose layers
render the exact `bagman-scan` production topology already accepted on
the live Mac mini M4 appliance (CD-6 ClamAV reliability hardening
governance delta, 2026-09-23 — see `deployment/compose/docker-compose
.mac-production.yml`'s own comment for the full RCA/rationale this
guards against regressing).

This is a config-rendering test, not application code: it shells out
to the real `docker compose ... config --format json` command against
the two tracked compose files and asserts on the PARSED, MERGED
semantics — never brittle raw-text/line matching, so reordering,
reformatting, or adding unrelated services/comments cannot break it.
No Docker daemon connection or running container is required —
`docker compose config` is a pure static render (verified directly:
passes with no secret files present, no daemon reachable, exactly the
GitHub Actions CI environment this also runs in).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BASE_COMPOSE = REPO_ROOT / "deployment" / "compose" / "docker-compose.yml"
MAC_PRODUCTION_OVERRIDE = REPO_ROOT / "deployment" / "compose" / "docker-compose.mac-production.yml"

#: The exact linux/arm64-pinned digest approved by the Architect
#: (CD-6 ClamAV reliability hardening, 2026-09-22/23) and already
#: proven in production — kept here as an explicit, named constant so
#: a future accidental digest drift in either compose file fails this
#: test loudly rather than silently.
APPROVED_SCANNER_DIGEST = (
    "clamav/clamav-debian@sha256:32770534ece41601bed0005be5d1e1b7a76d734255c0ad02ad52f9281eb30418"
)


def _render_config(*compose_files: Path) -> dict:
    args = ["docker", "compose"]
    for f in compose_files:
        args += ["-f", str(f)]
    args += ["config", "--format", "json"]
    result = subprocess.run(args, capture_output=True, text=True, cwd=REPO_ROOT, check=True)
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def mac_production_config() -> dict:
    """The merged, rendered config compose itself would actually use
    for `docker compose -f docker-compose.yml -f
    docker-compose.mac-production.yml up -d` — the real command the
    live appliance runs."""
    return _render_config(BASE_COMPOSE, MAC_PRODUCTION_OVERRIDE)


def test_scanner_uses_the_approved_immutable_digest(mac_production_config: dict) -> None:
    assert mac_production_config["services"]["bagman-scan"]["image"] == APPROVED_SCANNER_DIGEST


def test_scanner_mount_is_the_external_named_volume_at_the_proven_destination(
    mac_production_config: dict,
) -> None:
    volumes = mac_production_config["services"]["bagman-scan"]["volumes"]
    assert volumes == [
        {
            "type": "volume",
            "source": "bagman-clamav-data",
            "target": "/var/lib/clamav",
            "volume": {},
        }
    ]


def test_clamav_volume_is_declared_external_not_compose_managed(mac_production_config: dict) -> None:
    """`external: true` is the whole point of the governed provisioning
    doctrine (see ops/provision_clamav_volume.sh): compose must never
    create or destroy this volume itself, only attach to it."""
    volume = mac_production_config["volumes"]["bagman-clamav-data"]
    assert volume["external"] is True
    assert volume["name"] == "bagman-clamav-data"


def test_scanner_memory_limit_is_exactly_2gib(mac_production_config: dict) -> None:
    # Compose renders `deploy.resources.limits.memory` as a byte count
    # string regardless of the human-readable `2048M`/`2GiB` form the
    # source YAML uses — 2 GiB = 2048 * 1024 * 1024 bytes exactly.
    memory_bytes = mac_production_config["services"]["bagman-scan"]["deploy"]["resources"]["limits"]["memory"]
    assert int(memory_bytes) == 2 * 1024 * 1024 * 1024


def test_scanner_healthcheck_invokes_the_official_clamdcheck_script(mac_production_config: dict) -> None:
    assert mac_production_config["services"]["bagman-scan"]["healthcheck"]["test"] == [
        "CMD-SHELL",
        "clamdcheck.sh",
    ]


def test_scanner_publishes_no_host_port(mac_production_config: dict) -> None:
    # PID §32/§99.8: bagman-api reaches bagman-scan over bagman-net
    # only, at bagman-scan:3310 — never a published host port by
    # default. `docker-compose.debug-ports.yml` is the separate,
    # explicit opt-in override for local debugging (not applied here).
    assert "ports" not in mac_production_config["services"]["bagman-scan"]


def test_scanner_restart_policy_is_unless_stopped(mac_production_config: dict) -> None:
    assert mac_production_config["services"]["bagman-scan"]["restart"] == "unless-stopped"


def test_base_compose_alone_has_no_persistent_scanner_volume() -> None:
    """The base/portable compose file is deliberately unchanged by
    this governance delta (PID §99.2/§99.3's host-backed-volume
    doctrine is NOT touched there) — bagman-scan stays ephemeral
    (fine for local dev/CI, where no long-lived definition cache is
    needed) unless the Mac-production override layer is explicitly
    applied on top. This guards against the override's volume ever
    silently migrating into the base file."""
    base_only = _render_config(BASE_COMPOSE)
    assert "volumes" not in base_only["services"]["bagman-scan"]
    assert "bagman-clamav-data" not in base_only.get("volumes", {})
