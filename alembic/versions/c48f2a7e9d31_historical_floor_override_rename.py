"""rename governed_entities.email_bootstrap_floor_at to historical_floor_override_at (CD-6 architect amendment, second correction)

Revision ID: c48f2a7e9d31
Revises: a9c4e17f2b83
Create Date: 2026-09-18 00:00:00.000002

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c48f2a7e9d31'
down_revision: Union[str, Sequence[str], None] = 'a9c4e17f2b83'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # -- GovernedEntity: a pure column RENAME, nothing else. -----------
    #
    # The architect caught a real conceptual bug in `email_bootstrap_
    # floor_at`: it was treated, everywhere it was read, as "the
    # definitive literal historical-bootstrap date", set to each
    # entity's CURRENT accounting-period start. That is wrong — the
    # real historical-ingestion bootstrap boundary is a DERIVED value
    # (previous-COMPLETED-accounting-period-start, computed from
    # `fiscal_year_start_month_day` + "now"), and this column is only
    # ever an OPTIONAL CLAMP/OVERRIDE on that derivation (a real
    # commencement/incorporation date, when one exists) — never the
    # answer itself. See `core.entity.GovernedEntity
    # .historical_floor_override_at`'s own docstring, and
    # `services/mailbox/bootstrap_policy.py`'s module docstring, for
    # the full doctrine this rename encodes.
    #
    # DELIBERATE JUDGMENT CALL, flagged prominently (mirrors this
    # repository's own established style — see
    # `e5a2c917b6d4_mailbox_domain_gate.py` /
    # `a9c4e17f2b83_mailbox_folder_expansion.py` for the identical tone
    # on their own judgment calls): this migration performs ONLY the
    # schema-level column rename. It deliberately does NOT attempt to
    # correct or reinterpret any existing row's VALUE — no historical
    # sweep has ever actually run against a real value derived under
    # the OLD, wrong scheme (only a superseded 128-message acceptance
    # run under the even-older flat-7-day scheme has ever executed; no
    # real data depends on the value being corrected). Re-deriving and
    # writing the CORRECT value for each of the three real production
    # entities (Infosecurs Limited, NoustAI Limited, Matthew Scott
    # Personal) on the production Mac appliance is a deliberate,
    # separate, PL-executed live-data correction step performed AFTER
    # this migration deploys — not something a schema migration should
    # attempt blindly, and explicitly out of scope for this delivery.
    # A straight `ALTER TABLE ... RENAME COLUMN` preserves whatever
    # value is already stored under its new name unchanged; PL then
    # applies the real, verified correction directly.
    op.alter_column(
        'governed_entities',
        'email_bootstrap_floor_at',
        new_column_name='historical_floor_override_at',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column(
        'governed_entities',
        'historical_floor_override_at',
        new_column_name='email_bootstrap_floor_at',
    )
