"""Filename safety validation for untrusted uploader-supplied filenames
(PID §14, CD-4 WI-2).

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
    """
    if original_filename is None:
        return True
    if not isinstance(original_filename, str):
        return False
    if original_filename == "" or original_filename in (".", ".."):
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
        "original_filename failed PID §14 safety validation (path traversal, "
        "absolute path, control character, path separator, pathological "
        "length, or ambiguous Unicode) — refusing to retain it even for "
        f"display: {preview}"
    )
