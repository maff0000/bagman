// features/mailbox/email-activity.js — the "Email Activity" drawer
// (CD-6 mailbox-list GUI-completion WO, §104.6's own "Email Activity
// drill-down view" gap). Zero backend delta needed — every field
// rendered here already exists on `MailboxSweepRun.to_dict()` and is
// already returned in full by `GET /{mailbox_id}/{provider}/sweeps`
// (all three providers). This file is a pure GUI consumer.
//
// Structural precedent: mirrors `features/mailbox/domain-review.js`'s
// own `.open(mailbox)` drawer pattern exactly (see that file's own
// module docstring) — a `drawer.open({title, render})` call, a loading
// state while the fetch is in flight, then a real, understandable
// operational table, most-recent-first (the endpoint's own ordering —
// see `MailboxSweepRunRepository.list_runs`'s own docstring). Read-only:
// no action is offered from this view (a sweep run is a durable,
// terminal historical record — there is nothing to "resolve" here).
import { el, clear } from "../../shared/dom.js";
import { fmtDateTime } from "../../shared/format.js";
import { chip } from "../../shared/chips.js";
import { loadingState, emptyState, errorState } from "../../shared/state.js";
import * as drawer from "../../shell/drawer.js";

const STATUS_KIND = {
  RUNNING: "progress",
  SUCCEEDED: "ok",
  PARTIAL: "progress",
  FAILED: "bad",
};

function statRow(label, value) {
  return el("div", { class: "small muted", text: `${label}: ${value}` });
}

export const EmailActivity = {
  open(mailbox, providerAdapter) {
    drawer.open({
      title: `Email Activity — ${mailbox.display_name}`,
      render: (body) => this._render(body, mailbox, providerAdapter),
    });
  },

  async _render(body, mailbox, providerAdapter) {
    const panel = el("div", { class: "domain-review-panel" });
    body.appendChild(panel);
    panel.appendChild(loadingState("Loading sweep history…"));

    if (!providerAdapter || !providerAdapter.listSweeps) {
      clear(panel);
      panel.appendChild(emptyState("No sweep history available.", "This provider has no sweep-history feed wired yet."));
      return;
    }

    const { ok, status, body: result } = await providerAdapter.listSweeps(mailbox.mailbox_id);
    clear(panel);

    if (!ok || !result) {
      panel.appendChild(errorState(status, result, "Could not load sweep history"));
      return;
    }

    const runs = result.items || [];
    if (runs.length === 0) {
      panel.appendChild(
        emptyState("No sweeps have run yet.", "Use “Sweep now” on the mailbox card to run the first one.")
      );
      return;
    }

    for (const run of runs) {
      panel.appendChild(this._runCard(run));
    }
  },

  _runCard(run) {
    const card = el("div", { class: "card mailbox-card" });
    card.appendChild(
      el("div", { class: "card__title mailbox-card__header" }, [
        el("span", { text: `${run.trigger} sweep` }),
        el("div", { class: "mailbox-card__chips" }, [chip(run.status, STATUS_KIND[run.status] || "neutral")]),
      ])
    );

    const body = el("div", { class: "card__body" });
    body.appendChild(
      statRow("Started", fmtDateTime(run.started_at))
    );
    body.appendChild(
      statRow("Completed", run.completed_at ? fmtDateTime(run.completed_at) : "(still running)")
    );
    body.appendChild(statRow("Messages seen / new", `${run.messages_seen} / ${run.messages_new}`));
    body.appendChild(statRow("Evidence created", run.evidence_created));
    body.appendChild(statRow("Duplicates / quarantined / failures", `${run.duplicates} / ${run.quarantined} / ${run.failures}`));
    body.appendChild(
      statRow(
        "Domain gate — allowed / ignored / unknown",
        `${run.allowed_domain_messages} / ${run.ignored_domain_messages} / ${run.unknown_domain_messages}`
      )
    );
    body.appendChild(statRow("Likely financial candidates", run.likely_financial_candidates));
    body.appendChild(statRow("Messages with attachments", run.messages_with_attachments));
    if (run.unique_sender_domains) {
      body.appendChild(statRow("Unique sender domains", run.unique_sender_domains));
    }
    if (run.graph_throttle_retries) {
      body.appendChild(statRow("Throttle retries", run.graph_throttle_retries));
    }
    if (run.error_code || run.error_detail) {
      body.appendChild(chip(`${run.error_code || "ERROR"}: ${run.error_detail || ""}`, "bad"));
    }
    if (run.folders_attempted && run.folders_attempted.length > 0) {
      body.appendChild(
        el("div", {
          class: "small muted",
          text: `Folders: ${run.folders_attempted.map((f) => f.display_name || f.folder_id).join(", ")}`,
        })
      );
    }
    card.appendChild(body);
    return card;
  },
};
