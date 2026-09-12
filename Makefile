# BAGMAN operational commands (CD-3 WI-3, PID §50).
#
# GIT_COMMIT is resolved once here and exported for docker-compose.yml's
# `${BAGMAN_GIT_COMMIT:-unknown}` build-arg interpolation, so `make
# build`/`make start` always bake the real current commit into the
# image without anyone having to remember to pass it by hand.
GIT_COMMIT ?= $(shell git rev-parse HEAD 2>/dev/null || echo unknown)
export BAGMAN_GIT_COMMIT := $(GIT_COMMIT)

COMPOSE_FILE := deployment/compose/docker-compose.yml
COMPOSE := docker compose -p bagman -f $(COMPOSE_FILE)

.PHONY: build start stop status logs migrate test backup restore accept

## Build the bagman-api image (bakes in GIT_COMMIT).
build:
	$(COMPOSE) build

## Start the full BAGMAN runtime (bagman-db, bagman-objects, bagman-api).
start:
	$(COMPOSE) up -d

## Stop the runtime (containers/network only — named volumes are kept;
## use `docker compose ... down -v` directly for a full teardown).
stop:
	$(COMPOSE) down

## Show container status.
status:
	$(COMPOSE) ps

## Follow logs from every service.
logs:
	$(COMPOSE) logs -f

## Run `alembic upgrade head` against the RUNNING bagman-db, via the
## running bagman-api container (which already has alembic.ini/alembic/
## and the persistence/postgres/ package on its image) — the same
## explicit, visible migration step the container's own entrypoint runs
## at startup (PID §36), invokable again by hand.
migrate:
	$(COMPOSE) exec bagman-api alembic upgrade head

## Run the full test suite.
test:
	pytest tests/ -q

## Back up bagman-db (pg_dump, via ops/backup_postgres.sh) and every
## object currently in the bagman-evidence bucket (via
## ops/backup_objects.py, run inside a throwaway bagman-api container
## so it shares that container's real BAGMAN_OBJECT_STORE_* config/
## secrets and bagman-net reachability — no host port exposure
## required). Requires the stack to already be up (`make start`).
## Writes into backups/ (gitignored, CD-1 .gitignore's own `backups/`
## entry — PID §34).
backup:
	@mkdir -p backups/postgres backups/objects
	ops/backup_postgres.sh backups/postgres
	$(COMPOSE) run --rm --no-deps -e PYTHONPATH=/app \
		-v "$(CURDIR)/ops:/host-ops:ro" \
		-v "$(CURDIR)/backups:/host-backups" \
		--entrypoint python3 bagman-api \
		/host-ops/backup_objects.py "/host-backups/objects/$$(date -u +%Y%m%dT%H%M%SZ)"

## Restore a previous PostgreSQL dump and object-store export produced
## by `make backup` (PID §34-35). Usage:
##   make restore PG_DUMP=backups/postgres/bagman-postgres-<ts>.sql \
##                OBJECTS_DIR=backups/objects/<ts>
## PG_DUMP may be any path; OBJECTS_DIR must be given relative to the
## repo root, under backups/ (where backup_objects.py writes by
## convention), since it is mounted into the restore container the
## same way backup does.
restore:
	@test -n "$(PG_DUMP)" || { echo "usage: make restore PG_DUMP=<path/to/dump.sql> OBJECTS_DIR=backups/objects/<ts>" >&2; exit 2; }
	@test -n "$(OBJECTS_DIR)" || { echo "usage: make restore PG_DUMP=<path/to/dump.sql> OBJECTS_DIR=backups/objects/<ts>" >&2; exit 2; }
	ops/restore_postgres.sh "$(PG_DUMP)"
	$(COMPOSE) run --rm --no-deps -e PYTHONPATH=/app \
		-v "$(CURDIR)/ops:/host-ops:ro" \
		-v "$(CURDIR)/backups:/host-backups:ro" \
		--entrypoint python3 bagman-api \
		/host-ops/restore_objects.py "/host-backups/$(patsubst backups/%,%,$(OBJECTS_DIR))"

## Run the full CD-3 WI-4 acceptance evidence (restart, container
## rebuild, and restore-into-clean-target proofs — PID §43/§44/§35,
## §60 steps 12-20) against the REAL running stack, in the one order
## that lets the restore proof's final teardown be the single clean
## shutdown for all three (PID §60 step 20). Each script is also
## independently runnable directly — see tests/acceptance/README.md.
## Not part of `make test`/`pytest tests/ -q`: these mutate/replace the
## real bagman-db/bagman-objects/bagman-api runtime (including a full
## `docker compose down -v`) rather than using disposable fixtures, so
## they must be run deliberately, not on every ordinary test invocation.
accept:
	python3 tests/acceptance/restart_proof.py
	python3 tests/acceptance/container_rebuild_proof.py
	python3 tests/acceptance/restore_into_clean_target_proof.py
