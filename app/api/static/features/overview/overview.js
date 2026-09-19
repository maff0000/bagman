// features/overview/overview.js — the Overview tab: liveness/
// readiness/version summary (CD-4 WI-4, relocated unchanged), plus
// this WI's own compact AI status area (PID §46-47): Claude operator
// availability, the background gateway's (honestly-labelled
// gateway-wide) reachability, and a small recent-activity glance.
import { el, clear, qs } from "../../shared/dom.js";
import { fmtDateTime } from "../../shared/format.js";
import { API, apiGet } from "../../shared/api.js";
import { getAiHealth, listRecentInvocations, countPendingInvocations } from "../ai/ai-api.js";
import { NeedsYou } from "../needs-you/needs-you.js";

//: PID §98.2's own worked example greeting is time-of-day-agnostic in
//: spirit ("Good morning Matt") — this GUI renders the REAL local
//: time-of-day greeting rather than hardcoding "morning" regardless of
//: when Matt actually opens BAGMAN (a small honesty detail: PID §98.2's
//: whole design doctrine is "no fake buttons"/no fabricated content,
//: and an afternoon "Good morning" would be exactly that).
function timeOfDayGreeting() {
  const hour = new Date().getHours();
  if (hour < 12) return "Good morning";
  if (hour < 18) return "Good afternoon";
  return "Good evening";
}

export const Overview = {
  async load() {
    await Promise.all([
      this._loadGreeting(),
      this._loadHealth(),
      this._loadReady(),
      this._loadVersion(),
      this._loadAiStatus(),
    ]);
  },

  // ---- greeting / Needs You summary (PID §98.2) ----

  async _loadGreeting() {
    const host = qs("#overview-greeting");
    if (!host) return;
    clear(host);

    const summary = await NeedsYou.getOpenSummary();

    host.appendChild(el("h1", { text: `${timeOfDayGreeting()} Matt` }));

    if (summary.total === null) {
      host.appendChild(
        el("p", { class: "muted", text: "Could not reach the Needs You queue — try refreshing." })
      );
      return;
    }

    if (summary.total === 0) {
      host.appendChild(el("p", { class: "overview-greeting__count", text: "Nothing needs your attention." }));
      host.appendChild(el("p", { class: "muted", text: "Everything else is running normally." }));
      return;
    }

    host.appendChild(
      el("p", {
        class: "overview-greeting__count",
        text: `${summary.total} thing${summary.total === 1 ? "" : "s"} need${summary.total === 1 ? "s" : ""} your attention`,
      })
    );
    const lines = NeedsYou.summaryLines(summary.byType);
    if (lines.length) {
      const list = el("ul", { class: "overview-greeting__lines" });
      for (const line of lines) list.appendChild(el("li", { text: line }));
      host.appendChild(list);
    }
    host.appendChild(el("p", { class: "muted", text: "Everything else is running normally." }));

    const goToNeedsYouBtn = el("button", {
      class: "btn btn--secondary",
      text: "Open Needs You",
      attrs: { type: "button", "data-goto-tab": "needs-you" },
    });
    host.appendChild(goToNeedsYouBtn);
  },

  async _loadHealth() {
    const card = qs('[data-card="health"] .status-line');
    const { ok, body } = await apiGet(API.health);
    clear(card);
    const alive = ok && body && body.status === "alive";
    card.appendChild(el("span", { class: `dot ${alive ? "dot--ok" : "dot--bad"}` }));
    card.appendChild(el("span", { class: "status-text", text: alive ? "alive" : "unreachable" }));
  },

  async _loadReady() {
    const card = qs('[data-card="ready"] .status-line');
    const { body } = await apiGet(API.ready);
    clear(card);
    const ready = !!(body && body.ready);
    card.appendChild(el("span", { class: `dot ${ready ? "dot--ok" : "dot--bad"}` }));
    const text = ready
      ? `ready (${body.runtime_environment || "?"})`
      : `not ready${body && body.failed_dependency ? ` — ${body.failed_dependency}` : ""}`;
    card.appendChild(el("span", { class: "status-text", text }));
  },

  async _loadVersion() {
    const dl = qs("#version-kv");
    const { ok, body } = await apiGet(API.version);
    clear(dl);
    if (!ok || !body) {
      dl.appendChild(el("dt", { text: "unavailable" }));
      return;
    }
    const rows = [
      ["git_commit", body.git_commit],
      ["build_version", body.build_version],
      ["schema_migration_version", body.schema_migration_version],
      ["runtime_environment", body.runtime_environment],
    ];
    for (const [k, v] of rows) {
      dl.appendChild(el("dt", { text: k }));
      dl.appendChild(el("dd", { text: v === null || v === undefined ? "—" : String(v) }));
    }
  },

  // ---- AI status (CD-5 WI-4, PID §46-48) ----

  async _loadAiStatus() {
    await Promise.all([this._loadAiHealth(), this._loadRecentActivity()]);
  },

  async _loadAiHealth() {
    const claudeLine = qs('[data-card="ai-claude"] .status-line');
    const aliasesBody = qs("#ai-aliases-body");
    clear(claudeLine);
    clear(aliasesBody);

    const { ok, body } = await getAiHealth();
    if (!ok || !body) {
      claudeLine.appendChild(el("span", { class: "dot dot--bad" }));
      claudeLine.appendChild(el("span", { class: "status-text", text: "unreachable" }));
      aliasesBody.appendChild(el("p", { class: "muted small", text: "AI health endpoint unreachable." }));
      return;
    }

    // CD-5 Gate-2 closure (2026-09-16): `claude_code` (the bounded
    // headless Claude Code operator runner) is the real, live signal
    // now — `claude` (the superseded direct-Anthropic path) is kept
    // reporting for now but is no longer what Ask BAGMAN actually
    // uses, so it is deliberately NOT shown here any more (see
    // app/api/routers/ai.py's own correction note on that key).
    const claudeCodeOk = body.checks.claude_code === "ok";
    claudeLine.appendChild(el("span", { class: `dot ${claudeCodeOk ? "dot--ok" : "dot--bad"}` }));
    claudeLine.appendChild(el("span", { class: "status-text", text: body.checks.claude_code || "unknown" }));

    const aliasKeys = Object.keys(body.checks).filter((k) => k !== "claude" && k !== "claude_code");
    for (const alias of aliasKeys) {
      const aliasOk = body.checks[alias] === "ok";
      aliasesBody.appendChild(
        el("div", { class: "status-line status-line--compact" }, [
          el("span", { class: `dot ${aliasOk ? "dot--ok" : "dot--bad"}` }),
          el("span", { class: "status-text", text: `${alias}: ${body.checks[alias]}` }),
        ])
      );
    }
    aliasesBody.appendChild(
      el("p", {
        class: "muted small",
        text:
          body.granularity ||
          "gateway-wide: these reflect one shared LiteLLM-gateway reachability signal, not independently-measured per-alias health.",
      })
    );
  },

  async _loadRecentActivity() {
    const recentBody = qs("#ai-recent-body");
    const pendingLine = qs("#ai-pending-line");
    clear(recentBody);
    clear(pendingLine);

    const [{ ok, body }, pending] = await Promise.all([listRecentInvocations({ limit: 5 }), countPendingInvocations()]);

    if (pending.ok) {
      pendingLine.appendChild(
        el("span", { text: `Pending analyses: ${pending.count}${pending.capped ? "+" : ""}` })
      );
    } else {
      pendingLine.appendChild(el("span", { class: "muted", text: "Pending analyses: unavailable" }));
    }

    if (!ok || !body || !body.items || body.items.length === 0) {
      recentBody.appendChild(el("p", { class: "muted small", text: "No AI activity yet." }));
      return;
    }

    const list = el("ul", { class: "ai-recent-list" });
    for (const invocation of body.items) {
      list.appendChild(
        el("li", {}, [
          el("span", { class: "ai-recent-list__task", text: `${invocation.task_id}` }),
          // TIMED_OUT/CANCELLED (CD-6 reliability delta, PID §100) join
          // FAILED/REJECTED's terminal-outcome pill styling rather than
          // falling through to the still-in-flight "muted" default.
          el("span", { class: `pill pill--${invocation.status === "SUCCEEDED" ? "ok" : ["FAILED", "REJECTED", "TIMED_OUT", "CANCELLED"].includes(invocation.status) ? "bad" : "muted"}`, text: invocation.status }),
          el("span", { class: "muted small", text: fmtDateTime(invocation.started_at) }),
        ])
      );
    }
    recentBody.appendChild(list);
  },
};
