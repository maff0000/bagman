#!/usr/bin/env python3
"""scripts/reprocess_evidence_classification.py — CD-6 Slice 5 WI-6 §4-18.

Governed, bounded, resumable, DETERMINISTIC_RULE_ONLY historical
reprocessing tool for `EvidenceClassification`. Mirrors
`scripts/migrate_object_store.py`'s own dry-run-safety-by-default,
checkpoint/resume, single-JSON-report conventions (read that module's
docstring first if this one is unclear on a point it shares).

What this tool is, and is not
------------------------------------------------------------------------
* A bounded, operator-invoked maintenance utility — a CLI script, never
  an HTTP endpoint (WI-6 §3). There is still no "classify everything"
  route anywhere in this codebase, and this file adds none.
* DETERMINISTIC_RULE_ONLY (WI-6 §4) — this tool never calls any AI
  provider, never imports `ai.*`, and never touches
  `AIInvocation`/`NeedsYouItem` in any way. See
  `tests/integration/test_architecture_boundaries.py`'s WI-6 additions
  for the static proof.
* Rule-scoped (WI-6 §5): a run always names exactly one
  `EvidenceClassificationRule.rule_id`. This tool fetches the REAL
  ACTIVE rule via `rule_repository.get_rule(rule_id)` and reuses the
  authoritative WI-2 matcher (`classification_matcher.match_evidence_to_rule`)
  scoped to that ONE rule (passed as a one-element `active_rules` list —
  the matcher itself takes any `Sequence[EvidenceClassificationRule]`
  and does not care how many are passed) for planning/candidate
  identification. It never accepts sender/subject/document_type
  semantics as separate CLI flags that could diverge from the rule's
  own stored, governed identity.
* Never supersedes, never creates a rule, never mutates entity
  ownership: the actual MUTATION step (`--apply`) reuses the EXISTING,
  real, governed
  `services.evidence.classification_service.classify_evidence_deterministically`
  function wholesale — this script duplicates none of its
  persistence/audit logic (WI-6 §13). Because that function internally
  re-matches against every currently-ACTIVE rule (it has no `rule_id`
  parameter of its own), this script verifies AFTER every call that
  `result.matched_rule_id == the_requested_rule_id` — see
  `_apply_one_candidate` below. Any mismatch, or any outcome other than
  `CLASSIFIED`/`EXISTING`, STOPS the run (WI-6 §14) rather than
  silently skipping past it.

Candidate definition (WI-6 §6)
------------------------------------------------------------------------
An `EvidenceItem` for which: (1) the specified ACTIVE rule
authoritatively MATCHES (via `match_evidence_to_rule`, scoped to just
this rule); (2) no current `DOCUMENT_TYPE` classification exists yet.
Evidence already classified by ANY producer (this rule or another) is
excluded — this tool never supersedes.

Manifest doctrine (WI-6 §7-9)
------------------------------------------------------------------------
`--dry-run` computes the candidate set, orders it deterministically
(`received_at` ASC, `evidence_id` ASC — WI-6 §11), builds a canonical
JSON manifest (one entry per candidate: `evidence_id`, `content_hash`,
`sender_address`, `subject`, `rule_id`, `document_type`, `received_at`),
and reports its SHA-256. `--apply` REQUIRES
`--expected-manifest-sha256` and recomputes the full candidate manifest
from scratch, using the IDENTICAL scan/order logic, before doing
anything else; if the recomputed hash does not exactly match, the run
STOPS with a non-zero exit and mutates nothing.

Checkpoint doctrine (WI-6 §12) — a hint, never skip authority
------------------------------------------------------------------------
Exactly `scripts/migrate_object_store.py`'s own doctrine, applied here:
a checkpoint record for an `evidence_id` is evidence that this tool
PREVIOUSLY believed that item was done — never proof that it still is.
This tool never uses the checkpoint to decide whether to (re)process a
candidate: every candidate in the bounded `--limit` window is ALWAYS
re-submitted to the real, governed `classify_evidence_deterministically`
on every run, which is itself safe to call twice (a genuine second call
against an already-classified-by-this-rule item returns `EXISTING`,
writes nothing new). The checkpoint file exists purely as a durable,
fsync'd, append-only progress record for operational visibility/resume
narrative (`was_checkpointed` on each record) — it is read back and
consulted for reporting only, never as a condition that skips real
work. This is precisely what makes "checkpoint claims done, but the
real database disagrees" self-correcting rather than a silent bug.

No new database table (WI-6 §18)
------------------------------------------------------------------------
Business truth lives entirely in the existing `EvidenceClassification`/
audit tables via the existing governed service. This tool's own
checkpoint/report files are plain JSON/JSONL files under
`--runtime-dir` — no new persistence table of any kind.

Usage
-----
    # Planning (safe, no mutation):
    python3 scripts/reprocess_evidence_classification.py \\
        --runtime-dir /opt/bagman/runtime/classification-runs \\
        --rule-id <rule_id> --dry-run

    # Mutation (bounded, gated on the dry-run's own manifest hash):
    python3 scripts/reprocess_evidence_classification.py \\
        --runtime-dir /opt/bagman/runtime/classification-runs \\
        --rule-id <rule_id> --apply --limit 14 \\
        --expected-manifest-sha256 <hash from the dry-run above> \\
        --actor-type SYSTEM --actor-id bagman-pl-historical-reprocess

`--runtime-dir` may also be supplied via `BAGMAN_CLASSIFICATION_RUNTIME_DIR`.
Both are REQUIRED (one or the other) — this tool never silently picks a
default runtime path; `/opt/bagman/runtime/classification-runs` above
is a PRODUCTION-ONLY example, never hardcoded anywhere in this file.

This process expects to run with `BAGMAN_RUNTIME_ENV`/DB/object-store
env vars already set (typically `docker exec bagman-api python3
scripts/reprocess_evidence_classification.py ...`) — exactly like every
`app/api/routers/*.py` handler, via `app.api.composition.get_composition()`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import identity  # noqa: E402
from core.errors import NotFoundError  # noqa: E402
from core.timestamps import to_contract_string, utc_now  # noqa: E402
from services.evidence.classification import (  # noqa: E402
    CLASSIFICATION_TYPE_DOCUMENT_TYPE,
    SOURCE_DETERMINISTIC_RULE,
)
from services.evidence.classification_matcher import (  # noqa: E402
    OUTCOME_MATCH,
    match_evidence_to_rule,
)
from services.evidence.classification_rule import (  # noqa: E402
    RULE_STATUS_ACTIVE,
    EvidenceClassificationRule,
)
from services.evidence.classification_service import (  # noqa: E402
    OUTCOME_CLASSIFIED,
    OUTCOME_EXISTING,
    classify_evidence_deterministically,
)

#: A "safe upper bound" for `--limit` (WI-6 §10) — this tool must never
#: silently process an unbounded corpus. The real production run is
#: expected to process exactly 14 items; this ceiling is generous
#: headroom for future rule-scoped runs while still being a genuine,
#: deliberately-chosen bound, never "unlimited".
_MAX_LIMIT = 1000

#: Internal pagination page size for scanning `EvidenceRepository
#: .list_evidence` — bounded per call, looped until the source is
#: exhausted (see `_scan_candidates`).
_DEFAULT_SCAN_PAGE_SIZE = 200

#: Bound on how much of a candidate's `subject` appears in the
#: human-readable `candidate_summary` report field (WI-6 §7's own
#: "bounded summaries" instruction) — the full, untruncated subject is
#: still present verbatim in `candidate_manifest` (needed for exact
#: hash reproducibility), this only bounds the separate display field.
_SUMMARY_SUBJECT_MAX_LEN = 160


# ---------------------------------------------------------------------
# Candidate scanning — shared by --dry-run and --apply so both use the
# IDENTICAL ordering/selection logic (WI-6 §11).
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateEntry:
    evidence_id: str
    received_at: str
    content_hash: dict
    sender_address: str
    subject: str
    rule_id: str
    document_type: str

    def to_manifest_entry(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "received_at": self.received_at,
            "content_hash": dict(self.content_hash),
            "sender_address": self.sender_address,
            "subject": self.subject,
            "rule_id": self.rule_id,
            "document_type": self.document_type,
        }


@dataclass(frozen=True)
class ScanResult:
    rule: EvidenceClassificationRule
    total_evidence_scanned: int
    matching_evidence_count: int
    already_classified_count: int
    candidates: tuple[CandidateEntry, ...]
    current_classification_distribution: dict


def _list_all_evidence(evidence_repository, *, page_size: int) -> list:
    """Bounded-per-call pagination over `EvidenceRepository.list_evidence`
    until the source is exhausted. `list_evidence`'s own default
    ordering is `received_at` DESC (see that repository's own
    docstring) — this function does not rely on that ordering at all;
    the caller re-sorts the full candidate set itself (`received_at`
    ASC, `evidence_id` ASC — WI-6 §11) once every page has been
    collected, which is simpler and strictly more correct than fighting
    the repository's own default order across pages."""
    items: list = []
    offset = 0
    while True:
        page = evidence_repository.list_evidence(limit=page_size, offset=offset)
        if not page:
            break
        items.extend(page)
        offset += page_size
        if len(page) < page_size:
            break
    return items


def _scan_candidates(
    *,
    rule: EvidenceClassificationRule,
    evidence_repository,
    classification_repository,
    scan_page_size: int,
    include_own_rule_classified: bool = False,
) -> ScanResult:
    """WI-6 §6/§11 — the single implementation of "what is a
    candidate", used identically by `--dry-run` and `--apply`.

    `include_own_rule_classified` (default `False`, the WI-6 §6 letter
    of the law: "no current DOCUMENT_TYPE classification exists"): when
    `True`, an evidence item already classified by THIS EXACT rule
    (`source=DETERMINISTIC_RULE`, `rule_id == rule.rule_id`) is ALSO
    included as a candidate, alongside genuinely-unclassified items.

    This is used ONLY by `--apply`'s own pre-mutation manifest
    recomputation (`run_apply`, never by `--dry-run`'s own reporting).
    Without it, the manifest-hash gate (WI-6 §9) would make even a
    perfectly ordinary safe replay/resume impossible: once this run's
    own FIRST successful apply classifies an item, a bare re-scan would
    exclude it (it is no longer unclassified), shrinking the recomputed
    candidate set and manifest hash on every subsequent call — even
    when nothing outside THIS run's own prior progress changed. Every
    manifest entry for a self-rule-classified item (`evidence_id`,
    `content_hash`, `sender_address`, `subject`, `rule_id`,
    `document_type`) is bitwise IDENTICAL before and after this run
    classifies it (none of those fields are derived from the
    classification itself), so including it keeps the manifest hash
    stable across an idempotent replay/resume — while an item that some
    OTHER producer (a different source, or the same source under a
    DIFFERENT rule_id) claimed in the meantime is still correctly
    EXCLUDED here, changing the recomputed hash and correctly tripping
    the WI-6 §9 STOP gate: the real state changed, and this tool
    refuses to mutate on stale authority."""
    all_evidence = _list_all_evidence(evidence_repository, page_size=scan_page_size)
    active_rules = [rule]

    matching_evidence_count = 0
    already_classified_count = 0
    distribution: dict[str, int] = defaultdict(int)
    unordered_candidates: list = []

    for evidence in all_evidence:
        sender_address = evidence.metadata.get("sender_address")
        subject = evidence.metadata.get("subject")
        match = match_evidence_to_rule(sender_address=sender_address, subject=subject, active_rules=active_rules)
        if match.outcome != OUTCOME_MATCH:
            continue

        matching_evidence_count += 1
        current = classification_repository.get_current_classification(
            evidence.evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE
        )
        if current is None:
            distribution["NONE"] += 1
            unordered_candidates.append(evidence)
            continue

        distribution[f"{current.source}:{current.document_type}"] += 1
        already_classified_count += 1
        is_own_rule = current.source == SOURCE_DETERMINISTIC_RULE and current.rule_id == rule.rule_id
        if include_own_rule_classified and is_own_rule:
            unordered_candidates.append(evidence)

    unordered_candidates.sort(key=lambda e: (e.received_at, e.evidence_id))

    candidates = tuple(
        CandidateEntry(
            evidence_id=e.evidence_id,
            received_at=to_contract_string(e.received_at),
            content_hash=dict(e.content_hash),
            sender_address=e.metadata.get("sender_address"),
            subject=e.metadata.get("subject"),
            rule_id=rule.rule_id,
            document_type=rule.document_type,
        )
        for e in unordered_candidates
    )

    return ScanResult(
        rule=rule,
        total_evidence_scanned=len(all_evidence),
        matching_evidence_count=matching_evidence_count,
        already_classified_count=already_classified_count,
        candidates=candidates,
        current_classification_distribution=dict(distribution),
    )


def build_candidate_manifest(candidates: Sequence[CandidateEntry]) -> list[dict]:
    return [c.to_manifest_entry() for c in candidates]


def compute_manifest_sha256(manifest: Sequence[dict]) -> str:
    """Canonical JSON serialisation (WI-6 §8) — `sort_keys=True` makes
    each entry's own key order canonical; list order is preserved
    as-is (candidate order is already deterministic — WI-6 §11), so the
    hash is sensitive to any change in the candidate SET or ORDER."""
    canonical = json.dumps(list(manifest), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _truncate(text: Optional[str], max_len: int) -> Optional[str]:
    if text is None:
        return None
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def build_dry_run_report(scan: ScanResult, manifest: list[dict], manifest_sha256: str, *, run_id: str) -> dict:
    rule = scan.rule
    return {
        "mode": "DRY_RUN",
        "run_id": run_id,
        "rule_id": rule.rule_id,
        "rule_document_type": rule.document_type,
        "rule_sender_scope_type": rule.sender_scope_type,
        "rule_sender_scope_value": rule.sender_scope_value,
        "rule_subject_predicate_type": rule.subject_predicate_type,
        "rule_subject_predicate_value": rule.subject_predicate_value,
        "total_evidence_scanned": scan.total_evidence_scanned,
        "matching_evidence_count": scan.matching_evidence_count,
        "already_classified_count": scan.already_classified_count,
        "unclassified_candidate_count": len(scan.candidates),
        "candidate_evidence_ids": [c.evidence_id for c in scan.candidates],
        "candidate_summary": [
            {
                "evidence_id": c.evidence_id,
                "sender_address": c.sender_address,
                "subject": _truncate(c.subject, _SUMMARY_SUBJECT_MAX_LEN),
            }
            for c in scan.candidates
        ],
        "current_classification_distribution": scan.current_classification_distribution,
        "candidate_manifest": manifest,
        "candidate_manifest_sha256": manifest_sha256,
        "generated_at": to_contract_string(utc_now()),
    }


# ---------------------------------------------------------------------
# Checkpoint (append-only JSON-lines) — a revalidation HINT, never a
# skip authority. Mirrors `scripts/migrate_object_store.py`'s own
# `load_checkpoint`/`append_checkpoint` exactly (see module docstring).
# ---------------------------------------------------------------------


def load_checkpoint(path: Path) -> dict[str, dict]:
    done: dict[str, dict] = {}
    if not path.is_file():
        return done
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # A partially-written final line from a crash mid-flush
                # — the item it describes was therefore never
                # confirmed done and will simply be reprocessed for
                # real (never trusted from this record alone anyway).
                continue
            key = record.get("evidence_id")
            if key:
                done[key] = record
    return done


def append_checkpoint(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


# ---------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------


@dataclass
class ApplyOutcome:
    exit_code: int
    report: dict


def run_apply(
    *,
    rule_id: str,
    expected_manifest_sha256: str,
    limit: int,
    checkpoint_path: Path,
    evidence_repository,
    rule_repository,
    classification_repository,
    audit_repository,
    actor_type: str,
    actor_id: str,
    scan_page_size: int = _DEFAULT_SCAN_PAGE_SIZE,
    run_id: Optional[str] = None,
) -> ApplyOutcome:
    """WI-6 §9/§10/§12-17 — dependency-injected core of `--apply`
    (never touches `get_composition()` itself, so this is directly
    unit-testable against in-memory repositories).

    Raises:
        core.errors.NotFoundError: no such `rule_id`.
    """
    run_id = run_id or identity.generate_id()
    started_at = utc_now()

    rule = rule_repository.get_rule(rule_id)  # NotFoundError propagates
    if rule.status != RULE_STATUS_ACTIVE:
        report = {
            "mode": "APPLY",
            "run_id": run_id,
            "rule_id": rule_id,
            "error": "RULE_NOT_ACTIVE",
            "message": f"rule '{rule_id}' has status={rule.status!r} — only an ACTIVE rule may be reprocessed",
            "started_at": to_contract_string(started_at),
            "completed_at": to_contract_string(utc_now()),
        }
        return ApplyOutcome(exit_code=1, report=report)

    scan = _scan_candidates(
        rule=rule, evidence_repository=evidence_repository, classification_repository=classification_repository,
        scan_page_size=scan_page_size, include_own_rule_classified=True,
    )
    manifest = build_candidate_manifest(scan.candidates)
    actual_manifest_sha256 = compute_manifest_sha256(manifest)
    pre_classification_count = scan.already_classified_count

    if actual_manifest_sha256 != expected_manifest_sha256:
        report = {
            "mode": "APPLY",
            "run_id": run_id,
            "rule_id": rule_id,
            "error": "MANIFEST_HASH_MISMATCH",
            "expected_manifest_sha256": expected_manifest_sha256,
            "actual_manifest_sha256": actual_manifest_sha256,
            "message": (
                "the candidate set no longer matches the dry-run manifest this apply was gated on — "
                "refusing to mutate anything; no classification/audit rows were written"
            ),
            "started_at": to_contract_string(started_at),
            "completed_at": to_contract_string(utc_now()),
        }
        return ApplyOutcome(exit_code=1, report=report)

    candidates_to_process = scan.candidates[:limit]
    candidates_beyond_limit = scan.candidates[limit:]

    checkpoint_done = load_checkpoint(checkpoint_path)

    per_outcome_counts: dict[str, int] = defaultdict(int)
    classified_count = 0
    existing_count = 0
    processed_records: list[dict] = []
    stop_reason: Optional[str] = None
    stopped_at_evidence_id: Optional[str] = None

    for candidate in candidates_to_process:
        was_checkpointed = candidate.evidence_id in checkpoint_done

        # WI-6 §12/§13 — the checkpoint above is consulted ONLY for the
        # `was_checkpointed` report field; it is NEVER used to decide
        # whether to call the real, governed classifier below. Every
        # candidate in this bounded window is always re-submitted for
        # real — see module docstring's "Checkpoint doctrine".
        result = classify_evidence_deterministically(
            evidence_id=candidate.evidence_id,
            evidence_repository=evidence_repository,
            rule_repository=rule_repository,
            classification_repository=classification_repository,
            audit_repository=audit_repository,
            actor_type=actor_type,
            actor_id=actor_id,
        )

        if result.outcome in (OUTCOME_CLASSIFIED, OUTCOME_EXISTING) and result.matched_rule_id == rule_id:
            per_outcome_counts[result.outcome] += 1
            if result.outcome == OUTCOME_CLASSIFIED:
                classified_count += 1
            else:
                existing_count += 1
            record = {
                "evidence_id": candidate.evidence_id,
                "status": "done",
                "outcome": result.outcome,
                "classification_id": result.classification.classification_id if result.classification else None,
                "was_checkpointed": was_checkpointed,
            }
            append_checkpoint(checkpoint_path, record)
            processed_records.append(record)
            continue

        # WI-6 §14 — any other outcome (including a same-rule-mismatch
        # on CLASSIFIED/EXISTING, which should be structurally
        # impossible today but is checked defensively — see module
        # docstring) contradicts what the manifest already proved
        # moments ago (this evidence MATCHES this rule and has no
        # current classification): STOP. No partial/silent skip.
        stop_reason = (
            f"evidence '{candidate.evidence_id}' returned outcome={result.outcome!r} "
            f"matched_rule_id={result.matched_rule_id!r} during apply, but the manifest expected it to "
            f"cleanly resolve against rule_id={rule_id!r} — refusing to continue"
        )
        stopped_at_evidence_id = candidate.evidence_id
        break

    completed_at = utc_now()

    if stop_reason is not None:
        report = {
            "mode": "APPLY",
            "run_id": run_id,
            "rule_id": rule_id,
            "manifest_sha256": actual_manifest_sha256,
            "error": "UNEXPECTED_STATE_DURING_APPLY",
            "message": stop_reason,
            "stopped_at_evidence_id": stopped_at_evidence_id,
            "planned_count": len(candidates_to_process),
            "processed_before_stop_count": len(processed_records),
            "classified_count": classified_count,
            "existing_count": existing_count,
            "failure_count": 1,
            "pre_classification_count": pre_classification_count,
            "started_at": to_contract_string(started_at),
            "completed_at": to_contract_string(completed_at),
        }
        return ApplyOutcome(exit_code=1, report=report)

    # WI-6 §15 — final revalidation: every processed candidate must now
    # show current classification truth = this exact rule.
    revalidation_failures = []
    for candidate in candidates_to_process:
        current = classification_repository.get_current_classification(candidate.evidence_id, CLASSIFICATION_TYPE_DOCUMENT_TYPE)
        ok = (
            current is not None
            and current.source == SOURCE_DETERMINISTIC_RULE
            and current.rule_id == rule_id
            and current.document_type == rule.document_type
        )
        if not ok:
            revalidation_failures.append(candidate.evidence_id)

    post_scan = _scan_candidates(
        rule=rule, evidence_repository=evidence_repository, classification_repository=classification_repository,
        scan_page_size=scan_page_size,
    )

    report = {
        "mode": "APPLY",
        "run_id": run_id,
        "rule_id": rule_id,
        "manifest_sha256": actual_manifest_sha256,
        "planned_count": len(candidates_to_process),
        "candidates_beyond_limit_count": len(candidates_beyond_limit),
        "classified_count": classified_count,
        "existing_count": existing_count,
        "failure_count": len(revalidation_failures),
        "revalidation_failures": revalidation_failures,
        "per_outcome_counts": dict(per_outcome_counts),
        "pre_classification_count": pre_classification_count,
        "post_classification_count": post_scan.already_classified_count,
        "started_at": to_contract_string(started_at),
        "completed_at": to_contract_string(completed_at),
    }
    exit_code = 0 if not revalidation_failures else 1
    return ApplyOutcome(exit_code=exit_code, report=report)


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------


def _parse_args(argv: Optional[Iterable[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)

    parser.add_argument(
        "--runtime-dir",
        default=None,
        help=(
            "directory to hold checkpoint/report files (required here, or via "
            "BAGMAN_CLASSIFICATION_RUNTIME_DIR — never hardcoded by this tool)"
        ),
    )
    parser.add_argument("--rule-id", required=True, help="the single EvidenceClassificationRule.rule_id this run is scoped to")

    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("--dry-run", action="store_true", help="mutation-free planning mode")
    mode_group.add_argument("--apply", action="store_true", help="mutation mode — requires --expected-manifest-sha256 and --limit")

    parser.add_argument("--expected-manifest-sha256", default=None, help="required with --apply: the SHA-256 from a prior --dry-run")
    parser.add_argument("--limit", type=int, default=None, help=f"required with --apply: bounded candidate count to process (1-{_MAX_LIMIT})")
    parser.add_argument("--checkpoint-file", default=None, help="override the default <runtime-dir>/reprocess-<rule_id>-checkpoint.jsonl path")
    parser.add_argument("--actor-type", default="SYSTEM")
    parser.add_argument("--actor-id", default="reprocess-evidence-classification")
    parser.add_argument("--scan-page-size", type=int, default=_DEFAULT_SCAN_PAGE_SIZE)

    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.apply:
        if not args.expected_manifest_sha256:
            parser.error("--apply requires --expected-manifest-sha256 <hash from a prior --dry-run>")
        if args.limit is None:
            parser.error(f"--apply requires --limit <1-{_MAX_LIMIT}>")
        if not (1 <= args.limit <= _MAX_LIMIT):
            parser.error(f"--limit must be between 1 and {_MAX_LIMIT} (got {args.limit})")

    runtime_dir = args.runtime_dir or os.environ.get("BAGMAN_CLASSIFICATION_RUNTIME_DIR")
    if not runtime_dir:
        parser.error("--runtime-dir is required (or set BAGMAN_CLASSIFICATION_RUNTIME_DIR) — no default path is ever assumed")
    args.runtime_dir = runtime_dir

    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    runtime_dir = Path(args.runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)

    from app.api.composition import get_composition  # local import: keeps this module importable/testable with zero env vars set

    composition = get_composition()
    evidence_repository = composition.api.evidence_repository
    rule_repository = composition.classification_rule_repository
    classification_repository = composition.classification_repository
    audit_repository = composition.api.audit_repository

    run_id = identity.generate_id()

    try:
        if args.dry_run:
            rule = rule_repository.get_rule(args.rule_id)
            if rule.status != RULE_STATUS_ACTIVE:
                print(f"error: rule '{args.rule_id}' is not ACTIVE (status={rule.status!r})", file=sys.stderr)
                return 1
            scan = _scan_candidates(
                rule=rule, evidence_repository=evidence_repository, classification_repository=classification_repository,
                scan_page_size=args.scan_page_size,
            )
            manifest = build_candidate_manifest(scan.candidates)
            manifest_sha256 = compute_manifest_sha256(manifest)
            report = build_dry_run_report(scan, manifest, manifest_sha256, run_id=run_id)
            exit_code = 0
        else:
            checkpoint_path = (
                Path(args.checkpoint_file) if args.checkpoint_file
                else runtime_dir / f"reprocess-{args.rule_id}-checkpoint.jsonl"
            )
            outcome = run_apply(
                rule_id=args.rule_id,
                expected_manifest_sha256=args.expected_manifest_sha256,
                limit=args.limit,
                checkpoint_path=checkpoint_path,
                evidence_repository=evidence_repository,
                rule_repository=rule_repository,
                classification_repository=classification_repository,
                audit_repository=audit_repository,
                actor_type=args.actor_type,
                actor_id=args.actor_id,
                scan_page_size=args.scan_page_size,
                run_id=run_id,
            )
            report = outcome.report
            exit_code = outcome.exit_code
    except NotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    mode_tag = "dry-run" if args.dry_run else "apply"
    report_path = runtime_dir / f"reprocess-{args.rule_id}-{mode_tag}-{run_id}-report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    print(f"[reprocess_evidence_classification] report written to {report_path}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
