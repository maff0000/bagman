"""Golden/synthetic fixtures for the CD-5 evaluation harness (PID
§59-61, WI-5).

Every fixture pairs (a) a synthetic "document" (plain text — real
content extraction does not exist yet, see ``ai/gateway/background.py``'s
own module docstring for why that is an accepted, documented CD-5
limitation, not something this harness papers over) with (b) a
SCRIPTED, deterministic fake LiteLLM response representing what a real
model's answer WOULD look like, and (c) a ``check`` callable that
inspects the resulting, real ``AIInvocation`` (produced by actually
running it through the REAL ``ai.gateway.background.run_background_task``
orchestration function — nothing here reimplements that logic) and
reports whether the harness/task-contract machinery behaved as this
fixture expects.

Honesty note (repeated from the module docstring of ``harness.py``,
because it is the single most important thing to understand about this
file): fixtures in the ``EXPECTED_CLASSIFICATION`` category do NOT
evaluate real model quality — no real model is reachable from this
environment today (PID §14's Mac-mini gate / the LiteLLM-gateway
database outage, see the WI-5 delivery report). They evaluate THIS
harness's own scoring logic and ``ai.tasks``' task-contract shapes: can
the harness correctly tell a scripted "correct" answer from a scripted
"incorrect" one? A future delivery, once a real model is reachable,
would replace `content=` in these fixtures' `ScriptedResponse` with a
genuine live provider call recorded once and pinned — the fixture
SHAPE (golden document, expected type, pass/fail scoring) does not
change.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from ai.invocation import AIInvocation
from ai.providers.litellm.client import LiteLLMOutcomeStatus

# ---------------------------------------------------------------------
# synthetic "documents" — plain text, never real evidence
# ---------------------------------------------------------------------

SYNTHETIC_INVOICE_TEXT = (
    "INVOICE #4471\n"
    "Bill to: NoustAI Limited\n"
    "Date: 2026-09-01\n"
    "Description: Cloud services — September 2026\n"
    "Subtotal: $1,200.00\n"
    "Tax: $0.00\n"
    "Total Due: $1,200.00\n"
    "Payment terms: Net 30\n"
)

SYNTHETIC_RECEIPT_TEXT = (
    "RECEIPT\n"
    "Store: Acme Office Supplies\n"
    "Date: 2026-08-15\n"
    "Items: Paper (5 reams) — $45.00, Pens (2 boxes) — $12.00\n"
    "Total Paid: $57.00\n"
    "Payment method: Card ending 4242\n"
    "Thank you for your purchase!\n"
)

#: Genuinely ambiguous — no invoice/receipt/statement/contract markers
#: at all, deliberately, so a scripted "honest abstention" response is
#: the one that behaves correctly here, not a forced guess.
SYNTHETIC_AMBIGUOUS_TEXT = (
    "re: the thing we discussed\n"
    "let me know if that works for you\n"
    "thanks\n"
)

SYNTHETIC_LONG_DOCUMENT_TEXT = (
    "MASTER SERVICES AGREEMENT\n"
    "This agreement is entered into between NoustAI Limited and the "
    "Customer for the provision of managed AI infrastructure services. "
    "Term: 12 months, auto-renewing. Governing law: England and Wales.\n"
)


# ---------------------------------------------------------------------
# scripted provider responses
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class ScriptedSuccess:
    """A scripted well-formed (or deliberately malformed) provider
    response body — handed to ``FakeLiteLLMClient.queue_success`` as
    ``content`` verbatim. Building this from a plain dict via
    :func:`json_content` is the common case; ``raw_text`` lets a
    fixture script something that is not even valid JSON at all (the
    malformed-output-not-json category)."""

    raw_text: str


def json_content(payload: Mapping[str, Any]) -> ScriptedSuccess:
    return ScriptedSuccess(raw_text=json.dumps(payload))


@dataclass(frozen=True)
class ScriptedFailure:
    """A scripted transport/timeout/provider-side failure — handed to
    ``FakeLiteLLMClient.queue_failure``."""

    status: LiteLLMOutcomeStatus
    error_detail: str = ""


ScriptedResponse = "ScriptedSuccess | ScriptedFailure"


# ---------------------------------------------------------------------
# check results and factories
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    passed: bool
    detail: str


CheckFn = Callable[[AIInvocation], CheckResult]

#: Below this self-reported confidence, a SUCCEEDED classification
#: fixture is treated as an honest ABSTENTION rather than a confident
#: (right or wrong) answer. Task-specific to DOCUMENT_TYPE_PROPOSAL's
#: own confidence_policy (PID §55 — no universal threshold is implied
#: for any OTHER task by this constant).
ABSTENTION_CONFIDENCE_THRESHOLD = 0.3


def expect_valid_structured_output() -> CheckFn:
    """STRUCTURED-OUTPUT-VALIDITY category: a well-formed response
    validates against the task's own output_schema via the REAL
    ``ai.tasks.validate_task_output`` (exercised internally by
    ``run_background_task`` — never reimplemented here)."""

    def _check(invocation: AIInvocation) -> CheckResult:
        if invocation.status != "SUCCEEDED":
            return CheckResult(False, f"expected SUCCEEDED, got {invocation.status} (error_code={invocation.error_code!r})")
        if not invocation.validation_result or invocation.validation_result.get("valid") is not True:
            return CheckResult(False, f"expected output_schema validation to pass, got {invocation.validation_result}")
        return CheckResult(True, "well-formed output validated cleanly against the task's own output_schema")

    return _check


def expect_classification(*, golden_truth_type: str, scripted_should_match: bool) -> CheckFn:
    """EXPECTED-CLASSIFICATION category (see module docstring's honesty
    note): proves the harness's OWN scoring can tell a scripted-correct
    answer from a scripted-incorrect one for the same golden document —
    not real model quality."""

    def _check(invocation: AIInvocation) -> CheckResult:
        if invocation.status != "SUCCEEDED":
            return CheckResult(False, f"expected SUCCEEDED, got {invocation.status}")
        actual_type = invocation.output.get("proposed_type") if invocation.output else None
        matched = actual_type == golden_truth_type
        if matched != scripted_should_match:
            return CheckResult(
                False,
                f"harness scoring disagreement: golden_truth={golden_truth_type!r}, "
                f"actual proposed_type={actual_type!r}, scripted_should_match={scripted_should_match} "
                f"but matched={matched}",
            )
        verdict = "CORRECT" if matched else "INCORRECT (deliberately scripted wrong, to prove the harness detects it)"
        return CheckResult(True, f"classification scoring verdict: {verdict} (proposed_type={actual_type!r})")

    return _check


def expect_abstention() -> CheckFn:
    """ABSTENTION/UNCERTAINTY category: a genuinely ambiguous document,
    scripted to return a low-confidence, honest "UNKNOWN" — must be
    recognised and reported as its own distinct outcome, not conflated
    with either a confident-correct or confident-incorrect answer."""

    def _check(invocation: AIInvocation) -> CheckResult:
        if invocation.status != "SUCCEEDED":
            return CheckResult(False, f"expected SUCCEEDED, got {invocation.status}")
        confidence = invocation.output.get("confidence") if invocation.output else None
        proposed = invocation.output.get("proposed_type") if invocation.output else None
        if confidence is None or confidence > ABSTENTION_CONFIDENCE_THRESHOLD:
            return CheckResult(
                False,
                f"expected a low-confidence abstention (<= {ABSTENTION_CONFIDENCE_THRESHOLD}), "
                f"got confidence={confidence}",
            )
        if proposed != "UNKNOWN":
            return CheckResult(False, f"expected an honest 'UNKNOWN' for an ambiguous document, got {proposed!r}")
        return CheckResult(
            True,
            f"recognised as ABSTENTION (confidence={confidence}, proposed_type=UNKNOWN) — "
            "reported distinctly from a confident wrong answer",
        )

    return _check


def expect_confident_incorrect(*, golden_truth_type: str) -> CheckFn:
    """The other half of the ABSTENTION/UNCERTAINTY proof: a
    HIGH-confidence WRONG answer must be recognised and reported as its
    own outcome class, distinct from an honest low-confidence
    abstention — proving the harness does not conflate 'wrong' with
    'uncertain'."""

    def _check(invocation: AIInvocation) -> CheckResult:
        if invocation.status != "SUCCEEDED":
            return CheckResult(False, f"expected SUCCEEDED, got {invocation.status}")
        confidence = invocation.output.get("confidence") if invocation.output else None
        proposed = invocation.output.get("proposed_type") if invocation.output else None
        if confidence is None or confidence <= ABSTENTION_CONFIDENCE_THRESHOLD:
            return CheckResult(
                False,
                f"expected a HIGH-confidence wrong answer (> {ABSTENTION_CONFIDENCE_THRESHOLD}) to contrast "
                f"with abstention, got confidence={confidence}",
            )
        if proposed == golden_truth_type:
            return CheckResult(False, f"fixture is meant to script a WRONG answer, but got the correct one ({proposed!r})")
        return CheckResult(
            True,
            f"recognised as CONFIDENT_INCORRECT (confidence={confidence}, proposed_type={proposed!r} != "
            f"golden truth {golden_truth_type!r}) — reported distinctly from ABSTENTION",
        )

    return _check


def expect_malformed_output(*, error_code: str) -> CheckFn:
    """MALFORMED-OUTPUT category: a response that is not valid JSON at
    all, or that is valid JSON but fails the task's output_schema, must
    be recorded as FAILED with the exact PID §76 error_code — never
    silently repaired into a fabricated success."""

    def _check(invocation: AIInvocation) -> CheckResult:
        if invocation.status != "FAILED":
            return CheckResult(False, f"expected FAILED, got {invocation.status}")
        if invocation.error_code != error_code:
            return CheckResult(False, f"expected error_code {error_code!r}, got {invocation.error_code!r}")
        if invocation.validation_result is None or invocation.validation_result.get("valid") is not False:
            return CheckResult(False, f"expected a recorded invalid validation_result, got {invocation.validation_result}")
        return CheckResult(True, f"malformed output correctly flagged FAILED/{error_code}, never silently repaired")

    return _check


def expect_transport_failure(*, error_code: str) -> CheckFn:
    """TIMEOUT/ERROR-HANDLING category: a scripted transport/timeout
    failure must be recorded as its OWN distinct outcome class — never
    conflated with a content-validation (malformed-output) failure.
    Checked structurally: a transport-layer failure never carries a
    ``validation_result`` at all (see ``ai/gateway/background.py``'s
    own outcome-3 code path), whereas every malformed-output failure
    above always does — this is what makes the two classes provably
    distinct rather than merely differently-worded.
    """

    def _check(invocation: AIInvocation) -> CheckResult:
        if invocation.status != "FAILED":
            return CheckResult(False, f"expected FAILED, got {invocation.status}")
        if invocation.error_code != error_code:
            return CheckResult(False, f"expected error_code {error_code!r}, got {invocation.error_code!r}")
        if invocation.validation_result is not None:
            return CheckResult(
                False,
                f"a transport/timeout failure must carry NO validation_result (it never reached content "
                f"validation at all); got {invocation.validation_result} — this would conflate it with a "
                "malformed-output failure",
            )
        return CheckResult(
            True,
            f"transport/timeout failure recorded as its own distinct outcome class ({error_code}), "
            "never conflated with a content-validation failure",
        )

    return _check


# ---------------------------------------------------------------------
# the golden fixture set
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class GoldenFixture:
    name: str
    category: str
    task_id: str
    task_version: int
    evidence_content: str
    scripted: object  # ScriptedSuccess | ScriptedFailure
    check: CheckFn
    notes: str = ""


CATEGORY_STRUCTURED_OUTPUT_VALIDITY = "STRUCTURED_OUTPUT_VALIDITY"
CATEGORY_EXPECTED_CLASSIFICATION = "EXPECTED_CLASSIFICATION"
CATEGORY_ABSTENTION_UNCERTAINTY = "ABSTENTION_UNCERTAINTY"
CATEGORY_MALFORMED_OUTPUT = "MALFORMED_OUTPUT"
CATEGORY_TIMEOUT_ERROR_HANDLING = "TIMEOUT_ERROR_HANDLING"

ALL_CATEGORIES = (
    CATEGORY_STRUCTURED_OUTPUT_VALIDITY,
    CATEGORY_EXPECTED_CLASSIFICATION,
    CATEGORY_ABSTENTION_UNCERTAINTY,
    CATEGORY_MALFORMED_OUTPUT,
    CATEGORY_TIMEOUT_ERROR_HANDLING,
)


GOLDEN_FIXTURES: tuple[GoldenFixture, ...] = (
    # ---- structured-output validity ----
    GoldenFixture(
        name="structured_output_valid_document_summary",
        category=CATEGORY_STRUCTURED_OUTPUT_VALIDITY,
        task_id="DOCUMENT_SUMMARY",
        task_version=1,
        evidence_content=SYNTHETIC_LONG_DOCUMENT_TEXT,
        scripted=json_content(
            {
                "summary": "A 12-month auto-renewing master services agreement between NoustAI Limited and a customer, governed by the law of England and Wales.",
                "confidence": 0.88,
                "signals": ["explicit 'MASTER SERVICES AGREEMENT' heading", "named term/renewal/governing-law clauses"],
                "warnings": [],
            }
        ),
        check=expect_valid_structured_output(),
        notes="A well-formed DOCUMENT_SUMMARY response validates cleanly against its output_schema.",
    ),
    # ---- expected classification (harness self-test, see module docstring) ----
    GoldenFixture(
        name="expected_classification_correct_invoice",
        category=CATEGORY_EXPECTED_CLASSIFICATION,
        task_id="DOCUMENT_TYPE_PROPOSAL",
        task_version=1,
        evidence_content=SYNTHETIC_INVOICE_TEXT,
        scripted=json_content(
            {
                "proposed_type": "INVOICE",
                "confidence": 0.95,
                "signals": ["invoice number present", "total due present", "payment terms present"],
                "warnings": [],
            }
        ),
        check=expect_classification(golden_truth_type="INVOICE", scripted_should_match=True),
        notes="Golden truth: this synthetic document is an invoice. Scripted answer agrees.",
    ),
    GoldenFixture(
        name="expected_classification_detects_a_wrong_answer",
        category=CATEGORY_EXPECTED_CLASSIFICATION,
        task_id="DOCUMENT_TYPE_PROPOSAL",
        task_version=1,
        evidence_content=SYNTHETIC_RECEIPT_TEXT,
        scripted=json_content(
            {
                "proposed_type": "CONTRACT",  # deliberately wrong — golden truth is RECEIPT
                "confidence": 0.6,
                "signals": ["(deliberately incorrect fixture — see fixtures.py docstring)"],
                "warnings": [],
            }
        ),
        check=expect_classification(golden_truth_type="RECEIPT", scripted_should_match=False),
        notes=(
            "Golden truth: this synthetic document is a receipt. The scripted answer is "
            "DELIBERATELY wrong (CONTRACT) — this fixture PASSES exactly when the harness "
            "correctly detects the mismatch, proving the scoring logic can fail a wrong answer, "
            "not merely rubber-stamp every SUCCEEDED invocation."
        ),
    ),
    # ---- abstention/uncertainty, contrasted with confident-incorrect ----
    GoldenFixture(
        name="abstention_on_genuinely_ambiguous_document",
        category=CATEGORY_ABSTENTION_UNCERTAINTY,
        task_id="DOCUMENT_TYPE_PROPOSAL",
        task_version=1,
        evidence_content=SYNTHETIC_AMBIGUOUS_TEXT,
        scripted=json_content(
            {
                "proposed_type": "UNKNOWN",
                "confidence": 0.1,
                "signals": [],
                "warnings": ["document contains no structural or textual markers of any known document type"],
            }
        ),
        check=expect_abstention(),
        notes="A genuinely ambiguous document scripted with an honest, low-confidence abstention.",
    ),
    GoldenFixture(
        name="confident_incorrect_answer_is_distinguished_from_abstention",
        category=CATEGORY_ABSTENTION_UNCERTAINTY,
        task_id="DOCUMENT_TYPE_PROPOSAL",
        task_version=1,
        evidence_content=SYNTHETIC_RECEIPT_TEXT,
        scripted=json_content(
            {
                "proposed_type": "STATEMENT",  # wrong — golden truth is RECEIPT — but HIGH confidence
                "confidence": 0.9,
                "signals": ["(deliberately incorrect, high-confidence fixture — contrast case for abstention)"],
                "warnings": [],
            }
        ),
        check=expect_confident_incorrect(golden_truth_type="RECEIPT"),
        notes=(
            "Same document class as the abstention fixture's sibling case, but scripted with a "
            "confident WRONG answer — proves the harness reports 'confidently wrong' as a distinct "
            "outcome class from 'honestly uncertain', never conflating the two."
        ),
    ),
    # ---- malformed output ----
    GoldenFixture(
        name="malformed_output_not_json_at_all",
        category=CATEGORY_MALFORMED_OUTPUT,
        task_id="DOCUMENT_TYPE_PROPOSAL",
        task_version=1,
        evidence_content=SYNTHETIC_INVOICE_TEXT,
        scripted=ScriptedSuccess(raw_text="Sure! This looks like an invoice to me."),
        check=expect_malformed_output(error_code="OUTPUT_NOT_JSON"),
        notes="The (fake) provider returns prose instead of JSON — must FAIL, never be silently coerced.",
    ),
    GoldenFixture(
        name="malformed_output_valid_json_fails_schema",
        category=CATEGORY_MALFORMED_OUTPUT,
        task_id="DOCUMENT_TYPE_PROPOSAL",
        task_version=1,
        evidence_content=SYNTHETIC_INVOICE_TEXT,
        # valid JSON, but missing the required "confidence"/"signals"/"warnings" keys.
        scripted=ScriptedSuccess(raw_text=json.dumps({"proposed_type": "INVOICE"})),
        check=expect_malformed_output(error_code="OUTPUT_SCHEMA_INVALID"),
        notes="Valid JSON, but missing required output_schema keys — must FAIL, never be silently repaired.",
    ),
    # ---- timeout/error handling ----
    GoldenFixture(
        name="transport_timeout_is_its_own_outcome_class",
        category=CATEGORY_TIMEOUT_ERROR_HANDLING,
        task_id="DOCUMENT_SUMMARY",
        task_version=1,
        evidence_content=SYNTHETIC_INVOICE_TEXT,
        scripted=ScriptedFailure(status=LiteLLMOutcomeStatus.TIMEOUT, error_detail="synthetic scripted timeout"),
        check=expect_transport_failure(error_code="LITELLM_TIMEOUT"),
        notes="A scripted provider timeout must be recorded distinctly from a content-validation failure.",
    ),
    GoldenFixture(
        name="transport_error_is_its_own_outcome_class",
        category=CATEGORY_TIMEOUT_ERROR_HANDLING,
        task_id="ENTITY_PROPOSAL",
        task_version=1,
        evidence_content=SYNTHETIC_INVOICE_TEXT,
        scripted=ScriptedFailure(status=LiteLLMOutcomeStatus.TRANSPORT_ERROR, error_detail="synthetic scripted connection refused"),
        check=expect_transport_failure(error_code="LITELLM_TRANSPORT_ERROR"),
        notes="A scripted connection failure must also be recorded distinctly, for a different task/alias.",
    ),
)
