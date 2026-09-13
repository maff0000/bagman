# ops/

Owns operational runbooks and procedures for running and maintaining
BAGMAN once it is deployed.

Implemented by CD-3 WI-4 (PID §34-35, §50):

* `backup_postgres.sh` / `restore_postgres.sh` — logical PostgreSQL
  backup/restore (`pg_dump`/`psql`, run inside the real `bagman-db`
  container via `docker compose exec`; password read from the same
  mounted secret file the container itself uses, never a literal on
  any command line).
* `backup_objects.py` / `restore_objects.py` — logical export/import of
  every object in the `bagman-evidence` bucket, preserving the
  `evidence/<evidence_id>/<content_hash>` key structure exactly. Run
  inside the real `bagman-api` image (which already has the same
  `BAGMAN_OBJECT_STORE_*` configuration/secrets and `boto3`) via a
  throwaway `docker compose run` container — see the repo-root
  `Makefile`'s `backup`/`restore` targets for the exact invocation.
* `_objects_common.py` — small shared helper module for the two object
  scripts above (not a public entry point itself).

These are operational scripts, not a governed architectural component
— no `component.yaml` was introduced for `ops/` (see the CD-3 WI-4
completion report for that judgement call, per PID §19's "create
manifests only for components that genuinely exist").

See `tests/acceptance/README.md` for the end-to-end restart/rebuild/
restore-into-clean-target proofs that exercise this tooling against
the real running stack.
