"""CD-6 Slice 2 PostgreSQL persistence proofs (PID §98.4, architect spec
§1-24) — ``persistence/postgres/xero_repository.py`` against a REAL,
disposable PostgreSQL container (``tests/persistence/conftest.py``'s
session-scoped ``postgres_container`` fixture, migrated via a real
``alembic upgrade head``, never manual SQL). Mirrors
``tests/persistence/test_needs_you_repository.py``'s own style and
rigor for the closest structural precedent.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core import identity
from core.errors import ConflictError, InvalidStateTransitionError, NotFoundError, ValidationError
from persistence.postgres.session import get_engine, session_scope
from persistence.postgres.xero_models import XeroAccountRow, XeroConnectionRow
from persistence.postgres.xero_repository import (
    PostgresOAuthStateRepository,
    PostgresXeroAccountRepository,
    PostgresXeroConnectionRepository,
    PostgresXeroSyncRunRepository,
)
from services.xero.account import RawXeroAccount
from services.xero.connection import PROVIDER_XERO
from services.xero.oauth_state import consume_state
from services.xero.sync import SyncFailureReason
from core.timestamps import utc_now


def _raw(account_id="A1", *, name="Advertising", status="ACTIVE", type_="EXPENSE") -> RawXeroAccount:
    return RawXeroAccount(
        account_id=account_id,
        code="400",
        name=name,
        type=type_,
        account_class="EXPENSE",
        tax_type="NONE",
        status=status,
        show_in_expense_claims=True,
        reporting_code=None,
        reporting_code_name=None,
        updated_date_utc=None,
    )


# ---------------------------------------------------------------------
# XeroConnection — creation, round trip, both uniqueness directions
# ---------------------------------------------------------------------


def test_connection_persists_and_is_retrievable_via_a_fresh_repository_instance(fresh_engine):
    repo = PostgresXeroConnectionRepository()
    entity_id = identity.generate_id()
    created = repo.begin_connect(entity_id=entity_id)
    assert created.status == "PENDING"

    fresh_repo = PostgresXeroConnectionRepository(engine=fresh_engine)
    fetched = fresh_repo.get_connection(created.xero_connection_id)
    assert fetched.entity_id == entity_id
    assert fetched.provider == PROVIDER_XERO


def test_entity_id_uniqueness_is_enforced_at_the_database_level_not_only_in_application_code():
    """architect spec §17: 'a real unique constraint/index, not merely
    application-level pre-checking'. Proven directly at the ORM/table
    level — bypassing `begin_connect`'s own resolve-or-create logic
    entirely — so this test cannot pass merely because the repository
    method happens to check first."""
    entity_id = identity.generate_id()
    now = utc_now()
    row_1 = XeroConnectionRow(
        xero_connection_id=identity.generate_id(), entity_id=entity_id, provider=PROVIDER_XERO,
        status="PENDING", created_at=now, updated_at=now, metadata_={},
    )
    row_2 = XeroConnectionRow(
        xero_connection_id=identity.generate_id(), entity_id=entity_id, provider=PROVIDER_XERO,
        status="PENDING", created_at=now, updated_at=now, metadata_={},
    )
    with session_scope(get_engine()) as session:
        session.add(row_1)
    with pytest.raises(IntegrityError):
        with session_scope(get_engine()) as session:
            session.add(row_2)


def test_tenant_id_uniqueness_is_enforced_at_the_database_level_across_two_entities():
    """The other direction of architect spec §17 — a Xero tenant maps
    to at most one BAGMAN entity, enforced by
    `uq_xero_connections_tenant_id`'s real partial unique index."""
    tenant_id = f"tenant-{identity.generate_id()}"
    now = utc_now()
    row_1 = XeroConnectionRow(
        xero_connection_id=identity.generate_id(), entity_id=identity.generate_id(), provider=PROVIDER_XERO,
        tenant_id=tenant_id, status="CONNECTED", created_at=now, updated_at=now, metadata_={},
    )
    row_2 = XeroConnectionRow(
        xero_connection_id=identity.generate_id(), entity_id=identity.generate_id(), provider=PROVIDER_XERO,
        tenant_id=tenant_id, status="CONNECTED", created_at=now, updated_at=now, metadata_={},
    )
    with session_scope(get_engine()) as session:
        session.add(row_1)
    with pytest.raises(IntegrityError):
        with session_scope(get_engine()) as session:
            session.add(row_2)


def test_multiple_null_tenant_ids_are_allowed_the_partial_index_only_guards_non_null():
    """Confirms the partial-index technique itself: many `PENDING`
    connections (all `tenant_id IS NULL`) coexist freely — only a real,
    non-null duplicate tenant_id is rejected."""
    now = utc_now()
    for _ in range(3):
        row = XeroConnectionRow(
            xero_connection_id=identity.generate_id(), entity_id=identity.generate_id(), provider=PROVIDER_XERO,
            tenant_id=None, status="PENDING", created_at=now, updated_at=now, metadata_={},
        )
        with session_scope(get_engine()) as session:
            session.add(row)  # never raises


def test_complete_connect_via_repository_raises_conflict_error_for_a_duplicate_tenant():
    """The application-facing path (`PostgresXeroConnectionRepository
    .complete_connect`) translates the real constraint violation into
    `core.errors.ConflictError` — the same proof as the domain-layer
    in-memory test, but against the real database's own constraint."""
    repo = PostgresXeroConnectionRepository()
    entity_a, entity_b = identity.generate_id(), identity.generate_id()
    tenant_id = f"tenant-{identity.generate_id()}"

    conn_a = repo.begin_connect(entity_id=entity_a)
    repo.complete_connect(conn_a.xero_connection_id, tenant_id=tenant_id, tenant_name="Acme", token_expires_at=None)

    conn_b = repo.begin_connect(entity_id=entity_b)
    with pytest.raises(ConflictError):
        repo.complete_connect(conn_b.xero_connection_id, tenant_id=tenant_id, tenant_name="Acme", token_expires_at=None)


def test_get_by_entity_returns_none_not_notfounderror_for_an_unknown_entity():
    repo = PostgresXeroConnectionRepository()
    assert repo.get_by_entity(identity.generate_id()) is None


def test_malformed_connection_id_lookup_returns_not_found_never_persistence_error():
    repo = PostgresXeroConnectionRepository()
    with pytest.raises(NotFoundError):
        repo.get_connection("not-a-valid-uuid")


# ---------------------------------------------------------------------
# XeroAccount — idempotency, cross-company isolation, history retention
# ---------------------------------------------------------------------


def test_account_upsert_accounts_tenant_account_uniqueness_enforced_at_db_level():
    tenant_id = f"tenant-{identity.generate_id()}"
    account_id = "DUPLICATE-A1"
    now = utc_now()
    row_1 = XeroAccountRow(
        xero_account_row_id=identity.generate_id(), entity_id=identity.generate_id(), tenant_id=tenant_id,
        account_id=account_id, name="First", type="EXPENSE", first_synced_at=now, last_synced_at=now,
        last_sync_run_id=identity.generate_id(),
    )
    row_2 = XeroAccountRow(
        xero_account_row_id=identity.generate_id(), entity_id=identity.generate_id(), tenant_id=tenant_id,
        account_id=account_id, name="Second", type="EXPENSE", first_synced_at=now, last_synced_at=now,
        last_sync_run_id=identity.generate_id(),
    )
    with session_scope(get_engine()) as session:
        session.add(row_1)
    with pytest.raises(IntegrityError):
        with session_scope(get_engine()) as session:
            session.add(row_2)


def test_upsert_accounts_is_idempotent_against_real_postgres_never_duplicates():
    repo = PostgresXeroAccountRepository()
    entity_id = identity.generate_id()
    tenant_id = f"tenant-{identity.generate_id()}"
    run_id = identity.generate_id()

    created, updated = repo.upsert_accounts(entity_id=entity_id, tenant_id=tenant_id, raws=[_raw(name="Original")], sync_run_id=run_id)
    assert created == 1 and updated == 0

    created2, updated2 = repo.upsert_accounts(entity_id=entity_id, tenant_id=tenant_id, raws=[_raw(name="Renamed")], sync_run_id=identity.generate_id())
    assert created2 == 0 and updated2 == 1

    accounts = repo.list_accounts(entity_id=entity_id)
    assert len(accounts) == 1
    assert accounts[0].name == "Renamed"


def test_upsert_accounts_batch_is_atomic_a_mid_batch_failure_writes_nothing():
    """architect spec §5/§20's headline guarantee, proven against the
    REAL database transaction: a batch of three accounts where the
    THIRD is malformed (fails contract validation — an empty `name`)
    must leave ZERO of the three committed, not the first two."""
    repo = PostgresXeroAccountRepository()
    entity_id = identity.generate_id()
    tenant_id = f"tenant-{identity.generate_id()}"

    good_1 = _raw(account_id="G1", name="Good One")
    good_2 = _raw(account_id="G2", name="Good Two")
    # An empty `name` fails `contracts/xero/bagman.xero_account.v1.schema.json`'s
    # own `minLength: 1` constraint — a genuine, realistic malformed-
    # response shape (architect spec §4's own "malformed provider
    # responses... fail the sync run cleanly").
    broken = RawXeroAccount(
        account_id="BROKEN", code=None, name="", type="EXPENSE", account_class=None, tax_type=None,
        status="ACTIVE", show_in_expense_claims=None, reporting_code=None, reporting_code_name=None,
        updated_date_utc=None,
    )

    with pytest.raises(ValidationError):
        repo.upsert_accounts(entity_id=entity_id, tenant_id=tenant_id, raws=[good_1, good_2, broken], sync_run_id=identity.generate_id())

    # Nothing from this failed batch was committed — not even the two
    # good rows that were processed before the broken one.
    accounts = repo.list_accounts(entity_id=entity_id)
    assert accounts == []


def test_archived_account_retained_across_a_real_resync_never_deleted():
    repo = PostgresXeroAccountRepository()
    entity_id = identity.generate_id()
    tenant_id = f"tenant-{identity.generate_id()}"
    repo.upsert_accounts(entity_id=entity_id, tenant_id=tenant_id, raws=[_raw(status="ACTIVE")], sync_run_id=identity.generate_id())
    repo.upsert_accounts(entity_id=entity_id, tenant_id=tenant_id, raws=[_raw(status="ARCHIVED")], sync_run_id=identity.generate_id())

    accounts = repo.list_accounts(entity_id=entity_id)
    assert len(accounts) == 1
    assert accounts[0].status == "ARCHIVED"


def test_cross_company_isolation_against_real_postgres():
    """architect spec §29 — the same adversarial proof as the in-memory
    domain test, now against the real database (a real SQL `WHERE
    entity_id = ...` — this is the proof that actually matters)."""
    repo = PostgresXeroAccountRepository()
    entity_a, entity_b = identity.generate_id(), identity.generate_id()
    tenant_a, tenant_b = f"tenant-{identity.generate_id()}", f"tenant-{identity.generate_id()}"

    repo.upsert_accounts(entity_id=entity_a, tenant_id=tenant_a, raws=[_raw(account_id="SAME", name="Company A's account")], sync_run_id=identity.generate_id())
    repo.upsert_accounts(entity_id=entity_b, tenant_id=tenant_b, raws=[_raw(account_id="SAME", name="Company B's account")], sync_run_id=identity.generate_id())

    accounts_a = repo.list_accounts(entity_id=entity_a)
    accounts_b = repo.list_accounts(entity_id=entity_b)
    assert [a.name for a in accounts_a] == ["Company A's account"]
    assert [a.name for a in accounts_b] == ["Company B's account"]


# ---------------------------------------------------------------------
# XeroSyncRun
# ---------------------------------------------------------------------


def test_sync_run_create_succeed_round_trip(fresh_engine):
    repo = PostgresXeroSyncRunRepository()
    entity_id = identity.generate_id()
    run = repo.create_run(entity_id=entity_id, tenant_id="t1")
    assert run.status == "RUNNING"

    succeeded = repo.succeed_run(run.sync_run_id, accounts_seen_count=5, accounts_created_count=5, accounts_updated_count=0)
    assert succeeded.status == "SUCCEEDED"
    assert succeeded.completed_at is not None

    fresh_repo = PostgresXeroSyncRunRepository(engine=fresh_engine)
    fetched = fresh_repo.get_run(run.sync_run_id)
    assert fetched.status == "SUCCEEDED"
    assert fetched.accounts_seen_count == 5


def test_sync_run_fail_records_error_code_and_detail():
    repo = PostgresXeroSyncRunRepository()
    run = repo.create_run(entity_id=identity.generate_id(), tenant_id="t1")
    failed = repo.fail_run(run.sync_run_id, error_code=SyncFailureReason.PROVIDER_ERROR.value, error_detail="HTTP 500")
    assert failed.status == "FAILED"
    assert failed.error_code == "PROVIDER_ERROR"
    assert failed.error_detail == "HTTP 500"


def test_sync_run_cannot_transition_out_of_a_terminal_state():
    repo = PostgresXeroSyncRunRepository()
    run = repo.create_run(entity_id=identity.generate_id(), tenant_id="t1")
    repo.succeed_run(run.sync_run_id, accounts_seen_count=1, accounts_created_count=1, accounts_updated_count=0)
    with pytest.raises(InvalidStateTransitionError):
        repo.fail_run(run.sync_run_id, error_code="X", error_detail="y")


def test_list_runs_scoped_to_entity_and_most_recent_first():
    repo = PostgresXeroSyncRunRepository()
    entity_id = identity.generate_id()
    other_entity = identity.generate_id()
    run_1 = repo.create_run(entity_id=entity_id, tenant_id="t1")
    repo.succeed_run(run_1.sync_run_id, accounts_seen_count=1, accounts_created_count=1, accounts_updated_count=0)
    run_2 = repo.create_run(entity_id=entity_id, tenant_id="t1")
    repo.fail_run(run_2.sync_run_id, error_code="X", error_detail="y")
    repo.create_run(entity_id=other_entity, tenant_id="t2")  # must never appear below

    runs = repo.list_runs(entity_id=entity_id)
    assert [r.sync_run_id for r in runs] == [run_2.sync_run_id, run_1.sync_run_id]


# ---------------------------------------------------------------------
# OAuthState — real DB-backed anti-replay proof
# ---------------------------------------------------------------------


def test_oauth_state_real_db_replay_is_rejected():
    from core.errors import OAuthStateError

    repo = PostgresOAuthStateRepository()
    entity_id = identity.generate_id()
    state = repo.create_state(entity_id=entity_id)

    consumed = consume_state(repo, state.state)
    assert consumed.entity_id == entity_id

    with pytest.raises(OAuthStateError):
        consume_state(repo, state.state)


def test_oauth_state_persists_and_is_retrievable_via_a_fresh_repository_instance(fresh_engine):
    repo = PostgresOAuthStateRepository()
    state = repo.create_state(entity_id=identity.generate_id())

    fresh_repo = PostgresOAuthStateRepository(engine=fresh_engine)
    fetched = fresh_repo.get_state(state.state)
    assert fetched is not None
    assert fetched.consumed_at is None


def test_postgres_mark_consumed_is_itself_the_authoritative_gate_not_just_the_precheck():
    """`consume_state()`'s own unlocked pre-check (`get_state`) is not
    the real race-safety guarantee -- two near-simultaneous callback
    requests presenting the same `state` could both pass it before
    either reaches the repository's own `SELECT ... FOR UPDATE`. This
    calls `PostgresOAuthStateRepository.mark_consumed` directly, twice,
    bypassing `consume_state` entirely, to prove the real row-locked
    Postgres implementation re-validates `consumed_at` INSIDE its own
    transaction rather than trusting a caller who already checked
    once."""
    from core.errors import OAuthStateError

    repo = PostgresOAuthStateRepository()
    state = repo.create_state(entity_id=identity.generate_id())

    first = repo.mark_consumed(state.state)
    assert first.is_consumed

    with pytest.raises(OAuthStateError):
        repo.mark_consumed(state.state)


def test_postgres_mark_consumed_rejects_expiry_under_its_own_lock_too():
    """Same discipline, the expiry half: the real row-locked
    implementation must refuse an expired row even when called
    directly, not only via `consume_state`'s own pre-check."""
    from datetime import timedelta

    from core.errors import OAuthStateError

    repo = PostgresOAuthStateRepository()
    state = repo.create_state(entity_id=identity.generate_id())
    far_future = state.expires_at + timedelta(seconds=1)

    with pytest.raises(OAuthStateError):
        repo.mark_consumed(state.state, now=far_future)
