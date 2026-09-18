// features/mailbox/domain-review.js — the domain-review batch-triage
// surface for one Microsoft-provider mailbox (CD-6 GUI-operations-
// foundation WO). This is the GUI half of a WO whose backend half
// (services.xero.supplier_correlation, the two new
// app/api/routers/mailboxes_microsoft.py endpoints this file's own
// mailbox-api.js wrappers call) was already built and deployed
// separately — see this module's own dispatch for the real, live
// context: a real production mailbox (matt@infosecurs.com) has 90 OPEN
// MAILBOX_DOMAIN_REVIEW Needs You items from a completed historical
// discovery sweep, and until this file existed there was NO way for an
// operator to see/triage them beyond one-at-a-time via the generic
// Needs You queue (whose own review drawer explicitly shows a
// "not yet supported" placeholder for this item type — see
// features/needs-you/needs-you.js's own `allowed_action_type !==
// "COMPANY_WHAT_WHY"` branch, which this WO also teaches a real
// domain-review summary, separately from this dedicated page).
//
// Real, live constraint this file must render calmly, never as an
// alarming error: the REAL current Infosecurs Xero connection only has
// `accounting.settings.read` scope today — Contacts/Invoices access
// (what correlation needs) requires a real, later, operator-driven
// re-consent. Triggering correlation against it will genuinely fail
// with an honest `{ok: false, error_type: "XeroSupplierCorrelationFailedError"}`
// response until that happens. This is expected, not a bug — see
// `_runCorrelation()` below.
//
// DOM-helper/notification/API-wrapper conventions match
// features/needs-you/needs-you.js and features/mailbox/mailboxes.js
// exactly (read first, per this WO's own instruction) — vanilla JS, no
// framework, no build step, no innerHTML (shared/dom.js's own
// discipline).
import { el, clear } from "../../shared/dom.js";
import { fmtDateTime } from "../../shared/format.js";
import { chip, xeroCorrelationClassChip } from "../../shared/chips.js";
import { loadingState, emptyState, errorState } from "../../shared/state.js";
import { errorMessage } from "../../shared/api.js";
import { getActorId } from "../../shared/operator.js";
import * as notify from "../../shared/notify.js";
import * as drawer from "../../shell/drawer.js";
import { listEntities } from "../../shell/entities.js";
import {
  listDomainReviewItems,
  runXeroCorrelation,
  resolveMailboxDomainReviewItem,
  batchResolveMailboxDomainReview,
} from "./mailbox-api.js";

//: Sort options for the table — a plain, small closed set (no generic
//: click-any-column-header sort framework; this is a 90-ish-row
//: operator worklist, not a data-grid product). Each comparator reads
//: straight off the raw NeedsYouItem shape this page already fetched —
//: never a second round trip just to re-sort client-side.
//:
//: `review_priority_desc` is FIRST in this map on purpose — it is also
//: this table's own DEFAULT initial sort key (see `_loadTable` below,
//: which explicitly sets the `<select>`'s value rather than relying on
//: map/DOM insertion order alone to make that intent obvious) — the
//: mailbox-evidence-based triage addendum's whole point is "show the
//: operator the highest-yield domains first", so the highest-priority
//: domains land at the top of the table the moment the drawer opens,
//: not only once an operator manually picks this option. `domain_asc`
//: (the table's PREVIOUS default) remains one of the explicit choices
//: below, unchanged.
const SORT_OPTIONS = {
  review_priority_desc: {
    label: "Review priority (highest first)",
    // Tie-break: most candidate messages first — the SAME ordering
    // `candidates_desc` below already uses on its own, applied here as
    // the secondary key within one priority band.
    compare: (a, b) =>
      _priorityRank(b) - _priorityRank(a) || (b.metadata.candidate_message_count || 0) - (a.metadata.candidate_message_count || 0),
  },
  domain_asc: {
    label: "Domain (A–Z)",
    compare: (a, b) => (a.metadata.sender_domain || "").localeCompare(b.metadata.sender_domain || ""),
  },
  candidates_desc: {
    label: "Most candidate messages first",
    compare: (a, b) => (b.metadata.candidate_message_count || 0) - (a.metadata.candidate_message_count || 0),
  },
  attachment_ratio_desc: {
    label: "Attachment ratio (highest first)",
    compare: (a, b) => _attachmentRatio(b) - _attachmentRatio(a),
  },
  recurrence_span_desc: {
    label: "Recurrence span (widest first)",
    compare: (a, b) => _recurrenceSpanMs(b) - _recurrenceSpanMs(a),
  },
  last_seen_desc: {
    label: "Most recently seen first",
    compare: (a, b) => (b.metadata.last_seen_at || "").localeCompare(a.metadata.last_seen_at || ""),
  },
  correlation_class: {
    label: "Correlation class (strong first)",
    compare: (a, b) => _correlationRank(b) - _correlationRank(a),
  },
};

//: `review_priority` -> chip kind. Mirrors `shared/chips.js`'s own
//: `PRIORITY_KIND` (Needs You priority: `{HIGH: "bad", NORMAL:
//: "progress", LOW: "neutral"}`) exactly — the SAME three-bucket
//: "attention colour" convention, applied to this page's own closed
//: `services.mailbox.domain_review_priority.REVIEW_PRIORITIES`
//: vocabulary instead. Built on the generic `chip()` primitive
//: directly (see `shared/chips.js`'s own docstring) rather than a new
//: bespoke chip function — `chip()` already supports this cleanly.
const REVIEW_PRIORITY_KIND = { HIGH: "bad", MEDIUM: "progress", LOW: "neutral" };

function _priorityRank(item) {
  return { HIGH: 3, MEDIUM: 2, LOW: 1 }[item.review_priority] || 0;
}

//: `discovery_reason_counts` bucket key -> short, human display label
//: for the table's own "strongest aggregated discovery signals" cell —
//: mirrors `services/mailbox/domain_review_priority.py`'s own closed
//: 5-bucket vocabulary exactly (see that module's docstring). Never the
//: raw per-message `discovery_reason` string, never a subject line —
//: only this page's own short, bucketed label.
const DISCOVERY_REASON_BUCKET_LABELS = {
  invoice_subject_signal_count: "invoice subject",
  receipt_subject_signal_count: "receipt subject",
  invoice_like_attachment_filename_count: "invoice-like attachment",
  accounting_document_attachment_signal_count: "accounting-doc attachment",
  other_bounded_heuristic_reason_count: "other signal",
};

function _attachmentRatio(item) {
  const count = item.metadata.candidate_message_count || 0;
  if (!count) return 0;
  return (item.metadata.attachment_bearing_count || 0) / count;
}

function _attachmentRatioLabel(item) {
  const count = item.metadata.candidate_message_count || 0;
  if (!count) return "—";
  return `${Math.round(_attachmentRatio(item) * 100)}%`;
}

//: `(last_seen_at - first_seen_at)` in milliseconds, `0` when either
//: timestamp is missing — used for both the recurrence/span SORT and
//: the "widest span first" tie-break intent; display uses
//: `fmtDateTime` on the two raw timestamps directly (this page already
//: shows "First seen"/"Last seen" as their own columns), so no new
//: display-only span column is added — only the new sort key.
function _recurrenceSpanMs(item) {
  const first = item.metadata.first_seen_at ? new Date(item.metadata.first_seen_at).getTime() : 0;
  const last = item.metadata.last_seen_at ? new Date(item.metadata.last_seen_at).getTime() : 0;
  return last - first;
}

//: The single highest-count bucket from `discovery_reason_counts`,
//: rendered as short text (e.g. "6× invoice subject") — never the raw
//: per-message `discovery_reason` strings, never a subject line (WO's
//: own explicit instruction). "—" when an item has no candidate
//: messages at all (should not happen for a real domain-review item,
//: but defended against) or every bucket is zero.
function _strongestSignalLabel(item) {
  const counts = item.discovery_reason_counts || {};
  let bestBucket = null;
  let bestCount = 0;
  for (const [bucket, count] of Object.entries(counts)) {
    if (count > bestCount) {
      bestBucket = bucket;
      bestCount = count;
    }
  }
  if (!bestBucket) return "—";
  return `${bestCount}× ${DISCOVERY_REASON_BUCKET_LABELS[bestBucket] || bestBucket}`;
}

//: STRONG_PURCHASE_BILL outranks STRONG_BANK_SPEND (the SAME "more
//: traditionally governed accounting artifact wins by convention" rule
//: — CD-6 second-correlation-source WO), which outranks the shared-
//: domain-capped class, which outranks CONTACT_ONLY, which outranks a
//: real NONE verdict, which outranks "never correlated at all" —
//: mirrors services/xero/supplier_correlation.py::_rank's own class
//: ordering (display-only here; this file makes no correlation
//: DECISION itself).
const _CORRELATION_RANK = {
  STRONG_PURCHASE_BILL: 5,
  STRONG_BANK_SPEND: 4,
  SHARED_DOMAIN_REQUIRES_MANUAL_REVIEW: 3,
  CONTACT_ONLY: 2,
  NONE: 1,
};

function _correlationRank(item) {
  const cls = item.metadata.xero_correlation_class;
  return cls ? _CORRELATION_RANK[cls] || 0 : 0;
}

/** A generic (never hardcoded to any one real company — see the repo-
 * wide guardrail this file must respect,
 * test_no_hardcoded_company_truth_anywhere_in_static_ui) heuristic for
 * "does this entity's own canonical/display name plausibly match this
 * mailbox's email domain" — e.g. `matt@example.com` plausibly matches
 * an entity named "Example Ltd". Used only to PRE-SELECT the
 * correlation/Allow entity dropdown for operator convenience; the
 * server never trusts or infers anything from this (`entity_id` is
 * always explicit on the wire either way). */
function _entityMatchesMailboxDomain(entity, mailbox) {
  const emailDomain = (mailbox.email_address || "").split("@")[1] || "";
  const domainLabel = emailDomain.split(".")[0].toLowerCase();
  if (domainLabel.length < 3) return false;
  const haystack = `${entity.canonical_name || ""} ${entity.display_name || ""}`.toLowerCase().replace(/[^a-z0-9]/g, "");
  return haystack.includes(domainLabel);
}

export const DomainReview = {
  /** Real entry point — called from mailboxes.js's own "Domain Review
   * (N)" button on a mailbox card. Opens the shared drawer
   * (`.review-drawer` is 1040px-wide-capped — wide enough for a real
   * table with a toolbar, unlike the Needs You 2-column review form
   * this same drawer mount point usually hosts) with this mailbox's
   * full batch-triage surface. */
  open(mailbox) {
    drawer.open({
      title: `Domain review — ${mailbox.display_name}`,
      render: (body) => this._render(body, mailbox),
    });
  },

  async _render(body, mailbox) {
    // `.domain-review-panel` (style.css) spans the drawer's own
    // 2-column grid (built for the Needs You evidence+form layout) as
    // ONE full-width column — this page has no "original evidence"
    // left-hand pane, it IS the whole surface.
    const panel = el("div", { class: "domain-review-panel" });
    body.appendChild(panel);

    const entities = await listEntities();

    panel.appendChild(this._correlationSection(mailbox, entities));

    const tableHost = el("div", { class: "domain-review__table-host" });
    panel.appendChild(tableHost);
    await this._loadTable(tableHost, mailbox, entities);
  },

  // -------------------------------------------------------------
  // Xero correlation trigger (capability 1)
  // -------------------------------------------------------------

  /** `entities`: the already-fetched `listEntities()` array, shared
   * with the table/toolbar below so only one `GET /internal/entities`
   * round trip happens per drawer open. */
  _correlationSection(mailbox, entities) {
    // A fresh entity `<select>` node is built here every time a drawer
    // is (re)opened — reset the "listener already attached" flag so
    // `_buildToolbar` attaches its change listener to THIS new node
    // rather than assuming the old node's listener still applies.
    this._entityChangeListenerAdded = false;
    const section = el("div", { class: "review-drawer__section domain-review__correlate" });
    section.appendChild(el("h3", { text: "Xero-assisted correlation" }));
    section.appendChild(
      el("p", {
        class: "muted small",
        text:
          "Reads this company's real Xero Contacts/purchase Invoices and attaches a review-aid " +
          "classification to every still-open domain below — it never creates a rule or approves anything " +
          "by itself.",
      })
    );

    const entitySelect = el(
      "select",
      { attrs: { id: "domain-review-entity-select" } },
      entities.map((entity) => el("option", { attrs: { value: entity.entity_id }, text: entity.display_name }))
    );
    // Pre-select this mailbox's own `default_entity_id` hint when
    // present, else fall back to whichever company's canonical/display
    // name matches this mailbox's own email domain — a GUI convenience
    // only, never hardcoded to any one real company name/canonical_name
    // (repo-wide guardrail — see
    // tests/integration/test_architecture_boundaries.py
    // ::test_no_hardcoded_company_truth_anywhere_in_static_ui — every
    // company-aware surface resolves companies through
    // `GET /internal/entities`/`listEntities()` alone, PID §98.3/§98.4).
    // The server NEVER infers this itself either way — `entity_id` is
    // always explicit on the wire (see
    // XeroCorrelateMailboxDomainReviewRequest's own docstring); this is
    // purely "which option is highlighted when the drawer opens".
    const preselected =
      entities.find((e) => e.entity_id === mailbox.default_entity_id) ||
      entities.find((e) => _entityMatchesMailboxDomain(e, mailbox));
    if (preselected) entitySelect.value = preselected.entity_id;
    this._entitySelect = entitySelect;

    const correlateBtn = el("button", {
      class: "btn btn--primary btn--sm",
      text: "Run Xero correlation",
      attrs: { type: "button" },
    });
    // Reuses `.upload-status` (style.css) exactly as
    // features/needs-you/needs-you.js's own form status lines do — a
    // `data-kind` of "progress"/"ok"/"bad" is already styled there;
    // this file's one addition is genuinely reusing "progress" (calm,
    // muted) for the expected pre-re-authorization state too, rather
    // than inventing a new "info" kind for a single caller.
    const statusEl = el("div", { class: "upload-status", attrs: { "aria-live": "polite" } });

    section.appendChild(
      el("div", { class: "field field--inline" }, [
        el("label", { text: "Correlate against" }, []),
        entitySelect,
        correlateBtn,
      ])
    );
    section.appendChild(statusEl);

    correlateBtn.addEventListener("click", () => this._runCorrelation(mailbox, entitySelect, correlateBtn, statusEl));

    return section;
  },

  async _runCorrelation(mailbox, entitySelect, correlateBtn, statusEl) {
    if (!entitySelect.value) {
      statusEl.dataset.kind = "bad";
      statusEl.textContent = "Select a company first.";
      return;
    }
    correlateBtn.disabled = true;
    statusEl.dataset.kind = "progress";
    statusEl.textContent = "Running Xero correlation…";

    const { ok, status, body: result } = await runXeroCorrelation(mailbox.mailbox_id, {
      entityId: entitySelect.value,
      actorId: getActorId(),
    });
    correlateBtn.disabled = false;

    if (!ok || !result) {
      // A genuine request-level failure (bad mailbox/entity, no Xero
      // connection at all, a token refresh failure) — this IS an
      // alarming/unexpected state, unlike the ok:false case below.
      statusEl.dataset.kind = "bad";
      statusEl.textContent = `Could not run Xero correlation: ${errorMessage(status, result)}`;
      return;
    }

    if (!result.ok) {
      // The EXPECTED pre-re-authorization state (WO's own explicit
      // instruction: calm, never alarming) — e.g. the real current
      // Infosecurs connection only has accounting.settings.read scope
      // today.
      statusEl.dataset.kind = "progress"; // calm, muted — never an alarming colour for this expected state
      statusEl.textContent =
        "Xero correlation is not yet available — additional Xero read access has not been authorized yet.";
      return;
    }

    statusEl.dataset.kind = "ok";
    statusEl.textContent =
      `Correlation complete — ${result.contacts_read} contact(s), ${result.purchase_invoices_examined} ` +
      `invoice(s), and ${result.bank_transactions_examined} bank transaction(s) examined across ` +
      `${result.domain_review_items_updated} open domain(s): ` +
      `${result.strong_purchase_bill_count} strong (bill), ${result.strong_bank_spend_count} strong (bank spend), ` +
      `${result.contact_only_correlation_count} contact-only, ` +
      `${result.shared_domain_count} shared-domain, ${result.no_correlation_count} no match.`;
    notify.ok("Xero correlation complete.");

    const tableHost = this._tableHost;
    if (tableHost) await this._loadTable(tableHost, mailbox, this._entitiesCache);
  },

  // -------------------------------------------------------------
  // The table (capability 2) + selection toolbar (capability 3)
  // -------------------------------------------------------------

  async _loadTable(tableHost, mailbox, entities) {
    this._tableHost = tableHost;
    this._entitiesCache = entities;
    clear(tableHost);
    tableHost.appendChild(loadingState("Loading domain-review items…"));

    const { ok, status, body } = await listDomainReviewItems(mailbox.mailbox_id); // default status=OPEN
    clear(tableHost);
    if (!ok || !body) {
      tableHost.appendChild(errorState(status, body, "Could not load domain-review items"));
      return;
    }
    if (body.items.length === 0) {
      tableHost.appendChild(
        emptyState("No open domain-review items.", "Every candidate-bearing sender domain has already been decided.")
      );
      return;
    }

    this._items = body.items;
    this._selected = new Set();
    tableHost.appendChild(this._buildToolbar(mailbox, entities));
    this._resultsHost = el("div", { class: "domain-review__batch-results" });
    tableHost.appendChild(this._resultsHost);

    const filterInput = el("input", {
      attrs: { type: "text", placeholder: "Filter by domain…", autocomplete: "off" },
      class: "field-inline",
    });
    // Priority filter — a simple closed `<select>` alongside the
    // existing domain-text filter (mailbox-evidence-based triage
    // addendum). "" (the "All priorities" option's own value) means no
    // filtering by priority at all.
    const priorityFilterSelect = el("select", { class: "field-inline" }, [
      el("option", { attrs: { value: "" }, text: "All priorities" }),
      el("option", { attrs: { value: "HIGH" }, text: "HIGH" }),
      el("option", { attrs: { value: "MEDIUM" }, text: "MEDIUM" }),
      el("option", { attrs: { value: "LOW" }, text: "LOW" }),
    ]);
    const sortSelect = el(
      "select",
      {},
      Object.entries(SORT_OPTIONS).map(([key, opt]) => el("option", { attrs: { value: key }, text: opt.label }))
    );
    // Default initial ordering: highest-yield first (see SORT_OPTIONS'
    // own docstring above) — set explicitly rather than relying on
    // "first `<option>` in DOM order" alone, so this intent reads
    // clearly in the code itself.
    sortSelect.value = "review_priority_desc";
    tableHost.appendChild(
      el("div", { class: "filters" }, [filterInput, priorityFilterSelect, sortSelect])
    );

    const tableWrap = el("div", { class: "table-wrap" });
    tableHost.appendChild(tableWrap);

    const rerenderRows = () =>
      this._renderRows(tableWrap, mailbox, entities, filterInput.value, sortSelect.value, priorityFilterSelect.value);
    filterInput.addEventListener("input", rerenderRows);
    priorityFilterSelect.addEventListener("change", rerenderRows);
    sortSelect.addEventListener("change", rerenderRows);
    rerenderRows();
  },

  _buildToolbar(mailbox, entities) {
    const toolbar = el("div", { class: "list-panel__toolbar" });
    const countLabel = el("span", { class: "small muted", text: "0 selected" });
    this._selectionCountLabel = countLabel;

    const allowBtn = el("button", { class: "btn btn--primary btn--sm", text: "Allow selected", attrs: { type: "button", disabled: "true" } });
    const ignoreBtn = el("button", { class: "btn btn--ghost btn--sm", text: "Ignore selected", attrs: { type: "button", disabled: "true" } });
    this._allowBtn = allowBtn;
    this._ignoreBtn = ignoreBtn;
    this._updateAllowButtonLabel();
    // The entity `<select>` node is created once in `_correlationSection`
    // and persists across every `_loadTable` reload (correlation runs,
    // batch decisions) — attach this listener only once, never once per
    // reload, or repeated reloads would stack up duplicate listeners on
    // the same still-live DOM node.
    if (this._entitySelect && !this._entityChangeListenerAdded) {
      this._entityChangeListenerAdded = true;
      this._entitySelect.addEventListener("change", () => {
        this._updateAllowButtonLabel();
        this._updateSelectionUI();
      });
    }

    allowBtn.addEventListener("click", () => this._batchDecide(mailbox, "ALLOW"));
    ignoreBtn.addEventListener("click", () => this._batchDecide(mailbox, "IGNORE"));

    // `.list-panel__toolbar` (style.css) is a `justify-content:
    // space-between` flex row built for exactly two groups (a label on
    // one side, controls on the other — Documents' own toolbar uses it
    // the same way) — group both buttons into ONE second child so this
    // reuse gets that same "label left, actions right" layout instead
    // of three items spread evenly across the row.
    const actionsGroup = el("div", { class: "field--inline" }, [allowBtn, ignoreBtn]);
    toolbar.appendChild(countLabel);
    toolbar.appendChild(actionsGroup);
    return toolbar;
  },

  _updateAllowButtonLabel() {
    if (!this._allowBtn) return;
    const selectedOption = this._entitySelect && this._entitySelect.selectedOptions && this._entitySelect.selectedOptions[0];
    const label = selectedOption ? selectedOption.textContent : "company";
    this._allowBtn.textContent = `Allow selected → ${label}`;
  },

  _updateSelectionUI() {
    const n = this._selected.size;
    if (this._selectionCountLabel) this._selectionCountLabel.textContent = `${n} selected`;
    if (this._allowBtn) this._allowBtn.disabled = n === 0 || !this._entitySelect || !this._entitySelect.value;
    if (this._ignoreBtn) this._ignoreBtn.disabled = n === 0;
  },

  _renderRows(tableWrap, mailbox, entities, filterText, sortKey, priorityFilter) {
    clear(tableWrap);
    const needle = (filterText || "").trim().toLowerCase();
    let rows = this._items.filter((item) => (item.metadata.sender_domain || "").toLowerCase().includes(needle));
    if (priorityFilter) {
      rows = rows.filter((item) => item.review_priority === priorityFilter);
    }
    const sortOpt = SORT_OPTIONS[sortKey] || SORT_OPTIONS.review_priority_desc;
    rows = rows.slice().sort(sortOpt.compare);

    if (rows.length === 0) {
      tableWrap.appendChild(emptyState("No domains match this filter."));
      return;
    }

    const table = el("table", { class: "doc-table domain-review__table" });
    const thead = el("thead", {}, [
      el("tr", {}, [
        el("th", { text: "" }),
        el("th", { text: "Priority" }),
        el("th", { text: "Sender domain" }),
        el("th", { text: "Candidates" }),
        el("th", { text: "With attachment" }),
        el("th", { text: "Attachment ratio" }),
        el("th", { text: "Strongest signal" }),
        el("th", { text: "First seen" }),
        el("th", { text: "Last seen" }),
        el("th", { text: "Xero correlation" }),
        el("th", { text: "Purchase invoices" }),
        el("th", { text: "Most recent purchase" }),
        el("th", { text: "Supplier reference" }),
        el("th", { text: "" }),
      ]),
    ]);
    table.appendChild(thead);

    const tbody = el("tbody");
    for (const item of rows) {
      tbody.appendChild(this._row(item, mailbox, entities));
    }
    table.appendChild(tbody);
    tableWrap.appendChild(table);
  },

  _row(item, mailbox, entities) {
    const m = item.metadata;
    const checkbox = el("input", { attrs: { type: "checkbox" } });
    checkbox.checked = this._selected.has(item.item_id);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) this._selected.add(item.item_id);
      else this._selected.delete(item.item_id);
      this._updateSelectionUI();
    });

    const detailsBtn = el("button", { class: "btn btn--ghost btn--sm", text: "Details", attrs: { type: "button" } });
    detailsBtn.addEventListener("click", () => this._openDetails(item, mailbox, entities));

    return el("tr", {}, [
      el("td", {}, [checkbox]),
      el("td", {}, [chip(item.review_priority || "LOW", REVIEW_PRIORITY_KIND[item.review_priority] || "neutral")]),
      el("td", { text: m.sender_domain || "—" }),
      el("td", { text: m.candidate_message_count != null ? String(m.candidate_message_count) : "—" }),
      el("td", { text: m.attachment_bearing_count != null ? String(m.attachment_bearing_count) : "—" }),
      el("td", { text: _attachmentRatioLabel(item) }),
      el("td", { text: _strongestSignalLabel(item) }),
      el("td", { text: fmtDateTime(m.first_seen_at) }),
      el("td", { text: fmtDateTime(m.last_seen_at) }),
      el("td", {}, [xeroCorrelationClassChip(m.xero_correlation_class)]),
      el("td", { text: m.xero_purchase_invoice_count != null ? String(m.xero_purchase_invoice_count) : "—" }),
      el("td", { text: m.xero_most_recent_purchase_date ? fmtDateTime(m.xero_most_recent_purchase_date) : "—" }),
      el("td", { text: m.xero_supplier_reference || "—" }),
      el("td", {}, [detailsBtn]),
    ]);
  },

  async _batchDecide(mailbox, decision) {
    if (this._selected.size === 0) return;
    const entityId = this._entitySelect ? this._entitySelect.value : null;
    if (decision === "ALLOW" && !entityId) {
      notify.error("Select a company before allowing selected domains.");
      return;
    }

    this._allowBtn.disabled = true;
    this._ignoreBtn.disabled = true;

    const items = Array.from(this._selected).map((itemId) => ({
      itemId,
      decision,
      destinationEntityId: decision === "ALLOW" ? entityId : null,
      destinationMode: decision === "ALLOW" ? "FIXED" : null,
    }));

    const { ok, status, body } = await batchResolveMailboxDomainReview(mailbox.mailbox_id, {
      actorId: getActorId(),
      items,
    });

    if (!ok || !body) {
      notify.error(`Batch ${decision.toLowerCase()} failed: ${errorMessage(status, body)}`);
      this._updateSelectionUI();
      return;
    }

    // Per-item success/failure — the endpoint's own response already
    // carries this (partial batch success is normal and expected, see
    // batch_resolve_mailbox_domain_review's own docstring); a blanket
    // "done" toast would hide a real per-item failure from the
    // operator, so this renders the actual per-item outcome list.
    clear(this._resultsHost);
    const domainByItemId = new Map(this._items.map((i) => [i.item_id, i.metadata.sender_domain]));
    for (const result of body.results) {
      const domain = domainByItemId.get(result.item_id) || result.item_id;
      const line = el("div", { class: `small domain-review__result domain-review__result--${result.ok ? "ok" : "bad"}` });
      line.textContent = result.ok
        ? `✓ ${domain} — ${decision === "ALLOW" ? "allowed" : "ignored"}.`
        : `✗ ${domain} — ${result.error_type || "error"}: ${result.error || "unknown error"}`;
      this._resultsHost.appendChild(line);
    }
    notify.ok(`${body.succeeded_count}/${body.count} domain(s) ${decision === "ALLOW" ? "allowed" : "ignored"}.`);

    this._selected = new Set();
    document.dispatchEvent(new CustomEvent("bagman:needs-you-changed"));
    // Reload the table — every succeeded item is now RESOLVED and drops
    // out of the default OPEN-only listing; failed items remain, still
    // OPEN, for the operator to retry or inspect via Details. Reuses
    // the entities list already fetched for this drawer instance
    // (`this._entitiesCache`, set by `_loadTable` itself) rather than a
    // second `GET /internal/entities` round trip.
    if (this._tableHost) await this._loadTable(this._tableHost, mailbox, this._entitiesCache);
  },

  // -------------------------------------------------------------
  // "Details" — capability 4: the full item + the same individual
  // Allow/Ignore controls the single-item resolve flow already offers.
  // -------------------------------------------------------------

  _openDetails(item, mailbox, entities) {
    drawer.open({
      title: `Domain review — ${item.metadata.sender_domain || item.item_id}`,
      render: (body) => this._renderDetails(body, item, mailbox, entities),
    });
  },

  _renderDetails(body, item, mailbox, entities) {
    const panel = el("div", { class: "domain-review-panel" });
    body.appendChild(panel);

    const backBtn = el("button", { class: "btn btn--ghost btn--sm", text: "← Back to domain review", attrs: { type: "button" } });
    backBtn.addEventListener("click", () => this.open(mailbox));
    panel.appendChild(backBtn);

    panel.appendChild(el("div", { class: "review-drawer__question", text: item.question }));

    const m = item.metadata;
    const metaSection = el("div", { class: "review-drawer__section" });
    metaSection.appendChild(el("h3", { text: "Discovery" }));
    metaSection.appendChild(this._metaRow("Sender domain", m.sender_domain));
    metaSection.appendChild(this._metaRow("Candidate messages", m.candidate_message_count));
    metaSection.appendChild(this._metaRow("With attachment", m.attachment_bearing_count));
    metaSection.appendChild(this._metaRow("First seen", fmtDateTime(m.first_seen_at)));
    metaSection.appendChild(this._metaRow("Last seen", fmtDateTime(m.last_seen_at)));
    metaSection.appendChild(this._metaRow("Discovery signal", m.confidence_reason));
    panel.appendChild(metaSection);

    const xeroSection = el("div", { class: "review-drawer__section" });
    xeroSection.appendChild(el("h3", { text: "Xero correlation" }));
    xeroSection.appendChild(el("div", {}, [xeroCorrelationClassChip(m.xero_correlation_class)]));
    if (m.xero_correlation_class) {
      xeroSection.appendChild(this._metaRow("Contact match", m.xero_contact_match ? "Yes" : "No"));
      xeroSection.appendChild(this._metaRow("Evidence source", m.xero_evidence_source || "—"));
      xeroSection.appendChild(this._metaRow("Purchase invoices", m.xero_purchase_invoice_count));
      xeroSection.appendChild(this._metaRow("Most recent purchase", m.xero_most_recent_purchase_date ? fmtDateTime(m.xero_most_recent_purchase_date) : "—"));
      // CD-6 second-correlation-source WO: real SPEND BankTransaction
      // evidence, shown alongside the invoice fields above — always
      // rendered when present, even when purchase-bill evidence won
      // the single `xero_correlation_class` (see supplier_correlation
      // .py's own documented tie-break: bank-spend facts are recorded
      // regardless of which class wins).
      if (m.xero_bank_spend_count) {
        xeroSection.appendChild(this._metaRow("Bank-spend transactions", m.xero_bank_spend_count));
        xeroSection.appendChild(this._metaRow("First bank spend", m.xero_bank_spend_first_date ? fmtDateTime(m.xero_bank_spend_first_date) : "—"));
        xeroSection.appendChild(this._metaRow("Most recent bank spend", m.xero_bank_spend_last_date ? fmtDateTime(m.xero_bank_spend_last_date) : "—"));
        xeroSection.appendChild(
          this._metaRow(
            "Bank-spend total",
            m.xero_bank_spend_total_amount != null && m.xero_bank_spend_currency
              ? `${m.xero_bank_spend_total_amount} ${m.xero_bank_spend_currency}`
              : "Mixed currencies — see Xero directly"
          )
        );
      }
      xeroSection.appendChild(this._metaRow("Supplier reference", m.xero_supplier_reference));
      xeroSection.appendChild(this._metaRow("Shared/public domain", m.xero_is_shared_public_domain ? "Yes" : "No"));
      xeroSection.appendChild(this._metaRow("Correlated at", fmtDateTime(m.xero_correlated_at)));
    } else {
      xeroSection.appendChild(
        el("p", { class: "muted small", text: "This domain has not been through a Xero correlation run yet." })
      );
    }
    panel.appendChild(xeroSection);

    panel.appendChild(this._individualDecisionSection(item, mailbox, entities));
  },

  _metaRow(label, value) {
    return el("div", { class: "small domain-review__meta-row" }, [
      el("span", { class: "muted", text: `${label}: ` }),
      el("span", { text: value === null || value === undefined || value === "" ? "—" : String(value) }),
    ]);
  },

  /** The SAME individual Allow/Ignore decision the single-item resolve
   * flow already offers (WO's own explicit instruction: reuse the
   * existing single-item resolve endpoint directly, never duplicate its
   * business logic) — this is the mailbox-scoped
   * `POST .../domain-review/{item_id}/resolve` endpoint
   * (`resolveMailboxDomainReviewItem` in mailbox-api.js), the one that
   * actually creates/updates the real `MailboxDomainRule` and
   * back-processes history on ALLOW; never the generic
   * `/internal/needs-you/{id}/resolve` endpoint, which has no concept
   * of a mailbox domain rule at all. */
  _individualDecisionSection(item, mailbox, entities) {
    const section = el("div", { class: "review-drawer__section" });
    section.appendChild(el("h3", { text: "Decide" }));

    const entitySelect = el(
      "select",
      { attrs: { id: "domain-review-detail-entity-select" } },
      [el("option", { attrs: { value: "" }, text: "Select a company…" })].concat(
        entities.map((entity) => el("option", { attrs: { value: entity.entity_id }, text: entity.display_name }))
      )
    );
    section.appendChild(el("label", { class: "field", text: "Allow → company" }, [entitySelect]));

    const statusEl = el("div", { class: "upload-status", attrs: { "aria-live": "polite" } });
    const allowBtn = el("button", { class: "btn btn--primary btn--sm", text: "Allow", attrs: { type: "button" } });
    const ignoreBtn = el("button", { class: "btn btn--ghost btn--sm", text: "Ignore", attrs: { type: "button" } });
    section.appendChild(el("div", { class: "review-drawer__actions" }, [allowBtn, ignoreBtn]));
    section.appendChild(statusEl);

    const decide = async (decision) => {
      if (decision === "ALLOW" && !entitySelect.value) {
        statusEl.dataset.kind = "bad";
        statusEl.textContent = "Select a company before allowing this domain.";
        return;
      }
      allowBtn.disabled = true;
      ignoreBtn.disabled = true;
      statusEl.dataset.kind = "progress";
      statusEl.textContent = "Saving…";

      const { ok, status, body: result } = await resolveMailboxDomainReviewItem(mailbox.mailbox_id, item.item_id, {
        decision,
        destinationEntityId: decision === "ALLOW" ? entitySelect.value : null,
        destinationMode: decision === "ALLOW" ? "FIXED" : null,
        actorId: getActorId(),
      });

      if (!ok || !result) {
        statusEl.dataset.kind = "bad";
        statusEl.textContent = `Could not save: ${errorMessage(status, result)}`;
        allowBtn.disabled = false;
        ignoreBtn.disabled = false;
        return;
      }

      notify.ok(decision === "ALLOW" ? "Domain allowed." : "Domain ignored.");
      document.dispatchEvent(new CustomEvent("bagman:needs-you-changed"));
      this.open(mailbox); // back to the (now-refreshed) table
    };

    allowBtn.addEventListener("click", () => decide("ALLOW"));
    ignoreBtn.addEventListener("click", () => decide("IGNORE"));

    return section;
  },

  // -------------------------------------------------------------
  // A small, reasonable summary for the generic Needs You review
  // drawer's own fallback branch (capability 5 — see
  // features/needs-you/needs-you.js::_renderReviewBody). NOT the full
  // batch-triage experience (that lives on the dedicated mailbox page
  // above) — just enough that a domain-review item reached via the
  // generic queue never shows an unhelpful "not supported" placeholder.
  // -------------------------------------------------------------

  renderGenericQueueSummary(bodyNode, item) {
    const m = item.metadata || {};
    const section = el("div", { class: "review-drawer__section" });
    section.appendChild(el("h3", { text: "Mailbox domain review" }));
    section.appendChild(
      el("p", {
        class: "muted small",
        text:
          "This item asks whether BAGMAN should trust email from a new sender domain. The full triage " +
          "view (Xero correlation, batch Allow/Ignore) lives on that mailbox's own Domain Review page — " +
          "open it from the Email tab.",
      })
    );
    section.appendChild(this._metaRow("Sender domain", m.sender_domain));
    section.appendChild(this._metaRow("Candidate messages", m.candidate_message_count));
    section.appendChild(this._metaRow("First seen", fmtDateTime(m.first_seen_at)));
    section.appendChild(this._metaRow("Last seen", fmtDateTime(m.last_seen_at)));
    if (m.xero_correlation_class) {
      section.appendChild(el("div", {}, [xeroCorrelationClassChip(m.xero_correlation_class)]));
      section.appendChild(this._metaRow("Supplier reference", m.xero_supplier_reference));
    }
    bodyNode.appendChild(section);
  },
};
