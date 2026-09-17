// features/xero/connections.js — the Settings/Connections tab (CD-6
// Slice 2, architect spec §16): "each BAGMAN company with its Xero
// connection state, last sync, account count and Sync now/Connect/
// Disconnect actions — no token details ever rendered." Extends Slice
// 1's existing tab-shell system (shell/shell.js's own `TABS` map) —
// never a second parallel tab mechanism.
import { el, clear } from "../../shared/dom.js";
import { fmtDateTime } from "../../shared/format.js";
import { chip } from "../../shared/chips.js";
import { loadingState, errorState } from "../../shared/state.js";
import { errorMessage } from "../../shared/api.js";
import { getActorId } from "../../shared/operator.js";
import * as notify from "../../shared/notify.js";
import { listEntities, refresh as refreshEntities } from "../../shell/entities.js";
import { getXeroStatus, connectXero, syncXeroNow, disconnectXero } from "./xero-api.js";

//: XeroConnection.status -> chip kind (this delivery's own closed
//: status vocabulary — services/xero/connection.py's own STATUSES).
//: `null` (no connection row at all) is handled separately below.
const STATUS_KIND = {
  PENDING: "progress",
  CONNECTED: "ok",
  DISCONNECTED: "neutral",
  REVOKED: "bad",
  ERROR: "bad",
};

export const Connections = {
  _loaded: false,

  ensureLoaded() {
    if (this._loaded) return;
    this._loaded = true;
    this.load();
  },

  async load() {
    const list = document.querySelector("#connections-list");
    if (!list) return;
    clear(list);
    list.appendChild(loadingState("Loading company connections…"));

    const entities = await listEntities();
    if (entities.length === 0) {
      clear(list);
      list.appendChild(errorState(0, null, "Could not load BAGMAN companies"));
      return;
    }

    // Honest per-company live status (account_count/staleness are not
    // carried on the cached entity list — see shell/entities.js) — a
    // handful of companies, so N small parallel requests is fine.
    const statuses = await Promise.all(entities.map((e) => getXeroStatus(e.entity_id)));

    clear(list);
    entities.forEach((entity, i) => {
      list.appendChild(this._card(entity, statuses[i]));
    });
  },

  _card(entity, statusResult) {
    const status = statusResult.ok && statusResult.body ? statusResult.body : null;
    const connection = status && status.connection;
    const card = el("div", { class: "card connection-card" });

    const kind = connection ? STATUS_KIND[connection.status] || "neutral" : "neutral";
    const label = connection ? connection.status : "NOT CONNECTED";

    card.appendChild(
      el("div", { class: "card__title connection-card__header" }, [
        el("span", { text: entity.display_name }),
        chip(label, kind),
      ])
    );

    const body = el("div", { class: "card__body" });
    if (connection && connection.tenant_name) {
      body.appendChild(el("div", { class: "small", text: `Xero organisation: ${connection.tenant_name}` }));
    }
    body.appendChild(
      el("div", {
        class: "small muted",
        text: connection && connection.last_successful_sync_at
          ? `Last synced: ${fmtDateTime(connection.last_successful_sync_at)}`
          : "Never synced",
      })
    );
    if (status) {
      // Architect finding, live acceptance run: rendered the literal
      // string "undefined account(s) synced" for an entity with no
      // XeroConnection at all — the backend now always includes
      // `account_count` (see app/api/routers/xero.py's own fix), but
      // this fallback stays as defence-in-depth against the same
      // class of bug ever reappearing from either side.
      const accountCount = typeof status.account_count === "number" ? status.account_count : 0;
      body.appendChild(
        el("div", { class: "small muted", text: `${accountCount} account(s) synced` })
      );
      if (status.reference_data_stale && connection && connection.status === "CONNECTED") {
        body.appendChild(el("div", { class: "small", text: "Reference data may be stale." }));
      }
    }
    if (connection && connection.status === "ERROR" && connection.error_detail) {
      body.appendChild(el("div", { class: "small", text: `Error: ${connection.error_detail}` }));
    }
    card.appendChild(body);

    const actions = el("div", { class: "card__actions" });
    const isConnected = connection && connection.status === "CONNECTED";
    const isPending = connection && connection.status === "PENDING";
    // Every non-CONNECTED state must offer a real operator recovery
    // path (architect finding, Slice 2 acceptance review — "do not
    // leave any non-terminal state without an operator recovery
    // path"). `PENDING` was the one real dead end: `POST
    // /internal/xero/connect` has always supported re-beginning a
    // flow already in `PENDING` (see `begin_connect`'s own docstring —
    // "re-clicking 'Connect' while a flow is already in flight" is an
    // explicitly designed, safe case, and the router mints a genuinely
    // fresh `state` on every call regardless of the connection's
    // current status), but the GUI never exposed a button for it,
    // silently stranding an operator whose browser tab from the first
    // attempt was closed, lost, or never completed.
    const canConnect = !connection || ["DISCONNECTED", "REVOKED", "ERROR", "PENDING"].includes(connection.status);
    // Wording is deliberately state-specific and truthful about what
    // the click does (spec: "use wording that truthfully reflects the
    // behaviour") rather than one generic "Connect" label for every
    // non-connected state.
    const connectLabel = !connection
      ? "Connect Xero"
      : isPending
        ? "Restart Xero connection"
        : connection.status === "ERROR"
          ? "Reconnect"
          : connection.status === "REVOKED" || connection.status === "DISCONNECTED"
            ? "Reconnect"
            : "Connect Xero";

    if (canConnect) {
      const connectBtn = el("button", { class: "btn btn--primary btn--sm", text: connectLabel, attrs: { type: "button" } });
      connectBtn.addEventListener("click", () => this._connect(entity));
      actions.appendChild(connectBtn);
    }
    if (isConnected) {
      const syncBtn = el("button", { class: "btn btn--secondary btn--sm", text: "Sync now", attrs: { type: "button" } });
      syncBtn.addEventListener("click", () => this._syncNow(entity));
      actions.appendChild(syncBtn);

      const disconnectBtn = el("button", { class: "btn btn--ghost btn--sm", text: "Disconnect", attrs: { type: "button" } });
      disconnectBtn.addEventListener("click", () => this._disconnect(entity));
      actions.appendChild(disconnectBtn);
    }
    card.appendChild(actions);

    return card;
  },

  async _connect(entity) {
    const { ok, status, body } = await connectXero(entity.entity_id, getActorId());
    if (!ok || !body) {
      notify.error(`Could not start connecting: ${errorMessage(status, body)}`);
      return;
    }
    // Real navigation to Xero's own consent screen (architect spec §3
    // — "the browser may initiate a connect flow") — this tab is left
    // and the operator returns to BAGMAN after Xero redirects back to
    // the server-side callback.
    window.location.href = body.authorize_url;
  },

  async _syncNow(entity) {
    notify.ok("Sync started…");
    const { ok, status, body } = await syncXeroNow(entity.entity_id, getActorId());
    if (!ok || !body) {
      notify.error(`Sync request failed: ${errorMessage(status, body)}`);
      return;
    }
    if (body.status === "SUCCEEDED") {
      notify.ok(`Sync complete — ${body.accounts_seen_count} account(s) seen.`);
    } else {
      notify.error(`Sync failed: ${body.error_code || body.status}`);
    }
    this.load();
  },

  async _disconnect(entity) {
    const { ok, status, body } = await disconnectXero(entity.entity_id, getActorId());
    if (!ok) {
      notify.error(`Could not disconnect: ${errorMessage(status, body)}`);
      return;
    }
    notify.ok(`Disconnected ${entity.display_name} from Xero.`);
    refreshEntities();
    this.load();
  },
};
