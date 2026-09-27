// features/mailbox/message-triage.js — the "Messages" drawer (CD-6
// mailbox-list GUI-completion WO, gap map item 4: per-message triage).
//
// Zero backend delta for the data itself — every field an operator
// needs already exists across three already-shipped endpoints per
// provider: `GET .../messages` (MailboxMessage.to_dict()),
// `GET .../domain-review`/`GET .../security-review` (each Needs You
// item's own `source_object_reference` is the exact triggering
// message's `mailbox_message_id` — the real, already-correct linkage,
// confirmed in `services/mailbox/sweep.py`), and `GET .../domain-rules`
// (client-side matched against a message's own `sender_domain`/
// `sender_address`). This file joins all three client-side; no new
// canonical join/index is added server-side (a judgment call — see
// this WO's own delivery report: the per-mailbox item lists this joins
// against are small (dozens, not thousands), so a client-side scan is
// not the kind of "avoidable performance regression" the
// `count_discovery_candidates` backend delta exists to prevent).
//
// Structural precedent: mirrors `features/mailbox/domain-review.js`'s
// own drawer/table/details pattern exactly (loadingState/emptyState/
// errorState, `table-wrap`/`doc-table` classes, a list view with a
// per-row action opening a detail view within the SAME shared drawer
// mount point) — never a bespoke UI shape.
//
// Resolve reuse — never a second Needs-You-like mechanism
// ------------------------------------------------------------------
// Any action offered from this view calls the SAME governed resolve
// endpoint the existing dedicated pages already call:
//
// * Microsoft: literally reuses `DomainReview`'s own existing rendering
//   —`DomainReview._openDetails(item, mailbox, entities)` for a linked
//   domain-review item (the full Allow/Keep-checking/Ignore decision
//   surface, pre-selected to this one message's own item) and
//   `DomainReview._securityReviewRow(item, mailbox)` for a linked
//   security-review item — rather than rebuilding either flow a second
//   time. This is possible because Microsoft is the one provider that
//   already has a full "Domain Review" GUI page (`hasDomainReviewPage:
//   true` in `mailboxes.js`) to borrow this rendering from.
// * IMAP/Gmail: that full page does not exist yet for these two
//   providers (`hasDomainReviewPage: false` — a separate, pre-existing,
//   out-of-scope gap this WO does not build). Their own governed
//   resolve endpoints DO exist though (this WO's own
//   `resolveImapDomainReviewItem`/`resolveGmailDomainReviewItem`/
//   `resolveImapSecurityReviewItem`/`resolveGmailSecurityReviewItem`
//   wrappers) — this file calls those directly with a small,
//   proportionate action set, mirroring `DomainReview`'s own
//   `_individualDecisionSection`/`_securityReviewRow` shape (the same
//   three/two decisions, the same audit trail, the same endpoint) —
//   never a parallel decision mechanism, just a smaller rendering of
//   the identical governed call for the two providers that have no
//   larger page to open instead.
import { el, clear } from "../../shared/dom.js";
import { fmtDateTime, fmtBytes } from "../../shared/format.js";
import { chip } from "../../shared/chips.js";
import { loadingState, emptyState, errorState } from "../../shared/state.js";
import { errorMessage } from "../../shared/api.js";
import { getActorId } from "../../shared/operator.js";
import * as notify from "../../shared/notify.js";
import * as drawer from "../../shell/drawer.js";
import { listEntities } from "../../shell/entities.js";
import { Detail } from "../documents/detail.js";
import { DomainReview } from "./domain-review.js";
import {
  listDomainReviewItems,
  listImapDomainReviewItems,
  listGmailDomainReviewItems,
  listSecurityReviewItems,
  listImapSecurityReviewItems,
  listGmailSecurityReviewItems,
  resolveMailboxDomainReviewItem,
  resolveImapDomainReviewItem,
  resolveGmailDomainReviewItem,
  resolveSecurityReviewItem,
  resolveImapSecurityReviewItem,
  resolveGmailSecurityReviewItem,
} from "./mailbox-api.js";

//: provider_kind -> the domain-review/security-review list+resolve
//: wrappers `_PROVIDER_ADAPTERS` (mailboxes.js) does not itself carry
//: (that table only threads through connect/disconnect/sweep/test/
//: listMessages/listSweeps/listDomainRules/listDomainReviewItems — see
//: its own docstring). Built here rather than widening that shared
//: table, since these are needed ONLY by this file's own linked-item
//: lookup/decision flow.
const _DOMAIN_REVIEW_LIST_BY_PROVIDER = {
  MICROSOFT_GRAPH: listDomainReviewItems,
  IMAP: listImapDomainReviewItems,
  GOOGLE_GMAIL: listGmailDomainReviewItems,
};
const _SECURITY_REVIEW_LIST_BY_PROVIDER = {
  MICROSOFT_GRAPH: listSecurityReviewItems,
  IMAP: listImapSecurityReviewItems,
  GOOGLE_GMAIL: listGmailSecurityReviewItems,
};
const _DOMAIN_REVIEW_RESOLVE_BY_PROVIDER = {
  MICROSOFT_GRAPH: resolveMailboxDomainReviewItem,
  IMAP: resolveImapDomainReviewItem,
  GOOGLE_GMAIL: resolveGmailDomainReviewItem,
};
const _SECURITY_REVIEW_RESOLVE_BY_PROVIDER = {
  MICROSOFT_GRAPH: resolveSecurityReviewItem,
  IMAP: resolveImapSecurityReviewItem,
  GOOGLE_GMAIL: resolveGmailSecurityReviewItem,
};
//: Only Microsoft has a full, already-built "Details" decision page to
//: reuse wholesale (mirrors `mailboxes.js`'s own `hasDomainReviewPage`
//: doctrine exactly — see this file's own module docstring).
const _HAS_DOMAIN_REVIEW_PAGE = { MICROSOFT_GRAPH: true, IMAP: false, GOOGLE_GMAIL: false };

const INGESTION_STATUS_KIND = {
  INGESTED: "ok",
  QUARANTINED: "warn",
  FAILED: "bad",
  VANISHED: "neutral",
  CHECKED_NOT_CANDIDATE: "neutral",
  SECURITY_REVIEW: "bad",
};

function normalizeDomain(domain) {
  return (domain || "").trim().toLowerCase();
}

function domainInScope(candidateDomain, ruleDomain) {
  if (candidateDomain === ruleDomain) return true;
  return candidateDomain.endsWith(`.${ruleDomain}`);
}

/** Client-side "which MailboxDomainRule governs this message" match —
 * mirrors `services/mailbox/domain_rule.py::find_for_sender`'s own
 * most-specific-wins precedence (EXACT_ADDRESS > EXACT >
 * INCLUDE_SUBDOMAINS), DISPLAY-ONLY — this file never decides
 * anything from this match, it only shows the operator what already
 * governs this sender. */
function findGoverningRule(rules, message) {
  const domain = normalizeDomain(message.sender_domain);
  const address = (message.sender_address || "").trim().toLowerCase();
  const addressRule = rules.find((r) => r.match_mode === "EXACT_ADDRESS" && (r.sender_address || "").toLowerCase() === address);
  if (addressRule) return addressRule;
  const exactRule = rules.find((r) => r.match_mode === "EXACT" && normalizeDomain(r.sender_domain) === domain);
  if (exactRule) return exactRule;
  return rules.find(
    (r) => r.match_mode === "INCLUDE_SUBDOMAINS" && domain && domainInScope(domain, normalizeDomain(r.sender_domain))
  );
}

function metaRow(label, value) {
  return el("div", { class: "small domain-review__meta-row" }, [
    el("span", { class: "muted", text: `${label}: ` }),
    el("span", { text: value === null || value === undefined || value === "" ? "—" : String(value) }),
  ]);
}

export const MessageTriage = {
  /** Real entry point — called from `mailboxes.js`'s own mailbox card
   * "Messages" button. `providerAdapter` is the SAME `_PROVIDER_ADAPTERS`
   * entry `mailboxes.js` itself already resolved (mirrors
   * `EmailActivity.open`'s own signature exactly). */
  open(mailbox, providerAdapter) {
    drawer.open({
      title: `Messages — ${mailbox.display_name}`,
      render: (body) => this._renderList(body, mailbox, providerAdapter),
    });
  },

  async _renderList(body, mailbox, providerAdapter) {
    const panel = el("div", { class: "domain-review-panel" });
    body.appendChild(panel);
    panel.appendChild(
      el("p", {
        class: "muted small",
        text: "Every message BAGMAN has observed in this mailbox, most-recent-first. Open one to see its full context.",
      })
    );
    const listHost = el("div", {});
    panel.appendChild(listHost);
    listHost.appendChild(loadingState("Loading messages…"));

    if (!providerAdapter || !providerAdapter.listMessages) {
      clear(listHost);
      listHost.appendChild(errorState(null, null, "No message list is available for this provider."));
      return;
    }
    const { ok, status, body: result } = await providerAdapter.listMessages(mailbox.mailbox_id);
    clear(listHost);
    if (!ok || !result) {
      listHost.appendChild(errorState(status, result, "Could not load messages"));
      return;
    }
    const messages = result.items || [];
    if (messages.length === 0) {
      listHost.appendChild(emptyState("No messages observed yet.", "Messages appear here once a sweep has run."));
      return;
    }

    const tableWrap = el("div", { class: "table-wrap" });
    const table = el("table", { class: "doc-table" });
    table.appendChild(
      el("thead", {}, [
        el("tr", {}, ["Received", "Sender", "Subject", "Status", "Attachments", "Evidence", ""].map((h) => el("th", { text: h }))),
      ])
    );
    const tbody = el("tbody");
    for (const message of messages) {
      const viewBtn = el("button", { class: "btn btn--ghost btn--sm", text: "View", attrs: { type: "button" } });
      viewBtn.addEventListener("click", () => this._openMessage(mailbox, providerAdapter, message));
      tbody.appendChild(
        el("tr", {}, [
          el("td", { text: fmtDateTime(message.received_at) }),
          el("td", { text: message.sender_address || "(unknown sender)" }),
          el("td", { text: message.subject || "(no subject)" }),
          el("td", {}, [chip(message.ingestion_status, INGESTION_STATUS_KIND[message.ingestion_status] || "neutral")]),
          el("td", { text: message.has_attachments ? "Yes" : "No" }),
          el("td", { text: message.evidence_id ? "Yes" : "—" }),
          el("td", {}, [viewBtn]),
        ])
      );
    }
    table.appendChild(tbody);
    tableWrap.appendChild(table);
    listHost.appendChild(tableWrap);
  },

  _openMessage(mailbox, providerAdapter, message) {
    drawer.open({
      title: `Message — ${message.subject || "(no subject)"}`,
      render: (body) => this._renderMessage(body, mailbox, providerAdapter, message),
    });
  },

  async _renderMessage(body, mailbox, providerAdapter, message) {
    const panel = el("div", { class: "domain-review-panel" });
    body.appendChild(panel);

    const backBtn = el("button", { class: "btn btn--ghost btn--sm", text: "← Back to messages", attrs: { type: "button" } });
    backBtn.addEventListener("click", () => this.open(mailbox, providerAdapter));
    panel.appendChild(backBtn);

    panel.appendChild(this._identitySection(mailbox, message));
    panel.appendChild(this._evidenceSection(message));

    const ruleHost = el("div");
    const linkedHost = el("div");
    panel.appendChild(ruleHost);
    panel.appendChild(linkedHost);
    ruleHost.appendChild(loadingState("Loading governing rule…"));
    linkedHost.appendChild(loadingState("Looking up linked Needs You item…"));

    const rulesListFn = providerAdapter && providerAdapter.listDomainRules;
    const [rulesResult, entities, linkedItem] = await Promise.all([
      rulesListFn ? rulesListFn(mailbox.mailbox_id) : Promise.resolve({ ok: false, body: null }),
      listEntities(),
      this._findLinkedNeedsYouItem(mailbox, message),
    ]);

    clear(ruleHost);
    const rules = rulesResult.ok && rulesResult.body ? rulesResult.body.items : [];
    ruleHost.appendChild(this._ruleSection(findGoverningRule(rules, message), entities));

    clear(linkedHost);
    linkedHost.appendChild(this._linkedItemSection(mailbox, message, linkedItem, entities));
  },

  _identitySection(mailbox, message) {
    const section = el("div", { class: "review-drawer__section" });
    section.appendChild(el("h3", { text: "Message" }));
    section.appendChild(metaRow("Mailbox", mailbox.display_name));
    section.appendChild(metaRow("Subject", message.subject));
    section.appendChild(metaRow("Sender", message.sender_address));
    section.appendChild(metaRow("Sender domain", message.sender_domain));
    section.appendChild(metaRow("Received", fmtDateTime(message.received_at)));
    section.appendChild(metaRow("Folder", message.observed_folder_display_name || message.observed_folder));
    section.appendChild(
      el("div", {}, [chip(message.ingestion_status, INGESTION_STATUS_KIND[message.ingestion_status] || "neutral")])
    );
    section.appendChild(metaRow("Has attachments", message.has_attachments ? "Yes" : "No"));
    if (message.attachment_metadata && message.attachment_metadata.length) {
      const list = el("ul", { class: "small" });
      for (const a of message.attachment_metadata) {
        const size = a.size_bytes != null ? `, ${fmtBytes(a.size_bytes)}` : "";
        list.appendChild(el("li", { text: `${a.filename || "(unnamed)"} — ${a.content_type || "unknown type"}${size}` }));
      }
      section.appendChild(list);
    }
    if (message.auth_signals && Object.keys(message.auth_signals).length) {
      section.appendChild(
        metaRow(
          "Authentication (spf/dkim/dmarc)",
          Object.entries(message.auth_signals)
            .map(([k, v]) => `${k}=${v || "?"}`)
            .join(", ")
        )
      );
    }
    section.appendChild(
      metaRow("Discovery candidate", message.discovery_candidate === null ? "—" : message.discovery_candidate ? "Yes" : "No")
    );
    if (message.discovery_reason) section.appendChild(metaRow("Discovery reason", message.discovery_reason));
    if (message.discovery_checked_at) section.appendChild(metaRow("Discovery checked at", fmtDateTime(message.discovery_checked_at)));
    return section;
  },

  _evidenceSection(message) {
    const section = el("div", { class: "review-drawer__section" });
    section.appendChild(el("h3", { text: "Evidence" }));
    if (!message.evidence_id) {
      section.appendChild(el("p", { class: "muted small", text: "No evidence has been created from this message." }));
      return section;
    }
    section.appendChild(metaRow("Evidence ID", message.evidence_id));
    const viewBtn = el("button", { class: "btn btn--secondary btn--sm", text: "View evidence", attrs: { type: "button" } });
    viewBtn.addEventListener("click", () => Detail.openForEvidence(message.evidence_id));
    section.appendChild(el("div", { class: "review-drawer__actions" }, [viewBtn]));
    return section;
  },

  _ruleSection(rule, entities) {
    const section = el("div", { class: "review-drawer__section" });
    section.appendChild(el("h3", { text: "Governing rule" }));
    if (!rule) {
      section.appendChild(el("p", { class: "muted small", text: "No MailboxDomainRule governs this sender yet." }));
      return section;
    }
    section.appendChild(metaRow("Match mode", rule.match_mode));
    section.appendChild(metaRow("Policy", rule.policy));
    if (rule.policy === "MUST_READ") {
      const entity = entities.find((e) => e.entity_id === rule.destination_entity_id);
      section.appendChild(
        metaRow("Destination", rule.destination_mode === "FIXED" ? entity ? entity.display_name : rule.destination_entity_id : "Ask destination")
      );
    }
    if (rule.sender_address) section.appendChild(metaRow("Scoped to address", rule.sender_address));
    return section;
  },

  /** Client-side join — `NeedsYouItem.source_object_reference` is the
   * exact triggering message's own `mailbox_message_id` for BOTH
   * `MAILBOX_DOMAIN_REVIEW` (the first message that raised that domain's
   * own question) and `MAILBOX_AUTHENTICATION_ESCALATION` (always the
   * one failing message) items — see this file's own module docstring.
   * A domain-review question's OWN triggering message is the only one
   * this ever matches for that item (a later message from the same
   * still-open domain has no item of its own) — an honest consequence
   * of the underlying data model, not a bug in this join. */
  async _findLinkedNeedsYouItem(mailbox, message) {
    const domainListFn = _DOMAIN_REVIEW_LIST_BY_PROVIDER[mailbox.provider_kind];
    const securityListFn = _SECURITY_REVIEW_LIST_BY_PROVIDER[mailbox.provider_kind];
    if (!domainListFn && !securityListFn) return null;

    const calls = [];
    if (domainListFn) {
      calls.push(domainListFn(mailbox.mailbox_id, "OPEN"), domainListFn(mailbox.mailbox_id, "RESOLVED"));
    }
    if (securityListFn) {
      calls.push(securityListFn(mailbox.mailbox_id, "OPEN"), securityListFn(mailbox.mailbox_id, "RESOLVED"));
    }
    const results = await Promise.all(calls);
    const items = [].concat(...results.map((r) => (r.ok && r.body ? r.body.items : [])));
    return items.find((i) => i.source_object_reference === message.mailbox_message_id) || null;
  },

  _linkedItemSection(mailbox, message, item, entities) {
    const section = el("div", { class: "review-drawer__section" });
    section.appendChild(el("h3", { text: "Needs You item" }));
    if (!item) {
      section.appendChild(
        el("p", {
          class: "muted small",
          text:
            "No linked Needs You item — this message never triggered one directly (it may still be governed by " +
            "a domain-level question a DIFFERENT message from the same sender domain first raised).",
        })
      );
      return section;
    }
    section.appendChild(el("div", {}, [chip(item.status, item.status === "OPEN" ? "warn" : "ok")]));
    section.appendChild(metaRow("Question", item.question));
    if (item.metadata) section.appendChild(metaRow("Sender domain", item.metadata.sender_domain));

    const isDomainReview = item.item_type === "MAILBOX_DOMAIN_REVIEW";
    const providerKind = mailbox.provider_kind;

    if (_HAS_DOMAIN_REVIEW_PAGE[providerKind]) {
      // Microsoft — reuse DomainReview's own existing rendering
      // wholesale (this file's own module docstring: "never a second
      // Needs-You-like resolve mechanism").
      if (isDomainReview) {
        const openBtn = el("button", {
          class: "btn btn--primary btn--sm",
          text: item.status === "OPEN" ? "Decide" : "View decision",
          attrs: { type: "button" },
        });
        openBtn.addEventListener("click", () => DomainReview._openDetails(item, mailbox, entities));
        section.appendChild(el("div", { class: "review-drawer__actions" }, [openBtn]));
      } else {
        section.appendChild(DomainReview._securityReviewRow(item, mailbox));
      }
      return section;
    }

    // IMAP/Gmail — no full "Details" page exists yet for these
    // providers (module docstring); a small, proportionate decision
    // surface calling the SAME governed per-provider resolve endpoint
    // directly.
    if (item.status !== "OPEN") {
      section.appendChild(el("p", { class: "muted small", text: "This item has already been resolved." }));
      return section;
    }
    section.appendChild(this._inlineDecisionSection(mailbox, item, isDomainReview, entities));
    return section;
  },

  _inlineDecisionSection(mailbox, item, isDomainReview, entities) {
    const wrap = el("div", {});
    const statusEl = el("div", { class: "upload-status", attrs: { "aria-live": "polite" } });

    if (isDomainReview) {
      const entitySelect = el(
        "select",
        {},
        [el("option", { attrs: { value: "" }, text: "Select a company…" })].concat(
          entities.map((e) => el("option", { attrs: { value: e.entity_id }, text: e.display_name }))
        )
      );
      const allowBtn = el("button", { class: "btn btn--primary btn--sm", text: "Always Read", attrs: { type: "button" } });
      const grayBtn = el("button", { class: "btn btn--ghost btn--sm", text: "Keep checking with me", attrs: { type: "button" } });
      const ignoreBtn = el("button", { class: "btn btn--ghost btn--sm", text: "Ignore this source", attrs: { type: "button" } });
      wrap.appendChild(el("label", { class: "field", text: "Always Read → company" }, [entitySelect]));
      wrap.appendChild(el("div", { class: "review-drawer__actions" }, [allowBtn, grayBtn, ignoreBtn]));
      wrap.appendChild(statusEl);

      const resolveFn = _DOMAIN_REVIEW_RESOLVE_BY_PROVIDER[mailbox.provider_kind];
      const allButtons = [allowBtn, grayBtn, ignoreBtn];
      const decide = async (decision) => {
        if (decision === "ALLOW" && !entitySelect.value) {
          statusEl.dataset.kind = "bad";
          statusEl.textContent = "Select a company before choosing Always Read.";
          return;
        }
        allButtons.forEach((b) => (b.disabled = true));
        statusEl.dataset.kind = "progress";
        statusEl.textContent = "Saving…";
        const { ok, status, body: result } = await resolveFn(mailbox.mailbox_id, item.item_id, {
          decision,
          destinationEntityId: decision === "ALLOW" ? entitySelect.value : null,
          destinationMode: decision === "ALLOW" ? "FIXED" : null,
          actorId: getActorId(),
        });
        if (!ok || !result) {
          statusEl.dataset.kind = "bad";
          statusEl.textContent = `Could not save: ${errorMessage(status, result)}`;
          allButtons.forEach((b) => (b.disabled = false));
          return;
        }
        const message = {
          ALLOW: "BAGMAN will always read this source and will not ask again for ordinary future messages from it.",
          KEEP_GRAY: "This domain stays under review — BAGMAN will keep asking about it.",
          IGNORE: "BAGMAN will not raise this source again.",
        }[decision];
        notify.ok(message || "Saved.");
        document.dispatchEvent(new CustomEvent("bagman:needs-you-changed"));
        statusEl.dataset.kind = "ok";
        statusEl.textContent = "Saved.";
        allButtons.forEach((b) => (b.disabled = true));
      };
      allowBtn.addEventListener("click", () => decide("ALLOW"));
      grayBtn.addEventListener("click", () => decide("KEEP_GRAY"));
      ignoreBtn.addEventListener("click", () => decide("IGNORE"));
      return wrap;
    }

    // Security review — mirrors `DomainReview._securityReviewRow`'s own
    // two-decision shape exactly, just calling the IMAP/Gmail-specific
    // resolve wrapper instead of Microsoft's.
    const processBtn = el("button", { class: "btn btn--primary btn--sm", text: "Process this message once", attrs: { type: "button" } });
    const declineBtn = el("button", { class: "btn btn--ghost btn--sm", text: "Do not process", attrs: { type: "button" } });
    wrap.appendChild(el("div", { class: "review-drawer__actions" }, [processBtn, declineBtn]));
    wrap.appendChild(statusEl);

    const resolveFn = _SECURITY_REVIEW_RESOLVE_BY_PROVIDER[mailbox.provider_kind];
    const allButtons = [processBtn, declineBtn];
    const decide = async (decision) => {
      allButtons.forEach((b) => (b.disabled = true));
      statusEl.dataset.kind = "progress";
      statusEl.textContent = "Saving…";
      const { ok, status, body: result } = await resolveFn(mailbox.mailbox_id, item.item_id, { decision, actorId: getActorId() });
      if (!ok || !result) {
        statusEl.dataset.kind = "bad";
        statusEl.textContent = `Could not save: ${errorMessage(status, result)}`;
        allButtons.forEach((b) => (b.disabled = false));
        return;
      }
      notify.ok(
        decision === "PROCESS_THIS_MESSAGE_ONCE"
          ? "Message processed once — this never changes the source's own trust decision."
          : "Message left unprocessed. The source itself was never blacklisted."
      );
      document.dispatchEvent(new CustomEvent("bagman:needs-you-changed"));
      statusEl.dataset.kind = "ok";
      statusEl.textContent = "Saved.";
    };
    processBtn.addEventListener("click", () => decide("PROCESS_THIS_MESSAGE_ONCE"));
    declineBtn.addEventListener("click", () => decide("DO_NOT_PROCESS_THIS_MESSAGE"));
    return wrap;
  },
};
