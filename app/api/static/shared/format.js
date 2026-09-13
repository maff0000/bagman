// shared/format.js — formatting helpers (display only — no business
// meaning attached). CD-4 WI-4's fmtBytes/fmtDateTime/hashPrefix/
// statusBadge, relocated unchanged by CD-5 WI-4's modularisation, plus
// a second, distinct badge vocabulary for AIInvocation.status (PID
// §28) that features/ai/ reuses so the Documents AI panel and Ask
// BAGMAN never invent their own ad-hoc status colouring.

import { el } from "./dom.js";

export function fmtBytes(n) {
  if (n === null || n === undefined) return "—";
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let val = n;
  let i = -1;
  do {
    val /= 1024;
    i += 1;
  } while (val >= 1024 && i < units.length - 1);
  return `${val.toFixed(1)} ${units[i]}`;
}

export function fmtDateTime(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString();
  } catch {
    return iso;
  }
}

export function hashPrefix(contentHash) {
  if (!contentHash || !contentHash.value) return null;
  return contentHash.value.slice(0, 12) + "…";
}

function badge(text, group) {
  return el("span", { class: `badge badge--${group}`, text: text || "UNKNOWN" });
}

// Presentational grouping only (never a business decision the GUI
// makes about the record itself — just which color bucket a known,
// closed intake-state enum value falls into for the operator's eye).
const INTAKE_STATUS_GROUP = {
  RECEIVED: "progress",
  VALIDATING: "progress",
  ACCEPTED: "progress",
  REGISTERED: "ok",
  QUARANTINED: "warn",
  REJECTED: "bad",
  FAILED: "bad",
};

export function statusBadge(status) {
  return badge(status, INTAKE_STATUS_GROUP[status] || "progress");
}

// AIInvocation.status vocabulary (PID §28) — a DIFFERENT closed set
// from IntakeRecord.status above (REQUESTED/RUNNING/SUCCEEDED/FAILED/
// REJECTED), so deliberately its own map rather than folded into
// INTAKE_STATUS_GROUP, even though a couple of colour buckets coincide.
const INVOCATION_STATUS_GROUP = {
  REQUESTED: "progress",
  RUNNING: "progress",
  SUCCEEDED: "ok",
  FAILED: "bad",
  REJECTED: "warn",
};

export function invocationStatusBadge(status) {
  return badge(status, INVOCATION_STATUS_GROUP[status] || "progress");
}
