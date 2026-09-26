// features/documents/classification-format.js — the shared
// classification-badge vocabulary and human-friendly label helpers
// (CD-6 Slice 5 WI-5 §12/§14). Used by features/documents/documents.js
// (list badge), features/documents/classification-panel.js (detail
// current-state panel), and the classification-review drawer
// (features/needs-you/classification-review.js) — ONE implementation
// of "how does BAGMAN talk about a classification decision", never one
// per caller.
import { chip } from "../../shared/chips.js";

//: WI-5 §12 — the closed classification badge vocabulary. Never a raw
//: confidence percentage rendered as a fake success badge (WO's own
//: explicit prohibition) — confidence is shown as its own labelled
//: field elsewhere (the detail panel only), never folded into this
//: badge's text/colour.
export function classificationBadge(currentClassification) {
  if (!currentClassification) return chip("Unclassified", "neutral");
  const { source, status } = currentClassification;
  if (source === "OPERATOR_ASSIGNED" && status === "CLASSIFIED") return chip("Confirmed", "ok");
  if (source === "DETERMINISTIC_RULE" && status === "CLASSIFIED") return chip("Rule", "ok");
  if (source === "AI_PROPOSAL" && status === "REVIEW_REQUIRED") return chip("Needs review", "warn");
  if (status === "UNCLASSIFIABLE") return chip("Unknown", "neutral");
  // Should not happen for a governed row (every real combination is
  // covered above) — render honestly rather than hide an unexpected
  // shape.
  return chip(currentClassification.document_type || "Unclassified", "neutral");
}

//: WI-5 §14 — human-friendly decision-source labels for the detail
//: panel's current-state section.
const SOURCE_LABEL = {
  OPERATOR_ASSIGNED: "Confirmed by you",
  DETERMINISTIC_RULE: "Learned rule",
  AI_PROPOSAL: "BAGMAN proposal",
};

export function sourceLabel(source) {
  return SOURCE_LABEL[source] || source || "—";
}

//: WI-5 §11 — "Decision state" column: whether a human decision is
//: required RIGHT NOW, distinct from the classification badge itself.
export function decisionStateLabel(row) {
  const review = row.classification_review;
  if (review && review.status === "OPEN") return "Needs your review";
  if (!row.current_classification) return "—";
  if (row.current_classification.source === "AI_PROPOSAL") return "Reviewed"; // resolved review, AI row superseded elsewhere
  return "Decided";
}

//: WI-5 §11 — the "Source" column summary: real, bounded, never a
//: per-row provenance fetch (mirrors the previous static-label
//: doctrine documents.js already used, now backed by real projection
//: fields instead of a hardcoded string).
export function sourceSummaryLabel(row) {
  if (row.sender_domain) return `Email · ${row.sender_domain}`;
  if (row.intake) return "Manual upload";
  return row.evidence_type || "—";
}
