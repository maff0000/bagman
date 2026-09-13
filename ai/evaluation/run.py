"""CLI entry point for the CD-5 evaluation harness (PID §59-61, WI-5).

Usage::

    python3 -m ai.evaluation.run

Prints a human-readable report and exits ``0`` if every golden fixture
and the reused prompt-injection suite passed, ``1`` otherwise (a normal
CI/operator-facing pass/fail contract) — no live credentials required
(PID §61).
"""
from __future__ import annotations

import sys

from ai.evaluation.harness import run_all


def main() -> int:
    report = run_all()
    print(report.render_text())
    return 0 if report.all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
