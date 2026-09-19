// features/ai/ask-bagman.js — Ask BAGMAN, the persistent Claude-backed
// operator chat surface (PID §42-44, WI-4 GUI over WI-3's
// `POST /internal/operator/chat`).
//
// In-session-only conversation history (PID §60 explicitly allows
// this for this vertical slice — no durable server-side conversation
// persistence exists yet, and this module does not pretend otherwise:
// a page reload loses history, honestly).
//
// CD-6 reliability delta (PID §98/§100) fixed a real, previously-live
// backend defect this module used to have to surface rather than work
// around: `handle_operator_message` used to REQUIRE at least one of
// `evidence_id`/`intake_id`/`entity_id` to be attached — a genuinely
// subject-less "what needs my attention?" question returned HTTP 422.
// The architect ruled that wrong ("an operator-originated
// conversational message is itself a valid traceable input") and the
// backend now accepts a `conversation_id` as its own fallback subject
// (see `ai.invocation.derive_primary_input_reference`'s module
// docstring). This module generates ONE `conversationId` per OPEN
// drawer session (see `open()`/`close()` below) and sends it on every
// turn — PID's own "no business logic in the browser" invariant still
// holds: the JS does not decide what Claude/BAGMAN is allowed to do,
// it only supplies the conversation-scoped identifier the backend's
// own concurrency guard needs.
import { el, clear, qs } from "../../shared/dom.js";
import { fmtDateTime } from "../../shared/format.js";
import { errorMessage } from "../../shared/api.js";
import { getActorId } from "../../shared/operator.js";
import { generateRequestId } from "../../shared/uuid.js";
import { sendOperatorChat } from "./ai-api.js";
import { renderToolCalls, renderWarningsList } from "./invocation-card.js";

//: The literal `source` value sent on every turn from this surface
//: (CD-6 reliability delta, PID §98/§100) — a simple, honest literal,
//: not a taxonomy; this module is currently the ONLY caller of
//: `POST /internal/operator/chat`.
const SOURCE = "ask_bagman_drawer";

const conversation = []; // {role: 'user'|'assistant'|'error', text, toolCalls, referencedEvidenceIds, warnings, at}

let context = { evidenceId: null, intakeId: null, entityId: null };
//: One id per OPEN drawer session (CD-6 reliability delta) — generated
//: fresh the moment the drawer transitions from hidden to visible
//: (`open()`), reused across every turn typed while it stays open
//: (including across "Ask BAGMAN about this" re-attaching a different
//: document mid-conversation), and cleared on `close()` so the NEXT
//: open starts a genuinely new, non-conflicting conversation subject.
let conversationId = null;
let evidenceLinkHandler = null;
let sending = false;

function hasContext() {
  return Boolean(context.evidenceId || context.intakeId || context.entityId);
}

function contextLabel() {
  if (context.evidenceId) return `Document ${context.evidenceId}`;
  if (context.intakeId) return `Intake ${context.intakeId}`;
  if (context.entityId) return `Entity ${context.entityId}`;
  return null;
}

/** Wired by shell/shell.js at boot — lets a referenced evidence_id in
 * Claude's answer navigate to that document's detail view (PID §44)
 * without ask-bagman.js importing features/documents/detail.js
 * directly (which would create a two-way module dependency — detail.js
 * already imports THIS module to open "Ask BAGMAN about this"). */
export function setEvidenceLinkHandler(fn) {
  evidenceLinkHandler = fn;
}

function renderReferencedEvidence(ids) {
  if (!ids || ids.length === 0) return null;
  const wrap = el("div", { class: "referenced-evidence" }, [
    el("span", { class: "small muted", text: "Referenced: " }),
  ]);
  ids.forEach((id, idx) => {
    if (idx > 0) wrap.appendChild(el("span", { text: ", " }));
    wrap.appendChild(
      el("button", {
        class: "link-btn",
        text: id,
        attrs: { type: "button" },
        on: {
          click: () => {
            if (evidenceLinkHandler) evidenceLinkHandler(id);
          },
        },
      })
    );
  });
  return wrap;
}

function renderTurn(turn) {
  const bubble = el("div", { class: `chat-turn chat-turn--${turn.role}` });
  bubble.appendChild(el("div", { class: "chat-turn__meta small muted", text: fmtDateTime(turn.at) }));
  bubble.appendChild(el("div", { class: "chat-turn__text", text: turn.text }));

  if (turn.role === "assistant") {
    bubble.appendChild(
      el("div", { class: "ai-card__provenance-flag ai-card__provenance-flag--inline" }, [
        el("span", { class: "ai-card__flag-icon", text: "✨" }),
        el("span", { text: "AI-generated — not canonical" }),
      ])
    );
    const toolCalls = renderToolCalls(turn.toolCalls);
    if (toolCalls) bubble.appendChild(toolCalls);
    const refs = renderReferencedEvidence(turn.referencedEvidenceIds);
    if (refs) bubble.appendChild(refs);
    if (turn.warnings && turn.warnings.length > 0) {
      bubble.appendChild(renderWarningsList(turn.warnings));
    }
  }

  return bubble;
}

function render() {
  const drawer = qs("#ask-bagman-drawer");
  if (!drawer) return;

  const chip = qs("#ask-bagman-context-chip");
  clear(chip);
  const label = contextLabel();
  if (label) {
    chip.hidden = false;
    chip.appendChild(el("span", { text: `Context: ${label}` }));
    chip.appendChild(
      el("button", {
        class: "link-btn",
        text: "clear",
        attrs: { type: "button" },
        on: {
          click: () => {
            context = { evidenceId: null, intakeId: null, entityId: null };
            render();
          },
        },
      })
    );
  } else {
    chip.hidden = true;
  }

  const notice = qs("#ask-bagman-no-context-notice");
  notice.hidden = hasContext();

  const messages = qs("#ask-bagman-messages");
  clear(messages);
  if (conversation.length === 0) {
    messages.appendChild(
      el("p", { class: "muted small", text: "Ask about a document, intake, or entity — e.g. “What is this document?”" })
    );
  }
  for (const turn of conversation) {
    messages.appendChild(renderTurn(turn));
  }
  if (sending) {
    messages.appendChild(el("div", { class: "chat-turn chat-turn--pending small muted", text: "BAGMAN is thinking…" }));
  }
  messages.scrollTop = messages.scrollHeight;
}

async function submit() {
  if (sending) return;
  const input = qs("#ask-bagman-input");
  const message = input.value.trim();
  if (!message) return;

  conversation.push({ role: "user", text: message, at: new Date().toISOString() });
  input.value = "";
  sending = true;
  render();

  const result = await sendOperatorChat({
    message,
    actorId: getActorId(),
    evidenceId: context.evidenceId,
    intakeId: context.intakeId,
    entityId: context.entityId,
    conversationId,
    source: SOURCE,
  });

  sending = false;

  if (!result.ok) {
    conversation.push({
      role: "error",
      text: result.networkError
        ? `Network error — could not reach BAGMAN: ${result.networkError}`
        : errorMessage(result.status, result.body),
      at: new Date().toISOString(),
    });
    render();
    return;
  }

  const body = result.body;
  if (body.status !== "SUCCEEDED" || body.response_text === null || body.response_text === undefined) {
    conversation.push({
      role: "error",
      text: `Ask BAGMAN could not complete this request — ${body.status}${body.error_code ? `: ${body.error_code}` : ""}`,
      at: new Date().toISOString(),
    });
    render();
    return;
  }

  conversation.push({
    role: "assistant",
    text: body.response_text,
    toolCalls: body.output ? body.output.tool_calls : [],
    referencedEvidenceIds: body.referenced_evidence_ids || [],
    warnings: body.output ? body.output.warnings : [],
    at: body.completed_at || new Date().toISOString(),
  });
  render();
}

export function open(newContext = {}) {
  context = {
    evidenceId: newContext.evidenceId || null,
    intakeId: newContext.intakeId || null,
    entityId: newContext.entityId || null,
  };
  const drawer = qs("#ask-bagman-drawer");
  // CD-6 reliability delta: generate a fresh conversation id only on a
  // genuine hidden->visible transition (or if none exists yet) —
  // reused, not regenerated, if `open()` is called again while the
  // drawer is ALREADY visible (e.g. "Ask BAGMAN about this" clicked on
  // a different document while the drawer stays open) — see module
  // docstring and `close()` below.
  if (drawer.hidden || !conversationId) {
    conversationId = generateRequestId();
  }
  drawer.hidden = false;
  render();
  qs("#ask-bagman-input").focus();
}

export function close() {
  qs("#ask-bagman-drawer").hidden = true;
  // CD-6 reliability delta: closing the drawer ends this conversation
  // subject — the NEXT open() (even with the same document re-attached)
  // must not conflict with a still-in-flight turn from a conversation
  // the operator has already left (module docstring).
  conversationId = null;
}

/** The shell header's "Ask BAGMAN" button (reachable from anywhere,
 * PID §42) — deliberately resets any previously-attached document/
 * intake/entity context back to none. Without this, toggling the
 * drawer shut after using "Ask BAGMAN about this" on one document and
 * reopening it via the header button would silently keep asking
 * "about" that same document forever — surprising, and exactly the
 * kind of stale-context bug a real browser pass caught (this WI's own
 * report names it explicitly). Conversation HISTORY is untouched
 * (PID §60 — in-session only, but persists across open/close); only
 * the attached subject resets. "Ask BAGMAN about this" (`open()`
 * below) is the one deliberate way to attach context, and always wins
 * regardless of the drawer's current visibility.
 */
export function toggle() {
  const drawer = qs("#ask-bagman-drawer");
  if (drawer.hidden) {
    open({});
  } else {
    close();
  }
}

export function init() {
  qs("#ask-bagman-close").addEventListener("click", () => close());
  qs("#ask-bagman-form").addEventListener("submit", (e) => {
    e.preventDefault();
    submit();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !qs("#ask-bagman-drawer").hidden) close();
  });
  render();
}
