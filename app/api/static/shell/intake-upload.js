// shell/intake-upload.js — the ONE shared client-side helper that
// drives `POST /internal/intake/evidence` end-to-end (CD-6 Slice 1,
// PID §98.3). Extracted so the new global `+ Add` upload modal
// (`shell/add-menu.js`) does not hand-roll a second copy of the
// multipart-request-plus-poll-to-terminal-state logic
// `features/documents/documents.js`'s own dropzone form already
// implements — this module IS that same governed-intake contract
// (multipart `file` + JSON `metadata` form field, a fresh
// `Idempotency-Key` per submit, poll `GET /internal/intake/{id}` while
// still `RECEIVED`/`VALIDATING`), factored out once so it has exactly
// one implementation.
//
// `features/documents/documents.js`'s own dropzone form is
// DELIBERATELY left using its own existing, already-tested
// implementation rather than refactored onto this helper as part of
// this delivery — that form was working and covered by
// `tests/acceptance/browser_acceptance_proof.py` before this slice
// began, and CD-6 Slice 1's own "do not regress any existing CD-5
// functionality" instruction weighs against touching it merely for
// DRY's sake. A later cleanup pass may consolidate the two once both
// have equal acceptance coverage; recorded here as a known, deliberate
// duplication rather than silently accepted.
//
// NO second/parallel upload code path is introduced by this module —
// it is a thin client of the exact same, single governed
// `POST /internal/intake/evidence` endpoint every other uploader in
// this GUI already calls (PID §98.3's own "There is no special
// image-upload bypass").
import { API, apiGet } from "../shared/api.js";
import { generateRequestId } from "../shared/uuid.js";

/**
 * @param {File} file
 * @param {object} opts
 * @param {string|null} opts.entityHint
 * @param {string|null} opts.evidenceType
 * @param {string} opts.actorId
 * @param {string|null} opts.note
 * @param {(status: string) => void} [opts.onProgress] - called with the
 *   real, honest in-flight status string as it changes (never a
 *   fabricated multi-step animation, PID §98.2's own "no fake buttons"
 *   spirit) — the caller renders whatever it likes from this.
 * @returns {Promise<{ok: boolean, httpStatus: number, intake: object|null, evidence: object|null, errorText: string|null}>}
 */
export async function submitIntakeUpload(file, { entityHint, evidenceType, actorId, note, onProgress }) {
  const metadata = {
    entity_hint: entityHint || null,
    evidence_type: evidenceType || null,
    actor_type: "USER",
    actor_id: actorId,
    note: note || null,
  };
  const formData = new FormData();
  formData.append("file", file);
  formData.append("metadata", JSON.stringify(metadata));

  // `crypto.randomUUID()` (not `generateRequestId()`) would throw here
  // outside a browser "secure context" — see shared/uuid.js's own
  // docstring for the real bug this caused and why this call must
  // never be the un-caught `crypto.randomUUID()` form again.
  const idempotencyKey = generateRequestId();

  if (onProgress) onProgress("Uploading…");

  let response;
  try {
    response = await fetch(API.intakeEvidence, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: formData,
    });
  } catch (networkErr) {
    return { ok: false, httpStatus: 0, intake: null, evidence: null, errorText: `Network error: ${networkErr.message}` };
  }

  let body = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }

  if (!body || !body.intake) {
    return {
      ok: false,
      httpStatus: response.status,
      intake: null,
      evidence: null,
      errorText: (body && (body.message || body.detail)) || `HTTP ${response.status}`,
    };
  }

  let record = body.intake;
  let evidence = body.evidence;
  let attempts = 0;
  while ((record.status === "RECEIVED" || record.status === "VALIDATING") && attempts < 8) {
    if (onProgress) onProgress(record.status);
    await new Promise((r) => setTimeout(r, 1000));
    const { ok, body: polled } = await apiGet(API.intakeOne(record.intake_id));
    if (ok && polled) record = polled;
    attempts += 1;
  }
  if (evidence == null && record.evidence_id) {
    const { ok, body: fetchedEvidence } = await apiGet(API.evidenceOne(record.evidence_id));
    if (ok) evidence = fetchedEvidence;
  }

  const ok = record.status === "REGISTERED" && evidence != null;
  let errorText = null;
  if (!ok) {
    if (record.status === "QUARANTINED") errorText = `Quarantined — ${record.quarantine_reason || "no reason recorded"}`;
    else if (record.status === "REJECTED") errorText = `Rejected — ${record.failure_code || "no reason recorded"}`;
    else if (record.status === "FAILED") errorText = `Failed — ${record.failure_code || "no reason recorded"}`;
    else errorText = `Still ${record.status.toLowerCase()} — check Documents shortly.`;
  }

  return { ok, httpStatus: response.status, intake: record, evidence, errorText };
}
