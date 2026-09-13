# tests/acceptance/

CD-3 WI-4 acceptance evidence (PID §43, §44, §35, §60), extended by
CD-4 WI-5 (PID §25/§52-57/§63/§65) — real, directly-runnable scripts
that drive the ACTUAL `docker compose -p bagman` runtime (build/up/
down/restart/rm/recreate/stop/start) and the REAL live `bagman-api`
HTTP surface, using the real secrets already provisioned at
`/srv/bagman-secrets/`. No mocks anywhere in this directory.

## Why these are not `pytest`-collected

Every other test directory under `tests/` uses disposable, uniquely-
named fixture containers (`bagman-test-postgres-wi1`,
`bagman-test-minio-wi3`, ...) that a test session starts and tears
down itself, safe to run on every `pytest tests/ -q`. These scripts
are different in kind: they operate on the real `bagman-db` /
`bagman-objects` / `bagman-api` project stack itself, including a full
`docker compose -p bagman down -v` (destroying named volumes) as part
of the restore-into-clean-target proof. That must never happen as a
side effect of an ordinary test run, so these files are deliberately
named without a `test_` prefix — pytest does not collect them, and
`pytest tests/ -q` never touches the real runtime.

Run them explicitly, in this order (the middle script assumes a stack
is already up; the last one performs the final full teardown for all
three):

```bash
python3 tests/acceptance/restart_proof.py
python3 tests/acceptance/container_rebuild_proof.py
python3 tests/acceptance/restore_into_clean_target_proof.py
```

or all three via `make accept`. Each script also independently brings
the stack up itself first (`docker compose build` + `up -d --wait`),
so any one of them can also be run standalone against a cold host.

CD-4 WI-5 adds four more scripts, same style/rigor, same reasons for
NOT being `pytest`-collected (see below). Each also independently
brings the stack up first, so any one is runnable standalone; run them
in any order (they do not depend on state left behind by the three
scripts above, and — unlike `restore_into_clean_target_proof.py` above
— none of them perform a final `down -v`, so the stack is always left
running afterwards):

```bash
python3 tests/acceptance/idempotency_and_concurrency_proof.py
python3 tests/acceptance/content_policy_and_quarantine_proof.py
python3 tests/acceptance/dependency_failure_proof.py
python3 tests/acceptance/browser_acceptance_proof.py   # requires: pip install playwright && python3 -m playwright install chromium
```

## What each proves

* `restart_proof.py` — PID §43: register synthetic entity/source/
  evidence + provenance/audit via the live API, `docker compose down`
  (no `-v`) then `up -d` again, prove every canonical ID/hash/byte/
  lineage/audit fact survives, then repeat the exact same external
  observation and prove idempotent replay survives the restart too.
* `container_rebuild_proof.py` — PID §44: with the stack up, register
  fresh synthetic data, `docker compose rm -sf bagman-api` (ONLY that
  container — `bagman-db`/`bagman-objects` are proven untouched by
  their container IDs being unchanged), recreate it, prove the same
  data is still reachable — persistence lives in the volumes/services,
  not `bagman-api`'s own container filesystem layer.
* `restore_into_clean_target_proof.py` — PID §35/§60 steps 17-20: back
  up (via `ops/backup_postgres.sh` + `ops/backup_objects.py`),
  `docker compose down -v` (destroys the named volumes — a genuinely
  clean target), bring the stack back up (fresh empty schema/bucket),
  restore (via `ops/restore_postgres.sh` + `ops/restore_objects.py`),
  prove every canonical ID/hash/byte/lineage/audit fact from before the
  destroy is back, then perform the final clean shutdown (`down -v`,
  confirmed no `bagman-*` containers/volumes/networks remain, plus the
  `bagman-api` image removed).

## CD-4 WI-5 additions

* `idempotency_and_concurrency_proof.py` — PID §25/§52-54: idempotency
  survives a real restart; a same-key/different-bytes replay gets a
  real HTTP 409 `IDEMPOTENCY_CONFLICT`; two genuinely concurrent
  requests on the same idempotency key (real threads, real
  `requests.post` calls) resolve to one canonical outcome. Also runs a
  second, lower-level concurrency proof (real OS threads, barrier-
  synchronized, executed INSIDE the running `bagman-api` container via
  `docker compose exec`, directly against
  `IntakeRepository.create_intake_record`/`run_intake_validation`) —
  see this file's own module docstring for exactly why the HTTP-layer
  proof alone cannot force genuine interleaving on this single-worker
  deployment, and the real, reproduced-then-fixed race this second
  layer actually caught in `app/api/routers/intake.py`.
* `content_policy_and_quarantine_proof.py` — PID §63's full fixture
  list (valid PDF/JPEG/PNG/text/CSV, empty file, oversized stream,
  MIME mismatch, unsupported binary, synthetic archive, path-traversal
  filename, the standard EICAR test string, duplicate content) against
  the real stack with the REAL `bagman-scan` ClamAV daemon — no stub
  scanner anywhere in this script.
* `dependency_failure_proof.py` — PID §55-57: stops `bagman-scan`,
  `bagman-objects`, and `bagman-db` in turn, proving `/ready` reports
  503 with the correct `failed_dependency`, a real upload attempted
  during each outage fails closed/visibly/loudly (never a false
  success), and each dependency's restart brings the stack back to
  fully healthy with a fresh upload succeeding again.
* `browser_acceptance_proof.py` — PID §65/§66: a real headless-Chromium
  (Playwright) session against the REAL Docker Compose `bagman-api`
  (not the dev-mode instance WI-4 used) — app loads, Documents
  renders, a real upload through the real file input, the real
  workflow-status text progressing to completion, the new row
  appearing in the real list, the detail panel opening with real data,
  a byte-identical download, and both a QUARANTINED (real EICAR, real
  ClamAV) and a REJECTED (synthetic archive) upload rendering
  correctly and distinctly. Requires `playwright` (`pip install
  playwright && python3 -m playwright install chromium`) — deliberately
  not added to `requirements-dev.txt`; see the CD-4 WI-5 evidence file.

## CD-5 WI-5 additions (AI Foundation acceptance, PID §81-88)

Same style/rigor, same reasons for NOT being `pytest`-collected. Each
also independently brings the stack up first; run them in any order
(none perform a final `down -v`):

```bash
python3 tests/acceptance/mac_mini_background_tier_live_proof.py
python3 tests/acceptance/trinity_escalation_live_proof.py
python3 tests/acceptance/claude_operator_live_proof.py
python3 tests/acceptance/prompt_injection_live_proof.py
python3 tests/acceptance/ai_gui_browser_acceptance_proof.py   # requires: pip install playwright && python3 -m playwright install chromium
```

* `mac_mini_background_tier_live_proof.py` — PID §14/§83/§87: real
  `bagman-fast`/`bagman-core` calls via the real HTTP surface against
  the real, existing Trinity LiteLLM installation. Proves this WI's own
  `host.docker.internal`/`extra_hosts` container-network fix holds for
  real, re-checks the (previously found down) LiteLLM-gateway backing
  database live, and honestly reports either a genuine live completion
  or the exact real blocked outcome. Also proves canonical-evidence
  non-interference, no silent cross-tier fallback, restart survival,
  and retry semantics.
* `trinity_escalation_live_proof.py` — PID §81/§82/§87: the real
  `bagman-deep` alias, exercised via the real `LiteLLMClient` directly
  inside the real running `bagman-api` container (no CD-5 task
  contract currently prefers `bagman-deep`, so no HTTP-level task
  triggers it — a genuine, documented gap; see the script's own module
  docstring for why this is still a real, non-fake proof of the alias/
  adapter/gateway path).
* `claude_operator_live_proof.py` — PID §81/§84/§88: re-checks
  `/srv/bagman-secrets/anthropic_api_key`'s existence live and attempts
  a real Ask BAGMAN call regardless; honestly reports
  `CLAUDE_AUTHENTICATION_FAILED` (the expected, correct, fail-closed
  outcome while the key remains absent) or completes the full real
  proof if the key now exists.
* `prompt_injection_live_proof.py` — PID §77-80/§85: synthetic evidence
  containing hostile instructions, uploaded through the real intake
  pipeline; a real `POST /internal/ai/tasks` attempt against whichever
  tier is genuinely reachable; plus an explicitly fake-backed
  structural proof (the real orchestration function, inside the real
  container) for the part the real tier's own outage currently blocks.
* `ai_gui_browser_acceptance_proof.py` — PID §86: real headless
  Chromium against the real stack — Overview AI status, Documents
  regression, the AI Analysis panel (real analysis attempt, real
  provenance/capability-alias display), Ask BAGMAN (real or honestly
  fallback-rendered), and clean failure-state rendering throughout.

## Failure proof (PID §45) — not duplicated here

The hash-mismatch-rejection proof required by PID §45 is not
reachable through the current HTTP API (`POST /internal/evidence`
computes the content hash server-side from the uploaded bytes; there
is no caller-declared-hash-to-verify-against parameter) and is already
covered at the `persistence.objects` layer by WI-2's own
`tests/persistence/test_minio_store.py::test_put_hash_mismatch_rejected_and_nothing_stored`
and
`::test_verify_hash_and_get_detect_real_corruption_and_raise_integrity_error`.
This work item deliberately references/reruns those rather than
inventing a redundant third proof of the same invariant — see the
WI-4 completion report for the explicit rerun evidence.
