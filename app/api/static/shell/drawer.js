// shell/drawer.js — the generic right-hand review drawer/panel
// primitive (CD-6 Slice 1, PID §98.2). `features/documents/detail.js`'s
// slide-over and `features/ai/ask-bagman.js`'s own drawer both
// predate this module and are left exactly as they are (no regression
// risk taken on working CD-4/CD-5 surfaces) — this is the NEW shared
// implementation every CD-6-and-later reviewer surface builds on
// instead of hand-rolling a fourth one: today, the Needs You
// Company/What/Why review panel; PID §98.7's own future Invoice review
// surface ("LEFT: original image/PDF, RIGHT: fields...") is the exact
// same shape and is expected to reuse this unchanged.
//
// One DOM mount point (`#review-drawer` in index.html), `open()`
// re-renders its body from scratch every call — no per-field DOM diff,
// consistent with the rest of this GUI's disposable-render style
// (`features/documents/detail.js` does the same for its own panel).
import { clear, qs } from "../shared/dom.js";

function root() {
  return qs("#review-drawer");
}

/** `title`: drawer heading text. `render(bodyNode)`: a function that
 * populates `bodyNode` with whatever content this drawer instance
 * needs (a plain DOM-building callback, not a template string —
 * matches this GUI's "no innerHTML anywhere" discipline, PID §98.2's
 * own "no fake buttons" spirit extended to "no fake markup either"). */
export function open({ title, render }) {
  const drawer = root();
  if (!drawer) return;
  qs("#review-drawer-title", drawer).textContent = title || "";
  const body = qs("#review-drawer-body", drawer);
  clear(body);
  if (typeof render === "function") render(body);
  drawer.hidden = false;
}

export function close() {
  const drawer = root();
  if (drawer) drawer.hidden = true;
}

export function initClose() {
  const drawer = root();
  if (!drawer) return;
  qs("#review-drawer-close", drawer).addEventListener("click", close);
  drawer.addEventListener("click", (e) => {
    if (e.target === drawer) close();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !drawer.hidden) close();
  });
}
