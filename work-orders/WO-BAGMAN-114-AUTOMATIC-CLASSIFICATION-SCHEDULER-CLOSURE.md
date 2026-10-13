# WO-BAGMAN-114-AUTOMATIC-CLASSIFICATION-SCHEDULER — ACTIVATION CLOSURE

## 1. WO identifier

`WO-BAGMAN-114-AUTOMATIC-CLASSIFICATION-SCHEDULER` (`work-orders/WO-BAGMAN-114-AUTOMATIC-CLASSIFICATION-SCHEDULER.md`). Parent PID: `PID.md` §114 (`CLOSED GREEN`). This document closes the production-activation half of that WO (Stages D–G), per its own §11.2.

## 2. Canonical repository-implementation merge SHA

`3a40ab67eb0a69bc6aa15eca742e410f9a58c0c5` — the merge of PR #32 (Stage A/B repository implementation: `deployment/launchd/com.bagman.evidence-classification-scheduler.plist` + `deployment/launchd/README.md`), independently re-confirmed as the real `origin/main` tip immediately before Stage D and again before this closure.

## 3. Production authority proof (Stage E.1)

Target confirmed, independently and repeatedly across Stage D, Stage E (both passes), and Stage F/G: host `mac-prod-01`, `192.168.11.4`, per PID §0. Trinity was never touched at any point in this delivery.

## 4. Stage D/E/F/G commands/checks performed

- **Stage D**: pre-install discovery found `/opt/bagman/runtime/launchd/` absent — correctly BLOCKED, no production mutation, no directory created without separate authorization.
- **Stage D resumption** (Architect-authorized narrow provisioning): `mkdir -m 750 /opt/bagman/runtime/launchd` (matching the host's own `classification-runs/` sibling convention); canonical plist copied to `~/Library/LaunchAgents/com.bagman.evidence-classification-scheduler.plist`; hash-verified; `plutil -lint` → `OK`; scheduler left unloaded.
- **Stage E (first pass)**: RED — `BAGMAN_EVIDENCE_CLASSIFICATION_ACTIVATION_BOUNDARY` found genuinely absent from the live `bagman-api` container, confirmed via `docker inspect`, `docker exec env`, and the live compose file; every other mandatory condition (historical count, queue safeguards, Xero isolation, health) passed independently.
- **T_ACT remediation** (Architect-authorized, narrowly scoped): backup `bagman.docker-compose.yml.bak-pre-tact-activation-20261012T110438Z` created and verified byte-identical to the original (SHA-256 `f5005cbe3228706cb991ba232d68ac77681414b107a347d26e25aaeefbb7d7f1`); exactly one line added to `bagman-api`'s `environment:` block; scoped `docker compose up -d --no-deps --no-build --pull never bagman-api` recreated only that one container, reusing the same image (`sha256:0766e8f4...`); all six other containers retained their original container IDs/uptimes.
- **Stage E (fresh re-verification)**: GREEN — every gate independently re-derived from the corrected live state.
- **Stage F**: `launchctl load ~/Library/LaunchAgents/com.bagman.evidence-classification-scheduler.plist` run at `2026-10-13T11:32:30Z`.
- **Stage G**: bounded observation from `2026-10-13T11:32:30Z` to `2026-10-13T11:45:16Z` (~12m46s, covering two real scheduled fires).

## 5. `T_ACT` verification (Stage E.2) and re-verification at closure

Canonical value: `2026-10-02T09:58:51Z`. Confirmed present, exact, in the live `bagman-api` container at: the original Stage E RED pass (absent — the defect); the remediation's own post-change verification; the fresh post-remediation Stage E pass (present, exact); and independently re-confirmed again by the Delivery Controller directly via SSH immediately after Stage F/G completed (`docker exec bagman-api env | grep ACTIVATION_BOUNDARY` → `BAGMAN_EVIDENCE_CLASSIFICATION_ACTIVATION_BOUNDARY=2026-10-02T09:58:51Z`).

## 6. Zero-pre-`T_ACT`-job proof — pre-activation result and final re-run at closure

```sql
SELECT count(*) FROM evidence_classification_jobs j
JOIN evidence_items e ON e.evidence_id = j.evidence_id
WHERE e.created_at < '2026-10-02T09:58:51Z'::timestamptz;
```
- Pre-activation (Stage E fresh pass): `0`.
- Immediately pre-Stage-F (Step 1 reconfirmation): `0`.
- Post-Stage-G, at closure (independently re-run by the Delivery Controller directly via SSH): `0`.

## 7. Current health (Stage E.4)

All 7 production containers (`bagman-api`, `bagman-db`, `bagman-objects`, `bagman-scan`, `bagman-ai-gateway`, `bagman-ai-db`, `bagman-xero-oauth-tls`) confirmed healthy at every checkpoint across Stage D, Stage E, Stage F load, mid-Stage-G observation, and immediately after Stage G concluded — independently re-confirmed by the Delivery Controller via direct SSH after the production executor's own report.

## 8. Xero isolation — baseline (Stage E.5) and post-observation comparison (Stage G.10)

Baseline (Stage E): `xero_account_suggestions = 0`, `xero_account_assignments = 0`. Post-observation (after both real scheduler fires processed 3 real evidence items to `NON_ACCOUNTING_DOCUMENT`/`AI_PROPOSAL_REVIEW_REQUIRED`): `xero_account_suggestions = 0`, `xero_account_assignments = 0` — **no change**, independently re-confirmed by the Delivery Controller via direct SQL. Consistent with classification-only outcomes that do not reach Xero-mutating code.

## 9. Bounded-observation evidence (Stage G, all 11 items)

1. **At least one real scheduled invocation occurred** — two did, evidenced by real report files on disk (not assumed): `evidence-classification-jobs-process-01a1350d-acd7-7907-8a7b-22f4d808650c-report.json` (11:38) and `evidence-classification-jobs-process-01a13512-c655-7db1-a302-16b330ad539d-report.json` (11:43) — both independently confirmed present via direct `docker exec ls` by the Delivery Controller.
2. **The 3 existing eligible post-T_ACT EvidenceItems were handled** — all 3 claimed and processed in Invocation 1, each `SUCCEEDED`, outcome `AI_PROPOSAL_REVIEW_REQUIRED` (deterministic `NO_MATCH`, AI proposal `NON_ACCOUNTING_DOCUMENT`), each producing an `OPEN` `CLASSIFICATION_REVIEW` Needs-You item — independently re-confirmed by direct SQL (job table dump, Needs-You query).
3. **No pre-T_ACT EvidenceItem was automatically classified** — confirmed (§6 above).
4. **No duplicate classification jobs appeared** — `GROUP BY evidence_id HAVING count(*) > 1` returns zero rows, independently re-run.
5. **No overlapping/unsafe execution occurred** — zero `CLAIMED`/`IN_PROGRESS` rows at any checkpoint, independently re-confirmed; distinct `run_id`s and non-overlapping claim timestamps for both invocations.
6. **Job statuses/worker outputs recorded** — full JSON reports captured for both invocations (Invocation 1: `claimed_count=3, created_count=3, per_outcome_counts={"SUCCEEDED":3}`; Invocation 2: `claimed_count=0, created_count=0`, correct no-op).
7. **Needs-You reviewability confirmed** — 3 new `CLASSIFICATION_REVIEW` items, all `OPEN`, independently re-queried directly by the Delivery Controller.
8. **No Xero side effect** — confirmed (§8 above).
9. **Production health acceptable throughout** — confirmed (§7 above).
10. **Scheduler cadence correct** — fires at 11:37:31Z and 11:43:05Z, ≈334s apart, within normal `launchd` `StartInterval=300` scheduling jitter.
11. **No synthetic/manufactured work** — all 3 processed items were genuine, pre-existing `EvidenceItem`s from the original October mailbox sweep; nothing was created to produce this result.

## 10. Scheduler health/state at closure

`launchctl list` confirms `com.bagman.evidence-classification-scheduler` present, last exit status `0`, continuing on its 300-second cadence — independently re-confirmed by the Delivery Controller via direct SSH after the production executor's final report. The scheduler remains loaded and enabled, as intended for ongoing normal operation under this WO's accepted architecture.

## 11. Gmail finding noted but untouched

PID §112.E's `TOKEN_REFRESH_FAILED` finding remains untouched and out of scope for this delivery — not investigated or remediated at any point in Stages D–G.

## 12. Deviations

1. **Stage D first attempt correctly BLOCKED** on a missing log directory — resolved via a separate, narrow Architect-authorized provisioning step rather than silently created.
2. **Stage E first pass correctly RED** — `T_ACT` found genuinely absent in production — resolved via a separate, narrow, independently-verified Architect-authorized configuration remediation (one environment-variable addition, one scoped container recreation), not a scheduler/code change.
3. **Minor, non-blocking**: `--runtime-dir` on the host did not exist at Stage F start, because `bagman-api`'s Stage-E-remediation recreation ~1 day prior wiped the prior ephemeral (non-bind-mounted) directory from the original October canary. The worker's own `runtime_dir.mkdir(parents=True, exist_ok=True)` self-healed this on first fire, confirmed. **Flagged for future awareness**: classification-worker JSON reports are not durably persisted across `bagman-api` container recreation (only `classification-runs/` is bind-mounted, not `evidence-classification-jobs/`) — not a defect in this delivery, but worth noting for any future operational runbook.
4. All three deviations above are preserved as honest historical record, not retroactively smoothed over — each is reflected in this WO's own prior durable PR/review history (PRs #32 and the separate governance corrections already merged).

## 13. Evidence references

- PR #32 (repository implementation, merge `3a40ab67eb0a69bc6aa15eca742e410f9a58c0c5`).
- Stage D backup: `/opt/bagman/app/compose/bagman.docker-compose.yml.bak-pre-tact-activation-20261012T110438Z` (production-local, not Git-tracked, per the project's three-layer configuration doctrine).
- Job IDs: canary `01a0fc10-69f4-78f0-b4e1-f13661e8583d`; scheduler-run jobs `01a1350d-ace7-7323-bbd4-40947263b3d5`, `01a1350d-aceb-72b7-9bd9-a97b6729ad14`, `01a1350d-acec-709f-9263-fa46cb3beaed`.
- Run IDs: `01a1350d-acd7-7907-8a7b-22f4d808650c` (Invocation 1), `01a13512-c655-7db1-a302-16b330ad539d` (Invocation 2).
- Needs-You items: `01a1350d-e194-...`, `01a1350e-1132-...`, `01a1350e-2f5b-...`, all `CLASSIFICATION_REVIEW`/`OPEN`.
- Installed plist SHA-256: `c17bac7ff07eb69c3b915bc9f2bf647ccb7164c8a78d6903fb7be359d28b7b09`.
- Load timestamp: `2026-10-13T11:32:30Z`. Observation window end: `2026-10-13T11:45:16Z`.

No secrets of any kind appear in this document.

## 14. Implementer/production-executor verdict

**GREEN.** Stage D (with its intermediate correct BLOCK and subsequent narrow, authorized remediation), Stage E (with its intermediate correct RED and subsequent narrow, authorized remediation and fresh re-verification), Stage F (clean enablement), and Stage G (two genuine, bounded, safe scheduled executions with zero safety violations) are all independently verified, both by the dispatched production executor and separately, directly, by the Delivery Controller via fresh SSH/SQL at every decisive step. Recurring automatic evidence classification is now operating safely in production, within all governed bounds established by PID §112/§113/§114 and this Work Order.
