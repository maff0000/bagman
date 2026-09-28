"""``XeroAccountSuggestion`` domain model/repository, and
:func:`produce_account_suggestion` — the governed Xero Account
Suggestion Producer orchestrator (BAGMAN accounting platform,
`xero/account-suggestion-producer` WO).

Given one piece of eligible evidence (already classified as an
invoice/receipt), produces an AI-generated suggestion of which Xero
chart-of-accounts entry it should be coded to, validates that
suggestion against the real governed eligible-account set for the
correct company/Xero connection, and persists it as a non-authoritative
proposal — routed through BAGMAN's existing Needs You queue for human
confirmation or correction. Never auto-posts, never mutates Xero, never
becomes authoritative without a human decision (see
``services.xero.account_assignment`` for the separate, write-once
authoritative record a human resolution creates).

Shape mirrors ``services.evidence.classification_orchestrator
.classify_evidence`` — see that module's own docstring for the
"deterministic-first/AI-fallback, idempotent, fingerprint-guarded,
no-phantom-state-on-failure" discipline this orchestrator follows
(minus the deterministic-first part: there is no deterministic
Xero-account classifier, every suggestion here is AI-produced — the
idempotency/failure-handling discipline is what is mirrored, not the
deterministic-first control flow).

Judgment call — why this orchestrator does NOT reproduce
``classify_evidence``'s fingerprint-scoped invocation search verbatim
------------------------------------------------------------------------
``DOCUMENT_TYPE_PROPOSAL`` v2 can search prior ``AIInvocation`` rows BY
FINGERPRINT because its own `input_schema` carries a
`classifier_fingerprint` field inside `input_references` (see
`ai/tasks.py`'s own `_DOCUMENT_TYPE_PROPOSAL_V2_INPUT_SCHEMA`).
`XERO_ACCOUNT_SUGGESTION`'s `input_schema` was deliberately built to
mirror `ai/tasks.py::_DOCUMENT_INPUT_SCHEMA` instead (per this WO's own
spec: a closed schema carrying only `evidence_id`) — so there is no
field to persist a fingerprint into for a later search to filter on.

Given that constraint, this orchestrator's idempotency story is built
from TWO real, available mechanisms instead of one fingerprint-scoped
search:

1. **Business-level idempotency, checked FIRST, before any AI
   involvement at all** — :meth:`XeroAccountSuggestionRepository
   .get_by_evidence`: once a suggestion exists for an `evidence_id`, no
   second one is ever produced (outcome
   :data:`OUTCOME_SUGGESTION_ALREADY_EXISTS`) and no AI call is ever
   attempted. This is the guard that actually matters for "duplicate
   producer invocation is idempotent" (see this WO's own test 9) — it
   fires before invocation-level concerns are even reached.
2. **Subject-scoped `AIInvocation` concurrency/crash-recovery**, via
   `ai.invocation.AIInvocationRepository.find_active_invocation`/
   `list_invocations` keyed on `(task_id, task_version,
   primary_input_reference=evidence_id)` — exactly the same guard
   `ai/gateway/background.py`'s own PID §73 concurrency mechanism
   already provides for every task. A non-terminal prior invocation for
   this evidence_id -> :data:`OUTCOME_AI_IN_PROGRESS` (no second call).
   A prior SUCCEEDED invocation with no suggestion yet -> reused
   directly (the crash-recovery case: the model call itself completed
   but the process died before the suggestion row was persisted).

Deliberately NOT implemented: a hard "refuse to retry a FAILED
invocation" guard. `classify_evidence`'s own fingerprint-scoped guard
exists specifically to stop re-billing an IDENTICAL model call — but
this task has no content-hash dimension that would ever legitimately
change for a fixed `evidence_id` (unlike a reprocessing scenario), and
`ai/gateway/background.py`'s own module docstring states this doctrine
directly: *"No retry loop lives inside this function (PID §74): a
'retry' is simply calling `run_background_task` again once the prior
attempt has reached a terminal state — WI-1's own concurrency guard
already permits that cleanly and creates a genuinely new, distinct
`AIInvocation` row."* A permanent block against ever retrying after one
transient failure (a network blip, a momentary provider outage) would
make an ordinary infrastructure hiccup permanently unrecoverable for a
given evidence item, with no way to express "the underlying problem is
now fixed, try again" short of fabricating a fake schema field purely
to defeat the guard. This orchestrator therefore allows a fresh call
after a terminal `FAILED` invocation to attempt a genuinely new
`AIInvocation`, exactly as the gateway's own documented contract
already intends.
"""
from __future__ import annotations

import abc
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

from ai.gateway.background import run_background_task
from ai.prompts.loader import resolve_prompt_contract_version
from ai.tasks import get_task_contract
from core import identity
from core.contract_validation import validate_against_contract
from core.errors import NotFoundError, ValidationError
from core.timestamps import to_contract_string, utc_now
from services.evidence.classification import (
    CLASSIFICATION_TYPE_DOCUMENT_TYPE,
    DOCUMENT_TYPE_RECEIPT,
    DOCUMENT_TYPE_SUPPLIER_INVOICE,
    STATUS_CLASSIFIED,
)
from services.needs_you.needs_you import (
    ALLOWED_ACTION_XERO_ACCOUNT_REQUIRED,
    ITEM_TYPE_XERO_ACCOUNT_REQUIRED,
)
from services.xero.account_assignment import XeroAccountAssignmentRepository
from services.xero.account_suggestion_context import (
    OUTCOME_BUILT as _CONTEXT_OUTCOME_BUILT,
    build_account_suggestion_context,
)
from services.xero.ai_suggestion import resolve_ai_suggested_account
from services.xero.eligibility import list_eligible_accounts
from services.xero.sync import is_reference_data_stale

_SCHEMA = "xero/bagman.xero_account_suggestion.v1.schema.json"
SCHEMA_VERSION = "bagman.xero_account_suggestion.v1"

STATUS_REVIEW_REQUIRED = "REVIEW_REQUIRED"

#: The exact `(task_id, task_version)` this orchestrator governs —
#: never a caller-supplied parameter (mirrors
#: `classification_orchestrator.AI_TASK_ID`/`AI_TASK_VERSION`'s own
#: role). Also registered in `app/api/routers/ai.py`'s own
#: `_GENERIC_PATH_FORBIDDEN_TASKS`.
AI_TASK_ID = "XERO_ACCOUNT_SUGGESTION"
AI_TASK_VERSION = 1

#: The two closed `EvidenceClassification.document_type` values this WO
#: judges invoice/receipt/expense-shaped and therefore eligible for a
#: Xero-account coding suggestion (documented judgment call, mirroring
#: `services.xero.eligibility`'s own "document your own judgment calls
#: clearly" house style). The other six `DOCUMENT_TYPES` values
#: (ORDER_CONFIRMATION, REFUND_CONFIRMATION, BROKER_STATEMENT,
#: BROKER_ACTIVITY_NOTICE, NON_ACCOUNTING_DOCUMENT, UNKNOWN) are
#: deliberately excluded: an order confirmation/refund/broker document
#: is not itself an expense-coding decision the way an invoice/receipt
#: is, and NON_ACCOUNTING_DOCUMENT/UNKNOWN should never enter an
#: accounting-coding workflow at all.
ELIGIBLE_DOCUMENT_TYPES = frozenset({DOCUMENT_TYPE_SUPPLIER_INVOICE, DOCUMENT_TYPE_RECEIPT})

#: The closed set of outcomes :func:`produce_account_suggestion` may
#: return.
OUTCOME_ENTITY_NOT_RESOLVED = "ENTITY_NOT_RESOLVED"
OUTCOME_NOT_ELIGIBLE_FOR_ACCOUNT_SUGGESTION = "NOT_ELIGIBLE_FOR_ACCOUNT_SUGGESTION"
OUTCOME_XERO_NOT_CONNECTED = "XERO_NOT_CONNECTED"
OUTCOME_REFERENCE_DATA_STALE = "REFERENCE_DATA_STALE"
OUTCOME_ALREADY_ASSIGNED = "ALREADY_ASSIGNED"
OUTCOME_SUGGESTION_ALREADY_EXISTS = "SUGGESTION_ALREADY_EXISTS"
OUTCOME_NO_ELIGIBLE_ACCOUNTS = "NO_ELIGIBLE_ACCOUNTS"
OUTCOME_CONTEXT_UNSUPPORTED = "CONTEXT_UNSUPPORTED"
OUTCOME_AI_IN_PROGRESS = "AI_IN_PROGRESS"
OUTCOME_AI_INVOCATION_FAILED = "AI_INVOCATION_FAILED"
OUTCOME_SUGGESTED_ACCOUNT_REJECTED = "SUGGESTED_ACCOUNT_REJECTED"
OUTCOME_SUGGESTION_PRODUCED = "SUGGESTION_PRODUCED"

PRODUCE_ACCOUNT_SUGGESTION_OUTCOMES = frozenset(
    {
        OUTCOME_ENTITY_NOT_RESOLVED,
        OUTCOME_NOT_ELIGIBLE_FOR_ACCOUNT_SUGGESTION,
        OUTCOME_XERO_NOT_CONNECTED,
        OUTCOME_REFERENCE_DATA_STALE,
        OUTCOME_ALREADY_ASSIGNED,
        OUTCOME_SUGGESTION_ALREADY_EXISTS,
        OUTCOME_NO_ELIGIBLE_ACCOUNTS,
        OUTCOME_CONTEXT_UNSUPPORTED,
        OUTCOME_AI_IN_PROGRESS,
        OUTCOME_AI_INVOCATION_FAILED,
        OUTCOME_SUGGESTED_ACCOUNT_REJECTED,
        OUTCOME_SUGGESTION_PRODUCED,
    }
)

#: Bounded, system-owned audit event types this orchestrator emits —
#: never arbitrary model prose (mirrors
#: `classification_orchestrator`'s own reason-code discipline).
EVENT_SUGGESTION_PRODUCED = "XERO_ACCOUNT_SUGGESTION_PRODUCED"
EVENT_SUGGESTION_REJECTED = "XERO_ACCOUNT_SUGGESTION_REJECTED"


# ---------------------------------------------------------------------
# XeroAccountSuggestion domain model + repository
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class XeroAccountSuggestion:
    """One immutable, append-only AI proposal row. See module docstring
    — this row is NEVER itself authoritative; see
    ``services.xero.account_assignment.XeroAccountAssignment`` for the
    separate, write-once human decision record."""

    suggestion_id: str
    evidence_id: str
    entity_id: str
    tenant_id: str
    suggested_account_id: str
    confidence: Optional[float]
    signals: Sequence[str]
    ai_invocation_id: str
    created_at: datetime
    status: str = STATUS_REVIEW_REQUIRED
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        """Render exactly the shape required by
        ``contracts/xero/bagman.xero_account_suggestion.v1.schema.json``."""
        return {
            "suggestion_id": self.suggestion_id,
            "evidence_id": self.evidence_id,
            "entity_id": self.entity_id,
            "tenant_id": self.tenant_id,
            "suggested_account_id": self.suggested_account_id,
            "confidence": self.confidence,
            "signals": list(self.signals),
            "ai_invocation_id": self.ai_invocation_id,
            "status": self.status,
            "created_at": to_contract_string(self.created_at),
            "schema_version": self.schema_version,
        }


class XeroAccountSuggestionRepository(abc.ABC):
    """Repository abstraction for XeroAccountSuggestion. No supersession
    chain (unlike `EvidenceClassificationRepository`) — see module
    docstring's "one non-superseded suggestion attempt per fingerprint,
    enforced via the idempotency check in the orchestrator, not a DB
    constraint beyond a real PK"."""

    @abc.abstractmethod
    def create_suggestion(
        self,
        *,
        evidence_id: str,
        entity_id: str,
        tenant_id: str,
        suggested_account_id: str,
        confidence: Optional[float],
        signals: Sequence[str],
        ai_invocation_id: str,
    ) -> XeroAccountSuggestion:
        raise NotImplementedError

    @abc.abstractmethod
    def get_suggestion(self, suggestion_id: str) -> XeroAccountSuggestion:
        raise NotImplementedError

    @abc.abstractmethod
    def get_by_evidence(self, evidence_id: str) -> Optional[XeroAccountSuggestion]:
        """Read-only lookup, never `NotFoundError` — an evidence item
        with no suggestion yet is an ordinary, expected state. When more
        than one row exists for `evidence_id` (not expected in ordinary
        operation — see class docstring), the most recently created row
        wins."""
        raise NotImplementedError


class InMemoryXeroAccountSuggestionRepository(XeroAccountSuggestionRepository):
    """Narrow in-memory reference implementation (PID §21 Option A)."""

    def __init__(self) -> None:
        self._by_id: dict[str, XeroAccountSuggestion] = {}
        #: evidence_id -> ordered list of suggestion_ids, oldest first.
        self._by_evidence: dict[str, list[str]] = {}

    def create_suggestion(
        self,
        *,
        evidence_id: str,
        entity_id: str,
        tenant_id: str,
        suggested_account_id: str,
        confidence: Optional[float],
        signals: Sequence[str],
        ai_invocation_id: str,
    ) -> XeroAccountSuggestion:
        try:
            candidate = XeroAccountSuggestion(
                suggestion_id=identity.generate_id(),
                evidence_id=evidence_id,
                entity_id=entity_id,
                tenant_id=tenant_id,
                suggested_account_id=suggested_account_id,
                confidence=confidence,
                signals=tuple(signals),
                ai_invocation_id=ai_invocation_id,
                created_at=utc_now(),
            )
            validate_against_contract(candidate.to_dict(), _SCHEMA)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            raise ValidationError(f"could not create XeroAccountSuggestion: {exc}") from exc

        self._by_id[candidate.suggestion_id] = candidate
        self._by_evidence.setdefault(evidence_id, []).append(candidate.suggestion_id)
        return candidate

    def get_suggestion(self, suggestion_id: str) -> XeroAccountSuggestion:
        try:
            return self._by_id[suggestion_id]
        except KeyError:
            raise NotFoundError(f"no XeroAccountSuggestion with suggestion_id '{suggestion_id}'") from None

    def get_by_evidence(self, evidence_id: str) -> Optional[XeroAccountSuggestion]:
        ids = self._by_evidence.get(evidence_id) or []
        return self._by_id[ids[-1]] if ids else None


# ---------------------------------------------------------------------
# Idempotency fingerprint (documentation/audit provenance only — see
# module docstring for why this is never embedded in `input_references`)
# ---------------------------------------------------------------------


def compute_account_suggestion_fingerprint(
    *,
    task_id: str,
    task_version: int,
    prompt_contract_version: str,
    evidence_id: str,
    evidence_content_hash: str,
    context_contract_version: str,
    context_sha256: str,
    eligible_account_ids: Sequence[str],
) -> str:
    """Deterministic SHA-256 fingerprint mirroring
    `services.evidence.classification_ai_fingerprint
    .compute_classifier_fingerprint`'s own shape, scoped to this task.
    Never embedded in `AIInvocation.input_references` (see module
    docstring) — used only as durable, human-readable provenance
    recorded on the `NeedsYouItem`/audit-event payload this orchestrator
    produces, so a reader can independently verify two suggestions were
    (or were not) produced from byte-identical inputs.
    """
    canonical_payload = {
        "task_id": task_id,
        "task_version": task_version,
        "prompt_contract_version": prompt_contract_version,
        "evidence_id": evidence_id,
        "evidence_content_hash": evidence_content_hash,
        "context_contract_version": context_contract_version,
        "context_sha256": context_sha256,
        "eligible_account_ids": sorted(eligible_account_ids),
    }
    canonical_json = json.dumps(canonical_payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _evidence_content_hash(evidence) -> str:
    content_hash = evidence.content_hash
    if isinstance(content_hash, Mapping):
        return str(content_hash.get("value"))
    return str(content_hash)


# ---------------------------------------------------------------------
# ProduceAccountSuggestionResult + orchestrator
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class ProduceAccountSuggestionResult:
    """Result of :func:`produce_account_suggestion` — one of
    :data:`PRODUCE_ACCOUNT_SUGGESTION_OUTCOMES`."""

    outcome: str
    suggestion: Optional[XeroAccountSuggestion] = None
    assignment: Optional[Any] = None  # services.xero.account_assignment.XeroAccountAssignment, when ALREADY_ASSIGNED
    needs_you_item_id: Optional[str] = None
    ai_invocation_id: Optional[str] = None
    unsupported_reason: Optional[str] = None
    rejection_reason: Optional[str] = None
    fingerprint: Optional[str] = None

    def __post_init__(self) -> None:
        if self.outcome not in PRODUCE_ACCOUNT_SUGGESTION_OUTCOMES:
            raise ValueError(f"'{self.outcome}' is not one of {sorted(PRODUCE_ACCOUNT_SUGGESTION_OUTCOMES)}")


def _ensure_needs_you_item(
    *, suggestion: XeroAccountSuggestion, needs_you_repository, correlation_id: Optional[str],
):
    """Idempotent via `NeedsYouRepository.create_needs_you_item`'s own
    `(item_type, source_object_reference)` dedupe — calling this twice
    for the same `suggestion.evidence_id` returns the SAME item, never a
    second row. This is the crash-recovery mechanism: a retry after
    "suggestion committed, process died before the review item was
    created" simply calls this again (mirrors
    `services.evidence.classification_review.ensure_classification_review_item`'s
    own role for `CLASSIFICATION_REVIEW`)."""
    return needs_you_repository.create_needs_you_item(
        item_type=ITEM_TYPE_XERO_ACCOUNT_REQUIRED,
        domain="XERO",
        question=(
            f"BAGMAN suggests coding this document to Xero account {suggestion.suggested_account_id}. "
            "Confirm or choose a different account."
        ),
        allowed_action_type=ALLOWED_ACTION_XERO_ACCOUNT_REQUIRED,
        correlation_id=correlation_id or suggestion.suggestion_id,
        source_object_reference=suggestion.evidence_id,
        metadata={
            "evidence_id": suggestion.evidence_id,
            "entity_id": suggestion.entity_id,
            "suggestion_id": suggestion.suggestion_id,
            "suggested_account_id": suggestion.suggested_account_id,
            "confidence": suggestion.confidence,
            "ai_invocation_id": suggestion.ai_invocation_id,
        },
    )


def produce_account_suggestion(
    *,
    evidence_id: str,
    entity_id: str,
    evidence_repository,
    classification_repository,
    entity_repository,
    xero_connection_repository,
    xero_account_repository,
    suggestion_repository: XeroAccountSuggestionRepository,
    assignment_repository: XeroAccountAssignmentRepository,
    ai_invocation_repository,
    litellm_client,
    object_store,
    audit_repository,
    record_audit_event,
    needs_you_repository,
    actor_type: str,
    actor_id: str,
    correlation_id: Optional[str] = None,
) -> ProduceAccountSuggestionResult:
    """Run the full eligibility-gated, idempotent Xero-account-suggestion
    sequence for one `evidence_id`. See module docstring for the
    idempotency/failure-handling doctrine.

    Raises:
        core.errors.NotFoundError: no such `evidence_id`/`entity_id`.
    """
    evidence = evidence_repository.get_evidence(evidence_id)

    # Step 1 — entity resolution gate (fail closed, no AI call).
    if not evidence.entity_id or evidence.entity_id != entity_id:
        return ProduceAccountSuggestionResult(outcome=OUTCOME_ENTITY_NOT_RESOLVED)

    # Step 2 — classification eligibility gate. See module's
    # `ELIGIBLE_DOCUMENT_TYPES` for the documented judgment call on
    # which document types are in scope. `status == STATUS_CLASSIFIED`
    # already implies `source` is `DETERMINISTIC_RULE` or
    # `OPERATOR_ASSIGNED` by construction — an `AI_PROPOSAL`
    # classification is NEVER created with `status=CLASSIFIED` (see
    # `services.evidence.classification_orchestrator.classify_evidence`,
    # which only ever persists an AI proposal as `REVIEW_REQUIRED`/
    # `UNCLASSIFIABLE`) — so no separate `source` check is needed here;
    # the status check alone already excludes an unreviewed AI proposal.
    current = classification_repository.get_current_classification(evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE)
    if (
        current is None
        or current.status != STATUS_CLASSIFIED
        or current.document_type not in ELIGIBLE_DOCUMENT_TYPES
    ):
        return ProduceAccountSuggestionResult(outcome=OUTCOME_NOT_ELIGIBLE_FOR_ACCOUNT_SUGGESTION)

    # Step 3 — Xero connection gate.
    connection = xero_connection_repository.get_by_entity(entity_id)
    if connection is None or connection.status != "CONNECTED":
        return ProduceAccountSuggestionResult(outcome=OUTCOME_XERO_NOT_CONNECTED)

    # Step 4 — reference-data staleness gate (fail closed, no AI call —
    # never a Needs You item; see this WO's own explicit non-goal list).
    if is_reference_data_stale(connection):
        return ProduceAccountSuggestionResult(outcome=OUTCOME_REFERENCE_DATA_STALE)

    # Step 5 — "the question is already answered" guards. Checked in
    # this order (assignment BEFORE suggestion) deliberately: an
    # assignment can only exist once the question has been fully
    # resolved, which is the stronger, more accurate fact to report —
    # checking suggestion-existence first would report
    # SUGGESTION_ALREADY_EXISTS even when the evidence is, in truth,
    # already fully coded.
    existing_assignment = assignment_repository.get_by_evidence(evidence_id)
    if existing_assignment is not None:
        return ProduceAccountSuggestionResult(outcome=OUTCOME_ALREADY_ASSIGNED, assignment=existing_assignment)

    existing_suggestion = suggestion_repository.get_by_evidence(evidence_id)
    if existing_suggestion is not None:
        # Crash-recovery re-entry (mirrors `classify_evidence`'s own
        # top-of-function guard): the suggestion committed on an earlier
        # call, but the process may have died before the Needs You item
        # was created — ensure it exists before returning.
        needs_you_item = _ensure_needs_you_item(
            suggestion=existing_suggestion, needs_you_repository=needs_you_repository, correlation_id=correlation_id,
        )
        return ProduceAccountSuggestionResult(
            outcome=OUTCOME_SUGGESTION_ALREADY_EXISTS,
            suggestion=existing_suggestion,
            needs_you_item_id=needs_you_item.item_id,
        )

    # Step 6 — candidate universe. Never the raw synced set — always
    # through `list_eligible_accounts` (see services.xero.eligibility).
    eligible_accounts = list_eligible_accounts(xero_account_repository.list_accounts(entity_id=entity_id))
    if not eligible_accounts:
        return ProduceAccountSuggestionResult(outcome=OUTCOME_NO_ELIGIBLE_ACCOUNTS)

    # Step 7 — bounded context build.
    if not evidence.storage_reference:
        return ProduceAccountSuggestionResult(
            outcome=OUTCOME_CONTEXT_UNSUPPORTED,
            unsupported_reason="EvidenceItem has no stored content (storage_reference is empty)",
        )
    raw_content = object_store.get(evidence.storage_reference)
    entity = entity_repository.get_entity(entity_id)
    context_result = build_account_suggestion_context(
        evidence=evidence, raw_content=raw_content, classification=current,
        eligible_accounts=eligible_accounts, entity_name=entity.display_name,
    )
    if context_result.outcome != _CONTEXT_OUTCOME_BUILT:
        return ProduceAccountSuggestionResult(
            outcome=OUTCOME_CONTEXT_UNSUPPORTED, unsupported_reason=context_result.unsupported_reason,
        )
    context = context_result.context
    assert context is not None  # noqa: S101 - guaranteed by OUTCOME_BUILT

    # Resolving the task contract here (even though its return value is
    # not otherwise needed — `run_background_task` below resolves it
    # again on its own) proves the task is genuinely registered BEFORE
    # any further work, exactly mirroring `classify_evidence`'s own
    # early `get_task_contract` call.
    get_task_contract(AI_TASK_ID, AI_TASK_VERSION)
    prompt_contract_version = resolve_prompt_contract_version(AI_TASK_ID, AI_TASK_VERSION)
    fingerprint = compute_account_suggestion_fingerprint(
        task_id=AI_TASK_ID, task_version=AI_TASK_VERSION, prompt_contract_version=prompt_contract_version,
        evidence_id=evidence_id, evidence_content_hash=_evidence_content_hash(evidence),
        context_contract_version=context.context_contract_version, context_sha256=context.context_sha256,
        eligible_account_ids=[a.account_id for a in eligible_accounts],
    )

    # Step 8 — subject-scoped concurrency guard + crash-recovery reuse
    # (see module docstring for why this is subject-scoped, not
    # fingerprint-scoped).
    active = ai_invocation_repository.find_active_invocation(
        task_id=AI_TASK_ID, task_version=AI_TASK_VERSION, primary_input_reference=evidence_id,
    )
    if active is not None:
        return ProduceAccountSuggestionResult(
            outcome=OUTCOME_AI_IN_PROGRESS, ai_invocation_id=active.ai_invocation_id, fingerprint=fingerprint,
        )

    prior_succeeded = next(
        (
            inv
            for inv in ai_invocation_repository.list_invocations(
                task_id=AI_TASK_ID, task_version=AI_TASK_VERSION, primary_input_reference=evidence_id,
            )
            if inv.status == "SUCCEEDED"
        ),
        None,
    )
    if prior_succeeded is not None:
        # Crash-recovery: the model call itself already completed but
        # the suggestion row was never persisted (process died in
        # between) — reuse it rather than calling the model again.
        invocation = prior_succeeded
    else:
        invocation = run_background_task(
            task_id=AI_TASK_ID,
            task_version=AI_TASK_VERSION,
            input_references={"evidence_id": evidence_id},
            evidence_content=context.rendered_context,
            actor_type=actor_type,
            actor_id=actor_id,
            correlation_id=correlation_id,
            repository=ai_invocation_repository,
            litellm_client=litellm_client,
            record_audit_event=record_audit_event,
        )

    # Step 9 — AI failure -> ZERO suggestion rows (no phantom state).
    if invocation.status != "SUCCEEDED":
        return ProduceAccountSuggestionResult(
            outcome=OUTCOME_AI_INVOCATION_FAILED, ai_invocation_id=invocation.ai_invocation_id, fingerprint=fingerprint,
        )

    # Step 10 — validate the AI's proposal against the real eligible set
    # (never trusted on its own claim of validity).
    output: Mapping[str, Any] = invocation.output or {}
    suggested_account_id = output.get("suggested_account_id")
    confidence = output.get("confidence")
    signals = tuple(output.get("signals") or ())

    resolution = resolve_ai_suggested_account(suggested_account_id, eligible_candidates=eligible_accounts)
    if not resolution.resolved:
        record_audit_event(
            event_type=EVENT_SUGGESTION_REJECTED,
            actor_type=actor_type,
            actor_id=actor_id,
            subject_type="AIInvocation",
            subject_id=invocation.ai_invocation_id,
            correlation_id=correlation_id or invocation.ai_invocation_id,
            causation_id=None,
            payload={
                "evidence_id": evidence_id,
                "entity_id": entity_id,
                "reason": resolution.reason,
            },
        )
        return ProduceAccountSuggestionResult(
            outcome=OUTCOME_SUGGESTED_ACCOUNT_REJECTED,
            ai_invocation_id=invocation.ai_invocation_id,
            rejection_reason=resolution.reason,
            fingerprint=fingerprint,
        )

    # Step 11 — persist the suggestion, audit, raise the Needs You item.
    suggestion = suggestion_repository.create_suggestion(
        evidence_id=evidence_id,
        entity_id=entity_id,
        tenant_id=connection.tenant_id,
        suggested_account_id=resolution.account_id,
        confidence=confidence,
        signals=signals,
        ai_invocation_id=invocation.ai_invocation_id,
    )

    record_audit_event(
        event_type=EVENT_SUGGESTION_PRODUCED,
        actor_type=actor_type,
        actor_id=actor_id,
        subject_type="XeroAccountSuggestion",
        subject_id=suggestion.suggestion_id,
        correlation_id=correlation_id or suggestion.suggestion_id,
        causation_id=None,
        payload={
            "evidence_id": evidence_id,
            "entity_id": entity_id,
            "tenant_id": connection.tenant_id,
            "suggested_account_id": suggestion.suggested_account_id,
            "confidence": confidence,
            "ai_invocation_id": invocation.ai_invocation_id,
            "fingerprint": fingerprint,
        },
    )

    needs_you_item = _ensure_needs_you_item(
        suggestion=suggestion, needs_you_repository=needs_you_repository, correlation_id=correlation_id,
    )

    return ProduceAccountSuggestionResult(
        outcome=OUTCOME_SUGGESTION_PRODUCED,
        suggestion=suggestion,
        needs_you_item_id=needs_you_item.item_id,
        ai_invocation_id=invocation.ai_invocation_id,
        fingerprint=fingerprint,
    )
