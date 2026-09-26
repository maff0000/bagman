// shared/uuid.js — a request-id/Idempotency-Key generator that works
// regardless of browser "secure context" status (CD-6 Gate-B fix,
// 2026-09-17).
//
// Real bug found by a fresh Auditor auditing the CD-6 GUI redesign,
// live against the actual deployed URL (`http://192.168.11.4:8200`,
// exactly the address PID §98.1/§99.8 require BAGMAN be reachable at
// from `192.168.246.0/24`): `crypto.randomUUID()` is defined ONLY in
// a browser "secure context" (HTTPS, or a `localhost` origin) — see
// https://developer.mozilla.org/en-US/docs/Web/API/Crypto/randomUUID.
// BAGMAN's GUI is deliberately served over plain HTTP to a real LAN
// IP (PID §99.8 is explicit that only the GUI/API is user-facing, and
// nothing about this delivery's scope adds TLS termination), which a
// browser always treats as an INSECURE context — so `crypto.randomUUID`
// is `undefined` there, `shell/intake-upload.js` and
// `features/documents/documents.js`'s own upload handlers both threw
// immediately on their very first line, and (because neither handler
// had a `try`/`catch` wrapping that early a step) the exception
// propagated as an unhandled promise rejection: the submit button
// stayed disabled and the status text stuck at "Uploading…" forever,
// with no visible error at all — every upload path was silently dead
// on the real, documented URL, while working perfectly against
// `localhost`/an SSH tunnel (a secure context) the whole time, which
// is exactly why this was not caught until a fresh Auditor tested
// against the real address instead of a tunnel.
//
// `crypto.getRandomValues()` — unlike `crypto.randomUUID()` — is NOT
// restricted to secure contexts (it is the lower-level primitive
// `randomUUID()` itself is built from), so it is the correct
// fallback: still real, cryptographically-strong randomness, just
// formatted into a v4 UUID string by hand instead of by the browser.
export function generateRequestId() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    try {
      return crypto.randomUUID();
    } catch {
      // Some engines expose the function but still throw when called
      // outside a secure context — fall through to the manual path
      // below rather than let this exception escape uncaught.
    }
  }
  if (typeof crypto !== "undefined" && typeof crypto.getRandomValues === "function") {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
    bytes[8] = (bytes[8] & 0x3f) | 0x80; // variant 10 (RFC 4122)
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  // Last-resort degrade path (not cryptographically strong) for the
  // — currently believed unreachable in any real browser — case where
  // even `crypto.getRandomValues` is unavailable. Acceptable here
  // specifically because this value is only ever used as an
  // `Idempotency-Key`/client-side request id, never as a security
  // credential or anything `core`/`services` treats as authoritative.
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}
