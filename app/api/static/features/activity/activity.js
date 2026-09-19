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
];

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
    const summary = el("div", { class: "activity-row__summary" }, [
      el("span", { class: "activity-row__time muted small", text: fmtDateTime(event.occurred_at) }),
      chip(label, kind),
    ]);
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
