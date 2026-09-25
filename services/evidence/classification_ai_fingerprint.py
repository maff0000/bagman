"""Deterministic AI classifier fingerprint (CD-6 Slice 5 WI-3 §23-24).

A fingerprint means: "this exact evidence/context/task/prompt contract
has already been analyzed." It deliberately does NOT include the
physical `provider_model` LiteLLM actually routed to — a later physical
model change behind the SAME `bagman-core` alias does not automatically
invalidate/reclassify historical evidence; that is an explicit future
reprocessing decision, never an automatic side effect of infrastructure
changing underneath a fixed alias (WI-3 §24).

Used by `services.evidence.classification_orchestrator.classify_evidence`
for the automatic-invocation idempotency/reuse check (WI-3 §25-26): a
matching, SUCCEEDED prior `AIInvocation` for the exact same fingerprint
is reused rather than triggering a second model call.
"""
from __future__ import annotations

import hashlib
import json


def compute_classifier_fingerprint(
    *,
    task_id: str,
    task_version: int,
    prompt_contract_version: str,
    preferred_capability: str,
    classification_context_version: str,
    evidence_id: str,
    evidence_content_hash: str,
    classification_context_hash: str,
) -> str:
    """Return the deterministic SHA-256 hex digest fingerprint (WI-3
    §23) of exactly these eight fields, canonically (sorted-key, no
    extraneous whitespace) JSON-serialized — the same bytes in, the
    same fingerprint out, every time, on any process/host.
    """
    canonical_payload = {
        "task_id": task_id,
        "task_version": task_version,
        "prompt_contract_version": prompt_contract_version,
        "preferred_capability": preferred_capability,
        "classification_context_version": classification_context_version,
        "evidence_id": evidence_id,
        "evidence_content_hash": evidence_content_hash,
        "classification_context_hash": classification_context_hash,
    }
    canonical_json = json.dumps(canonical_payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
