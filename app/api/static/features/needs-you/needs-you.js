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
    if (item.status === "OPEN") {
      const reviewBtn = el("button", {
        class: "btn btn--primary",
        text: "Review",
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
    body.appendChild(el("div", { class: "review-drawer__section" }, [el("h3", { text: "Original evidence" }), previewHost]));

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

  async _renderCompanyWhatWhyForm(body, item) {
    const section = el("div", { class: "review-drawer__section" }, [el("h3", { text: "Company / What / Why" })]);
    body.appendChild(section);

    const entitySelect = el("select", { attrs: { id: "review-entity-select" } }, [
      el("option", { attrs: { value: "" }, text: "Select a company…" }),
    ]);
    const entities = await listEntities();
    for (const entity of entities) {
      entitySelect.appendChild(el("option", { attrs: { value: entity.entity_id }, text: entity.display_name }));
    }

    const whatInput = el("input", {
      attrs: { type: "text", id: "review-what-input", placeholder: "e.g. Software subscription", autocomplete: "off" },
    });
    const whyInput = el("input", {
      attrs: { type: "text", id: "review-why-input", placeholder: "Short business-purpose explanation", autocomplete: "off" },
    });

    const statusEl = el("div", { class: "upload-status", attrs: { "aria-live": "polite" } });

    const resolveBtn = el("button", { class: "btn btn--primary", text: "Save answer", attrs: { type: "button" } });
    const dismissBtn = el("button", { class: "btn btn--ghost", text: "Dismiss (not applicable)", attrs: { type: "button" } });

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
        resolution: { entity_id: entitySelect.value, what: whatInput.value.trim(), why: whyInput.value.trim() },
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

    section.appendChild(el("label", { class: "field", text: "Company" }, [entitySelect]));
    section.appendChild(el("label", { class: "field", text: "What" }, [whatInput]));
    section.appendChild(
      el("p", {
        class: "muted small",
        text: "Xero chart of accounts not yet connected — temporary free-text, will be replaced with real Xero account coding.",
      })
    );
    section.appendChild(el("label", { class: "field", text: "Why" }, [whyInput]));
    section.appendChild(el("div", { class: "review-drawer__actions" }, [resolveBtn, dismissBtn]));
    section.appendChild(statusEl);
  },

  /** Rendered content for the compact greeting summary lines (PID
   * §98.2's worked example) — used by `features/overview/overview.js`. */
  summaryLines(byType) {
    return Object.entries(byType)
      .filter(([, count]) => count > 0)
      .map(([type, count]) => (TYPE_PHRASE[type] ? TYPE_PHRASE[type](count) : `${count} ${type.toLowerCase().replace(/_/g, " ")}`));
  },
};
