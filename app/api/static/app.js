// app.js — the thin BAGMAN GUI entry point (CD-5 WI-4 modularisation).
//
// Plain vanilla JS, ES modules, no framework/build step/bundler/jQuery
// (PID §41 — no framework migration authorised merely because the
// code grew; see this WI's own report for the full module-structure
// rationale). This file itself contains no behaviour of its own beyond
// booting the shell — every feature lives under `shell/`, `shared/`,
// and `features/{overview,documents,ai}/`.
//
// User-controlled/AI-derived content is rendered via `textContent`
// only, never `innerHTML`, everywhere in this GUI — see
// `shared/dom.js`'s `el()` helper, the one place a DOM node is ever
// created from script.
import { initShell } from "./shell/shell.js";

initShell();
