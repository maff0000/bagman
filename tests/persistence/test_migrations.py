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


def test_every_canonical_primary_key_is_unique_by_construction(postgres_container):
    inspector = sa.inspect(get_engine())
    expected_pk_columns = {
        "governed_entities": ["entity_id"],
        "sources": ["source_id"],
        "external_references": ["external_reference_id"],
        "evidence_items": ["evidence_id"],
        "provenance": ["provenance_id"],
        "audit_events": ["audit_event_id"],
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
    }
    for table, cols in timestamp_columns.items():
        columns = {c["name"]: c for c in inspector.get_columns(table)}
        for col in cols:
            col_type = columns[col]["type"]
            assert getattr(col_type, "timezone", False) is True, (
                f"{table}.{col} must be TIMESTAMP WITH TIME ZONE, got {col_type!r}"
            )
