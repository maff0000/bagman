// features/mailbox/mailboxes.js — the Email tab (CD-6 Slice 3: Mailbox
// Management). An operator-facing mailbox DEFINITION registry only —
// see services/mailbox/mailbox.py's own module docstring for the full,
// explicit out-of-scope list (no provider OAuth/IMAP login, no mail
// fetch, no "Sweep now"/"Test Graph"/"Test IMAP" button anywhere in
// this file, not even disabled). Mirrors features/xero/connections.js's
// exact style/conventions (DOM helpers, chips, notify, the tab-shell
// wiring pattern) — the closest existing precedent for "a per-row
// card list with Add/Edit/lifecycle actions".
import { el, clear, qs } from "../../shared/dom.js";
import { fmtDateTime } from "../../shared/format.js";
import { chip } from "../../shared/chips.js";
import { loadingState, emptyState, errorState } from "../../shared/state.js";
import { errorMessage } from "../../shared/api.js";
import { getActorId } from "../../shared/operator.js";
import * as notify from "../../shared/notify.js";
import * as drawer from "../../shell/drawer.js";
import { listEntities } from "../../shell/entities.js";
import {
  listMailboxes,
  createMailbox,
  updateMailbox,
  enableMailbox,
  disableMailbox,
  retireMailbox,
  connectMicrosoftMailbox,
  disconnectMicrosoftMailbox,
  sweepMicrosoftMailboxNow,
  listMicrosoftMessages,
  listDomainReviewItems,
} from "./mailbox-api.js";
import { DomainReview } from "./domain-review.js";

//: provider_kind (the governed, closed Python-level enum —
//: services/mailbox/mailbox.py::PROVIDER_KINDS) -> human-readable
//: label. The GUI NEVER renders the raw enum value directly.
const PROVIDER_LABELS = {
  MICROSOFT_GRAPH: "Microsoft 365",
  IMAP: "IMAP",
  GOOGLE_GMAIL: "Gmail",
};

function providerLabel(providerKind) {
  return PROVIDER_LABELS[providerKind] || providerKind;
}

//: MailboxSource.status -> chip kind (this delivery's own closed
//: lifecycle vocabulary — services/mailbox/mailbox.py::STATUSES).
const STATUS_KIND = { ACTIVE: "ok", DISABLED: "neutral", RETIRED: "neutral" };
const STATUS_LABEL = { ACTIVE: "Enabled", DISABLED: "Disabled", RETIRED: "Retired" };

//: connection_state -> an HONEST, never-scary phrase (architect
//: doctrine carried over from Xero's own "Xero not connected" honesty
//: requirement) — every mailbox in THIS slice is NOT_CONFIGURED for
//: its entire lifetime (see services/mailbox/mailbox.py's own module
//: docstring); the other three values are forward-declared vocabulary
//: for a future slice's real adapter work and are rendered here too,
//: defensively, so this GUI never breaks if that future slice starts
//: producing them before this file is revisited.
const CONNECTION_STATE_LABEL = {
  NOT_CONFIGURED: "Authentication not configured",
  AUTH_REQUIRED: "Authentication required",
  READY_FOR_CONNECTION: "Ready to connect",
  CONNECTED: "Connected",
};

function connectionStateLabel(connectionState) {
  return CONNECTION_STATE_LABEL[connectionState] || connectionState;
}

//: CD-6 Slice 4 — only MICROSOFT_GRAPH has a real adapter behind it.
//: NoustAI IMAP (and any future Gmail row) stays exactly as Slice 3
//: left it: honest NOT_CONFIGURED, no connect/sweep button of any kind
//: — "no dead controls, no fake availability" (architect doctrine,
//: mirrors features/xero/connections.js's own identical discipline).
function hasWorkingAdapter(providerKind) {
  return providerKind === "MICROSOFT_GRAPH";
}

export const Mailboxes = {
  _loaded: false,
  _entitiesById: null,

  ensureLoaded() {
    if (this._loaded) return;
    this._loaded = true;
    this.load();
  },

  async _entityDisplayName(entityId) {
    if (!entityId) return null;
    if (!this._entitiesById) {
      const entities = await listEntities();
      this._entitiesById = new Map(entities.map((e) => [e.entity_id, e.display_name]));
    }
    return this._entitiesById.get(entityId) || null;
  },

  async load() {
    const list = qs("#mailboxes-list");
    if (!list) return;
    clear(list);
    list.appendChild(loadingState("Loading mailboxes…"));

    const { ok, status, body } = await listMailboxes();
    if (!ok || !body) {
      clear(list);
      list.appendChild(errorState(status, body, "Could not load mailboxes"));
      return;
    }
    if (body.items.length === 0) {
      clear(list);
      list.appendChild(
        emptyState(
          "No mailboxes configured yet.",
          "Add a mailbox to register a source of evidence BAGMAN will monitor in a future delivery."
        )
      );
      return;
    }

    const cards = await Promise.all(body.items.map((m) => this._card(m)));
    clear(list);
    for (const card of cards) list.appendChild(card);
  },

  async _card(mailbox) {
    const card = el("div", { class: `card mailbox-card${mailbox.status === "ACTIVE" ? "" : " card--muted"}` });

    card.appendChild(
      el("div", { class: "card__title mailbox-card__header" }, [
        el("span", { text: mailbox.display_name }),
        el("div", { class: "mailbox-card__chips" }, [
          chip(STATUS_LABEL[mailbox.status] || mailbox.status, STATUS_KIND[mailbox.status] || "neutral"),
        ]),
      ])
    );

    const body = el("div", { class: "card__body" });
    body.appendChild(el("div", { class: "small", text: mailbox.email_address }));
    body.appendChild(el("div", { class: "small muted", text: providerLabel(mailbox.provider_kind) }));
    body.appendChild(
      el("div", { class: "small muted", text: connectionStateLabel(mailbox.connection_state) })
    );

    const entityName = await this._entityDisplayName(mailbox.default_entity_id);
    if (entityName) {
      body.appendChild(el("div", { class: "small muted", text: `Default company: ${entityName}` }));
    }

    // "Omit if never happened" (spec) — last_successful_sweep_at is
    // null until a real sweep has ever succeeded for this mailbox
    // (CD-6 Slice 4's own first real producer — see
    // services/mailbox/mailbox.py::record_microsoft_sweep_success).
    if (mailbox.last_successful_sweep_at) {
      body.appendChild(
        el("div", { class: "small muted", text: `Last swept: ${fmtDateTime(mailbox.last_successful_sweep_at)}` })
      );
    }
    card.appendChild(body);

    if (hasWorkingAdapter(mailbox.provider_kind)) {
      card.appendChild(await this._sweepSummary(mailbox));
    }

    const actions = el("div", { class: "card__actions" });

    const editBtn = el("button", { class: "btn btn--secondary btn--sm", text: "Edit", attrs: { type: "button" } });
    editBtn.addEventListener("click", () => this.openEditDrawer(mailbox));
    actions.appendChild(editBtn);

    if (mailbox.status !== "RETIRED") {
      const toggleLabel = mailbox.status === "ACTIVE" ? "Disable" : "Enable";
      const toggleBtn = el("button", { class: "btn btn--ghost btn--sm", text: toggleLabel, attrs: { type: "button" } });
      toggleBtn.addEventListener("click", () =>
        mailbox.status === "ACTIVE" ? this._disable(mailbox) : this._enable(mailbox)
      );
      actions.appendChild(toggleBtn);

      const retireBtn = el("button", { class: "btn btn--ghost btn--sm", text: "Remove", attrs: { type: "button" } });
      retireBtn.addEventListener("click", () => this._retire(mailbox));
      actions.appendChild(retireBtn);
    }

    // CD-6 GUI-operations-foundation WO — the domain-review batch-triage
    // page. Shown for ANY Microsoft-provider mailbox with at least one
    // OPEN MAILBOX_DOMAIN_REVIEW item, regardless of the mailbox's own
    // enabled/connected state (these items describe REAL history a
    // historical discovery sweep already produced — an operator should
    // still be able to see/triage them even while a mailbox is
    // temporarily disabled or disconnected; unlike "Sweep now"/"Connect",
    // this is not an action that requires a live connection to perform).
    if (hasWorkingAdapter(mailbox.provider_kind)) {
      const domainReviewCount = await this._domainReviewOpenCount(mailbox);
      if (domainReviewCount > 0) {
        const domainReviewBtn = el("button", {
          class: "btn btn--secondary btn--sm",
          text: `Domain Review (${domainReviewCount})`,
          attrs: { type: "button" },
        });
        domainReviewBtn.addEventListener("click", () => DomainReview.open(mailbox));
        actions.appendChild(domainReviewBtn);
      }
    }

    if (hasWorkingAdapter(mailbox.provider_kind) && mailbox.status === "ACTIVE") {
      // "No dead controls, no fake availability" — Sweep now is ONLY
      // ever rendered when genuinely CONNECTED (architect doctrine,
      // mirrors features/xero/connections.js's Sync-now button exactly).
      const canConnect = mailbox.connection_state !== "CONNECTED";
      const connectLabel = mailbox.connection_state === "AUTH_REQUIRED" || mailbox.connection_state === "ERROR"
        ? "Reconnect Microsoft 365"
        : "Connect Microsoft 365";
      if (canConnect) {
        const connectBtn = el("button", { class: "btn btn--primary btn--sm", text: connectLabel, attrs: { type: "button" } });
        connectBtn.addEventListener("click", () => this._connectMicrosoft(mailbox));
        actions.appendChild(connectBtn);
      } else {
        const sweepBtn = el("button", { class: "btn btn--primary btn--sm", text: "Sweep now", attrs: { type: "button" } });
        sweepBtn.addEventListener("click", () => this._sweepMicrosoft(mailbox));
        actions.appendChild(sweepBtn);

        const disconnectBtn = el("button", { class: "btn btn--ghost btn--sm", text: "Disconnect", attrs: { type: "button" } });
        disconnectBtn.addEventListener("click", () => this._disconnectMicrosoft(mailbox));
        actions.appendChild(disconnectBtn);
      }
    }
    card.appendChild(actions);

    return card;
  },

  /** How many OPEN `MAILBOX_DOMAIN_REVIEW` items this mailbox currently
   * has (CD-6 GUI-operations-foundation WO) — drives whether the
   * "Domain Review (N)" button even appears at all (see `_card()`
   * above: "no dead controls, no fake availability" applies here
   * exactly as it does to Connect/Sweep). Returns `0` on a fetch
   * failure — a transient error here should never crash the whole
   * mailbox card, it just means the button doesn't render this load
   * (the operator can still reach the items via the generic Needs You
   * queue in the meantime). */
  async _domainReviewOpenCount(mailbox) {
    const { ok, body } = await listDomainReviewItems(mailbox.mailbox_id);
    return ok && body ? body.count : 0;
  },

  /** A minimal, NON-classifying recent-mail summary line (architect
   * spec's hard scope boundary — see services/mailbox/sweep.py's own
   * module docstring: relevant/irrelevant, invoice/not-invoice,
   * account coding, What/Why — none of that exists here, not even a
   * stub). Only ever rendered for a mailbox with a real adapter. */
  async _sweepSummary(mailbox) {
    if (mailbox.connection_state !== "CONNECTED") return el("div", { class: "small muted" });
    const { ok, body } = await listMicrosoftMessages(mailbox.mailbox_id);
    if (!ok || !body || body.count === 0) {
      return el("div", { class: "small muted", text: "No messages swept yet." });
    }
    const wrap = el("div", { class: "small muted mailbox-card__recent" }, [
      el("div", { text: `${body.count} message(s) evidenced from this mailbox.` }),
    ]);
    for (const m of body.items.slice(0, 3)) {
      wrap.appendChild(
        el("div", {
          class: "small",
          text: `${fmtDateTime(m.received_at)} — ${m.sender_address || "(unknown sender)"} — ${m.subject || "(no subject)"}`,
        })
      );
    }
    return wrap;
  },

  async _connectMicrosoft(mailbox) {
    const { ok, status, body } = await connectMicrosoftMailbox(mailbox.mailbox_id, getActorId());
    if (!ok || !body || !body.authorize_url) {
      notify.error(`Could not start Microsoft connect: ${errorMessage(status, body)}`);
      return;
    }
    // Mirrors features/xero/connections.js's own OAuth-begin pattern
    // exactly — a full-page navigation to the real Microsoft consent
    // screen; the browser never receives a token here.
    window.location.href = body.authorize_url;
  },

  async _disconnectMicrosoft(mailbox) {
    const { ok, status, body } = await disconnectMicrosoftMailbox(mailbox.mailbox_id, getActorId());
    if (!ok) {
      notify.error(`Could not disconnect: ${errorMessage(status, body)}`);
      return;
    }
    notify.ok(`Disconnected ${mailbox.display_name} from Microsoft 365.`);
    this.load();
  },

  async _sweepMicrosoft(mailbox) {
    notify.ok("Sweep started…");
    const { ok, status, body } = await sweepMicrosoftMailboxNow(mailbox.mailbox_id, getActorId());
    if (!ok || !body) {
      notify.error(`Sweep request failed: ${errorMessage(status, body)}`);
      return;
    }
    if (body.status === "SUCCEEDED") {
      notify.ok(`Sweep complete — ${body.evidence_created} new item(s) evidenced.`);
    } else if (body.status === "PARTIAL") {
      notify.error(`Sweep partially completed — ${body.failures} failure(s). It will retry next time.`);
    } else {
      notify.error(`Sweep failed: ${body.error_code || body.status}`);
    }
    this.load();
  },

  openAddDrawer() {
    drawer.open({
      title: "Add mailbox",
      render: (body) => this._renderForm(body, null),
    });
  },

  openEditDrawer(mailbox) {
    drawer.open({
      title: "Edit mailbox",
      render: (body) => this._renderForm(body, mailbox),
    });
  },

  /** `existing`: `null` for Add, a `MailboxSource` for Edit. Mirrors
   * `needs-you.js`'s own review-drawer form-building style — plain DOM
   * construction, no template strings. The "enabled" toggle is a GUI
   * convenience only: `POST .../create` and `PUT .../update` never
   * accept an `enabled`/`status` field at all (see
   * app/api/routers/mailboxes.py's own docstring) — after a successful
   * create/update, this form issues a SEPARATE enable/disable call only
   * if the toggle's value actually differs from the mailbox's current
   * state, exactly mirroring how this GUI already treats lifecycle
   * transitions as their own dedicated actions everywhere else. */
  async _renderForm(body, existing) {
    const nameInput = el("input", {
      attrs: { type: "text", id: "mailbox-form-name", autocomplete: "off", value: existing ? existing.display_name : "" },
    });
    const emailInput = el("input", {
      attrs: {
        type: "email",
        id: "mailbox-form-email",
        autocomplete: "off",
        placeholder: "matt@example.com",
        value: existing ? existing.email_address : "",
      },
    });

    const providerSelect = el(
      "select",
      { attrs: { id: "mailbox-form-provider" } },
      Object.entries(PROVIDER_LABELS).map(([value, label]) => el("option", { attrs: { value }, text: label }))
    );
    if (existing) providerSelect.value = existing.provider_kind;

    const enabledInput = el("input", {
      attrs: { type: "checkbox", id: "mailbox-form-enabled" },
    });
    enabledInput.checked = existing ? existing.status === "ACTIVE" : true;

    const entitySelect = el("select", { attrs: { id: "mailbox-form-entity" } }, [
      el("option", { attrs: { value: "" }, text: "No default company" }),
    ]);
    const entities = await listEntities();
    for (const entity of entities) {
      entitySelect.appendChild(el("option", { attrs: { value: entity.entity_id }, text: entity.display_name }));
    }
    if (existing && existing.default_entity_id) entitySelect.value = existing.default_entity_id;

    const statusEl = el("div", { class: "upload-status", attrs: { "aria-live": "polite" } });
    const saveBtn = el("button", {
      class: "btn btn--primary",
      text: existing ? "Save changes" : "Add mailbox",
      attrs: { type: "button" },
    });

    body.appendChild(
      el("div", { class: "review-drawer__section" }, [
        el("p", {
          class: "muted small",
          text:
            "A mailbox is a source of evidence — not a company, ledger, or invoice owner. " +
            "The default company below is only a display hint for later; it never assigns ownership automatically.",
        }),
        el("label", { class: "field", text: "Display name" }, [nameInput]),
        el("label", { class: "field", text: "Email address" }, [emailInput]),
        el("label", { class: "field", text: "Provider" }, [providerSelect]),
        el("label", { class: "field field--inline" }, [enabledInput, el("span", { text: "Enabled" })]),
        el("label", { class: "field", text: "Default company (optional hint)" }, [entitySelect]),
        el("div", { class: "review-drawer__actions" }, [saveBtn]),
        statusEl,
      ])
    );

    saveBtn.addEventListener("click", async () => {
      const displayName = nameInput.value.trim();
      const emailAddress = emailInput.value.trim();
      if (!displayName || !emailAddress) {
        statusEl.dataset.kind = "bad";
        statusEl.textContent = "Display name and email address are both required.";
        return;
      }
      saveBtn.disabled = true;
      statusEl.dataset.kind = "progress";
      statusEl.textContent = "Saving…";

      const actorId = getActorId();
      const formValues = {
        displayName,
        emailAddress,
        providerKind: providerSelect.value,
        defaultEntityId: entitySelect.value || null,
        actorId,
      };

      const { ok, status, body: result } = existing
        ? await updateMailbox(existing.mailbox_id, formValues)
        : await createMailbox(formValues);

      if (!ok || !result) {
        statusEl.dataset.kind = "bad";
        statusEl.textContent = `Could not save: ${errorMessage(status, result)}`;
        saveBtn.disabled = false;
        return;
      }

      // Reconcile the "Enabled" toggle against the saved record's
      // actual current state — a separate call only when it changed
      // (see this method's own docstring).
      const wantsEnabled = enabledInput.checked;
      const isEnabled = result.status === "ACTIVE";
      let final = result;
      if (wantsEnabled !== isEnabled) {
        const toggle = wantsEnabled ? await enableMailbox(result.mailbox_id, actorId) : await disableMailbox(result.mailbox_id, actorId);
        if (toggle.ok && toggle.body) final = toggle.body;
      }

      notify.ok(existing ? `Saved ${final.display_name}.` : `Added ${final.display_name}.`);
      drawer.close();
      this.load();
    });
  },

  async _enable(mailbox) {
    const { ok, status, body } = await enableMailbox(mailbox.mailbox_id, getActorId());
    if (!ok) {
      notify.error(`Could not enable: ${errorMessage(status, body)}`);
      return;
    }
    notify.ok(`Enabled ${mailbox.display_name}.`);
    this.load();
  },

  async _disable(mailbox) {
    const { ok, status, body } = await disableMailbox(mailbox.mailbox_id, getActorId());
    if (!ok) {
      notify.error(`Could not disable: ${errorMessage(status, body)}`);
      return;
    }
    notify.ok(`Disabled ${mailbox.display_name}.`);
    this.load();
  },

  /** "Remove" = retire (see services/mailbox/mailbox.py's own module
   * docstring — the row is preserved, never physically deleted). A
   * real confirmation step (spec requirement): this GUI has no
   * existing modal-confirm pattern to mirror (checked
   * connections.js/needs-you.js — neither uses one for its own
   * destructive-looking action), so a plain native `window.confirm`
   * is used here as the simplest honest "are you sure", naming exactly
   * what happens (never re-enableable from this slice's GUI). */
  async _retire(mailbox) {
    const confirmed = window.confirm(
      `Remove "${mailbox.display_name}" (${mailbox.email_address})? ` +
        "This retires the mailbox — it stops appearing as active, but its record is kept. " +
        "It cannot be re-enabled from here afterwards."
    );
    if (!confirmed) return;

    const { ok, status, body } = await retireMailbox(mailbox.mailbox_id, getActorId());
    if (!ok) {
      notify.error(`Could not remove: ${errorMessage(status, body)}`);
      return;
    }
    notify.ok(`Removed ${mailbox.display_name}.`);
    this.load();
  },
};
