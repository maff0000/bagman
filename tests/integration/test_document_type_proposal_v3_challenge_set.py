"""CD-6 follow-up — Task 4/5 proofs for `scripts/evaluate_document_classifier
.py`'s new `--ai-only` evaluation path (`evaluate_item_ai_only`/
`run_ai_only_evaluation`): a version-aware evaluation mode that
bypasses `classify_evidence`'s deterministic-first check entirely, so a
real, separately-created deterministic rule for the exact corporate-
event family v3's own prompt fix targets can never mask whether the AI
TASK ITSELF resolved that boundary correctly.

This is plumbing-correctness proof only (`FakeLiteLLMClient`, scripted
responses matching each fixture's own `expected_document_type`) — NOT a
live-model accuracy proof. Judging the real model's actual accuracy
against this (or any other) fixture set is the PL's own job, run
separately against the real production model (see
`tests/acceptance/document_type_proposal_v2_live_proof.py`'s own
identical "harness vs. live judgment" split for v2).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai.providers.litellm.fake import FakeLiteLLMClient

import scripts.evaluate_document_classifier as evl

_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "document_type_proposal_v3_challenge_set_synthetic.json"
)


@pytest.fixture
def challenge_set_items():
    return evl.load_challenge_set(str(_FIXTURE_PATH))


def _queue_expected_response(litellm_client, *, expected_document_type: str):
    litellm_client.queue_success(
        capability_alias="bagman-core",
        content=json.dumps(
            {
                "proposed_type": expected_document_type,
                "confidence": 0.9,
                "signals": ["synthetic scripted response for challenge-set plumbing proof"],
                "warnings": [],
            }
        ),
    )


# ---------------------------------------------------------------------
# Fixture-file shape/content proofs (Task 5)
# ---------------------------------------------------------------------


def test_challenge_set_fixture_is_synthetic_and_never_names_a_real_evidence_id():
    raw = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert "SYNTHETIC TEST DATA" in raw["_synthetic_notice"]
    assert len(raw["items"]) >= 10
    for entry in raw["items"]:
        assert "evidence_id" not in entry
        assert "SYNTHETIC TEST FIXTURE" in entry["body"]


def test_challenge_set_fixture_has_at_least_five_of_each_expected_family(challenge_set_items):
    non_accounting = [i for i in challenge_set_items if i["expected_document_type"] == "NON_ACCOUNTING_DOCUMENT"]
    broker_activity = [i for i in challenge_set_items if i["expected_document_type"] == "BROKER_ACTIVITY_NOTICE"]
    assert len(non_accounting) >= 5
    assert len(broker_activity) >= 5


def test_load_challenge_set_rejects_a_non_object_file(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(SystemExit):
        evl.load_challenge_set(str(path))


def test_load_challenge_set_rejects_entries_missing_required_fields(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"items": [{"sender": "a@b.example"}]}), encoding="utf-8")
    with pytest.raises(SystemExit):
        evl.load_challenge_set(str(path))


# ---------------------------------------------------------------------
# End-to-end harness plumbing proof against v3, via FakeLiteLLMClient
# ---------------------------------------------------------------------


def test_challenge_set_resolves_correctly_end_to_end_against_v3(challenge_set_items):
    litellm_client = FakeLiteLLMClient()
    for item in challenge_set_items:
        _queue_expected_response(litellm_client, expected_document_type=item["expected_document_type"])

    report = evl.run_ai_only_evaluation(
        items=challenge_set_items,
        task_version=3,
        litellm_client=litellm_client,
        manifest_file=str(_FIXTURE_PATH),
    )

    assert report["mode"] == "AI_ONLY_DIRECT"
    assert report["task_version"] == 3
    assert report["preferred_capability"] == "bagman-core"
    assert report["prompt_contract_version"] == "v3"
    assert report["overall"]["total"] == len(challenge_set_items)
    assert report["overall"]["correct"] == len(challenge_set_items)
    assert report["overall"]["incorrect"] == 0
    assert report["ai_metrics"]["total"] == len(challenge_set_items)
    assert report["ai_invocation_failures"] == []
    assert report["blocking_findings"] == []

    # Every recorded call used v3's own distinct prompt asset, not v2's.
    from ai.prompts.loader import load_system_prompt

    v3_prompt_text = load_system_prompt("DOCUMENT_TYPE_PROPOSAL", "v3")
    assert len(litellm_client.calls) == len(challenge_set_items)
    for call in litellm_client.calls:
        assert call.system_instructions == v3_prompt_text
        assert call.capability_alias == "bagman-core"


def test_challenge_set_also_runs_against_v2_without_code_duplication(challenge_set_items):
    """Proves the same core AI-calling logic serves both v2 and v3 with
    no duplication — only `task_version` differs."""
    litellm_client = FakeLiteLLMClient()
    for item in challenge_set_items:
        _queue_expected_response(litellm_client, expected_document_type=item["expected_document_type"])

    report = evl.run_ai_only_evaluation(
        items=challenge_set_items,
        task_version=2,
        litellm_client=litellm_client,
        manifest_file=str(_FIXTURE_PATH),
    )
    assert report["task_version"] == 2
    assert report["prompt_contract_version"] == "v2"
    assert report["overall"]["correct"] == len(challenge_set_items)


def test_a_genuine_v3_misclassification_is_honestly_reported_not_hidden(challenge_set_items):
    litellm_client = FakeLiteLLMClient()
    first_item = challenge_set_items[0]
    # Deliberately wrong scripted response for the first item only.
    wrong_type = (
        "BROKER_ACTIVITY_NOTICE" if first_item["expected_document_type"] != "BROKER_ACTIVITY_NOTICE" else "NON_ACCOUNTING_DOCUMENT"
    )
    _queue_expected_response(litellm_client, expected_document_type=wrong_type)
    for item in challenge_set_items[1:]:
        _queue_expected_response(litellm_client, expected_document_type=item["expected_document_type"])

    report = evl.run_ai_only_evaluation(
        items=challenge_set_items,
        task_version=3,
        litellm_client=litellm_client,
        manifest_file=str(_FIXTURE_PATH),
    )
    assert report["overall"]["incorrect"] == 1
    assert report["overall"]["correct"] == len(challenge_set_items) - 1


# ---------------------------------------------------------------------
# The deterministic-bypass is a genuine BEHAVIOURAL property, not just
# textual absence — proven by making classify_evidence explode if ever
# called.
# ---------------------------------------------------------------------


def test_run_ai_only_evaluation_never_calls_classify_evidence(monkeypatch, challenge_set_items):
    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("run_ai_only_evaluation must never call classify_evidence")

    monkeypatch.setattr(evl, "classify_evidence", _must_not_be_called)

    litellm_client = FakeLiteLLMClient()
    for item in challenge_set_items:
        _queue_expected_response(litellm_client, expected_document_type=item["expected_document_type"])

    report = evl.run_ai_only_evaluation(
        items=challenge_set_items,
        task_version=3,
        litellm_client=litellm_client,
        manifest_file=str(_FIXTURE_PATH),
    )
    assert report["overall"]["correct"] == len(challenge_set_items)


def test_evaluate_item_ai_only_needs_no_repository_of_any_kind_for_synthetic_items(challenge_set_items):
    """Structural proof that a synthetic item needs neither
    evidence_repository nor object_store at all — both stay `None` and
    the call still succeeds, proving zero repository involvement for
    this input shape."""
    litellm_client = FakeLiteLLMClient()
    item = challenge_set_items[0]
    _queue_expected_response(litellm_client, expected_document_type=item["expected_document_type"])

    result = evl.evaluate_item_ai_only(
        item, task_version=3, litellm_client=litellm_client, evidence_repository=None, object_store=None,
    )
    assert result.correct is True
    assert result.ai_invocation_id is None  # fully disposable — no AIInvocation row ever created
