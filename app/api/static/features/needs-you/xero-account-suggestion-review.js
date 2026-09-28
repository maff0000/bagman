// features/needs-you/xero-account-suggestion-review.js — the
// XERO_ACCOUNT_REQUIRED branch of the universal Needs You review drawer
// (BAGMAN accounting platform, `xero/account-suggestion-producer` WO).
// Mirrors features/needs-you/classification-review.js's own "separate
// file, imported into needs-you.js, exports a render function" shape
// exactly — this is NOT a second review modal/queue: it renders into
// the SAME drawer body `features/needs-you/needs-you.js::_renderReviewBody`
// already opened.
//
// No client-side eligibility/account-resolution logic: the eligible
// account list shown here is always a FRESH server fetch
// (`getXeroAccounts`), never the suggestion-time snapshot — the same
// discipline `services.xero.account_suggestion_resolution
// .resolve_account_suggestion` enforces server-side. No `innerHTML`
// anywhere — every evidence/model-derived string (signals, warnings,
// account names) renders via shared/dom.js's `el({text})`.
import { el, clear } from "../../shared/dom.js";
import { errorMessage } from "../../shared/api.js";
import { loadingState, emptyState } from "../../shared/state.js";
import { getActorId } from "../../shared/operator.js";
import * as notify from "../../shared/notify.js";
import * as drawer from "../../shell/drawer.js";
import { listEntities } from "../../shell/entities.js";
import { getInvocation } from "../ai/ai-api.js";
import { getXeroAccounts, getXeroStatus } from "../xero/xero-api.js";
import { resolveNeedsYouItem } from "./needs-you-api.js";
// Circular-import note: needs-you.js imports `XeroAccountSuggestionReview`
// from this file, and this file imports `NeedsYou` back from
// needs-you.js (to refresh the list after a successful resolve) — the
// same well-defined ES module cycle `classification-review.js` already
// documents; `NeedsYou` here is only ever touched inside the async
// submit handlers below, well after both modules have fully loaded.
import { NeedsYou } from "./needs-you.js";

function _accountLabel(account) {
  if (!account) return null;
  return `${account.code ? `[${account.code}] ` : ""}${account.name}`;
}

export const XeroAccountSuggestionReview = {
  async render(body, item) {
    const m = item.metadata || {};
    const evidenceId = m.evidence_id;
    const entityId = m.entity_id;

    // Note: the drawer's own generic "Original evidence" preview
    // (features/needs-you/needs-you.js::_renderReviewBody, run BEFORE
    // this branch) already rendered the source document above whatever
    // this function appends.

    const contextSection = el("div", { class: "review-drawer__section" });
    contextSection.appendChild(el("h3", { text: "Company & Xero connection" }));
    const entityRow = el("div", { class: "small domain-review__meta-row" }, [
      el("span", { class: "muted", text: "Company: " }),
      el("span", { text: "Loading…" }),
    ]);
    const tenantRow = el("div", { class: "small domain-review__meta-row" }, [
      el("span", { class: "muted", text: "Xero connection: " }),
      el("span", { text: "Loading…" }),
    ]);
    contextSection.appendChild(entityRow);
    contextSection.appendChild(tenantRow);
    body.appendChild(contextSection);

    let freshAccounts = [];
    let entityName = "—";
    let tenantName = "—";

    if (entityId) {
      const [entities, statusRes, accountsRes] = await Promise.all([
        listEntities(),
        getXeroStatus(entityId),
        getXeroAccounts(entityId, true),
      ]);
      const entity = entities.find((e) => e.entity_id === entityId);
      entityName = entity ? entity.display_name : entityId;
      if (statusRes.ok && statusRes.body) {
        tenantName = statusRes.body.connection ? statusRes.body.connection.tenant_name || "Connected" : "Not connected";
      }
      if (accountsRes.ok && accountsRes.body && accountsRes.body.connected) {
        freshAccounts = accountsRes.body.items || [];
      }
    }
    entityRow.lastChild.textContent = entityName;
    tenantRow.lastChild.textContent = tenantName;

    // ---- BAGMAN's suggestion ----
    const proposalSection = el("div", { class: "review-drawer__section" });
    proposalSection.appendChild(el("h3", { text: "BAGMAN's suggested account" }));
    const suggestedAccount = freshAccounts.find((a) => a.account_id === m.suggested_account_id);
    proposalSection.appendChild(
      this._metaRow("Suggested account", _accountLabel(suggestedAccount) || m.suggested_account_id || "—")
    );
    if (m.confidence != null) {
      proposalSection.appendChild(this._metaRow("Confidence", `${Math.round(m.confidence * 100)}%`));
    }
    body.appendChild(proposalSection);

    // ---- "Why BAGMAN thinks this" ----
    const whyHost = el("div", { class: "review-drawer__section" }, [loadingState("Loading BAGMAN's reasoning…")]);
    body.appendChild(whyHost);
    if (m.ai_invocation_id) {
      const { ok, body: invocation } = await getInvocation(m.ai_invocation_id);
      clear(whyHost);
      whyHost.appendChild(el("h3", { text: "Why BAGMAN thinks this" }));
      if (ok && invocation) {
        const output = invocation.output || {};
        const signals = output.signals || [];
        const warnings = output.warnings || [];
        if (signals.length === 0 && warnings.length === 0) {
          whyHost.appendChild(el("p", { class: "muted small", text: "No additional signals recorded for this suggestion." }));
        }
        if (signals.length > 0) {
          whyHost.appendChild(el("div", { class: "small", text: "Signals:" }));
          const list = el("ul", { class: "classification-panel__signal-list" });
          for (const s of signals) list.appendChild(el("li", { text: String(s) }));
          whyHost.appendChild(list);
        }
        if (warnings.length > 0) {
          whyHost.appendChild(el("div", { class: "small", text: "Warnings:" }));
          const list = el("ul", { class: "classification-panel__signal-list" });
          for (const w of warnings) list.appendChild(el("li", { text: String(w) }));
          whyHost.appendChild(list);
        }
      } else {
        whyHost.appendChild(el("p", { class: "muted small", text: "Could not load BAGMAN's reasoning for this suggestion." }));
      }
    } else {
      clear(whyHost);
    }

    // ---- Decide ----
    const decideSection = el("div", { class: "review-drawer__section" });
    decideSection.appendChild(el("h3", { text: "Decide" }));

    if (freshAccounts.length === 0) {
      decideSection.appendChild(
        emptyState("No eligible Xero accounts are currently available for this company — cannot resolve here.")
      );
      body.appendChild(decideSection);
      return;
    }

    const acceptBtn = el("button", {
      class: "btn btn--primary",
      text: suggestedAccount ? `Accept suggestion (${_accountLabel(suggestedAccount)})` : "Accept suggestion",
      attrs: { type: "button" },
    });
    acceptBtn.disabled = !suggestedAccount;
    decideSection.appendChild(el("div", { class: "review-drawer__actions" }, [acceptBtn]));

    decideSection.appendChild(el("p", { class: "muted small", text: "Or choose a different eligible account:" }));
    const accountSelect = el(
      "select",
      { attrs: { id: "review-xero-suggestion-account-select" } },
      [el("option", { attrs: { value: "" }, text: "Select an account…" })].concat(
        freshAccounts.map((a) => el("option", { attrs: { value: a.account_id }, text: _accountLabel(a) }))
      )
    );
    const chooseBtn = el("button", { class: "btn btn--secondary", text: "Use this account", attrs: { type: "button" } });
    decideSection.appendChild(el("label", { class: "field", text: "Account" }, [accountSelect]));
    decideSection.appendChild(el("div", { class: "review-drawer__actions" }, [chooseBtn]));

    const dismissBtn = el("button", { class: "btn btn--ghost", text: "Dismiss (decide later)", attrs: { type: "button" } });
    decideSection.appendChild(el("div", { class: "review-drawer__actions" }, [dismissBtn]));

    const statusEl = el("div", { class: "upload-status", attrs: { "aria-live": "polite" } });
    decideSection.appendChild(statusEl);
    body.appendChild(decideSection);

    const allButtons = [acceptBtn, chooseBtn, dismissBtn];
    const setBusy = (busy) => {
      for (const btn of allButtons) btn.disabled = busy;
      if (!busy && !suggestedAccount) acceptBtn.disabled = true;
    };

    const submitResolution = async (accountId) => {
      setBusy(true);
      statusEl.dataset.kind = "progress";
      statusEl.textContent = "Saving…";

      const { ok, status, body: result } = await resolveNeedsYouItem(item.item_id, {
        newStatus: "RESOLVED",
        resolution: { account_id: accountId },
        actorId: getActorId(),
      });

      if (ok) {
        notify.ok("Saved — account coding recorded.");
        drawer.close();
        NeedsYou.load();
        document.dispatchEvent(new CustomEvent("bagman:needs-you-changed"));
        return;
      }
      statusEl.dataset.kind = "bad";
      statusEl.textContent = `Could not save: ${errorMessage(status, result)}`;
      setBusy(false);
    };

    acceptBtn.addEventListener("click", () => {
      if (!suggestedAccount) return;
      submitResolution(suggestedAccount.account_id);
    });

    chooseBtn.addEventListener("click", () => {
      if (!accountSelect.value) {
        statusEl.dataset.kind = "bad";
        statusEl.textContent = "Select an account first.";
        return;
      }
      submitResolution(accountSelect.value);
    });

    dismissBtn.addEventListener("click", async () => {
      setBusy(true);
      const { ok, status, body: result } = await resolveNeedsYouItem(item.item_id, {
        newStatus: "DISMISSED",
        resolution: null,
        actorId: getActorId(),
      });
      if (ok) {
        notify.ok("Dismissed.");
        drawer.close();
        NeedsYou.load();
        document.dispatchEvent(new CustomEvent("bagman:needs-you-changed"));
      } else {
        statusEl.dataset.kind = "bad";
        statusEl.textContent = `Could not dismiss: ${errorMessage(status, result)}`;
        setBusy(false);
      }
    });
  },

  _metaRow(label, value) {
    return el("div", { class: "small domain-review__meta-row" }, [
      el("span", { class: "muted", text: `${label}: ` }),
      el("span", { text: value === null || value === undefined || value === "" ? "—" : String(value) }),
    ]);
  },
};
