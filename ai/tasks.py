"""AI task contract/registry framework (CD-5 PID §21-24/§31/§55, WI-1).

Introduces the typed AI task registry PID §21 requires instead of a
generic ``ask_llm(prompt)`` surface: every governed BAGMAN AI task is a
:class:`TaskContract` with a versioned, schema-validated input/output
shape, registered by exact ``(task_id, task_version)`` in
:data:`TASK_REGISTRY`. Callers request the TASK, never the model (PID
§22).

This module defines task *metadata* only — the original four CD-5 WI-1
tasks' actual prompt CONTENT (the wording sent to a model) is
explicitly out of WI-1 scope (PID §31: prompts are versioned assets,
but authoring them belongs to whichever work item actually calls the
provider — WI-2 for the three ``BACKGROUND`` tasks, WI-3 for
``OPERATOR_DOCUMENT_REVIEW``). What WI-1 DOES own is the contract SHAPE
those prompts/outputs must conform to: `input_schema`, `output_schema`,
`timeout_seconds`, `confidence_policy`, `data_policy` — real, usable
JSON Schemas, not placeholders, so WI-2/WI-3 can build directly on
them. WI-3 additively registers a 5th task, ``ASK_BAGMAN`` v1, further
down this module — the registry is deliberately OPEN (PID §22/§29's own
"open, not a closed enum" doctrine), so this is expected, additive
growth, not a WI-1 change.

Output shape convention
------------------------
Every task's `output_schema` below follows one consistent shape: one
task-specific "proposal" field (e.g. `proposed_type`,
`proposed_entity_hint`, `summary`, `decision_summary`) alongside the
three PID §24/§25 common fields `confidence` (task-specific meaning,
PID §55), `signals` (short evidence-based justifications), and
`warnings` (caveats/uncertainty) — directly generalising the `PID
§24` `DOCUMENT_TYPE_PROPOSAL` example. This is a documented convention
for THESE four tasks, not a schema-enforced requirement on tasks in
general — a future task's `output_schema` is free to shape itself
differently if that genuinely serves it better, since `TaskContract`
does not itself constrain `output_schema`'s shape beyond "a JSON
Schema".

Data policy (PID §51)
-----------------------
BAGMAN's chosen closed set is exactly PID §51's own suggested vocabulary
— ``LOCAL_OK`` / ``CLOUD_OPERATOR_OK`` / ``CLOUD_RESTRICTED`` — used
as-is with no reason found to depart from it:

* ``LOCAL_OK`` — safe to route to the dedicated, BAGMAN-exclusive
  Mac-mini tier (and, when a task's routing genuinely escalates,
  Trinity compute) via `bagman-fast`/`bagman-core`/`bagman-deep`. All
  three CD-5 `BACKGROUND` tasks use this: routine document/entity
  analysis is exactly the "local-first background privacy" case PID
  §52 describes.
* ``CLOUD_OPERATOR_OK`` — safe to send to Claude (BAGMAN's one
  authorised cloud operator provider, under Matt's own Anthropic key,
  PID §13/§17) but not to any other cloud provider. `OPERATOR_DOCUMENT_REVIEW`
  uses this.
* ``CLOUD_RESTRICTED`` — reserved for evidence too sensitive for any
  current AI tier; no CD-5 task uses it, but the vocabulary exists so a
  future task can declare it without a contract redesign.

WI-1 does not itself enforce `data_policy` against real evidence
sensitivity (there is no evidence-sensitivity classifier yet) — it only
declares each task's policy, ready for WI-2/WI-3's gateway to read and
act on before ever dispatching a call.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from ai.invocation import BACKGROUND_CAPABILITY_ALIASES, TaskRole
from core.contract_validation import describe_schema_errors
from core.errors import NotFoundError, ValidationError

#: PID §51's provider-aware data-policy vocabulary (see module
#: docstring's "Data policy" section for the rationale for each value).
DATA_POLICIES: frozenset[str] = frozenset({"LOCAL_OK", "CLOUD_OPERATOR_OK", "CLOUD_RESTRICTED"})


@dataclass(frozen=True)
class ValidationResult:
    """Structured, storable result of validating a task's output
    against its `output_schema` (PID §24/§76) — this shape is exactly
    what `AIInvocation.validation_result` /
    `bagman.ai_invocation.v1.validation_result` requires
    (`{"valid": bool, "errors": [str, ...]}`), so a caller can persist
    `to_dict()`'s result directly with no reshaping.
    """

    valid: bool
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"valid": self.valid, "errors": list(self.errors)}


@dataclass(frozen=True)
class TaskContract:
    """A single versioned AI task definition (PID §21-22). Every field
    PID §22 lists is present; construction validates the closed-set/
    cross-field rules documented on each field below and raises
    `core.errors.ValidationError` if violated — this runs for every
    `TaskContract` at module-import time (the five CD-5 tasks below —
    WI-1's original four plus WI-3's additive `ASK_BAGMAN`), so a shape
    mistake in this file fails immediately and loudly, not silently at
    first use.
    """

    task_id: str
    task_version: int
    role: TaskRole
    #: For a `BACKGROUND` task: one of `BACKGROUND_CAPABILITY_ALIASES`
    #: — the alias this task is normally routed to (WI-2's gateway may
    #: still escalate to `bagman-deep` under its own policy; this is
    #: the task's PREFERRED/default capability, not an unbreakable
    #: pin). For an `OPERATOR` task: always `None` — the same
    #: null-for-OPERATOR sentinel `AIInvocation.capability_alias` uses,
    #: since Claude is never reached via a LiteLLM alias (PID §8).
    preferred_capability: Optional[str]
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    timeout_seconds: int
    #: Small, task-specific structure documenting what `confidence`
    #: MEANS for this task and whether/how it should be used (PID
    #: §55) — deliberately never a bare numeric threshold, since PID
    #: §55 explicitly forbids one universal cutoff (e.g. 0.8) across
    #: every task.
    confidence_policy: Mapping[str, Any]
    #: One of `DATA_POLICIES` (PID §51).
    data_policy: str

    def __post_init__(self) -> None:
        if self.role not in ("OPERATOR", "BACKGROUND"):
            raise ValidationError(
                f"TaskContract '{self.task_id}' v{self.task_version}: role "
                f"'{self.role}' is not one of the closed set ('OPERATOR', 'BACKGROUND') (PID §8)"
            )
        if self.role == "BACKGROUND":
            if self.preferred_capability not in BACKGROUND_CAPABILITY_ALIASES:
                raise ValidationError(
                    f"TaskContract '{self.task_id}' v{self.task_version}: a BACKGROUND task's "
                    f"preferred_capability must be one of {sorted(BACKGROUND_CAPABILITY_ALIASES)} "
                    f"(PID §9); got {self.preferred_capability!r}"
                )
        else:  # OPERATOR
            if self.preferred_capability is not None:
                raise ValidationError(
                    f"TaskContract '{self.task_id}' v{self.task_version}: an OPERATOR task's "
                    f"preferred_capability must be null (Claude is never reached via a LiteLLM "
                    f"alias, PID §8); got {self.preferred_capability!r}"
                )
        if self.data_policy not in DATA_POLICIES:
            raise ValidationError(
                f"TaskContract '{self.task_id}' v{self.task_version}: data_policy "
                f"'{self.data_policy}' is not one of the closed set {sorted(DATA_POLICIES)} (PID §51)"
            )
        if self.task_version < 1:
            raise ValidationError(
                f"TaskContract '{self.task_id}': task_version must be >= 1, got {self.task_version}"
            )
        if self.timeout_seconds <= 0:
            raise ValidationError(
                f"TaskContract '{self.task_id}' v{self.task_version}: timeout_seconds must be "
                f"positive (PID §75 — no LLM call may wait forever); got {self.timeout_seconds}"
            )


def _common_confidence_signals_warnings_properties() -> dict:
    """The three PID §24/§25 fields shared by every CD-5 task's output
    shape (see module docstring's "Output shape convention")."""
    return {
        "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": "Task-specific confidence (PID §55) — see this task's own confidence_policy.",
        },
        "signals": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Short, evidence-based justifications supporting the proposal (PID §24).",
        },
        "warnings": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Caveats, ambiguity, or uncertainty the caller/operator should see (PID §24).",
        },
    }


#: Shared input shape for the three document-oriented tasks — every
#: one of them reasons about exactly one `EvidenceItem`, referenced by
#: `evidence_id` (PID §29). `OPERATOR_DOCUMENT_REVIEW` extends this
#: with an optional operator question (see its own schema below).
_DOCUMENT_INPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "evidence_id": {
            "type": "string",
            "minLength": 1,
            "description": "Canonical evidence_id this task reasons about (PID §29). Traceability, not a raw byte payload — the provider adapter (WI-2/WI-3) is responsible for loading whatever bounded, authorised context this evidence_id refers to.",
        }
    },
    "required": ["evidence_id"],
    "additionalProperties": False,
}


_DOCUMENT_SUMMARY_OUTPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "minLength": 1,
            "description": "A structured, factual summary of the document's material content — never a hidden chain-of-thought narrative (PID §25).",
        },
        **_common_confidence_signals_warnings_properties(),
    },
    "required": ["summary", "confidence", "signals", "warnings"],
    "additionalProperties": False,
}

DOCUMENT_SUMMARY_V1 = TaskContract(
    task_id="DOCUMENT_SUMMARY",
    task_version=1,
    role="BACKGROUND",
    preferred_capability="bagman-fast",
    input_schema=_DOCUMENT_INPUT_SCHEMA,
    output_schema=_DOCUMENT_SUMMARY_OUTPUT_SCHEMA,
    timeout_seconds=30,
    confidence_policy={
        "meaning": (
            "The model's self-reported confidence that `summary` faithfully and "
            "completely represents the source document's material content."
        ),
        "notes": "No fixed pass/fail threshold is enforced by this task contract (PID §55).",
    },
    data_policy="LOCAL_OK",
)


_DOCUMENT_TYPE_PROPOSAL_OUTPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "proposed_type": {
            "type": "string",
            "pattern": "^[A-Z][A-Z_]*$",
            "description": (
                "Proposed EvidenceItem.evidence_type-shaped classification (PID §24) — a "
                "PROPOSAL only, never a canonical assignment. Deliberately open (matches "
                "`bagman.evidence.v1.evidence_type`'s own open taxonomy), e.g. `INVOICE`, "
                "`RECEIPT`, `STATEMENT`, `CONTRACT`, `UNKNOWN`."
            ),
        },
        **_common_confidence_signals_warnings_properties(),
    },
    "required": ["proposed_type", "confidence", "signals", "warnings"],
    "additionalProperties": False,
}

DOCUMENT_TYPE_PROPOSAL_V1 = TaskContract(
    task_id="DOCUMENT_TYPE_PROPOSAL",
    task_version=1,
    role="BACKGROUND",
    preferred_capability="bagman-fast",
    input_schema=_DOCUMENT_INPUT_SCHEMA,
    output_schema=_DOCUMENT_TYPE_PROPOSAL_OUTPUT_SCHEMA,
    timeout_seconds=20,
    confidence_policy={
        "meaning": "The model's self-reported confidence that `proposed_type` is correct.",
        "notes": "No fixed pass/fail threshold is enforced by this task contract (PID §55).",
    },
    data_policy="LOCAL_OK",
)


#: The exact closed V2 canonical `proposed_type` vocabulary (CD-6 Slice
#: 5 WI-3 §5) — identical, verbatim, to Slice-5 V1's own closed
#: `document_type` vocabulary
#: (`services.evidence.classification.DOCUMENT_TYPES`). Duplicated here
#: (rather than imported) deliberately: `ai/` never imports
#: `services.evidence.*` (see this repo's own
#: `core`/`services`-never-imports-`app`/`persistence` boundary
#: discipline, and `ai.gateway.background`'s own "no `app.api
#: .composition` import inside `ai/gateway/` or `services/`" note) — the
#: two modules are independently governed contracts that happen to
#: share a vocabulary by design (WI-3 §5's own explicit instruction),
#: not one importing the other. A future drift between the two lists
#: would be a real defect; `tests/integration/test_document_type_proposal_v2.py`
#: asserts they stay in lockstep.
DOCUMENT_TYPE_PROPOSAL_V2_CANONICAL_TYPES: tuple[str, ...] = (
    "SUPPLIER_INVOICE",
    "RECEIPT",
    "ORDER_CONFIRMATION",
    "REFUND_CONFIRMATION",
    "BROKER_STATEMENT",
    "BROKER_ACTIVITY_NOTICE",
    "NON_ACCOUNTING_DOCUMENT",
    "UNKNOWN",
)

#: CD-6 Slice 5 WI-3 §12 — v2's `input_references` shape carries enough
#: immutable provenance to identify EXACTLY what classifier input was
#: analyzed, without ever carrying the document content itself.
#: `evidence_id` is also what `ai.invocation.derive_primary_input_reference`
#: keys the one-active-invocation-per-subject concurrency guard on (PID
#: §73) — it is first in `PRIMARY_INPUT_REFERENCE_KEYS`' precedence
#: order, so this task's subject is always its `evidence_id`.
_DOCUMENT_TYPE_PROPOSAL_V2_INPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "evidence_id": {
            "type": "string",
            "minLength": 1,
            "description": "Canonical evidence_id this proposal reasons about (PID §29).",
        },
        "evidence_content_hash": {
            "type": "string",
            "minLength": 1,
            "description": "The referenced EvidenceItem's own content_hash value — immutable provenance for exactly which stored bytes were analyzed.",
        },
        "classification_context_version": {
            "type": "string",
            "minLength": 1,
            "description": "The context-builder contract version that produced the evidence content sent to the model (e.g. 'bagman.evidence_classification_context.v1') — see services.evidence.classification_context.",
        },
        "classification_context_hash": {
            "type": "string",
            "minLength": 1,
            "description": "SHA-256 of the exact bounded, rendered context string sent to the model — never the context text itself.",
        },
        "classifier_fingerprint": {
            "type": "string",
            "minLength": 1,
            "description": "Deterministic SHA-256 fingerprint of (task_id, task_version, prompt_contract_version, preferred_capability, classification_context_version, evidence_id, evidence_content_hash, classification_context_hash) — see services.evidence.classification_ai_fingerprint. Used for automatic-invocation idempotency/reuse (WI-3 §25).",
        },
    },
    "required": [
        "evidence_id",
        "evidence_content_hash",
        "classification_context_version",
        "classification_context_hash",
        "classifier_fingerprint",
    ],
    "additionalProperties": False,
}

#: CD-6 Slice 5 WI-3 §6 — bounded output shape, distinct from
#: `_common_confidence_signals_warnings_properties()` (that shared
#: helper's `signals`/`warnings` are UNBOUNDED, which §6 explicitly
#: forbids for v2: "Bound: signal/warning item count; individual string
#: size"). `proposed_type` is a closed `enum` (never v1's open
#: `pattern`-based field) — the model contract itself must generate
#: canonical vocabulary, never a translation layer applied after the
#: fact (§5).
_DOCUMENT_TYPE_PROPOSAL_V2_OUTPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "proposed_type": {
            "type": "string",
            "enum": list(DOCUMENT_TYPE_PROPOSAL_V2_CANONICAL_TYPES),
            "description": (
                "Proposed canonical document type — MUST be exactly one of Slice-5 V1's own "
                "closed document_type vocabulary (WI-3 §5). Never a generic INVOICE/STATEMENT/"
                "CONTRACT/BILL/OTHER value. Use UNKNOWN when the evidence context is insufficient."
            ),
        },
        "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": (
                "Model self-reported confidence that proposed_type correctly describes the "
                "supplied bounded evidence context (WI-3 §7). This is NOT authorization to "
                "auto-accept the classification — no confidence threshold anywhere in this "
                "task contract promotes AI output to canonical accepted truth."
            ),
        },
        "signals": {
            "type": "array",
            "maxItems": 10,
            "items": {"type": "string", "maxLength": 300},
            "description": "Short, evidence-based justifications for the proposal — bounded item count and string length (WI-3 §6).",
        },
        "warnings": {
            "type": "array",
            "maxItems": 10,
            "items": {"type": "string", "maxLength": 300},
            "description": "Short caveats/uncertainty a reviewer should see — bounded item count and string length (WI-3 §6).",
        },
    },
    "required": ["proposed_type", "confidence", "signals", "warnings"],
    "additionalProperties": False,
}

#: CD-6 Slice 5 WI-3 — the first canonical, governed AI document
#: classifier. Registered ADDITIVELY alongside `DOCUMENT_TYPE_PROPOSAL_V1`
#: (untouched, still v1's own open/generic vocabulary — WI-3 §3) under
#: the SAME `task_id` but a distinct `task_version` — `TASK_REGISTRY`
#: is already keyed by `(task_id, task_version)`, so this is purely
#: additive. Uses `bagman-core` (WI-3 §4: "not bagman-fast, for the
#: first canonical classifier") — no alias escalation/fallback; if
#: bagman-core fails, `ai.gateway.background.run_background_task`
#: fails the invocation honestly (its own module contract already
#: guarantees this — WI-3 adds no new fallback logic).
DOCUMENT_TYPE_PROPOSAL_V2 = TaskContract(
    task_id="DOCUMENT_TYPE_PROPOSAL",
    task_version=2,
    role="BACKGROUND",
    preferred_capability="bagman-core",
    input_schema=_DOCUMENT_TYPE_PROPOSAL_V2_INPUT_SCHEMA,
    output_schema=_DOCUMENT_TYPE_PROPOSAL_V2_OUTPUT_SCHEMA,
    timeout_seconds=30,
    confidence_policy={
        "meaning": (
            "The model's self-reported confidence that proposed_type correctly describes the "
            "supplied bounded evidence context (WI-3 §7) — not a claim about ground truth beyond "
            "that context, and never an auto-acceptance authorization."
        ),
        "notes": (
            "No fixed pass/fail threshold is enforced by this task contract or by the WI-3 "
            "classification orchestrator — every AI proposal is persisted as "
            "source=AI_PROPOSAL/status=REVIEW_REQUIRED (or UNCLASSIFIABLE for UNKNOWN), never "
            "silently promoted to status=CLASSIFIED regardless of confidence value (PID §55; WI-3 §2)."
        ),
    },
    data_policy="LOCAL_OK",
)


#: CD-6 follow-up (post-Slice-5, "AI classifier boundary correction") —
#: a NEW prompt-contract version fixing one genuine semantic ambiguity
#: found by a 46-item fixed shadow evaluation of v2: v2's own
#: BROKER_ACTIVITY_NOTICE wording never explicitly required the discrete
#: event it describes to have ALREADY occurred/been executed/settled,
#: so a forward-looking, not-yet-executed corporate-action announcement
#: (e.g. a "FYI: Takeover Notification" from a third-party corporate-
#: events feed) could be — and once was — misclassified as
#: BROKER_ACTIVITY_NOTICE instead of NON_ACCOUNTING_DOCUMENT. v2 itself
#: is left byte-for-byte untouched (immutable historical classifier
#: meaning — WI-3's own "never edit an existing vN.md file's wording in
#: place" discipline, `ai/prompts/loader.py`'s module docstring) — v3 is
#: registered ADDITIVELY, alongside v1 and v2, under the SAME task_id,
#: reusing v2's own input/output schema shapes verbatim (the CONTEXT
#: shape this task consumes, and the closed eight-value canonical
#: vocabulary it must produce, are both completely unchanged — only the
#: system prompt's own wording changes, see
#: `ai/prompts/document_type_proposal/v3.md`). `timeout_seconds` stays
#: at v2's own 30 (explicitly not touched by this delivery), and
#: `data_policy` stays `LOCAL_OK` (matching V1/V2's own convention:
#: routine document/entity analysis routed to the BAGMAN-exclusive
#: Mac-mini tier, PID §52 — no reason found to depart from it for a
#: same-tier, same-provider, wording-only prompt fix).
DOCUMENT_TYPE_PROPOSAL_V3 = TaskContract(
    task_id="DOCUMENT_TYPE_PROPOSAL",
    task_version=3,
    role="BACKGROUND",
    preferred_capability="bagman-core",
    input_schema=_DOCUMENT_TYPE_PROPOSAL_V2_INPUT_SCHEMA,
    output_schema=_DOCUMENT_TYPE_PROPOSAL_V2_OUTPUT_SCHEMA,
    timeout_seconds=30,
    confidence_policy={
        "meaning": (
            "The model's self-reported confidence that proposed_type correctly describes the "
            "supplied bounded evidence context — not a claim about ground truth beyond that "
            "context, and never an auto-acceptance authorization (identical meaning to v2's own "
            "confidence_policy; only the underlying prompt wording changed)."
        ),
        "notes": (
            "No fixed pass/fail threshold is enforced by this task contract or by the "
            "classification orchestrator — every AI proposal is persisted as "
            "source=AI_PROPOSAL/status=REVIEW_REQUIRED (or UNCLASSIFIABLE for UNKNOWN), never "
            "silently promoted to status=CLASSIFIED regardless of confidence value (PID §55)."
        ),
    },
    data_policy="LOCAL_OK",
)


_ENTITY_PROPOSAL_OUTPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "proposed_entity_hint": {
            "oneOf": [
                {"type": "string", "minLength": 1},
                {"type": "null"},
            ],
            "description": (
                "Proposed GovernedEntity ownership HINT (e.g. a `canonical_name`-shaped slug "
                "such as `NOUSTAI_LIMITED`, or `null` for 'no confident candidate') — never an "
                "asserted `entity_id` foreign key. Same 'hint, not ownership assertion' doctrine "
                "as `sources.governed_entity_hint`/`IntakeRecord.entity_hint` (PID §9/§10); a "
                "proposal from this task can never itself resolve entity ownership."
            ),
        },
        **_common_confidence_signals_warnings_properties(),
    },
    "required": ["proposed_entity_hint", "confidence", "signals", "warnings"],
    "additionalProperties": False,
}

ENTITY_PROPOSAL_V1 = TaskContract(
    task_id="ENTITY_PROPOSAL",
    task_version=1,
    role="BACKGROUND",
    preferred_capability="bagman-core",
    input_schema=_DOCUMENT_INPUT_SCHEMA,
    output_schema=_ENTITY_PROPOSAL_OUTPUT_SCHEMA,
    timeout_seconds=30,
    confidence_policy={
        "meaning": "The model's self-reported confidence that `proposed_entity_hint` is the correct owning entity.",
        "notes": "No fixed pass/fail threshold is enforced by this task contract (PID §55).",
    },
    data_policy="LOCAL_OK",
)


_OPERATOR_DOCUMENT_REVIEW_INPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "evidence_id": {
            "type": "string",
            "minLength": 1,
            "description": "Canonical evidence_id under operator review (PID §29).",
        },
        "operator_question": {
            "oneOf": [
                {"type": "string", "minLength": 1},
                {"type": "null"},
            ],
            "description": (
                "Optional free-text question Matt asked about this evidence (e.g. 'What is "
                "this document?', PID §43) — untrusted operator input, not a task instruction "
                "override (the task/system instruction remains authoritative per PID §32); "
                "`null` for a bare 'review this' request with no specific question."
            ),
        },
    },
    "required": ["evidence_id", "operator_question"],
    "additionalProperties": False,
}

_OPERATOR_DOCUMENT_REVIEW_OUTPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "decision_summary": {
            "type": "string",
            "minLength": 1,
            "description": "Claude's structured, auditable summary of what it found (PID §25) — never hidden chain-of-thought, only the useful conclusion.",
        },
        **_common_confidence_signals_warnings_properties(),
    },
    "required": ["decision_summary", "confidence", "signals", "warnings"],
    "additionalProperties": False,
}

OPERATOR_DOCUMENT_REVIEW_V1 = TaskContract(
    task_id="OPERATOR_DOCUMENT_REVIEW",
    task_version=1,
    role="OPERATOR",
    preferred_capability=None,
    input_schema=_OPERATOR_DOCUMENT_REVIEW_INPUT_SCHEMA,
    output_schema=_OPERATOR_DOCUMENT_REVIEW_OUTPUT_SCHEMA,
    timeout_seconds=60,
    confidence_policy={
        "meaning": "Claude's self-reported confidence in `decision_summary`'s conclusion.",
        "notes": "No fixed pass/fail threshold is enforced by this task contract (PID §55).",
    },
    data_policy="CLOUD_OPERATOR_OK",
)


_ASK_BAGMAN_INPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "message": {
            "type": "string",
            "minLength": 1,
            "description": (
                "The operator's free-text Ask BAGMAN message (PID §42-43) — untrusted "
                "operator input, not a task instruction override."
            ),
        },
        "evidence_id": {
            "oneOf": [{"type": "string", "minLength": 1}, {"type": "null"}],
            "description": "Canonical evidence_id in context, if the operator is asking about a specific document.",
        },
        "intake_id": {
            "oneOf": [{"type": "string", "minLength": 1}, {"type": "null"}],
            "description": "Canonical intake_id in context, if the operator is asking about a specific intake attempt.",
        },
        "entity_id": {
            "oneOf": [{"type": "string", "minLength": 1}, {"type": "null"}],
            "description": "Canonical entity_id in context, if the operator is asking about a specific GovernedEntity.",
        },
        "conversation_id": {
            "oneOf": [{"type": "string", "minLength": 1}, {"type": "null"}],
            "description": (
                "CD-6 reliability delta (PID §98/§100): a GUI-generated identifier for ONE open "
                "Ask BAGMAN drawer session (see `app/api/static/features/ai/ask-bagman.js`) — the "
                "conversation-scoped subject `ai.invocation.derive_primary_input_reference` falls "
                "back to (last in precedence) when none of `evidence_id`/`intake_id`/`entity_id` is "
                "present, so a genuinely contextless 'hi bagman' turn is still traceable to a real "
                "canonical subject instead of being rejected outright."
            ),
        },
        "source": {
            "type": "string",
            "minLength": 1,
            "description": (
                "CD-6 reliability delta (PID §98/§100): a simple, honest literal recording which UI "
                "surface originated this call, e.g. `\"ask_bagman_drawer\"` — part of the full "
                "provenance list the architect specified for every Ask BAGMAN turn. Not a closed "
                "taxonomy (deliberately open, like `task_id`/`event_type` elsewhere) — `\"unknown\"` "
                "is used when a caller does not supply one."
            ),
        },
    },
    "required": ["message", "evidence_id", "intake_id", "entity_id", "conversation_id", "source"],
    "additionalProperties": False,
}

_ASK_BAGMAN_OUTPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "response_text": {
            "type": "string",
            "minLength": 1,
            "description": "Claude's final natural-language answer (PID §25 — the conclusion, never hidden chain-of-thought).",
        },
        "tool_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tool": {"type": "string", "minLength": 1},
                    "input": {"type": "object"},
                    "summary": {"type": "string"},
                },
                "required": ["tool", "input", "summary"],
                "additionalProperties": False,
            },
            "description": (
                "Structured, auditable record of which registered tools were called and "
                "with what inputs/outputs (PID §25/§62) — never raw model scratch reasoning."
            ),
        },
        "referenced_evidence_ids": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": "Canonical evidence_ids the response refers to (PID §44) — so the GUI can render them as clickable references.",
        },
        "warnings": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Caveats — e.g. the bounded tool-call iteration limit was reached (PID §24/§35).",
        },
    },
    "required": ["response_text", "tool_calls", "referenced_evidence_ids", "warnings"],
    "additionalProperties": False,
}

#: CD-5 WI-3 addition (additive, PID §21's registry stays open by
#: design — see this module's own docstring). Ask BAGMAN's general,
#: potentially-multi-turn-tool-calling conversational task: distinct
#: from `OPERATOR_DOCUMENT_REVIEW` (a single-shot, always-about-one-
#: `evidence_id` review) because Ask BAGMAN may reason about an
#: intake_id/entity_id instead, may call zero-or-more tools before
#: answering, and its output is conversational rather than a per-
#: document review. See `agent/claude_code/orchestrator.py`'s module
#: docstring (the CD-5 Gate-2 corrected implementation;
#: `agent/bagman/orchestrator.py`, the original WI-3 implementation
#: this comment once pointed to, was removed 2026-09-16 — see the CD-5
#: evidence file §6n) for the full "general chat has no evidence_id"
#: tension this task's `input_schema` resolves: `evidence_id`/`intake_id`/
#: `entity_id`/`conversation_id` are each optional (nullable), but
#: `ai.invocation.derive_primary_input_reference` still requires at
#: least one non-null. CD-6 reliability delta (PID §98/§100) added
#: `conversation_id` (last in precedence) and `source` specifically so
#: a genuinely subject-less "hi bagman"/"what needs my attention?"
#: query — the architect's own explicit ruling that "an
#: operator-originated conversational message is itself a valid
#: traceable input" — is no longer out of scope; a truly bare call with
#: not even a `conversation_id` still correctly raises `ValidationError`
#: (there is no recognised subject at all to key the concurrency guard
#: on), but the GUI's own Ask BAGMAN drawer always supplies one now
#: (see `app/api/static/features/ai/ask-bagman.js`), so this is no
#: longer reachable from ordinary GUI use.
ASK_BAGMAN_V1 = TaskContract(
    task_id="ASK_BAGMAN",
    task_version=1,
    role="OPERATOR",
    preferred_capability=None,
    input_schema=_ASK_BAGMAN_INPUT_SCHEMA,
    output_schema=_ASK_BAGMAN_OUTPUT_SCHEMA,
    timeout_seconds=90,
    confidence_policy={
        "meaning": (
            "Not applicable — Ask BAGMAN's conversational response does not carry a single "
            "numeric confidence value; per-tool-call results and `warnings` communicate "
            "uncertainty instead (PID §55 forbids one universal confidence threshold anyway)."
        ),
        "notes": "AIInvocation.confidence is left null for this task.",
    },
    data_policy="CLOUD_OPERATOR_OK",
)


#: The single source of truth for every registered task (PID §21),
#: keyed by exact `(task_id, task_version)` — callers request the
#: task, never the model (PID §22).
TASK_REGISTRY: Mapping[tuple[str, int], TaskContract] = {
    (contract.task_id, contract.task_version): contract
    for contract in (
        DOCUMENT_SUMMARY_V1,
        DOCUMENT_TYPE_PROPOSAL_V1,
        DOCUMENT_TYPE_PROPOSAL_V2,
        DOCUMENT_TYPE_PROPOSAL_V3,
        ENTITY_PROPOSAL_V1,
        OPERATOR_DOCUMENT_REVIEW_V1,
        ASK_BAGMAN_V1,
    )
}


def get_task_contract(task_id: str, task_version: int) -> TaskContract:
    """Look up the registered `TaskContract` for `(task_id, task_version)`.

    Raises:
        core.errors.NotFoundError: if no task is registered under that
            exact id/version pair.
    """
    try:
        return TASK_REGISTRY[(task_id, task_version)]
    except KeyError:
        raise NotFoundError(
            f"no registered AI task '{task_id}' version {task_version} — registered tasks: "
            f"{sorted(TASK_REGISTRY.keys())}"
        ) from None


def validate_task_output(task_contract: TaskContract, output: Mapping[str, Any]) -> ValidationResult:
    """Validate `output` against `task_contract.output_schema` (PID
    §24/§76), returning a non-raising, storable `ValidationResult`
    ready to persist directly as `AIInvocation.validation_result`.

    Never repairs or coerces `output` — an invalid output is reported
    as `ValidationResult(valid=False, errors=[...])`, never silently
    accepted or mutated (PID §76: "do not repair malformed output
    silently into canonical truth; record validation failure"). Note
    `output` is deliberately NOT forced through `dict(...)` before
    validation: a provider that returned something that is not even an
    object at all (a bare string, a list, `null`, ...) must be reported
    as a normal schema-type validation failure, not crash this
    function with a raw `TypeError`/`ValueError` from a failed
    conversion attempt.
    """
    errors = describe_schema_errors(output, dict(task_contract.output_schema))
    return ValidationResult(valid=not errors, errors=tuple(errors))
