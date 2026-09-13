"""Tests for `services.evidence.intake.content_sniffing` (CD-4 WI-2,
PID §15-18/§63).

Every fixture byte sequence here is hand-built synthetic content —
never a real document, never a real executable, never real malware
(PID §62/§63). Each is the smallest possible sequence that satisfies
the real format's magic-byte signature, not a full valid file — this
module only ever looks at the leading bytes anyway.
"""
from __future__ import annotations

from services.evidence.intake.content_sniffing import (
    SniffResult,
    UNKNOWN_BINARY_MIME_TYPE,
    sniff,
)


def test_sniff_pdf():
    result = sniff(b"%PDF-1.4\n%synthetic test content, not a real document\n")
    assert result.mime_type == "application/pdf"
    assert not result.is_archive
    assert not result.is_executable


def test_sniff_jpeg():
    result = sniff(b"\xff\xd8\xff\xe0synthetic-jpeg-bytes")
    assert result.mime_type == "image/jpeg"


def test_sniff_png():
    result = sniff(b"\x89PNG\r\n\x1a\nsynthetic-png-bytes")
    assert result.mime_type == "image/png"


def test_sniff_plain_text():
    result = sniff(b"This is synthetic plain text content for BAGMAN tests.\n")
    assert result.mime_type == "text/plain"


def test_sniff_csv_heuristic():
    sample = b"name,amount,date\nSynthetic Co,100.00,2026-09-01\nOther Ltd,50.00,2026-09-02\n"
    result = sniff(sample)
    assert result.mime_type == "text/csv"


def test_sniff_prose_with_occasional_commas_stays_plain_text():
    # A single line with commas, no consistent tabular structure across
    # multiple lines — must not be misclassified as CSV.
    sample = b"Hello, this is a synthetic note, with some commas, in it.\n"
    result = sniff(sample)
    assert result.mime_type == "text/plain"


def test_sniff_empty_sample_is_unknown_binary():
    result = sniff(b"")
    assert result.mime_type == UNKNOWN_BINARY_MIME_TYPE
    assert result.is_unknown


def test_sniff_zip_archive():
    result = sniff(b"PK\x03\x04synthetic-zip-local-file-header")
    assert result.mime_type == "application/zip"
    assert result.is_archive
    assert not result.is_executable


def test_sniff_gzip_archive():
    result = sniff(b"\x1f\x8b\x08\x00synthetic-gzip-bytes")
    assert result.mime_type == "application/gzip"
    assert result.is_archive


def test_sniff_tar_archive():
    # A minimal synthetic tar header: 257 arbitrary bytes, then the
    # POSIX ustar magic at the documented offset.
    sample = (b"\x00" * 257) + b"ustar\x00" + (b"\x00" * 100)
    result = sniff(sample)
    assert result.mime_type == "application/x-tar"
    assert result.is_archive


def test_sniff_rar_archive():
    result = sniff(b"Rar!\x1a\x07\x00synthetic-rar-bytes")
    assert result.mime_type == "application/x-rar-compressed"
    assert result.is_archive


def test_sniff_7z_archive():
    result = sniff(b"7z\xbc\xaf\x27\x1csynthetic-7z-bytes")
    assert result.mime_type == "application/x-7z-compressed"
    assert result.is_archive


def test_sniff_elf_executable():
    result = sniff(b"\x7fELF\x02\x01\x01\x00synthetic-elf-header")
    assert result.mime_type == "application/x-elf"
    assert result.is_executable
    assert not result.is_archive


def test_sniff_pe_executable():
    result = sniff(b"MZ\x90\x00\x03\x00synthetic-pe-header")
    assert result.mime_type == "application/x-msdownload"
    assert result.is_executable


def test_sniff_shebang_script_is_executable():
    result = sniff(b"#!/bin/sh\necho 'synthetic test script, not real malware'\n")
    assert result.mime_type == "text/x-shellscript"
    assert result.is_executable


def test_sniff_extension_never_influences_detection():
    """A ``.pdf``-named upload whose bytes are actually an ELF header
    must be detected as the ELF it actually is (PID §18: "detected
    content overrides filename extension") — this module never even
    sees a filename, which is itself the strongest possible proof: it
    has no way to be influenced by one."""
    result = sniff(b"\x7fELF\x02\x01\x01\x00pretending-to-be-a-pdf")
    assert result.mime_type == "application/x-elf"


def test_sniff_result_is_a_frozen_dataclass_value():
    a = sniff(b"%PDF-1.4")
    b = sniff(b"%PDF-1.4 different trailing bytes")
    assert isinstance(a, SniffResult)
    assert a.mime_type == b.mime_type
