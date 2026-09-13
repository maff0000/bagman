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


@pytest.mark.parametrize(
    "codepoint,label",
    [
        (0x061C, "ALM"),
        (0x200E, "LRM"),
        (0x200F, "RLM"),
        (0x202A, "LRE"),
        (0x202B, "RLE"),
        (0x202C, "PDF"),
        (0x202D, "LRO"),
        (0x202E, "RLO"),
        (0x2066, "LRI"),
        (0x2067, "RLI"),
        (0x2068, "FSI"),
        (0x2069, "PDI"),
    ],
)
def test_bidi_format_control_characters_are_rejected_anywhere_in_the_name(codepoint, label):
    """CD-5 WI-5 (PID §66): every bidi/directional-format control
    character this module documents must be rejected regardless of
    position (start, middle, end) in an otherwise-ordinary filename."""
    char = chr(codepoint)
    for candidate in (f"{char}invoice.pdf", f"invoice{char}.pdf", f"invoice.pdf{char}"):
        assert is_filename_safe(candidate) is False, f"{label} ({candidate!r}) should be rejected"
        with pytest.raises(ValidationError):
            assert_filename_safe(candidate)


def test_bidi_override_spoofed_extension_attack_is_rejected():
    """The canonical real-world attack this hardening item exists to
    close (PID §66's own "invisible/rendering-altering" concern, and
    CD-4's Auditor's own original finding): a filename using U+202E
    (RLO) so that a dangerous true extension (`.exe`) is what a human
    operator's save/download dialog actually WRITES to disk, while the
    text visually appears to end in a harmless extension (`.png`) —
    the classic "invoice<RLO>gnp.exe" pattern, which renders (RLO
    onward) as "invoice" + the reverse of "exe.png" = "invoice" +
    "gnp.exe" reversed-on-screen to read as "...exe.png"-shaped text,
    while the actual on-disk/underlying character sequence still ends
    literally in ".exe". BAGMAN must refuse to retain this filename at
    all, exactly like the existing path-traversal/control-character
    fixtures above — never attempt to "safely render" it.
    """
    rlo = "‮"
    hostile = f"invoice{rlo}gnp.exe"
    assert is_filename_safe(hostile) is False
    with pytest.raises(ValidationError):
        assert_filename_safe(hostile)
    # The intake-level UNSAFE_FILENAME failure_code this rejection
    # surfaces as end-to-end is proven at
    # tests/integration/test_intake_validation_pipeline.py::
    # test_bidi_override_spoofed_extension_filename_is_rejected_before_any_scanning
    # — this module only owns is_filename_safe/assert_filename_safe
    # themselves, not the pipeline's failure_code mapping.


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
