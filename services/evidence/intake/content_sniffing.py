"""Byte-level content-type detection (PID §15/§16/§17/§18, CD-4 WI-2).

Distinguishes ``reported_mime_type`` (an uploader-supplied HINT — never
trusted) from ``detected_mime_type`` (derived from the actual bytes,
PID §15). This module owns exactly the "derived from actual bytes"
half; ``services.evidence.intake.validation_pipeline`` is what compares
the two and decides what to do about a mismatch.

Hand-rolled magic-byte sniffing, deliberately, instead of a
``python-magic`` dependency
------------------------------------------------------------------------
PID §16's initial accepted set is five well-known formats
(PDF/JPEG/PNG/plain-text/CSV), plus PID §17/§18 asks for archives and
executable content to be detectable. Every one of these has either a
fixed magic-byte signature (PDF/JPEG/PNG/ZIP/GZIP/RAR/7z/ELF/PE) or a
simple structural tell (a shebang line for a script; UTF-8-decodability
plus an absence of NUL bytes for plain text; comma-delimited line
structure for a CSV heuristic). None of this needs a real content-
sniffing library. ``python-magic`` was considered and deliberately
rejected for this narrow set: it wraps the system ``libmagic`` C
library, which is not a pure-Python/pip-installable dependency — a
clean CI/container build would need an OS package
(``libmagic1``/``file``) installed *outside* ``pip install -r
requirements.txt``, which is exactly the "system package that would
break a clean-environment CI run" risk this work item was told to avoid
whenever there is any doubt. There was doubt, so the hand-rolled
sniffer below is used instead — zero extra runtime/system dependency,
and the entire detection surface is auditable in one small,
plain-Python file.

Known, documented limitations of this approach (not fixed here,
because fixing them needs more than magic-byte sniffing can honestly
provide):

* **CSV vs. plain text** is a structural, not a magic-byte, distinction
  — nothing about a CSV file's *bytes* differs from any other
  comma-containing text file. :func:`sniff` applies a best-effort
  heuristic (see ``_looks_like_csv``) only after bytes have already
  been classified as printable text; getting this heuristic "wrong"
  (a CSV mislabelled ``text/plain`` or vice versa) has **no security
  consequence** in this policy — both labels are treated identically
  safe/non-executable by every downstream check in
  ``services.evidence.intake.policy``/``validation_pipeline``. This is
  purely a labelling nicety, never a safety boundary.
* **Script/executable detection** covers ELF, Windows PE (``MZ``
  header), and any file beginning with a ``#!`` shebang line (labelled
  generically ``text/x-shellscript`` regardless of the interpreter
  named after ``#!`` — this deliberately does not attempt to
  distinguish "shell script" from "Python script" from "Node script"
  by parsing the interpreter path; PID §18's actual concern is
  "executable/script content must not become ordinary evidence",
  which this generic label already satisfies for policy-routing
  purposes). A JavaScript/Python/etc. file with **no** shebang line and
  no other binary signature (i.e. looks like ordinary source text) is
  NOT flagged as executable by this module — static byte-sniffing
  cannot distinguish arbitrary interpreted-language source text from
  any other plain text without actually parsing/executing it, and this
  work item does not attempt that. This is a real, documented gap;
  see the delivery report for the explicit call-out.
"""
from __future__ import annotations

from dataclasses import dataclass

#: PID §17's named archive formats, plus the MIME labels this module
#: assigns them.
ARCHIVE_MIME_TYPES = frozenset(
    {
        "application/zip",
        "application/gzip",
        "application/x-tar",
        "application/x-rar-compressed",
        "application/x-7z-compressed",
    }
)

#: PID §18's named executable/script categories, plus the MIME labels
#: this module assigns them.
EXECUTABLE_MIME_TYPES = frozenset(
    {
        "application/x-elf",
        "application/x-msdownload",
        "text/x-shellscript",
    }
)

#: Returned when nothing recognisable matched — an "unknown binary
#: format" in PID §16's own words.
UNKNOWN_BINARY_MIME_TYPE = "application/octet-stream"

#: How many leading bytes :func:`sniff` needs at most. 512 covers every
#: magic signature checked here, including the POSIX tar ``ustar``
#: magic at byte offset 257 (257 + 8 = 265, comfortably inside 512).
#: Callers only need to read this many bytes from a spooled file —
#: never the whole file — to sniff its type.
SNIFF_SAMPLE_SIZE = 512

#: How many bytes of a sample the plain-text/CSV heuristics examine.
_TEXT_HEURISTIC_SAMPLE = 4096


@dataclass(frozen=True)
class SniffResult:
    """The outcome of sniffing a byte sample."""

    mime_type: str

    @property
    def is_archive(self) -> bool:
        return self.mime_type in ARCHIVE_MIME_TYPES

    @property
    def is_executable(self) -> bool:
        return self.mime_type in EXECUTABLE_MIME_TYPES

    @property
    def is_unknown(self) -> bool:
        return self.mime_type == UNKNOWN_BINARY_MIME_TYPE


def _looks_like_csv(sample: bytes) -> bool:
    """Best-effort CSV heuristic — see module docstring's "known
    limitations" section. Only ever called after ``sample`` has already
    been confirmed to decode cleanly as text."""
    text = sample.decode("utf-8", errors="strict")
    lines = [line for line in text.splitlines() if line.strip() != ""][:20]
    if len(lines) < 2:
        return False
    comma_counts = {line.count(",") for line in lines}
    return len(comma_counts) == 1 and next(iter(comma_counts)) >= 1


def _is_plain_text(sample: bytes) -> bool:
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return False
    return True


def sniff(sample: bytes) -> SniffResult:
    """Detect the MIME type of ``sample`` — the leading bytes of a
    file (at least :data:`SNIFF_SAMPLE_SIZE` bytes when available; a
    shorter sample, including an empty one, is handled without error).

    Checks fixed magic-byte signatures first (PDF/JPEG/PNG/ZIP/GZIP/
    RAR/7z/ELF/PE), then a shebang line (script), then falls back to
    the plain-text/CSV heuristic, and finally
    :data:`UNKNOWN_BINARY_MIME_TYPE` if nothing matched.
    """
    if not sample:
        # An empty upload has no bytes to sniff at all — treated as
        # unknown/unsupported rather than optimistically "text/plain",
        # so it is routed through the same accepted-type policy check
        # as any other unrecognised content (PID §63's "empty file"
        # fixture case) rather than silently sailing through as valid
        # plain text.
        return SniffResult(UNKNOWN_BINARY_MIME_TYPE)

    if sample.startswith(b"%PDF-"):
        return SniffResult("application/pdf")
    if sample.startswith(b"\xff\xd8\xff"):
        return SniffResult("image/jpeg")
    if sample.startswith(b"\x89PNG\r\n\x1a\n"):
        return SniffResult("image/png")

    # Archives (PID §17).
    if sample[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return SniffResult("application/zip")
    if sample.startswith(b"\x1f\x8b"):
        return SniffResult("application/gzip")
    if sample[257:262] == b"ustar":
        return SniffResult("application/x-tar")
    if sample.startswith(b"Rar!\x1a\x07\x00") or sample.startswith(b"Rar!\x1a\x07\x01\x00"):
        return SniffResult("application/x-rar-compressed")
    if sample.startswith(b"7z\xbc\xaf\x27\x1c"):
        return SniffResult("application/x-7z-compressed")

    # Executable content (PID §18) — detected content overrides
    # whatever extension/reported MIME type accompanied the upload.
    if sample.startswith(b"\x7fELF"):
        return SniffResult("application/x-elf")
    if sample.startswith(b"MZ"):
        return SniffResult("application/x-msdownload")
    if sample.startswith(b"#!"):
        return SniffResult("text/x-shellscript")

    text_sample = sample[:_TEXT_HEURISTIC_SAMPLE]
    if _is_plain_text(text_sample):
        if _looks_like_csv(text_sample):
            return SniffResult("text/csv")
        return SniffResult("text/plain")

    return SniffResult(UNKNOWN_BINARY_MIME_TYPE)
