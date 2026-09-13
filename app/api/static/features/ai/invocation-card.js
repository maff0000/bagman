// features/ai/invocation-card.js — the ONE place an `AIInvocation` is
// rendered as a small card, shared by the Documents AI panel
// (features/documents/detail.js) and Ask BAGMAN's own tool-call
// display (features/ai/ask-bagman.js) — PID §45's own field list
// (task, status, proposal, confidence, capability, actual model,
// completed, warnings, validation) plus PID §54's mandatory "Canonical
// vs AI-derived" visual distinction, in exactly one implementation so
// the two surfaces can never silently drift apart.
//
// PID §54 (non-negotiable): every card carries `.ai-card` (a distinct
// background/border/icon — see style.css) and an explicit "AI proposed
// — not canonical" label. This must never be mistaken for part of the
// canonical EvidenceItem/IntakeRecord record itself.

import { el } from "../../shared/dom.js";
import { fmtDateTime, invocationStatusBadge } from "../../shared/format.js";

//: Per-task "headline" output field (PID §24's output-shape
//: convention, `ai/tasks.py`'s own module docstring) — the one field
//: worth foregrounding above the generic confidence/signals/warnings
//: trio every CD-5 task output shares. Deliberately a plain lookup
//: table, not inferred from the output's own keys — an unrecognised
//: task_id (a future addition this GUI has not been taught about yet)
//: falls through to the generic "every other key" renderer below
//: rather than guessing.
const HEADLINE_FIELD_BY_TASK = {
  DOCUMENT_TYPE_PROPOSAL: { key: "proposed_type", label: "Proposed type" },
  DOCUMENT_SUMMARY: { key: "summary", label: "Summary" },
  ENTITY_PROPOSAL: { key: "proposed_entity_hint", label: "Proposed entity hint" },
  OPERATOR_DOCUMENT_REVIEW: { key: "decision_summary", label: "Decision summary" },
  ASK_BAGMAN: { key: "response_text", label: "Response" },
};

const COMMON_OUTPUT_KEYS = new Set(["confidence", "signals", "warnings"]);

function kv(label, value) {
  return el("div", { class: "ai-card__kv" }, [
    el("span", { class: "ai-card__kv-label", text: label }),
    el("span", { class: "ai-card__kv-value", text: value }),
  ]);
}

function renderList(items) {
  if (!items || items.length === 0) return el("span", { class: "muted", text: "none" });
  const ul = el("ul", { class: "ai-card__list" });
  for (const item of items) {
    ul.appendChild(el("li", { text: typeof item === "string" ? item : JSON.stringify(item) }));
  }
  return ul;
}

/** Exported so Ask BAGMAN's tool-call display (PID §25/§62 — "a
 * structured, auditable record of which registered tools were called")
 * renders warnings/signals identically to the Documents AI panel. */
export function renderWarningsList(warnings) {
  return renderList(warnings);
}

/** Ask BAGMAN's `output.tool_calls` (see `ai/tasks.py`'s
 * `_ASK_BAGMAN_OUTPUT_SCHEMA`) — `{tool, input, summary}` triples.
 * Never rendered via innerHTML; `input` is an arbitrary object handed
 * back from a governed tool call (not raw model prose), stringified
 * safely for display only. */
export function renderToolCalls(toolCalls) {
  if (!toolCalls || toolCalls.length === 0) return null;
  const wrap = el("div", { class: "tool-calls" });
  wrap.appendChild(el("div", { class: "tool-calls__title", text: "Tool calls" }));
  for (const call of toolCalls) {
    wrap.appendChild(
      el("div", { class: "tool-calls__item" }, [
        el("span", { class: "tool-calls__name", text: call.tool }),
        el("span", { class: "tool-calls__summary", text: call.summary || "" }),
      ])
    );
  }
  return wrap;
}

function renderOutput(output, taskId) {
  if (!output) return el("p", { class: "muted small", text: "No output recorded." });

  const body = el("div", { class: "ai-card__output" });
  const headline = HEADLINE_FIELD_BY_TASK[taskId];
  if (headline && Object.prototype.hasOwnProperty.call(output, headline.key)) {
    const value = output[headline.key];
    body.appendChild(
      el("div", { class: "ai-card__headline" }, [
        el("div", { class: "ai-card__headline-label", text: headline.label }),
        el("div", {
          class: "ai-card__headline-value",
          text: value === null || value === undefined ? "(no confident candidate)" : String(value),
        }),
      ])
    );
  }

  if (Object.prototype.hasOwnProperty.call(output, "confidence") && output.confidence !== null) {
    body.appendChild(kv("Confidence", `${Math.round(output.confidence * 100)}%`));
  }
  if (Object.prototype.hasOwnProperty.call(output, "signals")) {
    const row = el("div", { class: "ai-card__kv ai-card__kv--block" }, [
      el("span", { class: "ai-card__kv-label", text: "Signals" }),
    ]);
    row.appendChild(renderList(output.signals));
    body.appendChild(row);
  }

  // Any remaining, task-specific keys this card doesn't already know
  // about by name (forward-compatible with a future task's output
  // shape) — rendered generically rather than silently dropped.
  const known = new Set([headline ? headline.key : null, ...COMMON_OUTPUT_KEYS, "warnings"]);
  for (const [key, value] of Object.entries(output)) {
    if (known.has(key)) continue;
    body.appendChild(kv(key, typeof value === "string" ? value : JSON.stringify(value)));
  }

  return body;
}

/**
 * Render one `AIInvocation.to_dict()` as a small, visually-distinct
 * card (PID §45/§54). `options.onRetry(invocation)`, when supplied,
 * adds a "Retry" button — the caller decides whether that is offered
 * (features/documents/detail.js only offers it on a terminal
 * FAILED/REJECTED card).
 */
export function renderInvocationCard(invocation, options = {}) {
  const card = el("div", { class: "ai-card" });

  const header = el("div", { class: "ai-card__header" }, [
    el("span", { class: "ai-card__task", text: `${invocation.task_id} v${invocation.task_version}` }),
    invocationStatusBadge(invocation.status),
  ]);
  card.appendChild(header);

  card.appendChild(
    el("div", { class: "ai-card__provenance-flag" }, [
      el("span", { class: "ai-card__flag-icon", text: "✨" /* sparkle — AI-derived marker */ }),
      el("span", { text: "AI proposed — not canonical" }),
    ])
  );

  card.appendChild(renderOutput(invocation.output, invocation.task_id));

  const meta = el("div", { class: "ai-card__meta" });
  meta.appendChild(kv("Capability", invocation.capability_alias || "— (Claude operator)"));
  meta.appendChild(
    kv("Observed model", invocation.provider_model ? `${invocation.provider_model} (observed — not chosen)` : "—")
  );
  meta.appendChild(kv("Started", fmtDateTime(invocation.started_at)));
  meta.appendChild(kv("Completed", fmtDateTime(invocation.completed_at)));
  if (invocation.latency_ms !== null && invocation.latency_ms !== undefined) {
    meta.appendChild(kv("Latency", `${invocation.latency_ms} ms`));
  }
  card.appendChild(meta);

  if (invocation.output && invocation.output.warnings && invocation.output.warnings.length > 0) {
    const warn = el("div", { class: "ai-card__warnings" }, [el("div", { class: "small muted", text: "Warnings" })]);
    warn.appendChild(renderWarningsList(invocation.output.warnings));
    card.appendChild(warn);
  }

  if (invocation.error_code) {
    card.appendChild(
      el("div", { class: "reason-box reason-box--bad", text: `${invocation.status}: ${invocation.error_code}` })
    );
  }

  if (invocation.validation_result && invocation.validation_result.valid === false) {
    const box = el("div", { class: "reason-box reason-box--bad" }, [
      el("div", { text: "Output failed structured validation:" }),
    ]);
    box.appendChild(renderList(invocation.validation_result.errors));
    card.appendChild(box);
  }

  const terminal = invocation.status === "FAILED" || invocation.status === "REJECTED";
  if (terminal && typeof options.onRetry === "function") {
    card.appendChild(
      el("div", { class: "ai-card__actions" }, [
        el("button", {
          class: "btn btn--secondary",
          text: "Retry",
          attrs: { type: "button" },
          on: { click: () => options.onRetry(invocation) },
        }),
      ])
    );
  }

  return card;
}
