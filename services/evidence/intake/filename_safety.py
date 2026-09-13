"""Filename safety validation for untrusted uploader-supplied filenames
(PID §14, CD-4 WI-2; bidi/format-control hardening added CD-5 WI-5,
PID §66).

An ``original_filename`` is retained purely for operator DISPLAY (PID
§14) — it must never be allowed to influence a filesystem or
object-store path (canonical storage is content-addressed by
``intake_id``/``evidence_id`` + SHA-256 hash — see
``persistence/objects/store.py``). This module's only job is deciding
whether a supplied filename is safe enough to retain for display at
all; it is intentionally defensive, since a filename is hostile input
(PID §62) exactly like file content itself.

This module never itself touches a filesystem path with the untrusted
value — no ``open()``, no ``os.path.join()``, nothing. It only
inspects the string's characters/length/shape.

Bidi/format-control hardening (CD-5 WI-5, PID §66)
----------------------------------------------------
Closes the CD-4 Auditor's carried-forward backlog item
(``bagman:backlog:filename_bidi_spoofing_hardening``, first flagged
during the CD-4 PR #4 delta round's focused Auditor pass — see
``app/api/http_headers.py``'s own module docstring for the other half
of that finding). The gap: this module's NFKC round-trip check (see
:func:`is_filename_safe`) catches compatibility-equivalence tricks
(ligatures, full/half-width variants) but a Unicode *bidi/format
control* character is not touched by NFKC normalisation at all — it
survives untouched, so a filename such as ``"invoice"
+ U+202E + "cod.exe"`` (the classic RLO attack: everything after the
override renders right-to-left, so a human operator visually reads
this as something ending in ``exe.doc`` while the actual byte sequence
still ends ``...exe``) previously passed ``is_filename_safe`` cleanly.

Chosen rejection set, and why: every code point in Unicode's own
"Explicit Directional Formatting Characters" family, i.e. exactly the
9 the PID names (U+202A LRE, U+202B RLE, U+202C PDF, U+202D LRO, U+202E
RLO, U+2066 LRI, U+2067 RLI, U+2068 FSI, U+2069 PDI) plus the three
narrower directional MARKS from the same functional family that are
not covered by the C0/C1 control check above but exist for exactly the
same "influence bidirectional rendering order" purpose and are
documented by the Unicode Standard alongside the nine above (U+200E
LRM, U+200F RLM, U+061C ALM). All twelve are zero-width-or-invisible
formatting characters whose *entire* purpose is to change how
surrounding text is rendered/ordered without changing what characters
are "there" — precisely the property a display-only, security-relevant
field must never trust. No other Unicode block is added: this module
does not attempt a general confusable-script defence (a much larger,
separate problem — homoglyph detection — deliberately out of this
bounded item's scope, per the PID's own "bounded security-hardening
item, not open-ended" framing).
"""
from __future__ import annotations

import unicodedata
from typing import Optional

from core.errors import ValidationError

#: PID §14's "pathological length" guard. 255 matches the common
#: filesystem filename-component limit (ext4/NTFS/etc.) and
#: ``services.evidence.intake.policy.IntakePolicy.max_filename_length``'s
#: own default — deliberately the same number, so a filename that
#: would already be rejected by most real filesystems is rejected here
#: too, not just an arbitrary smaller BAGMAN-specific cutoff.
DEFAULT_MAX_FILENAME_LENGTH = 255

#: Characters that are never acceptable in a retained filename,
#: regardless of position (PID §14: "NUL/control characters"). Every
#: C0 control character (0x00-0x1F) plus DEL (0x7F) and the C1 control
#: range (0x80-0x9F, reachable via a raw byte string decoded to
#: Unicode). NUL is the single most dangerous one (many C libraries/
#: filesystems truncate a path at the first NUL), but every control
#: character is rejected outright rather than allow-listing "NUL is
#: bad but tab is fine" distinctions that would need constant
#: revisiting.
def _has_control_character(name: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F or 0x80 <= ord(ch) <= 0x9F for ch in name)


#: Path separators that must never appear in a retained filename (PID
#: §14: "dangerous path separators"). Forward slash is the POSIX
#: separator; backslash is the Windows separator — rejected on every
#: platform BAGMAN might run on, not just the host OS's own native
#: one, since the untrusted string is never used as a real path
#: component anywhere regardless.
_DANGEROUS_SEPARATORS = ("/", "\\")

#: CD-5 WI-5 (PID §66) — Unicode bidi/directional-format control
#: characters rejected outright, regardless of position. See this
#: module's own docstring ("Bidi/format-control hardening") for the
#: exact rationale for each member and why this set (not a broader
#: confusable-script defence) is the right bounded scope.
#: Built via `chr(codepoint)` rather than embedding the literal
#: characters in this source file: these code points are, by
#: definition, invisible-or-rendering-altering (that is the entire
#: property this module exists to reject) — writing them literally
#: into a `.py` file would make THIS file's own source visually
#: reorder/hide itself in an editor or terminal, which is exactly the
#: confusing effect being guarded against, not something to reproduce
#: here for the sake of a slightly shorter literal.
_BIDI_FORMAT_CONTROL_CHARACTERS = frozenset(
    chr(codepoint)
    for codepoint in (
        0x061C,  # ALM — Arabic Letter Mark
        0x200E,  # LRM — Left-to-Right Mark
        0x200F,  # RLM — Right-to-Left Mark
        0x202A,  # LRE — Left-to-Right Embedding
        0x202B,  # RLE — Right-to-Left Embedding
        0x202C,  # PDF — Pop Directional Formatting
        0x202D,  # LRO — Left-to-Right Override
        0x202E,  # RLO — Right-to-Left Override (the classic spoofed-
                 # extension attack: "invoice" + RLO + "cod.exe" renders,
                 # right-to-left from the override on, as something a
                 # human reads left-to-right as ending "...exe.doc")
        0x2066,  # LRI — Left-to-Right Isolate
        0x2067,  # RLI — Right-to-Left Isolate
        0x2068,  # FSI — First Strong Isolate
        0x2069,  # PDI — Pop Directional Isolate
    )
)


def _has_bidi_format_control_character(name: str) -> bool:
    return any(ch in _BIDI_FORMAT_CONTROL_CHARACTERS for ch in name)


def is_filename_safe(
    original_filename: Optional[str],
    *,
    max_length: int = DEFAULT_MAX_FILENAME_LENGTH,
) -> bool:
    """Return ``True`` iff ``original_filename`` is safe to retain for
    display (PID §14). ``None`` (no filename supplied at all) is
    always safe — a caller is never required to supply one.

    Rejects (returns ``False`` for):

    * ``../`` or any ``..`` path-traversal segment
    * an absolute path (POSIX ``/...`` or Windows ``C:\\...``/``\\\\...``)
    * any NUL/control character
    * a forward or back slash anywhere in the name (a bare filename
      component should never contain a path separator at all — PID
      §14's "must never control filesystem/object paths" is easiest to
      guarantee by refusing to retain anything that even looks like a
      path rather than a single filename component)
    * a name exceeding ``max_length`` characters
    * an empty string (distinct from ``None``: an uploader that
      explicitly supplied ``""`` supplied something unsafe/meaningless,
      not "nothing")
    * a name that is only "." or ".." (not caught by the separator
      check alone, since neither contains a slash)
    * ambiguous-Unicode content that materially changes the string's
      apparent value/length when NFC-normalised (PID §14's "ambiguous
      Unicode where materially unsafe") — checked defensively via a
      round-trip comparison rather than attempting to allow-list
      "safe" Unicode ranges, which is an open-ended problem this
      narrow display-only field does not need to solve generally.
    * any Unicode bidi/directional-format control character (CD-5 WI-5,
      PID §66) — see this module's own docstring for the exact set and
      rationale; these are not caught by the NFKC round-trip check
      above (bidi controls are format characters, not compatibility-
      equivalent ones) and must be checked independently.
    """
    if original_filename is None:
        return True
    if not isinstance(original_filename, str):
        return False
    if original_filename == "" or original_filename in (".", ".."):
        return False
    if _has_bidi_format_control_character(original_filename):
        return False
    if len(original_filename) > max_length:
        return False
    if _has_control_character(original_filename):
        return False
    if any(sep in original_filename for sep in _DANGEROUS_SEPARATORS):
        return False
    if ".." in original_filename:
        return False
    # Windows-style absolute/drive-qualified path (e.g. "C:\\foo",
    # "C:/foo") even though "\\"/"/" are already rejected above, this
    # also catches a bare drive prefix on its own.
    if len(original_filename) >= 2 and original_filename[1] == ":":
        return False
    # Defensive Unicode-ambiguity guard: if NFKC normalisation (the
    # COMPATIBILITY form — unlike NFC, this also collapses ligatures,
    # full-width/half-width variants, and other compatibility
    # equivalences such as U+FB01 "ﬁ" -> "fi") changes the string at
    # all, distinct byte sequences could render/compare identically to
    # a human/downstream system while differing to BAGMAN's own
    # straightforward string checks above — refuse rather than reason
    # about which such differences are "materially unsafe".
    if unicodedata.normalize("NFKC", original_filename) != original_filename:
        return False
    return True


def assert_filename_safe(
    original_filename: Optional[str],
    *,
    max_length: int = DEFAULT_MAX_FILENAME_LENGTH,
) -> None:
    """Raise :class:`core.errors.ValidationError` if
    :func:`is_filename_safe` would return ``False``; otherwise return
    ``None``. The message never echoes the raw filename back verbatim
    at more than a bounded, escaped preview length — the filename is
    hostile input and this error may end up in a log line."""
    if is_filename_safe(original_filename, max_length=max_length):
        return
    preview = "" if original_filename is None else repr(original_filename[:80])
    raise ValidationError(
        "original_filename failed PID §14/§66 safety validation (path traversal, "
        "absolute path, control character, bidi/directional-format control "
        "character, path separator, pathological length, or ambiguous Unicode) "
        f"— refusing to retain it even for display: {preview}"
    )
