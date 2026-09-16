// shell/preview.js — the evidence/document preview component (CD-6
// Slice 1, PID §98.2's hard "original evidence beside BAGMAN's
// interpretation" requirement). Renders the ACTUAL uploaded bytes —
// never a description of them — via the existing, already-governed
// `GET /internal/evidence/{id}/content` endpoint (`shared/api.js`'s
// `API.evidenceContent`); this module adds no new HTTP surface of its
// own, it only decides HOW to render whichever of PDF/image/other
// comes back, from `evidence.mime_type` (real fetched metadata, never
// guessed from a filename extension — the same "do not guess a
// mapping" discipline `app/api/routers/internal.py`'s own
// `get_evidence_content` docstring already states for the reverse
// direction).
import { el } from "../shared/dom.js";
import { API } from "../shared/api.js";

const IMAGE_MIME_PREFIXES = ["image/"];
const PDF_MIME = "application/pdf";

/** Build the preview node for `evidence` (a real `EvidenceItem.to_dict()`
 * — needs `evidence_id` and `mime_type`). Reused by both the Needs You
 * review drawer (`features/needs-you/needs-you.js`) and, later,
 * Documents detail — one implementation of "how BAGMAN shows original
 * evidence", not one per caller. */
export function renderEvidencePreview(evidence) {
  if (!evidence || !evidence.evidence_id) {
    return el("div", { class: "evidence-preview evidence-preview--missing", text: "No evidence to preview." });
  }

  const src = API.evidenceContent(evidence.evidence_id);
  const mime = evidence.mime_type || "";

  if (mime === PDF_MIME) {
    return el("div", { class: "evidence-preview evidence-preview--pdf" }, [
      el("iframe", {
        attrs: { src, title: "Original evidence (PDF)" },
      }),
    ]);
  }

  if (IMAGE_MIME_PREFIXES.some((prefix) => mime.startsWith(prefix))) {
    return el("div", { class: "evidence-preview evidence-preview--image" }, [
      el("img", { attrs: { src, alt: "Original evidence" } }),
    ]);
  }

  // Neither a PDF nor an image this GUI can render inline (PID §98.3's
  // own "PDF, JPEG/JPG, PNG" scope) — an honest fallback, never a
  // fabricated preview, with a real download link to the same governed
  // endpoint so the operator can still inspect the original bytes.
  return el("div", { class: "evidence-preview evidence-preview--fallback" }, [
    el("p", { class: "muted small", text: `No inline preview available for ${mime || "this file type"}.` }),
    el("a", {
      class: "btn btn--secondary",
      text: "Download original",
      attrs: { href: src, download: evidence.original_name || evidence.evidence_id },
    }),
  ]);
}
