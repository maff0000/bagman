"""Tests for `services.evidence.intake.filename_safety` (CD-4 WI-2,
PID §14/§62/§63).

`original_filename` is untrusted, hostile input — every case here
proves a specific PID §14 rejection reason, plus the "path traversal
attempt" synthetic fixture PID §63 explicitly asks for.
"""
from __future__ import annotations

import pytest

from core.errors import ValidationError
from services.evidence.intake.filename_safety import assert_filename_safe, is_filename_safe


def test_none_filename_is_always_safe():
    assert is_filename_safe(None) is True
    assert_filename_safe(None)  # must not raise


@pytest.mark.parametrize(
    "safe_name",
    [
        "invoice.pdf",
        "Q3 Statement (final).csv",
        "photo.JPG",
        "a" * 255,
        "receipt_2026-09-13.txt",
    ],
)
def test_ordinary_filenames_are_safe(safe_name):
    assert is_filename_safe(safe_name) is True
    assert_filename_safe(safe_name)  # must not raise


@pytest.mark.parametrize(
    "unsafe_name",
    [
        "../../etc/passwd",  # PID §63's explicit path-traversal fixture
        "../secret.txt",
        "..",
        "foo/../../bar.pdf",
        "/etc/passwd",  # absolute POSIX path
        "C:\\Windows\\System32\\evil.exe",  # absolute Windows path
        "C:/Windows/evil.exe",
        "invoice\\..\\..\\evil.pdf",
        "invoice.pdf/",  # any path separator at all
        "sub/invoice.pdf",
        "invoice\x00.pdf",  # NUL byte
        "invoice\n.pdf",  # control character
        "invoice\x7f.pdf",  # DEL
        "",  # explicitly empty, distinct from None
        ".",
        "a" * 256,  # over the default max length
    ],
)
def test_unsafe_filenames_are_rejected(unsafe_name):
    assert is_filename_safe(unsafe_name) is False
    with pytest.raises(ValidationError):
        assert_filename_safe(unsafe_name)


def test_ambiguous_unicode_normalisation_is_rejected():
    # U+FB01 LATIN SMALL LIGATURE FI normalises under NFC to "fi" — the
    # raw string differs from its own NFC form, which is exactly the
    # ambiguity this defensive guard refuses rather than reason about.
    name = "\ufb01le.pdf"
    assert is_filename_safe(name) is False


def test_max_length_is_configurable():
    name = "a" * 10 + ".pdf"
    assert is_filename_safe(name, max_length=5) is False
    assert is_filename_safe(name, max_length=100) is True


def test_assert_filename_safe_message_does_not_explode_on_long_hostile_input():
    # Defensive: the error message previews at most a bounded prefix of
    # the raw (hostile) filename rather than echoing it in full.
    hostile = "../" * 10000 + "evil.pdf"
    with pytest.raises(ValidationError) as excinfo:
        assert_filename_safe(hostile)
    assert len(str(excinfo.value)) < 1000
