// BAGMAN Documents GUI — CD-4 WI-4 (PID §36-42).
//
// Plain vanilla JS, ES module, no framework/build step/bundler/jQuery
// (PID §42 — the framework decision is documented in the WI report).
// This file is the ONLY place browser-side logic lives for the
// Documents surface; it contains zero evidence business logic (PID
// §40) — it only calls BAGMAN's own `/internal/*` HTTP API and
// renders whatever authoritative result comes back. It never decides
// whether a file is safe, duplicate, or which entity it belongs to.
//
// User-controlled content (filenames, notes, quarantine/failure
// reasons, metadata values) is rendered with `textContent` only —
// never `innerHTML` — so nothing derived from an upload/caller can
// execute as markup/script in this page (see the `el()` helper below).

const API = {
  health: "/health",
  ready: "/ready",
  version: "/version",
  intake: "/internal/intake",
  intakeOne: (id) => `/internal/intake/${encodeURIComponent(id)}`,
  intakeEvidence: "/internal/intake/evidence",
  evidence: "/internal/evidence",
  evidenceOne: (id) => `/internal/evidence/${encodeURIComponent(id)}`,
  evidenceContent: (id) => `/internal/evidence/${encodeURIComponent(id)}/content`,
  provenance: (subjectType, subjectId) =>
    `/internal/provenance/${encodeURIComponent(subjectType)}/${encodeURIComponent(subjectId)}`,
};

// ---------------------------------------------------------------------
// tiny safe-DOM helpers (no innerHTML with unescaped data anywhere)
// ---------------------------------------------------------------------

function el(tag, opts = {}, children = []) {
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

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function qs(sel, root = document) {
  return root.querySelector(sel);
}
function qsa(sel, root = document) {
  return Array.from(root.querySelectorAll(sel));
}

// ---------------------------------------------------------------------
// formatting helpers (display only — no business meaning attached)
// ---------------------------------------------------------------------

function fmtBytes(n) {
  if (n === null || n === undefined) return "—";
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let val = n;
  let i = -1;
  do {
    val /= 1024;
    i += 1;
  } while (val >= 1024 && i < units.length - 1);
  return `${val.toFixed(1)} ${units[i]}`;
}

function fmtDateTime(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString();
  } catch {
    return iso;
  }
}

function hashPrefix(contentHash) {
  if (!contentHash || !contentHash.value) return null;
  return contentHash.value.slice(0, 12) + "…";
}

// Presentational grouping only (never a business decision the GUI
// makes about the record itself — just which color bucket a known,
// closed intake-state enum value falls into for the operator's eye).
const STATUS_GROUP = {
  RECEIVED: "progress",
  VALIDATING: "progress",
  ACCEPTED: "progress",
  REGISTERED: "ok",
  QUARANTINED: "warn",
  REJECTED: "bad",
  FAILED: "bad",
};

function statusBadge(status) {
  const group = STATUS_GROUP[status] || "progress";
  return el("span", { class: `badge badge--${group}`, text: status || "UNKNOWN" });
}

// ---------------------------------------------------------------------
// HTTP helpers
// ---------------------------------------------------------------------

async function apiGet(path) {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  let body = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  return { ok: res.ok, status: res.status, body };
}

/** Extract a human error message from whatever shape main.py's error
 * handling actually returns (see app/api/main.py's module docstring):
 * a BagmanError-mapped response is {error_code, message[, correlation_id]};
 * a bare FastAPI HTTPException (e.g. malformed multipart 'metadata')
 * is {detail: "..."}; anything else falls back to the raw HTTP status. */
function errorMessage(status, body) {
  if (body && typeof body === "object") {
    if (body.message) {
      const corr = body.correlation_id ? ` (correlation_id: ${body.correlation_id})` : "";
      return `${body.error_code || "ERROR"}: ${body.message}${corr}`;
    }
    if (body.detail) return String(body.detail);
  }
  return `HTTP ${status}`;
}

// ---------------------------------------------------------------------
// tab switching
// ---------------------------------------------------------------------

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
}

// ---------------------------------------------------------------------
// Overview tab — minimal health/readiness/version summary (PID §36:
// "Overview can be minimal — it is not this WI's focus")
// ---------------------------------------------------------------------

const Overview = {
  async load() {
    await Promise.all([this._loadHealth(), this._loadReady(), this._loadVersion()]);
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
};

// ---------------------------------------------------------------------
// Documents tab
// ---------------------------------------------------------------------

const Documents = {
  _loaded: false,
  state: {
    limit: 25,
    offset: 0,
    status: "",
    entity_hint: "",
    received_at_from: "",
    received_at_to: "",
  },
  _lastCount: 0,

  ensureLoaded() {
    if (this._loaded) return;
    this._loaded = true;
    this._wireUpload();
    this._wireFilters();
    this.load();
  },

  // ---- filters / pagination ----

  _wireFilters() {
    qs("#filter-apply").addEventListener("click", () => {
      this.state.status = qs("#filter-status").value;
      this.state.entity_hint = qs("#filter-entity").value.trim();
      const from = qs("#filter-from").value;
      const to = qs("#filter-to").value;
      this.state.received_at_from = from ? `${from}T00:00:00.000Z` : "";
      this.state.received_at_to = to ? `${to}T23:59:59.999Z` : "";
      this.state.offset = 0;
      this.load();
    });
    qs("#filter-clear").addEventListener("click", () => {
      qs("#filter-status").value = "";
      qs("#filter-entity").value = "";
      qs("#filter-from").value = "";
      qs("#filter-to").value = "";
      Object.assign(this.state, {
        status: "",
        entity_hint: "",
        received_at_from: "",
        received_at_to: "",
        offset: 0,
      });
      this.load();
    });
    qs("#list-refresh").addEventListener("click", () => this.load());
    qs("#page-prev").addEventListener("click", () => {
      this.state.offset = Math.max(0, this.state.offset - this.state.limit);
      this.load();
    });
    qs("#page-next").addEventListener("click", () => {
      if (this._lastCount < this.state.limit) return; // heuristic: short page = last page
      this.state.offset += this.state.limit;
      this.load();
    });
  },

  async load() {
    const tbody = qs("#doc-table-body");
    clear(tbody);
    tbody.appendChild(
      el("tr", {}, [el("td", { attrs: { colspan: "10" }, class: "muted", text: "Loading…" })])
    );

    const params = new URLSearchParams();
    params.set("limit", String(this.state.limit));
    params.set("offset", String(this.state.offset));
    if (this.state.status) params.set("status", this.state.status);
    if (this.state.entity_hint) params.set("entity_hint", this.state.entity_hint);
    if (this.state.received_at_from) params.set("received_at_from", this.state.received_at_from);
    if (this.state.received_at_to) params.set("received_at_to", this.state.received_at_to);

    const { ok, status, body } = await apiGet(`${API.intake}?${params.toString()}`);
    clear(tbody);

    if (!ok || !body) {
      tbody.appendChild(
        el("tr", {}, [
          el("td", {
            attrs: { colspan: "10" },
            class: "muted",
            text: errorMessage(status, body),
          }),
        ])
      );
      return;
    }

    this._lastCount = body.count || 0;
    qs("#page-info").textContent = `offset ${this.state.offset} · showing ${body.count} · limit ${body.limit}`;
    qs("#page-prev").disabled = this.state.offset === 0;
    qs("#page-next").disabled = this._lastCount < this.state.limit;

    if (!body.items || body.items.length === 0) {
      tbody.appendChild(
        el("tr", {}, [
          el("td", { attrs: { colspan: "10" }, class: "muted", text: "No intake records yet." }),
        ])
      );
      return;
    }

    for (const record of body.items) {
      tbody.appendChild(this._row(record));
    }
  },

  _row(record) {
    const evidenceTypeHint = (record.metadata && record.metadata.evidence_type_hint) || "UNKNOWN";
    const entity = record.entity_hint || "UNRESOLVED";
    const hp = hashPrefix(record.content_hash);

    const tr = el("tr", {
      on: { click: () => Detail.open(record) },
    });

    tr.appendChild(el("td", {}, [statusBadge(record.status)]));
    tr.appendChild(el("td", { text: record.evidence_id || "—" }));
    tr.appendChild(el("td", { text: entity }));
    tr.appendChild(el("td", { text: evidenceTypeHint }));
    tr.appendChild(el("td", { class: "wrap", text: record.original_filename || "—" }));
    tr.appendChild(el("td", { text: fmtDateTime(record.received_at) }));
    tr.appendChild(el("td", { text: fmtBytes(record.size_bytes) }));
    tr.appendChild(
      el("td", {}, [
        hp
          ? el("span", { class: "hash-chip hash-chip--ok", text: hp })
          : el("span", { class: "muted", text: "—" }),
      ])
    );
    // CD-4 has exactly one intake producer (MANUAL_UPLOAD, PID §9); no
    // GET /internal/sources/{id} endpoint exists yet to resolve a real
    // Source row per list-row without an expensive per-row provenance
    // fetch, so this list column is a documented static label. The
    // detail panel (below) resolves the REAL Source via
    // GET /internal/provenance/... once evidence exists, rather than
    // repeating this same assumption there.
    tr.appendChild(el("td", { text: "Manual Upload" }));

    const downloadCell = el("td", {});
    if (record.evidence_id) {
      downloadCell.appendChild(
        el("a", {
          text: "Download",
          attrs: {
            href: API.evidenceContent(record.evidence_id),
            // Client-side defense-in-depth (PID §48): forces a save
            // rather than in-page navigation/inline rendering, since
            // the underlying GET /internal/evidence/{id}/content
            // response does not itself set Content-Disposition — see
            // the WI-4 report's flagged gap.
            download: record.original_filename || record.evidence_id,
          },
        })
      );
    } else {
      downloadCell.appendChild(el("span", { class: "muted", text: "—" }));
    }
    tr.appendChild(downloadCell);

    return tr;
  },

  // ---- upload ----

  _wireUpload() {
    const dropzone = qs("#dropzone");
    const fileInput = qs("#file-input");
    const fileLabel = qs("#dropzone-file");
    const hint = qs(".dropzone__hint", dropzone);
    const submitBtn = qs("#upload-submit");
    const entitySelect = qs("#entity-select");
    const entityCustom = qs("#entity-custom");

    entitySelect.addEventListener("change", () => {
      entityCustom.hidden = entitySelect.value !== "__custom__";
    });

    function setFile(file) {
      if (!file) return;
      fileInput.files = (() => {
        const dt = new DataTransfer();
        dt.items.add(file);
        return dt.files;
      })();
      hint.hidden = true;
      fileLabel.hidden = false;
      fileLabel.textContent = `${file.name} (${fmtBytes(file.size)})`;
      submitBtn.disabled = false;
    }

    dropzone.addEventListener("click", () => fileInput.click());
    dropzone.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        fileInput.click();
      }
    });
    fileInput.addEventListener("change", () => {
      if (fileInput.files && fileInput.files[0]) setFile(fileInput.files[0]);
    });
    dropzone.addEventListener("dragover", (e) => {
      e.preventDefault();
      dropzone.classList.add("dropzone--drag");
    });
    dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dropzone--drag"));
    dropzone.addEventListener("drop", (e) => {
      e.preventDefault();
      dropzone.classList.remove("dropzone--drag");
      const file = e.dataTransfer.files && e.dataTransfer.files[0];
      if (file) setFile(file);
    });

    qs("#upload-form").addEventListener("submit", (e) => {
      e.preventDefault();
      this._submitUpload();
    });
  },

  async _submitUpload() {
    const submitBtn = qs("#upload-submit");
    const statusEl = qs("#upload-status");
    const fileInput = qs("#file-input");
    const entitySelect = qs("#entity-select");
    const entityCustom = qs("#entity-custom");
    const evidenceTypeSelect = qs("#evidence-type-select");
    const actorInput = qs("#actor-id-input");
    const noteInput = qs("#note-input");

    // Synchronously disable the button as the very first thing this
    // handler does, before any await — the concrete mechanism that
    // stops an accidental double-click from starting two in-flight
    // submits (the Idempotency-Key below only protects a genuine
    // network-level retry of the SAME submit, not two independently
    // dispatched ones).
    if (submitBtn.disabled) return;
    submitBtn.disabled = true;

    const file = fileInput.files && fileInput.files[0];
    const actorId = actorInput.value.trim();

    if (!file) {
      statusEl.dataset.kind = "bad";
      statusEl.textContent = "Choose a file before uploading.";
      submitBtn.disabled = false;
      return;
    }
    if (!actorId) {
      statusEl.dataset.kind = "bad";
      statusEl.textContent = "Enter who is uploading (operator identity) before uploading.";
      submitBtn.disabled = false;
      return;
    }

    let entityHint = entitySelect.value;
    if (entityHint === "__custom__") entityHint = entityCustom.value.trim() || null;
    if (entityHint === "") entityHint = null;

    let evidenceType = evidenceTypeSelect.value || null;

    const metadata = {
      entity_hint: entityHint,
      evidence_type: evidenceType,
      actor_type: "USER",
      actor_id: actorId,
      note: noteInput.value.trim() || null,
    };

    const formData = new FormData();
    formData.append("file", file);
    formData.append("metadata", JSON.stringify(metadata));

    const idempotencyKey = crypto.randomUUID();

    statusEl.dataset.kind = "progress";
    statusEl.textContent = "Uploading…"; // honest — this is the actual in-flight fetch, not a fabricated step (PID §38)

    let response;
    try {
      response = await fetch(API.intakeEvidence, {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
        body: formData,
      });
    } catch (networkErr) {
      statusEl.dataset.kind = "bad";
      statusEl.textContent = `Network error — could not reach BAGMAN: ${networkErr.message}`;
      submitBtn.disabled = false;
      return;
    }

    let body = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }

    // Distinguish a genuine workflow outcome (body carries "intake" —
    // see app/api/routers/intake.py: this shape is returned for EVERY
    // status the pipeline can honestly reach, including 422 REJECTED
    // and 503 FAILED, which are real outcomes, not framework errors)
    // from an actual request-level error (malformed metadata JSON,
    // idempotency conflict, or an unrelated 5xx) that never reached
    // the intake pipeline at all.
    if (body && body.intake) {
      await this._resolveOutcome(body, statusEl);
    } else {
      statusEl.dataset.kind = "bad";
      statusEl.textContent = `Upload failed: ${errorMessage(response.status, body)}`;
    }

    submitBtn.disabled = false;
    this.load();
  },

  async _resolveOutcome(body, statusEl) {
    let record = body.intake;

    // The synchronous endpoint already returns the FINAL state in the
    // overwhelming majority of cases. The one honest exception is the
    // narrow in-flight-replay race the router's own docstring
    // documents (HTTP 202, status RECEIVED/VALIDATING) — poll the real
    // GET /internal/intake/{id} a few times rather than inventing fake
    // intermediate progress text.
    let attempts = 0;
    while ((record.status === "RECEIVED" || record.status === "VALIDATING") && attempts < 8) {
      statusEl.dataset.kind = "progress";
      statusEl.textContent = `${record.status.charAt(0)}${record.status.slice(1).toLowerCase()}…`;
      await new Promise((r) => setTimeout(r, 1000));
      const { ok, body: polled } = await apiGet(API.intakeOne(record.intake_id));
      if (ok && polled) record = polled;
      attempts += 1;
    }

    this._renderFinalStatus(record, statusEl);
  },

  _renderFinalStatus(record, statusEl) {
    if (record.status === "REGISTERED") {
      statusEl.dataset.kind = "ok";
      statusEl.textContent = `Complete — registered as evidence ${record.evidence_id}`;
    } else if (record.status === "QUARANTINED") {
      statusEl.dataset.kind = "warn";
      statusEl.textContent = `Quarantined — ${record.quarantine_reason || "no reason recorded"}`;
    } else if (record.status === "REJECTED") {
      statusEl.dataset.kind = "bad";
      statusEl.textContent = `Rejected — ${record.failure_code || "no reason recorded"}`;
    } else if (record.status === "FAILED") {
      statusEl.dataset.kind = "bad";
      statusEl.textContent = `Failed — ${record.failure_code || "no reason recorded"}`;
    } else if (record.status === "ACCEPTED") {
      statusEl.dataset.kind = "progress";
      statusEl.textContent = "Accepted — registering evidence…";
    } else {
      statusEl.dataset.kind = "progress";
      statusEl.textContent = `Still ${record.status.toLowerCase()} — refresh the list shortly.`;
    }
  },
};

// ---------------------------------------------------------------------
// Detail panel (PID §39)
// ---------------------------------------------------------------------

const Detail = {
  async open(record) {
    const overlay = qs("#detail-overlay");
    const body = qs("#detail-body");
    qs("#detail-title").textContent = `Intake ${record.intake_id}`;
    clear(body);
    overlay.hidden = false;

    body.appendChild(el("div", { class: "detail-section" }, [statusBadge(record.status)]));

    if (record.status === "QUARANTINED" && record.quarantine_reason) {
      body.appendChild(
        el("div", { class: "reason-box reason-box--warn", text: `Quarantined: ${record.quarantine_reason}` })
      );
    }
    if ((record.status === "REJECTED" || record.status === "FAILED") && record.failure_code) {
      body.appendChild(
        el("div", {
          class: "reason-box reason-box--bad",
          text: `${record.status === "REJECTED" ? "Rejected" : "Failed"}: ${record.failure_code}`,
        })
      );
    }

    let evidence = null;
    let provenanceEdges = [];
    if (record.evidence_id) {
      const [evRes, provRes] = await Promise.all([
        apiGet(API.evidenceOne(record.evidence_id)),
        apiGet(API.provenance("EvidenceItem", record.evidence_id)),
      ]);
      if (evRes.ok) evidence = evRes.body;
      if (provRes.ok && Array.isArray(provRes.body)) provenanceEdges = provRes.body;
    }

    // Real Source, when we have it (from provenance) — falls back to
    // the same documented static label the list view uses when no
    // evidence/provenance exists yet (nothing to resolve it from).
    let sourceLabel = "Manual Upload (MANUAL_UPLOAD)";
    const sourceEdge = provenanceEdges.find((e) => e.source);
    if (sourceEdge && sourceEdge.source) {
      sourceLabel = `${sourceEdge.source.source_type} / ${sourceEdge.source.provider}`;
    }

    const kv = el("dl", { class: "detail-kv" });
    const rows = [
      ["Evidence ID", record.evidence_id || "—"],
      ["Intake ID", record.intake_id],
      ["Entity", record.entity_hint || "UNRESOLVED"],
      ["Filename", record.original_filename || "—"],
      ["Reported MIME", record.reported_mime_type || "—"],
      ["Detected MIME", record.detected_mime_type || "—"],
      [
        "Content hash",
        record.content_hash ? `${record.content_hash.algorithm} ${record.content_hash.value}` : "—",
      ],
      ["Size", fmtBytes(record.size_bytes)],
      ["Source", sourceLabel],
      ["Intake state", record.status],
      ["Evidence state", evidence ? evidence.status : "—"],
      ["Correlation ID", record.correlation_id],
      ["Idempotency key", record.idempotency_key || "—"],
    ];
    for (const [k, v] of rows) {
      kv.appendChild(el("dt", { text: k }));
      kv.appendChild(el("dd", { text: v === null || v === undefined ? "—" : String(v) }));
    }
    body.appendChild(el("div", { class: "detail-section" }, [el("h3", { text: "Fields" }), kv]));

    // ---- timeline (PID §39's "audit timeline") ----
    // No dedicated GET /internal/audit/... endpoint exists yet (this
    // WI's report flags that as a gap for a future delivery) — this
    // renders whatever real timeline-shaped information the endpoints
    // we DO have actually expose: the IntakeRecord's own two
    // timestamps, plus provenance edges when evidence exists. It is
    // not a substitute for a full AuditEvent trail.
    const timeline = el("ul", { class: "timeline" });
    timeline.appendChild(
      this._timelineItem("Intake received", record.received_at)
    );
    for (const edge of provenanceEdges) {
      if (edge.provenance) {
        const label = edge.provenance.relationship || edge.provenance.provenance_id || "Provenance link";
        timeline.appendChild(this._timelineItem(String(label), edge.provenance.created_at));
      }
    }
    if (evidence) {
      timeline.appendChild(this._timelineItem(`Evidence registered (${evidence.status})`, evidence.created_at));
    }
    if (record.completed_at) {
      timeline.appendChild(this._timelineItem(`Intake reached ${record.status}`, record.completed_at));
    }
    body.appendChild(
      el("div", { class: "detail-section" }, [
        el("h3", { text: "Timeline" }),
        timeline,
        el("p", {
          class: "muted small",
          text:
            "Derived from IntakeRecord timestamps and provenance edges only — a dedicated audit-event " +
            "endpoint would be needed for the complete AuditEvent trail (see WI-4 report).",
        }),
      ])
    );

    // ---- download ----
    const actions = el("div", { class: "detail-section" }, [el("h3", { text: "Actions" })]);
    if (record.evidence_id) {
      actions.appendChild(
        el("a", {
          class: "btn btn--primary",
          text: "Download original",
          attrs: {
            href: API.evidenceContent(record.evidence_id),
            download: record.original_filename || record.evidence_id,
          },
        })
      );
    } else {
      actions.appendChild(el("p", { class: "muted", text: "No evidence registered yet — nothing to download." }));
    }
    body.appendChild(actions);
  },

  _timelineItem(label, timestamp) {
    return el("li", {}, [
      el("div", { class: "t-label", text: label }),
      el("div", { class: "t-time", text: fmtDateTime(timestamp) }),
    ]);
  },

  close() {
    qs("#detail-overlay").hidden = true;
  },
};

function initDetailPanel() {
  qs("#detail-close").addEventListener("click", () => Detail.close());
  qs("#detail-overlay").addEventListener("click", (e) => {
    if (e.target === qs("#detail-overlay")) Detail.close();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") Detail.close();
  });
}

function initOverviewRefresh() {
  qs("#overview-refresh").addEventListener("click", () => Overview.load());
}

// ---------------------------------------------------------------------
// boot
// ---------------------------------------------------------------------

initTabs();
initDetailPanel();
initOverviewRefresh();
