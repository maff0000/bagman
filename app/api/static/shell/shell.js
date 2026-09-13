// shell/shell.js — the application shell: tab navigation (CD-4 WI-4's
// `initTabs`, relocated unchanged) plus this WI's own additions: the
// Ask BAGMAN drawer's mount point (PID §42 — reachable from anywhere,
// not buried in one tab) and the shared operator-identity field (see
// shared/operator.js's own docstring for why this lives at shell level
// rather than duplicated per-surface).
import { qs, qsa } from "../shared/dom.js";
import { getActorId, setActorId, defaultActorId } from "../shared/operator.js";
import { Overview } from "../features/overview/overview.js";
import { Documents } from "../features/documents/documents.js";
import { Detail } from "../features/documents/detail.js";
import * as AskBagman from "../features/ai/ask-bagman.js";

function initTabs() {
  const tabButtons = qsa(".tab-btn[data-tab]");
  const panels = {
    overview: qs("#panel-overview"),
    documents: qs("#panel-documents"),
  };

  function activate(tabName) {
    for (const btn of tabButtons) {
      if (btn.dataset.tab === tabName) {
        btn.setAttribute("aria-current", "page");
      } else {
        btn.removeAttribute("aria-current");
      }
    }
    for (const [name, panel] of Object.entries(panels)) {
      panel.hidden = name !== tabName;
    }
    if (tabName === "documents") {
      Documents.ensureLoaded();
    } else if (tabName === "overview") {
      Overview.load();
    }
  }

  for (const btn of tabButtons) {
    btn.addEventListener("click", () => activate(btn.dataset.tab));
  }
  for (const btn of qsa("[data-goto-tab]")) {
    btn.addEventListener("click", () => activate(btn.dataset.gotoTab));
  }

  activate("overview");
  return activate;
}

function initOverviewRefresh() {
  qs("#overview-refresh").addEventListener("click", () => Overview.load());
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

export function initShell() {
  const activateTab = initTabs();
  initOverviewRefresh();
  Detail.initClosePanel();
  initOperatorIdentity();
  initAskBagman(activateTab);
}
