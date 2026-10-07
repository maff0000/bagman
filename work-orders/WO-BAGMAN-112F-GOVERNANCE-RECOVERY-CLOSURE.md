# WO-BAGMAN-112F-GOVERNANCE-RECOVERY — CLOSURE

## 1. Work Order identifier

`WO-BAGMAN-112F-GOVERNANCE-RECOVERY` (file: `work-orders/WO-BAGMAN-112F-GOVERNANCE-RECOVERY.md`). Parent PID: `PID.md` §112.F. This closure document is produced by the FORGE Implementer stage of the governed chain (`Architecture decision → PID/Amendment → Git-tracked Work Order → Delivery Controller → Implementer → Independent Audit → PR → Architect Acceptance → Merge → Closure`). It is **read-only production evidence collection**, not a new implementation, and does not itself constitute the Independent Audit required by WO §12 / PID §111.

## 2. Canonical execution SHA

Dispatch SHA: `1f1dadf51fcf81ac9d672c70a47565c8e456c194` (verified canonical `origin/main` tip at dispatch time; worktree confirmed clean and at this commit before any work began). Work performed in worktree `/srv/bagman-worktrees/wo-112f-governance-recovery-execution` on branch `wo/WO-BAGMAN-112F-governance-recovery-execution`.

Note on the WO's own execution-authorisation state: the canonical WO document's header/§14 record that execution authorisation was completed via Independent Audit review `5425424908` (GREEN), Architect Acceptance review `5427495956` (GREEN), merge `f459dee0d218fd95bdd26e66098547ba81dceea5` (PR #24, independently confirmed via `gh pr view 24` → `MERGED`, `mergeCommit.oid = f459dee0d218fd95bdd26e66098547ba81dceea5`), and post-merge Security CI `37455123778` (SUCCESS), per PID §113's lifecycle rule. This FORGE Implementer relied on that durable chain plus the explicit dispatch instruction rather than re-auditing the WO's own authorisation (that is the Delivery Controller's responsibility, already discharged before dispatch).

## 3. Production authority proof

Target host independently confirmed by direct inspection, not assumed:

```text
$ ssh -i /root/.ssh/macmini_ed25519 matt@192.168.11.4 hostname; uname -a
mac-prod-01
Darwin mac-prod-01 24.6.0 Darwin Kernel Version 24.6.0 ... RELEASE_ARM64_T8132 arm64
```

This matches PID §0's named target (`192.168.11.4`) and the dispatch prompt's stated production authority. `docker ps` on this host shows the full canonical BAGMAN stack running: `bagman-api`, `bagman-db` (postgres:17), `bagman-objects` (seaweedfs), `bagman-scan` (clamav), `bagman-ai-gateway`, `bagman-ai-db`, `bagman-xero-oauth-tls` — all `Up`/`healthy` except the Caddy TLS proxy (no healthcheck defined, confirmed reachable, see §18).

**Trinity**: per explicit dispatch-prompt instruction, Trinity was **not inspected** as part of this execution — it is out of scope entirely and must not be touched in any way. Instead, "no Trinity BAGMAN writer" is established from the existing durable Git record in canonical `PID.md`, independently re-read in this checkout:
- §100.7/100.8/100.9 (2026-09-17 Phase B cutover): Mac mini established as sole writable canonical runtime; Trinity's `bagman-api` container explicitly removed (`docker rm`) to prevent split-brain; Trinity's `bagman-db`/`bagman-objects` retained only as a non-writable archive (no host-published ports, no writer container on the shared network).
- §110 (2026-10-01 incident, found and contained independently of this WO): a rogue `bagman-api:dev` container was found running against Trinity's retired archive, root-caused (a heading-pattern search miss, §110.2) and removed (§110.3); real Mac-mini production was independently confirmed healthy, current, and completely unaffected throughout that incident.
This is durable, Git-tracked evidence of the "no Trinity writer" state as of 2026-10-01, consistent with — and not contradicted by — anything observed on the Mac mini during this execution. No claim beyond what this record supports is made; a fresh direct inspection of Trinity was correctly out of scope and was not performed.

## 4. Commands/checks performed

All commands were run read-only over SSH (`ssh -i /root/.ssh/macmini_ed25519 matt@192.168.11.4`) using `/opt/homebrew/bin/docker`, plus one `psql` session per check wrapped in `BEGIN TRANSACTION READ ONLY; ... ROLLBACK;`. No `git push`/`alembic upgrade`/DDL/DML was ever issued. Checks performed: `docker ps`/`docker inspect` (image, env, health); `alembic current`/`alembic history` via `docker exec bagman-api`; `information_schema`/`pg_constraint` schema inspection; the hard-gate pre-`T_ACT` job query; full `evidence_classification_jobs` table read; `evidence_items`, `evidence_classifications`, `ai_invocations`, `needs_you_items`, `xero_account_suggestions`, `xero_account_assignments`, `mailbox_sweep_runs`, `mailbox_sources`, `mailbox_messages` read-only queries; filesystem inspection of `/opt/bagman/backups/` (size + `shasum -a 256`) and `/opt/bagman/runtime/`; `crontab -l`, `/etc/crontab`, `/etc/cron.d`, `~/Library/LaunchAgents`, `/Library/LaunchDaemons`, `/Library/LaunchAgents`, `launchctl list`, and the live `helm-bagman-stack-start.sh` script content; `ps aux` scan for persistent worker/classification processes; HTTP health probes (`curl` to `:8200/health`, `:8200/`, `:4100/health`, `:8543/`); `docker exec bagman-scan clamdscan --version`; `docker exec bagman-db pg_isready`; a scoped `grep` of the deployed image's `/app/alembic/versions` for migration `143b86b2ab44`; `gh pr view 24` (GitHub API, read-only) to confirm PR #24's merge SHA; local `git cat-file`/`git log`/`git merge-base --is-ancestor` to confirm `493af0c3...` is a real, durable, ancestor commit of the dispatch head.

## 5. Production image/commit

```text
Container:  bagman-api  (image bagman-api:main-493af0c, Up 5 days, healthy)
Env:        BAGMAN_GIT_COMMIT=493af0c3923bf6f1bdd308521da8f3aca3fafac2
Image ID:   sha256:0766e8f46d9e975d18bdf8638ae81c4266f3dca1750d043fe4e2f793e5243c54
Created:    2026-10-02T10:54:59+01:00 (2026-10-02T09:54:59Z)
```

Exact match to the reported §112.C deployment. **Deployment provenance**: `493af0c3923bf6f1bdd308521da8f3aca3fafac2` is confirmed, independently, to be a real commit in this repository's durable history — `git cat-file -t` returns `commit`; `git log --oneline -1` shows it is the merge commit for PR #22 ("evidence/classification-simplification"); `git merge-base --is-ancestor 493af0c... HEAD` succeeds against the current dispatch head. Production has **not** moved since the canary (container age "Up 5 days" as of 2026-10-07, created timestamp ~4 minutes before `T_ACT`, consistent with deploy-then-canary same day, 2026-10-02) — no unexplained drift to investigate.

## 6. Alembic state

```text
$ docker exec bagman-api alembic current
ae936a444eae (head)

$ docker exec bagman-api alembic history   (relevant tail)
f1a2b3c4d5e6 -> a7f34c9e2d18 -> ae936a444eae (head)
```

Exact match to reported ancestry. `143b86b2ab44` does **not** appear anywhere in `alembic history`'s full output (22 revisions total, base → `ae936a444eae`), and a direct scoped `grep` of the deployed image's `/app/alembic/versions` directory for `143b86b2ab44` returns no match (exit code 1). `evidence_classification_reconciliation_cursors` confirmed absent from the live schema (`information_schema.tables` query, 0 rows). `evidence_classification_jobs` exists with a `UNIQUE (evidence_id)` constraint (`uq_evidence_classification_jobs_evidence_id`) plus `PRIMARY KEY (job_id)` and `FOREIGN KEY (evidence_id) REFERENCES evidence_items(evidence_id)` — the "unique `evidence_id` protection" reported is confirmed, durable, enforced at the database level (not merely application-level).

## 7. Backup verification

```text
File: /opt/bagman/backups/pre-classification-simplification-deploy-20261002T095317Z.dump
Size: 3420026 bytes               (reported: 3,420,026 — exact match)
SHA-256: 5bb3509c3ad0288aba28a608b5d54645489f1c46c8344e72d49bdc8a8a59310b   (exact match)
```

File is present, untouched, not restored. Confirmed read-only (`stat`/`shasum` only).

## 8. `T_ACT` verification

Canonical `PID.md` §112.B (re-read fresh from this checkout) states exactly `T_ACT = 2026-10-02T09:58:51Z`. No scheduler of any kind was found (see §17) that could carry an alternate `T_ACT` — the only recurring mechanism on the host is a `launchd` boot-time docker-compose bring-up (`com.helm.bagman-stack`, `RunAtLoad` only, no `StartInterval`/`StartCalendarInterval`), which does not invoke the classification worker at all, let alone with a different activation boundary. No alternate value found anywhere in application config, compose files, or runtime artifacts.

## 9. Zero-pre-`T_ACT`-job proof (hard acceptance gate)

Exact query executed (inside `BEGIN TRANSACTION READ ONLY; ... ROLLBACK;`):

```sql
SELECT count(*) AS pre_tact_job_count
FROM evidence_classification_jobs j
JOIN evidence_items e ON e.evidence_id = j.evidence_id
WHERE e.created_at < '2026-10-02T09:58:51Z'::timestamptz;
```

Exact result:

```text
 pre_tact_job_count 
--------------------
                  0
(1 row)
```

**Result: `0`, exactly as required.** Supporting context: `evidence_classification_jobs` contains exactly **1** row total (the canary job), with `created_at = 2026-10-02 10:02:09.268969+00`; the table's single row's referenced evidence item has `created_at = 2026-10-02 10:00:32.879593+00`, which is `>= T_ACT`. Of 690 total `evidence_items` in the database, 689 pre-date `T_ACT` and exactly 4 were created at/after `T_ACT` — none of the 689 pre-`T_ACT` items has any classification job.

## 10. Canary report verification

The exact file named in WO §6.6, `/opt/bagman/runtime/evidence-classification-canary`, **does not exist** on the production host (`ls` → "No such file or directory"). `/opt/bagman/runtime/` contains only `classification-runs/` (33 files, all dated 2026-09-25–2026-09-27, i.e. pre-canary evaluation/harness artifacts unrelated to the 2026-10-02 canary) and an empty `generated/` directory. This is recorded honestly as a gap (see §20, Deviations) rather than papered over — **the preserved runtime report WO §6.6 expected is absent**. However, every factual element the report was expected to attest is independently reconciled directly against live production database state (§9, §11, §12, §13, §14, §15, §16), so the absence of the report file itself does not leave any of §112.D's claims unverified — it only means verification rested on direct database/runtime evidence rather than on a cached report artifact. Reported worker bounds (discovery limit 1, processing limit 1, created 1, claimed 1, succeeded 1) are consistent with the single-row `evidence_classification_jobs` table found.

## 11. Exact job verification

Job `01a0fc10-69f4-78f0-b4e1-f13661e8583d`, read directly from `evidence_classification_jobs`:

```text
status: SUCCEEDED
attempt_count / max_attempts: 1 / 3
actor_type / actor_id: SYSTEM / bagman-evidence-classification-discovery
classification_outcome: AI_PROPOSAL_REVIEW_REQUIRED
last_error: (empty)
evidence_id: 01a0fc0e-f16f-7e1b-aafe-4f0e2b62e5d3
created_at: 2026-10-02 10:02:09.268969+00
```

Exact match to §112.D/WO §6.7. The associated `evidence_items` row (`01a0fc0e-f16f-7e1b-aafe-4f0e2b62e5d3`) has `created_at = 2026-10-02 10:00:32.879593+00`, independently confirmed `>= T_ACT` (`2026-10-02T09:58:51Z`).

## 12. Classification verification

`evidence_classifications` row for the same evidence item:

```text
classification_type: DOCUMENT_TYPE
document_type:        NON_ACCOUNTING_DOCUMENT
status:                REVIEW_REQUIRED
source:                AI_PROPOSAL
reason_codes:          ["AI_PROPOSAL_PENDING_REVIEW"]
ai_invocation_id:      01a0fc10-6a0e-7b09-b343-e441534db9d8
```

Deterministic `NO_MATCH` is not itself a persisted row in `evidence_classifications` (only the terminal `DOCUMENT_TYPE` classification is persisted) but is consistent with, and not contradicted by, the fact that the only persisted classification for this evidence item is sourced `AI_PROPOSAL` (i.e. the deterministic path did not produce a persisted match, matching "deterministic classification returned `NO_MATCH`"). All other fields match §112.D/WO §6.8 exactly. Data read only, not modified.

## 13. AI invocation verification

`ai_invocations` row `01a0fc10-6a0e-7b09-b343-e441534db9d8`:

```text
task_id: DOCUMENT_TYPE_PROPOSAL
task_version: 2
provider: LITELLM
capability_alias: bagman-core
status: SUCCEEDED
inference_backend: MAC_LOCAL
primary_input_reference: 01a0fc0e-f16f-7e1b-aafe-4f0e2b62e5d3   (= the canary evidence_id, direct correlation confirmed)
started_at / completed_at: 2026-10-02 10:02:09.294015+00 / 2026-10-02 10:02:27.176533+00
```

Exact match to §112.D/WO §6.9, with direct correlation to the canary evidence item confirmed via `primary_input_reference`.

## 14. Needs You verification/current state

`needs_you_items` row `01a0fc10-affd-75da-b9b9-5af3283a8491`:

```text
item_type: CLASSIFICATION_REVIEW
status: OPEN
created_at: 2026-10-02 10:02:27.19728+00
resolved_at: (null)
resolved_by_actor_type: (null)
source_object_reference: 01a0fc10-aff4-788d-be46-3115ae024d10  (the evidence_classifications row above)
```

Status is **still `OPEN`**, unchanged since creation (no `resolved_at`, no resolver) — matches the reported original state exactly, and confirms it has not been resolved or otherwise mutated since 2026-10-02. No status change was made by this execution.

## 15. Mailbox evidence reconstruction

Three `mailbox_sweep_runs` rows exist on 2026-10-02 in the relevant window:

```text
mailbox 01a0b16a... (MICROSOFT_GRAPH, ACTIVE/CONNECTED): SUCCEEDED, messages_seen=255, messages_new=255, evidence_created=4, duplicates=0, failures=0
mailbox 01a0c3e9... (GOOGLE_GMAIL, ACTIVE/AUTH_REQUIRED):  FAILED, error_code=TOKEN_REFRESH_FAILED
mailbox 01a0c49b... (GOOGLE_GMAIL, ACTIVE/AUTH_REQUIRED):  FAILED, error_code=TOKEN_REFRESH_FAILED
```

The one **successful** sweep is the ordinary production Microsoft Graph mailbox mechanism (`trigger=MANUAL`, i.e. an operator-triggered but otherwise standard production sweep — not a synthetic/test code path), and its `evidence_created=4` matches exactly the 4 `evidence_items` found with `created_at >= T_ACT`. `mailbox_messages` rows for all 4 of those evidence items directly trace back to this one sweep's `mailbox_id` (`01a0b16a...`, `provider_kind=MICROSOFT_GRAPH`), each with `ingestion_status=INGESTED`, `first_seen_at` timestamps of 2026-10-02 10:00:32–10:00:47 (i.e. during this sweep's `started_at`–`completed_at` window of 10:00:23.96–10:00:58.97), and real historical `received_at` dates (2026-09-21, 2026-09-23, 2026-09-25, 2026-10-01 — genuine prior emails being ingested for the first time, not fabricated). This is direct, non-circumstantial proof: four legitimate `EvidenceItem`s were created by one ordinary production Microsoft Graph sweep after `T_ACT`, not manufactured or backfilled for the canary. No new sweep was run to produce or check this evidence — all of it was reconstructed from already-persisted rows.

## 16. Xero isolation proof

Scoped specifically to the canary evidence item (`01a0fc0e-f16f-7e1b-aafe-4f0e2b62e5d3`):

```text
xero_account_suggestions WHERE evidence_id = <canary>: 0 rows
xero_account_assignments WHERE evidence_id = <canary>: 0 rows
needs_you_items WHERE source_object_reference = <canary evidence_id> AND item_type = 'XERO_ACCOUNT_REQUIRED': 0 rows
```

(For context only, not as a global-zero requirement per WO §6.12's explicit instruction: `xero_account_suggestions`/`xero_account_assignments` created since `T_ACT` globally are also both 0 — there has been no Xero activity at all since `T_ACT`, canary-attributable or otherwise, which trivially satisfies the causal-isolation requirement without needing to distinguish "caused by the canary" from "caused by something else.") Zero Xero ledger mutation attributable to the canary, confirmed.

## 17. Scheduler absence proof

```text
crontab -l (user matthewscott): "no crontab for matthewscott"
crontab -l -u root:             "no crontab for root"
/etc/crontab, /etc/cron.d:      absent (not present on this macOS host)
~/Library/LaunchAgents:         com.helm.bagman-stack.plist, com.helm.colima.plist (both boot/service bring-up only)
/Library/LaunchDaemons:         com.helm.pf-enable.plist, com.local.virtual-display.plist, com.ollama.serve.plist, com.rustdesk.RustDesk.plist — none BAGMAN-classification-related
/Library/LaunchAgents (system): empty
grep for StartInterval/StartCalendarInterval across all found plists: no matches
launchctl list | grep bagman:   only "com.helm.bagman-stack" (the boot bring-up agent itself, not a recurring timer)
```

`com.helm.bagman-stack.plist` content confirmed: `RunAtLoad=true` only, no interval/calendar keys — it runs `/usr/local/sbin/helm-bagman-stack-start.sh` once per boot, which itself only waits for the Colima Docker socket and then runs `docker compose up -d` for the two BAGMAN compose projects (idempotent bring-up, not a worker invocation). `ps aux` shows no persistent classification/worker process. The one compose-file mention of "classification" is a code comment documenting a volume mount for operator-invoked reprocessing/evaluation CLI scripts ("Never used by the running server itself, only by an operator invoking these scripts via docker exec") — not a scheduler.

**Scheduler state: `NOT INSTALLED`, confirmed.** No recurring runner anywhere carries an alternative `T_ACT`.

## 18. Current health

```text
bagman-api:          Up 5 days (healthy)   — GET /health → HTTP 200 {"status":"alive"}, GET / → HTTP 200
bagman-db:            Up 10 days (healthy) — pg_isready: "accepting connections"
bagman-objects:       Up 10 days (healthy)
bagman-scan:          Up 10 days (healthy) — clamdscan --version: ClamAV 1.5.4/28145/2026-10-06
bagman-ai-gateway:    Up 10 days (healthy) — container healthcheck green; an unauthenticated GET /health returns HTTP 401 "No api key passed in" (expected behaviour for an auth-gated gateway endpoint probed without a key — not treated as unhealthy; no key was supplied deliberately, to avoid invoking real AI capability)
bagman-ai-db:         Up 10 days (healthy)
bagman-xero-oauth-tls: Up 10 days (no Docker healthcheck defined for this container) — GET https://localhost:8543/ → HTTP 200, confirmed reachable
```

All components healthy. No unhealthy component found; no restart attempted or required.

## 19. Gmail finding noted but untouched

Confirmed directly from `mailbox_sweep_runs` (read-only): two Gmail (`GOOGLE_GMAIL`) mailbox sweep attempts on 2026-10-02, both `status=FAILED`, `error_code=TOKEN_REFRESH_FAILED`, both mailboxes' `mailbox_sources.connection_state=AUTH_REQUIRED`. This matches PID §112.E exactly. **No remediation was attempted**: no OAuth refresh, no reconnect, no credential change, no mailbox configuration change. This remains out of scope, for a separate governed Work Order.

## 20. Deviations

1. **Canary runtime report file absent.** WO §6.6's expected `/opt/bagman/runtime/evidence-classification-canary` report does not exist on production (confirmed absent by direct `ls`). Every fact it would have attested was nonetheless independently reconciled from live database state (§9–§16), so this is recorded as a documentation/evidence-preservation gap, not a verification failure. No action was taken to recreate or fabricate the missing file.
2. **Trinity not directly inspected**, per explicit dispatch-prompt instruction (which is more restrictive than, and controls over, WO §5's permissive "may inspect Git evidence concerning Trinity"). The "no Trinity writer" finding (§3 above) rests entirely on pre-existing durable `PID.md` record (§100.7–§100.9, §110), not on a fresh direct check of Trinity performed during this execution.
3. No other deviation from WO scope. No excluded action (§8 of the WO) was performed. No mutation of any kind occurred.

## 21. Evidence references

- Canonical WO: `work-orders/WO-BAGMAN-112F-GOVERNANCE-RECOVERY.md` (this checkout, dispatch SHA `1f1dadf51fcf81ac9d672c70a47565c8e456c194`).
- Canonical PID sections read in full at dispatch: `PID.md` §0, §111, §112.A–G, §113.
- GitHub: PR #24 (`gh pr view 24` → `state=MERGED`, `mergeCommit.oid=f459dee0d218fd95bdd26e66098547ba81dceea5`), PR #22 (merge commit `493af0c3923bf6f1bdd308521da8f3aca3fafac2`, confirmed via local `git log`/`git cat-file`/`git merge-base --is-ancestor`).
- Production host: Mac mini `192.168.11.4` (`mac-prod-01`), inspected read-only via SSH on 2026-10-07, as detailed in §4–§18 above. All queried values are exact-string/exact-number matches to §112.C/§112.D and WO §6 except where explicitly noted in §10 and §20.

## 22. Implementer verdict

**GREEN.**

Every mandatory acceptance criterion in WO §13 that falls within the FORGE Implementer's read-only evidence-collection mandate is independently supported by direct, non-circumstantial production evidence gathered in this session:

1. Mac mini authority independently confirmed (§3).
2. Deployment/migration lineage explained and durably authorised — `493af0c...` is a real, ancestor, PR #22 merge commit; no unexplained drift (§5, §6).
3. No reconciliation-cursor schema exists (§6).
4. `T_ACT` confirmed exactly `2026-10-02T09:58:51Z`, no alternate value anywhere (§8).
5. Zero classification jobs for `created_at < T_ACT` — hard gate query returned exactly `0` (§9).
6. Canary database evidence reconciles exactly with §112.D in every respect that live data can attest; the one cached report artifact is absent, honestly disclosed (§10, §20).
7. Exact canary job independently verified (§11).
8. Classification/AI/Needs You lineage reconciles exactly, including correlation IDs (§12, §13, §14).
9. Xero isolation independently supported, canary-scoped (§16).
10. Scheduler confirmed absent, no alternate `T_ACT` carrier found (§17).
11. No historical backfill — the only classification job's evidence item post-dates `T_ACT`, and the only populated job table row is the canary itself (§9, §11).
12. **No production mutation occurred at any point during this execution.** Every command was read-only: `docker ps`/`inspect`/`exec cat`/`exec grep`/`exec find` (no write flags), `alembic current`/`history` (no `upgrade`/`downgrade`), SQL wrapped in `BEGIN TRANSACTION READ ONLY; ... ROLLBACK;` with `SELECT` only, `ls`/`stat`/`shasum` on the backup file (not restored), HTTP `GET`/`curl` health probes only, `crontab -l`/`launchctl list`/`cat`/`grep` for scheduler inspection only. No container was restarted, rebuilt, or redeployed; no DDL/DML executed; no Needs You item's status changed; no classification worker invoked; no new canary or mailbox sweep run; no `T_ACT` or environment variable changed; no scheduler installed; no Xero write; no OAuth/credential change; no object-store or backup mutation.

    **PRODUCTION MUTATIONS PERFORMED: NONE.**
13. Gmail issue confirmed present, untouched (§19).
14–16. Outside this Implementer's mandate — require the fresh Independent Auditor (WO §12) and subsequent Architect Acceptance/merge, neither of which this document performs or claims.

No material claim in §112.C/§112.D was found unsupported or contradicted by direct evidence. The one genuine gap found (§20.1, absent canary report file) does not itself unsupport any acceptance criterion, since the same facts were independently reconciled from live, persisted database state rather than from that report. This closure document is submitted for the required fresh, independent Auditor review per WO §12 — it is not itself that audit, and does not claim Architect Acceptance or merge authority.
