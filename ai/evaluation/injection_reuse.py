"""Prompt-injection resilience, reused (not duplicated) from WI-2/WI-3's
own structural proofs (CD-5 PID §59-61/§85, WI-5).

The evaluation harness's own "prompt-injection resilience" category is
this module: it re-runs the exact, already-established, pytest-collected
proofs at ``tests/integration/test_prompt_injection_structural.py``
(WI-2's own message-construction proof) and
``tests/security/test_prompt_injection_ask_bagman.py`` (WI-3's own
Ask BAGMAN system-prompt/tool-authority proof), as a real subprocess
``pytest`` invocation, and folds their pass/fail outcome into this
harness's own report.

Why a subprocess, not a direct in-process function call
----------------------------------------------------------
``tests/security/test_prompt_injection_ask_bagman.py``'s tests depend
on a `@pytest.fixture` (`wired`) that pytest itself resolves — calling
the underlying test functions directly, in-process, without pytest's
own fixture machinery would require reaching into pytest's private
wrapper internals (fragile, and liable to break silently on a pytest
upgrade). Shelling out to the real `pytest` executable is simpler,
robust, and — importantly — is exactly how these two files are ALREADY
run by every other part of this delivery (`pytest tests/security
tests/contract tests/integration`), so this harness genuinely reuses
the existing proof rather than reimplementing a parallel version of it.
No live credentials are involved (PID §61) — both files are already
fully deterministic, fake-backed pytest suites.
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_REUSED_TEST_FILES = (
    "tests/integration/test_prompt_injection_structural.py",
    "tests/security/test_prompt_injection_ask_bagman.py",
)


@dataclass(frozen=True)
class InjectionReuseResult:
    passed: bool
    detail: str
    stdout_tail: str


def run_prompt_injection_suite(*, timeout_seconds: float = 60.0) -> InjectionReuseResult:
    """Run the two existing prompt-injection proof files via a real
    `pytest` subprocess and report pass/fail. Never raises for a test
    failure — that is reported as `passed=False`, exactly like every
    other fixture check in this harness; only a genuine inability to
    even invoke pytest (e.g. it is not installed) raises.
    """
    cmd = [sys.executable, "-m", "pytest", "-q", *_REUSED_TEST_FILES]
    try:
        result = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:  # pragma: no cover - environment misconfiguration
        raise RuntimeError(f"could not invoke pytest to reuse the prompt-injection suites: {exc}") from exc

    combined_output = (result.stdout or "") + (result.stderr or "")
    tail = "\n".join(combined_output.strip().splitlines()[-15:])

    if result.returncode == 0:
        return InjectionReuseResult(
            passed=True,
            detail=f"reused suites passed: {', '.join(_REUSED_TEST_FILES)}",
            stdout_tail=tail,
        )
    return InjectionReuseResult(
        passed=False,
        detail=f"reused suites FAILED (pytest exit code {result.returncode}): {', '.join(_REUSED_TEST_FILES)}",
        stdout_tail=tail,
    )
