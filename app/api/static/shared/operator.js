// shared/operator.js — the operator identity the NEW AI surfaces
// (Documents AI panel "Run analysis"/"Retry", Ask BAGMAN) attach to
// their own requests (`actor_type: "USER"`, `actor_id: <this>`).
//
// A deliberate, small, documented WI-4 judgment call: CD-4's upload
// form already has its own dedicated "Uploaded by (operator)" field
// (`#actor-id-input`, scoped to the upload workflow only — left
// completely untouched by this WI) — the AI surfaces below are reached
// from multiple places (the Documents detail panel's AI section, the
// Ask BAGMAN drawer reachable from anywhere in the shell, PID §42), so
// rather than duplicating a text field on every surface, one small
// shared, session-only identity lives here, editable from a single
// compact field in the shell header (see shell/shell.js). Exactly the
// same "plain operator label, not a cryptographic login" doctrine the
// upload form's own note already states — BAGMAN's API is
// loopback/private-by-default in this delivery (no production
// authentication).
const DEFAULT_ACTOR_ID = "matt";

let currentActorId = DEFAULT_ACTOR_ID;

export function getActorId() {
  return currentActorId;
}

export function setActorId(value) {
  const trimmed = (value || "").trim();
  currentActorId = trimmed || DEFAULT_ACTOR_ID;
  return currentActorId;
}

export function defaultActorId() {
  return DEFAULT_ACTOR_ID;
}
