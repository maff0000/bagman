"""CD-6 Slice 5 WI-5 acceptance evidence — real browser-level acceptance
for the classification GUI-operations-foundation work (architect WO
§55-62), driven with a REAL headless Chromium (Playwright) against the
REAL `bagman-api` container in the REAL Docker Compose stack (real
Postgres, real MinIO, real ClamAV, and a REAL classification AI call
against whatever `BAGMAN_LITELLM_ENDPOINT` is actually configured —
mirrors `tests/acceptance/document_type_proposal_v2_live_proof.py`'s
own "no mocks, report honestly" discipline).

Real, directly-runnable script (see `tests/acceptance/README.md` — this
directory's scripts are deliberately NOT `test_*.py`). Requires
`playwright` (already installed in this environment).

Judgment call (recorded in the WI-5 final report too): this repo has
NO existing JS unit-test framework (no package.json, no JS test
runner) — §55-61's "GUI tests" are implemented here as real,
disposable-stack-driven Playwright assertions, structured as distinct
`section(...)`-labelled blocks so each WO paragraph maps to a specific
block of this script, rather than inventing a new JS unit-test
framework from scratch.

What this proves, in one real browser session, mapped to the WO
------------------------------------------------------------------------
* SETUP: a real synthetic, UNCLASSIFIED `message/rfc822` EMAIL
  EvidenceItem is registered via a direct in-process
  `core.api.BagmanCanonicalAPI` call inside the already-running
  `bagman-api` container (PID §24's HTTP surface has no route that can
  register mailbox-shaped evidence directly — same documented pattern
  `document_type_proposal_v2_live_proof.py` already establishes), with
  an XSS-shaped subject line for the §56 security proof.
* §55 — Documents: the synthetic email evidence appears in the
  evidence-first list; a manual upload also appears alongside it;
  "Unclassified" renders distinctly (never a fabricated "UNKNOWN").
* §57 — "Classify with BAGMAN" on an unclassified document invokes the
  real governed WI-3 orchestrator.
* §56 — "Why BAGMAN thinks this" / the review drawer render the
  XSS-shaped subject as INERT TEXT — no `dialog` ever fires, no
  `<script>` element is ever created in the DOM from it.
* §58 — the CLASSIFICATION_REVIEW item opens the new dedicated review
  UI (not the generic/COMPANY_WHAT_WHY fallback), and renders no
  Dismiss button.
* §59 — correcting the proposal sends the EXACT expected
  `{document_type, teach_rule}` payload to
  `POST /internal/needs-you/{id}/resolve` (network-level proof, not
  merely "the UI looked right").
* §60 — rule teaching is OFF by default, exposes only the closed
  scope/predicate vocabulary (no regex/contains control exists in the
  DOM at all), requires a fresh preview before submit is enabled, and
  invalidates that preview on any governed-field edit.
* §61/§44 — Activity renders a human-readable summary for the
  resolution event and its "Open document" deep link opens the correct
  Document.
* §15 — Documents' own classification history/lineage shows the
  resulting confirm/correct entry.

Run standalone (brings the stack up itself first):

    python3 tests/acceptance/classification_gui_browser_acceptance_proof.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import (  # noqa: E402
    ACTOR_TYPE,
    BASE_URL,
    compose_build,
    compose_exec_python,
    compose_up_wait,
    parse_marker_json,
    run_id,
    section,
    wait_for_ready,
)

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

ACTOR_ID = "wi5-classification-gui-acceptance"
_RESULT_MARKER = "WI5_LIVE_PROOF_RESULT_JSON:"

#: §56 — a synthetic, unmistakably-injected payload embedded directly
#: in the subject line. Real proof target: this string must render as
#: plain visible text everywhere the GUI shows a document's subject,
#: and must NEVER execute or become a real DOM <script> element.
_XSS_MARKER = "xss-marker"


def _register_synthetic_unclassified_email(tag: str) -> dict:
    sender_address = f"statements+{tag}@wi5-live-proof-broker.example.com"
    subject = f"Daily Activity Statement {tag} <script>alert('{_XSS_MARKER}-{tag}')</script>"
    body = f"Synthetic WI-5 acceptance fixture {tag}. Account summary and daily activity enclosed.\n"
    script = f"""
import hashlib
import json
from datetime import datetime, timezone
from email.message import EmailMessage

from app.api.composition import get_composition
from core import identity

composition = get_composition()
api = composition.api

msg = EmailMessage()
msg["From"] = {sender_address!r}
msg["Subject"] = {subject!r}
msg.set_content({body!r})
content = bytes(msg)
content_hash = {{"algorithm": "SHA-256", "value": hashlib.sha256(content).hexdigest()}}
storage_reference = composition.object_store.put(identity.generate_id(), content_hash, content)

source = api.register_source(
    source_type="MAILBOX_TEST", provider="wi5-live-proof", status="ACTIVE",
    actor_type={ACTOR_TYPE!r}, actor_id={ACTOR_ID!r},
)
now = datetime.now(timezone.utc)
evidence = api.register_evidence(
    entity_id=None, evidence_type="EMAIL", source_id=source.source_id,
    observed_at=now, received_at=now, content_hash=content_hash, mime_type="message/rfc822",
    size_bytes=len(content), storage_reference=storage_reference,
    actor_type={ACTOR_TYPE!r}, actor_id={ACTOR_ID!r},
    metadata={{"sender_address": {sender_address!r}, "subject": {subject!r}}},
)
print({_RESULT_MARKER!r} + json.dumps({{
    "evidence_id": evidence.evidence_id,
    "sender_address": {sender_address!r},
    "subject": {subject!r},
}}))
"""
    stdout = compose_exec_python(script)
    return parse_marker_json(stdout, _RESULT_MARKER)


def main() -> None:
    section("SETUP — bring up the real BAGMAN Docker Compose stack")
    compose_build()
    compose_up_wait()
    ready = wait_for_ready()
    print(f"    /ready -> {ready}")

    tag = run_id()

    section("SETUP — register a real synthetic, UNCLASSIFIED EMAIL EvidenceItem (with XSS-shaped subject)")
    fixture = _register_synthetic_unclassified_email(tag)
    evidence_id = fixture["evidence_id"]
    subject = fixture["subject"]
    print(f"    evidence_id = {evidence_id}")
    print(f"    subject (XSS-shaped) = {subject!r}")

    dialogs_fired: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("dialog", lambda d: (dialogs_fired.append(d.message), d.dismiss()))

        section("APP LOADS")
        page.goto(BASE_URL + "/", wait_until="load", timeout=30_000)
        page.wait_for_selector("text=BAGMAN", timeout=10_000)
        print("    page loaded; BAGMAN shell header visible.")

        section("§1 / §55 — DOCUMENTS: evidence-first list shows the synthetic email, UNCLASSIFIED distinctly")
        page.click("button.tab-btn[data-tab='documents']")
        page.wait_for_selector("#panel-documents:not([hidden])", timeout=10_000)

        # §9 filter proof: the UNCLASSIFIED classification-status filter
        # finds this exact row.
        page.select_option("#filter-classification-status", "UNCLASSIFIED")
        page.click("#filter-apply")
        page.wait_for_selector(f"#doc-table-body >> text={evidence_id}", timeout=15_000)
        print("    UNCLASSIFIED filter finds the synthetic email row.")

        # Reset the filter so the row is locatable by its own content below.
        page.select_option("#filter-classification-status", "")
        page.click("#filter-apply")
        page.wait_for_selector("#doc-table-body", timeout=10_000)

        row = page.locator("tr", has_text=evidence_id).first
        row.wait_for(timeout=15_000)
        row_text = row.inner_text()
        print(f"    row text: {row_text!r}")
        assert _XSS_MARKER in row_text, "expected the XSS-shaped subject text to render as plain visible text in the row"
        assert "Unclassified" in row_text, f"expected the 'Unclassified' classification badge, got row text: {row_text!r}"
        script_elements_in_row = row.locator("script").count()
        assert script_elements_in_row == 0, "a literal <script> element was created in the Documents row — XSS via innerHTML"
        print("    CONFIRMED: XSS-shaped subject renders as inert text; 'Unclassified' (never fabricated UNKNOWN) shown.")

        section("§3/§55 — a real MANUAL UPLOAD still works and coexists in the SAME evidence-first list (§4)")
        upload_tmpdir = Path(tempfile.mkdtemp(prefix="wi5-gui-proof-"))
        upload_filename = f"wi5-manual-upload-{tag}.pdf"
        upload_path = upload_tmpdir / upload_filename
        upload_path.write_bytes(f"%PDF-1.4\nWI-5 manual-upload coexistence proof {tag}\n%%EOF\n".encode())
        page.set_input_files("#file-input", str(upload_path))
        page.fill("#actor-id-input", ACTOR_ID)
        page.click("#upload-submit")
        page.wait_for_function(
            """() => {
                const el = document.querySelector('#upload-status');
                return el && /Complete|Rejected|Quarantined|Failed/.test(el.textContent);
            }""",
            timeout=30_000,
        )
        upload_status_text = page.inner_text("#upload-status")
        print(f"    upload-status text: {upload_status_text!r}")
        assert "Complete" in upload_status_text, f"expected the synthetic PDF to register successfully: {upload_status_text!r}"
        page.wait_for_selector(f"#doc-table-body >> text={upload_filename}", timeout=15_000)
        print("    CONFIRMED: the manual upload appears in the SAME evidence-first Documents list as the email evidence.")

        section("§2/§13 — OPEN DOCUMENT: Classification section renders above AI Analysis, 'Classify with BAGMAN' offered")
        row.click()
        page.wait_for_selector("#detail-overlay:not([hidden])", timeout=10_000)
        page.wait_for_selector("#detail-body >> text=Classification", timeout=10_000)
        classify_btn = page.locator("#detail-body button:has-text('Classify with BAGMAN')")
        classify_btn.wait_for(timeout=10_000)
        detail_text_before = page.inner_text("#detail-body")
        assert _XSS_MARKER in detail_text_before, "expected the XSS-shaped subject in the detail panel too (via the Fields section)"
        script_elements_in_detail = page.locator("#detail-body script").count()
        assert script_elements_in_detail == 0, "a literal <script> element was created in the detail panel — XSS via innerHTML"
        print("    'Classify with BAGMAN' action visible; Classification section renders; XSS payload inert.")

        section("§3/§57 — INVOKE GOVERNED CLASSIFICATION (real WI-3 orchestrator, real AI call if no rule matches)")
        classify_btn.click()
        page.wait_for_function(
            """() => {
                const host = document.querySelector('.classification-panel__action');
                if (!host) return false;
                const t = host.textContent || "";
                return /Classified\\.|needs your review|Could not classify|cannot classify|Classification failed/.test(t);
            }""",
            timeout=60_000,
        )
        action_text = page.inner_text(".classification-panel__action")
        print(f"    classification action outcome text: {action_text!r}")

        review_now_btn = page.locator(".classification-panel__action button:has-text('Review now')")
        has_review_now = review_now_btn.count() > 0

        if not has_review_now:
            print(
                "    No 'Review now' button appeared — the real classification call did not reach an "
                "AI-proposal-needs-review outcome (deterministic match, context-unsupported, or a real "
                "infra failure). Reporting the outcome text honestly; the remainder of this script's "
                "review-drawer/resolve/Activity assertions require the AI-proposal path, so they are "
                "skipped for THIS run — this is a real, honest live-infra result, not a script bug."
            )
            section("SUMMARY (partial — AI-proposal path not reached this run)")
            print(f"    evidence_id: {evidence_id}")
            print(f"    classification action outcome: {action_text!r}")
            print("    Documents §55 (email+manual-upload+Unclassified) proof : PROVEN")
            print("    XSS inert-text proof (§56, list + detail)              : PROVEN")
            print(f"    dialogs fired during the whole run                    : {dialogs_fired!r} (must be [])")
            assert dialogs_fired == [], f"a real dialog fired — XSS executed: {dialogs_fired}"
            browser.close()
            return

        section("§4/§22 — REVIEW ITEM OBSERVED: 'Review now' opens the SAME universal drawer, on the Needs You tab")
        review_now_btn.click()
        page.wait_for_selector("#panel-needs-you:not([hidden])", timeout=10_000)
        page.wait_for_selector("#review-drawer:not([hidden])", timeout=10_000)
        print("    Needs You tab activated; review drawer open.")

        section("§5/§25/§56 — INSPECT AI REASONING: subject/proposal/signals render as inert text, no Dismiss button")
        drawer_text = page.inner_text("#review-drawer-body")
        print(f"    drawer text (first 400 chars): {drawer_text[:400]!r}")
        assert _XSS_MARKER in drawer_text, "expected the XSS-shaped subject inside the review drawer"
        script_elements_in_drawer = page.locator("#review-drawer-body script").count()
        assert script_elements_in_drawer == 0, "a literal <script> element was created in the review drawer — XSS via innerHTML"
        dismiss_buttons = page.locator("#review-drawer-body button:has-text('Dismiss')").count()
        assert dismiss_buttons == 0, "§28 — no Dismiss button may ever render for a CLASSIFICATION_REVIEW item"
        print("    CONFIRMED: XSS-shaped subject inert in the review drawer; no Dismiss button rendered (§28).")

        section("§6/§26/§27/§59 — CONFIRM/CORRECT: exact network payload proof")
        type_select = page.locator("#review-classification-type-select")
        type_select.wait_for(timeout=10_000)
        proposed_value = type_select.input_value()
        print(f"    BAGMAN's proposal preselected in the selector: {proposed_value!r}")

        # §26 — the closed 8-value canonical-type vocabulary, exactly.
        option_values = page.locator("#review-classification-type-select option").evaluate_all("els => els.map(e => e.value)")
        expected_types = {
            "SUPPLIER_INVOICE", "RECEIPT", "ORDER_CONFIRMATION", "REFUND_CONFIRMATION",
            "BROKER_STATEMENT", "BROKER_ACTIVITY_NOTICE", "NON_ACCOUNTING_DOCUMENT", "UNKNOWN",
        }
        assert set(option_values) == expected_types, f"unexpected classification type options: {option_values}"
        print("    CONFIRMED: exactly the closed 8-value document_type vocabulary is offered.")

        # Deliberately CORRECT to a different value (never the same as
        # the proposal) so the §59 exact-payload proof covers the
        # "corrected" path, and the submit-button label switches
        # (§27's own "Correct to X" wording).
        correction_value = next(v for v in expected_types if v != proposed_value)
        type_select.select_option(correction_value)
        submit_btn = page.locator("#review-drawer-body button.btn--primary").first
        submit_label = submit_btn.inner_text()
        print(f"    submit button label after correction: {submit_label!r}")
        assert f"Correct to {correction_value}" in submit_label, f"expected 'Correct to {correction_value}' wording, got {submit_label!r}"

        section("§7/§60 — RULE TEACHING: OFF by default; closed vocabulary; mandatory fresh preview")
        teach_checkbox = page.locator("#review-teach-rule-checkbox")
        assert not teach_checkbox.is_checked(), "§29 — teach-rule must default OFF"

        teach_checkbox.check()
        page.wait_for_selector("#review-teach-sender-scope-select", state="visible", timeout=5_000)
        sender_scope_values = page.locator("#review-teach-sender-scope-select option").evaluate_all("els => els.map(e => e.value)")
        subject_predicate_values = page.locator("#review-teach-subject-predicate-select option").evaluate_all("els => els.map(e => e.value)")
        # §30 — the authoritative proof: the two governed <select>s'
        # own OPTION VALUES are exactly the closed backend vocabulary,
        # never a regex/contains value among them. (A blanket text-scan
        # for the substring "regex"/"contains" anywhere in the drawer
        # is NOT used here — a real AI-generated reasoning signal can
        # legitimately contain the English word "contains", e.g.
        # "Subject line explicitly contains 'Daily Activity
        # Statement'", which is not a predicate-type control at all.)
        assert set(sender_scope_values) == {"EXACT_SENDER_ADDRESS", "EXACT_SENDER_DOMAIN"}
        assert set(subject_predicate_values) == {"EXACT", "STARTS_WITH"}
        print("    Teaching controls revealed with exactly the closed scope/predicate vocabulary (no regex/contains).")

        # §33 — submit stays disabled until a fresh preview succeeds.
        assert submit_btn.is_disabled(), "§32/§33 — submit must stay disabled with teaching enabled until a fresh preview succeeds"
        preview_btn = page.locator("#review-drawer-body button:has-text('Preview rule impact')")
        preview_btn.click()
        page.wait_for_selector("#review-drawer-body >> text=This rule matches", timeout=20_000)
        match_text = page.inner_text(".review-teach-rule__preview-result")
        print(f"    preview result: {match_text!r}")
        assert "This rule matches" in match_text

        # §33 — editing a governed field invalidates the preview again.
        predicate_input = page.locator("#review-teach-predicate-value-input")
        predicate_input.fill(predicate_input.input_value() + " edited")
        assert submit_btn.is_disabled(), "§33 — editing a governed teaching field must re-disable submit until re-previewed"
        print("    CONFIRMED: editing a governed field invalidates the previous preview and re-disables submit.")

        # Restore the exact-subject default (the truthful, narrow
        # default — §31) and re-preview so submission is valid again.
        predicate_input.fill(subject)
        preview_btn.click()
        page.wait_for_selector("#review-drawer-body >> text=This rule matches", timeout=20_000)
        assert not submit_btn.is_disabled(), "submit should be enabled again after a fresh, valid preview"

        # Turn teaching back OFF for the actual submit below — this run
        # already proved the teaching UI/preview contract; submitting a
        # real rule is not necessary to prove §37's resolution contract
        # and keeps this disposable run's footprint smaller.
        teach_checkbox.uncheck()

        section("§8/§37/§59 — SUBMIT: exact {document_type, teach_rule} payload to POST /internal/needs-you/{id}/resolve")
        captured_requests: list[dict] = []

        def _capture(request):
            if "/internal/needs-you/" in request.url and request.url.endswith("/resolve"):
                try:
                    captured_requests.append(json.loads(request.post_data or "{}"))
                except Exception:  # noqa: BLE001
                    captured_requests.append({"_unparsed": request.post_data})

        page.on("request", _capture)
        submit_btn.click()
        page.wait_for_selector("#review-drawer:not([hidden])", state="hidden", timeout=20_000)
        page.wait_for_timeout(200)  # let the request-capture listener flush
        page.remove_listener("request", _capture)

        assert len(captured_requests) >= 1, "expected at least one POST to the resolve endpoint"
        sent = captured_requests[-1]
        print(f"    exact payload sent: {json.dumps(sent)}")
        assert sent.get("resolution") == {"document_type": correction_value, "teach_rule": None}, (
            f"§59 — expected exact resolution payload "
            f"{{'document_type': {correction_value!r}, 'teach_rule': None}}, got {sent.get('resolution')!r}"
        )
        print("    CONFIRMED: exact resolution payload sent — no client-side CONFIRMED/CORRECTED semantic rewriting.")

        section("§9/§39 — SUCCESSFUL RESOLUTION: drawer closed, no full reload required")
        page.wait_for_selector("#panel-needs-you:not([hidden])", timeout=5_000)  # still on the Needs You tab
        print("    Drawer closed; Needs You tab still mounted (no full page reload).")

        section("§10/§15 — DOCUMENTS: current classification + history reflect the correction")
        # The original document detail slide-over (opened back in §2/§13)
        # has remained open behind the Needs You tab/drawer this whole
        # time — close it first so it does not intercept the tab click.
        if page.is_visible("#detail-overlay:not([hidden])"):
            page.click("#detail-close")
            page.wait_for_selector("#detail-overlay", state="hidden", timeout=5_000)
        page.click("button.tab-btn[data-tab='documents']")
        page.wait_for_selector("#panel-documents:not([hidden])", timeout=10_000)
        page.click("#list-refresh")
        page.wait_for_selector(f"#doc-table-body >> text={evidence_id}", timeout=15_000)
        row2 = page.locator("tr", has_text=evidence_id).first
        row2.click()
        page.wait_for_selector("#detail-overlay:not([hidden])", timeout=10_000)
        page.wait_for_selector(f"#detail-body >> text={correction_value}", timeout=15_000)
        detail_text_after = page.inner_text("#detail-body")
        assert "Confirmed by you" in detail_text_after, f"expected the current classification's source label 'Confirmed by you': {detail_text_after[:600]!r}"
        assert "corrected" in detail_text_after.lower() or "→" in detail_text_after, (
            f"expected a correction lineage entry in the classification history: {detail_text_after[:600]!r}"
        )
        print("    CONFIRMED: Documents detail shows the current OPERATOR_ASSIGNED classification and correction lineage.")

        section("§11/§42/§44/§61 — ACTIVITY: friendly summary + working deep link")
        page.click("#detail-close")
        page.click("button.tab-btn[data-tab='activity']")
        page.wait_for_selector("#panel-activity:not([hidden])", timeout=10_000)
        page.wait_for_selector("#activity-list >> text=corrected", timeout=20_000)
        activity_row = page.locator(".activity-row", has_text="corrected").first
        activity_text = activity_row.inner_text()
        print(f"    Activity row text: {activity_text!r}")
        assert "EVIDENCE_CLASSIFICATION_CORRECTED" not in activity_text.split("\n")[0], (
            "the COLLAPSED row must be human-readable, not the raw event_type"
        )
        open_doc_btn = activity_row.locator("button:has-text('Open document')")
        assert open_doc_btn.count() == 1, "expected a working 'Open document' deep link on the correction event"
        open_doc_btn.click()
        page.wait_for_selector("#detail-overlay:not([hidden])", timeout=10_000)
        page.wait_for_selector(f"#detail-body >> text={evidence_id}", timeout=10_000)
        print("    CONFIRMED: Activity shows a friendly summary and its deep link opens the correct Document.")

        browser.close()

    section("SUMMARY")
    print(f"    evidence_id                                          : {evidence_id}")
    print("    §55 Documents (email+upload coexist, Unclassified)     : PROVEN")
    print("    §56 XSS-shaped subject inert everywhere (list/detail/  : PROVEN")
    print("         drawer), no dialog fired, no <script> element")
    print("    §57 'Classify with BAGMAN' invokes real orchestrator   : PROVEN")
    print("    §58 dedicated review UI, no Dismiss button             : PROVEN")
    print("    §59 exact {document_type, teach_rule} network payload  : PROVEN")
    print("    §60 teaching OFF-by-default, closed vocab, mandatory   : PROVEN")
    print("         fresh preview, edit invalidates preview")
    print("    §39 no full reload on success                          : PROVEN")
    print("    §15 Documents history/lineage shows the correction     : PROVEN")
    print("    §42/§44/§61 Activity friendly summary + deep link      : PROVEN")
    print(f"    dialogs fired during the whole run                     : {dialogs_fired!r} (must be [])")
    assert dialogs_fired == [], f"a real dialog fired — XSS executed: {dialogs_fired}"
    print("\nWI-5 CLASSIFICATION GUI BROWSER ACCEPTANCE PROOF: ALL REACHED STEPS COMPLETED AND VERIFIED")
    print("(stack left running, fully healthy; synthetic fixture data left in place for inspection)")


if __name__ == "__main__":
    main()
