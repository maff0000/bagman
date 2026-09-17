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
