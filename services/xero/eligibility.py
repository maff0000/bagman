"""Account-eligibility filtering policy (CD-6 Slice 2, architect spec
§5/§6) — "which synced accounts does the GUI's 'usable for coding a
purchase/expense' dropdown show BY DEFAULT".

A real, documented, easily-extensible function — never scattered inline
conditionals in a router or in JS (architect spec §5's explicit
instruction) — so the policy can be read, reasoned about, and extended
in exactly one place.

Judgment calls recorded here (architect spec §6: "document your own
judgment calls clearly")
------------------------------------------------------------------------
* **Status.** Xero's own `Status` is an OPEN, provider-supplied string
  (never a hand-typed closed enum — see `services.xero.account`'s own
  module docstring). This policy excludes an account from the DEFAULT
  view only when `status` is (case-insensitively) exactly `"ARCHIVED"`
  or `"DELETED"` — the two values Xero's own published Chart-of-Accounts
  documentation names as its closed lifecycle-end states. Any OTHER
  status value (including one this module has never seen before) is
  treated as eligible by default — architect spec §6's own "if
  uncertainty exists, expose more rather than silently invent
  accounting policy", applied literally: this module refuses to guess
  that an unfamiliar status value means "hide it".
* **Type/Class.** Rather than a strict ALLOWLIST of "known good"
  expense types (which would silently hide any type this module has
  never seen — exactly the "invent accounting policy" the architect
  spec forbids), this policy uses a small, explicit EXCLUSION list of
  types that are unambiguously never a valid expense/purchase coding
  target: `BANK` (a bank account itself — coding a purchase TO a bank
  account, rather than an expense/asset/liability category, is a
  structurally different operation Xero's own UI never offers either).
  Every other `type` value — including ones this module has never
  seen — is eligible by default. This is deliberately the more
  permissive of the two possible designs (allowlist vs. exclusion
  list); the architect spec's own "err toward showing more when
  genuinely unsure" instruction is the reason.
* **`ShowInExpenseClaims`.** Read as a POSITIVE signal only when
  present and `False` alongside a `type` that is not obviously
  expense-shaped — it deliberately does NOT drive this policy alone
  (architect spec's explicit "not solely `ShowInExpenseClaims`"
  instruction). Concretely: this field is not read by this policy's
  default-eligibility test at all today — it is exposed on every
  `XeroAccount` row for the GUI to use as a supplementary hint/sort
  signal if a later delivery wants one, but this function's own
  eligibility verdict does not depend on it, precisely so a company
  whose Xero data has this field unset/inconsistent never loses
  otherwise-legitimate accounts from its default list.
* **A historically-referenced account is NEVER hidden**, regardless of
  the above — see :func:`list_eligible_accounts`'s `referenced_account_ids`
  parameter. This is enforced at the LISTING layer, not by
  :func:`is_eligible_by_default` itself (which has no way to know what
  is referenced) — every caller that renders a picker list must go
  through :func:`list_eligible_accounts`, never call
  :func:`is_eligible_by_default` directly and drop the rest.
"""
from __future__ import annotations

from typing import Iterable

from services.xero.account import XeroAccount

#: Xero's own documented closed lifecycle-end status values (case
#: preserved as Xero returns them; compared case-insensitively below).
_EXCLUDED_STATUSES = frozenset({"ARCHIVED", "DELETED"})

#: The one `type` value unambiguously never a valid expense/purchase
#: coding target — see module docstring's "Type/Class" judgment call.
_EXCLUDED_TYPES = frozenset({"BANK"})


def is_eligible_by_default(account: XeroAccount) -> bool:
    """True if `account` should appear in the DEFAULT eligible-accounts
    view — see module docstring for the full, documented policy."""
    status = (account.status or "").strip().upper()
    if status in _EXCLUDED_STATUSES:
        return False

    account_type = (account.type or "").strip().upper()
    if account_type in _EXCLUDED_TYPES:
        return False

    return True


def list_eligible_accounts(
    accounts: Iterable[XeroAccount],
    *,
    referenced_account_ids: frozenset[str] = frozenset(),
) -> list[XeroAccount]:
    """The real, callable listing primitive every caller (the GUI's
    dropdown endpoint, a future AI-suggestion candidate-set builder)
    must go through — never call :func:`is_eligible_by_default` directly
    on a raw list and drop the rest, or the "never hide a historically
    referenced account" guarantee is lost.

    `referenced_account_ids` — the set of `account_id` values some real
    BAGMAN record already resolves to (architect spec §5/§6's "never
    silently hiding an account already referenced by a real BAGMAN
    record... a historical reference must always resolve, even to an
    account no longer in the default picker list") — is OR'd onto the
    ordinary eligibility test, so a referenced account always appears
    even if it would otherwise be excluded (e.g. it was subsequently
    archived in Xero). Callers with no referenced-account context yet
    (there are none in Slice 2 — no consequential record exists that
    codes a purchase to a Xero account today; this parameter exists for
    the invoice-coding delivery architect spec §6 names as this
    guarantee's real motivating future caller) simply omit it, which is
    the same as passing an empty set — ordinary default filtering only.

    Returns accounts sorted by `code` then `name` (a stable, predictable
    dropdown ordering), `code=None` sorting first.
    """
    eligible = [
        a for a in accounts if is_eligible_by_default(a) or a.account_id in referenced_account_ids
    ]
    return sorted(eligible, key=lambda a: (a.code or "", a.name))
