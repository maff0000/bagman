// features/documents/documents.js — the Documents tab: upload, the
// combined intake+evidence list with filters/pagination. CD-4 WI-4's
// `Documents` object, relocated unchanged by CD-5 WI-4's
// modularisation (behaviour preserved exactly — see this WI's own
// report for the regression check). Row clicks open
// features/documents/detail.js's `Detail.open(record)`.
import { el, clear, qs } from "../../shared/dom.js";
import { fmtBytes, fmtDateTime, hashPrefix, statusBadge } from "../../shared/format.js";
import { API, apiGet, errorMessage } from "../../shared/api.js";
import { Detail } from "./detail.js";

export const Documents = {
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
            // CD-4 PR #4 Architect delta (2026-09-13): the server's own
            // `Content-Disposition: attachment` header (set by
            // GET /internal/evidence/{id}/content itself — see
            // app/api/http_headers.py) is now the actual safety
            // boundary that forces a save rather than in-page
            // navigation/inline rendering. This `download` attribute is
            // defense-in-depth only from here on — a same-origin hint
            // for browsers, not something the safety property depends
            // on.
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
