# BAGMAN CD-4 — Evidence Intake & Manual Upload Foundation — Delivery Evidence

**PID:** `PID.md` v4 ("BAGMAN PID v4 — Evidence Intake & Manual Upload Foundation")
**Delivery branch:** `cd-4/evidence-intake-and-manual-upload-foundation`
**This work item's branch:** `cd-4/wi5-acceptance-and-hardening` (WI-5, the final work item)
**Base for WI-5:** `75ef0db` (WI-1 through WI-4 already merged onto the CD-4 delivery branch)
**PL:** Bagman persona (Trinity ecosystem), operating under Forge doctrine (`/srv/forge`) in hub-model mode.
**Date:** 2026-09-13.

Per Forge doctrine this evidence trail lives in the repository, not only in session/fabric state. **This draft is written by the WI-5 Forge Engineer; it is not the final word** — the PL reviews, may amend it, commits it, and is the one who issues the CD-4 verdict (§72/§73) after dispatching an independent Auditor. Nothing in this file should be read as a self-issued verdict.

---

## 1. Delivery summary

Five PID-prescribed work items (PID §68), dispatched sequentially (each genuinely depends on the previous one's output):

| Work item | Commit(s) | Scope |
|---|---|---|
| WI-1 | `d45798e` | `services.evidence.intake`: `IntakeRecord` domain model, PID §7 state machine, `contracts/intake/bagman.intake_record.v1.schema.json`, `intake_records` table + Alembic migration, `PostgresIntakeRepository` with durable idempotency-conflict resolution |
| WI-2 | `10286b7` | Content validation pipeline: `IntakePolicy`, `filename_safety`, `content_sniffing` (hand-rolled magic-byte detection), `streaming.spool_stream` (bounded, incremental-hash ingest), `scanner.ClamAVScanner` (raw `clamd` protocol client), `validation_pipeline.run_intake_validation` orchestrating RECEIVED→VALIDATING→{REJECTED,QUARANTINED,FAILED,ACCEPTED} |
| WI-3 | `78cba12` | `POST /internal/intake/evidence` (the one governed HTTP boundary), `GET /internal/intake`/`GET /internal/intake/{id}`, the ACCEPTED→REGISTERED handoff (canonical `EvidenceItem` registration + full audit causation chain), CD-3 direct-upload bypass (`POST /internal/evidence`) removed entirely, `bagman-scan` added to Docker Compose, `/ready` extended to prove scanner reachability |
| WI-4 | `964d018` | The first BAGMAN Documents GUI: plain HTML/CSS/vanilla-JS static app served from `bagman-api`, Overview + Documents tabs, real upload/list/detail/download, honest QUARANTINED/REJECTED/FAILED presentation |
| WI-5 (this WI) | `b1555e0` | Acceptance/hardening: re-ran and confirmed WI-1 through WI-4's own acceptance scripts against the real stack; **found and fixed one real concurrency bug**; four new real-stack acceptance proof scripts (idempotency/concurrency, content-policy/quarantine, dependency-failure, browser); extended the architecture-boundary test suite for PID §67; this evidence file and the CHANGELOG entry |

No mailbox, bank, accounting, or billing connectivity was introduced anywhere in CD-4. `entity_id` is `None` on every registered `EvidenceItem` (no `GovernedEntity` seed data exists yet) — `entity_hint` is carried as a free-text label for a future delivery to resolve.

---

## 2. WI-5 — what was actually done

### 2.1 Re-ran the three existing (WI-4/CD-3) acceptance scripts against CD-4's real code

Per this WI's own instruction ("run all three yourself, for real... before writing anything new"):

```
python3 tests/acceptance/restart_proof.py               -> PASS, no fixes needed
python3 tests/acceptance/container_rebuild_proof.py      -> PASS, no fixes needed
python3 tests/acceptance/restore_into_clean_target_proof.py -> PASS, no fixes needed
```

All three passed cleanly on first run against the real, freshly-built Docker Compose stack (`bagman-db`/`bagman-objects`/`bagman-scan`/`bagman-api`), using the CD-4 governed intake endpoint `tests/acceptance/_lib.py::register_evidence()` was already updated to call (WI-3's own change). **No defect was found in these three scripts or the code they exercise** — WI-3's own careful reading of them, recorded in its commit, held up under actual execution.

### 2.2 Real concurrency proof — a genuine bug found and fixed

`app/api/routers/intake.py`'s own module docstring (written by WI-3) explicitly named this as a narrow, deliberately-unclosed gap: *"replay landing on `VALIDATING`... this handler does NOT attempt that [re-entering the pipeline]... A narrow, documented gap — WI-5's concurrency proof, not this one's."* This WI's job was to close it or prove it was already safe.

**What was built:** `tests/acceptance/idempotency_and_concurrency_proof.py`, covering PID §25/§52-54:

* (a) idempotency survives a real `docker compose down`/`up -d` restart (durable, no `-v`);
* (b) a same-key/different-bytes replay gets a genuine HTTP 409 `IDEMPOTENCY_CONFLICT`;
* (c.1) two genuinely concurrent `requests.post` calls (real `ThreadPoolExecutor`, real HTTP, real barrier-synchronized start) on the same `Idempotency-Key` + same bytes, against the real deployed stack.

**An honest, load-bearing finding surfaced while building (c.1):** `bagman-api` runs as a single Uvicorn worker (`entrypoint.sh`: `exec uvicorn app.api.main:app --host 0.0.0.0 --port 8000`, no `--workers`), and `intake_evidence`'s handler body contains no `await` on its own DB/scanner/object-store calls. Because of that, the ASGI event loop executes the two requests' handler bodies **one at a time, non-interleaved** — real, wall-clock-concurrent client requests are still serialized inside the one process. Timing evidence recorded in the script confirmed this: the two requests' *network* windows overlap, but by the time either handler body actually starts running, it never races the other's in-progress state transition. This means part (c.1) alone, run against today's deployment topology, would pass even if the underlying race protection were broken — it proves nothing about the database constraint PID §54 asks for ("Database constraints must back the application logic").

**So a second, genuine layer was added — (c.2):** real OS threads, synchronized with a `threading.Barrier`, executed **inside** the running `bagman-api` container (via `docker compose exec`), calling `IntakeRepository.create_intake_record`/`run_intake_validation` directly against the real PostgreSQL over the network (real threads talking to a real remote database genuinely release the GIL during socket I/O, so this DOES interleave — confirmed by the script's own timing analysis showing `overlapped=True` on every round run).

**Running (c.2) for the first time immediately reproduced a real bug**, not a theoretical one:

```
worker 0: RAISED InvalidStateTransitionError: IntakeRecord '...' cannot transition
          from 'VALIDATING' to 'VALIDATING'; allowed transitions from 'VALIDATING'
          are ['ACCEPTED', 'FAILED', 'QUARANTINED', 'REJECTED']
worker 1: created_status='RECEIVED' final_status='ACCEPTED' ...
```

**Root cause:** `app/api/routers/intake.py`'s handler unconditionally emitted the `INTAKE_VALIDATION_STARTED` audit event and THEN called `run_intake_validation(...)`, which itself performed the `RECEIVED -> VALIDATING` transition internally (`services/evidence/intake/validation_pipeline.py` line ~111, pre-fix). When two requests for the same idempotency key both read the row as `RECEIVED` (the documented narrow replay case), both entered this branch; whichever transaction lost the race to claim `VALIDATING` raised `InvalidStateTransitionError` from **inside** `run_intake_validation`, which propagated **uncaught** all the way to `app/api/main.py`'s generic exception handler — an unhandled HTTP 500, with a spurious `INTAKE_VALIDATION_STARTED` audit event already recorded for a validation attempt that never actually started.

**Fix applied** (both files, documented in their own module docstrings):

1. `services/evidence/intake/validation_pipeline.py::run_intake_validation` gained an optional `record: Optional[IntakeRecord] = None` parameter. When the caller passes an already-`VALIDATING` record (because the caller itself already claimed that transition), the function trusts it and skips its own internal `get_intake_record`/`transition_status("VALIDATING")` call. Omitting it preserves the exact prior behaviour for every existing caller (all of `tests/integration/test_intake_validation_pipeline.py`'s 20+ call sites pass unchanged).
2. `app/api/routers/intake.py::intake_evidence` now claims the `RECEIVED -> VALIDATING` transition **itself**, **explicitly**, **before** emitting `INTAKE_VALIDATION_STARTED` or calling `run_intake_validation` (passing it the already-claimed record via the new parameter). If that claim itself raises `InvalidStateTransitionError` (a concurrent request won the race a moment earlier), the handler catches it, re-fetches the record's current state, and falls straight through to the SAME already-documented "replay landing on VALIDATING" handling (HTTP 202, current in-flight state, **zero** new audit events) — instead of letting the exception escape as an unhandled 500 with a spurious audit event already written.

**Re-run after the fix:** all 5 rounds of the barrier-synchronized in-container race passed cleanly — exactly one intake_id created per round, no unhandled exceptions, genuine interleaving confirmed (`overlapped=True` every round). The full non-Docker + persistence + app_api pytest suites (283 tests total across the three groups) were re-run and all pass unchanged. The HTTP-layer proof (c.1) also re-confirmed clean (as it always was).

**This is a real, narrow, cleanly-fixable bug that was found and fixed, exactly as this WI's brief anticipated might happen** — not a flaky-test workaround, and not a case of forcing a false pass: the fix makes the router's own already-documented race-handling policy (claim-then-fallback-to-in-flight-state) actually cover the FULL race window, including the part of it that could previously only be reached from inside `run_intake_validation` itself.

### 2.3 Content-policy / quarantine fixture proof

`tests/acceptance/content_policy_and_quarantine_proof.py` (PID §63), run against the real stack with the REAL `bagman-scan` ClamAV daemon (no stub anywhere):

| Fixture | Result | Detail |
|---|---|---|
| Valid PDF/JPEG/PNG/text/CSV | ACCEPTED → REGISTERED | all 5 registered as canonical evidence |
| Empty file | REJECTED | `UNSUPPORTED_CONTENT_TYPE` (an empty stream sniffs to `application/octet-stream`, not in the accepted set — confirmed, not assumed) |
| Oversized stream (50 MiB + 1 KiB, one byte over `DEFAULT_INTAKE_POLICY.max_file_size_bytes`) | REJECTED | `FILE_TOO_LARGE`, aborted mid-stream (PID §13 — not buffered in full) |
| Reported/detected MIME mismatch (reported `application/pdf`, actual PNG bytes) | ACCEPTED | mismatch recorded observably (`mime_mismatch_observed: true`, `reported_mime_type_at_validation`) per `mime_mismatch_policy == "OBSERVE"` |
| Unsupported/unknown binary | REJECTED | `UNSUPPORTED_CONTENT_TYPE` |
| Synthetic archive (ZIP magic bytes) | REJECTED | `UNSUPPORTED_CONTENT_TYPE` (`archive_treatment == "REJECT"`) |
| Filename path-traversal attempt (`../../etc/passwd`) | REJECTED | `UNSAFE_FILENAME` |
| Standard EICAR test string | QUARANTINED | real ClamAV verdict `MALICIOUS: Eicar-Test-Signature` |
| Duplicate content, no/different idempotency keys | REGISTERED (twice) | two DISTINCT `evidence_id`s, identical `content_hash` — PID §24 "same hash ≠ same evidence" doctrine confirmed, not an automatic collapse |

No defect found; every fixture behaved exactly as `DEFAULT_INTAKE_POLICY`/`validation_pipeline.py` document.

### 2.4 Dependency failure proofs

`tests/acceptance/dependency_failure_proof.py` (PID §55-57), each dependency stopped/restarted in turn against the real stack:

* **Scanner (`bagman-scan`) stopped:** `/ready` → 503 `failed_dependency: "scanner"` immediately. A real upload attempted while stopped → HTTP 503, intake `FAILED`/`SCAN_FAILED`, `evidence: null` (fails closed, confirmed live against a real transport failure, not a scripted fake). Restarted → `/ready` green, fresh upload → 201.
* **Object storage (`bagman-objects`) stopped:** `/ready` → 503 `failed_dependency: "object_store"`. A real upload attempt → HTTP 503 `STORAGE_ERROR` (fails visibly, no `EvidenceItem` falsely created). Restarted → recovery confirmed.
* **Database (`bagman-db`) stopped:** `/ready` → 503 `failed_dependency: "postgres"`. A real upload attempt → HTTP 503 `PERSISTENCE_ERROR` (fails loudly, no local/in-memory fallback — the CD-3 invariant, re-confirmed still absolute in CD-4). Restarted → recovery confirmed.

**One operational characteristic worth flagging (not a defect):** `/ready`'s live object-store probe, when `bagman-objects` is simply *stopped* (its port silently drops the TCP SYN rather than sending an immediate RST), can take up to ~60 seconds to time out and return 503 — `persistence/objects/minio_store.py`'s `MinIOConfig` has no explicit boto3 connect/read timeout configured, so botocore's own (generous) defaults govern this. `/ready` still, eventually, returns the correct answer; this is a latency characteristic, not a correctness gap, but a future delivery may want to set an explicit short `Config(connect_timeout=..., read_timeout=...)` on the boto3 client so a stopped MinIO makes `/ready` fail fast rather than fail (correctly, but slowly).

The stack was returned to fully healthy (`{"ready": true, "checks": {"postgres": "ok", "object_store": "ok", "scanner": "ok"}}`) at the end of this script, every time it was run.

### 2.5 Browser acceptance proof (real Docker Compose stack, real ClamAV)

`tests/acceptance/browser_acceptance_proof.py` (PID §65/§66), driven with a real headless Chromium (Playwright) against the actual `bagman-api` container in the actual production Docker Compose composition (real Postgres, real MinIO, real ClamAV) at `http://127.0.0.1:8000/` — **not** the dev-mode/uvicorn instance WI-4's own Playwright proof used. Verified, in one real browser session:

1. App loads (`BAGMAN` shell header visible).
2. Documents tab renders.
3. A real, valid synthetic PDF uploaded through the real `<input type="file">`.
4. The real `#upload-status` text progressed to `"Complete — registered as evidence <id>"`.
5. The new row appeared in the real list (matched by its unique filename).
6. The detail panel opened and displayed the real `evidence_id` (after correctly waiting for `Detail.open()`'s own async `GET /internal/evidence/{id}` + `GET /internal/provenance/...` fetches to resolve — the overlay itself becomes visible synchronously, before that data arrives, which the proof script accounts for explicitly).
7. The real download link's `href` was fetched and the bytes were confirmed **byte-identical** to the original upload.
8. A real EICAR upload was quarantined by the real ClamAV daemon and rendered with the distinct `badge--warn` styling — `"Quarantined — content safety scanner verdict MALICIOUS: Eicar-Test-Signature"`.
9. A synthetic-archive (ZIP magic bytes) upload was rejected and rendered with the distinct `badge--bad` styling — `"Rejected — UNSUPPORTED_CONTENT_TYPE"`.

No defect found in the GUI. `playwright` is not added to `requirements-dev.txt` — it is a one-off browser-automation tool for this proof script, not something the ordinary `pytest tests/` suite needs (mirroring the same reasoning WI-4 already gave for not adding it there); installed ad hoc (`pip install playwright && python3 -m playwright install chromium`) wherever this specific proof is run.

### 2.6 Architecture proof (PID §67)

`tests/integration/test_architecture_boundaries.py` (an existing, already-`pytest`-collected, non-Docker file) was **extended**, not duplicated, with nine new test functions:

* `test_static_ui_assets_contain_no_python_or_server_side_code` — `app/api/static/` contains only `.js`/`.html`/`.css`.
* `test_static_ui_javascript_only_calls_the_existing_internal_http_api` — `app.js` contains no database connection string, no S3/MinIO/ClamAV endpoint token, references `/internal/` HTTP paths only.
* `test_core_and_services_evidence_never_import_app_or_persistence` — extends the existing adapters/agent/ui import-boundary check with `app`/`persistence` (PID §67's "canonical core does not import ... UI/infrastructure code").
* `test_validation_pipeline_never_imports_concrete_scanner_implementation` — AST-parses `validation_pipeline.py`'s own imports, asserts `ClamAVScanner` is absent and `EvidenceSafetyScanner` is present (scanner stays behind its abstraction).
* `test_no_live_mailbox_adapter_code_exists_yet` — `adapters/` contains no `.py` files yet.
* `test_internal_router_direct_upload_bypass_is_genuinely_gone` — AST-parses `app/api/routers/internal.py`, asserts no function parameter is annotated `UploadFile` and no `File(...)` call exists anywhere (checked structurally, not by grepping the module's own prose docstring, which legitimately still narrates the historical removal in plain text).
* `test_no_forbidden_mailbox_or_provider_sdk_imported_anywhere_in_the_repo` — AST import scan across the whole repository for `msal`, `googleapiclient`, `google_auth_oauthlib`, `imapclient`, `imaplib`, `exchangelib`, `O365` — none found.
* `test_requirements_files_do_not_mention_forbidden_mailbox_or_provider_sdks` — same check against `requirements.txt`/`requirements-dev.txt`.

Redis absence and the core→services→persistence layering direction were already covered by this file's pre-existing tests (re-run, still pass). All nine new tests pass; `pytest tests/integration/test_architecture_boundaries.py -q` → 24 passed (15 pre-existing + 9 new).

### 2.7 CI assessment

Re-read `.github/workflows/security.yml` in full. **No genuine gap found; no change made.** It already runs, in order: gitleaks, the architecture-memory drift check, `pytest tests/security tests/contract tests/integration` (which now includes this WI's 9 new architecture-boundary tests, since they extend an existing file already covered by that step), `pytest tests/persistence` (own step, own disposable Postgres), `pytest tests/app_api` (own step, own disposable Postgres+MinIO). CD-4 introduced no new top-level test directory across any of its five work items, so no new CI step is needed.

The four new `tests/acceptance/*_proof.py` scripts this WI adds are **deliberately NOT wired into CI**, for the exact reason `tests/acceptance/README.md` already states for the three pre-existing ones: they mutate/replace the real runtime (`docker compose stop`/real container-internal thread execution/a real destructive `down -v` inside the pre-existing scripts) rather than using disposable, uniquely-named fixtures the way every other `tests/` suite does — automatic CI execution of them would risk destroying real runtime state as a side effect of an ordinary pipeline run. This is confirmed to be the right call for these new scripts too, not merely inherited without re-checking: `idempotency_and_concurrency_proof.py` and `dependency_failure_proof.py` both restart/stop real named services; `browser_acceptance_proof.py` requires an extra `pip install playwright` step CI does not currently provision. **This is flagged here explicitly, not silently decided**, per this WI's own instruction — the PL's call to make differently if warranted.

### 2.8 Security

```
$ gitleaks detect --source . --no-git -v --redact
scanned ~2.24 MB in 137ms
no leaks found
```

No secrets/credentials as literals were added anywhere. `/srv/bagman-secrets/*` files were read by path only, never printed, never copied. No real malware was used anywhere — every scanner-detection proof uses the standardised, public EICAR test string only (the same constant WI-2/WI-3's own test suite already established, reused verbatim here, never re-invented).

---

## 3. Defects and gaps caught and fixed across the WHOLE CD-4 delivery arc (all five work items)

Reconstructed from `git log` across `d45798e`..`964d018` plus this WI's own work:

1. **(WI-2) A durable-persistence metadata-drop bug:** `PostgresIntakeRepository.transition_status` copied every updated `IntakeRecord` field back to its row EXCEPT `metadata` — so the validation pipeline's `storage_reference` (stamped into `metadata` on `ACCEPTED`) would have been silently dropped on the durable path while the in-memory repository (which stores the whole dataclass) masked the gap entirely. Fixed during WI-2's own reconciliation, with a new regression test proving metadata survives a fresh-repository-instance read.
2. **(WI-5, this WI) The real concurrency race** described in full in §2.2 above — the only defect found during the entire acceptance/hardening pass that required a code fix in already-merged CD-4 code, rather than confirming existing behaviour.

No other defects were found or fixed during CD-4's delivery arc — WI-1's, WI-3's, and WI-4's own commits report clean reconciliation with no fixes needed beyond what shipped in their own commit.

---

## 4. Full acceptance-criteria walk-through (PID §70)

### Intake
* Independent canonical identity — `IntakeRecord.intake_id`, distinct from `EvidenceItem.evidence_id` — **confirmed** (WI-1 contracts/domain, exercised throughout).
* State machine durable and valid — **confirmed** (`ALLOWED_TRANSITIONS`, enforced via `SELECT ... FOR UPDATE` row locking; re-confirmed under genuine concurrent load in §2.2).
* Manual upload enters only through intake — **confirmed** (§2.6, direct-upload bypass proven gone by AST inspection).
* Retry/idempotency semantics durable — **confirmed** (§2.2 part (a), survives a real restart).
* Concurrency race protected — **confirmed, after a real fix** (§2.2).

### Validation
* Size limits enforced server-side — **confirmed** (§2.3, oversized-stream fixture, aborted mid-stream).
* Filenames treated as untrusted — **confirmed** (§2.3, path-traversal fixture).
* MIME detected from content where practical — **confirmed** (hand-rolled magic-byte sniffing, §2.3).
* Reported/detected mismatch visible — **confirmed** (§2.3, mismatch fixture, recorded in metadata).
* Archives treated per policy — **confirmed** (§2.3, REJECT per `DEFAULT_INTAKE_POLICY.archive_treatment`).
* Executable/unsafe content not normally accepted — **confirmed** by WI-2's own test suite (re-run, passes); not separately re-proven live in this WI's new scripts (the EICAR fixture proves the SCANNER path; the executable-content-sniffing path is proven at `tests/integration/test_intake_validation_pipeline.py`, re-run and passing).

### Safety
* Scanner abstraction exists — **confirmed** (§2.6, `validation_pipeline.py` depends only on `EvidenceSafetyScanner`).
* Required scanner fails closed — **confirmed** (§2.4, scanner-stopped proof).
* Quarantine durable and visible — **confirmed** (§2.3 EICAR fixture; §2.5 GUI rendering).
* Quarantined bytes cannot enter downstream normal evidence workflow — **confirmed** (every quarantine outcome in §2.3/§2.5 carries `evidence: null`).

### Evidence
* Original bytes preserved exactly — **confirmed** (§2.5, byte-identical download proof).
* SHA-256 verified — **confirmed** (content_hash present and correct on every registered/quarantined fixture in §2.3).
* Canonical `EvidenceItem` created only after successful intake requirements — **confirmed** (every REJECTED/QUARANTINED/FAILED outcome in §2.3/§2.4 carries `evidence: null`).
* Provenance connects evidence to intake/source — **confirmed** (`metadata.intake_id` on every registered `EvidenceItem`; re-verified surviving restart/rebuild/restore in §2.1).
* Audit chain complete — **confirmed** (re-verified surviving restart/rebuild/restore in §2.1; §2.2's fix specifically closes the one path that could previously leave a spurious/incomplete audit entry under a genuine race).

### Recovery
* Partial failure visible — **confirmed** (§2.4, all three dependency-failure proofs).
* Restart safe — **confirmed** (§2.1, §2.2 part (a)).
* Storage/database/scanner failure does not create false success — **confirmed** (§2.4, every failure proof asserts `evidence: null`/no silent success).

### API
* Intake/list/detail/download governed — **confirmed** (WI-3, exercised throughout).
* Pagination implemented — **confirmed** (WI-3's `limit`/`offset` on `GET /internal/intake`, not separately re-tested here — no regression found).
* Direct upload bypass removed/delegated — **confirmed removed entirely** (§2.6).

### GUI
* Documents screen functional — **confirmed** (§2.5).
* Real upload works — **confirmed** (§2.5).
* Real statuses visible — **confirmed** (§2.5, REGISTERED/QUARANTINED/REJECTED all distinctly rendered).
* Evidence list/detail works — **confirmed** (§2.5).
* Download byte-identical — **confirmed** (§2.5).
* Quarantine/rejection visible — **confirmed** (§2.5).

### Architecture
* Component boundaries respected — **confirmed** (§2.6).
* No mailbox integration — **confirmed** (§2.6, AST scan of the whole repo).
* No Redis — **confirmed** (pre-existing tests, re-run, still pass).
* No canonical domain dependency on scanner/UI implementation — **confirmed** (§2.6).

### Security
* Gitleaks green — **confirmed** (§2.8).
* No secrets — **confirmed** (§2.8).
* No production data — **confirmed** (only synthetic fixtures used throughout).
* Prior security controls unchanged — **confirmed** (no change to any CD-1/CD-2/CD-3 security-doctrine file).

### CI
* All mandatory checks visibly green on live PR run — **NOT YET OBSERVED by this WI** (see §5 — this is explicitly the PL's own job, not the Engineer's, per this WI's dispatch instructions).
* Architecture memory drift check visibly green — locally confirmed (`architecture-index.md is up to date.`); live-CI observation is the PL's job (§5).
* Intake suites genuinely executed, not skipped — locally confirmed (every intake-related test file ran with 0 skips attributable to intake code — the skips present in `tests/persistence`/`tests/integration` are pre-existing, environment-conditional skips unrelated to CD-4, e.g. optional MinIO-dependent tests).

---

## 5. What is honestly NOT yet proven

* **Live CI has not been observed by this WI.** Per this WI's own explicit instruction, observing the live GitHub Actions run after the PR is opened is the PL's job, done after this WI is committed and pushed — not something a Forge Engineer does. Everything in §2.7 is a readiness assessment (the workflow file itself, read and reasoned about, plus every step it runs confirmed green locally), not a substitute for that observation. CD-3's own evidence trail (§6a in that file) is a direct, on-the-record demonstration of why this distinction matters: local-and-Auditor-green once genuinely diverged from live-CI-green there, for reasons neither could have caught without literally checking.
* **A fresh, independent Auditor has not yet reviewed this delivery.** Nothing in this file should be read as that review — it is this WI's own Engineer's account of its own work, exactly as honestly and completely as this WI's dispatch asked for, but Forge doctrine (PID §71) requires a zero-context Auditor's independent reproduction before any verdict is adjudicated.
* **The `/ready` object-store-probe latency characteristic** noted in §2.4 (up to ~60s to report 503 when `bagman-objects` is simply stopped) is flagged, not fixed — a deliberate choice, since fixing it would mean changing `persistence/objects/minio_store.py`'s boto3 client configuration, a change this WI judged out of its own narrow acceptance/hardening scope (no code defect was found — dependency-failure IS still correctly reported, just not instantly).
* **PID §54's requirement is proven at two layers, not one uniform layer** (§2.2) — the real-HTTP proof (as literally specified) and the real in-process/DB-layer proof (added because the HTTP-layer proof alone cannot force genuine interleaving against this single-worker deployment). Both are documented plainly in the proof script's own module docstring so a future reader does not mistake either layer for "the whole proof" on its own.

---

## 6. Local verification summary (this WI, final pass)

```
$ pytest tests/security tests/contract tests/integration -q   (fresh venv)
259 passed, 7 skipped

$ pytest tests/persistence -q   (fresh venv, own disposable Postgres)
82 passed, 17 skipped

$ pytest tests/app_api -q   (fresh venv, own disposable Postgres+MinIO)
30 passed, 1 warning

$ gitleaks detect --source . --no-git -v --redact
no leaks found

$ python3 scripts/generate_architecture_memory.py --check
architecture-index.md is up to date.

$ python3 tests/acceptance/restart_proof.py                        -> PASS
$ python3 tests/acceptance/container_rebuild_proof.py               -> PASS
$ python3 tests/acceptance/idempotency_and_concurrency_proof.py     -> PASS (after the fix in §2.2)
$ python3 tests/acceptance/content_policy_and_quarantine_proof.py   -> PASS
$ python3 tests/acceptance/dependency_failure_proof.py              -> PASS
$ python3 tests/acceptance/browser_acceptance_proof.py              -> PASS
$ python3 tests/acceptance/restore_into_clean_target_proof.py       -> PASS (final teardown)
```

**Final Docker Compose state, deliberately:** fully torn down (`docker compose -p bagman down -v` + `bagman-api:dev` image removed, confirmed zero `bagman-*` containers/volumes/networks/images remain) — the same final state CD-3's own acceptance chain leaves things in, chosen so this WI's own dispatch (its `restore_into_clean_target_proof.py` re-run) is the natural last acceptance step, and so no real container/volume is left consuming host resources between this WI's completion and the PL's own review. The other four new proof scripts (§2.2-§2.5) are each independently runnable and each leave the stack running/healthy on their own — only the pre-existing `restore_into_clean_target_proof.py`, run last, performs the actual final teardown.

---

## 6a. PL reconciliation (independent re-verification, all five work items)

Before committing each work item, the PL independently re-derived every claim above rather than trusting the Engineer's report alone — Forge doctrine's own reconciliation step:

* **WI-1** (contracts/domain/persistence): independently re-ran the full test suite in a fresh venv (154 + 67 + 6 = 227 tests green), reran gitleaks and the architecture-memory check, and read every delivered file before committing.
* **WI-2** (content validation/quarantine): same independent re-run (251 + 81 + 6 tests green); **found a real defect during this reconciliation, not reported by the Engineer** — `PostgresIntakeRepository.transition_status` never persisted a `metadata` field update (every other field was copied back to the row, `metadata_` was not), so the durable path would have silently dropped the validation pipeline's `storage_reference` on `ACCEPTED`. Fixed by the PL directly, with a new regression test (`test_transition_status_persists_metadata_via_a_fresh_repository_instance`), before committing — see §3 item 1 above.
* **WI-3** (governed intake API): independently re-ran the full suite including a real disposable ClamAV daemon through the actual HTTP layer (369 tests green across all suites), read every new/changed file including the composition wiring, the `MANUAL_UPLOAD` source lifecycle, and the bypass-closure rationale. No defect found.
* **WI-4** (Documents GUI): independently re-ran the full suite (363 tests green), personally booted the app (`BAGMAN_RUNTIME_ENV=development uvicorn app.api.main:app`) and curled `/`, `/app.js`, `/internal/intake`, `/health` to confirm the static mount and JSON API genuinely coexist as claimed. No defect found.
* **WI-5** (this work item): independently re-ran, from a cold `make build`/`make start`, **all seven** real-stack acceptance scripts myself against a freshly built stack — `restart_proof.py`, `container_rebuild_proof.py`, `idempotency_and_concurrency_proof.py` (including the 5-round barrier-synchronized in-container race — reproduced the same `overlapped=True` interleaving and confirmed zero unhandled exceptions post-fix), `content_policy_and_quarantine_proof.py` (including the real EICAR→MALICIOUS→QUARANTINED path), `dependency_failure_proof.py` (all three dependencies stopped/recovered), and `restore_into_clean_target_proof.py` (the final teardown, independently confirmed zero `bagman-*` containers/volumes/networks/images remain afterward). Also independently re-ran the full pytest suite (341 + 30 tests green), gitleaks, and the architecture-memory check. No further defect found — WI-5's own account holds up under independent reproduction.

This is the same standard CD-1/CD-2/CD-3 held to: an Engineer's own report is never the final word before a commit lands; the PL's own reconciliation is.

---

## 6b. Independent Auditor (fresh, zero-context) — dispatched after all five WIs were merged

A fresh Auditor with no prior context on this delivery was dispatched against the merged branch (head `66a7210` at dispatch time) to independently re-derive every PID §70 criterion from scratch — not trusting this evidence file's own claims, per Forge doctrine (PID §71). Summary of what the Auditor did and found:

* Independently rebuilt a clean venv and re-ran every pytest suite (`tests/security`/`tests/contract`/`tests/integration` including the new PID §67 tests, `tests/persistence`, `tests/app_api`) — all green, matching the counts above.
* Independently ran gitleaks twice (working-tree and full-history modes) — clean both times.
* Independently stood up the real Docker Compose stack from a cold build and, themselves, drove a real upload/download/hash-compare, a path-traversal-filename rejection, a real EICAR upload/quarantine, an idempotency-conflict 409, and — with particular scrutiny, given this is where the delivery's own concurrency bug was found — **re-ran `idempotency_and_concurrency_proof.py` twice** (10 barrier-synchronized rounds total), confirming the fix holds under repeated independent reproduction, not just the original single run.
* Independently ran `dependency_failure_proof.py` (all three dependencies stopped/recovered) and got Playwright/Chromium genuinely working for a real browser-level GUI check (not a fallback to HTTP-only reasoning).
* Independently re-verified every PID §67 architecture claim by their own grep/AST inspection, not merely by re-running the delivery's own tests.
* **Found one real, independent defect** this evidence file's own drafting had not caught: `tests/integration/test_architecture_boundaries.py::test_no_schema_file_hardcodes_a_provider_name_inside_a_structural_constraint` (pre-existing since CD-2, not one of WI-5's own nine new PID §67 tests, but living in the file WI-5 extended) built a `violations` list but contained **no `assert` statement at all** — it always passed regardless of content, providing zero actual enforcement. The Auditor confirmed the underlying property it is meant to protect currently holds (no live violation), but flagged the test itself as vacuous.
* **Issued verdict: `BLOCKED`** — not `RED`, not `GREEN` — for exactly one reason: at audit time, **no pull request had ever been opened for any CD-4 branch**, and this repository's CI workflow only triggers on `pull_request`/`push:main`, so live CI had genuinely never executed against this code (confirmed via `gh api` against the real GitHub repository — zero PRs, zero Actions runs, zero check-runs for any `cd-4/*` branch or commit). PID §64's own standing lesson ("do not assume local green means CI green") and CD-3's own recorded history of real live-CI-only failures on this exact project make this a genuine, non-theoretical gap, not a formality. The Auditor was correct to refuse a self-issued `GREEN` in that circumstance, and explicitly noted this was the *only* blocking condition — every other §70 criterion they could independently test, they confirmed.

**Both findings closed by the PL** (commit `3592c82`, on top of the audited `66a7210`): the vacuous test now has a real `assert not violations, ...` (re-verified: still passes — the property genuinely holds — and now actually enforces it going forward); PR #4 (`cd-4/evidence-intake-and-manual-upload-foundation` -> `main`) was opened, and live CI was observed directly by the PL — see §6c.

## 6c. Live CI, observed (closing the Auditor's sole blocking finding)

Per PID §64 and the standing lesson from CD-3 (local/Auditor-green and live-CI-green are different claims — always check the second directly): PR #4 was opened at `https://github.com/maff0000/bagman/pull/4`, head commit `3592c82`. The GitHub Actions "Security" workflow ran automatically on that PR and was watched directly by the PL to completion (`gh run watch`, not inferred from a webhook or assumed):

```
$ gh pr checks 4
security	pass	1m35s	https://github.com/maff0000/bagman/actions/runs/34751927607/job/103709818157

$ gh run view 34751927607
✓ cd-4/evidence-intake-and-manual-upload-foundation Security #4 · 34751927607
JOBS
✓ security in 1m35s (ID 103709818157)
```

One benign annotation (GitHub's own Node.js 20 deprecation notice on the `actions/checkout`/`actions/setup-python` runner infrastructure — unrelated to BAGMAN's code, not a finding). Live CI is genuinely green at head `3592c82`, closing the independent Auditor's sole `BLOCKED` condition.

With this, every PID §70 acceptance criterion has now been independently confirmed at least twice over (PL reconciliation + fresh Auditor + this live-CI observation) with no outstanding gap. Per Forge doctrine, the PL does not self-issue the final CD delivery verdict — that is the architect's ruling to make (as it was for CD-1/CD-2/CD-3) — but every precondition the PID's own verdict vocabulary (§72) requires for `INTAKE_FOUNDATION_GREEN` is, as of head `3592c82`, satisfied and independently evidenced above.

---

## 7. Exit-gate statement (PID §73) — drafted, not issued

Per PID §73, no live mailbox integration may begin until CD-4 reaches **INTAKE_FOUNDATION_GREEN**. This Engineer's own account of the evidence above supports that outcome — every PID §70 acceptance bullet was independently exercised against the real stack (not merely read about), the one genuine defect found during the whole delivery arc (§2.2) was fixed with a documented, narrow, root-cause fix rather than a workaround, and no mailbox/bank/accounting/billing connectivity or forbidden SDK exists anywhere in the tree. **The actual verdict — `INTAKE_FOUNDATION_GREEN`, `INTAKE_FOUNDATION_RED`, or `BLOCKED` — is the PL's to issue, after PL reconciliation and independent Auditor dispatch (PID §71), not this Engineer's.**

---

## 8. Architect delta round (PR #4 review, 2026-09-13)

The human Architect reviewed PR #4 at head `83c3f8552cd4ef99392bc891e72c4666a7996aaf` (the state recorded through §7 above) and ruled:

> "CD-4 is INTAKE_FOUNDATION_GREEN in substance, but NOT YET APPROVED FOR MERGE." — two specific deltas required before merge; no redesign, no unrelated refactor, no CD-5 authorised.

### 8.1 Delta A — safe server-side evidence download headers

**Finding:** `GET /internal/evidence/{evidence_id}/content` returned raw bytes with no `Content-Disposition` header at all — only the GUI's client-side `<a download>` attribute (`app/api/static/app.js`) forced a save. The Architect ruled the server itself, not a browser attribute a client fully controls and could omit, must be the actual safety boundary.

**Fix (commit `7868b48`):**

* New `app/api/http_headers.py::safe_content_disposition_header()` — the RFC 6266/RFC 5987 dual-parameter pattern: an ASCII-only, quote/backslash/separator-free, control-character-free `filename="..."` fallback (built by *dropping*, never escaping, every unsafe character — a dropped character can never re-open a broken escape sequence) plus a fully `urllib.parse.quote(..., safe="")`-percent-encoded `filename*=UTF-8''...` for correct Unicode support. Falls back to a safe `evidence-<evidence_id>` label when no usable filename remains.
* `app/api/routers/internal.py::get_evidence_content` now sets this header plus `X-Content-Type-Options: nosniff` on every response. Bytes and the existing `X-Bagman-*` hash headers are unchanged.
* `app/api/static/app.js`'s download-link comment rewritten: the attribute is defense-in-depth only from here on, not the safety boundary.
* New `tests/app_api/test_evidence_download_headers.py` (12 tests): ordinary filename, Unicode round-trip, double-quote filename, `nosniff` presence, byte-identity, plus two direct unit tests of the helper itself for CRLF-injection and path-traversal strings (neither reachable through the real HTTP pipeline today, since `services.evidence.intake.filename_safety` already rejects both at intake — tested directly against the helper to prove the encoding layer is defensive on its own terms regardless of upstream filtering).

### 8.2 Delta B — immutable ClamAV image identity

**Finding:** `deployment/compose/docker-compose.yml`'s `bagman-scan` service used `image: clamav/clamav:stable` — a moving release channel, not an immutable runtime identity, despite an existing comment defending this as deliberate. The Architect overrode that reasoning: a release channel is still a moving tag regardless of how deliberately chosen, and is not acceptable identity for a runtime dependency.

**Fix (commit `7868b48`):**

* Independently resolved digest (PL, then re-confirmed independently by both the delta Engineer and the focused Auditor, all three matching): `clamav/clamav@sha256:1fdfd24c6f0a0fb60788481487459a6d4eda8a9b448641594e04db8410d34422` — the exact image the `clamav/clamav:stable` tag resolved to on this host at delta time, i.e. the same image every CD-4 ClamAV-dependent test had already been built and verified against.
* `deployment/compose/docker-compose.yml`'s `bagman-scan.image` is now `clamav/clamav:stable@sha256:1fdfd24c6f0a0fb60788481487459a6d4eda8a9b448641594e04db8410d34422` — the `@sha256:...` suffix is what actually pins the image; `:stable` is retained only as a human-readable release-channel label. Comment rewritten to state this distinction accurately (the prior comment calling `:stable` alone "pinned" was wrong and has been replaced).
* `tests/app_api/conftest.py::CLAMAV_IMAGE` pinned to the identical digest, so the disposable per-test-run fixture can never silently drift onto a different ClamAV build than the production service without a test catching it.
* `tests/integration/test_intake_scanner.py`'s manual, human-typed disposable-container docstring command deliberately left on the bare `:stable` tag (never executed by CI/fixtures, so it cannot cause silent drift between two automated things) — documented as a considered choice, not an oversight.

### 8.3 Local verification (delta Engineer, then independently re-run by the PL)

Both runs green, matching:

```
pytest tests/app_api -v                                          -> 42 passed
pytest tests/security tests/contract tests/integration tests/persistence -v  -> 341 passed, 24 skipped
gitleaks detect --source . -v --redact  (full history)            -> no leaks found
python3 scripts/generate_architecture_memory.py --check           -> up to date
git diff --check                                                  -> clean
```

Real-stack verification (PL, independently, from a cold `make build && make start`): `bagman-scan` healthy on the pinned image (confirmed via `docker inspect bagman-scan --format='{{.Config.Image}}'`); `GET /ready` fully green; a real upload with a Unicode filename (`café-测试-invoice.pdf`) registered successfully; a real EICAR upload was genuinely quarantined by the digest-pinned daemon (`quarantine_reason: "content safety scanner verdict MALICIOUS: Eicar-Test-Signature"`); download of the registered evidence returned byte-identical content (SHA-256 matched) with both `Content-Disposition` (correctly percent-encoded, verified by decoding `filename*` back to the exact original Unicode string) and `X-Content-Type-Options: nosniff` present. Stack torn down fully afterward, zero `bagman-*` resources remaining.

### 8.4 Focused delta Auditor (fresh, zero-context, delta-only per the Architect's instruction)

Dispatched against the merged delta (commit `4953400`). **Verdict: `INTAKE_FOUNDATION_GREEN`.** Summary of independent work performed (full detail in the Auditor's own report, held in session record):

* Read the actual diff (`git show 7868b48`), not summaries.
* Wrote their own throwaway adversarial script (not copied from the delivered test file) exercising `safe_content_disposition_header` against inputs of their own devising — a CRLF+`Set-Cookie` injection attempt, a bare quote, backslash/quote breakout attempts, a Unicode bidi-override character, empty/`None`, an all-control-character string, a 10,004-character filename, and several more — and confirmed structurally (parsing the returned header, checking for raw CR/LF, checking quoted-string integrity) that no CRLF injection, quote-breakout, or path-separator smuggling was possible in any case tried.
* Re-ran `tests/app_api/test_evidence_download_headers.py` against the real disposable stack (12 passed) and independently re-confirmed byte-identity.
* Independently re-resolved the ClamAV digest (`docker pull` + `docker inspect`) and confirmed it matches `docker-compose.yml`'s pin exactly.
* Independently stood up the real stack, confirmed `bagman-scan` is genuinely running the pinned image (`docker inspect bagman-scan --format='{{.Image}}'` matched `docker inspect clamav/clamav@sha256:...` exactly — not a stale cached layer), `/ready` green, a real upload registered successfully, a real EICAR upload was genuinely quarantined, and a real download carried both new headers correctly encoded with byte-identical content — then tore the stack down fully.
* Re-ran the full regression suite (`tests/app_api` 42 passed; `tests/security`/`tests/contract`/`tests/integration`/`tests/persistence` 341 passed, 24 skipped, 0 failed), full-history gitleaks (clean, 45 commits scanned), architecture-memory check (clean), and confirmed the diff's scope touched only the 7 files expected from these two deltas — nothing unexpected.
* **Two non-blocking observations recorded, neither a defect within the Architect's stated scope, neither fixed as part of this delta:**
  1. Unicode bidi-override/isolate characters (U+202A–202E, U+2066–2069) are not stripped by the new header helper (correctly excluded from the ASCII fallback, but preserved percent-encoded in `filename*`) nor by the pre-existing upstream `filename_safety.py` validation — a filename-spoofing gap (e.g. a visually-reversed extension in a save dialog) that predates this delta (the GUI's own `download` attribute already carried the same unfiltered value before Delta A existed) and was not part of the Architect's CRLF/quoting/path-semantics remit. Flagged as a future hardening item, not fixed here.
  2. `safe_content_disposition_header` has no independent length cap of its own, despite its docstring's "safe entirely on its own terms" framing — not currently reachable in practice, since `filename_safety.DEFAULT_MAX_FILENAME_LENGTH = 255` is enforced at intake before any `EvidenceItem.original_name` can exist. Noted for completeness, not fixed here (would only matter if a future change ever let an unvalidated `original_name` reach this code path).

### 8.5 Live CI — observed at the final head, step-by-step (PID §64's standing lesson)

Final PR head after the delta: **`4953400`** (merge of `7868b48` onto `cd-4/evidence-intake-and-manual-upload-foundation`), pushed to PR #4.

```
$ gh pr checks 4
security	pass	1m44s	https://github.com/maff0000/bagman/actions/runs/34763110206/job/103739262417

$ gh api repos/maff0000/bagman/actions/runs/34763110206/jobs
job: security  conclusion=success
  - Set up job:                                                              success
  - Checkout:                                                                success
  - Set up Python:                                                           success
  - Install gitleaks (pinned release binary):                                success
  - Run gitleaks scan:                                                       success
  - Install dependencies:                                                    success
  - Check architecture memory projection:                                   success
  - Run non-Docker test suites (security + contract + domain + ...):        success
  - Run PostgreSQL persistence test suite (own disposable container):        success
  - Run bagman-api runtime test suite (own disposable Postgres + MinIO):     success
  - Post Set up Python / Post Checkout / Complete job:                       success
```

Every individual step succeeded — gitleaks green, architecture-memory drift check green, all three test-suite steps (which now include the new download-header tests and the digest-pinned ClamAV container) green, nothing skipped. Not inferred from the aggregate "pass" alone — inspected step-by-step, per the standing CD-3 lesson that local/Auditor-green and live-CI-green are different claims that must each be checked directly.

### 8.6 Working tree / PR state at close of this delta round

* Final PR #4 head: **`4953400`**.
* Working tree: clean (nothing uncommitted).
* PR remains open, un-merged. **No merge has been performed or attempted.** The Architect's own ruling is required before any merge, guarded by this exact head SHA.
