"""Contract tests for `contracts/ai/bagman.ai_invocation.v1.schema.json`
(CD-5 WI-1, PID §26-30/§73).
"""
from __future__ import annotations

import pytest

from core.contract_validation import validate_against_contract
from core.errors import ValidationError

SCHEMA = "ai/bagman.ai_invocation.v1.schema.json"


def test_valid_ai_invocation_is_accepted(make_ai_invocation):
    validate_against_contract(make_ai_invocation(), SCHEMA)


def test_ai_invocation_missing_required_field_is_rejected(make_ai_invocation):
    instance = make_ai_invocation()
    del instance["status"]
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_ai_invocation_unknown_additional_property_is_rejected(make_ai_invocation):
    instance = make_ai_invocation()
    instance["not_a_real_field"] = "nope"
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


# ---------------------------------------------------------------------
# status (PID §28)
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "valid_status",
    # TIMED_OUT/CANCELLED added by the CD-6 reliability delta (PID §100.14/§100.16).
    ["REQUESTED", "RUNNING", "SUCCEEDED", "FAILED", "REJECTED", "TIMED_OUT", "CANCELLED"],
)
def test_every_pid_section_28_status_value_is_accepted(make_ai_invocation, valid_status):
    instance = make_ai_invocation(status=valid_status)
    validate_against_contract(instance, SCHEMA)


def test_status_is_a_closed_enum_rejecting_unknown_values(make_ai_invocation):
    instance = make_ai_invocation(status="SOMETHING_MADE_UP")
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


# ---------------------------------------------------------------------
# role / provider / capability_alias closed sets and cross-field
# correspondence (PID §2/§8/§9)
# ---------------------------------------------------------------------


def test_background_role_with_litellm_provider_and_valid_alias_is_accepted(make_ai_invocation):
    instance = make_ai_invocation(role="BACKGROUND", provider="LITELLM", capability_alias="bagman-core")
    validate_against_contract(instance, SCHEMA)


def test_operator_role_with_anthropic_provider_and_null_alias_is_accepted(make_ai_invocation):
    instance = make_ai_invocation(role="OPERATOR", provider="ANTHROPIC", capability_alias=None)
    validate_against_contract(instance, SCHEMA)


def test_role_is_a_closed_enum(make_ai_invocation):
    instance = make_ai_invocation(role="SOMETHING_MADE_UP")
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_provider_is_a_closed_enum(make_ai_invocation):
    instance = make_ai_invocation(provider="OLLAMA")
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


@pytest.mark.parametrize("valid_alias", ["bagman-fast", "bagman-core", "trinity-core"])
def test_every_background_capability_alias_is_accepted(make_ai_invocation, valid_alias):
    instance = make_ai_invocation(capability_alias=valid_alias)
    validate_against_contract(instance, SCHEMA)


def test_bagman_deep_is_no_longer_a_valid_capability_alias(make_ai_invocation):
    """CD-6 §103 Inference Architecture Ruling: `bagman-deep` is
    RETIRED — no longer a member of the closed `capability_alias` enum
    at all (see `ai.invocation.BACKGROUND_CAPABILITY_ALIASES`'s own
    docstring for the full history)."""
    instance = make_ai_invocation(capability_alias="bagman-deep")
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


@pytest.mark.parametrize("bad_alias", ["trinity-fast", "trinity-deep", "trinity-embed"])
def test_a_forbidden_trinity_star_alias_is_rejected(make_ai_invocation, bad_alias):
    """PID §9/CD-6 §103: `trinity-core` is the SOLE authorised Trinity
    alias (see `test_every_background_capability_alias_is_accepted`
    above) — every other `trinity-*` alias remains rejected."""
    instance = make_ai_invocation(capability_alias=bad_alias)
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_an_arbitrary_raw_model_name_as_capability_alias_is_rejected(make_ai_invocation):
    """PID §5/§10: capability_alias must never become a place a caller
    injects a raw/physical model name."""
    instance = make_ai_invocation(capability_alias="gemma-3-12b-it-q4")
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_background_role_requires_a_non_null_capability_alias(make_ai_invocation):
    instance = make_ai_invocation(role="BACKGROUND", provider="LITELLM", capability_alias=None)
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_operator_role_requires_a_null_capability_alias(make_ai_invocation):
    instance = make_ai_invocation(role="OPERATOR", provider="ANTHROPIC", capability_alias="bagman-fast")
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


# ---------------------------------------------------------------------
# inference_backend (CD-6 §103 Inference Architecture Ruling)
# ---------------------------------------------------------------------


@pytest.mark.parametrize("valid_backend", ["MAC_LOCAL", "TRINITY_CORE_OVERFLOW"])
def test_every_inference_backend_value_is_accepted(make_ai_invocation, valid_backend):
    instance = make_ai_invocation(inference_backend=valid_backend)
    validate_against_contract(instance, SCHEMA)


def test_inference_backend_is_a_closed_enum_rejecting_unknown_values(make_ai_invocation):
    instance = make_ai_invocation(inference_backend="SOMETHING_MADE_UP")
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_inference_backend_is_a_required_field(make_ai_invocation):
    instance = make_ai_invocation()
    del instance["inference_backend"]
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_provider_model_null_is_accepted(make_ai_invocation):
    instance = make_ai_invocation(provider_model=None)
    validate_against_contract(instance, SCHEMA)


def test_provider_model_string_is_accepted(make_ai_invocation):
    instance = make_ai_invocation(provider_model="claude-sonnet-4-5-20260101")
    validate_against_contract(instance, SCHEMA)


# ---------------------------------------------------------------------
# actor (PID §14, reused exactly)
# ---------------------------------------------------------------------


def test_actor_missing_key_is_rejected(make_ai_invocation):
    instance = make_ai_invocation()
    del instance["actor"]
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_actor_invalid_actor_type_is_rejected(make_ai_invocation):
    instance = make_ai_invocation(actor={"actor_type": "NOT_A_REAL_TYPE", "actor_id": "matt"})
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_actor_unknown_additional_property_is_rejected(make_ai_invocation):
    instance = make_ai_invocation()
    instance["actor"] = {"actor_type": "USER", "actor_id": "matt", "extra": "nope"}
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


@pytest.mark.parametrize("valid_actor_type", ["SYSTEM", "USER", "AGENT", "SERVICE", "EXTERNAL_SYSTEM"])
def test_every_closed_actor_type_is_accepted(make_ai_invocation, valid_actor_type):
    instance = make_ai_invocation(actor={"actor_type": valid_actor_type, "actor_id": "matt"})
    validate_against_contract(instance, SCHEMA)


# ---------------------------------------------------------------------
# input_references (PID §29) — open object
# ---------------------------------------------------------------------


def test_input_references_with_arbitrary_recognised_keys_is_accepted(make_ai_invocation):
    instance = make_ai_invocation(input_references={"evidence_id": "abc", "intake_id": "def"})
    validate_against_contract(instance, SCHEMA)


def test_input_references_missing_key_is_rejected(make_ai_invocation):
    instance = make_ai_invocation()
    del instance["input_references"]
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_input_references_with_only_conversation_id_is_accepted(make_ai_invocation):
    """CD-6 reliability delta (PID §98/§100): a genuinely contextless
    Ask BAGMAN turn's `input_references` — no evidence/intake/entity_id
    at all — is still a valid instance of this open contract."""
    instance = make_ai_invocation(input_references={"message": "hi bagman", "conversation_id": "conv-1"})
    validate_against_contract(instance, SCHEMA)


# ---------------------------------------------------------------------
# confidence (PID §55)
# ---------------------------------------------------------------------


def test_confidence_null_is_accepted(make_ai_invocation):
    instance = make_ai_invocation(confidence=None)
    validate_against_contract(instance, SCHEMA)


@pytest.mark.parametrize("valid_confidence", [0, 0.5, 1])
def test_confidence_within_bounds_is_accepted(make_ai_invocation, valid_confidence):
    instance = make_ai_invocation(confidence=valid_confidence)
    validate_against_contract(instance, SCHEMA)


@pytest.mark.parametrize("invalid_confidence", [-0.1, 1.1])
def test_confidence_out_of_bounds_is_rejected(make_ai_invocation, invalid_confidence):
    instance = make_ai_invocation(confidence=invalid_confidence)
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


# ---------------------------------------------------------------------
# validation_result (PID §24/§76)
# ---------------------------------------------------------------------


def test_validation_result_null_is_accepted(make_ai_invocation):
    instance = make_ai_invocation(validation_result=None)
    validate_against_contract(instance, SCHEMA)


def test_validation_result_valid_shape_is_accepted(make_ai_invocation):
    instance = make_ai_invocation(validation_result={"valid": False, "errors": ["confidence: required"]})
    validate_against_contract(instance, SCHEMA)


def test_validation_result_missing_errors_key_is_rejected(make_ai_invocation):
    instance = make_ai_invocation(validation_result={"valid": True})
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_validation_result_non_string_error_entry_is_rejected(make_ai_invocation):
    instance = make_ai_invocation(validation_result={"valid": False, "errors": [123]})
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


# ---------------------------------------------------------------------
# output / usage_metadata / latency_ms / schema_version
# ---------------------------------------------------------------------


def test_output_null_is_accepted(make_ai_invocation):
    instance = make_ai_invocation(output=None)
    validate_against_contract(instance, SCHEMA)


def test_output_object_is_accepted(make_ai_invocation):
    instance = make_ai_invocation(output={"proposed_type": "INVOICE"})
    validate_against_contract(instance, SCHEMA)


def test_usage_metadata_defaults_to_object_and_accepts_open_content(make_ai_invocation):
    instance = make_ai_invocation(usage_metadata={"input_tokens": 120, "output_tokens": 40})
    validate_against_contract(instance, SCHEMA)


def test_latency_ms_null_is_accepted(make_ai_invocation):
    instance = make_ai_invocation(latency_ms=None)
    validate_against_contract(instance, SCHEMA)


def test_latency_ms_negative_is_rejected(make_ai_invocation):
    instance = make_ai_invocation(latency_ms=-1)
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_schema_version_must_equal_the_pinned_const(make_ai_invocation):
    instance = make_ai_invocation(schema_version="bagman.ai_invocation.v2")
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)


def test_task_id_open_pattern_accepts_a_new_future_task(make_ai_invocation):
    """PID §21/§22: task_id is deliberately open (matching
    `bagman.audit_event.v1.event_type`'s own doctrine) so new tasks can
    be registered in `ai.tasks.TASK_REGISTRY` without a contract
    redesign."""
    instance = make_ai_invocation(task_id="SOME_FUTURE_TASK")
    validate_against_contract(instance, SCHEMA)


def test_task_version_below_one_is_rejected(make_ai_invocation):
    instance = make_ai_invocation(task_version=0)
    with pytest.raises(ValidationError):
        validate_against_contract(instance, SCHEMA)
