// shell/shell.js — the application shell: tab navigation (CD-4 WI-4's
// `initTabs`, extended by CD-6 Slice 1 with the Needs You/Activity
// tabs — never replaced, see PID §98.2's own instruction to extend the
// existing tab system rather than build a second one), the Ask BAGMAN
// drawer's mount point (PID §42 — reachable from anywhere, not buried
// in one tab), the shared operator-identity field, and (CD-6 Slice 1)
// the global `+ Add` menu (`shell/add-menu.js`) plus the generic review
// drawer (`shell/drawer.js`) Needs You resolution uses.
import { qs, qsa } from "../shared/dom.js";
import { getActorId, setActorId, defaultActorId } from "../shared/operator.js";
import { Overview } from "../features/overview/overview.js";
import { Documents } from "../features/documents/documents.js";
import { Detail } from "../features/documents/detail.js";
import { NeedsYou } from "../features/needs-you/needs-you.js";
import { Activity } from "../features/activity/activity.js";
import { Connections } from "../features/xero/connections.js";
import { Mailboxes } from "../features/mailbox/mailboxes.js";
import * as AskBagman from "../features/ai/ask-bagman.js";
import * as AddMenu from "./add-menu.js";
import * as ReviewDrawer from "./drawer.js";

//: name -> {panel, onActivate} — the single source of truth for every
//: known tab. Adding a tab (CD-6 Slice 1's own Needs You/Activity, and
//: any later CD-6 slice's tab) means adding ONE entry here, never a
//: second parallel tab-switch mechanism.
const TABS = {
  overview: { panel: "#panel-overview", onActivate: () => Overview.load() },
  "needs-you": { panel: "#panel-needs-you", onActivate: () => NeedsYou.ensureLoaded() },
  documents: { panel: "#panel-documents", onActivate: () => Documents.ensureLoaded() },
  activity: { panel: "#panel-activity", onActivate: () => Activity.ensureLoaded() },
  //: CD-6 Slice 2 (architect spec §16) — the Settings/Connections tab.
  connections: { panel: "#panel-connections", onActivate: () => Connections.ensureLoaded() },
  //: CD-6 Slice 3 (Mailbox Management) — the Email tab.
  mailboxes: { panel: "#panel-mailboxes", onActivate: () => Mailboxes.ensureLoaded() },
};

function initTabs() {
  const tabButtons = qsa(".tab-btn[data-tab]");
  const panels = {};
  for (const [name, config] of Object.entries(TABS)) {
    panels[name] = qs(config.panel);
  }

  function activate(tabName) {
    if (!TABS[tabName]) return;
    for (const btn of tabButtons) {
      if (btn.dataset.tab === tabName) {
        btn.setAttribute("aria-current", "page");
      } else {
        btn.removeAttribute("aria-current");
      }
    }
    for (const [name, panel] of Object.entries(panels)) {
      if (panel) panel.hidden = name !== tabName;
    }
    TABS[tabName].onActivate();
  }

  for (const btn of tabButtons) {
    btn.addEventListener("click", () => activate(btn.dataset.tab));
  }
  // Event DELEGATION (not a one-time qsa() snapshot) — CD-6 Slice 1
  // needs this: Overview's own greeting (features/overview/overview.js)
  // renders its "Open Needs You" button dynamically, after this
  // function has already run once, so a snapshot loop would never see
  // it. Delegating to `document` means any `[data-goto-tab]` button,
  // present now or added later by any feature module, works the same
  // way without that feature needing to know about shell.js at all.
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-goto-tab]");
    if (btn) activate(btn.dataset.gotoTab);
  });

  activate("overview");
  return activate;
}

function initOverviewRefresh() {
  qs("#overview-refresh").addEventListener("click", () => Overview.load());
}

//: CD-6 Slice 3 (Mailbox Management) — the Email tab's own "Add
//: mailbox" button (outside the global `+ Add` menu, which is scoped
//: to evidence upload only — a mailbox definition is not evidence).
function initMailboxesAdd() {
  const btn = qs("#mailboxes-add");
  if (!btn) return;
  btn.addEventListener("click", () => Mailboxes.openAddDrawer());
}

function initOperatorIdentity() {
  const input = qs("#operator-identity-input");
  if (!input) return;
  input.value = getActorId();
  input.placeholder = defaultActorId();
  input.addEventListener("change", () => {
    input.value = setActorId(input.value);
  });
}

function initAskBagman(activateTab) {
  AskBagman.init();
  // #ask-bagman-toggle is the header's search-styled affordance (PID
  // §42's "reachable from anywhere") — one element, one listener;
  // "Ask BAGMAN about this" (Documents detail panel) is the separate
  // contextual entry point into the same drawer.
  qs("#ask-bagman-toggle").addEventListener("click", () => AskBagman.toggle());
  // Wires "referenced evidence" links inside an Ask BAGMAN answer (PID
  // §44) back to the Documents detail view — switching tabs first so
  // the operator actually sees where they landed, then opening the
  // reduced evidence-only detail view (see Detail.openForEvidence's own
  // docstring for why this is evidence-only, not a fabricated intake
  // record).
  AskBagman.setEvidenceLinkHandler((evidenceId) => {
    // Ask BAGMAN renders on top of the detail slide-over (it can be
    // summoned FROM the detail panel, so it must out-rank it) — close
    // it first so the detail view this click opens is actually visible
    // rather than hidden behind the still-open drawer.
    AskBagman.close();
    activateTab("documents");
    Detail.openForEvidence(evidenceId);
  });
}

/** CD-6 Slice 1: whenever evidence is newly registered (the global
 * `+ Add` upload modal) or a Needs You item changes (resolved/
 * dismissed), the Needs You list and Overview's own greeting count are
 * both potentially stale — refresh whichever is currently mounted
 * rather than requiring a manual tab switch to see the new state. */
function initCrossFeatureRefresh() {
  document.addEventListener("bagman:evidence-registered", () => {
    NeedsYou._loaded = false; // force a real reload next activation/immediately below
    NeedsYou.load();
    Overview._loadGreeting();
  });
  document.addEventListener("bagman:needs-you-changed", () => {
    Overview._loadGreeting();
  });
}

export function initShell() {
  const activateTab = initTabs();
  initOverviewRefresh();
  initMailboxesAdd();
  Detail.initClosePanel();
  ReviewDrawer.initClose();
  AddMenu.initAddMenu();
  initOperatorIdentity();
  initAskBagman(activateTab);
  initCrossFeatureRefresh();
}
