// shared/notify.js — the confirmation/error notice ("toast") component
// (CD-6 Slice 1, PID §98.2's "no fake buttons" — every action gets a
// real, honest outcome notice, success or failure, never a silent
// no-op). One shared toast stack, mounted once by `shell/shell.js`
// (`#toast-stack` in index.html) — any feature module calls
// `notify.ok(...)`/`notify.error(...)` rather than building its own ad
// hoc notice element.
import { el, clear, qs } from "./dom.js";

const AUTO_DISMISS_MS = 6000;

function stack() {
  return qs("#toast-stack");
}

function show(text, kind) {
  const host = stack();
  if (!host) return; // defensive — never throw merely because a toast could not be shown
  const node = el("div", { class: `toast toast--${kind}`, text });
  const closeBtn = el("button", {
    class: "toast__close",
    text: "×",
    attrs: { type: "button", "aria-label": "Dismiss" },
  });
  closeBtn.addEventListener("click", () => node.remove());
  node.appendChild(closeBtn);
  host.appendChild(node);
  setTimeout(() => node.remove(), AUTO_DISMISS_MS);
}

export function ok(text) {
  show(text, "ok");
}

export function error(text) {
  show(text, "bad");
}

export function info(text) {
  show(text, "info");
}

export function clearAll() {
  const host = stack();
  if (host) clear(host);
}
