"""Direct pytest proof of PID §12/§42/§63's migration requirements.

This session's own `postgres_container` fixture (see `conftest.py`)
has already run `alembic upgrade head` against a clean, empty
PostgreSQL database with NO manual SQL of any kind before any test in
this module runs. These tests assert the resulting schema and
Alembic's own bookkeeping are exactly what is expected, and that
upgrading an already-current database is a safe no-op.
"""
from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from persistence.postgres.session import get_engine

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

EXPECTED_TABLES = {
    "governed_entities",
    "sources",
    "external_references",
    "evidence_items",
    "provenance",
    "audit_events",
    "intake_records",  # CD-4 WI-1
    "ai_invocations",  # CD-5 WI-1
    "needs_you_items",  # CD-6 Slice 1
    "xero_connections",  # CD-6 Slice 2
    "xero_accounts",  # CD-6 Slice 2
    "xero_sync_runs",  # CD-6 Slice 2
    "xero_oauth_states",  # CD-6 Slice 2
}


def _alembic_config() -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return cfg


def test_all_six_canonical_tables_exist_after_a_clean_migration(postgres_container):
    inspector = sa.inspect(get_engine())
    assert EXPECTED_TABLES <= set(inspector.get_table_names())


def test_alembic_current_reports_exactly_one_applied_revision(postgres_container):
    with get_engine().connect() as conn:
        rows = conn.execute(sa.text("SELECT version_num FROM alembic_version")).fetchall()
    assert len(rows) == 1


def test_re_running_upgrade_head_is_a_safe_no_op(postgres_container):
    with get_engine().connect() as conn:
        before = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()

    command.upgrade(_alembic_config(), "head")

    with get_engine().connect() as conn:
        after = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()

    inspector = sa.inspect(get_engine())
    assert after == before
    assert set(inspector.get_table_names()) == EXPECTED_TABLES | {"alembic_version"}


def test_xero_migration_downgrade_genuinely_undoes_the_upgrade(postgres_container):
    """CD-6 Slice 2 migration safety proof: `5e8c1f42b9a7`'s own
    `downgrade()` genuinely removes exactly the four `xero_*` tables it
    added (and nothing else), and `upgrade("head")` genuinely restores
    them — a real round trip against a real database, not merely
    reading the migration file's source and trusting it by inspection.
    Restores the database to `head` again at the end (via the `finally`)
    so later tests in this module/session are unaffected.
    """
    cfg = _alembic_config()
    try:
        command.downgrade(cfg, "712c5a2aab92")
        inspector = sa.inspect(get_engine())
        tables_after_downgrade = set(inspector.get_table_names())
        assert "xero_connections" not in tables_after_downgrade
        assert "xero_accounts" not in tables_after_downgrade
        assert "xero_sync_runs" not in tables_after_downgrade
        assert "xero_oauth_states" not in tables_after_downgrade
        # Every OTHER canonical table must still be present — downgrade
        # must remove ONLY what this one migration added.
        assert (EXPECTED_TABLES - {"xero_connections", "xero_accounts", "xero_sync_runs", "xero_oauth_states"}) <= tables_after_downgrade

        command.upgrade(cfg, "head")
        inspector = sa.inspect(get_engine())
        tables_after_reupgrade = set(inspector.get_table_names())
        assert EXPECTED_TABLES <= tables_after_reupgrade
    finally:
        command.upgrade(cfg, "head")


def test_external_references_unique_constraint_exists_at_the_database_level(postgres_container):
    inspector = sa.inspect(get_engine())
    constraints = {
        (c["name"], tuple(c["column_names"]))
        for c in inspector.get_unique_constraints("external_references")
    }
    assert (
        "uq_external_references_tuple",
        ("provider", "source_id", "resource_type", "external_id"),
    ) in constraints


def test_evidence_items_entity_id_foreign_key_is_nullable_not_not_null(postgres_container):
    inspector = sa.inspect(get_engine())
    columns = {c["name"]: c for c in inspector.get_columns("evidence_items")}
    assert columns["entity_id"]["nullable"] is True

    fks = inspector.get_foreign_keys("evidence_items")
    entity_fk = next(fk for fk in fks if fk["constrained_columns"] == ["entity_id"])
    assert entity_fk["referred_table"] == "governed_entities"


def test_sources_has_no_foreign_keys_governed_entity_hint_is_a_hint_not_an_fk(postgres_container):
    inspector = sa.inspect(get_engine())
    assert inspector.get_foreign_keys("sources") == []


def test_provenance_evidence_id_foreign_key_exists(postgres_container):
    inspector = sa.inspect(get_engine())
    fks = inspector.get_foreign_keys("provenance")
    evidence_fk = next(fk for fk in fks if fk["constrained_columns"] == ["evidence_id"])
    assert evidence_fk["referred_table"] == "evidence_items"


def test_external_references_and_evidence_items_source_id_foreign_keys_exist(postgres_container):
    inspector = sa.inspect(get_engine())

    ext_ref_fks = inspector.get_foreign_keys("external_references")
    ext_ref_source_fk = next(fk for fk in ext_ref_fks if fk["constrained_columns"] == ["source_id"])
    assert ext_ref_source_fk["referred_table"] == "sources"

    evidence_fks = inspector.get_foreign_keys("evidence_items")
    evidence_source_fk = next(fk for fk in evidence_fks if fk["constrained_columns"] == ["source_id"])
    assert evidence_source_fk["referred_table"] == "sources"


def test_intake_records_source_and_evidence_foreign_keys_exist(postgres_container):
    """CD-4 WI-1: `intake_records.source_id` is a real FK (an intake
    attempt's source is resolved, not a hint); `intake_records.evidence_id`
    is a real, nullable FK (set only once REGISTERED)."""
    inspector = sa.inspect(get_engine())
    fks = inspector.get_foreign_keys("intake_records")

    source_fk = next(fk for fk in fks if fk["constrained_columns"] == ["source_id"])
    assert source_fk["referred_table"] == "sources"

    evidence_fk = next(fk for fk in fks if fk["constrained_columns"] == ["evidence_id"])
    assert evidence_fk["referred_table"] == "evidence_items"

    columns = {c["name"]: c for c in inspector.get_columns("intake_records")}
    assert columns["source_id"]["nullable"] is False
    assert columns["evidence_id"]["nullable"] is True
    assert columns["entity_hint"]["nullable"] is True  # hint only, PID §9/§10 — no FK at all


def test_every_canonical_primary_key_is_unique_by_construction(postgres_container):
    inspector = sa.inspect(get_engine())
    expected_pk_columns = {
        "governed_entities": ["entity_id"],
        "sources": ["source_id"],
        "external_references": ["external_reference_id"],
        "evidence_items": ["evidence_id"],
        "provenance": ["provenance_id"],
        "audit_events": ["audit_event_id"],
        "intake_records": ["intake_id"],
    }
    for table, expected_columns in expected_pk_columns.items():
        pk = inspector.get_pk_constraint(table)
        assert pk["constrained_columns"] == expected_columns


def test_all_canonical_timestamp_columns_are_timezone_aware(postgres_container):
    inspector = sa.inspect(get_engine())
    timestamp_columns = {
        "governed_entities": ["created_at"],
        "evidence_items": ["observed_at", "received_at", "created_at"],
        "external_references": ["first_observed_at"],
        "provenance": ["created_at"],
        "audit_events": ["occurred_at"],
        "intake_records": ["received_at", "completed_at"],
    }
    for table, cols in timestamp_columns.items():
        columns = {c["name"]: c for c in inspector.get_columns(table)}
        for col in cols:
            col_type = columns[col]["type"]
            assert getattr(col_type, "timezone", False) is True, (
                f"{table}.{col} must be TIMESTAMP WITH TIME ZONE, got {col_type!r}"
            )
