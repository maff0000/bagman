// shared/state.js — the empty / loading / error state primitives (CD-6
// Slice 1, PID §98.2). Every list/panel in this GUI that can be
// empty, still loading, or have failed to load renders ONE of these
// three, rather than each feature inventing its own "Loading…"/"No
// records yet." string inline — small, but it is what keeps the GUI's
// voice consistent across Needs You / Activity / Documents / Overview.
import { el } from "./dom.js";
import { errorMessage } from "./api.js";

export function loadingState(label = "Loading…") {
  return el("div", { class: "state state--loading" }, [
    el("span", { class: "state__spinner", attrs: { "aria-hidden": "true" } }),
    el("span", { text: label }),
  ]);
}

export function emptyState(label, hint) {
  return el("div", { class: "state state--empty" }, [
    el("div", { class: "state__label", text: label }),
    hint ? el("div", { class: "state__hint muted small", text: hint }) : null,
  ]);
}

/** `status`/`body` are exactly `apiGet`/`apiPost`'s own `{status, body}`
 * shape — this renders whatever `errorMessage()` (`shared/api.js`)
 * already knows how to extract, so error text is honest and consistent
 * with every other error surface in this GUI. */
export function errorState(status, body, label = "Something went wrong") {
  return el("div", { class: "state state--error" }, [
    el("div", { class: "state__label", text: label }),
    el("div", { class: "state__hint small", text: errorMessage(status, body) }),
  ]);
}
