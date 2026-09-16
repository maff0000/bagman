"""CD-4 WI-5 acceptance evidence — real browser-level acceptance (PID
§65), driven with a REAL headless Chromium (Playwright) against the REAL
`bagman-api` container in the REAL Docker Compose production stack (real
Postgres, real MinIO, real ClamAV) — NOT the dev-mode/uvicorn instance
WI-4 used for its own initial Playwright proof.

Real, directly-runnable script (see ``tests/acceptance/README.md`` — this
directory's scripts are deliberately NOT ``test_*.py``). Requires
``playwright`` (``pip install playwright && python3 -m playwright install
chromium``) — not part of ``requirements-dev.txt`` (a one-off tool for
this proof, not something the ordinary pytest suite needs; see the CD-4
WI-5 evidence file for why it is not added there).

What this drives, in one real browser session (PID §65 / §66 steps
3-13/20-21)
------------------------------------------------------------------------
1. Navigate to ``http://127.0.0.1:8000/`` — the app loads.
2. Click the "Documents" tab — the Documents screen renders.
3. Upload a real, valid synthetic PDF through the REAL file input.
4. Observe the real workflow status text progress to completion
   (whatever BAGMAN's own JS renders — no fabricated steps, PID §38).
5. Confirm the new evidence appears in the real list (by its unique
   filename).
6. Click the row to open the real detail panel; confirm it shows the
   real intake/evidence identifiers.
7. Download the evidence content via the real download link's `href`
   and prove the downloaded bytes are byte-identical to the original
   upload.
8. Upload the industry-standard EICAR test string (never real malware)
   through the same real file input; confirm the GUI renders a
   distinct, correct QUARANTINED status (not a false "success").
9. Upload a synthetic ZIP-magic-bytes "archive" fixture; confirm the
   GUI renders a distinct, correct REJECTED status.

Run standalone (assumes the real stack is already up — the other
acceptance scripts in this directory bring it up; this one does NOT
touch Docker Compose itself, only the browser + the already-published
``127.0.0.1:8000`` port):

    python3 tests/acceptance/browser_acceptance_proof.py
"""
from __future__ import annotations

import re
import sys
import tempfile
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _lib  # noqa: E402
from _lib import BASE_URL, run_id, section, wait_for_ready  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError as exc:  # pragma: no cover
    print(
        "playwright is not installed in this environment. Install with:\n"
        "  pip install playwright && python3 -m playwright install chromium\n"
        f"(ImportError: {exc})",
        file=sys.stderr,
    )
    raise SystemExit(1)

EICAR_TEST_STRING = rb"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


def main() -> None:
    section("PRECONDITION — confirm the real stack is already up and ready")
    ready = wait_for_ready(timeout=30)
    print(f"    /ready -> {ready}")
    assert ready["ready"] is True

    tag = run_id()
    tmpdir = Path(tempfile.mkdtemp(prefix="bagman-browser-proof-"))

    pdf_bytes = (
        f"%PDF-1.4\nWI-5 real-browser acceptance proof, run {tag}\n%%EOF\n".encode()
    )
    pdf_filename = f"wi5-browser-{tag}.pdf"
    pdf_path = tmpdir / pdf_filename
    pdf_path.write_bytes(pdf_bytes)

    eicar_filename = f"wi5-browser-eicar-{tag}.txt"
    eicar_path = tmpdir / eicar_filename
    eicar_path.write_bytes(EICAR_TEST_STRING)

    zip_filename = f"wi5-browser-archive-{tag}.zip"
    zip_path = tmpdir / zip_filename
    zip_path.write_bytes(b"PK\x03\x04synthetic-zip-local-file-header-not-a-real-archive" * 4)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        section("1. APP LOADS")
        page.goto(BASE_URL + "/", wait_until="load", timeout=30_000)
        page.wait_for_selector("text=BAGMAN", timeout=10_000)
        # CD-6 Slice 1 (PID §98) renamed the shell subtitle from "Evidence
        # Intake" to "Operations" — the GUI is no longer scoped to intake
        # alone (Needs You/Activity tabs, global + Add). A deliberate text
        # change, not a regression; updated here to match.
        page.wait_for_selector(".shell-header__sub:has-text('Operations')", timeout=10_000)
        print("    page loaded; BAGMAN shell header visible.")

        section("2. DOCUMENTS SCREEN RENDERS")
        page.click("button.tab-btn[data-tab='documents']")
        page.wait_for_selector("#panel-documents:not([hidden])", timeout=10_000)
        page.wait_for_selector("#upload-form", timeout=10_000)
        print("    Documents panel visible; upload form present.")

        section("3-4. REAL UPLOAD (valid synthetic PDF) + WORKFLOW STATUS PROGRESSES")
        page.set_input_files("#file-input", str(pdf_path))
        page.fill("#actor-id-input", "wi5-browser-acceptance")
        page.fill("#note-input", f"WI-5 browser acceptance proof {tag}")
        assert not page.is_disabled("#upload-submit"), "upload button should be enabled once a file is chosen"
        page.click("#upload-submit")

        page.wait_for_function(
            """() => {
                const el = document.querySelector('#upload-status');
                return el && /Complete|Rejected|Quarantined|Failed/.test(el.textContent);
            }""",
            timeout=30_000,
        )
        status_text = page.inner_text("#upload-status")
        print(f"    final upload-status text: {status_text!r}")
        assert "Complete" in status_text, f"expected a clean synthetic PDF to register successfully: {status_text!r}"
        evidence_id_match = re.search(r"evidence ([0-9a-f-]+)", status_text)
        assert evidence_id_match, f"could not find evidence_id in status text: {status_text!r}"
        evidence_id = evidence_id_match.group(1)
        print(f"    evidence_id = {evidence_id}")

        section("5. EVIDENCE APPEARS IN THE REAL LIST")
        page.wait_for_selector(f"#doc-table-body >> text={pdf_filename}", timeout=15_000)
        print(f"    row for {pdf_filename!r} visible in the Documents list.")

        section("6. DETAIL VIEW OPENS WITH REAL DATA")
        row = page.locator("tr", has_text=pdf_filename).first
        row.click()
        page.wait_for_selector("#detail-overlay:not([hidden])", timeout=10_000)
        detail_title = page.inner_text("#detail-title")
        print(f"    detail panel title: {detail_title!r}")
        # Detail.open() is async (it awaits GET /internal/evidence/{id}
        # + GET /internal/provenance/... before rendering the "Fields"
        # section) — the overlay itself becomes visible synchronously,
        # BEFORE that data has arrived, so wait for the real
        # evidence_id text to actually appear rather than racing it.
        page.wait_for_selector(f"#detail-body >> text={evidence_id}", timeout=10_000)
        detail_body_text = page.inner_text("#detail-body")
        assert evidence_id in detail_body_text, (
            f"expected evidence_id {evidence_id} to appear in the detail panel body: {detail_body_text[:500]!r}"
        )
        print("    detail panel shows the real evidence_id.")

        section("7. DOWNLOAD IS BYTE-IDENTICAL")
        download_href = page.get_attribute("#detail-body a:has-text('Download')", "href")
        if download_href is None:
            # fall back to the list row's own download link
            download_href = row.locator("a:has-text('Download')").get_attribute("href")
        assert download_href, "no Download link found in either the detail panel or the list row"
        download_url = BASE_URL + download_href if download_href.startswith("/") else download_href
        print(f"    downloading via real browser-rendered href: {download_url}")
        downloaded = requests.get(download_url, timeout=15)
        downloaded.raise_for_status()
        assert downloaded.content == pdf_bytes, "downloaded bytes are NOT byte-identical to the original upload"
        print(f"    downloaded {len(downloaded.content)} bytes — BYTE-IDENTICAL to the original upload.")

        page.click("#detail-close")
        page.wait_for_selector("#detail-overlay", state="hidden", timeout=5_000)

        section("8. QUARANTINED FILE DISPLAYS CORRECTLY (real EICAR, real ClamAV)")
        page.set_input_files("#file-input", str(eicar_path))
        page.fill("#actor-id-input", "wi5-browser-acceptance")
        page.click("#upload-submit")
        page.wait_for_function(
            """() => {
                const el = document.querySelector('#upload-status');
                return el && /Complete|Rejected|Quarantined|Failed/.test(el.textContent);
            }""",
            timeout=30_000,
        )
        quarantine_status_text = page.inner_text("#upload-status")
        print(f"    final upload-status text: {quarantine_status_text!r}")
        assert "Quarantined" in quarantine_status_text, (
            f"expected the real ClamAV daemon to quarantine EICAR: {quarantine_status_text!r}"
        )
        page.wait_for_selector(f"#doc-table-body >> text={eicar_filename}", timeout=15_000)
        eicar_row = page.locator("tr", has_text=eicar_filename).first
        badge_class = eicar_row.locator(".badge").get_attribute("class")
        print(f"    EICAR row badge class: {badge_class!r}")
        assert "badge--warn" in badge_class, f"expected the QUARANTINED badge styling, got {badge_class!r}"
        print("    QUARANTINED file renders distinctly (warn badge) in the real list, not as a false success.")

        section("9. REJECTED FILE DISPLAYS CORRECTLY (synthetic archive)")
        page.set_input_files("#file-input", str(zip_path))
        page.fill("#actor-id-input", "wi5-browser-acceptance")
        page.click("#upload-submit")
        page.wait_for_function(
            """() => {
                const el = document.querySelector('#upload-status');
                return el && /Complete|Rejected|Quarantined|Failed/.test(el.textContent);
            }""",
            timeout=30_000,
        )
        rejected_status_text = page.inner_text("#upload-status")
        print(f"    final upload-status text: {rejected_status_text!r}")
        assert "Rejected" in rejected_status_text, f"expected the synthetic archive to be rejected: {rejected_status_text!r}"
        page.wait_for_selector(f"#doc-table-body >> text={zip_filename}", timeout=15_000)
        zip_row = page.locator("tr", has_text=zip_filename).first
        zip_badge_class = zip_row.locator(".badge").get_attribute("class")
        print(f"    archive row badge class: {zip_badge_class!r}")
        assert "badge--bad" in zip_badge_class, f"expected the REJECTED badge styling, got {zip_badge_class!r}"
        print("    REJECTED file renders distinctly (bad badge) in the real list, not as a false success.")

        browser.close()

    section("SUMMARY")
    print("    app loads                              : PROVEN")
    print("    Documents screen renders                : PROVEN")
    print("    real upload via real file input         : PROVEN")
    print("    workflow status progresses/completes     : PROVEN")
    print("    evidence appears in real list            : PROVEN")
    print("    detail view opens with real data         : PROVEN")
    print("    download byte-identical                 : PROVEN")
    print("    quarantined file displays correctly      : PROVEN (real ClamAV)")
    print("    rejected file displays correctly         : PROVEN")
    print("\nPID §65/§66 BROWSER ACCEPTANCE PROOF: ALL STEPS COMPLETED AND VERIFIED")
    print(f"(temporary fixture files left at {tmpdir} for inspection if needed)")


if __name__ == "__main__":
    main()
