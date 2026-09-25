// features/documents/classification-panel.js — the Documents detail
// panel's "Classification" section (CD-6 Slice 5 WI-5 §13-22), imported
// into features/documents/detail.js (mirrors how features/ai/
// invocation-card.js is its own file imported into detail.js already —
// keeps detail.js itself from becoming enormous).
//
// Renders, above the generic "AI Analysis" section: current-state
// (document type / status / decision source / confidence-when-AI),
// explainability ("Why BAGMAN thinks this" for an AI proposal, the
// learned-rule scope for a deterministic classification), the
// oldest-to-current lineage, and the "Classify with BAGMAN" action when
// no classification exists yet. Every displayed string that originates
// from evidence/model content (signals, warnings, subject, sender
// values) is rendered via shared/dom.js's `el({text: ...})` — never
// `innerHTML` (WI-5 §52).
import { el, clear } from "../../shared/dom.js";
import { errorMessage } from "../../shared/api.js";
import { loadingState, errorState } from "../../shared/state.js";
import { getActorId } from "../../shared/operator.js";
import * as notify from "../../shared/notify.js";
import { getClassificationHistory, getClassificationRule, classifyWithBagman } from "./classification-api.js";
import { getInvocation } from "../ai/ai-api.js";
import { classificationBadge, sourceLabel } from "./classification-format.js";
import { NeedsYou } from "../needs-you/needs-you.js";
import { listNeedsYou } from "../needs-you/needs-you-api.js";

//: WI-5 §22 — the outcomes classify_evidence's own orchestrator can
//: return that mean "an AI proposal now exists and needs operator
//: review" — see services/evidence/classification_orchestrator.py's
//: own OUTCOME_* constants (this is a plain literal mirror of those,
//: the same "compare against the real string value" discipline
//: features/needs-you/needs-you.js already uses for
//: `allowed_action_type`).
const AI_PROPOSAL_OUTCOMES = new Set(["AI_PROPOSAL_REVIEW_REQUIRED", "AI_PROPOSAL_UNCLASSIFIABLE"]);
const DETERMINISTIC_OUTCOMES = new Set(["DETERMINISTIC_CLASSIFIED", "DETERMINISTIC_EXISTING"]);

function _historyLine(item, byId) {
  if (item.source === "AI_PROPOSAL") {
    const pct = item.confidence != null ? ` · ${Math.round(item.confidence * 100)}%` : "";
    return `BAGMAN proposed: ${item.document_type}${pct}`;
  }
  if (item.source === "DETERMINISTIC_RULE") {
    return `Learned rule applied: ${item.document_type}`;
  }
  // OPERATOR_ASSIGNED — WI-5 §15's "Confirmed"/"Corrected" lineage
  // example, worded as "you" rather than a hardcoded operator name
  // (the classification row itself carries no actor display name — see
  // classification-format.js's own `sourceLabel` for the identical
  // "Confirmed by you" convention).
  const prior = item.supersedes_classification_id ? byId[item.supersedes_classification_id] : null;
  if (prior && prior.document_type !== item.document_type) {
    return `You corrected: ${prior.document_type} → ${item.document_type}`;
  }
  return `You confirmed: ${item.document_type}`;
}

export const ClassificationPanel = {
  /** Renders the whole Classification section into `container` (an
   * element `detail.js` provides, already positioned above the generic
   * AI Analysis section). */
  async render(container, evidenceId) {
    clear(container);
    container.appendChild(el("h3", { text: "Classification" }));

    const body = el("div", { class: "classification-panel" }, [loadingState("Loading classification…")]);
    container.appendChild(body);

    const { ok, status, body: history } = await getClassificationHistory(evidenceId);
    clear(body);
    if (!ok || !history) {
      body.appendChild(errorState(status, history, "Could not load classification"));
      return;
    }

    const items = history.items || [];
    const byId = {};
    for (const item of items) byId[item.classification_id] = item;
    const current = items.find((item) => item.is_current) || null;

    body.appendChild(this._currentStateBlock(current));

    if (current) {
      const explainHost = el("div", { class: "classification-panel__explain" });
      body.appendChild(explainHost);
      await this._renderExplanation(explainHost, current);
    }

    if (items.length > 0) {
      body.appendChild(this._historyBlock(items, byId));
    }

    const actionHost = el("div", { class: "classification-panel__action" });
    body.appendChild(actionHost);
    this._renderAction(actionHost, evidenceId, current, container);
  },

  // ---- current-state (WI-5 §14) ----

  _currentStateBlock(current) {
    const section = el("div", { class: "classification-panel__current" });
    if (!current) {
      section.appendChild(el("p", { class: "muted small", text: "Unclassified — BAGMAN has not classified this document yet." }));
      return section;
    }

    const kv = el("dl", { class: "detail-kv" });
    const rows = [
      ["Document type", current.document_type],
      ["Status", current.status],
      ["Decision source", sourceLabel(current.source)],
    ];
    if (current.source === "AI_PROPOSAL" && current.confidence != null) {
      rows.push(["Confidence", `${Math.round(current.confidence * 100)}%`]);
    }
    rows.push(["Classification ID", current.classification_id]);
    for (const [k, v] of rows) {
      kv.appendChild(el("dt", { text: k }));
      kv.appendChild(el("dd", { text: v === null || v === undefined ? "—" : String(v) }));
    }
    section.appendChild(el("div", {}, [classificationBadge(current)]));
    section.appendChild(kv);
    return section;
  },

  // ---- explainability (WI-5 §17/§18) ----

  async _renderExplanation(host, current) {
    if (current.source === "AI_PROPOSAL" && current.ai_invocation_id) {
      host.appendChild(el("h4", { text: "Why BAGMAN thinks this" }));
      const { ok, body: invocation } = await getInvocation(current.ai_invocation_id);
      if (!ok || !invocation) {
        host.appendChild(el("p", { class: "muted small", text: "Could not load the AI reasoning for this proposal." }));
        return;
      }
      const output = invocation.output || {};
      const signals = output.signals || [];
      const warnings = output.warnings || [];
      const meta = el("p", { class: "muted small" }, [
        el("span", { text: `Model: ${invocation.capability_alias || invocation.provider_model || "—"}` }),
      ]);
      host.appendChild(meta);
      if (signals.length > 0) {
        host.appendChild(el("div", { class: "small", text: "Signals:" }));
        const list = el("ul", { class: "classification-panel__signal-list" });
        for (const signal of signals) list.appendChild(el("li", { text: String(signal) }));
        host.appendChild(list);
      }
      if (warnings.length > 0) {
        host.appendChild(el("div", { class: "small", text: "Warnings:" }));
        const list = el("ul", { class: "classification-panel__signal-list" });
        for (const warning of warnings) list.appendChild(el("li", { text: String(warning) }));
        host.appendChild(list);
      }
      // WI-5 §17 — never system prompt / raw prompt / chain of thought
      // / complete MIME/body here. Only the bounded fields above.
      return;
    }

    if (current.source === "DETERMINISTIC_RULE" && current.rule_id) {
      host.appendChild(el("h4", { text: "Why BAGMAN thinks this" }));
      const { ok, body: rule } = await getClassificationRule(current.rule_id);
      if (!ok || !rule) {
        host.appendChild(el("p", { class: "muted small", text: "Could not load the learned rule." }));
        return;
      }
      const senderDesc =
        rule.sender_scope_type === "EXACT_SENDER_ADDRESS"
          ? `email from ${rule.sender_scope_value}`
          : `email from ${rule.sender_scope_value}`;
      const subjectDesc =
        rule.subject_predicate_type === "STARTS_WITH"
          ? `whose subject starts with '${rule.subject_predicate_value}'`
          : `whose subject is exactly '${rule.subject_predicate_value}'`;
      host.appendChild(el("p", { text: `Learned rule: ${senderDesc} ${subjectDesc}.` }));
      host.appendChild(
        el("p", { class: "muted small", text: `Rule ID: ${rule.rule_id}` })
      );
      return;
    }
    // OPERATOR_ASSIGNED — no further explanation needed beyond the
    // current-state block's own "Confirmed by you" source label.
  },

  // ---- history / lineage (WI-5 §15) ----

  _historyBlock(items, byId) {
    const section = el("div", { class: "classification-panel__history" });
    section.appendChild(el("h4", { text: "History" }));
    const list = el("ol", { class: "timeline" });
    for (const item of items) {
      list.appendChild(
        el("li", {}, [
          el("div", { class: "t-label", text: _historyLine(item, byId) }),
          el("div", { class: "t-time muted small", text: item.created_at }),
        ])
      );
    }
    section.appendChild(list);
    return section;
  },

  // ---- action (WI-5 §20/§22) ----

  _renderAction(host, evidenceId, current, container) {
    if (current) return; // §20 — only offered when NO current classification exists.

    const btn = el("button", { class: "btn btn--primary", text: "Classify with BAGMAN", attrs: { type: "button" } });
    const statusEl = el("div", { class: "upload-status", attrs: { "aria-live": "polite" } });
    host.appendChild(btn);
    host.appendChild(statusEl);

    btn.addEventListener("click", async () => {
      btn.disabled = true;
      statusEl.dataset.kind = "progress";
      statusEl.textContent = "Classifying…"; // honest — the fetch IS the wait

      const { ok, status, body } = await classifyWithBagman(evidenceId, { actorId: getActorId() });

      if (!ok) {
        // §22 conflict/error path — never a silent fallback.
        statusEl.dataset.kind = "bad";
        statusEl.textContent = `Could not classify: ${errorMessage(status, body)}`;
        btn.disabled = false;
        return;
      }

      const outcome = body.outcome;
      if (DETERMINISTIC_OUTCOMES.has(outcome) || outcome === "CURRENT_CLASSIFICATION_EXISTS") {
        // §22 — deterministic match, or existing classification found:
        // refresh the panel to show the resulting/current truth.
        statusEl.dataset.kind = "ok";
        statusEl.textContent = "Classified.";
        await this.render(container, evidenceId);
        return;
      }
      if (AI_PROPOSAL_OUTCOMES.has(outcome)) {
        statusEl.dataset.kind = "progress";
        statusEl.textContent = "BAGMAN has a proposal — needs your review.";
        const reviewBtn = el("button", { class: "btn btn--secondary", text: "Review now", attrs: { type: "button" } });
        reviewBtn.addEventListener("click", () => this._openReviewFor(body.evidence_classification && body.evidence_classification.classification_id));
        host.appendChild(reviewBtn);
        return;
      }
      if (outcome === "CONTEXT_UNSUPPORTED") {
        statusEl.dataset.kind = "warn";
        statusEl.textContent = `BAGMAN cannot classify this document: ${body.unsupported_reason || "unsupported content"}.`;
        btn.disabled = false;
        return;
      }
      // AI_PRIOR_FAILURE / AI_INVOCATION_FAILED / anything else — an
      // honest failure, never a silent fallback (§22).
      statusEl.dataset.kind = "bad";
      statusEl.textContent = `Classification failed: ${body.error_code || outcome}.`;
      btn.disabled = false;
    });
  },

  /** WI-5 §22's "Review now" — finds the OPEN CLASSIFICATION_REVIEW
   * NeedsYouItem for this AI classification (via the same
   * `(item_type, source_object_reference)` shape the backend itself
   * uses to dedupe it — see services/evidence/classification_review.py)
   * and opens the SAME review drawer the Needs You tab uses (WI-5 §24 —
   * never a second review surface). */
  async _openReviewFor(classificationId) {
    if (!classificationId) return;
    const { ok, body } = await listNeedsYou({ status: "OPEN", itemType: "CLASSIFICATION_REVIEW", limit: 200 });
    if (!ok || !body) {
      notify.error("Could not open the review — try the Needs You tab.");
      return;
    }
    const item = (body.items || []).find((i) => i.source_object_reference === classificationId);
    if (!item) {
      notify.error("Could not find the review item — try the Needs You tab.");
      return;
    }
    const tabBtn = document.querySelector('.tab-btn[data-tab="needs-you"]');
    if (tabBtn) tabBtn.click();
    NeedsYou.openReview(item);
  },
};
