// features/needs-you/classification-review.js — the CLASSIFICATION_REVIEW
// branch of the universal Needs You review drawer (CD-6 Slice 5 WI-5
// §24-40). Mirrors features/mailbox/domain-review.js's own "separate
// file, imported into needs-you.js, exports a render function" shape
// exactly (WI-5's own grounding instruction) — this is NOT a second
// review modal/queue (§24): it renders into the SAME drawer body
// `features/needs-you/needs-you.js::_renderReviewBody` already opened.
//
// No client-side classification/rule-evaluation logic (§51): every
// decision (match count, conflict, final CONFIRMED/CORRECTED semantics)
// comes from a real backend call. No `innerHTML` anywhere — every
// evidence/model-derived string (subject, sender, signals, warnings,
// representative subjects) renders via shared/dom.js's `el({text})`
// (§52).
import { el, clear } from "../../shared/dom.js";
import { errorMessage } from "../../shared/api.js";
import { loadingState } from "../../shared/state.js";
import { getActorId } from "../../shared/operator.js";
import * as notify from "../../shared/notify.js";
import * as drawer from "../../shell/drawer.js";
import { getInvocation } from "../ai/ai-api.js";
import { previewClassificationRule } from "../documents/classification-api.js";
import { classificationBadge } from "../documents/classification-format.js";
import { listEntities } from "../../shell/entities.js";
import { API, apiGet } from "../../shared/api.js";
import { resolveNeedsYouItem } from "./needs-you-api.js";
// Circular-import note: needs-you.js imports `ClassificationReview`
// from this file, and this file imports `NeedsYou` back from
// needs-you.js (to refresh the list after a successful resolve, §39,
// the SAME thing needs-you.js's own `_renderCompanyWhatWhyForm` already
// does for itself). ES module circular imports are well-defined as
// long as the cycle is not dereferenced during either module's own
// top-level evaluation — `NeedsYou` here is only ever touched inside
// the async submit handler below, well after both modules have fully
// loaded, so this is safe.
import { NeedsYou } from "./needs-you.js";

//: WI-5 §26 — the exact closed canonical-type selector vocabulary
//: (services.evidence.classification.DOCUMENT_TYPES — mirrored here as
//: a plain literal, the same "compare against the real string value"
//: convention every other GUI module in this codebase already uses for
//: a closed backend vocabulary, e.g. needs-you.js's own
//: `allowed_action_type` comparisons).
const DOCUMENT_TYPES = [
  "SUPPLIER_INVOICE",
  "RECEIPT",
  "ORDER_CONFIRMATION",
  "REFUND_CONFIRMATION",
  "BROKER_STATEMENT",
  "BROKER_ACTIVITY_NOTICE",
  "NON_ACCOUNTING_DOCUMENT",
  "UNKNOWN",
];

//: WI-5 §30 — the exact closed sender-scope/subject-predicate
//: vocabularies (services.evidence.classification_matcher's own
//: SENDER_SCOPE_TYPES/SUBJECT_PREDICATE_TYPES) — no regex/contains
//: option exists in the backend at all, so none is offered here.
const SENDER_SCOPE_OPTIONS = [
  { value: "EXACT_SENDER_ADDRESS", label: "Exact sender address" },
  { value: "EXACT_SENDER_DOMAIN", label: "Exact sender domain" },
];
const SUBJECT_PREDICATE_OPTIONS = [
  { value: "EXACT", label: "Exact subject" },
  { value: "STARTS_WITH", label: "Subject starts with" },
];

function _domainFromAddress(address) {
  const at = (address || "").lastIndexOf("@");
  return at >= 0 ? address.slice(at + 1) : "";
}

export const ClassificationReview = {
  async render(body, item) {
    const m = item.metadata || {};
    const evidenceId = m.evidence_id;
    // Note: the drawer's own generic "Original evidence" preview
    // (features/needs-you/needs-you.js::_renderReviewBody, run BEFORE
    // any branch — including this one — is called) already renders
    // §19/§25's original-evidence preview above whatever this function
    // appends; this branch does not repeat it.

    // ---- Evidence context (§25) ----
    const contextSection = el("div", { class: "review-drawer__section" });
    contextSection.appendChild(el("h3", { text: "Document" }));
    contextSection.appendChild(this._metaRow("Subject", m.subject));
    contextSection.appendChild(this._metaRow("Sender", m.sender_address));
    const entityRow = el("div", { class: "small domain-review__meta-row" }, [
      el("span", { class: "muted", text: "Entity: " }),
      el("span", { text: "Loading…" }),
    ]);
    contextSection.appendChild(entityRow);
    body.appendChild(contextSection);

    // Entity resolution — the review item's own metadata carries no
    // entity_id (WI-4's own bounded metadata shape, see
    // services/evidence/classification_review.py), so this fetches the
    // real EvidenceItem once and resolves its entity_id against the
    // existing entities cache (mirrors needs-you.js's own Company step
    // 1 use of listEntities()).
    let evidence = null;
    if (evidenceId) {
      const [evRes, entities] = await Promise.all([apiGet(API.evidenceOne(evidenceId)), listEntities()]);
      if (evRes.ok && evRes.body) {
        evidence = evRes.body;
        const entity = evidence.entity_id ? entities.find((e) => e.entity_id === evidence.entity_id) : null;
        entityRow.lastChild.textContent = entity ? entity.display_name : "UNRESOLVED";
      } else {
        entityRow.lastChild.textContent = "—";
      }
    } else {
      entityRow.lastChild.textContent = "—";
    }

    // ---- BAGMAN's proposal ----
    const proposalSection = el("div", { class: "review-drawer__section" });
    proposalSection.appendChild(el("h3", { text: "BAGMAN's proposal" }));
    proposalSection.appendChild(
      el("div", {}, [classificationBadge({ source: "AI_PROPOSAL", status: "REVIEW_REQUIRED", document_type: m.proposed_type })])
    );
    proposalSection.appendChild(this._metaRow("Proposed type", m.proposed_type));
    if (m.confidence != null) {
      proposalSection.appendChild(this._metaRow("Confidence", `${Math.round(m.confidence * 100)}%`));
    }
    body.appendChild(proposalSection);

    // ---- "Why BAGMAN thinks this" (§17/§25) ----
    const whyHost = el("div", { class: "review-drawer__section" }, [loadingState("Loading BAGMAN's reasoning…")]);
    body.appendChild(whyHost);
    if (m.ai_invocation_id) {
      const { ok, body: invocation } = await getInvocation(m.ai_invocation_id);
      clear(whyHost);
      whyHost.appendChild(el("h3", { text: "Why BAGMAN thinks this" }));
      if (ok && invocation) {
        const output = invocation.output || {};
        const signals = output.signals || [];
        const warnings = output.warnings || [];
        if (signals.length === 0 && warnings.length === 0) {
          whyHost.appendChild(el("p", { class: "muted small", text: "No additional signals recorded for this proposal." }));
        }
        if (signals.length > 0) {
          whyHost.appendChild(el("div", { class: "small", text: "Signals:" }));
          const list = el("ul", { class: "classification-panel__signal-list" });
          for (const s of signals) list.appendChild(el("li", { text: String(s) }));
          whyHost.appendChild(list);
        }
        if (warnings.length > 0) {
          whyHost.appendChild(el("div", { class: "small", text: "Warnings:" }));
          const list = el("ul", { class: "classification-panel__signal-list" });
          for (const w of warnings) list.appendChild(el("li", { text: String(w) }));
          whyHost.appendChild(list);
        }
      } else {
        whyHost.appendChild(el("p", { class: "muted small", text: "Could not load BAGMAN's reasoning for this proposal." }));
      }
    } else {
      clear(whyHost);
    }

    // ---- Final classification control (§26/§27) ----
    const decideSection = el("div", { class: "review-drawer__section" });
    decideSection.appendChild(el("h3", { text: "Decide" }));

    const typeSelect = el(
      "select",
      { attrs: { id: "review-classification-type-select" } },
      DOCUMENT_TYPES.map((t) => el("option", { attrs: { value: t }, text: t }))
    );
    if (m.proposed_type && DOCUMENT_TYPES.includes(m.proposed_type)) typeSelect.value = m.proposed_type;
    decideSection.appendChild(el("label", { class: "field", text: "Document type" }, [typeSelect]));

    const submitBtn = el("button", { class: "btn btn--primary", attrs: { type: "button" } });
    const updateSubmitLabel = () => {
      submitBtn.textContent =
        typeSelect.value === m.proposed_type ? `Confirm ${typeSelect.value}` : `Correct to ${typeSelect.value}`;
    };
    updateSubmitLabel();
    typeSelect.addEventListener("change", updateSubmitLabel);

    // ---- Optional rule teaching (§29-36) ----
    const teachCheckbox = el("input", { attrs: { type: "checkbox", id: "review-teach-rule-checkbox" } });
    teachCheckbox.checked = false; // §29 — default OFF, never silently learn
    decideSection.appendChild(
      el("label", { class: "field field--inline" }, [teachCheckbox, el("span", { text: "Teach BAGMAN to recognise similar documents" })])
    );

    const teachControls = el("div", { class: "review-teach-rule", attrs: { hidden: "true" } });

    const senderScopeSelect = el(
      "select",
      { attrs: { id: "review-teach-sender-scope-select" } },
      SENDER_SCOPE_OPTIONS.map((o) => el("option", { attrs: { value: o.value }, text: o.label }))
    );
    const subjectPredicateSelect = el(
      "select",
      { attrs: { id: "review-teach-subject-predicate-select" } },
      SUBJECT_PREDICATE_OPTIONS.map((o) => el("option", { attrs: { value: o.value }, text: o.label }))
    );
    const predicateValueInput = el("input", {
      attrs: { type: "text", id: "review-teach-predicate-value-input", autocomplete: "off" },
    });

    // §31 — safe teaching defaults: the NARROWEST truthful defaults
    // (exact sender address; exact current subject) — never the
    // broader domain+prefix combination preselected silently. The
    // operator may deliberately broaden.
    senderScopeSelect.value = "EXACT_SENDER_ADDRESS";
    subjectPredicateSelect.value = "EXACT";
    predicateValueInput.value = m.subject || "";

    teachControls.appendChild(el("label", { class: "field", text: "Sender scope" }, [senderScopeSelect]));
    teachControls.appendChild(el("label", { class: "field", text: "Subject match" }, [subjectPredicateSelect]));
    teachControls.appendChild(el("label", { class: "field", text: "Predicate value" }, [predicateValueInput]));

    const previewBtn = el("button", { class: "btn btn--secondary btn--sm", text: "Preview rule impact", attrs: { type: "button" } });
    const previewResultHost = el("div", { class: "review-teach-rule__preview-result" });
    teachControls.appendChild(
      el("p", {
        class: "muted small",
        text:
          "This teaches BAGMAN how to classify matching documents in future or during an explicit " +
          "reprocessing run. It does not silently rewrite historical documents.",
      })
    );
    teachControls.appendChild(previewBtn);
    teachControls.appendChild(previewResultHost);
    decideSection.appendChild(teachControls);

    // §33 — preview validity tracking: any edit to a governed field
    // invalidates the previous preview and re-disables submission.
    let previewValid = false;
    let lastPreview = null;
    const invalidatePreview = () => {
      previewValid = false;
      lastPreview = null;
      clear(previewResultHost);
      updateSubmitEnabled();
    };
    const updateSubmitEnabled = () => {
      if (!teachCheckbox.checked) {
        submitBtn.disabled = false;
        return;
      }
      submitBtn.disabled = !previewValid;
    };

    // §31/§27 — switching whether teaching is even considered also
    // re-evaluates submit-enablement (teaching OFF never blocks
    // submission on "no preview yet").
    teachCheckbox.addEventListener("change", () => {
      teachControls.hidden = !teachCheckbox.checked;
      invalidatePreview();
    });
    for (const control of [senderScopeSelect, subjectPredicateSelect, predicateValueInput, typeSelect]) {
      control.addEventListener("input", invalidatePreview);
      control.addEventListener("change", invalidatePreview);
    }

    const statusEl = el("div", { class: "upload-status", attrs: { "aria-live": "polite" } });

    previewBtn.addEventListener("click", async () => {
      const senderScopeValue =
        senderScopeSelect.value === "EXACT_SENDER_DOMAIN"
          ? _domainFromAddress(m.sender_address || "")
          : m.sender_address || "";
      if (!senderScopeValue || !predicateValueInput.value.trim()) {
        clear(previewResultHost);
        previewResultHost.appendChild(
          el("p", { class: "muted small", text: "Sender and subject predicate values are required before previewing." })
        );
        return;
      }
      previewBtn.disabled = true;
      clear(previewResultHost);
      previewResultHost.appendChild(loadingState("Checking rule impact…"));

      const { ok, status, body: result } = await previewClassificationRule({
        senderScopeType: senderScopeSelect.value,
        senderScopeValue,
        subjectPredicateType: subjectPredicateSelect.value,
        subjectPredicateValue: predicateValueInput.value.trim(),
        documentType: typeSelect.value,
      });
      previewBtn.disabled = false;
      clear(previewResultHost);

      if (!ok || !result) {
        previewResultHost.appendChild(
          el("p", { class: "reason-box reason-box--bad", text: `Could not preview rule: ${errorMessage(status, result)}` })
        );
        previewValid = false;
        updateSubmitEnabled();
        return;
      }

      previewResultHost.appendChild(
        el("p", {}, [el("strong", { text: `This rule matches ${result.match_count} existing document${result.match_count === 1 ? "" : "s"}.` })])
      );
      if (result.representative_subjects && result.representative_subjects.length > 0) {
        const list = el("ul", { class: "classification-panel__signal-list" });
        for (const subject of result.representative_subjects) list.appendChild(el("li", { text: subject || "(no subject)" }));
        previewResultHost.appendChild(list);
      }

      // §35 — reviewed-evidence guard: definitive when the representative
      // sample is exhaustive (match_count within the sample bound);
      // otherwise the client cannot conclusively tell, so it defers to
      // the backend's own authoritative guard at submit time (never
      // reimplemented client-side, §51) rather than block on a
      // possibly-wrong client-side guess.
      const sampleIsExhaustive = result.match_count <= (result.representative_evidence_ids || []).length;
      const reviewedDocumentIncluded = (result.representative_evidence_ids || []).includes(evidenceId);
      if (sampleIsExhaustive && result.match_count > 0 && !reviewedDocumentIncluded) {
        previewResultHost.appendChild(
          el("p", {
            class: "reason-box reason-box--warn",
            text: "This rule scope does not match the document you are reviewing — adjust the scope/predicate before submitting.",
          })
        );
        previewValid = false;
        updateSubmitEnabled();
        return;
      }

      lastPreview = {
        senderScopeType: senderScopeSelect.value, senderScopeValue,
        subjectPredicateType: subjectPredicateSelect.value, subjectPredicateValue: predicateValueInput.value.trim(),
        documentType: typeSelect.value,
      };
      previewValid = true;
      updateSubmitEnabled();
    });

    decideSection.appendChild(el("div", { class: "review-drawer__actions" }, [submitBtn]));
    decideSection.appendChild(statusEl);
    body.appendChild(decideSection);

    // §28 — no Dismiss button anywhere in this branch (the backend
    // already rejects DISMISSED for CLASSIFICATION_REVIEW — see
    // app/api/routers/needs_you.py — so this is not merely an omission,
    // it is the correct rendering of "no valid dismiss action exists").

    updateSubmitEnabled();

    submitBtn.addEventListener("click", async () => {
      if (submitBtn.disabled) return; // §38 — double-submit protection
      if (teachCheckbox.checked && !previewValid) return; // §33 belt-and-braces

      submitBtn.disabled = true;
      statusEl.dataset.kind = "progress";
      statusEl.textContent = "Saving…";

      const teachRule = teachCheckbox.checked && lastPreview
        ? {
            sender_scope_type: lastPreview.senderScopeType,
            sender_scope_value: lastPreview.senderScopeValue,
            subject_predicate_type: lastPreview.subjectPredicateType,
            subject_predicate_value: lastPreview.subjectPredicateValue,
          }
        : null;

      const { ok, status, body: result } = await resolveNeedsYouItem(item.item_id, {
        newStatus: "RESOLVED",
        resolution: { document_type: typeSelect.value, teach_rule: teachRule },
        actorId: getActorId(),
      });

      if (ok) {
        notify.ok(`Saved — ${typeSelect.value} recorded.`);
        drawer.close();
        // §39 — refresh Needs You list/open-count, Documents, Activity
        // without a full page reload.
        NeedsYou.load();
        document.dispatchEvent(new CustomEvent("bagman:needs-you-changed"));
        return;
      }

      // §36 — a real rule conflict (an ACTIVE rule already exists at
      // this exact scope with a different document_type) surfaces the
      // exact wording the WO specifies; every other failure renders
      // honestly via errorMessage().
      if (status === 409 && teachRule) {
        statusEl.dataset.kind = "bad";
        statusEl.textContent = "BAGMAN already has a different rule at this exact scope.";
      } else {
        statusEl.dataset.kind = "bad";
        statusEl.textContent = `Could not save: ${errorMessage(status, result)}`;
      }
      submitBtn.disabled = false;
    });
  },

  _metaRow(label, value) {
    return el("div", { class: "small domain-review__meta-row" }, [
      el("span", { class: "muted", text: `${label}: ` }),
      el("span", { text: value === null || value === undefined || value === "" ? "—" : String(value) }),
    ]);
  },
};
