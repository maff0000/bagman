// features/documents/documents.js — the Documents tab: upload, plus
// the primary Documents list. CD-4 WI-4's `Documents` object, relocated
// unchanged by CD-5 WI-4's modularisation; CD-6 Slice 5 WI-5 replaces
// the list's own DATA SOURCE and ROW RENDERING with the new
// evidence-first `GET /internal/documents` projection (WI-5 §3/§11) —
// canonical EvidenceItem-first, regardless of source, so email-
// originated documents appear alongside manual uploads. Upload wiring
// (`_wireUpload`/`_submitUpload`/`_resolveOutcome`/`_renderFinalStatus`)
// is completely unchanged (WI-5 §4 — this is a LIST/DETAIL authority
// change only, never a removal of intake). Row clicks open
// features/documents/detail.js's `Detail.openDocument(row)`.
import { el, clear, qs } from "../../shared/dom.js";
import { fmtBytes, fmtDateTime, hashPrefix } from "../../shared/format.js";
import { API, apiGet, errorMessage } from "../../shared/api.js";
import { generateRequestId } from "../../shared/uuid.js";
import { listDocuments } from "./documents-api.js";
import { classificationBadge, decisionStateLabel, sourceSummaryLabel } from "./classification-format.js";
import { Detail } from "./detail.js";
import { openUploadModal } from "../../shell/add-menu.js";
import { listEntities } from "../../shell/entities.js";

export const Documents = {
  _loaded: false,
  state: {
    limit: 25,
    offset: 0,
    entityId: "",
    classificationStatus: "",
    documentType: "",
    reviewRequired: false,
    received_at_from: "",
    received_at_to: "",
  },
  _lastCount: 0,

  ensureLoaded() {
    if (this._loaded) return;
    this._loaded = true;
    this._wireUpload();
    this._wireFilters();
    this._wireUploadInvoiceButton();
    this._populateEntityFilter();
    this.load();
    document.addEventListener("bagman:evidence-registered", () => this.load());
  },

  // WI-5 §9 — the entity filter is a real <select> of canonical
  // entities (`entity_id` values), never a free-text
  // canonical_name match against IntakeRecord.entity_hint any more.
  async _populateEntityFilter() {
    const select = qs("#filter-entity");
    if (!select) return;
    const entities = await listEntities();
    for (const entity of entities) {
      select.appendChild(el("option", { attrs: { value: entity.entity_id }, text: entity.display_name }));
    }
  },

  // CD-6 Slice 1 (PID §98.3) — "Also add Upload invoice within the
  // Invoices/Documents tab": opens the SAME global upload modal
  // (shell/add-menu.js) pre-selected to the invoice/receipt kind,
  // rather than a second, duplicated upload form.
  _wireUploadInvoiceButton() {
    const btn = qs("#documents-upload-invoice");
    if (!btn) return;
    btn.addEventListener("click", () => openUploadModal("INVOICE_RECEIPT"));
  },

  // ---- filters / pagination ----

  _wireFilters() {
    qs("#filter-apply").addEventListener("click", () => {
      this.state.entityId = qs("#filter-entity").value;
      this.state.classificationStatus = qs("#filter-classification-status").value;
      this.state.documentType = qs("#filter-document-type").value;
      this.state.reviewRequired = qs("#filter-review-required").checked;
      const from = qs("#filter-from").value;
      const to = qs("#filter-to").value;
      this.state.received_at_from = from ? `${from}T00:00:00.000Z` : "";
      this.state.received_at_to = to ? `${to}T23:59:59.999Z` : "";
      this.state.offset = 0;
      this.load();
    });
    qs("#filter-clear").addEventListener("click", () => {
      qs("#filter-entity").value = "";
      qs("#filter-classification-status").value = "";
      qs("#filter-document-type").value = "";
      qs("#filter-review-required").checked = false;
      qs("#filter-from").value = "";
      qs("#filter-to").value = "";
      Object.assign(this.state, {
        entityId: "",
        classificationStatus: "",
        documentType: "",
        reviewRequired: false,
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
      el("tr", {}, [el("td", { attrs: { colspan: "8" }, class: "muted", text: "Loading…" })])
    );

    const { ok, status, body } = await listDocuments({
      entityId: this.state.entityId || undefined,
      classificationStatus: this.state.classificationStatus || undefined,
      documentType: this.state.documentType || undefined,
      reviewRequired: this.state.reviewRequired ? true : undefined,
      receivedAtFrom: this.state.received_at_from || undefined,
      receivedAtTo: this.state.received_at_to || undefined,
      limit: this.state.limit,
      offset: this.state.offset,
    });
    clear(tbody);

    if (!ok || !body) {
      tbody.appendChild(
        el("tr", {}, [
          el("td", {
            attrs: { colspan: "8" },
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
          el("td", { attrs: { colspan: "8" }, class: "muted", text: "No documents yet." }),
        ])
      );
      return;
    }

    for (const row of body.items) {
      tbody.appendChild(this._row(row));
    }
  },

  // WI-5 §11/§12 — evidence-first row: Document, Entity,
  // Classification, Decision state, Source, Received, Integrity,
  // Download. `row` is one item from GET /internal/documents (see
  // services/evidence/document_projection.py's own row shape) — never
  // a raw IntakeRecord any more.
  _row(row) {
    const entityLabel = row.entity ? row.entity.display_name : "UNRESOLVED";
    const hp = hashPrefix(row.content_hash);

    const tr = el("tr", {
      on: { click: () => Detail.openDocument(row) },
    });

    const documentCell = el("td", { class: "wrap" });
    documentCell.appendChild(el("div", { text: row.document_label || row.evidence_id }));
    documentCell.appendChild(
      el("div", { class: "muted small mono", text: row.evidence_id, attrs: { title: row.evidence_id } })
    );
    tr.appendChild(documentCell);

    tr.appendChild(el("td", { text: entityLabel }));
    tr.appendChild(el("td", {}, [classificationBadge(row.current_classification)]));
    tr.appendChild(el("td", { text: decisionStateLabel(row) }));
    tr.appendChild(el("td", { text: sourceSummaryLabel(row) }));
    tr.appendChild(el("td", { text: fmtDateTime(row.received_at) }));
    tr.appendChild(
      el("td", {}, [
        hp
          ? el("span", { class: "hash-chip hash-chip--ok", text: hp })
          : el("span", { class: "muted", text: "—" }),
      ])
    );

    const downloadCell = el("td", {});
    downloadCell.appendChild(
      el("a", {
        text: "Download",
        attrs: {
          href: API.evidenceContent(row.evidence_id),
          // CD-4 PR #4 Architect delta (2026-09-13): the server's own
          // `Content-Disposition: attachment` header (set by
          // GET /internal/evidence/{id}/content itself — see
          // app/api/http_headers.py) is now the actual safety
          // boundary that forces a save rather than in-page
          // navigation/inline rendering. This `download` attribute is
          // defense-in-depth only from here on — a same-origin hint
          // for browsers, not something the safety property depends
          // on.
          download: row.original_name || row.evidence_id,
        },
      })
    );
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

    // CD-6 Slice 2 (architect spec §1): populated from the real
    // canonical entity list, never hardcoded company labels in
    // index.html's own markup — the static markup keeps only the two
    // app-semantic, non-business options ("UNRESOLVED"/"Other…").
    // `entity.canonical_name` is used as this <option>'s value (the
    // SAME shape this free-text `entity_hint` field already expected —
    // see `_submitUpload()` below), never `entity_id`, so this remains
    // a pure "swap the source of the labels" fix with no change to
    // what CD-4's intake endpoint actually receives.
    listEntities().then((entities) => {
      const customOption = entitySelect.querySelector('option[value="__custom__"]');
      for (const entity of entities) {
        const option = el("option", { attrs: { value: entity.canonical_name }, text: entity.display_name });
        entitySelect.insertBefore(option, customOption);
      }
    });

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

    // Real bug found by a fresh Auditor testing live against the
    // actual deployed URL (not a localhost/SSH-tunnel secure context):
    // `crypto.randomUUID()` (used below, and previously called
    // directly here) throws outside a browser secure context, which
    // this plain-HTTP LAN deployment always is — and, because nothing
    // wrapped that early a step, the exception silently left this
    // button disabled and the status text stuck at "Uploading…"
    // forever, indistinguishable from a hung request. Every step from
    // here on is now wrapped so ANY unexpected exception — not just a
    // network error from fetch() — surfaces a real, visible error and
    // re-enables the button, matching PID §98.2's own "no fake
    // buttons" doctrine in the failure direction too.
    try {
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

      const idempotencyKey = generateRequestId();

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
    } catch (unexpectedErr) {
      statusEl.dataset.kind = "bad";
      statusEl.textContent = `Upload failed: ${unexpectedErr.message || "unexpected error"}`;
      submitBtn.disabled = false;
    }
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
