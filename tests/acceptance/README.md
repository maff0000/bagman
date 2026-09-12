# tests/acceptance/

CD-3 WI-4 acceptance evidence (PID §43, §44, §35, §60) — real,
directly-runnable scripts that drive the ACTUAL `docker compose -p
bagman` runtime (build/up/down/restart/rm/recreate) and the REAL live
`bagman-api` HTTP surface, using the real secrets already provisioned
at `/srv/bagman-secrets/`. No mocks anywhere in this directory.

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
