// shell/add-menu.js — the global "+ Add" control and its shared upload
// modal (CD-6 Slice 1, PID §98.3). Three entry points, all funnelling
// through the SAME governed endpoint (`shell/intake-upload.js` ->
// `POST /internal/intake/evidence` — no bypass, no second upload code
// path):
//
//   * "Upload invoice / receipt" — evidence_type hint "INVOICE"/"RECEIPT"
//     (operator picks which on the form itself, PID §98.3's own two
//     named categories)
//   * "Upload other document"    — evidence_type hint "DOCUMENT"
//   * "Add photo"                — accepts image capture, evidence_type
//     hint "PHOTO" (an open hint value — `evidence_type` has no closed
//     enum, see `app/api/routers/intake.py::IntakeUploadMetadata`)
//
// `openUploadModal(presetKind)` is exported so `features/documents/documents.js`'s
// own new "Upload invoice" button (PID §98.3's "Also add Upload
// invoice within the Documents tab") can open the SAME modal
// pre-selected to the invoice/receipt kind, rather than duplicating
// this modal's markup/logic a second time.
import { el, clear, qs } from "../shared/dom.js";
import { fmtBytes } from "../shared/format.js";
import { getActorId } from "../shared/operator.js";
import { listEntities } from "./entities.js";
import { submitIntakeUpload } from "./intake-upload.js";
import * as notify from "../shared/notify.js";

//: Each kind's own accepted-file `accept` attribute, default
//: `evidence_type` hint, and heading — PID §98.3's "Supported evidence
//: should include: PDF, JPEG/JPG, PNG" (HEIC deliberately NOT added —
//: see this module's own docstring note below and the final delivery
//: report's "left out of scope" section for why).
const KIND_CONFIG = {
  INVOICE_RECEIPT: {
    heading: "Upload invoice / receipt",
    accept: ".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png",
    evidenceTypeOptions: [
      { value: "INVOICE", label: "Invoice" },
      { value: "RECEIPT", label: "Receipt" },
    ],
  },
  OTHER_DOCUMENT: {
    heading: "Upload other document",
    accept: ".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png",
    evidenceTypeOptions: [
      { value: "DOCUMENT", label: "Document" },
      { value: "STATEMENT", label: "Statement" },
      { value: "CONTRACT", label: "Contract" },
      { value: "TAX_DOCUMENT", label: "Tax document" },
      { value: "CORRESPONDENCE", label: "Correspondence" },
    ],
  },
  PHOTO: {
    heading: "Add photo",
    // `capture="environment"` is a progressive-enhancement hint only
    // (ignored harmlessly by a desktop browser, offers the camera
    // directly on a phone) — the underlying accepted MIME types are
    // unchanged from PID §98.3's own JPEG/PNG scope.
    accept: ".jpg,.jpeg,.png,image/jpeg,image/png",
    capture: "environment",
    evidenceTypeOptions: [{ value: "PHOTO", label: "Photo" }],
  },
};

let _selectedFile = null;

function closeModal() {
  const modal = qs("#upload-modal");
  if (modal) modal.hidden = true;
  _selectedFile = null;
}

function closeMenu() {
  const list = qs("#add-menu-list");
  const toggle = qs("#add-menu-toggle");
  if (list) list.hidden = true;
  if (toggle) toggle.setAttribute("aria-expanded", "false");
}

async function buildEntitySelect() {
  const select = el("select", { attrs: { id: "upload-modal-entity" } }, [
    el("option", { attrs: { value: "" }, text: "Unresolved (no company yet)" }),
  ]);
  const entities = await listEntities();
  for (const entity of entities) {
    select.appendChild(
      el("option", { attrs: { value: entity.canonical_name }, text: entity.display_name })
    );
  }
  return select;
}

export async function openUploadModal(kind) {
  const config = KIND_CONFIG[kind] || KIND_CONFIG.OTHER_DOCUMENT;
  const modal = qs("#upload-modal");
  const body = qs("#upload-modal-body");
  if (!modal || !body) return;
  closeMenu();
  _selectedFile = null;
  qs("#upload-modal-title").textContent = config.heading;
  clear(body);

  const fileInput = el("input", {
    attrs: {
      type: "file",
      id: "upload-modal-file",
      accept: config.accept,
      ...(config.capture ? { capture: config.capture } : {}),
    },
  });
  const fileLabel = el("div", { class: "muted small", text: "No file chosen." });
  fileInput.addEventListener("change", () => {
    const file = fileInput.files && fileInput.files[0];
    _selectedFile = file || null;
    fileLabel.textContent = file ? `${file.name} (${fmtBytes(file.size)})` : "No file chosen.";
    submitBtn.disabled = !file;
  });

  const evidenceTypeSelect = el(
    "select",
    { attrs: { id: "upload-modal-evidence-type" } },
    config.evidenceTypeOptions.map((opt) => el("option", { attrs: { value: opt.value }, text: opt.label }))
  );

  const entitySelect = await buildEntitySelect();

  const noteInput = el("input", {
    attrs: { type: "text", id: "upload-modal-note", placeholder: "Note (optional)", autocomplete: "off" },
  });

  const statusEl = el("div", { class: "upload-status", attrs: { "aria-live": "polite" } });

  const submitBtn = el("button", {
    class: "btn btn--primary",
    text: "Upload",
    attrs: { type: "button", disabled: "true" },
  });
  submitBtn.disabled = true;
  submitBtn.addEventListener("click", async () => {
    if (!_selectedFile || submitBtn.disabled) return;
    submitBtn.disabled = true;
    statusEl.dataset.kind = "progress";
    statusEl.textContent = "Uploading…";

    // Defensive try/catch around the whole call, not just its HTTP
    // outcome (`result.ok`/`result.errorText` below already handle a
    // clean failure `submitIntakeUpload` itself reports) — a fresh
    // Auditor found live that an UNCAUGHT exception inside
    // `submitIntakeUpload` (its own `crypto.randomUUID()` call, now
    // fixed — see shared/uuid.js) left this button disabled and the
    // status stuck at "Uploading…" forever with no visible error at
    // all. This catch is the second, independent layer against that
    // whole failure CLASS recurring for any other reason in future.
    let result;
    try {
      result = await submitIntakeUpload(_selectedFile, {
        entityHint: entitySelect.value || null,
        evidenceType: evidenceTypeSelect.value || null,
        actorId: getActorId(),
        note: noteInput.value.trim() || null,
        onProgress: (status) => {
          statusEl.dataset.kind = "progress";
          statusEl.textContent = `${status.charAt(0)}${status.slice(1).toLowerCase()}…`;
        },
      });
    } catch (unexpectedErr) {
      statusEl.dataset.kind = "bad";
      statusEl.textContent = `Upload failed: ${unexpectedErr.message || "unexpected error"}`;
      notify.error(`Upload failed: ${unexpectedErr.message || "unexpected error"}`);
      submitBtn.disabled = false;
      return;
    }

    if (result.ok) {
      statusEl.dataset.kind = "ok";
      statusEl.textContent = `Uploaded — registered as evidence ${result.evidence.evidence_id}.`;
      notify.ok("Upload complete — check Needs You for the follow-up review.");
      document.dispatchEvent(new CustomEvent("bagman:evidence-registered", { detail: result }));
      setTimeout(closeModal, 1200);
    } else {
      statusEl.dataset.kind = "bad";
      statusEl.textContent = result.errorText || "Upload failed.";
      notify.error(`Upload failed: ${result.errorText || "unknown error"}`);
      submitBtn.disabled = false;
    }
  });

  body.appendChild(
    el("div", { class: "upload-modal__form" }, [
      el("label", { class: "field", text: "File" }, [fileInput, fileLabel]),
      el("label", { class: "field", text: "Type" }, [evidenceTypeSelect]),
      el("label", { class: "field", text: "Company (optional — answered fully in Needs You)" }, [entitySelect]),
      el("label", { class: "field", text: "Note" }, [noteInput]),
      el("div", { class: "upload-modal__actions" }, [submitBtn, statusEl]),
    ])
  );

  modal.hidden = false;
}

export function initAddMenu() {
  const toggle = qs("#add-menu-toggle");
  const list = qs("#add-menu-list");
  if (!toggle || !list) return;

  toggle.addEventListener("click", () => {
    const willOpen = list.hidden;
    list.hidden = !willOpen;
    toggle.setAttribute("aria-expanded", String(willOpen));
  });
  document.addEventListener("click", (e) => {
    if (!list.hidden && !list.contains(e.target) && e.target !== toggle) closeMenu();
  });
  for (const btn of list.querySelectorAll("[data-add-kind]")) {
    btn.addEventListener("click", () => openUploadModal(btn.dataset.addKind));
  }

  const modal = qs("#upload-modal");
  if (modal) {
    qs("#upload-modal-close").addEventListener("click", closeModal);
    modal.addEventListener("click", (e) => {
      if (e.target === modal) closeModal();
    });
  }
}
