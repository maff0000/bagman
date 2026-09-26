// features/activity/activity.js — the concise Activity tab plus its
// drill-down detail (CD-6 Slice 1, PID §98.8). Default view is
// concise (one line per event); clicking a row expands the SAME
// `AuditEvent` fields already carried over the wire (correlation_id/
// causation_id/payload/actor_*/timestamps) — see
// `app/api/routers/activity.py`'s own module docstring for why the
// server sends the full record and this module owns the concise-vs-
// forensic rendering split, not a second server-side projection.
import { el, clear, qs } from "../../shared/dom.js";
import { fmtDateTime } from "../../shared/format.js";
import { chip } from "../../shared/chips.js";
import { loadingState, emptyState, errorState } from "../../shared/state.js";
import { listActivity } from "./activity-api.js";
// CD-6 Slice 5 WI-5 §44 — deep links from a classification event to
// its Document detail view. No import cycle: detail.js never imports
// activity.js.
import { Detail } from "../documents/detail.js";

//: event_type prefix/substring -> chip colour bucket, and a short
//: human label for the concise line — PID §98.8's own worked example
//: ("Adobe invoice processed automatically", "Matt approved Screwfix
//: receipt", ...). Deliberately keyword-matched rather than an exact
//: closed map — `event_type` is an open vocabulary (PID §13); an event
//: type this table has never seen still renders (falls back to its raw
//: name), it just does not get a tailored label.
const EVENT_LABELS = [
  ["NEEDS_YOU_ITEM_CREATED", "ok", (e) => `Needs You: new item raised`],
  ["NEEDS_YOU_ITEM_RESOLVED", "ok", (e) => `Needs You: item resolved by ${e.actor_id}`],
  ["NEEDS_YOU_ITEM_DISMISSED", "neutral", (e) => `Needs You: item dismissed by ${e.actor_id}`],
  ["INTAKE_COMPLETED", "ok", () => "Document uploaded and accepted"],
  ["INTAKE_REJECTED", "bad", () => "Upload rejected"],
  ["INTAKE_QUARANTINED", "warn", () => "Upload quarantined"],
  ["INTAKE_FAILED", "bad", () => "Upload processing failed"],
  ["EVIDENCE_REGISTERED", "ok", () => "Evidence registered"],
  ["ENTITY_REGISTERED", "neutral", (e) => `Entity registered: ${(e.payload && e.payload.canonical_name) || ""}`],
  // CD-6 Slice 5 WI-5 §42 — friendly classification/rule presentation.
  // Order matters: none of these five literal event_type strings is a
  // true prefix of another (verified deliberately — "EVIDENCE_CLASSIFIED"
  // vs "EVIDENCE_CLASSIFICATION_*" diverge at "...CLASSIFIE[D|C...]"),
  // so `describe()`'s own `startsWith` matching below never confuses
  // one for another regardless of array order.
  [
    "EVIDENCE_CLASSIFICATION_RULE_CREATED",
    "ok",
    (e) => `Learned classification rule: ${(e.payload && e.payload.document_type) || "?"} for ${(e.payload && e.payload.sender_scope_value) || "sender"}`,
  ],
  [
    "EVIDENCE_CLASSIFICATION_RULE_RETIRED",
    "neutral",
    (e) => `Retired classification rule for ${(e.payload && e.payload.sender_scope_value) || "sender"}`,
  ],
  [
    "EVIDENCE_CLASSIFICATION_CONFIRMED",
    "ok",
    // WO worked example: "Matt confirmed BROKER_STATEMENT" — rendered
    // with the event's own real actor_id rather than a hardcoded name,
    // so this reads correctly for whichever operator actually acted.
    (e) => `${e.actor_id || "Operator"} confirmed ${(e.payload && e.payload.new_document_type) || "the classification"}`,
  ],
  [
    "EVIDENCE_CLASSIFICATION_CORRECTED",
    "warn",
    (e) => {
      const p = e.payload || {};
      return `${e.actor_id || "Operator"} corrected ${p.previous_document_type || "?"} → ${p.new_document_type || "?"}`;
    },
  ],
  [
    "EVIDENCE_CLASSIFIED",
    "ok",
    (e) => {
      const p = e.payload || {};
      if (p.rule_id) return `BAGMAN classified a document as ${p.document_type || "?"} (learned rule)`;
      return `BAGMAN proposed ${p.document_type || "?"}`;
    },
  ],
];

//: WI-5 §44 — every classification-shaped `event_type` whose payload
//: carries a direct `evidence_id` reference (the AI/rule/confirm/
//: correct producers all do — see services/evidence/classification*.py's
//: own `record_audit_event(...)` call sites). Never RULE_CREATED/
//: RULE_RETIRED (no evidence_id on those payloads — WI-5 §44's own
//: "never create broken links for events lacking navigable subject
//: data").
const _EVIDENCE_DEEP_LINK_EVENT_TYPES = new Set([
  "EVIDENCE_CLASSIFIED",
  "EVIDENCE_CLASSIFICATION_CONFIRMED",
  "EVIDENCE_CLASSIFICATION_CORRECTED",
]);

function describe(event) {
  const match = EVENT_LABELS.find(([prefix]) => event.event_type.startsWith(prefix));
  if (match) return { kind: match[1], label: match[2](event) };
  return { kind: "neutral", label: event.event_type };
}

export const Activity = {
  _loaded: false,

  ensureLoaded() {
    if (this._loaded) return;
    this._loaded = true;
    this.load();
  },

  async load() {
    const list = qs("#activity-list");
    if (!list) return;
    clear(list);
    list.appendChild(loadingState("Loading activity…"));

    const { ok, status, body } = await listActivity({ limit: 50 });
    clear(list);
    if (!ok || !body) {
      list.appendChild(errorState(status, body, "Could not load activity"));
      return;
    }
    if (body.items.length === 0) {
      list.appendChild(emptyState("No activity yet."));
      return;
    }
    for (const event of body.items) {
      list.appendChild(this._row(event));
    }
  },

  _row(event) {
    const { kind, label } = describe(event);
    const row = el("div", { class: "activity-row" });
    const summaryChildren = [
      el("span", { class: "activity-row__time muted small", text: fmtDateTime(event.occurred_at) }),
      chip(label, kind),
    ];

    // WI-5 §44 — a deep link when the payload identifies a navigable
    // evidence_id; never a broken link for an event without one (e.g.
    // rule CREATED/RETIRED events, which carry rule scope instead —
    // shown in the expanded technical detail below, no invented rule
    // detail page).
    const evidenceId = _EVIDENCE_DEEP_LINK_EVENT_TYPES.has(event.event_type) ? event.payload && event.payload.evidence_id : null;
    if (evidenceId) {
      const openBtn = el("button", {
        class: "btn btn--ghost btn--sm",
        text: "Open document",
        attrs: { type: "button" },
      });
      openBtn.addEventListener("click", (e) => {
        e.stopPropagation(); // never also toggle the technical-detail expand
        Detail.openForEvidence(evidenceId);
      });
      summaryChildren.push(openBtn);
    }

    const summary = el("div", { class: "activity-row__summary" }, summaryChildren);
    const detail = el("dl", { class: "activity-row__detail detail-kv", attrs: { hidden: "true" } });
    const fields = [
      ["Event type", event.event_type],
      ["Subject", `${event.subject_type} ${event.subject_id}`],
      ["Actor", `${event.actor_type} / ${event.actor_id}`],
      ["Correlation ID", event.correlation_id],
      ["Causation ID", event.causation_id || "—"],
      ["Payload", JSON.stringify(event.payload)],
    ];
    for (const [k, v] of fields) {
      detail.appendChild(el("dt", { text: k }));
      detail.appendChild(el("dd", { text: v == null ? "—" : String(v) }));
    }
    summary.addEventListener("click", () => {
      detail.hidden = !detail.hidden;
    });
    row.appendChild(summary);
    row.appendChild(detail);
    return row;
  },
};
