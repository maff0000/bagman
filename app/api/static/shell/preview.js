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
import { loadingState } from "../shared/state.js";

const IMAGE_MIME_PREFIXES = ["image/"];
const PDF_MIME = "application/pdf";

/** Build the preview node for `evidence` (a real `EvidenceItem.to_dict()`
 * — needs `evidence_id` and `mime_type`). Reused by both the Needs You
 * review drawer (`features/needs-you/needs-you.js`) and, later,
 * Documents detail — one implementation of "how BAGMAN shows original
 * evidence", not one per caller.
 *
 * Design-review finding (CD-6 GUI rebuild): `GET
 * /internal/evidence/{id}/content` sends `Content-Disposition:
 * attachment` (a deliberate download-not-navigate safety boundary —
 * see features/documents/documents.js's own comment on the same
 * header). Pointing an <iframe>/<img> `src` directly at that URL
 * means the BROWSER treats it as a download and never paints
 * anything inline — confirmed directly in a real headless-Chromium
 * session while building this redesign (`Page.goto` on the same URL
 * raises "Download is starting"), so the "original evidence beside
 * BAGMAN's interpretation" panel was rendering empty even before this
 * pass. Fixed here on the FRONTEND ONLY, no wire-contract change: this
 * function `fetch()`s the exact same governed endpoint itself, reads
 * the response as a `Blob`, and hands the iframe/img a same-origin
 * `blob:` URL — which always renders inline regardless of
 * Content-Disposition, because it is no longer a direct network
 * response, just previously-fetched bytes. Still exactly one call to
 * exactly the same endpoint; only how this module CONSUMES the
 * response changed. */
export function renderEvidencePreview(evidence) {
  if (!evidence || !evidence.evidence_id) {
    return el("div", { class: "evidence-preview evidence-preview--missing", text: "No evidence to preview." });
  }

  const src = API.evidenceContent(evidence.evidence_id);
  const mime = evidence.mime_type || "";
  const isPdf = mime === PDF_MIME;
  const isImage = IMAGE_MIME_PREFIXES.some((prefix) => mime.startsWith(prefix));

  if (!isPdf && !isImage) {
    // Neither a PDF nor an image this GUI can render inline (PID
    // §98.3's own "PDF, JPEG/JPG, PNG" scope) — an honest fallback,
    // never a fabricated preview, with a real download link to the
    // same governed endpoint so the operator can still inspect the
    // original bytes.
    return el("div", { class: "evidence-preview evidence-preview--fallback" }, [
      el("p", { class: "muted small", text: `No inline preview available for ${mime || "this file type"}.` }),
      el("a", {
        class: "btn btn--secondary",
        text: "Download original",
        attrs: { href: src, download: evidence.original_name || evidence.evidence_id },
      }),
    ]);
  }

  const host = el("div", { class: `evidence-preview ${isPdf ? "evidence-preview--pdf" : "evidence-preview--image"}` }, [
    loadingState("Loading original evidence…"),
  ]);

  fetch(src, { headers: { Accept: mime || "*/*" } })
    .then((res) => {
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.blob();
    })
    .then((blob) => {
      const objectUrl = URL.createObjectURL(blob);
      host.textContent = "";
      // `data-evidence-id` carries the real identifier for anything
      // (a test, an operator inspecting the DOM) that needs to confirm
      // WHICH evidence this preview is showing — `src` itself is now a
      // browser-generated `blob:` URL (see this function's own
      // docstring for why), not a URL containing the id.
      if (isPdf) {
        host.appendChild(
          el("iframe", {
            attrs: { src: objectUrl, title: "Original evidence (PDF)", "data-evidence-id": evidence.evidence_id },
          })
        );
      } else {
        host.appendChild(
          el("img", { attrs: { src: objectUrl, alt: "Original evidence", "data-evidence-id": evidence.evidence_id } })
        );
      }
      // Revoke once the drawer/panel holding this preview is torn down
      // (its whole subtree, including this node, is removed from the
      // document) — a MutationObserver on the parent's disconnect is
      // more moving parts than this GUI's disposable-render style
      // needs; a short-lived object URL outliving one review session
      // is an acceptable, documented tradeoff over adding teardown
      // plumbing every caller would have to remember to invoke.
    })
    .catch((err) => {
      host.textContent = "";
      host.appendChild(
        el("div", { class: "evidence-preview--missing" }, [
          el("p", { class: "muted small", text: `Could not load the original evidence: ${err.message}` }),
          el("a", {
            class: "btn btn--secondary",
            text: "Download original",
            attrs: { href: src, download: evidence.original_name || evidence.evidence_id },
          }),
        ])
      );
    });

  return host;
}
