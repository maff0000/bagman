// shared/chips.js — the small coloured "status chip" primitive (CD-6
// Slice 1, PID §98.2's "restrained use of colour" doctrine). Distinct
// from `shared/format.js`'s `statusBadge`/`invocationStatusBadge`
// (which are pre-bound to ONE specific closed vocabulary each,
// IntakeRecord.status / AIInvocation.status) — `chip()` here is the
// generic, reusable primitive underneath: any feature that needs a
// small coloured pill (Needs You priority, Needs You item status,
// Activity event-type grouping, a future tab's own vocabulary) builds
// on THIS, rather than every feature inventing its own inline
// `el("span", {class: "badge badge--x"})` call. `shared/format.js`'s
// own two functions are left exactly as they are (no regression risk)
// — a later cleanup could rebuild them on top of this, but that is not
// this delivery's job.
import { el } from "./dom.js";

//: The only five colour "kinds" this GUI ever uses for a chip — PID
//: §98.2's own "restrained use of colour" instruction taken literally:
//: every feature's own vocabulary (Needs You priority, Needs You
//: status, ...) maps onto ONE of these five buckets, never invents a
//: sixth colour.
const KNOWN_KINDS = new Set(["neutral", "progress", "ok", "warn", "bad"]);

/** Build one small coloured pill. `kind` must be one of `KNOWN_KINDS`
 * (falls back to `"neutral"` for anything else — a chip must never
 * silently render with no styling class at all). */
export function chip(text, kind = "neutral") {
  const safeKind = KNOWN_KINDS.has(kind) ? kind : "neutral";
  return el("span", { class: `chip chip--${safeKind}`, text: text == null ? "" : String(text) });
}

//: Needs You priority -> chip kind (PID §98.5's own closed priority
//: vocabulary).
const PRIORITY_KIND = { HIGH: "bad", NORMAL: "progress", LOW: "neutral" };

export function priorityChip(priority) {
  return chip(priority || "NORMAL", PRIORITY_KIND[priority] || "progress");
}

//: NeedsYouItem.status -> chip kind (this delivery's own closed status
//: vocabulary — services/needs_you/needs_you.py's own STATUSES).
const NEEDS_YOU_STATUS_KIND = { OPEN: "warn", RESOLVED: "ok", DISMISSED: "neutral" };

export function needsYouStatusChip(status) {
  return chip(status || "OPEN", NEEDS_YOU_STATUS_KIND[status] || "neutral");
}

//: services.xero.supplier_correlation's own closed
//: `xero_correlation_class` vocabulary -> chip kind (CD-6
//: GUI-operations-foundation WO). STRONG gets its own distinct colour
//: (the operator's real "safe to batch-approve" signal); the shared-
//: domain class is deliberately `warn` (never `ok` — the architect's
//: own explicit "the domain cannot safely represent one supplier" rule,
//: see services/xero/supplier_correlation.py's own module docstring)
//: even though it also has real purchase history; CONTACT_ONLY/NONE
//: both read as ordinary/uncertain, not alarming.
//: CD-6 second-correlation-source WO: `STRONG` split into
//: `STRONG_PURCHASE_BILL` (renamed) and `STRONG_BANK_SPEND` (new) — see
//: services/xero/supplier_correlation.py's own module docstring. Both
//: read as "ok" (a real, positive match found) — this chip does not
//: distinguish evidence source by colour, only by label; the Details
//: drawer shows the full evidence breakdown.
const XERO_CORRELATION_CLASS_KIND = {
  STRONG_PURCHASE_BILL: "ok",
  STRONG_BANK_SPEND: "ok",
  CONTACT_ONLY: "progress",
  SHARED_DOMAIN_REQUIRES_MANUAL_REVIEW: "warn",
  NONE: "neutral",
};

const XERO_CORRELATION_CLASS_LABEL = {
  STRONG_PURCHASE_BILL: "Strong match (purchase bill)",
  STRONG_BANK_SPEND: "Strong match (bank spend)",
  CONTACT_ONLY: "Contact only",
  SHARED_DOMAIN_REQUIRES_MANUAL_REVIEW: "Shared domain — review",
  NONE: "No match",
};

/** `correlationClass`: one of `xero_correlation_class`'s closed values,
 * or `undefined`/`null` when a domain-review item has never been
 * through a correlation run yet (no `xero_*` metadata at all) — that
 * "not yet correlated" state renders as its own honest neutral chip,
 * never silently as `NONE` (which means something different: a real
 * correlation run happened and found nothing). */
export function xeroCorrelationClassChip(correlationClass) {
  if (!correlationClass) return chip("Not yet correlated", "neutral");
  return chip(XERO_CORRELATION_CLASS_LABEL[correlationClass] || correlationClass, XERO_CORRELATION_CLASS_KIND[correlationClass] || "neutral");
}
