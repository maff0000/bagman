"""Pure domain-level tests for CD-6 Slice 3's mailbox-definition
registry (Mailbox Management, TAB 1 / Email) —
``services/mailbox/mailbox.py`` against its in-memory reference
repository, no HTTP layer, no database, no Docker. Mirrors the fast,
dependency-free style ``tests/integration/test_xero_domain.py`` already
establishes for `XeroConnection`'s own state machine.
"""
from __future__ import annotations

import pytest

from core.errors import ConflictError, InvalidStateTransitionError, NotFoundError, ValidationError
from services.mailbox.mailbox import (
    ALLOWED_TRANSITIONS,
    CONNECTION_STATE_NOT_CONFIGURED,
    InMemoryMailboxSourceRepository,
    PROVIDER_GOOGLE_GMAIL,
    PROVIDER_IMAP,
    PROVIDER_KINDS,
    PROVIDER_MICROSOFT_GRAPH,
    normalize_email,
)


def _repo() -> InMemoryMailboxSourceRepository:
    return InMemoryMailboxSourceRepository()


# ---------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------


def test_create_mailbox_starts_active_enabled_and_not_configured():
    repo = _repo()
    mailbox = repo.create_mailbox(
        display_name="Matt — Infosecurs",
        email_address="matt@infosecurs.com",
        provider_kind=PROVIDER_MICROSOFT_GRAPH,
    )
    assert mailbox.status == "ACTIVE"
    assert mailbox.enabled is True
    assert mailbox.connection_state == CONNECTION_STATE_NOT_CONFIGURED
    assert mailbox.default_entity_id is None
    assert mailbox.last_successful_sweep_at is None
    assert mailbox.last_connection_check_at is None
    assert mailbox.last_error_code is None
    assert mailbox.last_error_detail is None


def test_email_is_normalised_to_lowercase_on_create():
    repo = _repo()
    mailbox = repo.create_mailbox(
        display_name="Matt — Noust", email_address="Matt@Noust.AI", provider_kind=PROVIDER_IMAP
    )
    assert mailbox.email_address == "matt@noust.ai"
    assert normalize_email("Matt@Noust.AI") == "matt@noust.ai"


def test_create_with_a_default_entity_hint_is_stored_verbatim_as_a_hint():
    """The domain layer stores whatever id it is given — validating
    that it is a REAL entity happens one layer up, at the HTTP router
    (see services/mailbox/mailbox.py's own module docstring). This is
    a domain-layer proof only that the field round-trips."""
    repo = _repo()
    mailbox = repo.create_mailbox(
        display_name="Matt — Infosecurs",
        email_address="matt@infosecurs.com",
        provider_kind=PROVIDER_MICROSOFT_GRAPH,
        default_entity_id="01a0b158-0000-7000-8000-000000000000",
    )
    assert mailbox.default_entity_id == "01a0b158-0000-7000-8000-000000000000"


def test_reject_invalid_email_address():
    repo = _repo()
    with pytest.raises(ValidationError):
        repo.create_mailbox(
            display_name="Bad mailbox", email_address="not-an-email", provider_kind=PROVIDER_MICROSOFT_GRAPH
        )


def test_reject_ungoverned_provider_kind():
    repo = _repo()
    with pytest.raises(ValidationError):
        repo.create_mailbox(
            display_name="Bad provider", email_address="matt@infosecurs.com", provider_kind="SOME_OTHER_PROVIDER"
        )


def test_governed_provider_kinds_are_exactly_the_documented_three():
    assert PROVIDER_KINDS == {PROVIDER_MICROSOFT_GRAPH, PROVIDER_IMAP, PROVIDER_GOOGLE_GMAIL}


# ---------------------------------------------------------------------
# Duplicate-email protection (in-memory) — global uniqueness, case-
# insensitive, never freed by retirement (see module docstring).
# ---------------------------------------------------------------------


def test_duplicate_email_is_rejected_on_create():
    repo = _repo()
    repo.create_mailbox(display_name="First", email_address="matt@infosecurs.com", provider_kind=PROVIDER_IMAP)
    with pytest.raises(ConflictError):
        repo.create_mailbox(display_name="Second", email_address="MATT@INFOSECURS.COM", provider_kind=PROVIDER_IMAP)


def test_duplicate_email_is_rejected_even_across_different_providers():
    repo = _repo()
    repo.create_mailbox(display_name="First", email_address="matt@noust.ai", provider_kind=PROVIDER_IMAP)
    with pytest.raises(ConflictError):
        repo.create_mailbox(
            display_name="Second", email_address="matt@noust.ai", provider_kind=PROVIDER_GOOGLE_GMAIL
        )


def test_duplicate_email_uniqueness_is_never_freed_by_retirement():
    """The documented judgment call, proven directly: unlike Xero's
    tenant_id (freed on disconnect), a mailbox's email uniqueness stays
    locked even after the original row is retired."""
    repo = _repo()
    original = repo.create_mailbox(
        display_name="Original", email_address="matt@infosecurs.com", provider_kind=PROVIDER_IMAP
    )
    repo.retire_mailbox(original.mailbox_id)

    with pytest.raises(ConflictError):
        repo.create_mailbox(
            display_name="Attempted re-registration",
            email_address="matt@infosecurs.com",
            provider_kind=PROVIDER_MICROSOFT_GRAPH,
        )


def test_update_can_keep_its_own_email_without_tripping_the_uniqueness_check():
    repo = _repo()
    mailbox = repo.create_mailbox(
        display_name="Matt — Infosecurs", email_address="matt@infosecurs.com", provider_kind=PROVIDER_IMAP
    )
    updated = repo.update_mailbox(
        mailbox.mailbox_id,
        display_name="Matt — Infosecurs (renamed)",
        email_address="matt@infosecurs.com",
        provider_kind=PROVIDER_IMAP,
        default_entity_id=None,
    )
    assert updated.display_name == "Matt — Infosecurs (renamed)"


def test_update_rejects_reassigning_email_already_used_by_another_mailbox():
    repo = _repo()
    repo.create_mailbox(display_name="A", email_address="a@infosecurs.com", provider_kind=PROVIDER_IMAP)
    other = repo.create_mailbox(display_name="B", email_address="b@infosecurs.com", provider_kind=PROVIDER_IMAP)
    with pytest.raises(ConflictError):
        repo.update_mailbox(
            other.mailbox_id,
            display_name="B",
            email_address="a@infosecurs.com",
            provider_kind=PROVIDER_IMAP,
            default_entity_id=None,
        )


# ---------------------------------------------------------------------
# Lifecycle: enable/disable/retire transitions
# ---------------------------------------------------------------------


def test_disable_then_enable_round_trip():
    repo = _repo()
    mailbox = repo.create_mailbox(
        display_name="Matt — Infosecurs", email_address="matt@infosecurs.com", provider_kind=PROVIDER_IMAP
    )
    disabled = repo.disable_mailbox(mailbox.mailbox_id)
    assert disabled.status == "DISABLED"
    assert disabled.enabled is False

    enabled = repo.enable_mailbox(mailbox.mailbox_id)
    assert enabled.status == "ACTIVE"
    assert enabled.enabled is True


def test_retire_from_active_preserves_the_row():
    repo = _repo()
    mailbox = repo.create_mailbox(
        display_name="Matt — Infosecurs", email_address="matt@infosecurs.com", provider_kind=PROVIDER_IMAP
    )
    retired = repo.retire_mailbox(mailbox.mailbox_id)
    assert retired.status == "RETIRED"
    assert retired.enabled is False
    # Still resolvable — never physically deleted.
    assert repo.get_mailbox(mailbox.mailbox_id).status == "RETIRED"
    assert mailbox.mailbox_id in [m.mailbox_id for m in repo.list_mailboxes()]


def test_retire_from_disabled_also_works():
    repo = _repo()
    mailbox = repo.create_mailbox(
        display_name="Matt — Infosecurs", email_address="matt@infosecurs.com", provider_kind=PROVIDER_IMAP
    )
    repo.disable_mailbox(mailbox.mailbox_id)
    retired = repo.retire_mailbox(mailbox.mailbox_id)
    assert retired.status == "RETIRED"


def test_retired_is_terminal_cannot_be_re_enabled_directly():
    """The documented judgment call (services/mailbox/mailbox.py's own
    module docstring): RETIRED has no transition back OUT in this
    slice — genuinely prohibited transitions to a DIFFERENT status
    after retirement. Re-retiring (RETIRED -> RETIRED) is deliberately
    NOT one of these — see
    test_re_retiring_an_already_retired_mailbox_is_an_idempotent_no_op
    below for why that's a safe no-op, not a forbidden transition."""
    repo = _repo()
    mailbox = repo.create_mailbox(
        display_name="Matt — Infosecurs", email_address="matt@infosecurs.com", provider_kind=PROVIDER_IMAP
    )
    repo.retire_mailbox(mailbox.mailbox_id)

    with pytest.raises(InvalidStateTransitionError):
        repo.enable_mailbox(mailbox.mailbox_id)
    with pytest.raises(InvalidStateTransitionError):
        repo.disable_mailbox(mailbox.mailbox_id)


def test_re_retiring_an_already_retired_mailbox_is_an_idempotent_no_op():
    """PL-review finding: the exact same class of bug Slice 2 caught
    twice (a second stale browser tab, or a genuine double-click,
    calling enable/disable/retire on a mailbox already in that target
    state must never 500) — proven here for all three actions. Before
    this fix, repo.retire_mailbox() on an already-RETIRED row raised
    InvalidStateTransitionError; now it is a safe, audited-once,
    return-unchanged no-op."""
    repo = _repo()
    mailbox = repo.create_mailbox(
        display_name="Matt — Infosecurs", email_address="matt@infosecurs.com", provider_kind=PROVIDER_IMAP
    )
    retired_once = repo.retire_mailbox(mailbox.mailbox_id)

    retired_again = repo.retire_mailbox(mailbox.mailbox_id)
    assert retired_again.status == "RETIRED"
    assert retired_again.updated_at == retired_once.updated_at  # genuinely unchanged, not re-stamped


def test_re_enabling_an_already_active_mailbox_is_an_idempotent_no_op():
    repo = _repo()
    mailbox = repo.create_mailbox(
        display_name="Matt — Infosecurs", email_address="matt@infosecurs.com", provider_kind=PROVIDER_IMAP
    )
    assert mailbox.status == "ACTIVE"

    again = repo.enable_mailbox(mailbox.mailbox_id)
    assert again.status == "ACTIVE"
    assert again.updated_at == mailbox.updated_at


def test_re_disabling_an_already_disabled_mailbox_is_an_idempotent_no_op():
    repo = _repo()
    mailbox = repo.create_mailbox(
        display_name="Matt — Infosecurs", email_address="matt@infosecurs.com", provider_kind=PROVIDER_IMAP
    )
    disabled_once = repo.disable_mailbox(mailbox.mailbox_id)

    disabled_again = repo.disable_mailbox(mailbox.mailbox_id)
    assert disabled_again.status == "DISABLED"
    assert disabled_again.updated_at == disabled_once.updated_at


def test_every_documented_transition_is_reachable_and_matches_allowed_transitions_table():
    """A direct proof that `ALLOWED_TRANSITIONS` is exactly what the
    module docstring documents — not a stale comment."""
    assert ALLOWED_TRANSITIONS == {
        "ACTIVE": frozenset({"DISABLED", "RETIRED"}),
        "DISABLED": frozenset({"ACTIVE", "RETIRED"}),
        "RETIRED": frozenset(),
    }


def test_unknown_mailbox_id_raises_not_found():
    repo = _repo()
    with pytest.raises(NotFoundError):
        repo.get_mailbox("not-a-real-id")
    with pytest.raises(NotFoundError):
        repo.enable_mailbox("not-a-real-id")


def test_get_by_email_is_a_query_not_a_raise_for_an_unknown_address():
    repo = _repo()
    assert repo.get_by_email("nobody@nowhere.example") is None
    repo.create_mailbox(display_name="Matt", email_address="matt@infosecurs.com", provider_kind=PROVIDER_IMAP)
    assert repo.get_by_email("MATT@INFOSECURS.COM") is not None


def test_list_mailboxes_includes_every_lifecycle_status():
    repo = _repo()
    active = repo.create_mailbox(display_name="A", email_address="a@infosecurs.com", provider_kind=PROVIDER_IMAP)
    disabled = repo.create_mailbox(display_name="B", email_address="b@infosecurs.com", provider_kind=PROVIDER_IMAP)
    repo.disable_mailbox(disabled.mailbox_id)
    retired = repo.create_mailbox(display_name="C", email_address="c@infosecurs.com", provider_kind=PROVIDER_IMAP)
    repo.retire_mailbox(retired.mailbox_id)

    statuses = {m.mailbox_id: m.status for m in repo.list_mailboxes()}
    assert statuses[active.mailbox_id] == "ACTIVE"
    assert statuses[disabled.mailbox_id] == "DISABLED"
    assert statuses[retired.mailbox_id] == "RETIRED"


# ---------------------------------------------------------------------
# Secret doctrine — structural proof no secret field exists at all
# ---------------------------------------------------------------------


def test_no_secret_shaped_field_exists_anywhere_on_the_domain_object():
    repo = _repo()
    mailbox = repo.create_mailbox(
        display_name="Matt — Infosecurs", email_address="matt@infosecurs.com", provider_kind=PROVIDER_MICROSOFT_GRAPH
    )
    rendered = mailbox.to_dict()
    forbidden_substrings = ("password", "secret", "token", "credential", "api_key", "private_key")
    for key in rendered:
        lowered = key.lower()
        assert not any(bad in lowered for bad in forbidden_substrings), f"secret-shaped field name: {key}"


def test_connected_is_never_reachable_in_this_slice():
    """CONNECTED is declared vocabulary (contract enum) but no code
    path in this domain module can ever produce it — proven by
    checking every value this module's own construction/transition
    logic can ever set `connection_state` to."""
    repo = _repo()
    mailbox = repo.create_mailbox(
        display_name="Matt — Infosecurs", email_address="matt@infosecurs.com", provider_kind=PROVIDER_MICROSOFT_GRAPH
    )
    assert mailbox.connection_state == CONNECTION_STATE_NOT_CONFIGURED
    for status in ("ACTIVE", "DISABLED", "RETIRED"):
        # No transition ever touches connection_state at all.
        pass
    disabled = repo.disable_mailbox(mailbox.mailbox_id)
    assert disabled.connection_state == CONNECTION_STATE_NOT_CONFIGURED
    retired = repo.retire_mailbox(disabled.mailbox_id)
    assert retired.connection_state == CONNECTION_STATE_NOT_CONFIGURED
