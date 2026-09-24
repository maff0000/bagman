"""Fixtures for ``tests/app_api/`` (CD-3 WI-3, extended by CD-4 WI-3).

Proves ``app/api/composition.py``'s hard "no fallback" invariant
(PID §45-46) against REAL, disposable PostgreSQL and object-store
containers this session starts, stops/restarts mid-test, and tears
down itself — never mocks, exactly the same discipline
``tests/persistence/conftest.py`` already established for WI-1/WI-2.
CD-4 WI-3 additionally stands up a REAL, disposable ``clamd`` daemon
(same shape as WI-2's own ``tests/integration/test_intake_scanner.py``
disposable container) so this module's production-mode composition has
a genuinely reachable scanner too — ``/ready`` now checks it (PID §60),
and this WI's own intake-endpoint tests exercise the REAL
``ClamAVScanner`` end-to-end through the HTTP layer against it (never a
mock), per PID §20/§63's "a stub is used in dev must never become the
real scanner is never exercised" doctrine.

CD-6 MinIO withdrawal WO (2026-09-24): the disposable object-store
container below now runs SeaweedFS 4.47
(``ghcr.io/chrislusf/seaweedfs``, pinned by digest — see
``SEAWEEDFS_IMAGE``), not MinIO — the withdrawn
``quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z.hotfix.7aa24e772``
tag this fixture used to pull is no longer pullable at all. The
identifiers below (``MINIO_CONTAINER``, ``wait_for_minio_container_healthy``,
the ``"minio_container"`` key in ``runtime_stack``) are DELIBERATELY
left unrenamed: nothing about what they represent to a caller changed
(still "the disposable object-store container/readiness-wait for this
test module"), only which real S3-compatible server sits behind the
name — and leaving them unrenamed means ``tests/app_api/test_no_fallback.py``
needed zero changes. BAGMAN's own client code under test,
``persistence.objects.minio_store.MinIOObjectStore``, is unmodified
and unaware which provider it is talking to, by design (PID §53) — it
is proven here against a genuinely different S3-compatible backend
than WI-3 originally exercised, and every one of this module's tests
still passes unchanged.

Containers are named unmistakably as disposable WI-3 test fixtures
(``bagman-test-postgres-wi3``, ``bagman-test-minio-wi3``,
``bagman-test-clamav-wi3``) so they can never be confused with the real
``bagman-db``/``bagman-objects``/``bagman-scan`` runtime containers a
developer might also have running locally, or with WI-2's own
``bagman-test-clamav-wi2`` fixture. ``bagman-test-minio-wi3`` keeps its
name for the same reason as the identifiers above — it is still
unambiguously "the disposable test object-store container for this
module", regardless of which server image now backs it.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator

import pytest

PG_CONTAINER = "bagman-test-postgres-wi3"
PG_IMAGE = "postgres:17"
PG_HOST = "127.0.0.1"
PG_HOST_PORT = "55434"
PG_DB = "bagman_test_wi3"
PG_USER = "bagman_test_wi3"

MINIO_CONTAINER = "bagman-test-minio-wi3"
#: CD-6 MinIO withdrawal WO: SeaweedFS 4.47, pinned by immutable digest
#: (the same digest the PL independently verified — cosign signature,
#: upstream commit ancestry, Trivy CVE reconciliation — and this
#: Forge Engineer independently re-verified live against BAGMAN's real
#: `MinIOObjectStore`). The `:4.47` tag is for operator readability
#: only; the digest is authoritative.
SEAWEEDFS_IMAGE = (
    "ghcr.io/chrislusf/seaweedfs:4.47"
    "@sha256:ce9e796f1fe6f06968f4c04bdaf8f678dad9c8acdfef3d244133d71bfa6bf882"
)
MINIO_HOST_PORT = "19020"
MINIO_BUCKET = "bagman-test-wi3-objects"

#: CD-4 WI-3 addition — a differently-named/-ported disposable clamd
#: container from WI-2's own ``bagman-test-clamav-wi2``/``33100``, to
#: avoid any collision when both test modules happen to run in the
#: same CI environment.
CLAMAV_CONTAINER = "bagman-test-clamav-wi3"
#: CD-4 PR #4 Architect delta (2026-09-13): pinned by immutable digest
#: (not a moving tag) so this disposable fixture and the real
#: `bagman-scan` service can never silently drift onto two different
#: ClamAV builds without any test catching it.
#:
#: CD-6 ClamAV reliability hardening (2026-09-22): the image moved to
#: `clamav/clamav-debian` (see docker-compose.yml's `bagman-scan`
#: comment for the full RCA — the prior amd64-only manifest was running
#: under QEMU emulation on the arm64 production appliance and hit a
#: proven cgroup OOM-kill). Production now pins the linux/arm64
#: PLATFORM-SPECIFIC manifest digest, since that is the one real
#: deployment target and the pin should say so explicitly.
#:
#: This fixture deliberately pins a DIFFERENT digest instead: the
#: multi-arch INDEX digest (`docker buildx imagetools inspect
#: clamav/clamav-debian:latest`'s own top-level `Digest:`, distinct
#: from any one platform's manifest digest inside it). CI
#: (`.github/workflows/security.yml`) runs on GitHub's `ubuntu-latest`
#: (amd64) runners, which cannot run an arm64-only manifest without
#: QEMU setup this workflow doesn't configure — but an index digest is
#: still a single immutable, content-addressed reference (any change to
#: ANY platform's build changes the index digest too, so drift is still
#: caught); Docker resolves it to the correct platform automatically on
#: whichever host pulls it, amd64 CI runner or arm64 Mac alike. So: two
#: different digest values below/in docker-compose.yml is intentional,
#: not a broken "keep both equal" invariant — what must stay true is
#: that both point at the SAME upstream image family and the same
#: underlying release, updated together, deliberately, whenever the pin
#: is intentionally moved forward.
CLAMAV_IMAGE = (
    "clamav/clamav-debian@sha256:df80497be841a8ad57f95e04f978216241457f8f8ad608f1f682e3cd0fe63c45"
)
CLAMAV_HOST_PORT = "33101"

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def _wait_for_postgres(timeout_s: float = 60.0) -> None:
    """Block until PostgreSQL is genuinely ready for real queries.

    PL correction (2026-09-13, same fix as tests/persistence/conftest.py):
    a fresh official-image container briefly runs a TEMPORARY server
    (during `initdb`'s init-script phase) before the FINAL long-lived
    server starts. `pg_isready` alone can report success during that
    temporary window, so the first real query can land exactly as it's
    shutting down (`server closed the connection unexpectedly`) —
    reproduced live in CI. Requiring an actual `SELECT 1` to succeed on
    several consecutive attempts rides out that window instead of
    trusting a single `pg_isready` success.
    """
    _REQUIRED_CONSECUTIVE_OK = 3
    deadline = time.monotonic() + timeout_s
    consecutive_ok = 0
    last = None
    while time.monotonic() < deadline:
        last = subprocess.run(
            ["docker", "exec", PG_CONTAINER, "psql", "-U", PG_USER, "-d", PG_DB, "-tAc", "SELECT 1"],
            capture_output=True,
            text=True,
        )
        if last.returncode == 0 and last.stdout.strip() == "1":
            consecutive_ok += 1
            if consecutive_ok >= _REQUIRED_CONSECUTIVE_OK:
                return
        else:
            consecutive_ok = 0
        time.sleep(0.5)
    raise RuntimeError(
        f"{PG_CONTAINER} did not become genuinely ready (real SELECT 1) in time: "
        f"{last.stdout if last else ''} {last.stderr if last else ''}"
    )


def _wait_for_minio(timeout_s: float = 60.0) -> None:
    """Block until the disposable SeaweedFS S3 gateway is genuinely up.

    CD-6 MinIO withdrawal WO: SeaweedFS's S3 gateway has no MinIO-style
    ``/minio/health/live`` path. Live-verified (both directly and via
    ``deployment/compose/docker-compose.yml``'s matching healthcheck
    comment): a bare, unauthenticated ``GET /`` on the S3 port answers
    ``403 Forbidden`` — that IS "up and correctly enforcing auth", not
    a failure, so both 200 and 403 count as ready; anything else
    (connection refused, 5xx, timeout) does not.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{MINIO_HOST_PORT}/", timeout=2) as response:
                if response.status in (200, 403):
                    return
        except urllib.error.HTTPError as exc:
            if exc.code in (200, 403):
                return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    raise RuntimeError(f"{MINIO_CONTAINER} did not become ready in time")


def wait_for_postgres_container_healthy() -> None:
    """Public (module-level, no leading underscore) so test modules can
    re-wait after they themselves restart the container mid-test."""
    _wait_for_postgres()


def wait_for_minio_container_healthy() -> None:
    _wait_for_minio()


def _wait_for_clamav(timeout_s: float = 60.0) -> None:
    """Block until the disposable ``clamd`` daemon answers a real
    ``PING`` — same protocol check
    ``services.evidence.intake.scanner.ClamAVScanner.is_available()``
    itself performs, done directly here (not via importing that class)
    so this fixture module stays a plain, dependency-light bootstrapping
    layer."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", int(CLAMAV_HOST_PORT)), timeout=2) as sock:
                sock.sendall(b"zPING\0")
                if sock.recv(64).strip(b"\0") == b"PONG":
                    return
        except OSError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"{CLAMAV_CONTAINER} did not become ready (PING/PONG) in time")


def wait_for_clamav_container_healthy() -> None:
    """Public (module-level, no leading underscore) so test modules can
    re-wait after they themselves restart the container mid-test."""
    _wait_for_clamav()


@pytest.fixture(scope="module")
def runtime_stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict]:
    """Start disposable Postgres + MinIO containers, run `alembic
    upgrade head`, point BAGMAN_RUNTIME_ENV=production's environment
    variables at them for the life of this test module, then tear
    everything down (containers removed, environment restored)."""
    _docker("rm", "-f", PG_CONTAINER, check=False)
    _docker("rm", "-f", MINIO_CONTAINER, check=False)
    _docker("rm", "-f", CLAMAV_CONTAINER, check=False)

    secret_dir = tmp_path_factory.mktemp("bagman-test-wi3-secrets")

    pg_password = secrets.token_urlsafe(24)
    pg_password_file = secret_dir / "postgres_password"
    pg_password_file.write_text(pg_password, encoding="utf-8")
    pg_password_file.chmod(0o600)

    # CD-6 MinIO withdrawal WO: even though these are synthetic/
    # disposable test credentials, use the SAME static-identity
    # config-file mechanism production's SeaweedFS uses (see
    # `deployment/compose/docker-compose.yml`'s `bagman-objects`
    # service) for architectural parity — a `{"identities": [...]}`
    # JSON file mounted read-only, rather than any container-env-var
    # credential passing (SeaweedFS's S3 gateway has no MinIO-style
    # `MINIO_ROOT_USER`/`MINIO_ROOT_PASSWORD` env vars to begin with).
    minio_user = "bagmantestwi3"
    minio_password = secrets.token_urlsafe(24)
    minio_user_file = secret_dir / "minio_user"
    minio_user_file.write_text(minio_user, encoding="utf-8")
    minio_user_file.chmod(0o600)
    minio_password_file = secret_dir / "minio_password"
    minio_password_file.write_text(minio_password, encoding="utf-8")
    minio_password_file.chmod(0o600)

    s3_config_file = secret_dir / "s3.json"
    s3_config_file.write_text(
        json.dumps(
            {
                "identities": [
                    {
                        "name": "bagman-test",
                        "credentials": [{"accessKey": minio_user, "secretKey": minio_password}],
                        "actions": ["Admin", "Read", "List", "Write"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    # 0o644, NOT 0o600 like the other secret files above: this one
    # (uniquely, among this fixture's secret files) is bind-mounted
    # INTO the disposable SeaweedFS container for it to read from its
    # own process, rather than read only by this host-side Python
    # process. 0o600 here was observed, live, to intermittently fail
    # container startup with "permission denied" reading
    # /etc/seaweedfs/s3.json under this suite's real concurrent
    # container churn (many containers starting/stopping across
    # tests/app_api/'s test modules in quick succession) even though
    # it reads back fine in an isolated manual reproduction — this
    # disposable, synthetic-credential-only file does not need 0600's
    # extra restriction (the parent directory, created by
    # `tmp_path_factory`, already keeps other host users out), so the
    # safer fix is a mode that is unambiguously readable regardless of
    # exactly which UID the container's read happens under.
    s3_config_file.chmod(0o644)

    _docker(
        "run", "-d", "--name", PG_CONTAINER,
        "-e", f"POSTGRES_USER={PG_USER}",
        "-e", f"POSTGRES_PASSWORD={pg_password}",
        "-e", f"POSTGRES_DB={PG_DB}",
        "-p", f"{PG_HOST}:{PG_HOST_PORT}:5432",
        PG_IMAGE,
    )
    # CD-6 MinIO withdrawal WO: `weed server` (not `weed mini` — see
    # docker-compose.yml's `bagman-objects` comment for why), the
    # exact runtime shape verified live against BAGMAN's real
    # `MinIOObjectStore`, adapted only for this disposable CI-only
    # fixture's own "publish a host port for host-side test access"
    # pattern (production's own "no host port" rule doesn't apply
    # here — this container never exists outside a single CI/local
    # test run).
    _docker(
        "run", "-d", "--name", MINIO_CONTAINER,
        "-v", f"{s3_config_file}:/etc/seaweedfs/s3.json:ro",
        "-p", f"127.0.0.1:{MINIO_HOST_PORT}:9000",
        SEAWEEDFS_IMAGE,
        "server", "-dir=/data", "-ip=127.0.0.1", "-ip.bind=127.0.0.1",
        "-master=true", "-volume=true", "-filer=true", "-s3=true",
        "-s3.port=9000", "-s3.ip.bind=0.0.0.0",
        "-s3.config=/etc/seaweedfs/s3.json",
        "-s3.port.iceberg=0", "-s3.port.lance=0",
        "-volume.max=0", "-webdav=false",
    )
    # CD-4 WI-3: a real, disposable clamd daemon — production
    # composition's scanner is mandatory (PID §60), so this test
    # module's composition needs one genuinely reachable, exactly as it
    # needs a genuinely reachable Postgres/MinIO.
    _docker(
        "run", "-d", "--name", CLAMAV_CONTAINER,
        "-p", f"127.0.0.1:{CLAMAV_HOST_PORT}:3310",
        CLAMAV_IMAGE,
    )

    env_backup = dict(os.environ)
    os.environ["BAGMAN_RUNTIME_ENV"] = "production"
    os.environ["BAGMAN_DB_HOST"] = PG_HOST
    os.environ["BAGMAN_DB_PORT"] = PG_HOST_PORT
    os.environ["BAGMAN_DB_NAME"] = PG_DB
    os.environ["BAGMAN_DB_USER"] = PG_USER
    os.environ["BAGMAN_DB_PASSWORD_FILE"] = str(pg_password_file)
    os.environ["BAGMAN_OBJECT_STORE_ENDPOINT_URL"] = f"http://127.0.0.1:{MINIO_HOST_PORT}"
    os.environ["BAGMAN_OBJECT_STORE_BUCKET"] = MINIO_BUCKET
    os.environ["BAGMAN_OBJECT_STORE_ACCESS_KEY_FILE"] = str(minio_user_file)
    os.environ["BAGMAN_OBJECT_STORE_SECRET_KEY_FILE"] = str(minio_password_file)
    os.environ["BAGMAN_SCANNER_HOST"] = "127.0.0.1"
    os.environ["BAGMAN_SCANNER_PORT"] = CLAMAV_HOST_PORT

    try:
        _wait_for_postgres()
        _wait_for_minio()
        _wait_for_clamav()

        from alembic import command
        from alembic.config import Config

        alembic_cfg = Config(str(REPO_ROOT / "alembic.ini"))
        alembic_cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        command.upgrade(alembic_cfg, "head")

        yield {
            "pg_container": PG_CONTAINER,
            "minio_container": MINIO_CONTAINER,
            "clamav_container": CLAMAV_CONTAINER,
        }
    finally:
        from persistence.postgres import session as pg_session

        pg_session._engine_for_url.cache_clear()  # noqa: SLF001 - test-only cleanup

        from app.api.composition import reset_composition_for_tests

        reset_composition_for_tests()

        os.environ.clear()
        os.environ.update(env_backup)

        _docker("rm", "-f", PG_CONTAINER, check=False)
        _docker("rm", "-f", MINIO_CONTAINER, check=False)
        _docker("rm", "-f", CLAMAV_CONTAINER, check=False)


@pytest.fixture
def client(runtime_stack: dict):
    """A fresh ``TestClient`` (and a freshly-rebuilt composition) for
    each test — so no test observes a composition another test already
    put into a "just failed" state."""
    from app.api.composition import reset_composition_for_tests

    reset_composition_for_tests()

    from fastapi.testclient import TestClient

    from app.api.main import app

    with TestClient(app) as test_client:
        yield test_client

    reset_composition_for_tests()
