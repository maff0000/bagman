# bagman-api image (CD-3 WI-3, PID §7).
#
# Reproducible/versionable (PID §7): never tagged/promoted as `latest`
# anywhere in Compose (see deployment/compose/docker-compose.yml) —
# the same image is meant to be promotable between environments later,
# with only external configuration/secrets differing.
#
# GIT_COMMIT is supplied from OUTSIDE the build (a build arg), never
# computed by running `git` inside the container — this image has no
# git history of its own to inspect. See app/api/routers/version.py
# for how BAGMAN_GIT_COMMIT is read back out at request time.
FROM python:3.12-slim

ARG GIT_COMMIT=unknown
ENV BAGMAN_GIT_COMMIT=${GIT_COMMIT} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=UTC

WORKDIR /app

# Install dependencies before copying application code so a
# code-only change does not invalidate this layer's build cache.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Exactly the source the application needs at runtime (PID §51's
# directory-clarity expectation) — no tests/, no scripts/, no memory/.
COPY core/ ./core/
COPY services/ ./services/
COPY persistence/ ./persistence/
COPY contracts/ ./contracts/
COPY config/ ./config/
COPY app/ ./app/
# CD-5 WI-5 fix (2026-09-13): `ai/` (BAGMAN.AI — task contracts,
# invocation domain, the LiteLLM/Claude provider adapters, the
# background gateway, prompts, and this delivery's own evaluation
# harness) and `agent/` (BAGMAN.AGENT — Ask BAGMAN's orchestration loop
# and the governed tool registry) were never added here by WI-1/WI-2/
# WI-3/WI-4 — the whole of CD-5's AI backend was consequently ABSENT
# from every previously-built `bagman-api` image. `app/api/composition
# .py`'s production builder imports both packages directly
# (`ai.providers.litellm.client.LiteLLMClient`,
# `ai.gateway.background.run_background_task`,
# `ai.providers.claude.client.ClaudeClient`,
# `agent.tools.handlers.build_default_tool_registry`, ...), so every
# real `bagman-api` container built from the prior Dockerfile would
# have raised `ModuleNotFoundError` the moment `get_composition()`
# first built a PRODUCTION composition (lazy, on first request) —
# confirmed directly in this WI by building the prior Dockerfile and
# inspecting the resulting image's `/app` directory before this fix
# (neither `ai/` nor `agent/` was present). See the WI-5 delivery
# report for the full finding.
COPY ai/ ./ai/
COPY agent/ ./agent/
COPY alembic/ ./alembic/
COPY alembic.ini ./alembic.ini
COPY VERSION ./VERSION

COPY deployment/docker/api/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000

# Migrations are a separable, VISIBLE step before the application
# starts (PID §36) — see entrypoint.sh; this is not hidden inside
# apparent app startup.
ENTRYPOINT ["/entrypoint.sh"]
