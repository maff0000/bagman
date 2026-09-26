// features/needs-you/needs-you.js — the universal Needs You queue tab
// (CD-6 Slice 1, PID §98.2/§98.5) and the review drawer that answers
// Slice 1's one real item type, `COMPANY_REQUIRED` (Company/What/Why,
// PID §98.3). Also exposes `getOpenSummary()`, the single source of
// truth `features/overview/overview.js`'s real greeting is built from
// — no separate/duplicated counting logic between the two surfaces.
import { el, clear, qs } from "../../shared/dom.js";
import { fmtDateTime } from "../../shared/format.js";
import { priorityChip, needsYouStatusChip } from "../../shared/chips.js";
import { loadingState, emptyState, errorState } from "../../shared/state.js";
import { API, apiGet } from "../../shared/api.js";
import { getActorId } from "../../shared/operator.js";
import * as notify from "../../shared/notify.js";
import * as drawer from "../../shell/drawer.js";
import { listEntities } from "../../shell/entities.js";
import { renderEvidencePreview } from "../../shell/preview.js";
import { listNeedsYou, resolveNeedsYouItem } from "./needs-you-api.js";
import { getXeroAccounts } from "../xero/xero-api.js";
// CD-6 GUI-operations-foundation WO — teaches this generic queue's own
// review drawer a real summary for MAILBOX_DOMAIN_REVIEW items (see
// `_renderReviewBody` below) instead of the "not yet supported"
// fallback every non-COMPANY_WHAT_WHY item used to hit. The full
// batch-triage experience stays on the dedicated mailbox page
// (`features/mailbox/domain-review.js::open`) — this is a lighter
// summary-only render, reusing that module's own metadata-row helper
// rather than duplicating it.
import { DomainReview } from "../mailbox/domain-review.js";
// CD-6 Slice 5 WI-5 §24 — the CLASSIFICATION_REVIEW branch of this
// SAME universal review drawer (never a separate review modal/queue).
// See features/needs-you/classification-review.js's own module
// docstring for why it imports `NeedsYou` back from this file.
import { ClassificationReview } from "./classification-review.js";

//: services.needs_you.needs_you.ALLOWED_ACTION_MAILBOX_DOMAIN_REVIEW's
//: own real string value (services/needs_you/needs_you.py) — kept here
//: as a plain literal, matching this file's own existing convention of
//: comparing `allowed_action_type` against a literal string
//: ("COMPANY_WHAT_WHY" below) rather than importing a Python constant
//: across the wire.
const ALLOWED_ACTION_MAILBOX_DOMAIN_REVIEW = "MAILBOX_DOMAIN_REVIEW";

//: item_type -> human phrase template for both the queue card heading
//: and Overview's own greeting breakdown (PID §98.2's worked example —
//: "2 invoices need a company" / "1 email needs classification" / "1
//: rule proposal needs approval"). Slice 1 has a live producer only for
//: `COMPANY_REQUIRED`; the others are listed here because the
//: VOCABULARY is open/known today (services/needs_you/needs_you.py)
//: even though nothing raises them yet — see `summaryLines()` below,
//: which only ever renders a line for a type with a REAL non-zero
//: count, never a fabricated placeholder line for a type with zero
//: items (PID §98.2's own "don't fabricate the other categories"
//: instruction, carried from the CD-6 dispatch itself).
const TYPE_PHRASE = {
  COMPANY_REQUIRED: (n) => `${n} document${n === 1 ? "" : "s"} need${n === 1 ? "s" : ""} a company`,
  PURPOSE_REQUIRED: (n) => `${n} document${n === 1 ? "" : "s"} need${n === 1 ? "s" : ""} a purpose`,
  CLASSIFICATION_REVIEW: (n) => `${n} email${n === 1 ? "" : "s"} need${n === 1 ? "s" : ""} classification`,
  RULE_APPROVAL: (n) => `${n} rule proposal${n === 1 ? "" : "s"} need${n === 1 ? "s" : ""} approval`,
  GENERIC_QUESTION: (n) => `${n} question${n === 1 ? "" : "s"} need${n === 1 ? "s" : ""} an answer`,
};

export const NeedsYou = {
  _loaded: false,

  ensureLoaded() {
    if (this._loaded) return;
    this._loaded = true;
    this.load();
  },

  /** Real, live open-item counts, grouped by `item_type` — Overview's
   * own greeting (PID §98.2) reads this, never a separately-maintained
   * count. Returns `{total, byType: {ITEM_TYPE: count}}`; `total: null`
   * on a fetch failure (the caller renders an honest "unavailable"
   * state rather than a fabricated zero — see overview.js). */
  async getOpenSummary() {
    const { ok, body } = await listNeedsYou({ status: "OPEN", limit: 200 });
    if (!ok || !body) return { total: null, byType: {} };
    const byType = {};
    for (const item of body.items) {
      byType[item.item_type] = (byType[item.item_type] || 0) + 1;
    }
    // `open_count` (server-computed, PID §98.5's own field) is the
    // authoritative total even if this page's own `items` array is
    // itself capped at the 200-row query above.
    return { total: body.open_count, byType };
  },

  async load() {
    const list = qs("#needs-you-list");
    if (!list) return;
    clear(list);
    list.appendChild(loadingState("Loading Needs You queue…"));

    const { ok, status, body } = await listNeedsYou({ limit: 100 });
    clear(list);
    if (!ok || !body) {
      list.appendChild(errorState(status, body, "Could not load the Needs You queue"));
      return;
    }
    if (body.items.length === 0) {
      list.appendChild(
        emptyState("Nothing needs you right now.", "Everything else is running normally.")
      );
      return;
    }
    for (const item of body.items) {
      list.appendChild(this._card(item));
    }
  },

  _card(item) {
    const card = el("div", { class: `needs-you-card needs-you-card--${item.status.toLowerCase()}` });
    card.appendChild(
      el("div", { class: "needs-you-card__header" }, [
        el("div", { class: "needs-you-card__chips" }, [
          item.status === "OPEN" ? priorityChip(item.priority) : null,
          needsYouStatusChip(item.status),
        ]),
        el("span", { class: "muted small", text: fmtDateTime(item.created_at) }),
      ])
    );
    card.appendChild(el("div", { class: "needs-you-card__question", text: item.question }));
    card.appendChild(
      el("div", { class: "needs-you-card__meta muted small", text: `${item.domain} · ${item.item_type}` })
    );
    // CD-6 Slice 5 WI-5 §23 — the CLASSIFICATION_REVIEW card renders a
    // distinct, bounded summary (subject/filename, BAGMAN's proposal,
    // confidence) straight from the item's own metadata — never a
    // per-card evidence/entity fetch (the review drawer itself already
    // resolves the real entity, §25). Every value here comes straight
    // from `services.evidence.classification_review
    // .ensure_classification_review_item`'s own bounded metadata shape
    // — no client-side classification logic (§51).
    if (item.allowed_action_type === "CLASSIFICATION_REVIEW") {
      const m = item.metadata || {};
      const pct = m.confidence != null ? ` · ${Math.round(m.confidence * 100)}%` : "";
      card.appendChild(
        el("div", { class: "needs-you-card__meta small", text: `${m.subject || "(no subject)"}${pct}` })
      );
    }
    if (item.status === "OPEN") {
      const reviewBtn = el("button", {
        class: "btn btn--primary",
        text: item.allowed_action_type === "CLASSIFICATION_REVIEW" ? "Review classification" : "Review",
        attrs: { type: "button" },
      });
      reviewBtn.addEventListener("click", () => this.openReview(item));
      card.appendChild(el("div", { class: "needs-you-card__actions" }, [reviewBtn]));
    } else {
      card.appendChild(
        el("div", { class: "needs-you-card__resolution muted small" }, [
          el("span", {
            text: `${item.status === "RESOLVED" ? "Resolved" : "Dismissed"} by ${item.resolved_by_actor_id || "—"} · ${fmtDateTime(item.resolved_at)}`,
          }),
        ])
      );
    }
    return card;
  },

  /** Opens the review drawer for one item. Slice 1 only ever renders
   * the `COMPANY_WHAT_WHY` form (`allowed_action_type`) — any other
   * `allowed_action_type` (none exist yet; a future slice's producer)
   * falls back to a generic honest notice rather than pretending to
   * offer a form this GUI does not yet know how to render (PID §98.2's
   * "no fake buttons"). */
  async openReview(item) {
    drawer.open({
      title: "Review",
      render: (body) => this._renderReviewBody(body, item),
    });
  },

  async _renderReviewBody(body, item) {
    body.appendChild(el("div", { class: "review-drawer__question", text: item.question }));

    const evidenceId = item.metadata && item.metadata.evidence_id;
    const previewHost = el("div", { class: "review-drawer__preview" }, [loadingState("Loading original evidence…")]);
    body.appendChild(
      el("div", { class: "review-drawer__section review-drawer__section--preview" }, [
        el("h3", { text: "Original evidence" }),
        previewHost,
      ])
    );

    if (evidenceId) {
      const { ok, body: evidence } = await apiGet(API.evidenceOne(evidenceId));
      clear(previewHost);
      if (ok && evidence) {
        previewHost.appendChild(renderEvidencePreview(evidence));
      } else {
        previewHost.appendChild(emptyState("Original evidence could not be loaded."));
      }
    } else {
      clear(previewHost);
      previewHost.appendChild(emptyState("This item is not anchored to a single evidence record."));
    }

    if (item.allowed_action_type === "CLASSIFICATION_REVIEW") {
      // CD-6 Slice 5 WI-5 §24 — the full classification-review drawer
      // body (§25-36), branched exactly like MAILBOX_DOMAIN_REVIEW/
      // COMPANY_WHAT_WHY below. §28 — this branch never renders a
      // Dismiss button (the backend already rejects DISMISSED for this
      // item type, see app/api/routers/needs_you.py).
      await ClassificationReview.render(body, item);
      return;
    }

    if (item.allowed_action_type === ALLOWED_ACTION_MAILBOX_DOMAIN_REVIEW) {
      // A real, useful summary (CD-6 GUI-operations-foundation WO) —
      // never the generic "not yet supported" placeholder below for
      // this item type any more. The full Allow/Ignore/batch-triage
      // experience lives on the dedicated mailbox Domain Review page;
      // this is a summary-only render reached via the generic queue.
      DomainReview.renderGenericQueueSummary(body, item);
      return;
    }

    if (item.allowed_action_type !== "COMPANY_WHAT_WHY") {
      body.appendChild(
        el("div", { class: "review-drawer__section" }, [
          el("p", {
            class: "muted small",
            text: `BAGMAN does not yet have a review form for '${item.allowed_action_type}' — this item type is reserved for a future delivery.`,
          }),
        ])
      );
      return;
    }

    await this._renderCompanyWhatWhyForm(body, item);
  },

  /** The Company/What/Why form, presented as three questions that flow
   * one into the next (PID §98.2's "feels like a workflow, not a
   * database table") rather than one dense simultaneous form — each
   * step reveals once the one before it is answered. Still submits
   * all three together in the ONE existing `resolve` call once the
   * operator reaches "Save answer" (`needs-you-api.js`'s
   * `resolveNeedsYouItem` — unchanged wire contract, PID §98.3): this
   * is a front-end sequencing/presentation choice only, never a
   * second backend interaction. Element ids (`#review-entity-select`/
   * `#review-what-input`/`#review-why-input`) and the "Save answer"
   * button text are kept stable on purpose — this is the same tested
   * contract `gui_operations_foundation_browser_acceptance_proof.py`
   * already drives, just revealed progressively instead of all at
   * once. */
  async _renderCompanyWhatWhyForm(body, item) {
    const wrap = el("div", { class: "review-drawer__section review-steps" });
    body.appendChild(wrap);

    // ---- step 1: Company ----
    const step1 = el("div", { class: "review-step review-step--active" });
    step1.appendChild(el("div", { class: "review-step__label", text: "Step 1 of 3" }));
    step1.appendChild(el("div", { class: "review-step__prompt", text: "Which company is this for?" }));

    const entitySelect = el("select", { attrs: { id: "review-entity-select" } }, [
      el("option", { attrs: { value: "" }, text: "Select a company…" }),
    ]);
    const entities = await listEntities();
    for (const entity of entities) {
      entitySelect.appendChild(el("option", { attrs: { value: entity.entity_id }, text: entity.display_name }));
    }
    step1.appendChild(el("label", { class: "field", text: "Company" }, [entitySelect]));
    wrap.appendChild(step1);

    // ---- step 2: What (free-text business-purpose, unchanged) PLUS a
    // genuinely separate "Accounting category" control backed by real
    // synced Xero accounts for the selected company (CD-6 Slice 2, PID
    // §98.4, architect spec §8/§9/§10) ----
    const step2 = el("div", { class: "review-step", attrs: { hidden: "true" } });
    step2.appendChild(el("div", { class: "review-step__label", text: "Step 2 of 3" }));
    step2.appendChild(el("div", { class: "review-step__prompt", text: "What was this for?" }));
    const whatInput = el("input", {
      attrs: { type: "text", id: "review-what-input", placeholder: "e.g. Software subscription", autocomplete: "off" },
    });
    step2.appendChild(el("label", { class: "field", text: "What" }, [whatInput]));

    // Accounting category — a SEPARATE control from "What" (architect
    // spec §8: "keep 'What' as the free-text business-purpose
    // description unchanged, and add a genuinely separate 'Accounting
    // category' control"). The <select>'s VALUE is always the real
    // Xero `AccountID` — the visible option TEXT (`Code — Name`) is
    // display only and is never itself trusted as identity (architect
    // spec §9/§10). `xeroAccountFilter` is a plain client-side filter
    // over an ALREADY server-filtered eligible set fetched once per
    // company change — never a fetch-everything-then-filter-in-browser
    // "source of truth" (that policy decision stays server-side, PID
    // §98.4/architect spec §5 — see services/xero/eligibility.py).
    let xeroAccountsForEntity = [];
    const xeroAccountFilter = el("input", {
      attrs: { type: "text", id: "review-xero-account-filter", placeholder: "Filter accounts…", autocomplete: "off" },
      class: "field-inline",
    });
    xeroAccountFilter.hidden = true;
    const xeroAccountSelect = el("select", { attrs: { id: "review-xero-account-select", disabled: "true" } }, [
      el("option", { attrs: { value: "" }, text: "No Xero account" }),
    ]);
    const xeroStatusNote = el("p", { class: "muted small", text: "Select a company to see its Xero accounts." });
    step2.appendChild(
      el("label", { class: "field", text: "Accounting category (Xero)" }, [xeroAccountSelect, xeroAccountFilter])
    );
    step2.appendChild(xeroStatusNote);

    function renderXeroOptions(filterText) {
      clear(xeroAccountSelect);
      xeroAccountSelect.appendChild(el("option", { attrs: { value: "" }, text: "No Xero account" }));
      const needle = (filterText || "").trim().toLowerCase();
      for (const account of xeroAccountsForEntity) {
        const label = `${account.code ? `[${account.code}] ` : ""}${account.name}`;
        if (needle && !label.toLowerCase().includes(needle)) continue;
        xeroAccountSelect.appendChild(el("option", { attrs: { value: account.account_id }, text: label }));
      }
    }
    xeroAccountFilter.addEventListener("input", () => renderXeroOptions(xeroAccountFilter.value));

    wrap.appendChild(step2);

    // ---- step 3: Why ----
    const step3 = el("div", { class: "review-step", attrs: { hidden: "true" } });
    step3.appendChild(el("div", { class: "review-step__label", text: "Step 3 of 3" }));
    step3.appendChild(el("div", { class: "review-step__prompt", text: "Why was it purchased?" }));
    const whyInput = el("input", {
      attrs: { type: "text", id: "review-why-input", placeholder: "Short business-purpose explanation", autocomplete: "off" },
    });
    step3.appendChild(el("label", { class: "field", text: "Why" }, [whyInput]));
    wrap.appendChild(step3);

    // ---- actions (revealed once all three are answered) ----
    const statusEl = el("div", { class: "upload-status", attrs: { "aria-live": "polite" } });
    const resolveBtn = el("button", { class: "btn btn--primary", text: "Save answer", attrs: { type: "button" } });
    const dismissBtn = el("button", { class: "btn btn--ghost", text: "Dismiss (not applicable)", attrs: { type: "button" } });
    const actionsRow = el("div", { class: "review-drawer__actions" }, [resolveBtn, dismissBtn]);
    wrap.appendChild(actionsRow);
    wrap.appendChild(statusEl);

    // Reveal step 2 the moment a company is chosen; step 3 the moment
    // "what" has real text — a real DOM `change`/`input` listener
    // driving real reveals, not a scripted/fake multi-step animation
    // (PID §98.2's own "no fake buttons" spirit: every reveal here
    // responds to genuine operator input).
    entitySelect.addEventListener("change", () => {
      if (entitySelect.value) {
        step1.classList.add("review-step--done");
        step1.classList.remove("review-step--active");
        step2.hidden = false;
        step2.classList.add("review-step--active");
        loadXeroAccountsForSelectedCompany();
      }
    });

    /** Fetches this company's real synced, eligible Xero accounts (CD-6
     * Slice 2) the moment a company is chosen — never a fake/fallback
     * list, and never a client-side-filtered "every account" fetch
     * (see the field's own construction comment above). Shows "Xero not
     * connected" honestly (architect spec §9) and still leaves Company/
     * What/Why fully answerable when there is no connection. */
    async function loadXeroAccountsForSelectedCompany() {
      xeroAccountsForEntity = [];
      xeroAccountFilter.hidden = true;
      xeroAccountFilter.value = "";
      renderXeroOptions("");
      xeroAccountSelect.disabled = true;
      xeroStatusNote.textContent = "Loading Xero accounts…";

      const { ok, body } = await getXeroAccounts(entitySelect.value, true);
      if (!ok || !body) {
        xeroStatusNote.textContent = "Could not load Xero accounts for this company.";
        return;
      }
      if (!body.connected) {
        xeroStatusNote.textContent = "Xero chart of accounts not connected for this company.";
        return;
      }
      xeroAccountsForEntity = body.items;
      xeroAccountSelect.disabled = false;
      xeroAccountFilter.hidden = xeroAccountsForEntity.length < 8; // small lists don't need a filter box
      xeroStatusNote.textContent =
        xeroAccountsForEntity.length > 0
          ? `${xeroAccountsForEntity.length} account(s) from ${body.tenant_name || "Xero"}.`
          : "This company's Xero organisation has no eligible accounts synced yet.";
      renderXeroOptions("");
    }
    whatInput.addEventListener("input", () => {
      if (whatInput.value.trim()) {
        step2.classList.add("review-step--done");
        step3.hidden = false;
        step3.classList.add("review-step--active");
      }
    });

    resolveBtn.addEventListener("click", async () => {
      if (!entitySelect.value) {
        statusEl.dataset.kind = "bad";
        statusEl.textContent = "Select a company before saving.";
        return;
      }
      if (!whatInput.value.trim() || !whyInput.value.trim()) {
        statusEl.dataset.kind = "bad";
        statusEl.textContent = "Fill in both What and Why before saving.";
        return;
      }
      resolveBtn.disabled = true;
      dismissBtn.disabled = true;
      statusEl.dataset.kind = "progress";
      statusEl.textContent = "Saving…";

      const { ok, status, body: result } = await resolveNeedsYouItem(item.item_id, {
        newStatus: "RESOLVED",
        resolution: {
          entity_id: entitySelect.value,
          what: whatInput.value.trim(),
          why: whyInput.value.trim(),
          // The real Xero AccountID (never the displayed "[Code] Name"
          // label) — `null` when the operator left "No Xero account"
          // selected, e.g. this company has no Xero connection yet
          // (architect spec §9/§10).
          xero_account_id: xeroAccountSelect.value || null,
        },
        actorId: getActorId(),
      });

      if (ok) {
        notify.ok("Saved — company/what/why recorded.");
        drawer.close();
        NeedsYou.load();
        document.dispatchEvent(new CustomEvent("bagman:needs-you-changed"));
      } else {
        statusEl.dataset.kind = "bad";
        statusEl.textContent = `Could not save: ${(result && (result.message || result.detail)) || `HTTP ${status}`}`;
        resolveBtn.disabled = false;
        dismissBtn.disabled = false;
      }
    });

    dismissBtn.addEventListener("click", async () => {
      resolveBtn.disabled = true;
      dismissBtn.disabled = true;
      const { ok, status, body: result } = await resolveNeedsYouItem(item.item_id, {
        newStatus: "DISMISSED",
        resolution: null,
        actorId: getActorId(),
      });
      if (ok) {
        notify.ok("Dismissed.");
        drawer.close();
        NeedsYou.load();
        document.dispatchEvent(new CustomEvent("bagman:needs-you-changed"));
      } else {
        statusEl.dataset.kind = "bad";
        statusEl.textContent = `Could not dismiss: ${(result && (result.message || result.detail)) || `HTTP ${status}`}`;
        resolveBtn.disabled = false;
        dismissBtn.disabled = false;
      }
    });
  },

  /** Rendered content for the compact greeting summary lines (PID
   * §98.2's worked example) — used by `features/overview/overview.js`. */
  summaryLines(byType) {
    return Object.entries(byType)
      .filter(([, count]) => count > 0)
      .map(([type, count]) => (TYPE_PHRASE[type] ? TYPE_PHRASE[type](count) : `${count} ${type.toLowerCase().replace(/_/g, " ")}`));
  },
};
