// shared/dom.js — tiny safe-DOM helpers (CD-4 WI-4, relocated unchanged
// by CD-5 WI-4's modularisation).
//
// No `innerHTML` with unescaped data anywhere in this file or any
// caller of `el()` — user-controlled/AI-derived text always goes
// through `opts.text` (which sets `.textContent`), never through raw
// markup. This is the ONE place `document.createElement` is called
// across the whole GUI; every feature module builds DOM through this
// helper so the "never innerHTML" discipline (PID §77-ish security
// invariant, restated in this WI's own dispatch) has exactly one place
// it could be violated, and isn't.

export function el(tag, opts = {}, children = []) {
  const node = document.createElement(tag);
  if (opts.class) node.className = opts.class;
  if (opts.text !== undefined) node.textContent = opts.text; // always textContent
  if (opts.attrs) {
    for (const [k, v] of Object.entries(opts.attrs)) node.setAttribute(k, v);
  }
  if (opts.on) {
    for (const [evt, fn] of Object.entries(opts.on)) node.addEventListener(evt, fn);
  }
  for (const child of children) {
    if (child) node.appendChild(child);
  }
  return node;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

export function qs(sel, root = document) {
  return root.querySelector(sel);
}

export function qsa(sel, root = document) {
  return Array.from(root.querySelectorAll(sel));
}
