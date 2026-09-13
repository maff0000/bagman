"""Pytest-collected wrapper around ``ai.evaluation`` (CD-5 PID §59-61,
WI-5) — so `pytest tests/` alone already exercises the mandatory
evaluation harness on every ordinary run, in addition to its own
`python3 -m ai.evaluation.run` standalone invocation (see
``ai/evaluation/harness.py``'s own module docstring).

Deliberately does not re-derive `ai.evaluation`'s own scoring logic —
that would just duplicate `harness.py`. This module's job is narrower:
prove the harness runs cleanly end-to-end, prove it covers every PID
§59 category with at least one fixture, and prove a genuinely
"currently broken" harness (a fixture that fails, or a check function
with a bug) is NOT silently reported as green — i.e. that
`EvaluationReport.all_passed` really does turn `False` when something
is wrong, not just when everything happens to be right.
"""
from __future__ import annotations

from ai.evaluation.fixtures import (
    ALL_CATEGORIES,
    GOLDEN_FIXTURES,
    CheckResult,
    GoldenFixture,
    ScriptedSuccess,
)
from ai.evaluation.harness import run_all


def test_the_full_harness_runs_cleanly_and_is_fully_green():
    report = run_all()
    failing = [r for r in report.fixture_results if not r.passed]
    assert failing == [], f"evaluation harness reported failing fixtures: {failing}"
    assert report.injection_result.passed, report.injection_result.stdout_tail
    assert report.all_passed is True
    # A visible, human-readable report is part of this harness's own
    # contract (PID §59's "producing a clear pass/fail report").
    rendered = report.render_text()
    assert "OVERALL: GREEN" in rendered


def test_every_pid_59_category_has_at_least_one_golden_fixture():
    categories_present = {f.category for f in GOLDEN_FIXTURES}
    assert set(ALL_CATEGORIES) <= categories_present, (
        f"expected every PID §59 category to have at least one fixture; missing: "
        f"{set(ALL_CATEGORIES) - categories_present}"
    )
    # Belt-and-braces: every fixture must declare a category from the
    # closed set, never a stray/typo'd string that would silently never
    # be counted anywhere.
    for fixture in GOLDEN_FIXTURES:
        assert fixture.category in ALL_CATEGORIES, f"{fixture.name} has an unrecognised category {fixture.category!r}"


def test_a_genuinely_wrong_fixture_is_correctly_reported_as_failing():
    """Proves the harness's pass/fail machinery itself is not vacuous —
    a fixture whose `check` function is bound to disagree with reality
    is reported as FAILED, and `EvaluationReport.all_passed` becomes
    `False`. This is the harness-level equivalent of the CD-4 Auditor's
    own "an assert-less test always passes" lesson: never trust a
    green report that was never actually capable of turning red.
    """

    def _always_wrong(_invocation) -> CheckResult:
        return CheckResult(False, "deliberately-failing check, for this test only")

    broken_fixture = GoldenFixture(
        name="deliberately_broken_fixture_for_harness_self_test",
        category="STRUCTURED_OUTPUT_VALIDITY",
        task_id="DOCUMENT_TYPE_PROPOSAL",
        task_version=1,
        evidence_content="irrelevant",
        scripted=ScriptedSuccess(raw_text='{"proposed_type": "INVOICE", "confidence": 0.9, "signals": [], "warnings": []}'),
        check=_always_wrong,
    )
    report = run_all(fixtures=(broken_fixture,))
    assert report.fixture_results[0].passed is False
    assert report.all_passed is False
