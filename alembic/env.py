"""Alembic migration environment for BAGMAN's PostgreSQL schema (CD-3
WI-1, PID §12).

Reuses ``persistence.postgres.session.get_database_url()`` for the
connection string — the exact same logic
``persistence/postgres/session.py`` itself uses to build engines at
runtime — rather than duplicating any connection-string/env-var/
secret-file logic here (PL instruction).
"""
from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

from persistence.postgres.models import Base
from persistence.postgres.session import get_database_url

# Registers IntakeRecordRow on the shared `Base.metadata` (CD-4 WI-1) —
# intake_models.py lives in its own module (see its own docstring for
# why), so it must be imported somewhere before `target_metadata` is
# read below, or 'alembic revision --autogenerate' would never see
# `intake_records` at all.
from persistence.postgres import intake_models  # noqa: F401

# Registers AIInvocationRow on the shared `Base.metadata` (CD-5 WI-1) —
# same reason as intake_models above: ai_invocation_models.py lives in
# its own module, so it must be imported here or 'alembic revision
# --autogenerate' would never see `ai_invocations` at all.
from persistence.postgres import ai_invocation_models  # noqa: F401

# Registers XeroConnectionRow/XeroAccountRow/XeroSyncRunRow/
# XeroOAuthStateRow on the shared `Base.metadata` (CD-6 Slice 2) — same
# reason as intake_models/ai_invocation_models above: xero_models.py
# lives in its own module, so it must be imported here or
# 'alembic revision --autogenerate' would never see the `xero_*` tables
# at all. (Note: `needs_you_models` has this same gap, pre-existing
# from CD-6 Slice 1 — out of this WI's own scope to fix; flagged here
# for whoever next touches this file.)
from persistence.postgres import xero_models  # noqa: F401

# Registers EvidenceClassificationRuleRow/EvidenceClassificationRow on
# the shared `Base.metadata` (CD-6 Slice 5 WI-1) — same reason as
# intake_models/ai_invocation_models/xero_models above: these two
# modules each live in their own file, so they must be imported here or
# 'alembic revision --autogenerate' would never see the
# `evidence_classification_rules`/`evidence_classifications` tables at
# all. Rules module imported first — evidence_classification_models
# declares a real foreign key to it.
from persistence.postgres import evidence_classification_rule_models  # noqa: F401
from persistence.postgres import evidence_classification_models  # noqa: F401

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# BAGMAN's canonical PostgreSQL schema — one table per CD-2 canonical
# domain (persistence/postgres/models.py). Enables 'alembic revision
# --autogenerate' and is what 'alembic upgrade head' migrates towards.
target_metadata = Base.metadata

# Override the placeholder in alembic.ini with the real connection URL,
# built the same way persistence/postgres/session.py builds it for the
# application itself (env vars + a password read from the file named
# by BAGMAN_DB_PASSWORD_FILE — never a literal credential here or in
# alembic.ini; PID §29/§30).
config.set_main_option("sqlalchemy.url", get_database_url())


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
