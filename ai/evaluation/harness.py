"""The CD-5 evaluation harness itself (PID §59-61, WI-5).

Runs every golden fixture in ``ai/evaluation/fixtures.py`` through the
REAL ``ai.gateway.background.run_background_task`` orchestration
function (never a reimplementation of it) against a deterministic
``ai.providers.litellm.fake.FakeLiteLLMClient`` scripted per fixture,
plus one reused prompt-injection proof (``ai/evaluation/injection_reuse.py``),
and renders a clear, human-readable pass/fail report.

Scope, honestly stated (PID §59's own six required categories)
------------------------------------------------------------------
* **structured-output validity** — a well-formed response validates
  against the task's own `output_schema` (`ai.tasks.validate_task_output`,
  reused, never reimplemented).
* **expected classification** — proves the HARNESS's own scoring logic
  and the task contract's shape can distinguish a scripted-correct
  answer from a scripted-incorrect one for the same golden document.
  This is explicitly NOT a real-model-quality benchmark — no real
  model is reachable from this environment today (PID §14's Mac-mini
  gate; the LiteLLM-gateway database outage — see the WI-5 delivery
  report). See `ai/evaluation/fixtures.py`'s own module docstring.
* **abstention/uncertainty** — a genuinely ambiguous document scripted
  with an honest, low-confidence "UNKNOWN" answer, reported distinctly
  from a confident WRONG answer to the same class of document.
* **prompt-injection resilience** — reuses (never duplicates) WI-2's
  and WI-3's own established structural proofs.
* **malformed output** — a non-JSON response, and a JSON response that
  fails `output_schema`, are both recorded as FAILED, never silently
  repaired.
* **timeout/error handling** — a scripted transport/timeout failure is
  recorded as its own distinct outcome class, never conflated with a
  content-validation (malformed-output) failure.

Test determinism (PID §61)
----------------------------
Every fixture here runs against `FakeLiteLLMClient` — no live
credentials, no network access, no live LLM of any kind. Running this
harness twice produces identical results (deterministic fixtures,
deterministic scoring logic).

How to run
------------
As a plain script::

    python3 -m ai.evaluation.run

or, since this harness's own fixtures are plain, dependency-free
Python (no pytest fixtures required), it is also directly imported and
asserted on by the pytest-collected
`tests/integration/test_ai_evaluation_harness.py` — so `pytest tests/`
also exercises it on every ordinary run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ai.evaluation.fixtures import (
    ALL_CATEGORIES,
    GOLDEN_FIXTURES,
    GoldenFixture,
    ScriptedFailure,
    ScriptedSuccess,
)
from ai.evaluation.injection_reuse import InjectionReuseResult, run_prompt_injection_suite
from ai.gateway.background import run_background_task
from ai.invocation import InMemoryAIInvocationRepository
from ai.providers.litellm.fake import FakeLiteLLMClient
from ai.tasks import get_task_contract
from core.api import BagmanCanonicalAPI

ACTOR_TYPE = "SYSTEM"
ACTOR_ID = "ai-evaluation-harness"


@dataclass(frozen=True)
class FixtureResult:
    name: str
    category: str
    passed: bool
    detail: str
    notes: str = ""


@dataclass(frozen=True)
class EvaluationReport:
    fixture_results: tuple[FixtureResult, ...]
    injection_result: InjectionReuseResult

    @property
    def all_passed(self) -> bool:
        return self.injection_result.passed and all(r.passed for r in self.fixture_results)

    @property
    def counts_by_category(self) -> dict[str, tuple[int, int]]:
        """category -> (passed, total)."""
        counts: dict[str, list[int]] = {c: [0, 0] for c in ALL_CATEGORIES}
        for result in self.fixture_results:
            bucket = counts.setdefault(result.category, [0, 0])
            bucket[1] += 1
            if result.passed:
                bucket[0] += 1
        return {k: (v[0], v[1]) for k, v in counts.items()}

    def render_text(self) -> str:
        lines: list[str] = []
        lines.append("=" * 78)
        lines.append("CD-5 AI EVALUATION HARNESS REPORT (PID §59-61)")
        lines.append("=" * 78)
        for category in ALL_CATEGORIES:
            passed, total = self.counts_by_category.get(category, (0, 0))
            lines.append(f"\n[{category}] {passed}/{total} passed")
            for result in self.fixture_results:
                if result.category != category:
                    continue
                mark = "PASS" if result.passed else "FAIL"
                lines.append(f"  [{mark}] {result.name}: {result.detail}")

        lines.append(f"\n[PROMPT_INJECTION_RESILIENCE (reused WI-2/WI-3 suites)]")
        mark = "PASS" if self.injection_result.passed else "FAIL"
        lines.append(f"  [{mark}] {self.injection_result.detail}")
        if not self.injection_result.passed:
            lines.append("  --- reused suite output (tail) ---")
            for line in self.injection_result.stdout_tail.splitlines():
                lines.append(f"  {line}")

        total_fixtures = len(self.fixture_results)
        total_passed = sum(1 for r in self.fixture_results if r.passed)
        lines.append("\n" + "-" * 78)
        lines.append(
            f"TOTAL: {total_passed}/{total_fixtures} golden fixtures passed; "
            f"prompt-injection reuse suite {'PASSED' if self.injection_result.passed else 'FAILED'}"
        )
        lines.append(f"OVERALL: {'GREEN' if self.all_passed else 'RED'}")
        lines.append("-" * 78)
        return "\n".join(lines)


def _run_fixture(fixture: GoldenFixture) -> FixtureResult:
    repository = InMemoryAIInvocationRepository()
    litellm = FakeLiteLLMClient()
    api = BagmanCanonicalAPI()

    contract = get_task_contract(fixture.task_id, fixture.task_version)
    capability_alias = contract.preferred_capability

    if isinstance(fixture.scripted, ScriptedSuccess):
        litellm.queue_success(capability_alias=capability_alias, content=fixture.scripted.raw_text)
    elif isinstance(fixture.scripted, ScriptedFailure):
        litellm.queue_failure(
            capability_alias=capability_alias,
            status=fixture.scripted.status,
            error_detail=fixture.scripted.error_detail,
        )
    else:  # pragma: no cover - defensive; fixtures.py only builds the two types above
        return FixtureResult(
            name=fixture.name,
            category=fixture.category,
            passed=False,
            detail=f"unrecognised scripted response type: {type(fixture.scripted)!r}",
            notes=fixture.notes,
        )

    invocation = run_background_task(
        task_id=fixture.task_id,
        task_version=fixture.task_version,
        input_references={"evidence_id": f"eval-fixture-{fixture.name}"},
        evidence_content=fixture.evidence_content,
        actor_type=ACTOR_TYPE,
        actor_id=ACTOR_ID,
        correlation_id=None,
        repository=repository,
        litellm_client=litellm,
        record_audit_event=api.record_audit_event,
    )

    check_result = fixture.check(invocation)
    return FixtureResult(
        name=fixture.name,
        category=fixture.category,
        passed=check_result.passed,
        detail=check_result.detail,
        notes=fixture.notes,
    )


def run_all(*, fixtures: Optional[tuple[GoldenFixture, ...]] = None) -> EvaluationReport:
    """Run every golden fixture (or a caller-supplied subset — used by
    the pytest wrapper to test the harness's own machinery in
    isolation) plus the reused prompt-injection suite, and return a
    complete :class:`EvaluationReport`. Never raises for a fixture's own
    check failing — that is reported as data, exactly like the
    invocations it inspects (PID §76's own doctrine, mirrored here at
    the evaluation layer: a failing case is a REPORTED outcome, not an
    exception)."""
    selected = fixtures if fixtures is not None else GOLDEN_FIXTURES
    fixture_results = tuple(_run_fixture(f) for f in selected)
    injection_result = run_prompt_injection_suite()
    return EvaluationReport(fixture_results=fixture_results, injection_result=injection_result)
