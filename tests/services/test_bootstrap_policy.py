"""Pure-function proofs for ``services.mailbox.bootstrap_policy`` (CD-6
architect amendment, second correction — the fix for the real
conceptual-conflation bug the architect caught before any real
historical sweep had run: ``email_bootstrap_floor_at`` was wrongly
treated as the definitive literal historical-bootstrap date; the real
answer is DERIVED from ``fiscal_year_start_month_day`` + "now", clamped
by the renamed, optional ``historical_floor_override_at``).

These tests exercise the pure derivation functions directly — never via
a repository or ``services.mailbox.sweep.compute_bootstrap_floor``'s
own mailbox-scoping/fallback logic (that is proven separately in
``tests/integration/test_mailbox_sweep.py``'s own "compute_bootstrap_floor
— mailbox scoping" section). "now" throughout is pinned to
2026-09-18T00:00:00Z — the exact date the architect's own worked
examples (module docstring of ``services/mailbox/bootstrap_policy.py``)
were verified by hand against.
"""
from __future__ import annotations

import datetime as _dt

import pytest

from core.entity import GovernedEntity
from services.mailbox.bootstrap_policy import (
    compute_current_accounting_period_start,
    compute_entity_historical_bootstrap,
    compute_previous_completed_accounting_period_start,
)

_NOW = _dt.datetime(2026, 9, 18, tzinfo=_dt.timezone.utc)


def _entity(
    *,
    canonical_name: str,
    fiscal_year_start_month_day: str | None,
    historical_floor_override_at: _dt.datetime | None = None,
) -> GovernedEntity:
    """A minimal, directly-constructed ``GovernedEntity`` — these tests
    exercise pure functions, never a repository, so there is no need to
    go through ``register_entity``/contract validation here."""
    return GovernedEntity(
        entity_id="00000000-0000-7000-8000-000000000001",
        entity_type="COMPANY",
        canonical_name=canonical_name,
        display_name=canonical_name,
        status="ACTIVE",
        created_at=_NOW,
        fiscal_year_start_month_day=fiscal_year_start_month_day,
        historical_floor_override_at=historical_floor_override_at,
    )


# ---------------------------------------------------------------------
# 1. Infosecurs current period remains 2025-11-01
# ---------------------------------------------------------------------


def test_infosecurs_current_accounting_period_start_is_2025_11_01():
    assert compute_current_accounting_period_start("11-01", now=_NOW) == _dt.datetime(
        2025, 11, 1, tzinfo=_dt.timezone.utc
    )


# ---------------------------------------------------------------------
# 2. Infosecurs historical email bootstrap is 2024-11-01
# ---------------------------------------------------------------------


def test_infosecurs_historical_bootstrap_is_2024_11_01():
    infosecurs = _entity(
        canonical_name="INFOSECURS_LIMITED", fiscal_year_start_month_day="11-01", historical_floor_override_at=None
    )
    assert compute_entity_historical_bootstrap(infosecurs, now=_NOW) == _dt.datetime(
        2024, 11, 1, tzinfo=_dt.timezone.utc
    )


# ---------------------------------------------------------------------
# 3. Personal's current-tax-year-start and bootstrap-start remain
#    conceptually distinct — two genuinely separate computations, not
#    one field wearing two hats.
# ---------------------------------------------------------------------


def test_personal_current_period_start_and_historical_bootstrap_are_different_values():
    personal = _entity(
        canonical_name="MATTHEW_SCOTT_PERSONAL", fiscal_year_start_month_day="04-06", historical_floor_override_at=None
    )

    current_period_start = compute_current_accounting_period_start("04-06", now=_NOW)
    historical_bootstrap = compute_entity_historical_bootstrap(personal, now=_NOW)

    assert current_period_start == _dt.datetime(2026, 4, 6, tzinfo=_dt.timezone.utc)
    assert historical_bootstrap == _dt.datetime(2025, 4, 6, tzinfo=_dt.timezone.utc)
    assert current_period_start != historical_bootstrap  # two genuinely separate computations


# ---------------------------------------------------------------------
# 4. NoustAI bootstrap never predates incorporation — the max()-clamp
#    genuinely engages and wins, not just "the final answer happens to
#    be right".
# ---------------------------------------------------------------------


def test_noustai_naive_previous_period_alone_would_predate_incorporation():
    """The naive derivation, taken alone (no clamp applied), gives the
    WRONG answer — 2025-01-01, before the company existed. Proven
    directly so the next test's clamp is proven to matter, not merely
    coincide with the naive answer."""
    naive = compute_previous_completed_accounting_period_start("01-01", now=_NOW)
    assert naive == _dt.datetime(2025, 1, 1, tzinfo=_dt.timezone.utc)


def test_noustai_historical_bootstrap_is_clamped_to_its_real_incorporation_date():
    noustai = _entity(
        canonical_name="NOUSTAI_LIMITED",
        fiscal_year_start_month_day="01-01",
        historical_floor_override_at=_dt.datetime(2025, 12, 5, tzinfo=_dt.timezone.utc),
    )
    result = compute_entity_historical_bootstrap(noustai, now=_NOW)
    assert result == _dt.datetime(2025, 12, 5, tzinfo=_dt.timezone.utc)
    # The clamp actually WON over the (earlier, wrong) naive value —
    # not merely "the final answer happens to be correct".
    naive = compute_previous_completed_accounting_period_start("01-01", now=_NOW)
    assert result > naive


# ---------------------------------------------------------------------
# 6. Changing bootstrap policy does not mutate canonical accounting-
#    period facts — compute_entity_historical_bootstrap is a pure read.
# ---------------------------------------------------------------------


def test_computing_the_historical_bootstrap_never_mutates_the_entity_it_reads():
    from core.entity import InMemoryEntityRepository

    repo = InMemoryEntityRepository()
    registered = repo.register_entity(
        entity_type="COMPANY",
        canonical_name="PURITY_CHECK_LTD",
        display_name="Purity Check Ltd",
        status="ACTIVE",
        fiscal_year_start_month_day="11-01",
        historical_floor_override_at=None,
    )

    # Call the derivation repeatedly, with different `now` values, and
    # even feed its own output back in — none of this may touch the
    # repository's stored row.
    compute_entity_historical_bootstrap(registered, now=_NOW)
    compute_entity_historical_bootstrap(registered, now=_NOW + _dt.timedelta(days=200))
    compute_current_accounting_period_start("11-01", now=_NOW)
    compute_previous_completed_accounting_period_start("11-01", now=_NOW)

    refetched = repo.get_entity(registered.entity_id)
    assert refetched.fiscal_year_start_month_day == "11-01"
    assert refetched.historical_floor_override_at is None
    assert refetched == registered  # byte-identical — nothing on the stored row changed


# ---------------------------------------------------------------------
# Error handling — missing configuration, Feb-29 defensiveness
# ---------------------------------------------------------------------


def test_compute_entity_historical_bootstrap_raises_when_fiscal_year_start_is_not_configured():
    unconfigured = _entity(canonical_name="UNCONFIGURED_LTD", fiscal_year_start_month_day=None)
    with pytest.raises(ValueError):
        compute_entity_historical_bootstrap(unconfigured, now=_NOW)


def test_feb_29_fiscal_year_start_clamps_to_feb_28_on_a_non_leap_target_year_rather_than_raising():
    # 2026 is not a leap year — "now" itself is 2026-09-18, well past
    # 29 Feb of a leap year, so the current period's candidate start
    # falls in 2026 (clamped to 28 Feb) and the previous one in 2025
    # (also clamped).
    current = compute_current_accounting_period_start("02-29", now=_NOW)
    assert current == _dt.datetime(2026, 2, 28, tzinfo=_dt.timezone.utc)

    previous = compute_previous_completed_accounting_period_start("02-29", now=_NOW)
    assert previous == _dt.datetime(2025, 2, 28, tzinfo=_dt.timezone.utc)


def test_feb_29_fiscal_year_start_uses_the_real_29th_on_a_leap_target_year():
    leap_now = _dt.datetime(2028, 6, 1, tzinfo=_dt.timezone.utc)  # 2028 is a leap year
    current = compute_current_accounting_period_start("02-29", now=leap_now)
    assert current == _dt.datetime(2028, 2, 29, tzinfo=_dt.timezone.utc)
