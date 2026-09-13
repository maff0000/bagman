"""HTTP response header encoding helpers.

CD-4 PR #4 Architect delta (2026-09-13): ``GET /internal/evidence/
{evidence_id}/content`` (``app/api/routers/internal.py``) previously
returned raw evidence bytes with no ``Content-Disposition`` header at
all — the GUI's client-side ``<a download>`` attribute
(``app/api/static/app.js``) was the only thing forcing a save-as
rather than in-page/inline rendering. The Architect's ruling: the
SERVER must be the actual safety boundary here, not a browser
attribute a client fully controls and could simply omit.

This module centralises the one piece of that fix worth isolating and
testing on its own: safely turning an arbitrary, untrusted filename
(``EvidenceItem.original_name`` — free-text, uploader-supplied,
carried through CD-4's intake pipeline, PID §14) into a
``Content-Disposition`` header value that can never inject a CRLF/
control character, break out of quoted-string escaping, or read as a
path.

Defense-in-depth, deliberately assuming nothing about upstream
filtering: ``services.evidence.intake.filename_safety`` already
rejects control characters, path separators, and ``..`` segments at
INTAKE time (WI-2), so none of that hostile input can reach here today
through the real pipeline. This function is nonetheless written to be
safe entirely on its own terms — if that upstream validation is ever
weakened, bypassed, or simply not run (e.g. a future direct-DB-write
path, a migration, a different producer), this is still the last line
of defense before the value becomes an HTTP header. Correctness here
must not depend on correctness elsewhere.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import quote

#: Characters that would break out of, or otherwise corrupt, the
#: quoted-string ASCII fallback parameter (``filename="..."``) if
#: passed through unescaped. Dropped outright rather than
#: backslash-escaped: a *dropped* character can never re-open a
#: malformed escape sequence the way a mishandled backslash-escape
#: could, and this is a display-only fallback value (the correct
#: original value is always additionally carried, fully
#: percent-encoded, in ``filename*``), so losing these characters from
#: it costs nothing real.
_ASCII_FALLBACK_DROP = {'"', "\\"}

#: Path separators dropped from the ASCII fallback too, even though
#: they are not otherwise unsafe *inside* an HTTP header: a filename
#: containing ``/`` or ``\`` read literally by a non-browser HTTP
#: client that (unlike a browser) trusts ``Content-Disposition`` at
#: face value when deciding where to write a downloaded file (a real,
#: previously-exploited class of client bug — e.g. tools honouring
#: ``curl -OJ``-style filenames) must never be able to walk outside
#: the client's intended download directory because of a name BAGMAN
#: itself served. ``filename*`` does not need this treatment
#: separately: RFC 5987 percent-encoding (via ``quote(..., safe="")``
#: below) already turns every ``/``/``\\`` into ``%2F``/``%5C``, so it
#: can never be read back as a literal separator either.
_ASCII_FALLBACK_DROP |= {"/", "\\"}


def _ascii_fallback(filename: str) -> str:
    """Reduce ``filename`` to a printable-ASCII, control-character-free,
    quote-safe, separator-free string suitable for the legacy quoted
    ``filename="..."`` parameter (RFC 6266 §4.3). Every C0/C1 control
    character (CR/LF included — the actual header-injection vector this
    function exists to close), DEL, any non-ASCII code point, and every
    character in :data:`_ASCII_FALLBACK_DROP` is dropped entirely.
    Dropping (not escaping, not truncating-at-first-bad-char) means the
    result can never contain anything that could terminate the quoted
    string early or start a new header line, no matter what precedes or
    follows it in the input.
    """
    out = []
    for ch in filename:
        codepoint = ord(ch)
        if codepoint < 0x20 or codepoint == 0x7F:
            continue  # C0 control chars, including CR/LF/NUL/TAB
        if 0x80 <= codepoint <= 0x9F:
            continue  # C1 control range
        if codepoint > 0x9F:
            continue  # non-ASCII — filename* carries the real value
        if ch in _ASCII_FALLBACK_DROP:
            continue
        out.append(ch)
    return "".join(out)


def safe_content_disposition_header(
    filename: Optional[str],
    *,
    fallback: str,
    disposition: str = "attachment",
) -> str:
    """Build a safe ``Content-Disposition`` header value for
    ``filename``, an untrusted, uploader-supplied display name.

    Implements the standard RFC 6266 / RFC 5987 dual-parameter pattern:

    * ``filename="..."`` — an ASCII-only, quoted-string fallback for
      clients that don't understand ``filename*`` (RFC 6266 §4.3).
    * ``filename*=UTF-8''...`` — the fully percent-encoded value,
      correct for any Unicode filename, understood by every modern
      browser and preferred over the plain ``filename`` parameter when
      both are present.

    ``fallback`` must already be a known-safe, printable-ASCII string
    (e.g. an ``evidence_id`` or an ``"evidence-<id>"`` label) with no
    further processing applied to it. It is used verbatim in two
    cases: when ``filename`` is ``None``/empty, or when everything in
    ``filename`` gets dropped by the ASCII-safety pass above (e.g. an
    all-emoji or all-control-character name) and nothing usable would
    otherwise remain for the quoted-string fallback.

    ``filename*`` is included whenever a real filename was supplied,
    even one that happens to already be plain ASCII — RFC 6266
    recommends sending both parameters together rather than
    conditionally, and doing so unconditionally keeps this function's
    behaviour simple and uniform rather than name-dependent.
    """
    if not filename:
        return f'{disposition}; filename="{fallback}"'

    ascii_name = _ascii_fallback(filename) or fallback
    star_value = quote(filename, safe="")
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{star_value}"
