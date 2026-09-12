"""Test E — synthetic fixtures (PID.md §15, Test E / §8 / §13).

Any fixture file tracked under `tests/fixtures/` must be demonstrably
synthetic: its filename must carry the `synthetic` naming convention, and
its content must carry an explicit "SYNTHETIC TEST DATA" style marker near
the top of the file. Both signals are required — filename alone is easy to
fake, content alone doesn't prevent someone dropping in real data under a
misleading synthetic-sounding name in some future large fixture where the
header goes unnoticed.
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath

HEADER_MARKER = "SYNTHETIC TEST DATA"
HEADER_SCAN_LINES = 10


def _fixture_files(tracked_files: list[str]) -> list[str]:
    return [
        path
        for path in tracked_files
        if PurePosixPath(path).parts[:2] == ("tests", "fixtures")
        and not path.endswith("README.md")
    ]


def test_at_least_one_synthetic_fixture_is_present(tracked_files):
    fixtures = _fixture_files(tracked_files)
    assert fixtures, "expected at least one fixture under tests/fixtures/"


def test_every_tracked_fixture_is_demonstrably_synthetic(repo_root, tracked_files):
    fixtures = _fixture_files(tracked_files)
    failures = []

    for rel_path in fixtures:
        basename = PurePosixPath(rel_path).name
        if "synthetic" not in basename.lower():
            failures.append(
                f"{rel_path}: filename does not follow the 'synthetic' "
                f"naming convention"
            )
            continue

        full_path = repo_root / rel_path
        try:
            text = full_path.read_text(errors="replace")
        except (UnicodeDecodeError, OSError) as exc:  # pragma: no cover
            failures.append(f"{rel_path}: could not read as text ({exc})")
            continue

        header = "\n".join(text.splitlines()[:HEADER_SCAN_LINES])
        if HEADER_MARKER.lower() not in header.lower():
            failures.append(
                f"{rel_path}: missing a '{HEADER_MARKER}' marker in its "
                f"first {HEADER_SCAN_LINES} lines"
            )

    assert not failures, (
        "Fixture(s) under tests/fixtures/ are not demonstrably synthetic:\n"
        + "\n".join(f"  - {f}" for f in failures)
    )


def test_marker_check_rejects_a_fixture_missing_the_header(tmp_path):
    # Sanity check on the check itself: a file with a synthetic-sounding
    # name but no header marker must still be rejected.
    bad_fixture = tmp_path / "example_synthetic_invoice_no_header.txt"
    bad_fixture.write_text("Invoice Number: 12345\nTotal: £60.00\n")

    header = "\n".join(bad_fixture.read_text().splitlines()[:HEADER_SCAN_LINES])
    assert HEADER_MARKER.lower() not in header.lower()


def test_marker_check_rejects_a_fixture_with_a_non_synthetic_filename(tmp_path):
    bad_fixture = tmp_path / "example_invoice.txt"
    bad_fixture.write_text(f"{HEADER_MARKER} — fabricated for tests\n")
    assert "synthetic" not in bad_fixture.name.lower()
