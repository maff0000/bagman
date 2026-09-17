"""Pure domain-level tests for CD-6 Slice 2's Xero reference-data
domain (PID §98.4, architect spec §1-24) — ``services/xero/*.py``
against their in-memory reference repositories, no HTTP layer, no
database, no Docker. Mirrors the fast, dependency-free style
``tests/integration/test_ai_invocation_domain.py`` already establishes
for `AIInvocation`'s own state machine.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core import identity
from core.errors import ConflictError, InvalidStateTransitionError, NotFoundError, OAuthStateError, ValidationError
from services.xero.account import InMemoryXeroAccountRepository, RawXeroAccount
from services.xero.ai_suggestion import UNRESOLVED, resolve_ai_suggested_account
from services.xero.client import (
    XeroAccountsResult,
    XeroConnectionInfo,
    XeroConnectionsResult,
    XeroOutcomeStatus,
    XeroTokenResult,
)
from services.xero.connection import ALLOWED_TRANSITIONS, InMemoryXeroConnectionRepository, transition
from services.xero.eligibility import is_eligible_by_default, list_eligible_accounts
from services.xero.fake_client import FakeXeroAccountingClient, FakeXeroOAuthClient, fake_token_bundle
from services.xero.oauth_state import InMemoryOAuthStateRepository, STATE_TTL_SECONDS, consume_state
from services.xero.secrets import InMemoryTokenStore
from services.xero.sync import (
    InMemoryXeroSyncRunRepository,
    REFERENCE_DATA_STALE_THRESHOLD_SECONDS,
    SyncFailureReason,
    is_reference_data_stale,
    run_sync,
)


def _eid() -> str:
    return identity.generate_id()


def _raw(account_id="A1", *, name="Advertising", type_="EXPENSE", status="ACTIVE", code="400", show_in_expense_claims=True) -> RawXeroAccount:
    return RawXeroAccount(
        account_id=account_id,
        code=code,
        name=name,
        type=type_,
        account_class="EXPENSE",
        tax_type="NONE",
        status=status,
        show_in_expense_claims=show_in_expense_claims,
        reporting_code=None,
        reporting_code_name=None,
        updated_date_utc=None,
    )


# ---------------------------------------------------------------------
# XeroConnection state machine
# ---------------------------------------------------------------------


def test_begin_connect_creates_a_pending_row_for_a_new_entity():
    repo = InMemoryXeroConnectionRepository()
    entity_id = _eid()
    conn = repo.begin_connect(entity_id=entity_id)
    assert conn.status == "PENDING"
    assert conn.entity_id == entity_id
    assert conn.tenant_id is None
    assert conn.provider == "XERO"


def test_begin_connect_reuses_the_same_row_for_the_same_entity():
    repo = InMemoryXeroConnectionRepository()
    entity_id = _eid()
    first = repo.begin_connect(entity_id=entity_id)
    second = repo.begin_connect(entity_id=entity_id)
    assert first.xero_connection_id == second.xero_connection_id


def test_complete_connect_transitions_pending_to_connected_with_tenant():
    repo = InMemoryXeroConnectionRepository()
    entity_id = _eid()
    conn = repo.begin_connect(entity_id=entity_id)
    completed = repo.complete_connect(
        conn.xero_connection_id, tenant_id="tenant-1", tenant_name="Acme", token_expires_at=None
    )
    assert completed.status == "CONNECTED"
    assert completed.tenant_id == "tenant-1"
    assert completed.connected_at is not None


def test_complete_connect_rejects_a_tenant_already_mapped_to_a_different_entity():
    """Architect spec §17 — the SECOND direction of the anti-duplicate-
    mapping guarantee: one Xero tenant maps to at most one BAGMAN
    entity."""
    repo = InMemoryXeroConnectionRepository()
    entity_a, entity_b = _eid(), _eid()
    conn_a = repo.begin_connect(entity_id=entity_a)
    repo.complete_connect(conn_a.xero_connection_id, tenant_id="shared-tenant", tenant_name="Acme", token_expires_at=None)

    conn_b = repo.begin_connect(entity_id=entity_b)
    with pytest.raises(ConflictError):
        repo.complete_connect(conn_b.xero_connection_id, tenant_id="shared-tenant", tenant_name="Acme", token_expires_at=None)


def test_disconnect_clears_tenant_identity_and_is_reconnectable():
    repo = InMemoryXeroConnectionRepository()
    entity_id = _eid()
    conn = repo.begin_connect(entity_id=entity_id)
    conn = repo.complete_connect(conn.xero_connection_id, tenant_id="tenant-1", tenant_name="Acme", token_expires_at=None)

    disconnected = repo.disconnect(conn.xero_connection_id)
    assert disconnected.status == "DISCONNECTED"
    assert disconnected.tenant_id is None
    assert disconnected.tenant_name is None

    # A DIFFERENT entity can now claim the same tenant_id — the
    # original mapping released it (architect spec §17's own "unless
    # explicitly supported" carve-out does not apply here; this is a
    # genuinely fresh mapping after an explicit disconnect).
    other_entity = _eid()
    other_conn = repo.begin_connect(entity_id=other_entity)
    completed = repo.complete_connect(
        other_conn.xero_connection_id, tenant_id="tenant-1", tenant_name="Acme", token_expires_at=None
    )
    assert completed.status == "CONNECTED"


def test_fail_refresh_moves_connected_to_error_and_can_recover():
    repo = InMemoryXeroConnectionRepository()
    entity_id = _eid()
    conn = repo.begin_connect(entity_id=entity_id)
    conn = repo.complete_connect(conn.xero_connection_id, tenant_id="tenant-1", tenant_name="Acme", token_expires_at=None)

    errored = repo.fail_refresh(conn.xero_connection_id, error_detail="invalid_grant")
    assert errored.status == "ERROR"
    assert errored.error_detail == "invalid_grant"

    recovered = repo.record_sync_success(conn.xero_connection_id, tenant_name="Acme", token_expires_at=None)
    assert recovered.status == "CONNECTED"
    assert recovered.error_detail is None


def test_fail_auth_revoked_moves_connected_to_revoked():
    repo = InMemoryXeroConnectionRepository()
    entity_id = _eid()
    conn = repo.begin_connect(entity_id=entity_id)
    conn = repo.complete_connect(conn.xero_connection_id, tenant_id="tenant-1", tenant_name="Acme", token_expires_at=None)
    revoked = repo.fail_auth_revoked(conn.xero_connection_id, error_detail="403")
    assert revoked.status == "REVOKED"
    assert revoked.tenant_id is None


@pytest.mark.parametrize(
    "status,forbidden_target",
    [
        ("PENDING", "REVOKED"),
        ("DISCONNECTED", "CONNECTED"),  # DISCONNECTED can only ever -> PENDING
        ("REVOKED", "DISCONNECTED"),
        ("REVOKED", "CONNECTED"),
    ],
)
def test_illegal_transitions_are_rejected(status, forbidden_target):
    now = datetime.now(timezone.utc)
    from services.xero.connection import PROVIDER_XERO, XeroConnection

    connection = XeroConnection(
        xero_connection_id=_eid(),
        entity_id=_eid(),
        provider=PROVIDER_XERO,
        tenant_id=None,
        tenant_name=None,
        status=status,
        connected_at=None,
        last_successful_sync_at=None,
        last_attempted_sync_at=None,
        token_expires_at=None,
        created_at=now,
        updated_at=now,
    )
    with pytest.raises(InvalidStateTransitionError):
        transition(connection, forbidden_target)


def test_every_documented_transition_is_reachable_and_matches_allowed_transitions_table():
    """A direct proof that `ALLOWED_TRANSITIONS` is exactly what the
    module docstring documents — not a stale comment."""
    assert ALLOWED_TRANSITIONS == {
        "PENDING": frozenset({"CONNECTED", "ERROR", "DISCONNECTED"}),
        "CONNECTED": frozenset({"ERROR", "REVOKED", "DISCONNECTED", "PENDING"}),
        "ERROR": frozenset({"CONNECTED", "DISCONNECTED", "PENDING"}),
        "DISCONNECTED": frozenset({"PENDING"}),
        "REVOKED": frozenset({"PENDING"}),
    }


# ---------------------------------------------------------------------
# OAuthState anti-CSRF/replay
# ---------------------------------------------------------------------


def test_oauth_state_round_trip_succeeds_exactly_once():
    repo = InMemoryOAuthStateRepository()
    entity_id = _eid()
    state = repo.create_state(entity_id=entity_id)
    consumed = consume_state(repo, state.state)
    assert consumed.entity_id == entity_id
    assert consumed.is_consumed


def test_oauth_state_replay_is_rejected():
    repo = InMemoryOAuthStateRepository()
    state = repo.create_state(entity_id=_eid())
    consume_state(repo, state.state)
    with pytest.raises(OAuthStateError):
        consume_state(repo, state.state)


def test_oauth_state_unknown_value_is_rejected():
    repo = InMemoryOAuthStateRepository()
    with pytest.raises(OAuthStateError):
        consume_state(repo, "a-value-nobody-ever-minted")


def test_oauth_state_expired_value_is_rejected():
    repo = InMemoryOAuthStateRepository()
    state = repo.create_state(entity_id=_eid())
    far_future = state.expires_at + timedelta(seconds=1)
    with pytest.raises(OAuthStateError):
        consume_state(repo, state.state, now=far_future)


def test_oauth_state_ttl_matches_documented_constant():
    assert STATE_TTL_SECONDS == 600.0


def test_mark_consumed_itself_is_the_authoritative_gate_not_just_the_precheck():
    """A real, live-findable TOCTOU race: `consume_state()`'s own
    `is_consumed` check runs UNLOCKED, before `mark_consumed` is ever
    called — so two near-simultaneous callers presenting the same
    `state` could both pass that pre-check before either reaches the
    repository's own locked write. This test bypasses `consume_state`
    entirely and calls `mark_consumed` directly twice, proving the
    REPOSITORY is the real, authoritative gate (re-validates under its
    own lock) rather than merely trusting a caller who already checked
    once — the exact "re-check under the lock, never trust a pre-lock
    read alone" discipline `ai.invocation._recover_if_stale` already
    establishes elsewhere in this same delivery."""
    repo = InMemoryOAuthStateRepository()
    state = repo.create_state(entity_id=_eid())

    first = repo.mark_consumed(state.state)
    assert first.is_consumed

    with pytest.raises(OAuthStateError):
        repo.mark_consumed(state.state)


def test_mark_consumed_rejects_expiry_under_its_own_lock_too():
    """Same discipline, the expiry half: `mark_consumed` itself must
    refuse an expired row even when called directly, not only via
    `consume_state`'s own pre-check."""
    repo = InMemoryOAuthStateRepository()
    state = repo.create_state(entity_id=_eid())
    far_future = state.expires_at + timedelta(seconds=1)

    with pytest.raises(OAuthStateError):
        repo.mark_consumed(state.state, now=far_future)


# ---------------------------------------------------------------------
# XeroAccount idempotency + history preservation
# ---------------------------------------------------------------------


def test_upsert_account_is_idempotent_never_duplicates():
    repo = InMemoryXeroAccountRepository()
    entity_id = _eid()
    repo.upsert_account(entity_id=entity_id, tenant_id="t1", raw=_raw(name="Advertising"), sync_run_id=_eid())
    repo.upsert_account(entity_id=entity_id, tenant_id="t1", raw=_raw(name="Advertising Renamed"), sync_run_id=_eid())
    accounts = repo.list_accounts(entity_id=entity_id)
    assert len(accounts) == 1
    assert accounts[0].name == "Advertising Renamed"


def test_upsert_account_preserves_identity_and_first_synced_at_across_updates():
    repo = InMemoryXeroAccountRepository()
    entity_id = _eid()
    first = repo.upsert_account(entity_id=entity_id, tenant_id="t1", raw=_raw(), sync_run_id=_eid())
    second = repo.upsert_account(entity_id=entity_id, tenant_id="t1", raw=_raw(name="Renamed"), sync_run_id=_eid())
    assert first.xero_account_row_id == second.xero_account_row_id
    assert first.first_synced_at == second.first_synced_at


def test_archived_account_is_retained_not_deleted_and_still_resolves():
    """Architect spec §5/§6 — inactive/archived accounts are retained
    historically, never silently removed."""
    repo = InMemoryXeroAccountRepository()
    entity_id = _eid()
    repo.upsert_account(entity_id=entity_id, tenant_id="t1", raw=_raw(status="ACTIVE"), sync_run_id=_eid())
    archived = repo.upsert_account(entity_id=entity_id, tenant_id="t1", raw=_raw(status="ARCHIVED"), sync_run_id=_eid())
    assert archived.status == "ARCHIVED"
    # still resolvable by its row id and by external id — never deleted
    resolved = repo.get_account(archived.xero_account_row_id)
    assert resolved.status == "ARCHIVED"
    by_external = repo.get_by_external_id(tenant_id="t1", account_id="A1")
    assert by_external is not None and by_external.status == "ARCHIVED"


def test_cross_company_isolation_list_accounts_never_leaks_another_entitys_rows():
    """Architect spec §29 — explicitly named as something that "must be
    tested", not merely asserted. An adversarial-shaped proof: two
    companies' accounts share the SAME account_id/code/name under
    DIFFERENT tenants, and each company's own `list_accounts(entity_id=...)`
    call must return only its own."""
    repo = InMemoryXeroAccountRepository()
    entity_a, entity_b = _eid(), _eid()
    repo.upsert_account(entity_id=entity_a, tenant_id="tenant-a", raw=_raw(account_id="A1", name="Company A's account"), sync_run_id=_eid())
    repo.upsert_account(entity_id=entity_b, tenant_id="tenant-b", raw=_raw(account_id="A1", name="Company B's account"), sync_run_id=_eid())

    accounts_a = repo.list_accounts(entity_id=entity_a)
    accounts_b = repo.list_accounts(entity_id=entity_b)
    assert len(accounts_a) == 1 and accounts_a[0].name == "Company A's account"
    assert len(accounts_b) == 1 and accounts_b[0].name == "Company B's account"
    assert accounts_a[0].xero_account_row_id != accounts_b[0].xero_account_row_id


# ---------------------------------------------------------------------
# Eligibility filtering policy
# ---------------------------------------------------------------------


def test_archived_and_deleted_accounts_excluded_from_default_eligibility():
    assert not is_eligible_by_default(_as_account(_raw(status="ARCHIVED")))
    assert not is_eligible_by_default(_as_account(_raw(status="DELETED")))
    assert is_eligible_by_default(_as_account(_raw(status="ACTIVE")))


def test_unfamiliar_status_value_defaults_to_eligible():
    """Architect spec §6's own 'expose more rather than silently invent
    accounting policy' — an unrecognised status is never excluded."""
    assert is_eligible_by_default(_as_account(_raw(status="SOME_FUTURE_XERO_STATUS")))


def test_bank_type_excluded_but_other_unfamiliar_types_remain_eligible():
    assert not is_eligible_by_default(_as_account(_raw(type_="BANK")))
    assert is_eligible_by_default(_as_account(_raw(type_="SOME_FUTURE_TYPE")))


def test_list_eligible_accounts_never_hides_a_historically_referenced_account():
    archived = _as_account(_raw(account_id="OLD1", status="ARCHIVED"))
    active = _as_account(_raw(account_id="NEW1", status="ACTIVE"))
    result = list_eligible_accounts([archived, active], referenced_account_ids=frozenset({"OLD1"}))
    ids = {a.account_id for a in result}
    assert ids == {"OLD1", "NEW1"}

    # Without the referenced-account carve-out, the archived one is
    # correctly excluded — proves the carve-out is doing real work.
    result_without = list_eligible_accounts([archived, active])
    assert {a.account_id for a in result_without} == {"NEW1"}


def _as_account(raw: RawXeroAccount):
    from services.xero.account import XeroAccount

    now = datetime.now(timezone.utc)
    return XeroAccount(
        xero_account_row_id=_eid(),
        entity_id=_eid(),
        tenant_id="t1",
        account_id=raw.account_id,
        code=raw.code,
        name=raw.name,
        type=raw.type,
        account_class=raw.account_class,
        tax_type=raw.tax_type,
        status=raw.status,
        show_in_expense_claims=raw.show_in_expense_claims,
        reporting_code=None,
        reporting_code_name=None,
        source_updated_date_utc=None,
        first_synced_at=now,
        last_synced_at=now,
        last_sync_run_id=_eid(),
    )


# ---------------------------------------------------------------------
# AI-suggestion candidate-set constraint (architect spec §6)
# ---------------------------------------------------------------------


def test_ai_suggestion_within_candidate_set_resolves():
    candidates = [_as_account(_raw(account_id="A1"))]
    result = resolve_ai_suggested_account("A1", eligible_candidates=candidates)
    assert result.resolved
    assert result.account_id == "A1"


def test_ai_suggestion_outside_candidate_set_is_rejected_as_unresolved():
    """The exact, required 'fake/scripted test proving an out-of-set
    suggestion is rejected/treated as UNRESOLVED, never passed through'
    — a scripted fake AI response proposing an AccountID that was never
    in the candidate list supplied to it."""
    candidates = [_as_account(_raw(account_id="A1")), _as_account(_raw(account_id="A2"))]
    scripted_fake_ai_response_account_id = "A999-INVENTED-BY-MODEL"
    result = resolve_ai_suggested_account(scripted_fake_ai_response_account_id, eligible_candidates=candidates)
    assert not result.resolved
    assert result.account_id is None
    assert "not one of" in result.reason


def test_ai_suggestion_none_is_unresolved_not_an_error():
    result = resolve_ai_suggested_account(None, eligible_candidates=[])
    assert not result.resolved


def test_unresolved_sentinel_is_the_documented_literal_string():
    assert UNRESOLVED == "UNRESOLVED"


# ---------------------------------------------------------------------
# Sync orchestration (services.xero.sync.run_sync)
# ---------------------------------------------------------------------


def _connected_fixture():
    conn_repo = InMemoryXeroConnectionRepository()
    acct_repo = InMemoryXeroAccountRepository()
    run_repo = InMemoryXeroSyncRunRepository()
    token_store = InMemoryTokenStore()
    oauth_client = FakeXeroOAuthClient()
    acct_client = FakeXeroAccountingClient()

    entity_id = _eid()
    conn = conn_repo.begin_connect(entity_id=entity_id)
    conn = conn_repo.complete_connect(conn.xero_connection_id, tenant_id="t1", tenant_name="Acme", token_expires_at=None)
    token_store.write(entity_id, access_token="tok", refresh_token="ref", expires_at=fake_token_bundle().expires_at)
    return entity_id, conn_repo, acct_repo, run_repo, token_store, oauth_client, acct_client


def test_run_sync_succeeds_and_creates_accounts():
    entity_id, conn_repo, acct_repo, run_repo, token_store, oauth_client, acct_client = _connected_fixture()
    acct_client.queue_accounts_result(XeroAccountsResult(status=XeroOutcomeStatus.OK, accounts=(_raw(),)))

    run = run_sync(
        entity_id=entity_id,
        connection_repository=conn_repo,
        account_repository=acct_repo,
        sync_run_repository=run_repo,
        accounting_client=acct_client,
        oauth_client=oauth_client,
        token_store=token_store,
    )
    assert run.status == "SUCCEEDED"
    assert run.accounts_created_count == 1
    connection = conn_repo.get_by_entity(entity_id)
    assert connection.last_successful_sync_at is not None


def test_run_sync_without_a_connection_raises_conflict():
    conn_repo = InMemoryXeroConnectionRepository()
    with pytest.raises(ConflictError):
        run_sync(
            entity_id=_eid(),
            connection_repository=conn_repo,
            account_repository=InMemoryXeroAccountRepository(),
            sync_run_repository=InMemoryXeroSyncRunRepository(),
            accounting_client=FakeXeroAccountingClient(),
            oauth_client=FakeXeroOAuthClient(),
            token_store=InMemoryTokenStore(),
        )


def test_run_sync_failed_sync_leaves_last_known_good_projection_intact():
    """Architect spec §5/§20's headline guarantee, tested directly: sync
    succeeds, sync fails, the projection is UNCHANGED — only the
    sync-run/connection status reflects the failure."""
    entity_id, conn_repo, acct_repo, run_repo, token_store, oauth_client, acct_client = _connected_fixture()
    acct_client.queue_accounts_result(XeroAccountsResult(status=XeroOutcomeStatus.OK, accounts=(_raw(name="Original"),)))
    run_sync(
        entity_id=entity_id, connection_repository=conn_repo, account_repository=acct_repo,
        sync_run_repository=run_repo, accounting_client=acct_client, oauth_client=oauth_client, token_store=token_store,
    )
    before = acct_repo.list_accounts(entity_id=entity_id)
    assert len(before) == 1 and before[0].name == "Original"

    acct_client.queue_accounts_result(XeroAccountsResult(status=XeroOutcomeStatus.PROVIDER_ERROR, error_detail="500"))
    failed_run = run_sync(
        entity_id=entity_id, connection_repository=conn_repo, account_repository=acct_repo,
        sync_run_repository=run_repo, accounting_client=acct_client, oauth_client=oauth_client, token_store=token_store,
    )
    assert failed_run.status == "FAILED"
    assert failed_run.error_code == SyncFailureReason.PROVIDER_ERROR.value

    after = acct_repo.list_accounts(entity_id=entity_id)
    assert len(after) == 1 and after[0].name == "Original"  # UNCHANGED


def test_run_sync_401_transitions_connection_to_revoked_after_refresh_attempt_fails():
    entity_id, conn_repo, acct_repo, run_repo, token_store, oauth_client, acct_client = _connected_fixture()
    acct_client.queue_accounts_result(XeroAccountsResult(status=XeroOutcomeStatus.AUTH_ERROR, error_detail="401"))
    oauth_client.queue_refresh_result(XeroTokenResult(status=XeroOutcomeStatus.OK, tokens=fake_token_bundle()))
    acct_client.queue_accounts_result(XeroAccountsResult(status=XeroOutcomeStatus.AUTH_ERROR, error_detail="401 again"))

    run = run_sync(
        entity_id=entity_id, connection_repository=conn_repo, account_repository=acct_repo,
        sync_run_repository=run_repo, accounting_client=acct_client, oauth_client=oauth_client, token_store=token_store,
    )
    assert run.status == "FAILED"
    assert run.error_code == SyncFailureReason.AUTH_REVOKED.value
    connection = conn_repo.get_by_entity(entity_id)
    assert connection.status == "REVOKED"


def test_run_sync_token_refresh_failure_transitions_connection_to_error():
    entity_id, conn_repo, acct_repo, run_repo, token_store, oauth_client, acct_client = _connected_fixture()
    # Force the pre-emptive refresh path: store an already-expired token.
    token_store.write(entity_id, access_token="stale", refresh_token="ref", expires_at=datetime.now(timezone.utc) - timedelta(seconds=5))
    oauth_client.queue_refresh_result(XeroTokenResult(status=XeroOutcomeStatus.AUTH_ERROR, error_detail="invalid_grant"))

    run = run_sync(
        entity_id=entity_id, connection_repository=conn_repo, account_repository=acct_repo,
        sync_run_repository=run_repo, accounting_client=acct_client, oauth_client=oauth_client, token_store=token_store,
    )
    assert run.status == "FAILED"
    assert run.error_code == SyncFailureReason.TOKEN_REFRESH_FAILED.value
    connection = conn_repo.get_by_entity(entity_id)
    assert connection.status == "ERROR"


def test_run_sync_rate_limited_retries_once_then_fails():
    entity_id, conn_repo, acct_repo, run_repo, token_store, oauth_client, acct_client = _connected_fixture()
    acct_client.queue_accounts_result(XeroAccountsResult(status=XeroOutcomeStatus.RATE_LIMITED, retry_after_seconds=0.01))
    acct_client.queue_accounts_result(XeroAccountsResult(status=XeroOutcomeStatus.RATE_LIMITED, retry_after_seconds=0.01))

    slept = []
    run = run_sync(
        entity_id=entity_id, connection_repository=conn_repo, account_repository=acct_repo,
        sync_run_repository=run_repo, accounting_client=acct_client, oauth_client=oauth_client, token_store=token_store,
        sleep_fn=slept.append,
    )
    assert run.status == "FAILED"
    assert run.error_code == SyncFailureReason.RATE_LIMITED.value
    assert len(acct_client.calls) == 2  # exactly one bounded retry, never unbounded
    assert len(slept) == 1


def test_run_sync_malformed_response_fails_cleanly():
    entity_id, conn_repo, acct_repo, run_repo, token_store, oauth_client, acct_client = _connected_fixture()
    acct_client.queue_accounts_result(XeroAccountsResult(status=XeroOutcomeStatus.MALFORMED_RESPONSE, error_detail="bad json"))
    run = run_sync(
        entity_id=entity_id, connection_repository=conn_repo, account_repository=acct_repo,
        sync_run_repository=run_repo, accounting_client=acct_client, oauth_client=oauth_client, token_store=token_store,
    )
    assert run.status == "FAILED"
    assert run.error_code == SyncFailureReason.MALFORMED_RESPONSE.value


def test_run_sync_missing_tokens_fails_as_config_error():
    conn_repo = InMemoryXeroConnectionRepository()
    entity_id = _eid()
    conn = conn_repo.begin_connect(entity_id=entity_id)
    conn_repo.complete_connect(conn.xero_connection_id, tenant_id="t1", tenant_name="Acme", token_expires_at=None)
    run = run_sync(
        entity_id=entity_id,
        connection_repository=conn_repo,
        account_repository=InMemoryXeroAccountRepository(),
        sync_run_repository=InMemoryXeroSyncRunRepository(),
        accounting_client=FakeXeroAccountingClient(),
        oauth_client=FakeXeroOAuthClient(),
        token_store=InMemoryTokenStore(),  # nothing written
    )
    assert run.status == "FAILED"
    assert run.error_code == SyncFailureReason.CONFIG_ERROR.value


# ---------------------------------------------------------------------
# Stale sync/reference-data threshold (architect spec §29)
# ---------------------------------------------------------------------


def test_reference_data_stale_threshold_is_documented_and_finite():
    assert REFERENCE_DATA_STALE_THRESHOLD_SECONDS == 24 * 60 * 60.0


def test_reference_data_never_synced_is_stale():
    entity_id, conn_repo, *_ = _connected_fixture()
    connection = conn_repo.get_by_entity(entity_id)
    assert connection.last_successful_sync_at is None
    assert is_reference_data_stale(connection)


def test_reference_data_just_synced_is_not_stale():
    entity_id, conn_repo, acct_repo, run_repo, token_store, oauth_client, acct_client = _connected_fixture()
    acct_client.queue_accounts_result(XeroAccountsResult(status=XeroOutcomeStatus.OK, accounts=(_raw(),)))
    run_sync(
        entity_id=entity_id, connection_repository=conn_repo, account_repository=acct_repo,
        sync_run_repository=run_repo, accounting_client=acct_client, oauth_client=oauth_client, token_store=token_store,
    )
    connection = conn_repo.get_by_entity(entity_id)
    assert not is_reference_data_stale(connection)


def test_reference_data_synced_long_ago_is_stale():
    entity_id, conn_repo, acct_repo, run_repo, token_store, oauth_client, acct_client = _connected_fixture()
    acct_client.queue_accounts_result(XeroAccountsResult(status=XeroOutcomeStatus.OK, accounts=(_raw(),)))
    run_sync(
        entity_id=entity_id, connection_repository=conn_repo, account_repository=acct_repo,
        sync_run_repository=run_repo, accounting_client=acct_client, oauth_client=oauth_client, token_store=token_store,
    )
    connection = conn_repo.get_by_entity(entity_id)
    far_future = datetime.now(timezone.utc) + timedelta(seconds=REFERENCE_DATA_STALE_THRESHOLD_SECONDS + 1)
    assert is_reference_data_stale(connection, now=far_future)


# ---------------------------------------------------------------------
# Architect spec §18 — no Xero write/posting capability exists anywhere
# in this slice. This is the single most spec-critical compliance check
# in the whole delivery (PL review finding, added directly rather than
# left as prose in a report): a structural, introspection-based proof
# that survives refactors better than a fragile string/regex scan of
# `client.py`'s source. `XeroAccountingClient` is the ONLY class in this
# codebase that ever calls Xero's own Accounting API — if it never
# exposes any callable beyond `list_accounts` (a GET), there is no code
# path anywhere that could construct/PUT/POST an Accounts-write, an
# invoice, a payment, or any other transactional write to Xero.
# ---------------------------------------------------------------------


def test_xero_accounting_client_exposes_no_write_capability_at_all():
    """`XeroAccountingClient` (the one class that ever calls Xero's
    Accounting API) has exactly ONE public, non-dunder callable —
    `list_accounts`, a `GET`. No `create_*`/`update_*`/`post_*`/`put_*`/
    `delete_*` method exists, so there is no code path in this slice
    that could ever write/post to Xero (architect spec §18: 'explicitly
    NO posting to Xero in this slice')."""
    from services.xero.client import XeroAccountingClient

    public_methods = {
        name
        for name in dir(XeroAccountingClient)
        if not name.startswith("_") and callable(getattr(XeroAccountingClient, name))
    }
    assert public_methods == {"list_accounts"}


def test_xero_accounting_client_never_issues_a_non_get_http_request(monkeypatch):
    """Belt-and-braces on the same guarantee, proven at the transport
    boundary: patch `urllib.request.Request` itself and assert every
    call `XeroAccountingClient.list_accounts` makes uses `method="GET"`
    — even if a future edit added a write-shaped method, THIS call
    (the only one this class makes today) can never silently become a
    write."""
    import urllib.error
    import urllib.request

    from services.xero.client import XeroAccountingClient

    seen_methods: list[str] = []
    real_request = urllib.request.Request

    def _spy_request(*args, **kwargs):
        seen_methods.append(kwargs.get("method", "GET"))
        raise urllib.error.URLError("no real network access in a unit test")

    monkeypatch.setattr(urllib.request, "Request", _spy_request)
    client = XeroAccountingClient()
    try:
        client.list_accounts(tenant_id="some-tenant", access_token="fake-token")
    except Exception:
        pass  # the spy deliberately breaks the call after recording it — the method used is what matters here
    finally:
        monkeypatch.setattr(urllib.request, "Request", real_request)

    assert seen_methods == ["GET"]
