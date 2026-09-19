"""Cross-provider equivalence tests for
``services.mailbox.review_resolution`` (CD-6 GUI-operations-foundation
follow-on WO — second mailbox provider delivery).

Proves the ONE shared resolver (:func:`resolve_domain_review`/
:func:`resolve_security_review`) produces IDENTICAL policy/Needs-You/
destination/audit/security-review semantics regardless of which
provider adapter it is invoked through — the Microsoft Graph mailbox
(``MicrosoftGraphMailboxAdapter`` backed by
``FakeMicrosoftGraphClient``) and the IMAP mailbox
(``ImapMailboxAdapter`` backed by ``FakeImapClient``). Only genuinely
provider-specific transport details (which fake client got called, the
shape of the provider message identity) are allowed to differ — every
provider-neutral field (rule policy, destination mode/entity, match
mode, audit-relevant resolution fields, ingestion outcome) must match.

Reuses the existing per-provider integration-test harnesses directly
(``test_mailbox_sweep.Harness`` for Microsoft,
``test_mailbox_imap_sweep.Harness`` for IMAP) rather than re-implementing
their fake-client plumbing. Imported by BARE module name (``import
test_mailbox_sweep``, never ``tests.integration.test_mailbox_sweep``) —
this package has no ``tests/__init__.py``, so pytest's own rootdir
insertion makes every file directly under ``tests/integration/``
importable by its bare name only; the dotted-package form is a
CONFIRMED BROKEN pattern already present elsewhere in this repo (see
``tests/integration/test_intake_validation_pipeline.py``'s own
``from tests.integration.test_intake_scanner import ...`` — one of the
four pre-existing collection failures this WO's own verification
checklist explicitly excludes).

IMAP authentication caveat (unchanged from the prior WO)
------------------------------------------------------------------------
``services.mailbox.imap.authentication.assess_imap_authentication``
always returns ``UNKNOWN`` right now (a deliberate, correctly-
conservative fix — see that module's own docstring). The
domain-review-resolve equivalence scenario below therefore reuses
``tests.integration.test_mailbox_imap_sweep._force_auth_pass`` for the
IMAP side of its historical-reprocessing step ONLY, to exercise the
downstream MIME-fetch/evidence mechanics equivalently to Microsoft's
own genuinely-passing header fixture — never to weaken or bypass the
real selector itself. The security-review equivalence scenario below
needs no such forcing: ``process_security_reviewed_message_once``
deliberately never re-runs the authentication gate (see its own
docstring), and IMAP's message legitimately (for real, unforced
reasons) reaches ``SECURITY_REVIEW`` in the first place — which is
exactly the state both providers' own dedicated resolution workflow
exists to act on.
"""
from __future__ import annotations

from services.mailbox.imap.imap_client import ImapConnectResult, ImapFetchHeadersResult, ImapOutcomeStatus
from services.mailbox.microsoft.graph_client import GraphDeltaPageResult, GraphOutcomeStatus
from services.mailbox.review_resolution import resolve_domain_review, resolve_security_review
from services.needs_you.needs_you import ITEM_TYPE_MAILBOX_AUTHENTICATION_ESCALATION, ITEM_TYPE_MAILBOX_DOMAIN_REVIEW

# Bare module-name imports — see module docstring for why the dotted
# `tests.integration.X` form is NOT used here.
from test_mailbox_imap_sweep import Harness as ImapHarness
from test_mailbox_imap_sweep import _force_auth_pass, _headers as _imap_headers
from test_mailbox_sweep import Harness as MicrosoftHarness
from test_mailbox_sweep import _content as _ms_content
from test_mailbox_sweep import _headers_ok as _ms_headers_ok
from test_mailbox_sweep import _msg as _ms_msg

ACTOR_TYPE = "SYSTEM"
ACTOR_ID = "cross-provider-equivalence-test"

#: The SAME sender domain string used on both providers' own scenario
#: setup below — a genuine provider-neutral equality check, never two
#: different domains that merely "look similar".
_CROSS_PROVIDER_DOMAIN = "cross-provider.example"
_CROSS_PROVIDER_SENDER = f"billing@{_CROSS_PROVIDER_DOMAIN}"


def _open_item(needs_you_repo, *, item_type: str, mailbox_id: str):
    items = [
        i
        for i in needs_you_repo.list_needs_you_items(item_type=item_type, status="OPEN")
        if i.metadata.get("mailbox_id") == mailbox_id
    ]
    assert len(items) == 1, f"expected exactly one OPEN {item_type} item for mailbox {mailbox_id!r}, got {len(items)}"
    return items[0]


# ---------------------------------------------------------------------
# ALLOW + FIXED domain-review resolution
# ---------------------------------------------------------------------


def _microsoft_allow_fixed_scenario():
    h = MicrosoftHarness(allow_default_domain=False)
    msg = _ms_msg("m1", subject="Invoice attached", sender_address=_CROSS_PROVIDER_SENDER)
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(msg,), delta_link="d1"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    h.sweep()

    item = _open_item(h.needs_you_repo, item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW, mailbox_id=h.mailbox.mailbox_id)
    assert item.metadata["sender_domain"] == _CROSS_PROVIDER_DOMAIN

    h.graph_client.queue_headers_result(_ms_headers_ok())
    h.graph_client.queue_content_result(
        _ms_content(body=f"From: {_CROSS_PROVIDER_SENDER}\r\nSubject: Invoice\r\n\r\nBody".encode())
    )
    result = resolve_domain_review(
        needs_you_repository=h.needs_you_repo,
        mailbox_message_repository=h.message_repo,
        mailbox_domain_rule_repository=h.domain_rule_repo,
        entity_repository=h.api.entity_repository,
        api=h.api,
        object_store=h.object_store,
        scanner=h.scanner,
        adapter=h.adapter,
        mailbox=h.mailbox,
        mailbox_id=h.mailbox.mailbox_id,
        mailbox_source_id=h.source_id,
        item_id=item.item_id,
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
        decision="ALLOW",
        destination_entity_id=h.entity.entity_id,
        destination_mode="FIXED",
        match_mode="EXACT",
        processor_hint=None,
        sender_address=None,
    )
    return h, result


def _imap_allow_fixed_scenario(monkeypatch):
    h = ImapHarness(allow_default_domain=False)
    h.queue_discovery()
    h.queue_folder_round(
        uids=(1,), headers_by_uid={1: _imap_headers(1, subject="Invoice attached", sender=_CROSS_PROVIDER_SENDER)}
    )
    h.sweep()

    item = _open_item(h.needs_you_repo, item_type=ITEM_TYPE_MAILBOX_DOMAIN_REVIEW, mailbox_id=h.mailbox.mailbox_id)
    assert item.metadata["sender_domain"] == _CROSS_PROVIDER_DOMAIN

    # See module docstring — proves downstream reprocessing mechanics
    # equivalently to Microsoft's own genuinely-passing fixture; never
    # weakens the real (correctly conservative) selector itself.
    _force_auth_pass(monkeypatch)
    h.client.queue_connect_result(ImapConnectResult(status=ImapOutcomeStatus.OK))
    h.client.queue_fetch_headers_result(
        ImapFetchHeadersResult(status=ImapOutcomeStatus.OK, messages=(_imap_headers(1, sender=_CROSS_PROVIDER_SENDER),))
    )
    h.queue_content(body=f"From: {_CROSS_PROVIDER_SENDER}\r\nSubject: Invoice\r\n\r\nBody".encode())

    result = resolve_domain_review(
        needs_you_repository=h.needs_you_repo,
        mailbox_message_repository=h.message_repo,
        mailbox_domain_rule_repository=h.domain_rule_repo,
        entity_repository=h.api.entity_repository,
        api=h.api,
        object_store=h.object_store,
        scanner=h.scanner,
        adapter=h.adapter,
        mailbox=h.mailbox,
        mailbox_id=h.mailbox.mailbox_id,
        mailbox_source_id=h.source_id,
        item_id=item.item_id,
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
        decision="ALLOW",
        destination_entity_id=h.entity.entity_id,
        destination_mode="FIXED",
        match_mode="EXACT",
        processor_hint=None,
        sender_address=None,
    )
    return h, result


def test_allow_fixed_domain_review_resolution_is_equivalent_across_providers(monkeypatch):
    ms_h, ms_result = _microsoft_allow_fixed_scenario()
    imap_h, imap_result = _imap_allow_fixed_scenario(monkeypatch)

    # Needs You item — provider-neutral fields equivalent.
    for result in (ms_result, imap_result):
        assert result["needs_you_item"]["status"] == "RESOLVED"
        assert result["needs_you_item"]["resolution"]["decision"] == "ALLOW"
        assert result["needs_you_item"]["resolution"]["destination_mode"] == "FIXED"
        assert result["needs_you_item"]["resolution"]["match_mode"] == "EXACT"

    # MailboxDomainRule — provider-neutral fields equivalent.
    for h, result in ((ms_h, ms_result), (imap_h, imap_result)):
        rule = result["mailbox_domain_rule"]
        assert rule["policy"] == "MUST_READ"
        assert rule["destination_mode"] == "FIXED"
        assert rule["match_mode"] == "EXACT"
        assert rule["sender_domain"] == _CROSS_PROVIDER_DOMAIN
        assert rule["source"] == "OPERATOR"
        assert rule["destination_entity_id"] == h.entity.entity_id

    # Historical reprocessing — provider-neutral outcome equivalent.
    for result in (ms_result, imap_result):
        assert len(result["reprocessed_messages"]) == 1
        assert result["reprocessed_messages"][0]["ingestion_status"] == "INGESTED"
        assert result["reprocessed_messages"][0]["evidence_id"] is not None

    # Real evidence exists under the SAME destination entity on both
    # providers (FIXED destination threaded through identically).
    ms_evidence_id = ms_result["reprocessed_messages"][0]["evidence_id"]
    imap_evidence_id = imap_result["reprocessed_messages"][0]["evidence_id"]
    assert ms_h.api.get_evidence(ms_evidence_id).entity_id == ms_h.entity.entity_id
    assert imap_h.api.get_evidence(imap_evidence_id).entity_id == imap_h.entity.entity_id

    # Provider-specific TRANSPORT details legitimately differ — each
    # provider's own fake client was used, never the other's, and each
    # provider's own message-identity shape is its own (Graph immutable
    # id vs IMAP uidvalidity/uid composite). Never asserted equal.
    assert ms_h.graph_client.content_calls == ["m1"]
    assert len(imap_h.client.fetch_content_calls) == 1
    assert ms_result["reprocessed_messages"][0]["mailbox_message_id"] != imap_result["reprocessed_messages"][0]["mailbox_message_id"]


# ---------------------------------------------------------------------
# Security-review PROCESS_THIS_MESSAGE_ONCE resolution
# ---------------------------------------------------------------------


def _microsoft_security_review_scenario():
    """A genuine, TRUSTED authentication FAIL on a MUST_READ message —
    mirrors
    ``tests.integration.test_mailbox_sweep.test_must_read_message_with_hard_auth_failure_escalates_never_ingests``'s
    own real (non-monkeypatched) setup exactly, landing the message in
    real ``SECURITY_REVIEW`` for a real reason."""
    h = MicrosoftHarness()  # allow_default_domain=True — MUST_READ rule on "vendor.com"
    failing_msg = _ms_msg(
        "sec-1",
        raw_headers=(
            {
                "name": "Authentication-Results",
                "value": "spf=fail smtp.mailfrom=vendor.com; dkim=fail header.d=vendor.com; "
                "dmarc=fail action=quarantine header.from=vendor.com; compauth=fail reason=001",
            },
        ),
    )
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(failing_msg,), delta_link="d1"))
    h.graph_client.queue_delta_result(GraphDeltaPageResult(status=GraphOutcomeStatus.OK, messages=(), delta_link="d-junk"))
    h.sweep()

    item = _open_item(h.needs_you_repo, item_type=ITEM_TYPE_MAILBOX_AUTHENTICATION_ESCALATION, mailbox_id=h.mailbox.mailbox_id)

    h.graph_client.queue_content_result(_ms_content())
    result = resolve_security_review(
        needs_you_repository=h.needs_you_repo,
        mailbox_message_repository=h.message_repo,
        mailbox_domain_rule_repository=h.domain_rule_repo,
        api=h.api,
        object_store=h.object_store,
        scanner=h.scanner,
        adapter=h.adapter,
        mailbox=h.mailbox,
        mailbox_id=h.mailbox.mailbox_id,
        mailbox_source_id=h.source_id,
        item_id=item.item_id,
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
        decision="PROCESS_THIS_MESSAGE_ONCE",
    )
    return h, result


def _imap_security_review_scenario():
    """The REAL, unforced IMAP selector always returns UNKNOWN right now
    (see module docstring) — a MUST_READ message therefore lands
    ``SECURITY_REVIEW`` for real, genuine reasons with zero
    monkeypatching, mirroring
    ``tests.integration.test_mailbox_imap_sweep.test_must_read_message_with_the_real_unforced_selector_goes_to_security_review_never_evidence``'s
    own setup exactly."""
    h = ImapHarness()  # allow_default_domain=True — MUST_READ rule on "vendor.com"
    h.queue_discovery()
    h.queue_folder_round(uids=(1,))
    h.sweep()

    item = _open_item(h.needs_you_repo, item_type=ITEM_TYPE_MAILBOX_AUTHENTICATION_ESCALATION, mailbox_id=h.mailbox.mailbox_id)

    h.queue_content()
    result = resolve_security_review(
        needs_you_repository=h.needs_you_repo,
        mailbox_message_repository=h.message_repo,
        mailbox_domain_rule_repository=h.domain_rule_repo,
        api=h.api,
        object_store=h.object_store,
        scanner=h.scanner,
        adapter=h.adapter,
        mailbox=h.mailbox,
        mailbox_id=h.mailbox.mailbox_id,
        mailbox_source_id=h.source_id,
        item_id=item.item_id,
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
        decision="PROCESS_THIS_MESSAGE_ONCE",
    )
    return h, result


def test_security_review_process_once_resolution_is_equivalent_across_providers():
    ms_h, ms_result = _microsoft_security_review_scenario()
    imap_h, imap_result = _imap_security_review_scenario()

    for result in (ms_result, imap_result):
        assert result["needs_you_item"]["status"] == "RESOLVED"
        assert result["needs_you_item"]["resolution"] == {"decision": "PROCESS_THIS_MESSAGE_ONCE"}
        assert result["mailbox_message"]["ingestion_status"] == "INGESTED"
        assert result["mailbox_message"]["evidence_id"] is not None

    # The governing rule is NEVER modified by a one-message override —
    # equivalent on both providers.
    for h in (ms_h, imap_h):
        rule = h.domain_rule_repo.find_for_sender(mailbox_id=h.mailbox.mailbox_id, sender_domain="vendor.com")
        assert rule.policy == "MUST_READ"
        assert rule.destination_mode == "REVIEW_REQUIRED"

    # Provider-neutral: REVIEW_REQUIRED never assigns a default entity.
    ms_evidence = ms_h.api.get_evidence(ms_result["mailbox_message"]["evidence_id"])
    imap_evidence = imap_h.api.get_evidence(imap_result["mailbox_message"]["evidence_id"])
    assert ms_evidence.entity_id is None
    assert imap_evidence.entity_id is None

    # Provider-specific transport details legitimately differ.
    assert len(ms_h.graph_client.content_calls) == 1
    assert len(imap_h.client.fetch_content_calls) == 1
