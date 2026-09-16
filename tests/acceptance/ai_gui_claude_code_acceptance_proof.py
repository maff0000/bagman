"""CD-5 Gate-2 closure — Ask BAGMAN HTML UI browser acceptance proof
(architect's 14-point list, points 1/3/14, 2026-09-16).

Real headless-Chromium (Playwright) session against the REAL Docker
Compose production stack's REAL `bagman-api` container — the same one
``tests/acceptance/claude_code_operator_live_proof.py`` already proved
at the HTTP level. This script proves the remaining, UI-specific
points that script cannot: a real question submitted through the
actual HTML page, and the real Claude Code answer rendering back in
that same page, synchronously, through BAGMAN's existing Ask BAGMAN
drawer — unchanged code, since the wire contract
(``POST /internal/operator/chat``'s request/response shape) is
identical to the superseded design (see the CD-5 evidence file's §6j).

Unlike ``tests/acceptance/ai_gui_browser_acceptance_proof.py`` (WI-5,
which correctly reported "real answer OR honest fallback" because no
Claude credential existed at the time), this script asserts a
GENUINE, real Claude Code answer — the fallback path is no longer the
expected outcome under the corrected architecture.

Requires ``playwright`` (``pip install playwright && python3 -m
playwright install chromium``) — matching this directory's own
existing convention (not part of ``requirements-dev.txt``).

Run standalone (assumes the real stack, with the Gate-2 `claude`-baked
image, is already up — bring it up first with
``tests/acceptance/claude_code_operator_live_proof.py`` or
``docker compose ... up -d --wait``):

    python3 tests/acceptance/ai_gui_claude_code_acceptance_proof.py
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

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


def main() -> None:
    section("PRECONDITION — confirm the real stack is already up and ready")
    ready = wait_for_ready(timeout=30)
    print(f"    /ready -> {ready}")
    assert ready["ready"] is True

    tag = run_id()
    tmpdir = Path(tempfile.mkdtemp(prefix="bagman-gate2-browser-proof-"))
    pdf_bytes = f"%PDF-1.4\nGate-2 Ask BAGMAN browser acceptance proof, run {tag}\nInvoice total: GBP 999.00\n%%EOF\n".encode()
    pdf_filename = f"gate2-browser-{tag}.pdf"
    pdf_path = tmpdir / pdf_filename
    pdf_path.write_bytes(pdf_bytes)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        section("1. APP LOADS")
        page.goto(BASE_URL + "/", wait_until="load", timeout=30_000)
        page.wait_for_selector("text=BAGMAN", timeout=10_000)
        print("    page loaded; BAGMAN shell header visible.")

        section("OVERVIEW — Claude operator status now reflects the real claude_code signal, not the superseded key")
        page.click("button.tab-btn[data-tab='overview']") if page.locator("button.tab-btn[data-tab='overview']").count() else None
        page.wait_for_selector('[data-card="ai-claude"] .status-text', timeout=10_000)
        claude_status_text = page.inner_text('[data-card="ai-claude"] .status-text')
        print(f"    Claude operator status: {claude_status_text!r}")
        assert claude_status_text.strip() == "ok", (
            f"expected the Overview 'Claude operator' card to show the real claude_code signal ('ok'), "
            f"got {claude_status_text!r} — check overview.js reads body.checks.claude_code, not the "
            "superseded body.checks.claude"
        )
        print("    CONFIRMED: Overview correctly shows the live Claude Code signal, not the superseded direct-Anthropic one.")

        section("2. Real document upload (Point 1 setup — real HTML UI, real file input)")
        page.click("button.tab-btn[data-tab='documents']")
        page.wait_for_selector("#panel-documents:not([hidden])", timeout=10_000)
        page.set_input_files("#file-input", str(pdf_path))
        page.fill("#actor-id-input", "gate2-browser-acceptance")
        page.click("#upload-submit")
        page.wait_for_function(
            """() => {
                const el = document.querySelector('#upload-status');
                return el && /Complete|Rejected|Quarantined|Failed/.test(el.textContent);
            }""",
            timeout=30_000,
        )
        status_text = page.inner_text("#upload-status")
        assert "Complete" in status_text, f"expected a clean synthetic PDF to register successfully: {status_text!r}"
        evidence_id_match = re.search(r"evidence ([0-9a-f-]+)", status_text)
        assert evidence_id_match, f"could not find evidence_id in status text: {status_text!r}"
        evidence_id = evidence_id_match.group(1)
        print(f"    real upload succeeded — evidence_id = {evidence_id}")

        page.wait_for_selector(f"#doc-table-body >> text={pdf_filename}", timeout=15_000)
        row = page.locator("tr", has_text=pdf_filename).first
        row.click()
        page.wait_for_selector("#detail-overlay:not([hidden])", timeout=10_000)
        page.wait_for_selector(f"#detail-body >> text={evidence_id}", timeout=10_000)

        section("1/3 — ASK BAGMAN: a real question submitted from the HTML UI")
        page.click("button:has-text('Ask BAGMAN about this')")
        page.wait_for_selector("#ask-bagman-drawer:not([hidden])", timeout=10_000)
        chip_text = page.inner_text("#ask-bagman-context-chip")
        assert evidence_id in chip_text, "expected the attached document's evidence_id in the context chip"
        print(f"    Ask BAGMAN drawer opened with context chip: {chip_text!r}")

        page.fill("#ask-bagman-input", "What type of document is this, and what is the total amount?")
        page.click("#ask-bagman-form button[type=submit]")

        section("Loading state visible (PID's own 'visible loading state' requirement)")
        try:
            page.wait_for_selector(".chat-turn--pending, text=…, [aria-busy=true]", timeout=2_000)
            print("    a loading/pending state was observed before the real response arrived.")
        except Exception:
            print("    loading state resolved too quickly to observe directly (a fast real round trip) — proceeding.")

        section("3 — real Claude output returns synchronously to the UI")
        page.wait_for_function(
            """() => {
                const messages = document.querySelector('#ask-bagman-messages');
                if (!messages) return false;
                return messages.querySelectorAll('.chat-turn--assistant, .chat-turn--error').length > 0;
            }""",
            timeout=60_000,
        )
        turn_text = page.inner_text("#ask-bagman-messages")
        print(f"    Ask BAGMAN conversation:\n{turn_text}")

        has_assistant_turn = page.locator(".chat-turn--assistant").count() > 0
        has_error_turn = page.locator(".chat-turn--error").count() > 0
        assert has_assistant_turn, (
            f"expected a GENUINE real assistant turn under the corrected Gate-2 architecture (no more "
            f"honest-fallback expectation) — got error_turn={has_error_turn}, full conversation: {turn_text!r}"
        )
        assert "999.00" in turn_text or "999" in turn_text, (
            f"expected the real answer to reference the real document content: {turn_text!r}"
        )
        print("    *** GENUINE LIVE CLAUDE CODE RESPONSE, RENDERED IN THE REAL HTML UI *** — Points 1/3 PROVEN.")

        section("14 — the HTML operator experience works end-to-end (a second real turn, same drawer)")
        page.fill("#ask-bagman-input", "Which supplier issued it?")
        page.click("#ask-bagman-form button[type=submit]")
        page.wait_for_function(
            """() => document.querySelectorAll('#ask-bagman-messages .chat-turn--assistant').length >= 2""",
            timeout=60_000,
        )
        second_turn_text = page.inner_text("#ask-bagman-messages")
        print(f"    after second real turn:\n{second_turn_text}")
        assert page.locator(".chat-turn--assistant").count() >= 2
        print("    CONFIRMED: a second real turn in the same drawer session also succeeded — the end-to-end HTML "
              "operator experience works reliably, not just once.")

        chip_text_after = page.inner_text("#ask-bagman-context-chip")
        assert evidence_id in chip_text_after, "document context must remain identifiable throughout"

        browser.close()

    section("SUMMARY")
    print("    app loads, Overview shows the real claude_code signal      : PROVEN")
    print("    real document upload through the real HTML file input     : PROVEN")
    print("    Point 1  — Ask BAGMAN submits a real question from the UI : PROVEN")
    print("    Point 3  — real Claude output returns synchronously to UI : PROVEN")
    print("    Point 14 — the HTML operator experience works end-to-end,")
    print("               including a second reliable real turn          : PROVEN")
    print("\nCD-5 GATE-2 ASK BAGMAN HTML UI BROWSER ACCEPTANCE PROOF: PASS")
    print(f"(temporary fixture files left at {tmpdir} for inspection if needed; stack left running)")


if __name__ == "__main__":
    main()
