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

.PHONY: build start stop status logs migrate test

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

# backup / restore: CD-3 WI-4's job (PID §34-35), not this work item.
# Deliberately no stub targets here yet — an empty/no-op `make backup`
# would be more misleading than a target that simply does not exist.
