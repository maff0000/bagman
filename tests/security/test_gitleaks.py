"""Test D — Gitleaks (PID.md §15, Test D / §10).

Verifies the repository passes a `gitleaks detect` scan, invoked as a
subprocess against the repo root this test suite is running in. This is
the same tool and invocation shape documented in `README.md` /
`PID.md` §10 (`gitleaks detect --source . --verbose`), run non-interactively
with JSON reporting so failures are easy to read from CI logs.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest


def _run_gitleaks(repo_root, report_path):
    return subprocess.run(
        [
            "gitleaks",
            "detect",
            "--source",
            str(repo_root),
            "--redact",  # keep findings readable in logs; redact secret values
            "--report-format",
            "json",
            "--report-path",
            str(report_path),
            "--exit-code",
            "1",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(
    shutil.which("gitleaks") is None,
    reason="gitleaks binary not installed in this environment",
)
def test_gitleaks_detects_no_leaks(repo_root, tmp_path):
    report_path = tmp_path / "gitleaks-report.json"
    result = _run_gitleaks(repo_root, report_path)

    findings = []
    if report_path.exists() and report_path.stat().st_size > 0:
        try:
            findings = json.loads(report_path.read_text())
        except json.JSONDecodeError:
            findings = []

    if result.returncode != 0:
        details = "\n".join(
            f"  - {f.get('RuleID')}: {f.get('File')}:{f.get('StartLine')}" for f in findings
        ) or result.stdout or result.stderr
        pytest.fail(
            f"gitleaks detected potential secret(s) (exit code "
            f"{result.returncode}):\n{details}"
        )

    assert result.returncode == 0
    assert findings == []
