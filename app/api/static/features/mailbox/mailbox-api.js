// features/mailbox/mailbox-api.js — the `/internal/mailboxes/*` HTTP
// surface (CD-6 Slice 3: Mailbox Management). Owned by the `mailbox`
// feature, same "feature owns its own endpoint map" pattern
// `features/xero/xero-api.js`/`features/needs-you/needs-you-api.js`
// already establish — never folded into `shared/api.js`.
import { apiGet, apiPost } from "../../shared/api.js";

export const MAILBOX_API = {
  list: "/internal/mailboxes",
  create: "/internal/mailboxes",
  one: (mailboxId) => `/internal/mailboxes/${encodeURIComponent(mailboxId)}`,
  update: (mailboxId) => `/internal/mailboxes/${encodeURIComponent(mailboxId)}`,
  enable: (mailboxId) => `/internal/mailboxes/${encodeURIComponent(mailboxId)}/enable`,
  disable: (mailboxId) => `/internal/mailboxes/${encodeURIComponent(mailboxId)}/disable`,
  retire: (mailboxId) => `/internal/mailboxes/${encodeURIComponent(mailboxId)}/retire`,
  //: CD-6 Slice 4 — first real Microsoft Graph adapter + sweep engine.
  //: Mirrors features/xero/xero-api.js's own endpoint-map shape exactly.
  microsoftConnect: (mailboxId) => `/internal/mailboxes/${encodeURIComponent(mailboxId)}/microsoft/connect`,
  microsoftDisconnect: (mailboxId) => `/internal/mailboxes/${encodeURIComponent(mailboxId)}/microsoft/disconnect`,
  microsoftSweep: (mailboxId) => `/internal/mailboxes/${encodeURIComponent(mailboxId)}/microsoft/sweep`,
  microsoftSweeps: (mailboxId) => `/internal/mailboxes/${encodeURIComponent(mailboxId)}/microsoft/sweeps`,
  microsoftMessages: (mailboxId) => `/internal/mailboxes/${encodeURIComponent(mailboxId)}/microsoft/messages`,
  //: CD-6 GUI-operations-foundation WO — the domain-review batch-triage
  //: surface (services.xero.supplier_correlation-assisted review of the
  //: real 90 OPEN MAILBOX_DOMAIN_REVIEW items a Phase A historical
  //: discovery sweep produced). `microsoftDomainReviewResolveOne` is the
  //: SAME governed single-item endpoint
  //: `app/api/routers/mailboxes_microsoft.py::resolve_mailbox_domain_review`
  //: already offers (it creates the real MailboxDomainRule and
  //: back-processes history on ALLOW) — used by the "Details" drawer's
  //: own individual Allow/Ignore controls, never the generic
  //: `/internal/needs-you/{id}/resolve` endpoint (that one has no idea
  //: what a `MailboxDomainRule` is).
  microsoftDomainReview: (mailboxId) => `/internal/mailboxes/${encodeURIComponent(mailboxId)}/microsoft/domain-review`,
  //: CD-6 GUI-operations-foundation follow-on WO (three-state
  //: MUST_READ/GRAYLIST/BLACKLIST operator-learning model) — the
  //: mailbox's own governed MailboxDomainRule list, used to show each
  //: domain-review row's CURRENT policy prominently (architect §7).
  microsoftDomainRules: (mailboxId) => `/internal/mailboxes/${encodeURIComponent(mailboxId)}/microsoft/domain-rules`,
  microsoftDomainReviewXeroCorrelate: (mailboxId) =>
    `/internal/mailboxes/${encodeURIComponent(mailboxId)}/microsoft/domain-review/xero-correlate`,
  microsoftDomainReviewResolveOne: (mailboxId, itemId) =>
    `/internal/mailboxes/${encodeURIComponent(mailboxId)}/microsoft/domain-review/${encodeURIComponent(itemId)}/resolve`,
  microsoftDomainReviewBatchResolve: (mailboxId) =>
    `/internal/mailboxes/${encodeURIComponent(mailboxId)}/microsoft/domain-review/batch-resolve`,
  //: CD-6 GUI-operations-foundation follow-on WO (item D) — the
  //: SECURITY_REVIEW resolution surface, its own dedicated endpoint
  //: mirroring the domain-review resolve endpoint's own shape.
  microsoftSecurityReview: (mailboxId) => `/internal/mailboxes/${encodeURIComponent(mailboxId)}/microsoft/security-review`,
  microsoftSecurityReviewResolveOne: (mailboxId, itemId) =>
    `/internal/mailboxes/${encodeURIComponent(mailboxId)}/microsoft/security-review/${encodeURIComponent(itemId)}/resolve`,
  //: CD-6 policy-rules-endpoint WO — the provider-neutral surface (lives
  //: under plain `/internal/mailboxes/*`, NOT `/microsoft/*`) for
  //: creating/updating one `MailboxDomainRule` directly, by its own
  //: exact identity, independent of any Needs You item. See
  //: `app/api/routers/mailboxes.py::upsert_mailbox_policy_rule`'s own
  //: docstring for the full behaviour.
  policyRules: (mailboxId) => `/internal/mailboxes/${encodeURIComponent(mailboxId)}/policy-rules`,
  //: CD-6 GUI-operations-foundation follow-on WO — the SECOND real
  //: mailbox connection lifecycle + sweep engine HTTP surface (plain
  //: IMAP adapter, `matt@noust.ai`). Mirrors every `microsoft*` entry
  //: above exactly — see `app/api/routers/mailboxes_imap.py`'s own
  //: module docstring for the one structural difference (`imapConnect`
  //: is a direct login attempt, never an OAuth redirect).
  imapConnect: (mailboxId) => `/internal/mailboxes/${encodeURIComponent(mailboxId)}/imap/connect`,
  imapDisconnect: (mailboxId) => `/internal/mailboxes/${encodeURIComponent(mailboxId)}/imap/disconnect`,
  imapSweep: (mailboxId) => `/internal/mailboxes/${encodeURIComponent(mailboxId)}/imap/sweep`,
  imapSweeps: (mailboxId) => `/internal/mailboxes/${encodeURIComponent(mailboxId)}/imap/sweeps`,
  imapMessages: (mailboxId) => `/internal/mailboxes/${encodeURIComponent(mailboxId)}/imap/messages`,
  imapDomainRules: (mailboxId) => `/internal/mailboxes/${encodeURIComponent(mailboxId)}/imap/domain-rules`,
  imapDomainReview: (mailboxId) => `/internal/mailboxes/${encodeURIComponent(mailboxId)}/imap/domain-review`,
  imapDomainReviewResolveOne: (mailboxId, itemId) =>
    `/internal/mailboxes/${encodeURIComponent(mailboxId)}/imap/domain-review/${encodeURIComponent(itemId)}/resolve`,
  imapDomainReviewBatchResolve: (mailboxId) =>
    `/internal/mailboxes/${encodeURIComponent(mailboxId)}/imap/domain-review/batch-resolve`,
  imapSecurityReview: (mailboxId) => `/internal/mailboxes/${encodeURIComponent(mailboxId)}/imap/security-review`,
  imapSecurityReviewResolveOne: (mailboxId, itemId) =>
    `/internal/mailboxes/${encodeURIComponent(mailboxId)}/imap/security-review/${encodeURIComponent(itemId)}/resolve`,
};

export function listMailboxes() {
  return apiGet(MAILBOX_API.list);
}

export function getMailbox(mailboxId) {
  return apiGet(MAILBOX_API.one(mailboxId));
}

export function createMailbox({ displayName, emailAddress, providerKind, defaultEntityId, actorId }) {
  return apiPost(MAILBOX_API.create, {
    display_name: displayName,
    email_address: emailAddress,
    provider_kind: providerKind,
    default_entity_id: defaultEntityId || null,
    actor_type: "USER",
    actor_id: actorId,
  });
}

/** `PUT` — see app/api/routers/mailboxes.py's own docstring: a full
 * metadata replace, never a partial patch. */
export function updateMailbox(mailboxId, { displayName, emailAddress, providerKind, defaultEntityId, actorId }) {
  return apiPut(MAILBOX_API.update(mailboxId), {
    display_name: displayName,
    email_address: emailAddress,
    provider_kind: providerKind,
    default_entity_id: defaultEntityId || null,
    actor_type: "USER",
    actor_id: actorId,
  });
}

export function enableMailbox(mailboxId, actorId) {
  return apiPost(MAILBOX_API.enable(mailboxId), { actor_type: "USER", actor_id: actorId });
}

export function disableMailbox(mailboxId, actorId) {
  return apiPost(MAILBOX_API.disable(mailboxId), { actor_type: "USER", actor_id: actorId });
}

export function retireMailbox(mailboxId, actorId) {
  return apiPost(MAILBOX_API.retire(mailboxId), { actor_type: "USER", actor_id: actorId });
}

//: CD-6 Slice 4 — the first real mailbox connection lifecycle + sweep
//: engine (Microsoft Graph adapter only). Browser never receives a
//: token/secret/delta-link from any of these — see
//: app/api/routers/mailboxes_microsoft.py's own module docstring.

export function connectMicrosoftMailbox(mailboxId, actorId) {
  return apiPost(MAILBOX_API.microsoftConnect(mailboxId), { actor_type: "USER", actor_id: actorId });
}

export function disconnectMicrosoftMailbox(mailboxId, actorId) {
  return apiPost(MAILBOX_API.microsoftDisconnect(mailboxId), { actor_type: "USER", actor_id: actorId });
}

export function sweepMicrosoftMailboxNow(mailboxId, actorId) {
  return apiPost(MAILBOX_API.microsoftSweep(mailboxId), { actor_type: "USER", actor_id: actorId });
}

export function listMicrosoftSweeps(mailboxId) {
  return apiGet(MAILBOX_API.microsoftSweeps(mailboxId));
}

export function listMicrosoftMessages(mailboxId) {
  return apiGet(MAILBOX_API.microsoftMessages(mailboxId));
}

//: CD-6 GUI-operations-foundation WO — domain-review batch-triage.

/** `status` defaults server-side to `"OPEN"` when omitted (see
 * app/api/routers/mailboxes_microsoft.py
 * ::list_microsoft_domain_review_items's own docstring) — pass an
 * explicit value (e.g. `"RESOLVED"`) only when the caller genuinely
 * wants a different slice. */
export function listDomainReviewItems(mailboxId, status) {
  const suffix = status ? `?status=${encodeURIComponent(status)}` : "";
  return apiGet(`${MAILBOX_API.microsoftDomainReview(mailboxId)}${suffix}`);
}

/** This mailbox's own governed `MailboxDomainRule` list — used purely
 * to show each domain's CURRENT policy (MUST_READ/GRAYLIST/BLACKLIST,
 * or "not yet reviewed" when no rule exists) alongside the domain-
 * review table (architect §7). */
export function listMicrosoftDomainRules(mailboxId) {
  return apiGet(MAILBOX_API.microsoftDomainRules(mailboxId));
}

/** Triggers one bounded Xero-assisted supplier-domain correlation run
 * for `mailboxId` against `entityId`'s Xero connection. The response
 * body's own `ok` field (NOT the HTTP status — this always resolves to
 * a normal 200 for the "Xero access itself failed" case, see the
 * endpoint's own docstring) tells the caller whether the correlation
 * itself actually succeeded; a genuine HTTP-level failure (bad
 * mailbox/entity, no Xero connection at all) still comes back as
 * `{ok: false}` at THIS wrapper's own `{ok, status, body}` envelope
 * level (`apiPost`'s standard contract) — callers must check both. */
export function runXeroCorrelation(mailboxId, { entityId, actorId }) {
  return apiPost(MAILBOX_API.microsoftDomainReviewXeroCorrelate(mailboxId), {
    entity_id: entityId,
    actor_type: "USER",
    actor_id: actorId,
  });
}

/** The single-item domain-review resolve call (creates/updates the real
 * `MailboxDomainRule` and, on ALLOW, immediately back-processes every
 * historical candidate for that domain — see the endpoint's own
 * docstring). `destinationMode`/`destinationEntityId` are required when
 * `decision === "ALLOW"`; omitted entirely for `"IGNORE"`.
 *
 * `senderAddress` (CD-6 GUI-operations-foundation follow-on WO, item A)
 * — required, and ONLY sent, when `matchMode === "EXACT_ADDRESS"`: scope
 * this rule to one specific, OBSERVED sender address rather than the
 * whole domain. The server independently re-validates it was actually
 * observed for this mailbox — this wrapper never second-guesses that. */
export function resolveMailboxDomainReviewItem(
  mailboxId,
  itemId,
  { decision, destinationEntityId, destinationMode, matchMode, processorHint, senderAddress, actorId }
) {
  return apiPost(MAILBOX_API.microsoftDomainReviewResolveOne(mailboxId, itemId), {
    actor_type: "USER",
    actor_id: actorId,
    decision,
    destination_entity_id: destinationEntityId || null,
    destination_mode: destinationMode || null,
    match_mode: matchMode || "EXACT",
    processor_hint: processorHint || null,
    sender_address: senderAddress || null,
  });
}

/** Batched version of `resolveMailboxDomainReviewItem` — `items` is an
 * array of `{itemId, decision, destinationEntityId, destinationMode,
 * matchMode, processorHint, senderAddress}` (camelCase in, snake_case on
 * the wire). Partial batch success is normal (see the endpoint's own
 * docstring) — this wrapper does no interpretation of `body.results`
 * itself; the caller (`features/mailbox/domain-review.js`) renders
 * per-item outcomes. */
export function batchResolveMailboxDomainReview(mailboxId, { actorId, items }) {
  return apiPost(MAILBOX_API.microsoftDomainReviewBatchResolve(mailboxId), {
    actor_type: "USER",
    actor_id: actorId,
    items: items.map((item) => ({
      item_id: item.itemId,
      decision: item.decision,
      destination_entity_id: item.destinationEntityId || null,
      destination_mode: item.destinationMode || null,
      match_mode: item.matchMode || "EXACT",
      processor_hint: item.processorHint || null,
      sender_address: item.senderAddress || null,
    })),
  });
}

//: CD-6 GUI-operations-foundation follow-on WO (item D) —
//: SECURITY_REVIEW resolution.

/** `status` defaults server-side to `"OPEN"` (mirrors
 * `listDomainReviewItems`'s own convention). */
export function listSecurityReviewItems(mailboxId, status) {
  const suffix = status ? `?status=${encodeURIComponent(status)}` : "";
  return apiGet(`${MAILBOX_API.microsoftSecurityReview(mailboxId)}${suffix}`);
}

/** `decision` is exactly `"PROCESS_THIS_MESSAGE_ONCE"` or
 * `"DO_NOT_PROCESS_THIS_MESSAGE"` — see the endpoint's own docstring for
 * exactly what each does. Never touches the governing `MailboxDomainRule`. */
export function resolveSecurityReviewItem(mailboxId, itemId, { decision, actorId }) {
  return apiPost(MAILBOX_API.microsoftSecurityReviewResolveOne(mailboxId, itemId), {
    actor_type: "USER",
    actor_id: actorId,
    decision,
  });
}

//: CD-6 policy-rules-endpoint WO.

/** Create/update one `MailboxDomainRule` directly, independent of any
 * Needs You item — the gap this closes: once a domain has already been
 * learned (a resolved `MAILBOX_DOMAIN_REVIEW` item), there was
 * previously no way to later add a more-specific address-level
 * override underneath it. Never touches a Needs You item, never
 * triggers historical back-processing (see the endpoint's own
 * docstring). `reason` is required, free-text, operator rationale —
 * goes only into the resulting audit event. Response carries
 * `was_no_op: true` when this exact request changed nothing (a
 * harmless re-submission — no misleading "changed" state to render). */
export function upsertMailboxPolicyRule(
  mailboxId,
  { matchMode, senderDomain, senderAddress, policy, destinationMode, destinationEntityId, processorHint, reason, actorId }
) {
  return apiPost(MAILBOX_API.policyRules(mailboxId), {
    actor_type: "USER",
    actor_id: actorId,
    match_mode: matchMode,
    sender_domain: senderDomain,
    sender_address: senderAddress || null,
    policy,
    destination_mode: destinationMode || null,
    destination_entity_id: destinationEntityId || null,
    processor_hint: processorHint || null,
    reason,
  });
}

//: CD-6 GUI-operations-foundation follow-on WO — plain IMAP
//: (`matt@noust.ai`) connection lifecycle + sweep. `connectImapMailbox`
//: is the ONE structural difference from its Microsoft counterpart —
//: no `authorize_url`/redirect; the response IS the updated
//: `MailboxSource` (see `app/api/routers/mailboxes_imap.py::connect_imap`'s
//: own docstring: "connect" here means "attempt a real login now").

export function connectImapMailbox(mailboxId, actorId) {
  return apiPost(MAILBOX_API.imapConnect(mailboxId), { actor_type: "USER", actor_id: actorId });
}

export function disconnectImapMailbox(mailboxId, actorId) {
  return apiPost(MAILBOX_API.imapDisconnect(mailboxId), { actor_type: "USER", actor_id: actorId });
}

export function sweepImapMailboxNow(mailboxId, actorId) {
  return apiPost(MAILBOX_API.imapSweep(mailboxId), { actor_type: "USER", actor_id: actorId });
}

export function listImapSweeps(mailboxId) {
  return apiGet(MAILBOX_API.imapSweeps(mailboxId));
}

export function listImapMessages(mailboxId) {
  return apiGet(MAILBOX_API.imapMessages(mailboxId));
}

export function listImapDomainReviewItems(mailboxId, status) {
  const suffix = status ? `?status=${encodeURIComponent(status)}` : "";
  return apiGet(`${MAILBOX_API.imapDomainReview(mailboxId)}${suffix}`);
}

export function listImapDomainRules(mailboxId) {
  return apiGet(MAILBOX_API.imapDomainRules(mailboxId));
}

/** `shared/api.js` exports `apiGet`/`apiPost` only (no `apiPut`) — this
 * mirrors `apiPost`'s exact shape/contract (never throws on a non-2xx
 * response; always resolves to `{ok, status, body}`) for the one PUT
 * this feature needs, rather than adding a generic PUT helper to the
 * shared module for a single caller. */
async function apiPut(path, jsonBody) {
  let res;
  try {
    res = await fetch(path, {
      method: "PUT",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(jsonBody),
    });
  } catch (networkErr) {
    return { ok: false, status: 0, body: null, networkError: networkErr.message };
  }
  let body = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  return { ok: res.ok, status: res.status, body };
}
