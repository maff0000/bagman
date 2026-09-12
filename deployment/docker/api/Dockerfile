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
