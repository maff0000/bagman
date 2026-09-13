"""Tests for `services.evidence.intake.policy` (CD-4 WI-2, PID §16/§34)."""
from __future__ import annotations

import pytest

from services.evidence.intake.policy import (
    DEFAULT_ACCEPTED_MIME_TYPES,
    DEFAULT_INTAKE_POLICY,
    IntakePolicy,
)


def test_default_policy_accepts_pid_section_16_initial_set():
    assert DEFAULT_INTAKE_POLICY.accepted_mime_types == DEFAULT_ACCEPTED_MIME_TYPES
    assert DEFAULT_ACCEPTED_MIME_TYPES == {
        "application/pdf",
        "image/jpeg",
        "image/png",
        "text/plain",
        "text/csv",
    }


def test_default_policy_is_identifiable():
    assert DEFAULT_INTAKE_POLICY.policy_id == "bagman.intake_policy.v1"


def test_default_policy_documented_treatments():
    assert DEFAULT_INTAKE_POLICY.archive_treatment == "REJECT"
    assert DEFAULT_INTAKE_POLICY.executable_treatment == "QUARANTINE"
    assert DEFAULT_INTAKE_POLICY.unsupported_content_treatment == "REJECT"
    assert DEFAULT_INTAKE_POLICY.scanner_required is True
    assert DEFAULT_INTAKE_POLICY.scan_error_treatment == "FAIL"
    assert DEFAULT_INTAKE_POLICY.scan_unsupported_treatment == "QUARANTINE"
    assert DEFAULT_INTAKE_POLICY.mime_mismatch_policy == "OBSERVE"


def test_policy_is_frozen():
    with pytest.raises(Exception):  # dataclasses.FrozenInstanceError subclasses AttributeError
        DEFAULT_INTAKE_POLICY.max_file_size_bytes = 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_file_size_bytes": 0},
        {"max_file_size_bytes": -1},
        {"max_request_size_bytes": 0},
        {"max_filename_length": 0},
        {"archive_treatment": "BOGUS"},
        {"executable_treatment": "BOGUS"},
        {"unsupported_content_treatment": "BOGUS"},
        {"scan_error_treatment": "BOGUS"},
        {"scan_unsupported_treatment": "BOGUS"},
        {"mime_mismatch_policy": "BOGUS"},
    ],
)
def test_invalid_policy_values_are_rejected_at_construction(kwargs):
    with pytest.raises(ValueError):
        IntakePolicy(**kwargs)


def test_a_custom_policy_can_relax_defaults_without_mutating_the_shared_default():
    custom = IntakePolicy(policy_id="bagman.intake_policy.v1-test", max_file_size_bytes=1024)
    assert custom.max_file_size_bytes == 1024
    assert DEFAULT_INTAKE_POLICY.max_file_size_bytes != 1024
