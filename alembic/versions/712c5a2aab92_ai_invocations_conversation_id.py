"""ai_invocations primary_input_reference conversation_id fallback (CD-6
reliability delta, PID §98/§100)

Revision ID: 712c5a2aab92
Revises: 9c2f4b1e7a05
Create Date: 2026-09-17 13:30:00.000000

Adds `conversation_id` as the fourth (last-precedence) fallback key in
`primary_input_reference`'s STORED GENERATED expression, so a genuinely
contextless Ask BAGMAN turn (no `evidence_id`/`intake_id`/`entity_id` —
the "hi bagman" case, PID §98/§100's own traceability ruling) still
gets a real subject the one-active-invocation concurrency guard (PID
§73) can key on, instead of the generated column evaluating to `NULL`.

PostgreSQL does not support altering a GENERATED column's expression
in place (`ALTER COLUMN ... SET EXPRESSION` does not exist) — the
column must be dropped and re-added. Dropping `primary_input_reference`
cascades to drop the partial unique index that references it
(`uq_ai_invocations_active_subject`) and the plain correlation/task
indexes do NOT reference this column so they are untouched; the unique
index is explicitly recreated afterward with the exact same
definition it had before (only the underlying column's VALUES change
going forward — the index's own shape is unchanged).

Existing rows: PostgreSQL recomputes a STORED GENERATED column for
every existing row when the column is re-added with `sa.Computed(...)`
(this is a full table rewrite, acceptable at this delivery's data
volume — see PID §100.1's own row counts, low hundreds). No existing
row's `primary_input_reference` value changes as a result of this
migration (none of them carry a `conversation_id` key yet — this
migration only adds a FOURTH fallback that is simply unreachable for
every row created before this delta shipped).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '712c5a2aab92'
down_revision: Union[str, Sequence[str], None] = '9c2f4b1e7a05'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Must stay in lockstep with
# persistence/postgres/ai_invocation_models.py's
# `_PRIMARY_INPUT_REFERENCE_SQL_EXPRESSION` and
# ai.invocation.PRIMARY_INPUT_REFERENCE_KEYS's precedence order.
_OLD_PRIMARY_INPUT_REFERENCE_SQL_EXPRESSION = (
    "COALESCE(input_references->>'evidence_id', "
    "input_references->>'intake_id', "
    "input_references->>'entity_id')"
)
_NEW_PRIMARY_INPUT_REFERENCE_SQL_EXPRESSION = (
    "COALESCE(input_references->>'evidence_id', "
    "input_references->>'intake_id', "
    "input_references->>'entity_id', "
    "input_references->>'conversation_id')"
)


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index('uq_ai_invocations_active_subject', table_name='ai_invocations')
    op.drop_column('ai_invocations', 'primary_input_reference')
    op.add_column(
        'ai_invocations',
        sa.Column(
            'primary_input_reference',
            sa.String(),
            sa.Computed(_NEW_PRIMARY_INPUT_REFERENCE_SQL_EXPRESSION, persisted=True),
            nullable=True,
        ),
    )
    op.create_index(
        'uq_ai_invocations_active_subject',
        'ai_invocations',
        ['task_id', 'task_version', 'primary_input_reference'],
        unique=True,
        postgresql_where=sa.text("status IN ('REQUESTED', 'RUNNING')"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_ai_invocations_active_subject', table_name='ai_invocations')
    op.drop_column('ai_invocations', 'primary_input_reference')
    op.add_column(
        'ai_invocations',
        sa.Column(
            'primary_input_reference',
            sa.String(),
            sa.Computed(_OLD_PRIMARY_INPUT_REFERENCE_SQL_EXPRESSION, persisted=True),
            nullable=True,
        ),
    )
    op.create_index(
        'uq_ai_invocations_active_subject',
        'ai_invocations',
        ['task_id', 'task_version', 'primary_input_reference'],
        unique=True,
        postgresql_where=sa.text("status IN ('REQUESTED', 'RUNNING')"),
    )
