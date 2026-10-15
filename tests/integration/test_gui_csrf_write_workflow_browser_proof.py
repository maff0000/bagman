"""WO-BAGMAN-117 §6.A'.12 / §6.A' required test #1 — a REAL, logged-in
GUI-write-workflow test that exercises the ACTUAL SHIPPED client-side
JavaScript (not a Python HTTP client manually supplying the CSRF
header).

What this proves, and how
------------------------------------------------------------------------
A real headless Chromium (Playwright — already installed in this
environment, the SAME tool `tests/acceptance/*_browser_acceptance_proof.py`
already use for this codebase's own "network-level proof, not merely
'the UI looked right'" rigor standard) navigates to a REAL page served
by a REAL, live `uvicorn` HTTP server running the REAL `app.api.main:app`
ASGI app (bound to `127.0.0.1`, the SAME lightweight development/test
in-memory composition `tests/security/test_auth_access_control.py`'s
own `dev_client` fixture already uses — no Docker/Postgres/MinIO/ClamAV
required; see that module's own docstring for why this composition is
sufficient).

From inside that real browser page, this test:

1. Logs in for real (`fetch('/auth/login', ...)`), so the browser
   genuinely holds the real `Set-Cookie`-issued `__Host-bagman_session`/
   `__Host-bagman_csrf` cookies Chromium actually stored (NOT handed to
   it by Python) — `__Host-`/`Secure` cookies require a secure context,
   which Chromium treats `http://127.0.0.1` as (empirically confirmed
   while building this test), so no self-signed TLS certificate is
   needed for this proof.
2. Dynamically `import()`s the four REAL, unmodified, as-shipped ES
   modules this delivery changed — `shared/api.js`, `features/mailbox/
   mailbox-api.js`, `shell/intake-upload.js`, `features/documents/
   documents.js` — fetched from the SAME live server, at their REAL
   static paths. Nothing here is a copy or a re-implementation.
3. Calls the REAL, exported functions each module ships
   (`mailboxApi.createMailbox`/`updateMailbox` — the latter is the one
   PUT call this delivery's own local `apiPut` helper makes;
   `intakeUpload.submitIntakeUpload`; `Documents._submitUpload()`,
   driven against a minimal synthetic DOM built for this harness only —
   see `_DOCUMENTS_HARNESS_DOM_IDS` below) and inspects the REAL,
   wrapped `window.fetch`'s own captured request headers plus the REAL
   HTTP response status each one actually received from the REAL
   server — never the DOM's own rendered status text (which,
   deliberately, this test does NOT rely on: `Documents.load()` is
   stubbed to a no-op for this harness ONLY, in the page's own live
   object graph — never the shipped file on disk — because this proof
   is about the UPLOAD request, not this harness's lack of a full
   document-list UI to refresh afterward).
4. Clears the CSRF cookie via `document.cookie` (same-origin JS, the
   same mechanism a real attacker/bug would have to go through) and
   repeats the same four calls, proving each is REJECTED (403
   `CSRF_REJECTED`) specifically because the header the shipped code
   itself constructs from that cookie is now absent — not from some
   unrelated fault.

Honest limitation
------------------------------------------------------------------------
This is a REAL browser (Chromium) executing the REAL shipped JS against
a REAL live server — not a Python client bypassing the JS, and not a
static-analysis assertion about source text. The one thing it is NOT:
a full end-user click-through of the production SPA shell (logging in
via the real `/login.html` form, clicking through to the Documents tab,
dragging a file onto the real dropzone). Building that full shell
interaction was judged disproportionate for an auth-correction WO
(`tests/acceptance/*_browser_acceptance_proof.py`'s own equivalent proofs
require a full Docker Compose stack — real Postgres/MinIO/ClamAV — which
this correction is explicitly NOT authorised to touch/require). Calling
each shipped function's real, exported entry point directly — with a
synthetic-but-real DOM for the one case (`documents.js`) that reads
input elements by ID rather than exposing a standalone function — still
proves the actual question this test exists to answer: does the real,
shipped client code attach the CSRF header the server actually checks,
and does the server actually enforce it against that real code. Noted
here rather than overclaimed.
"""
from __future__ import annotations

import base64
import socket
import threading
import time

import pytest
import uvicorn

from app.api.composition import reset_composition_for_tests
from core.auth import hash_password

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

ACTOR_ID = "wo117-csrf-gui-browser-proof"
_TEST_PASSWORD = "a genuinely random csrf gui browser proof password 4k9p"
SYNTHETIC_PDF_B64 = base64.b64encode(
    b"%PDF-1.4\nSYNTHETIC TEST DATA - not a real document\n%%EOF"
).decode("ascii")

pytestmark = pytest.mark.skipif(
    sync_playwright is None, reason="playwright is not installed in this environment"
)


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
def live_server(monkeypatch, tmp_path):
    """A REAL `uvicorn` HTTP server serving the REAL `app.api.main:app`
    against the SAME lightweight development/test (in-memory)
    composition `tests/security/test_auth_access_control.py` already
    uses — see this module's own docstring for why a real browser needs
    this (a real page + real cookies), and why no Docker/real-TLS is
    required."""
    monkeypatch.setenv("BAGMAN_RUNTIME_ENV", "development")
    monkeypatch.setenv("BAGMAN_AUTH_ENABLED", "true")

    hash_file = tmp_path / "operator_password_hash"
    hash_file.write_text(hash_password(_TEST_PASSWORD), encoding="utf-8")
    monkeypatch.setenv("BAGMAN_OPERATOR_PASSWORD_HASH_FILE", str(hash_file))

    reset_composition_for_tests()

    from app.api.main import app

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "live uvicorn test server did not start in time"

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        reset_composition_for_tests()


# ---------------------------------------------------------------------
# The in-page harness script. Runs inside the REAL browser, against the
# REAL live server `live_server` points at. Everything it imports is
# the real, unmodified, as-shipped static file — this script only
# supplies the minimal synthetic DOM `documents.js`'s `_submitUpload`
# reads by element ID (it has no standalone exported function; the
# other three modules are called via their own real exported
# functions, no synthetic DOM needed for them at all).
# ---------------------------------------------------------------------

_HARNESS_JS = """
async (args) => {
  const { password, actorId, pdfBase64 } = args;
  const nativeFetch = window.fetch.bind(window);
  const captured = [];
  window.fetch = async (input, init) => {
    const url = typeof input === "string" ? input : (input && input.url) || String(input);
    const method = (init && init.method) || "GET";
    let headers = {};
    try {
      headers = Object.fromEntries(new Headers((init && init.headers) || {}).entries());
    } catch (e) {
      headers = { __error__: String(e) };
    }
    // `documents.js` and `intake-upload.js` both POST to the exact
    // same `/internal/intake/evidence` endpoint (by design — see
    // intake-upload.js's own module docstring: "the ONE shared
    // ... governed intake contract"), so URL+method alone cannot tell
    // their two calls apart below. Each call uses a distinct
    // synthetic filename (see `runWriteWorkflows` below) — read it
    // back out of the real `FormData` body so each captured request
    // can be matched to the exact call site that issued it.
    let fileName = null;
    try {
      if (init && init.body && typeof FormData !== "undefined" && init.body instanceof FormData) {
        const f = init.body.get("file");
        if (f && f.name) fileName = f.name;
      }
    } catch (e) {
      fileName = null;
    }
    const res = await nativeFetch(input, init);
    captured.push({ url, method, headers, status: res.status, fileName });
    return res;
  };

  function pdfFile(name) {
    const bytes = Uint8Array.from(atob(pdfBase64), (c) => c.charCodeAt(0));
    return new File([bytes], name, { type: "application/pdf" });
  }

  function buildDocumentsHarnessDom() {
    document.body.innerHTML = "";
    const ids = [
      ["file-input", "input"],
      ["upload-submit", "button"],
      ["upload-status", "div"],
      ["entity-select", "input"],
      ["entity-custom", "input"],
      ["evidence-type-select", "input"],
      ["actor-id-input", "input"],
      ["note-input", "input"],
    ];
    for (const [id, tag] of ids) {
      const node = document.createElement(tag);
      node.id = id;
      if (id === "file-input") node.type = "file";
      document.body.appendChild(node);
    }
    document.querySelector("#actor-id-input").value = actorId;
  }

  async function runWriteWorkflows(mailboxApi, intakeUpload, documentsMod, label) {
    const out = {};

    // --- shared/api.js's apiPost, via mailbox create ------------------
    const createRes = await mailboxApi.createMailbox({
      displayName: `CSRF Proof Mailbox ${label}`,
      emailAddress: `csrf-proof-${label}@example.test`,
      providerKind: "IMAP",
      defaultEntityId: null,
      actorId,
    });
    out.mailboxCreate = { ok: createRes.ok, status: createRes.status, body: createRes.body };

    // --- documents.js's own multipart upload --------------------------
    buildDocumentsHarnessDom();
    const dt = new DataTransfer();
    dt.items.add(pdfFile(`csrf-proof-documents-${label}.pdf`));
    document.querySelector("#file-input").files = dt.files;
    documentsMod.Documents.load = async () => {}; // harness-only stub (see module docstring)
    await documentsMod.Documents._submitUpload();
    out.documentsUploadStatusText = document.querySelector("#upload-status").textContent;

    // --- shell/intake-upload.js's own multipart upload ----------------
    const intakeResult = await intakeUpload.submitIntakeUpload(pdfFile(`csrf-proof-intake-${label}.pdf`), {
      entityHint: null,
      evidenceType: null,
      actorId,
      note: null,
    });
    out.intakeUpload = { ok: intakeResult.ok, httpStatus: intakeResult.httpStatus };

    return out;
  }

  // ---- Phase 0: real login (sets the real cookies) -------------------
  const loginRes = await nativeFetch("/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
  const loginOk = loginRes.ok;
  captured.push({ marker: "AFTER_LOGIN" });

  const shared = await import("/shared/api.js");
  const mailboxApi = await import("/features/mailbox/mailbox-api.js");
  const intakeUpload = await import("/shell/intake-upload.js");
  const documentsMod = await import("/features/documents/documents.js");

  // ---- Phase 1: WITH the real CSRF cookie present --------------------
  const phase1 = await runWriteWorkflows(mailboxApi, intakeUpload, documentsMod, "phasea");

  // The one PUT call (mailbox-api.js's own local apiPut), reusing the
  // mailbox just created above.
  const mailboxId = phase1.mailboxCreate.body && phase1.mailboxCreate.body.mailbox_id;
  const updateRes = await mailboxApi.updateMailbox(mailboxId, {
    displayName: "CSRF Proof Mailbox Updated",
    emailAddress: "csrf-proof-phasea@example.test",
    providerKind: "IMAP",
    defaultEntityId: null,
    actorId,
  });
  phase1.mailboxUpdate = { ok: updateRes.ok, status: updateRes.status };

  captured.push({ marker: "PHASE1_DONE" });

  // ---- Clear the CSRF cookie (same-origin JS) -------------------------
  document.cookie = "__Host-bagman_csrf=; Max-Age=0; Path=/; Secure; SameSite=Lax";
  const cookieAfterClear = document.cookie;
  captured.push({ marker: "PHASE2_START" });

  // ---- Phase 2: WITHOUT the CSRF cookie --------------------------------
  const phase2 = await runWriteWorkflows(mailboxApi, intakeUpload, documentsMod, "phaseb");
  const updateRes2 = await mailboxApi.updateMailbox(mailboxId, {
    displayName: "CSRF Proof Mailbox Updated Again",
    emailAddress: "csrf-proof-phasea@example.test",
    providerKind: "IMAP",
    defaultEntityId: null,
    actorId,
  });
  phase2.mailboxUpdate = { ok: updateRes2.ok, status: updateRes2.status };

  return { loginOk, cookieAfterClear, phase1, phase2, captured };
}
"""


def _entries_between(captured, start_marker, end_marker=None):
    """The non-marker (real captured-request) entries strictly between
    `start_marker` and `end_marker` — `end_marker=None` means "through
    the end of the list". A marker entry (`{"marker": "..."}`) and a
    real request entry (no `"marker"` key at all, so `.get("marker")`
    is `None`) are told apart by whether `"marker"` is `None` — NOT by
    comparing to the `end_marker` value directly, which would wrongly
    match on the very first ordinary entry whenever `end_marker` is
    itself `None` (a real bug this function's own first draft had)."""
    started = False
    out = []
    for entry in captured:
        marker = entry.get("marker")
        if marker is not None:
            if marker == start_marker:
                started = True
                continue
            if end_marker is not None and marker == end_marker:
                break
            continue
        if started:
            out.append(entry)
    return out


def _find(entries, *, method, url_contains, file_contains=None):
    matches = [e for e in entries if e["method"] == method and url_contains in e["url"]]
    if file_contains is not None:
        matches = [e for e in matches if e.get("fileName") and file_contains in e["fileName"]]
    assert matches, f"no captured request found for {method} *{url_contains}* (file~{file_contains!r}) in {entries}"
    return matches[-1]


def test_real_browser_executed_gui_write_workflow_sends_csrf_header_and_is_rejected_without_it(live_server):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            # A real, always-public, always-present page — see
            # app/api/main.py's own public allow-list docstring. Used
            # only as a real same-origin document to run this harness
            # from; the login itself happens via a real fetch() below,
            # not via this page's own form.
            page.goto(f"{live_server}/login.html")
            result = page.evaluate(
                _HARNESS_JS,
                {"password": _TEST_PASSWORD, "actorId": ACTOR_ID, "pdfBase64": SYNTHETIC_PDF_B64},
            )
        finally:
            browser.close()

    assert result["loginOk"] is True, "real /auth/login (real fetch, real cookies) must succeed"

    captured = result["captured"]
    phase1_requests = _entries_between(captured, "AFTER_LOGIN", "PHASE1_DONE")
    phase2_requests = _entries_between(captured, "PHASE2_START")

    # -------------------------------------------------------------
    # WITH the real CSRF cookie present: every real shipped write path
    # must have genuinely attached the header, and the server must
    # have accepted each request (never a 403).
    # -------------------------------------------------------------
    create_req = _find(phase1_requests, method="POST", url_contains="/internal/mailboxes")
    assert "x-bagman-csrf-token" in create_req["headers"]
    assert create_req["headers"]["x-bagman-csrf-token"]
    assert create_req["status"] == 201, create_req
    assert result["phase1"]["mailboxCreate"]["ok"] is True

    update_req = _find(phase1_requests, method="PUT", url_contains="/internal/mailboxes")
    assert "x-bagman-csrf-token" in update_req["headers"]
    assert update_req["headers"]["x-bagman-csrf-token"]
    assert update_req["status"] == 200, update_req
    assert result["phase1"]["mailboxUpdate"]["ok"] is True

    intake_req_1 = _find(
        phase1_requests, method="POST", url_contains="/internal/intake/evidence", file_contains="intake-phasea"
    )
    assert "x-bagman-csrf-token" in intake_req_1["headers"]
    assert intake_req_1["headers"]["x-bagman-csrf-token"]
    assert intake_req_1["status"] in (200, 201, 202), intake_req_1
    assert result["phase1"]["intakeUpload"]["ok"] is True

    # `documents.js` deliberately calls the SAME governed
    # `POST /internal/intake/evidence` endpoint `intake-upload.js` does
    # (see that file's own module docstring: "the ONE shared ...
    # governed intake contract") — disambiguated from the call above by
    # its own distinct synthetic filename (see `runWriteWorkflows`).
    documents_req_1 = _find(
        phase1_requests, method="POST", url_contains="/internal/intake/evidence", file_contains="documents-phasea"
    )
    assert "x-bagman-csrf-token" in documents_req_1["headers"]
    assert documents_req_1["headers"]["x-bagman-csrf-token"]
    assert documents_req_1["status"] in (200, 201, 202), documents_req_1

    # -------------------------------------------------------------
    # The CSRF cookie was genuinely cleared, same-origin, in-page.
    # -------------------------------------------------------------
    assert "__Host-bagman_csrf" not in result["cookieAfterClear"]

    # -------------------------------------------------------------
    # WITHOUT the CSRF cookie: the SAME real shipped code must now
    # send NO csrf header at all (never a stale/cached one), and the
    # server must reject every one of these four write paths with
    # 403 CSRF_REJECTED.
    # -------------------------------------------------------------
    create_req_2 = _find(phase2_requests, method="POST", url_contains="/internal/mailboxes")
    assert "x-bagman-csrf-token" not in create_req_2["headers"]
    assert create_req_2["status"] == 403, create_req_2
    assert result["phase2"]["mailboxCreate"]["ok"] is False
    assert result["phase2"]["mailboxCreate"]["body"]["error_code"] == "CSRF_REJECTED"

    update_req_2 = _find(phase2_requests, method="PUT", url_contains="/internal/mailboxes")
    assert "x-bagman-csrf-token" not in update_req_2["headers"]
    assert update_req_2["status"] == 403, update_req_2
    assert result["phase2"]["mailboxUpdate"]["ok"] is False

    intake_req_2 = _find(
        phase2_requests, method="POST", url_contains="/internal/intake/evidence", file_contains="intake-phaseb"
    )
    assert "x-bagman-csrf-token" not in intake_req_2["headers"]
    assert intake_req_2["status"] == 403, intake_req_2
    assert result["phase2"]["intakeUpload"]["ok"] is False

    documents_req_2 = _find(
        phase2_requests, method="POST", url_contains="/internal/intake/evidence", file_contains="documents-phaseb"
    )
    assert "x-bagman-csrf-token" not in documents_req_2["headers"]
    assert documents_req_2["status"] == 403, documents_req_2
